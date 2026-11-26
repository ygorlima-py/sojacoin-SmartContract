"""HTTP endpoints for issuance batch creation and remote participant approval."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from web3.exceptions import Web3Exception

from blockchain.models import BlockchainTransaction
from companies.rules.company_access import CompanyAccessMixin
from companies.rules.role_access import CompanyAction, require_company_action
from receipts.models import ReceiptDocument

from .models import IssuanceBatch, IssuanceBatchAttempt
from .services import (
    cancel_approval_link,
    confirm_batch_transaction,
    create_approval_link,
    get_batch_issuance_display,
    get_approval_link,
    mark_batch_action_started,
    prepare_batch_cancellation,
    prepare_issuance_batch,
    prepare_participant_approval_and_issue,
    register_batch_transaction_hash,
    reject_batch_action,
)


logger = logging.getLogger(__name__)


def _validation_messages(error: ValidationError) -> str:
    """Flatten Django field and non-field validation messages for JSON clients."""

    if hasattr(error, "message_dict"):
        return " ".join(message for values in error.message_dict.values() for message in values)
    return " ".join(error.messages)


def _prepared_json(prepared: Any) -> JsonResponse:
    """Serialize only the transaction request required by the browser wallet."""

    attempt = prepared.attempt
    return JsonResponse(
        {
            "batch_id": prepared.batch.pk,
            "attempt_id": attempt.pk,
            "kind": attempt.kind,
            "status": prepared.status,
            "chain_id": prepared.chain_id,
            "contract_address": prepared.contract_address,
            "transaction_data": prepared.transaction_data,
            "transaction_hash": attempt.transaction.transaction_hash,
        }
    )


def _action_response(
    *, attempt: IssuanceBatchAttempt, status: str | None = None, pending_message: str = ""
) -> JsonResponse:
    """Serialize a reconciled action without returning stored calldata or signatures."""

    # Refresh related rows too: reconciliation may have changed the transaction
    # and batch after this view loaded the attempt.
    attempt.refresh_from_db()
    transaction = attempt.transaction
    transaction.refresh_from_db()
    batch = attempt.batch
    batch.refresh_from_db()
    payload: dict[str, Any] = {
        "attempt_id": attempt.pk,
        "kind": attempt.kind,
        "status": status or transaction.status,
        "transaction_hash": transaction.transaction_hash,
        "batch_status": batch.status,
        "chain_batch_id": batch.chain_batch_id,
        "error": transaction.error_message,
    }
    if pending_message:
        payload["message"] = pending_message
    return JsonResponse(payload)


class IssuanceCompanyActionView(CompanyAccessMixin, View):
    """Protect a company-scoped write endpoint with the active issuance role."""

    http_method_names = ["post"]

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Require the issuance role before a request reaches its handler."""

        require_company_action(request.user, self.company.pk, CompanyAction.CREATE_ISSUANCE)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Return JSON permission errors because the caller is an asynchronous form."""

        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)


class PrepareBatchView(IssuanceCompanyActionView):
    """Validate a confirmed receipt and persist its create-batch wallet request."""

    def post(self, request: HttpRequest, company_id: int, receipt_id: int) -> JsonResponse:
        """Prepare one batch without transmitting a blockchain transaction."""

        try:
            deadline = int(request.POST.get("deadline", ""))
            prepared = prepare_issuance_batch(
                user=request.user,
                company_id=self.company.pk,
                receipt_id=receipt_id,
                deadline=deadline,
                sender_address=request.POST.get("sender_address", ""),
            )
            return _prepared_json(prepared)
        except (TypeError, ValueError):
            return JsonResponse({"error": "Informe um prazo de aprovação válido."}, status=400)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)
        except (ConnectionError, Web3Exception) as error:
            logger.warning("Issuance batch preparation RPC failed: %s", error)
            return JsonResponse({"error": "Não foi possível consultar a rede. Tente novamente."}, status=503)


class BatchAttemptActionView(IssuanceCompanyActionView):
    """Handle a staff wallet prompt, transaction hash, or confirmation poll."""

    def post(
        self,
        request: HttpRequest,
        company_id: int,
        batch_id: int,
        attempt_id: int,
        action: str,
    ) -> JsonResponse:
        """Scope each action to the batch and recheck its operator authorization."""

        attempt = get_object_or_404(
            IssuanceBatchAttempt.objects.select_related("batch__participant", "transaction"),
            pk=attempt_id,
            batch_id=batch_id,
            batch__company_id=self.company.pk,
        )
        try:
            if action == "started":
                attempt = mark_batch_action_started(
                    attempt=attempt,
                    sender_address=request.POST.get("sender_address", ""),
                    user=request.user,
                )
                return _action_response(attempt=attempt)
            if action == "rejected":
                attempt = reject_batch_action(attempt=attempt, user=request.user)
                return _action_response(attempt=attempt)
            if action == "hash":
                record = register_batch_transaction_hash(
                    attempt=attempt,
                    transaction_hash=request.POST.get("transaction_hash", ""),
                    user=request.user,
                )
                if record is None:
                    return _action_response(
                        attempt=attempt,
                        status="pending",
                        pending_message="O RPC ainda não encontrou esta hash. A tentativa foi preservada.",
                    )
                attempt.refresh_from_db()
                return _action_response(attempt=attempt)
            if action == "confirm":
                result = confirm_batch_transaction(attempt=attempt, user=request.user)
                attempt.refresh_from_db()
                return _action_response(attempt=attempt, status=result.status)
            return JsonResponse({"error": "Ação de transação desconhecida."}, status=404)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)
        except (ConnectionError, Web3Exception) as error:
            logger.warning("Issuance batch action RPC failed: %s", error)
            return JsonResponse({"error": "A rede não respondeu. A tentativa foi preservada."}, status=503)


class ApprovalLinkView(IssuanceCompanyActionView):
    """Create a one-time participant approval URL after batch creation is confirmed."""

    def post(self, request: HttpRequest, company_id: int, batch_id: int) -> JsonResponse:
        """Return a bearer URL once; the database stores only its token digest."""

        try:
            link, token = create_approval_link(
                user=request.user,
                company_id=self.company.pk,
                batch_id=batch_id,
            )
            url = request.build_absolute_uri(
                reverse("issuance_batches:participant-approval", kwargs={"token": token})
            )
            return JsonResponse({"url": url, "expires_at": link.expires_at.isoformat()})
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)


class CancelApprovalLinkView(IssuanceCompanyActionView):
    """Revoke an unused participant bearer link without deleting its history."""

    def post(self, request: HttpRequest, company_id: int, batch_id: int, link_id: int) -> JsonResponse:
        """Cancel only a link belonging to the authorized batch and company."""

        try:
            link = cancel_approval_link(
                user=request.user,
                company_id=self.company.pk,
                batch_id=batch_id,
                link_id=link_id,
            )
            return JsonResponse({"status": "canceled", "link_id": link.pk})
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)


class PrepareBatchCancellationView(IssuanceCompanyActionView):
    """Prepare an on-chain cancel call by a verified, authorized staff wallet."""

    def post(self, request: HttpRequest, company_id: int, batch_id: int) -> JsonResponse:
        """Persist cancellation calldata before asking MetaMask to send it."""

        batch = get_object_or_404(IssuanceBatch, pk=batch_id, company_id=self.company.pk)
        try:
            prepared = prepare_batch_cancellation(
                batch=batch,
                sender_address=request.POST.get("sender_address", ""),
                user=request.user,
                requested_by=request.user,
            )
            return _prepared_json(prepared)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)
        except (ConnectionError, Web3Exception) as error:
            logger.warning("Issuance batch cancellation preparation failed: %s", error)
            return JsonResponse({"error": "Não foi possível consultar a rede. Tente novamente."}, status=503)


class ParticipantApprovalView(View):
    """Present one batch to its named participant through a private bearer URL."""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        """Render the batch snapshot and only the documents attached to its receipts."""

        try:
            link = get_approval_link(
                token,
                allow_completed=True,
                allow_in_progress=True,
                allow_cancel=True,
            )
        except ValidationError as error:
            return render(request, "issuance_batches/link_unavailable.html", {"error": _validation_messages(error)}, status=410)
        batch = link.batch
        attempts = batch.attempts.select_related("transaction").order_by("created_at", "pk")
        current_link = link
        context = {
            "batch": batch,
            "link": current_link,
            "receipts": [row.receipt for row in batch.receipt_rows.select_related(
                "receipt__weighing_ticket", "receipt__tax_invoice", "receipt__unit", "receipt__participant"
            ).prefetch_related("receipt__documents")],
            "attempts": attempts,
            "prepare_approval_url": reverse("issuance_batches:participant-prepare", kwargs={"token": token}),
            "prepare_cancel_url": reverse("issuance_batches:participant-cancel-prepare", kwargs={"token": token}),
            "attempt_url_prefix": reverse(
                "issuance_batches:participant-approval", kwargs={"token": token}
            ).rstrip("/") + "/attempt/",
            "link_token": token,
            "approval_available": batch.status == IssuanceBatch.Status.AWAITING_APPROVAL and not batch.approval_expired and link.is_usable,
            "cancellation_available": (
                batch.status == IssuanceBatch.Status.AWAITING_APPROVAL
                and link.canceled_at is None
                and link.expires_at > timezone.now()
            ),
            "expired": batch.approval_expired or link.expires_at <= timezone.now(),
            "issuance_display": get_batch_issuance_display(batch),
        }
        response = render(request, "issuance_batches/participant_approval.html", context)
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "no-referrer"
        return response


class ParticipantPublicActionView(View):
    """Base JSON endpoint for the participant using a bearer approval link."""

    http_method_names = ["post"]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Convert expected authorization failures into safe JSON responses."""

        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error)}, status=403)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)


