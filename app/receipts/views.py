"""Company-scoped pages for creating and reviewing local load receipts."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, FormView, ListView
from web3.exceptions import Web3Exception

from companies.models import CompanyMembership, UnitAccess
from companies.rules.company_access import CompanyAccessMixin
from companies.rules.role_access import (
    CompanyAction,
    require_company_action,
    require_receiving_access,
)

from .blockchain import (
    confirm_receipt_call,
    get_receipt_blockchain_context,
    mark_receipt_call_started,
    prepare_next_receipt_call,
    register_receipt_call_hash,
    reject_receipt_call,
)
from .documents import receipt_pdf_max_size
from .forms import ReceiptCreateForm
from .models import Receipt, ReceiptDocument
from .services import create_receipt


logger = logging.getLogger(__name__)


class ReceivingCompanyAccessMixin(CompanyAccessMixin):
    """Require an active receiving role before showing any company receipt page."""

    receiving_membership: CompanyMembership

    def dispatch(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """Render a clear 403 page when company or unit authorization is missing."""

        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied:
            # The company has already been resolved from an active membership by
            # CompanyContextMixin. Keep the denial inside this app and do not
            # expose the underlying permission rule or protected receipt data.
            return render(
                request,
                "receipts/permission_denied.html",
                {"company": getattr(self, "company", None)},
                status=403,
            )

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Verify the URL company and the receiving role through shared rules."""

        self.receiving_membership = require_company_action(
            request.user,
            self.company.pk,
            CompanyAction.RECEIVE_LOAD,
        )


