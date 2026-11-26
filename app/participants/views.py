"""Company participant screens and public terms-acceptance endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.db.models import QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView
from web3.exceptions import Web3Exception

from blockchain.models import BlockchainTransaction, ContractInstance, Network
from companies.models import MembershipRole
from companies.rules.company_access import CompanyAccessMixin
from companies.rules.role_access import (
    require_administrator_access,
    require_registration_access,
)

from .documents import participant_terms_pdf_max_size_label
from .forms import ParticipantForm, ParticipantTermsDocumentUploadForm
from .models import (
    Participant,
    ParticipantRegistrationAttempt,
    ParticipantTerms,
    ParticipantTermsDocument,
)
from .services import (
    confirm_participant_attempt,
    cancel_participant_acceptance_link,
    create_participant,
    create_participant_acceptance_challenge,
    create_participant_acceptance_link,
    current_acceptance_link,
    get_acceptance_attempt_for_link,
    get_participant_acceptance_link,
    get_participant_attempt_for_company,
    get_current_participant_terms_document,
    get_participant_soja_coin_networks,
    list_participant_terms_documents,
    list_participant_attempts,
    mark_participant_submission_started,
    prepare_participant_acceptance,
    prepare_participant_proposal,
    publish_participant_terms_pdf,
    register_participant_transaction_hash,
    reject_participant_attempt,
)


logger = logging.getLogger(__name__)


def _participant_terms_pdf_response(document: ParticipantTermsDocument) -> FileResponse:
    """Stream a stored PDF inline without exposing its storage URL."""

    try:
        pdf_file = document.pdf_file.open("rb")
    except OSError as error:
        logger.exception("Published participant terms PDF is missing from private storage.")
        raise Http404("O PDF publicado não está disponível no armazenamento.") from error

    response = FileResponse(
        pdf_file,
        content_type="application/pdf",
        as_attachment=False,
        filename=f"termos-do-participante-v{document.version}.pdf",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Content-Security-Policy"] = "frame-ancestors 'self'"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _request_data(request: HttpRequest) -> dict[str, Any]:
    """Read a JSON object or regular form body without trusting extra fields."""

    if request.content_type == "application/json":
        try:
            value = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValidationError("Os dados enviados não são um JSON válido.") from error
        if not isinstance(value, dict):
            raise ValidationError("Os dados enviados precisam ser um objeto.")
        return value
    return request.POST.dict()


def _json_error(error: Exception) -> JsonResponse:
    """Return a user-readable error without disclosing RPC credentials."""

    if isinstance(error, PermissionDenied):
        return JsonResponse({"error": str(error) or "Ação não autorizada."}, status=403)
    if isinstance(error, ValidationError):
        if hasattr(error, "message_dict"):
            messages_text = [
                str(message)
                for errors in error.message_dict.values()
                for message in errors
            ]
        else:
            messages_text = [str(message) for message in error.messages]
        return JsonResponse(
            {"error": " ".join(messages_text) or "Confira os dados enviados."},
            status=400,
        )
    if isinstance(error, (ConnectionError, ImproperlyConfigured, Web3Exception)):
        logger.warning("Participant operation could not reach its configured RPC: %s", error)
        return JsonResponse(
            {"error": "Não foi possível consultar a rede agora. A tentativa foi preservada."},
            status=502,
        )
    raise error


def _positive_int(value: object, label: str) -> int:
    """Parse a positive integer from a browser request."""

    try:
        number = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValidationError(f"Informe um {label} válido.") from error
    if number < 1:
        raise ValidationError(f"Informe um {label} válido.")
    return number


def _attempt_json(attempt: ParticipantRegistrationAttempt) -> dict[str, Any]:
    """Serialize the public wallet request for a saved attempt."""

    record = attempt.transaction
    return {
        "attempt_id": attempt.pk,
        "transaction_id": record.pk,
        "status": record.status,
        "chain_id": record.network.chain_id,
        "from": attempt.sender_address,
        "to": record.contract.address_normalized,
        "data": attempt.prepared_data,
        "send_allowed": (
            record.status == BlockchainTransaction.Status.PREPARED
            and attempt.submission_started_at is None
            and attempt.is_open
        ),
        "transaction_hash": record.transaction_hash or "",
    }


class ParticipantListView(CompanyAccessMixin, ListView):
    """List participants only within the company authorized by the URL."""

    template_name = "participants/participant_list.html"
    context_object_name = "participants"
    paginate_by = 30

    def get_queryset(self) -> QuerySet[Participant]:
        """Keep company scoping in the database query, not in the template."""

        return Participant.objects.filter(company=self.company).select_related(
            "created_by"
        ).prefetch_related("terms_versions")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the company and whether this member can prepare a proposal."""

        context = super().get_context_data(**kwargs)
        context["company"] = self.company
        context["current_terms_document"] = get_current_participant_terms_document(
            self.company.pk
        )
        context["can_prepare_participant"] = (
            self.company.active
            and self.membership.roles.filter(
                active=True,
                role__in=(
                    MembershipRole.Role.ADMINISTRATOR,
                    MembershipRole.Role.REGISTRATION,
                ),
            ).exists()
        )
        return context