class PrepareParticipantApprovalView(ParticipantPublicActionView):
    """Persist the participant's single-call approval and issuance request."""

    def post(self, request: HttpRequest, token: str) -> JsonResponse:
        """Validate wallet and network, then save exact calldata before MetaMask opens."""

        try:
            chain_id = int(request.POST.get("chain_id", ""))
            prepared = prepare_participant_approval_and_issue(
                token=token,
                wallet_address=request.POST.get("wallet_address", ""),
                wallet_chain_id=chain_id,
            )
            return _prepared_json(prepared)
        except (TypeError, ValueError):
            return JsonResponse({"error": "A rede informada é inválida."}, status=400)


class PrepareParticipantCancellationView(ParticipantPublicActionView):
    """Prepare participant cancellation, including after approval expiry."""

    def post(self, request: HttpRequest, token: str) -> JsonResponse:
        """Let only the participant wallet request the contract's cancel action."""

        link = get_approval_link(token, allow_cancel=True)
        try:
            prepared = prepare_batch_cancellation(
                batch=link.batch,
                sender_address=request.POST.get("sender_address", ""),
                token=token,
            )
            return _prepared_json(prepared)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)


class ParticipantAttemptActionView(ParticipantPublicActionView):
    """Start, recover, or poll a participant-owned transaction from its link."""

    def post(self, request: HttpRequest, token: str, attempt_id: int, action: str) -> JsonResponse:
        """Scope the transaction attempt to the batch selected by the bearer token."""

        link = get_approval_link(
            token,
            allow_completed=True,
            allow_in_progress=True,
            allow_cancel=True,
        )
        attempt = get_object_or_404(
            IssuanceBatchAttempt.objects.select_related("batch__participant", "transaction"),
            pk=attempt_id,
            batch_id=link.batch_id,
        )
        try:
            if action == "started":
                mark_batch_action_started(
                    attempt=attempt,
                    sender_address=request.POST.get("sender_address", ""),
                    token=token,
                )
                attempt.refresh_from_db()
                return _action_response(attempt=attempt)
            if action == "rejected":
                attempt = reject_batch_action(attempt=attempt, token=token)
                return _action_response(attempt=attempt)
            if action == "hash":
                record = register_batch_transaction_hash(
                    attempt=attempt,
                    transaction_hash=request.POST.get("transaction_hash", ""),
                    token=token,
                )
                if record is None:
                    return _action_response(attempt=attempt, status="pending", pending_message="O RPC ainda não encontrou a hash. A tentativa foi preservada.")
                attempt.refresh_from_db()
                return _action_response(attempt=attempt)
            if action == "confirm":
                result = confirm_batch_transaction(attempt=attempt, token=token)
                attempt.refresh_from_db()
                return _action_response(attempt=attempt, status=result.status)
            return JsonResponse({"error": "Ação desconhecida."}, status=404)
        except ValidationError as error:
            return JsonResponse({"error": _validation_messages(error)}, status=400)
        except (ConnectionError, Web3Exception) as error:
            logger.warning("Participant batch transaction RPC failed: %s", error)
            return JsonResponse({"error": "A rede não respondeu. A tentativa foi preservada."}, status=503)


class ParticipantReceiptDocumentView(View):
    """Serve one batch receipt PDF only while its bearer link is valid."""

    def get(self, request: HttpRequest, token: str, receipt_id: int, document_id: int) -> FileResponse:
        """Verify link-to-batch-to-receipt relationships before streaming a PDF."""

        try:
            link = get_approval_link(
                token,
                allow_completed=True,
                allow_in_progress=True,
                allow_cancel=True,
            )
        except ValidationError as error:
            raise Http404("O link para este documento expirou.") from error
        row = get_object_or_404(
            link.batch.receipt_rows.select_related("receipt"),
            receipt_id=receipt_id,
        )
        document = get_object_or_404(ReceiptDocument, pk=document_id, receipt=row.receipt)
        try:
            pdf_file = document.pdf_file.open("rb")
        except OSError as error:
            raise Http404("O documento deste recebimento não está disponível.") from error
        response = FileResponse(pdf_file, content_type="application/pdf", as_attachment=False, filename=document.original_filename)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Content-Security-Policy"] = "frame-ancestors 'self'"
        response["Referrer-Policy"] = "no-referrer"
        return response