class ReceiptListView(ReceivingCompanyAccessMixin, ListView):
    """List receipts only for units where the signed-in user can receive loads."""

    template_name = "receipts/receipt_list.html"
    context_object_name = "receipts"
    paginate_by = 25

    def get_queryset(self) -> QuerySet[Receipt]:
        """Apply company and active unit-receiving filters at the database layer."""

        accessible_unit_ids = UnitAccess.objects.filter(
            membership=self.receiving_membership,
            active=True,
            can_receive=True,
            unit__active=True,
        ).values_list("unit_id", flat=True)
        return (
            Receipt.objects.filter(company=self.company, unit_id__in=accessible_unit_ids)
            .select_related("participant", "unit", "created_by", "weighing_ticket")
            .order_by("-created_at", "-pk")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show whether the operator has an eligible registered unit and participant."""

        context = super().get_context_data(**kwargs)
        context["has_receiving_units"] = UnitAccess.objects.filter(
            membership=self.receiving_membership,
            active=True,
            can_receive=True,
            unit__active=True,
            unit__chain_unit_id__gt=0,
        ).exists()
        return context


class ReceiptCreateView(ReceivingCompanyAccessMixin, FormView):
    """Collect weighing, crop, participant and document details for one local load."""

    template_name = "receipts/receipt_form.html"
    form_class = ReceiptCreateForm

    def get_form_kwargs(self) -> dict[str, Any]:
        """Pass only URL-validated company context into restricted choice fields."""

        kwargs = super().get_form_kwargs()
        kwargs.update(
            {
                "company_id": self.company.pk,
                "membership": self.receiving_membership,
            }
        )
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the private PDF limit and explain the local-only workflow state."""

        context = super().get_context_data(**kwargs)
        context["pdf_limit_megabytes"] = receipt_pdf_max_size() / (1024 * 1024)
        context["has_eligible_units"] = context["form"].fields["unit"].queryset.exists()
        context["has_confirmed_participants"] = (
            context["form"].fields["participant"].queryset.exists()
        )
        return context

    def get_success_url(self) -> str:
        """Return to the newly created receipt's company-scoped detail page."""

        if not hasattr(self, "created_receipt"):
            raise RuntimeError("The receipt must be saved before resolving its detail URL.")
        return reverse(
            "receipts:detail",
            kwargs={"company_id": self.company.pk, "receipt_id": self.created_receipt.pk},
        )

    def form_valid(self, form: ReceiptCreateForm) -> HttpResponse:
        """Persist the form through the domain service and redirect to its result."""

        data = form.cleaned_data
        try:
            self.created_receipt = create_receipt(
                user=self.request.user,
                company_id=self.company.pk,
                participant_id=data["participant"].pk,
                unit_id=data["unit"].pk,
                crop_year=data["crop_year"],
                gross_weight_kg=data["gross_weight_kg"],
                tare_weight_kg=data["tare_weight_kg"],
                moisture_percent=data["moisture_percent"],
                moisture_deduction_kg=data["moisture_deduction_kg"],
                impurities_percent=data["impurities_percent"],
                impurities_deduction_kg=data["impurities_deduction_kg"],
                damaged_grain_percent=data["damaged_grain_percent"],
                damaged_grain_deduction_kg=data["damaged_grain_deduction_kg"],
                other_deductions_kg=data["other_deductions_kg"],
                weighed_at=data["weighed_at"],
                invoice_access_key=data["invoice_access_key"],
                weighing_ticket_pdf=data["weighing_ticket_pdf"],
                classification_report_pdf=data.get("classification_report_pdf"),
                invoice_pdf=data["invoice_pdf"],
            )
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                for field_name, field_errors in error.message_dict.items():
                    for message in field_errors:
                        form.add_error(field_name if field_name in form.fields else None, message)
            else:
                for message in error.messages:
                    form.add_error(None, message)
            return self.form_invalid(form)

        messages.success(
            self.request,
            "Recebimento salvo no SaaS. Nenhuma transação foi enviada à blockchain.",
        )
        return redirect(self.get_success_url())


class ReceiptDetailView(ReceivingCompanyAccessMixin, DetailView):
    """Show local receipt data after rechecking access to its specific unit."""

    template_name = "receipts/receipt_detail.html"
    context_object_name = "receipt"
    pk_url_kwarg = "receipt_id"

    def get_object(self, queryset: QuerySet[Receipt] | None = None) -> Receipt:
        """Load the receipt inside company scope, then require its unit grant."""

        receipt = get_object_or_404(
            Receipt.objects.select_related(
                "company",
                "participant",
                "unit",
                "created_by",
                "weighing_ticket",
                "tax_invoice",
            ).prefetch_related("documents"),
            pk=self.kwargs[self.pk_url_kwarg],
            company=self.company,
        )
        require_receiving_access(
            self.request.user,
            self.company.pk,
            receipt.unit_id,
        )
        return receipt

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show the ordered transaction state and eligible saved networks."""

        context = super().get_context_data(**kwargs)
        context.update(get_receipt_blockchain_context(self.object))
        return context


class ReceiptBlockchainActionView(ReceivingCompanyAccessMixin, View):
    """Handle CSRF-protected receipt transaction actions as JSON responses."""

    http_method_names = ["post"]

    def _response_for_attempt(self, attempt: Any, *, status: int = 200) -> JsonResponse:
        """Return safe transaction lifecycle data without exposing private records."""

        transaction_record = attempt.transaction
        return JsonResponse(
            {
                "attempt_id": attempt.pk,
                "call_kind": attempt.call_kind,
                "status": transaction_record.status,
                "transaction_hash": transaction_record.transaction_hash,
                "error": transaction_record.error_message,
            },
            status=status,
        )

    def post(
        self,
        request: HttpRequest,
        company_id: int,
        receipt_id: int,
        attempt_id: int | None = None,
        action: str | None = None,
    ) -> JsonResponse:
        """Dispatch one requested preparation, wallet, hash, or polling action."""

        try:
            if action is None:
                network_id = int(request.POST.get("network_id", ""))
                prepared = prepare_next_receipt_call(
                    user=request.user,
                    company_id=company_id,
                    receipt_id=receipt_id,
                    network_id=network_id,
                    sender_address=request.POST.get("sender_address", ""),
                )
                return JsonResponse(
                    {
                        "status": prepared.status,
                        "attempt_id": prepared.attempt.pk,
                        "call_kind": prepared.attempt.call_kind,
                        "chain_id": prepared.chain_id,
                        "contract_address": prepared.contract_address,
                        "transaction_data": prepared.transaction_data,
                        "transaction_hash": prepared.attempt.transaction.transaction_hash,
                    }
                )

            if attempt_id is None:
                return JsonResponse({"error": "Identificador de tentativa ausente."}, status=400)

            if action == "started":
                attempt = mark_receipt_call_started(
                    user=request.user,
                    company_id=company_id,
                    receipt_id=receipt_id,
                    attempt_id=attempt_id,
                    sender_address=request.POST.get("sender_address", ""),
                )
                return self._response_for_attempt(attempt)
            if action == "rejected":
                attempt = reject_receipt_call(
                    user=request.user,
                    company_id=company_id,
                    receipt_id=receipt_id,
                    attempt_id=attempt_id,
                )
                return self._response_for_attempt(attempt)
            if action == "hash":
                record = register_receipt_call_hash(
                    user=request.user,
                    company_id=company_id,
                    receipt_id=receipt_id,
                    attempt_id=attempt_id,
                    transaction_hash=request.POST.get("transaction_hash", ""),
                )
                if record is None:
                    return JsonResponse(
                        {
                            "status": "pending",
                            "message": "O RPC ainda não encontrou esta hash. A tentativa foi preservada.",
                        }
                    )
                attempt = record.receipt_chain_attempt
                return self._response_for_attempt(attempt)
            if action == "confirm":
                result = confirm_receipt_call(
                    user=request.user,
                    company_id=company_id,
                    receipt_id=receipt_id,
                    attempt_id=attempt_id,
                )
                return JsonResponse(
                    {
                        "status": result.status,
                        "attempt_id": result.attempt.pk,
                        "transaction_hash": result.transaction.transaction_hash,
                        "deposit_id": result.deposit_id,
                        "error": result.transaction.error_message,
                    }
                )
            return JsonResponse({"error": "Ação de transação desconhecida."}, status=404)
        except (TypeError, ValueError):
            return JsonResponse({"error": "Os dados enviados estão incompletos ou inválidos."}, status=400)
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                details = [message for values in error.message_dict.values() for message in values]
            else:
                details = error.messages
            return JsonResponse({"error": " ".join(details)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)
        except IntegrityError:
            return JsonResponse(
                {"error": "Outra solicitação reservou esta etapa. Atualize a página."},
                status=409,
            )
        except (ConnectionError, Web3Exception) as error:
            logger.warning("Receipt blockchain RPC action failed: %s", error)
            return JsonResponse(
                {"error": "Não foi possível consultar a rede. A tentativa foi preservada; tente novamente."},
                status=503,
            )


class ReceiptDocumentFileView(ReceivingCompanyAccessMixin, View):
    """Serve a receipt PDF only after company, receipt and unit authorization."""

    def get(
        self,
        request: HttpRequest,
        company_id: int,
        receipt_id: int,
        document_id: int,
    ) -> FileResponse:
        """Stream a private PDF with browser sniffing and caching disabled."""

        receipt = get_object_or_404(
            Receipt.objects.select_related("unit"),
            pk=receipt_id,
            company=self.company,
        )
        require_receiving_access(request.user, self.company.pk, receipt.unit_id)
        document = get_object_or_404(
            ReceiptDocument,
            pk=document_id,
            receipt=receipt,
        )
        try:
            pdf_file = document.pdf_file.open("rb")
        except OSError as error:
            logger.exception("A private receipt PDF is missing from storage.")
            raise Http404("O documento deste recebimento não está disponível.") from error

        response = FileResponse(
            pdf_file,
            content_type="application/pdf",
            as_attachment=False,
            filename=document.original_filename,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Content-Security-Policy"] = "frame-ancestors 'self'"
        response["Referrer-Policy"] = "no-referrer"
        return response