class ParticipantCreateView(CompanyAccessMixin, FormView):
    """Create a participant record using the company's current published PDF."""

    template_name = "participants/participant_form.html"
    form_class = ParticipantForm

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Only a registration operator or company administrator may add one."""

        require_registration_access(request.user, self.company.pk)

    def get_form_kwargs(self) -> dict[str, Any]:
        """Supply the URL-authorized company for address uniqueness checks."""

        kwargs = super().get_form_kwargs()
        kwargs["company_id"] = self.company.pk
        return kwargs

    def get_initial(self) -> dict[str, Any]:
        """Pin the form to the PDF version displayed on the page."""

        initial = super().get_initial()
        document = get_current_participant_terms_document(self.company.pk)
        initial["terms_document_id"] = document.pk if document else ""
        return initial

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show which immutable terms version will be attached to this participant."""

        context = super().get_context_data(**kwargs)
        context["company"] = self.company
        context["current_terms_document"] = get_current_participant_terms_document(
            self.company.pk
        )
        return context

    def form_valid(self, form: ParticipantForm) -> HttpResponse:
        """Create participant and its first term through the domain service."""

        try:
            participant, terms = create_participant(
                user=self.request.user,
                company_id=self.company.pk,
                name=form.cleaned_data["name"],
                wallet_address=form.cleaned_data["wallet_address"],
                terms_document_id=form.cleaned_data.get("terms_document_id"),
            )
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                for field, errors in error.message_dict.items():
                    if field in form.fields:
                        for message in errors:
                            form.add_error(field, message)
                    else:
                        for message in errors:
                            form.add_error(None, message)
            else:
                for message in error.messages:
                    form.add_error(None, message)
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"Participante cadastrado com a versão {terms.version} do PDF dos termos.",
        )
        return redirect(
            "participants:detail",
            company_id=self.company.pk,
            participant_id=participant.pk,
        )


class ParticipantTermsDocumentsView(CompanyAccessMixin, FormView):
    """Let the company's active administrator publish and review terms PDFs."""

    template_name = "participants/terms_documents.html"
    form_class = ParticipantTermsDocumentUploadForm

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Limit publication and version history to the active company administrator."""

        require_administrator_access(request.user, self.company.pk)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show current and superseded versions from this company only."""

        context = super().get_context_data(**kwargs)
        context.update(
            {
                "company": self.company,
                "current_terms_document": get_current_participant_terms_document(
                    self.company.pk
                ),
                "terms_documents": list_participant_terms_documents(self.company.pk),
                "max_pdf_size_mb": participant_terms_pdf_max_size_label(),
            }
        )
        return context

    def form_valid(self, form: ParticipantTermsDocumentUploadForm) -> HttpResponse:
        """Publish a new version for future participant records."""

        try:
            document = publish_participant_terms_pdf(
                user=self.request.user,
                company_id=self.company.pk,
                uploaded_file=form.cleaned_data["pdf_file"],
                company_approval_confirmed=form.cleaned_data[
                    "company_approval_confirmed"
                ],
            )
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                for field, errors in error.message_dict.items():
                    if field in form.fields:
                        for message in errors:
                            form.add_error(field, message)
                    else:
                        for message in errors:
                            form.add_error(None, message)
            else:
                for message in error.messages:
                    form.add_error(None, message)
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"PDF publicado como versão {document.version}. Novos cadastros usarão esta versão.",
        )
        return redirect("participants:terms_manage", company_id=self.company.pk)


class ParticipantTermsDocumentFileView(CompanyAccessMixin, View):
    """Serve one PDF only to members of the company that published it."""

    def get(
        self,
        request: HttpRequest,
        company_id: int,
        document_id: int,
    ) -> FileResponse:
        document = get_object_or_404(
            ParticipantTermsDocument.objects.filter(company=self.company),
            pk=document_id,
        )
        return _participant_terms_pdf_response(document)


class ParticipantDetailView(CompanyAccessMixin, DetailView):
    """Show one participant, the exact stored term and its operation history."""

    template_name = "participants/participant_detail.html"
    context_object_name = "participant"
    pk_url_kwarg = "participant_id"

    def get_queryset(self) -> QuerySet[Participant]:
        """Prevent a company member from reading a participant in another company."""

        return Participant.objects.filter(company=self.company).select_related(
            "company", "created_by"
        ).prefetch_related(
            "terms_versions__document",
            "terms_versions__attempts__transaction",
            "terms_versions__acceptance_links",
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Build the proposal, link and transaction controls from saved records."""

        context = super().get_context_data(**kwargs)
        participant: Participant = self.object
        terms = participant.latest_terms
        role_allowed = self.membership.roles.filter(
            active=True,
            role__in=(MembershipRole.Role.ADMINISTRATOR, MembershipRole.Role.REGISTRATION),
        ).exists()
        context.update(
            {
                "company": self.company,
                "terms": terms,
                "networks": get_participant_soja_coin_networks(self.company.pk),
                "attempts": list_participant_attempts(participant),
                "active_link": current_acceptance_link(terms) if terms else None,
                "can_prepare_participant": self.company.active and role_allowed,
            }
        )
        return context


class ParticipantOperatorActionMixin(CompanyAccessMixin):
    """Restrict all authenticated participant state changes to registration roles."""

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Apply the shared company action rule on every action request."""

        require_registration_access(request.user, self.company.pk)


class ParticipantProposalPrepareView(ParticipantOperatorActionMixin, View):
    """Prepare an owner-signed proposal for the company and participant in the URL."""

    def post(
        self,
        request: HttpRequest,
        company_id: int,
        participant_id: int,
    ) -> JsonResponse:
        """Persist the exact call before JavaScript opens the owner wallet."""

        try:
            participant = get_object_or_404(
                Participant.objects.filter(company=self.company), pk=participant_id
            )
            terms = participant.latest_terms
            if terms is None:
                raise ValidationError("Este participante ainda não tem um termo salvo.")
            data = _request_data(request)
            prepared = prepare_participant_proposal(
                user=request.user,
                company_id=self.company.pk,
                participant_id=participant.pk,
                terms_id=terms.pk,
                network_id=_positive_int(data.get("network_id"), "rede"),
                owner_address=str(data.get("owner_address", "")),
                wallet_chain_id=_positive_int(data.get("wallet_chain_id"), "chain ID"),
            )
            return JsonResponse(
                {
                    "attempt_id": prepared.attempt.pk,
                    "transaction_id": prepared.attempt.transaction_id,
                    "chain_id": prepared.chain_id,
                    "from": prepared.sender_address,
                    "to": prepared.contract_address,
                    "data": prepared.transaction_data,
                    "send_allowed": prepared.send_allowed,
                    "status": prepared.attempt.transaction.status,
                }
            )
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAttemptActionView(ParticipantOperatorActionMixin, View):
    """Apply one POST-only action scoped to a company and participant."""

    def get_attempt(
        self,
        company_id: int,
        participant_id: int,
        attempt_id: int,
    ) -> ParticipantRegistrationAttempt:
        """Check the full URL relationship before any transaction action."""

        return get_participant_attempt_for_company(
            company_id=self.company.pk,
            participant_id=participant_id,
            attempt_id=attempt_id,
        )


class ParticipantAttemptStartedView(ParticipantAttemptActionView):
    """Mark that the matching owner wallet prompt is about to open."""

    def post(self, request: HttpRequest, company_id: int, participant_id: int, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(company_id, participant_id, attempt_id)
            attempt, send_allowed = mark_participant_submission_started(attempt_id)
            return JsonResponse({**_attempt_json(attempt), "send_allowed": send_allowed})
        except (PermissionDenied, ValidationError) as error:
            return _json_error(error)


class ParticipantAttemptRejectedView(ParticipantAttemptActionView):
    """Release a prepared proposal only after explicit wallet rejection."""

    def post(self, request: HttpRequest, company_id: int, participant_id: int, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(company_id, participant_id, attempt_id)
            attempt = reject_participant_attempt(attempt_id)
            return JsonResponse({"status": attempt.transaction.status, "attempt_id": attempt.pk})
        except (PermissionDenied, ValidationError) as error:
            return _json_error(error)


class ParticipantAttemptHashView(ParticipantAttemptActionView):
    """Validate a wallet-returned proposal hash against the saved RPC call."""

    def post(self, request: HttpRequest, company_id: int, participant_id: int, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(company_id, participant_id, attempt_id)
            data = _request_data(request)
            attempt = register_participant_transaction_hash(
                attempt_id,
                str(data.get("transaction_hash", "")),
            )
            return JsonResponse(_attempt_json(attempt))
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAttemptConfirmView(ParticipantAttemptActionView):
    """Query the saved proposal transaction without opening the wallet again."""

    def post(self, request: HttpRequest, company_id: int, participant_id: int, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(company_id, participant_id, attempt_id)
            result = confirm_participant_attempt(attempt_id)
            labels = {
                "pending": "A rede ainda não confirmou esta transação.",
                "failed": "A transação falhou. Uma nova tentativa poderá ser preparada.",
                "proposal_confirmed": "Proposta confirmada. Agora o participante pode ler e aceitar o termo.",
                "confirmed": "Cadastro confirmado na SojaCoin.",
                "review": "A confirmação precisa ser conferida; o cadastro não está marcado como concluído.",
            }
            return JsonResponse(
                {
                    "status": result.status,
                    "message": labels[result.status],
                    "transaction_status": result.transaction.status,
                    "transaction_hash": result.transaction.transaction_hash or "",
                }
            )
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAcceptanceLinkCreateView(ParticipantOperatorActionMixin, View):
    """Create a one-use public link only after the chain confirms the proposal."""

    def post(self, request: HttpRequest, company_id: int, participant_id: int) -> JsonResponse:
        try:
            participant = get_object_or_404(
                Participant.objects.filter(company=self.company), pk=participant_id
            )
            terms = participant.latest_terms
            if terms is None:
                raise ValidationError("O participante ainda não tem um termo salvo.")
            link, raw_token = create_participant_acceptance_link(
                user=request.user,
                company_id=self.company.pk,
                participant_id=participant.pk,
                terms_id=terms.pk,
            )
            relative_url = reverse("participants:accept", kwargs={"token": raw_token})
            public_base_url = getattr(settings, "PARTICIPANT_PUBLIC_BASE_URL", "")
            url = (
                f"{public_base_url}{relative_url}"
                if public_base_url
                else request.build_absolute_uri(relative_url)
            )
            return JsonResponse(
                {
                    "url": url,
                    "expires_at": link.expires_at.isoformat(),
                    "message": "Copie este link e entregue ao participante. Ele será mostrado somente agora.",
                }
            )
        except (PermissionDenied, ValidationError) as error:
            return _json_error(error)


class ParticipantAcceptanceLinkCancelView(ParticipantOperatorActionMixin, View):
    """Cancel a bearer link before it is used by the participant."""

    def post(
        self,
        request: HttpRequest,
        company_id: int,
        participant_id: int,
        link_id: int,
    ) -> HttpResponse:
        """Cancel only a link inside the participant and company from the URL."""

        try:
            cancel_participant_acceptance_link(
                user=request.user,
                company_id=self.company.pk,
                participant_id=participant_id,
                link_id=link_id,
            )
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
        else:
            messages.success(request, "Link cancelado. O participante não poderá mais abrir esta proposta por ele.")
        return redirect(
            "participants:detail",
            company_id=self.company.pk,
            participant_id=participant_id,
        )


class ParticipantAcceptanceView(View):
    """Show the exact terms proposal through the participant's bearer link."""

    template_name = "participants/participant_acceptance.html"

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        """Render only the proposal named by the valid link; never list company data."""

        try:
            link = get_participant_acceptance_link(
                token,
                allow_completed=True,
                allow_in_progress=True,
            )
        except ValidationError as error:
            return render(
                request,
                "participants/participant_acceptance_unavailable.html",
                {"message": " ".join(error.messages)},
                status=410,
            )
        attempt = get_acceptance_attempt_for_link(link)
        response = render(
            request,
            self.template_name,
            {
                "link": link,
                "token": token,
                "attempt": attempt,
                "company": link.terms.participant.company,
                "network": link.terms.network,
                "contract_address": link.terms.contract.address_normalized,
            },
        )
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "no-referrer"
        return response


class ParticipantAcceptanceTermsPdfView(View):
    """Serve only the PDF attached to the proposal named by a bearer link."""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        """Require a valid link before returning its one immutable document."""

        try:
            link = get_participant_acceptance_link(
                token,
                allow_completed=True,
                allow_in_progress=True,
            )
        except ValidationError:
            return HttpResponse("Este link não está mais disponível.", status=410)
        document = link.terms.document
        if document is None:
            return HttpResponse("Este cadastro antigo não possui um PDF anexado.", status=404)
        return _participant_terms_pdf_response(document)


class ParticipantAcceptanceActionView(View):
    """Base for token-authorized, CSRF-protected participant actions."""

    def post(self, request: HttpRequest, token: str, **kwargs: Any) -> JsonResponse:
        raise NotImplementedError


class ParticipantAcceptanceChallengeView(ParticipantAcceptanceActionView):
    """Create a fresh EIP-712 challenge for the proposal's public wallet."""

    def post(self, request: HttpRequest, token: str) -> JsonResponse:
        try:
            data = _request_data(request)
            result = create_participant_acceptance_challenge(
                token=token,
                wallet_address=str(data.get("wallet_address", "")),
                wallet_chain_id=_positive_int(data.get("wallet_chain_id"), "chain ID"),
            )
            return JsonResponse(
                {
                    "challenge_id": result.challenge.pk,
                    "deadline": result.challenge.deadline,
                    "typed_data": result.typed_data,
                }
            )
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAcceptancePrepareView(ParticipantAcceptanceActionView):
    """Verify the EIP-712 signer and save the participant transaction."""

    def post(self, request: HttpRequest, token: str) -> JsonResponse:
        try:
            data = _request_data(request)
            prepared = prepare_participant_acceptance(
                token=token,
                challenge_id=_positive_int(data.get("challenge_id"), "desafio"),
                signature=str(data.get("signature", "")),
                wallet_address=str(data.get("wallet_address", "")),
                wallet_chain_id=_positive_int(data.get("wallet_chain_id"), "chain ID"),
            )
            return JsonResponse(
                {
                    "attempt_id": prepared.attempt.pk,
                    "transaction_id": prepared.attempt.transaction_id,
                    "chain_id": prepared.chain_id,
                    "from": prepared.sender_address,
                    "to": prepared.contract_address,
                    "data": prepared.transaction_data,
                    "send_allowed": prepared.send_allowed,
                    "status": prepared.attempt.transaction.status,
                }
            )
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAcceptanceAttemptActionView(ParticipantAcceptanceActionView):
    """Check a public attempt belongs to the active token before acting on it."""

    def get_attempt(self, token: str, attempt_id: int) -> ParticipantRegistrationAttempt:
        """Load by attempt and the one proposal link named in the URL."""

        link = get_participant_acceptance_link(
            token,
            allow_completed=True,
            allow_in_progress=True,
        )
        try:
            return ParticipantRegistrationAttempt.objects.select_related(
                "terms__participant", "transaction__network", "transaction__contract",
                "acceptance_link", "acceptance_challenge",
            ).get(
                pk=attempt_id,
                terms=link.terms,
                acceptance_link=link,
                kind=ParticipantRegistrationAttempt.Kind.ACCEPTANCE,
            )
        except ParticipantRegistrationAttempt.DoesNotExist as error:
            raise ValidationError("A tentativa não pertence a este link de aceite.") from error


class ParticipantAcceptanceStartedView(ParticipantAcceptanceAttemptActionView):
    """Open the participant's transaction prompt once for the saved attempt."""

    def post(self, request: HttpRequest, token: str, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(token, attempt_id)
            attempt, send_allowed = mark_participant_submission_started(
                attempt_id,
                access_token=token,
            )
            return JsonResponse({**_attempt_json(attempt), "send_allowed": send_allowed})
        except (PermissionDenied, ValidationError) as error:
            return _json_error(error)


class ParticipantAcceptanceRejectedView(ParticipantAcceptanceAttemptActionView):
    """Record an explicit MetaMask rejection while preserving the signed history."""

    def post(self, request: HttpRequest, token: str, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(token, attempt_id)
            attempt = reject_participant_attempt(attempt_id, access_token=token)
            return JsonResponse({"status": attempt.transaction.status})
        except (PermissionDenied, ValidationError) as error:
            return _json_error(error)


class ParticipantAcceptanceHashView(ParticipantAcceptanceAttemptActionView):
    """Check the participant's transaction hash against the saved EIP-712 call."""

    def post(self, request: HttpRequest, token: str, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(token, attempt_id)
            data = _request_data(request)
            attempt = register_participant_transaction_hash(
                attempt_id,
                str(data.get("transaction_hash", "")),
                access_token=token,
            )
            return JsonResponse(_attempt_json(attempt))
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)


class ParticipantAcceptanceConfirmView(ParticipantAcceptanceAttemptActionView):
    """Check receipt, events and current participant authorization on chain."""

    def post(self, request: HttpRequest, token: str, attempt_id: int) -> JsonResponse:
        try:
            self.get_attempt(token, attempt_id)
            result = confirm_participant_attempt(attempt_id, access_token=token)
            labels = {
                "pending": "A rede ainda não confirmou esta transação.",
                "failed": "A transação falhou. O participante poderá assinar um novo aceite.",
                "proposal_confirmed": "Proposta confirmada.",
                "confirmed": "Cadastro confirmado na SojaCoin.",
                "review": "A confirmação precisa ser conferida; o cadastro não está marcado como concluído.",
            }
            return JsonResponse(
                {
                    "status": result.status,
                    "message": labels[result.status],
                    "transaction_status": result.transaction.status,
                }
            )
        except (PermissionDenied, ValidationError, ConnectionError, ImproperlyConfigured, Web3Exception) as error:
            return _json_error(error)
