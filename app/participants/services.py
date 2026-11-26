"""Prepare, send and reconcile participant registration on the SojaCoin contract."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_keys.exceptions import BadSignature
from eth_utils.exceptions import ValidationError as EthSignatureValidationError
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction as db_transaction
from django.db.models import Max
from django.utils import timezone
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound, Web3Exception

from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    Network,
    PARTICIPANT_REGISTRATION_OPERATION_TYPE,
)
from blockchain.services.contracts import bind_contract_to_web3
from blockchain.services.rpc import connect_to_rpc
from blockchain.services.transaction_confirmation import (
    DecodedContractEvent,
    confirm_transaction_from_chain,
)
from blockchain.services.transactions import fail_transaction, submit_transaction
from companies.models import Company
from companies.rules.role_access import (
    require_administrator_access,
    require_registration_access,
)
from utils.identifiers import (
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from .models import (
    Participant,
    ParticipantAcceptanceChallenge,
    ParticipantAcceptanceLink,
    ParticipantRegistrationAttempt,
    ParticipantTerms,
    ParticipantTermsDocument,
)
from .documents import participant_terms_pdf_hash, read_valid_participant_terms_pdf


LINK_LIFETIME = timedelta(days=7)
ACCEPTANCE_SIGNATURE_LIFETIME = timedelta(minutes=15)
MAX_SIGNATURE_LENGTH = 132
HEX_BYTES_RE = re.compile(r"0x(?:[0-9a-f]{2})+\Z")


@dataclass(frozen=True)
class PreparedParticipantTransaction:
    """Return a saved wallet request and whether it can be sent now."""

    attempt: ParticipantRegistrationAttempt
    chain_id: int
    contract_address: str
    sender_address: str
    transaction_data: str
    send_allowed: bool


@dataclass(frozen=True)
class ParticipantTransactionResult:
    """Describe the database and chain result of a transaction lookup."""

    status: Literal["pending", "failed", "proposal_confirmed", "confirmed", "review"]
    attempt: ParticipantRegistrationAttempt
    transaction: BlockchainTransaction


@dataclass(frozen=True)
class AcceptanceChallengeResult:
    """Return the exact typed-data message shown to the participant's wallet."""

    challenge: ParticipantAcceptanceChallenge
    typed_data: dict[str, Any]


def create_participant(
    *,
    user: AbstractBaseUser,
    company_id: int,
    name: str,
    wallet_address: str,
    terms_document_id: int | None = None,
) -> tuple[Participant, ParticipantTerms]:
    """Create a participant linked to the company's currently published PDF terms."""

    membership = require_registration_access(user, company_id)
    normalized_wallet = normalize_wallet_address(wallet_address)
    if not is_valid_wallet_address(normalized_wallet):
        raise ValidationError({"wallet_address": "Informe uma carteira EVM válida."})

    clean_name = (name or "").strip()
    if not clean_name:
        raise ValidationError({"name": "Informe o nome do participante."})

    with db_transaction.atomic():
        company = Company.objects.select_for_update().get(
            pk=membership.company_id,
            active=True,
        )
        document = get_current_participant_terms_document(company.pk, lock=True)
        if document is None:
            raise ValidationError(
                "A empresa ainda não publicou o PDF dos termos do participante. "
                "Peça ao administrador para publicá-lo antes de cadastrar participantes."
            )
        if terms_document_id is not None and document.pk != terms_document_id:
            raise ValidationError(
                "A empresa publicou uma nova versão enquanto o formulário estava aberto. "
                "Atualize a página e confira o termo vigente."
            )
        participant = Participant.objects.create(
            company=company,
            name=clean_name,
            wallet_address=normalized_wallet,
            created_by=user,
        )
        terms = ParticipantTerms.objects.create(
            participant=participant,
            document=document,
            version=document.version,
            content="",
            terms_hash=document.terms_hash,
        )
    return participant, terms


def get_current_participant_terms_document(
    company_id: int,
    *,
    lock: bool = False,
) -> ParticipantTermsDocument | None:
    """Return the published PDF version selected for new participants."""

    documents = ParticipantTermsDocument.objects.filter(
        company_id=company_id,
        is_current=True,
    )
    if lock:
        documents = documents.select_for_update()
    return documents.first()


def list_participant_terms_documents(
    company_id: int,
) -> list[ParticipantTermsDocument]:
    """Return every immutable terms version belonging to one company."""

    return list(
        ParticipantTermsDocument.objects.filter(company_id=company_id)
        .select_related("uploaded_by")
        .order_by("-version", "-pk")
    )


def publish_participant_terms_pdf(
    *,
    user: AbstractBaseUser,
    company_id: int,
    uploaded_file: UploadedFile,
    company_approval_confirmed: bool,
) -> ParticipantTermsDocument:
    """Publish an approved PDF as a new immutable version for the company."""

    membership = require_administrator_access(user, company_id)
    if not company_approval_confirmed:
        raise ValidationError(
            "Confirme que a empresa aprovou este PDF antes de publicá-lo."
        )
    pdf_bytes = read_valid_participant_terms_pdf(uploaded_file)
    terms_hash = participant_terms_pdf_hash(pdf_bytes)
    original_filename = Path(
        (uploaded_file.name or "termos.pdf").replace("\\", "/")
    ).name[:255]
    uploaded_file.seek(0)
    document: ParticipantTermsDocument | None = None

    try:
        with db_transaction.atomic():
            company = Company.objects.select_for_update().get(
                pk=membership.company_id,
                active=True,
            )
            current = ParticipantTermsDocument.objects.select_for_update().filter(
                company=company,
                is_current=True,
            ).first()
            if current and current.terms_hash == terms_hash:
                raise ValidationError(
                    f"Este mesmo PDF já está publicado como versão {current.version}."
                )

            last_version = ParticipantTermsDocument.objects.filter(
                company=company
            ).aggregate(version=Max("version"))["version"] or 0
            if current:
                current.is_current = False
                current.save(update_fields=("is_current", "updated_at"))

            document = ParticipantTermsDocument(
                company=company,
                version=last_version + 1,
                original_filename=original_filename,
                pdf_file=uploaded_file,
                terms_hash=terms_hash,
                uploaded_by=user,
                company_approval_confirmed=company_approval_confirmed,
                is_current=True,
            )
            document.save()
    except Exception:
        # File storage does not participate in the database transaction. Remove
        # an orphaned upload if the new version could not be committed.
        if document and document.pdf_file.name and document.pdf_file._committed:
            document.pdf_file.storage.delete(document.pdf_file.name)
        raise

    assert document is not None
    return document


def get_participant_soja_coin_networks(company_id: int) -> list[Network]:
    """List active networks with an active SojaCoin belonging to one company."""

    return list(
        Network.objects.filter(
            active=True,
            contract_instances__company_id=company_id,
            contract_instances__kind=ContractInstance.Kind.SOJA_COIN,
            contract_instances__active=True,
        )
        .distinct()
        .order_by("name", "pk")
    )


def _get_company_contract(
    *, company_id: int, network_id: int
) -> tuple[Company, Network, ContractInstance]:
    """Load the active company, network and SojaCoin as one validated set."""

    try:
        company = Company.objects.get(pk=company_id, active=True)
        network = Network.objects.get(pk=network_id, active=True)
        contract = ContractInstance.objects.get(
            company=company,
            network=network,
            kind=ContractInstance.Kind.SOJA_COIN,
            active=True,
        )
    except (Company.DoesNotExist, Network.DoesNotExist, ContractInstance.DoesNotExist) as error:
        raise ValidationError(
            "Não encontrei uma SojaCoin ativa da empresa na rede escolhida."
        ) from error
    except ContractInstance.MultipleObjectsReturned as error:
        raise ValidationError(
            "Há mais de uma SojaCoin ativa para esta empresa e rede. Peça uma conferência."
        ) from error
    return company, network, contract


def _get_contract(web3: Web3, deployment: ContractInstance) -> Contract:
    """Bind the saved SojaCoin ABI and reject an address without deployed code."""

    checksum_address = Web3.to_checksum_address(deployment.address_normalized)
    if not web3.eth.get_code(checksum_address):
        raise ValidationError("Não há código SojaCoin no endereço cadastrado nesta rede.")
    return bind_contract_to_web3(web3, deployment)


def _verify_terms_contract(
    terms: ParticipantTerms,
    contract: ContractInstance,
    network: Network,
) -> None:
    """Ensure a terms version never moves to a different chain deployment."""

    if terms.network_id and terms.network_id != network.pk:
        raise ValidationError("Este termo já foi ligado a outra rede.")
    if terms.contract_id and terms.contract_id != contract.pk:
        raise ValidationError("A SojaCoin mudou desde a criação desta proposta.")


def _transaction_input(value: object) -> str:
    """Normalize an RPC transaction input for exact prepared-data comparison."""

    if isinstance(value, (bytes, bytearray)):
        normalized = Web3.to_hex(value).lower()
    elif isinstance(value, str):
        normalized = value.lower()
    else:
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    if not HEX_BYTES_RE.fullmatch(normalized):
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    return normalized


def _ensure_wallet_chain(network: Network, wallet_chain_id: int) -> None:
    """Reject wallet requests from a chain other than the saved deployment."""

    if isinstance(wallet_chain_id, bool) or wallet_chain_id != network.chain_id:
        raise ValidationError(
            f"A carteira está em outra rede. Selecione {network.name} (ID {network.chain_id})."
        )


def _active_attempt(
    terms: ParticipantTerms,
    kind: str,
) -> ParticipantRegistrationAttempt | None:
    """Return the still-open attempt, if a wallet send is already in progress."""

    return (
        ParticipantRegistrationAttempt.objects.select_related(
            "transaction__network", "transaction__contract", "terms__participant"
        )
        .filter(terms=terms, kind=kind, is_open=True)
        .first()
    )


def _serialize_prepared_attempt(
    attempt: ParticipantRegistrationAttempt,
) -> PreparedParticipantTransaction:
    """Return only public transaction fields required by the browser wallet."""

    record = attempt.transaction
    return PreparedParticipantTransaction(
        attempt=attempt,
        chain_id=record.network.chain_id,
        contract_address=record.contract.address_normalized,
        sender_address=attempt.sender_address,
        transaction_data=attempt.prepared_data,
        send_allowed=(
            record.status == BlockchainTransaction.Status.PREPARED
            and attempt.submission_started_at is None
        ),
    )


def _create_transaction_attempt(
    *,
    terms: ParticipantTerms,
    network: Network,
    contract: ContractInstance,
    kind: str,
    sender_address: str,
    prepared_data: str,
    acceptance_link: ParticipantAcceptanceLink | None = None,
    challenge: ParticipantAcceptanceChallenge | None = None,
    signature: str = "",
    deadline: int | None = None,
) -> ParticipantRegistrationAttempt:
    """Create an operation transaction and its attempt within the caller's lock."""

    transaction_record = BlockchainTransaction.objects.create(
        company=terms.participant.company,
        network=network,
        contract=contract,
        operation_type=PARTICIPANT_REGISTRATION_OPERATION_TYPE,
        sender_address=sender_address,
        status=BlockchainTransaction.Status.PREPARED,
    )
    return ParticipantRegistrationAttempt.objects.create(
        terms=terms,
        kind=kind,
        transaction=transaction_record,
        acceptance_link=acceptance_link,
        acceptance_challenge=challenge,
        sender_address=sender_address,
        prepared_data=prepared_data,
        signature=signature,
        deadline=deadline,
    )


def prepare_participant_proposal(
    *,
    user: AbstractBaseUser,
    company_id: int,
    participant_id: int,
    terms_id: int,
    network_id: int,
    owner_address: str,
    wallet_chain_id: int,
) -> PreparedParticipantTransaction:
    """Prepare the owner's registration proposal and persist it before MetaMask.

    Repeating this call returns the existing open attempt. It never creates a
    second pending proposal while the wallet's answer or RPC result is uncertain.
    """

    require_registration_access(user, company_id)
    owner = normalize_wallet_address(owner_address)
    if not is_valid_wallet_address(owner):
        raise ValidationError({"owner_address": "Conecte uma carteira EVM válida."})

    try:
        terms = ParticipantTerms.objects.select_related(
            "participant__company", "network", "contract"
        ).get(
            pk=terms_id,
            participant_id=participant_id,
            participant__company_id=company_id,
        )
    except ParticipantTerms.DoesNotExist as error:
        raise ValidationError("O termo deste participante não foi encontrado.") from error

    company, network, deployment = _get_company_contract(
        company_id=company_id,
        network_id=network_id,
    )
    _ensure_wallet_chain(network, wallet_chain_id)
    _verify_terms_contract(terms, deployment, network)

    # The chain reads happen before taking database locks; the transaction is
    # rechecked below so no stale state is used to create a competing proposal.
    web3 = connect_to_rpc(network)
    contract = _get_contract(web3, deployment)
    current_owner = normalize_wallet_address(str(contract.functions.owner().call()))
    if current_owner != owner:
        raise PermissionDenied("Conecte a carteira dona atual da SojaCoin para propor o cadastro.")

    participant_wallet = Web3.to_checksum_address(terms.participant.wallet_address_normalized)
    if contract.functions.participantRegistered(participant_wallet).call():
        raise ValidationError("Esta carteira já está cadastrada na SojaCoin.")
    if contract.functions.participantRegistrationPending(participant_wallet).call():
        raise ValidationError(
            "Já existe uma proposta pendente para esta carteira na SojaCoin."
        )

    prepared_data = contract.functions.proposeParticipantRegistration(
        participant_wallet,
        bytes.fromhex(terms.terms_hash[2:]),
    )._encode_transaction_data().lower()

    with db_transaction.atomic():
        locked_terms = ParticipantTerms.objects.select_for_update().get(pk=terms.pk)
        existing = _active_attempt(locked_terms, ParticipantRegistrationAttempt.Kind.PROPOSAL)
        if existing is not None:
            if (
                existing.transaction.network_id != network.pk
                or existing.transaction.contract_id != deployment.pk
                or existing.sender_address != owner
                or existing.prepared_data != prepared_data
            ):
                raise ValidationError(
                    "Há uma tentativa de proposta aberta com dados diferentes. Confira-a antes de continuar."
                )
            return _serialize_prepared_attempt(existing)

        # Refuse a stale proposal if chain state changed while the form was open.
        if contract.functions.participantRegistered(participant_wallet).call():
            raise ValidationError("Esta carteira já está cadastrada na SojaCoin.")
        if contract.functions.participantRegistrationPending(participant_wallet).call():
            raise ValidationError("Já existe uma proposta pendente para esta carteira.")

        locked_terms.network = network
        locked_terms.contract = deployment
        locked_terms.status = ParticipantTerms.Status.PROPOSAL_PREPARED
        locked_terms.save(update_fields=("network", "contract", "status", "updated_at"))

        try:
            attempt = _create_transaction_attempt(
                terms=locked_terms,
                network=network,
                contract=deployment,
                kind=ParticipantRegistrationAttempt.Kind.PROPOSAL,
                sender_address=owner,
                prepared_data=prepared_data,
            )
        except IntegrityError as error:
            raise ValidationError(
                "Outra solicitação preparou a proposta ao mesmo tempo. Atualize a página."
            ) from error

    return _serialize_prepared_attempt(attempt)


def _get_attempt(attempt_id: int) -> ParticipantRegistrationAttempt:
    """Load an attempt with the records needed by wallet and RPC checks."""

    if isinstance(attempt_id, bool) or not isinstance(attempt_id, int) or attempt_id < 1:
        raise ValidationError("O número da tentativa é inválido.")
    try:
        return ParticipantRegistrationAttempt.objects.select_related(
            "terms__participant__company",
            "terms__network",
            "terms__contract",
            "transaction__network",
            "transaction__contract",
            "acceptance_link",
            "acceptance_challenge",
        ).get(pk=attempt_id)
    except ParticipantRegistrationAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa do participante não encontrada.") from error


def mark_participant_submission_started(
    attempt_id: int,
    *,
    access_token: str | None = None,
) -> tuple[ParticipantRegistrationAttempt, bool]:
    """Record that a wallet prompt is about to open and return send permission.

    A second request returns ``False`` so retrying an HTTP request cannot cause a
    second call to ``eth_sendTransaction`` for the same saved attempt.
    """

    with db_transaction.atomic():
        attempt = ParticipantRegistrationAttempt.objects.select_for_update().select_related(
            "terms__participant", "acceptance_link", "transaction"
        ).get(pk=attempt_id)
        _authorize_attempt_token(attempt, access_token)
        if not attempt.is_open or attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
            raise ValidationError("Esta tentativa não está pronta para abrir a carteira.")
        if attempt.submission_started_at is not None:
            return attempt, False
        attempt.submission_started_at = timezone.now()
        attempt.save(update_fields=("submission_started_at", "updated_at"))
        return attempt, True


def _authorize_attempt_token(
    attempt: ParticipantRegistrationAttempt,
    access_token: str | None,
    *,
    allow_completed: bool = False,
) -> None:
    """Require the original acceptance link for unauthenticated participant actions."""

    if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL:
        return
    if access_token is None or attempt.acceptance_link is None:
        raise PermissionDenied("Este link não pode acessar a tentativa de aceite.")
    if not secrets.compare_digest(
        _token_digest(access_token), attempt.acceptance_link.token_hash
    ):
        raise PermissionDenied("O link não corresponde à tentativa de aceite.")
    completed_read_only = (
        allow_completed
        and attempt.acceptance_link.used_at is not None
        and attempt.terms.status == ParticipantTerms.Status.CONFIRMED
    )
    reorg_review_read_only = (
        allow_completed
        and attempt.acceptance_link.used_at is not None
        and attempt.terms.status == ParticipantTerms.Status.NEEDS_REVIEW
        and attempt.transaction.status == BlockchainTransaction.Status.REORGED
    )
    if not attempt.acceptance_link.is_usable and not (
        completed_read_only or reorg_review_read_only
    ):
        raise PermissionDenied("Este link de aceite não está mais ativo.")


def _token_digest(token: str) -> str:
    """Return a deterministic digest for comparing a raw bearer token safely."""

    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def reject_participant_attempt(
    attempt_id: int,
    *,
    access_token: str | None = None,
) -> ParticipantRegistrationAttempt:
    """Close an attempt only after the wallet explicitly reports user rejection."""

    attempt = _get_attempt(attempt_id)
    _authorize_attempt_token(attempt, access_token)
    if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Só uma transação ainda não enviada pode ser recusada.")
    with db_transaction.atomic():
        attempt = _get_attempt(attempt_id)
        transaction_record = fail_transaction(
            attempt.transaction,
            "A solicitação foi recusada explicitamente na carteira.",
        )
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
        if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL:
            terms = attempt.terms
            terms.status = ParticipantTerms.Status.FAILED
            terms.save(update_fields=("status", "updated_at"))
        elif attempt.acceptance_challenge_id:
            challenge = attempt.acceptance_challenge
            challenge.status = ParticipantAcceptanceChallenge.Status.INVALIDATED
            challenge.save(update_fields=("status", "updated_at"))
            terms = attempt.terms
            terms.status = ParticipantTerms.Status.AWAITING_ACCEPTANCE
            terms.save(update_fields=("status", "updated_at"))
        attempt.transaction = transaction_record
        return attempt


def register_participant_transaction_hash(
    attempt_id: int,
    transaction_hash: str,
    *,
    access_token: str | None = None,
) -> ParticipantRegistrationAttempt:
    """Verify the RPC transaction exactly before recording its hash locally."""

    attempt = _get_attempt(attempt_id)
    _authorize_attempt_token(attempt, access_token)
    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError({"transaction_hash": "Informe uma hash de transação válida."})

    record = attempt.transaction
    if record.status == BlockchainTransaction.Status.SUBMITTED:
        if record.transaction_hash == canonical_hash:
            return attempt
        raise ValidationError("Esta tentativa já está ligada a outra hash.")
    if record.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Esta tentativa não pode receber uma hash.")
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A solicitação de envio não foi aberta para esta tentativa.")

    web3 = connect_to_rpc(record.network)
    try:
        chain_transaction = web3.eth.get_transaction(canonical_hash)
    except TransactionNotFound as error:
        raise ValidationError(
            "O RPC ainda não encontrou essa hash. Confira a hash e tente novamente."
        ) from error
    except Web3Exception as error:
        raise ValidationError("Não foi possível consultar a transação no RPC.") from error

    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != record.network.chain_id:
        raise ValidationError("A transação encontrada pertence a outra rede.")
    if normalize_wallet_address(str(chain_transaction.get("from", ""))) != attempt.sender_address:
        raise ValidationError("A carteira que enviou a transação não corresponde à tentativa.")
    if normalize_wallet_address(str(chain_transaction.get("to") or "")) != record.contract.address_normalized:
        raise ValidationError("O destino da transação não é a SojaCoin desta empresa.")
    if _transaction_input(chain_transaction.get("input")) != attempt.prepared_data:
        raise ValidationError("Os dados enviados não correspondem à chamada preparada.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("A transação preparada não deve transferir moeda nativa.")

    with db_transaction.atomic():
        locked_attempt = ParticipantRegistrationAttempt.objects.select_for_update().get(
            pk=attempt.pk
        )
        _authorize_attempt_token(locked_attempt, access_token)
        if locked_attempt.transaction.status == BlockchainTransaction.Status.SUBMITTED:
            if locked_attempt.transaction.transaction_hash == canonical_hash:
                return locked_attempt
            raise ValidationError("Esta tentativa já foi enviada com outra hash.")
        submitted = submit_transaction(locked_attempt.transaction, canonical_hash)
        locked_attempt.transaction = submitted
        locked_attempt.save(update_fields=("updated_at",))
        terms = locked_attempt.terms
        terms.status = (
            ParticipantTerms.Status.PROPOSAL_SUBMITTED
            if locked_attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL
            else ParticipantTerms.Status.ACCEPTANCE_SUBMITTED
        )
        terms.save(update_fields=("status", "updated_at"))
        return locked_attempt


def _mark_failed_attempt(attempt: ParticipantRegistrationAttempt) -> None:
    """Release a definitive failed attempt while preserving its transaction row."""

    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
    if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL:
        terms = attempt.terms
        terms.status = ParticipantTerms.Status.FAILED
        terms.save(update_fields=("status", "updated_at"))
    else:
        terms = attempt.terms
        terms.status = ParticipantTerms.Status.AWAITING_ACCEPTANCE
        terms.save(update_fields=("status", "updated_at"))


def _handle_participant_event(
    attempt: ParticipantRegistrationAttempt,
    transaction_record: BlockchainTransaction,
    event: DecodedContractEvent,
    contract: Contract,
) -> None:
    """Validate this operation's event and matching live contract storage."""

    expected_wallet = attempt.terms.participant.wallet_address_normalized
    expected_hash = attempt.terms.terms_hash
    args = event.args
    if event.transaction_hash.lower() != (transaction_record.transaction_hash or "").lower():
        raise ValidationError("O evento veio de outra transação.")

    if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL:
        if event.event_name != "ParticipantRegistrationProposed":
            return
        if (
            normalize_wallet_address(str(args.get("walletAddress", ""))) != expected_wallet
            or Web3.to_hex(args.get("participationTermsHash", b"")).lower() != expected_hash
            or normalize_wallet_address(str(args.get("proposedBy", "")))
            != attempt.sender_address
        ):
            raise ValidationError("O evento da proposta diverge do participante ou do termo salvo.")
        if not contract.functions.participantRegistrationPending(
            Web3.to_checksum_address(expected_wallet)
        ).call():
            raise ValidationError("A proposta não aparece pendente no estado atual do contrato.")
        stored_hash = Web3.to_hex(
            contract.functions.participationTermsHashByParticipant(
                Web3.to_checksum_address(expected_wallet)
            ).call()
        ).lower()
        if stored_hash != expected_hash:
            raise ValidationError("A hash de termo guardada no contrato é diferente da proposta.")
        terms = attempt.terms
        terms.status = ParticipantTerms.Status.AWAITING_ACCEPTANCE
        terms.proposal_confirmed_at = timezone.now()
        terms.save(update_fields=("status", "proposal_confirmed_at", "updated_at"))
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
        return

    if event.event_name == "ParticipantRegistered":
        if (
            normalize_wallet_address(str(args.get("walletAddress", ""))) != expected_wallet
            or Web3.to_hex(args.get("participationTermsHash", b"")).lower() != expected_hash
            or normalize_wallet_address(str(args.get("acceptedBy", ""))) != expected_wallet
        ):
            raise ValidationError("O evento de aceite diverge do participante ou do termo salvo.")
    elif event.event_name == "DepositAuthorizationChanged":
        if (
            normalize_wallet_address(str(args.get("producer", ""))) != expected_wallet
            or args.get("authorized") is not True
            or Web3.to_hex(args.get("participationTermsHash", b"")).lower() != expected_hash
            or normalize_wallet_address(str(args.get("changedBy", ""))) != expected_wallet
        ):
            raise ValidationError("O evento de autorização de depósito não corresponde ao aceite.")
    else:
        return

    wallet = Web3.to_checksum_address(expected_wallet)
    if not contract.functions.participantRegistered(wallet).call():
        raise ValidationError("O participante ainda não aparece cadastrado no contrato.")
    if not contract.functions.depositAuthorizationActive(wallet).call():
        raise ValidationError("A autorização de depósito ainda não está ativa no contrato.")
    stored_hash = Web3.to_hex(
        contract.functions.participationTermsHashByParticipant(wallet).call()
    ).lower()
    if stored_hash != expected_hash:
        raise ValidationError("O contrato não guarda a hash do termo assinado.")

    terms = attempt.terms
    if terms.status != ParticipantTerms.Status.CONFIRMED:
        terms.status = ParticipantTerms.Status.CONFIRMED
        terms.accepted_at = timezone.now()
        terms.save(update_fields=("status", "accepted_at", "updated_at"))
    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
    link = attempt.acceptance_link
    if link is not None and link.used_at is None:
        link.used_at = timezone.now()
        link.save(update_fields=("used_at", "updated_at"))


def confirm_participant_attempt(
    attempt_id: int,
    *,
    access_token: str | None = None,
) -> ParticipantTransactionResult:
    """Reconcile a participant transaction through receipt, events and storage."""

    attempt = _get_attempt(attempt_id)
    _authorize_attempt_token(attempt, access_token, allow_completed=True)
    record = attempt.transaction

    if record.status == BlockchainTransaction.Status.REORGED:
        return ParticipantTransactionResult("review", attempt, record)
    if record.status == BlockchainTransaction.Status.FAILED:
        return ParticipantTransactionResult("failed", attempt, record)
    if record.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
    }:
        raise ValidationError("A transação ainda não foi enviada pela carteira.")

    network = record.network
    web3 = connect_to_rpc(network)
    contract = _get_contract(web3, record.contract)
    required_events = (
        ("ParticipantRegistrationProposed",)
        if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL
        else ("ParticipantRegistered", "DepositAuthorizationChanged")
    )

    def handle_event(
        transaction_record: BlockchainTransaction,
        event: DecodedContractEvent,
    ) -> None:
        """Apply event changes only after matching the saved attempt."""

        _handle_participant_event(attempt, transaction_record, event, contract)

    checked = confirm_transaction_from_chain(
        record.pk,
        event_handler=handle_event,
        required_event_names=required_events,
    )
    attempt.refresh_from_db()
    record.refresh_from_db()
    if checked is None:
        return ParticipantTransactionResult("pending", attempt, record)
    if checked.status == BlockchainTransaction.Status.FAILED:
        with db_transaction.atomic():
            attempt = ParticipantRegistrationAttempt.objects.select_for_update().get(
                pk=attempt.pk
            )
            _mark_failed_attempt(attempt)
        return ParticipantTransactionResult("failed", attempt, checked)
    if checked.status == BlockchainTransaction.Status.REORGED:
        return ParticipantTransactionResult("review", attempt, checked)

    terms = attempt.terms
    if attempt.kind == ParticipantRegistrationAttempt.Kind.PROPOSAL:
        return ParticipantTransactionResult("proposal_confirmed", attempt, checked)
    if terms.status != ParticipantTerms.Status.CONFIRMED:
        # A successful receipt with missing or inconsistent event effects must
        # never be shown as a successful participant acceptance.
        terms.status = ParticipantTerms.Status.NEEDS_REVIEW
        terms.save(update_fields=("status", "updated_at"))
        return ParticipantTransactionResult("review", attempt, checked)
    return ParticipantTransactionResult("confirmed", attempt, checked)


def create_participant_acceptance_link(
    *,
    user: AbstractBaseUser,
    company_id: int,
    participant_id: int,
    terms_id: int,
) -> tuple[ParticipantAcceptanceLink, str]:
    """Create a seven-day bearer link after the owner's proposal is confirmed."""

    require_registration_access(user, company_id)
    try:
        terms = ParticipantTerms.objects.select_related("participant").get(
            pk=terms_id,
            participant_id=participant_id,
            participant__company_id=company_id,
        )
    except ParticipantTerms.DoesNotExist as error:
        raise ValidationError("O termo deste participante não foi encontrado.") from error
    if terms.status != ParticipantTerms.Status.AWAITING_ACCEPTANCE:
        raise ValidationError("O link só pode ser criado depois da proposta confirmada.")
    proposal = terms.attempts.filter(
        kind=ParticipantRegistrationAttempt.Kind.PROPOSAL,
        transaction__status=BlockchainTransaction.Status.CONFIRMED,
    ).exists()
    if not proposal:
        raise ValidationError("A transação da proposta ainda não foi confirmada.")

    raw_token = secrets.token_urlsafe(32)
    token_hash = _token_digest(raw_token)
    now = timezone.now()
    with db_transaction.atomic():
        for old_link in ParticipantAcceptanceLink.objects.select_for_update().filter(
            terms=terms,
            used_at__isnull=True,
            canceled_at__isnull=True,
        ):
            old_link.canceled_at = now
            old_link.save(update_fields=("canceled_at", "updated_at"))
        link = ParticipantAcceptanceLink.objects.create(
            terms=terms,
            token_hash=token_hash,
            expires_at=now + LINK_LIFETIME,
            created_by=user,
        )
    return link, raw_token


def cancel_participant_acceptance_link(
    *,
    user: AbstractBaseUser,
    company_id: int,
    participant_id: int,
    link_id: int,
) -> ParticipantAcceptanceLink:
    """Cancel an unused proposal link while preserving its access history."""

    require_registration_access(user, company_id)
    with db_transaction.atomic():
        try:
            link = ParticipantAcceptanceLink.objects.select_for_update().get(
                pk=link_id,
                terms__participant_id=participant_id,
                terms__participant__company_id=company_id,
            )
        except ParticipantAcceptanceLink.DoesNotExist as error:
            raise ValidationError("Este link não pertence ao participante e à empresa.") from error
        if link.used_at is not None:
            raise ValidationError("Um link já usado não pode ser cancelado.")
        if link.canceled_at is None:
            link.canceled_at = timezone.now()
            link.save(update_fields=("canceled_at", "updated_at"))
    return link


def get_participant_acceptance_link(
    token: str,
    *,
    allow_completed: bool = False,
    allow_in_progress: bool = False,
) -> ParticipantAcceptanceLink:
    """Resolve an active link without exposing the raw code in database queries."""

    if not isinstance(token, str) or len(token) < 32 or len(token) > 128:
        raise ValidationError("Este link de aceite é inválido ou expirou.")
    digest = _token_digest(token)
    try:
        link = ParticipantAcceptanceLink.objects.select_related(
            "terms__participant__company",
            "terms__network",
            "terms__contract",
            "terms__document",
        ).get(token_hash=digest)
    except ParticipantAcceptanceLink.DoesNotExist as error:
        raise ValidationError("Este link de aceite é inválido ou expirou.") from error
    completed_read_only = (
        allow_completed
        and link.used_at is not None
        and link.terms.status == ParticipantTerms.Status.CONFIRMED
    )
    reorg_review_read_only = (
        allow_completed
        and link.used_at is not None
        and link.terms.status == ParticipantTerms.Status.NEEDS_REVIEW
        and link.terms.attempts.filter(
            kind=ParticipantRegistrationAttempt.Kind.ACCEPTANCE,
            transaction__status=BlockchainTransaction.Status.REORGED,
        ).exists()
    )
    acceptance_in_progress = (
        allow_in_progress
        and link.terms.status
        in (
            ParticipantTerms.Status.ACCEPTANCE_PREPARED,
            ParticipantTerms.Status.ACCEPTANCE_SUBMITTED,
        )
    )
    if not link.is_usable and not (completed_read_only or reorg_review_read_only):
        raise ValidationError("Este link foi usado, cancelado ou expirou.")
    if (
        link.terms.status != ParticipantTerms.Status.AWAITING_ACCEPTANCE
        and not (
            completed_read_only
            or reorg_review_read_only
            or acceptance_in_progress
        )
    ):
        raise ValidationError("Esta proposta não está mais aguardando o aceite.")
    return link


def _check_live_proposal(
    link: ParticipantAcceptanceLink,
) -> tuple[Web3, Contract, int, str]:
    """Check the exact saved deployment and proposal state before signing."""

    terms = link.terms
    if terms.network is None or terms.contract is None:
        raise ValidationError("A proposta não tem rede e contrato salvos.")
    if not terms.network.active or not terms.contract.active:
        raise ValidationError("A rede ou a SojaCoin desta proposta está inativa.")
    if (
        terms.contract.company_id != terms.participant.company_id
        or terms.contract.network_id != terms.network_id
        or terms.contract.kind != ContractInstance.Kind.SOJA_COIN
    ):
        raise ValidationError("A proposta não está ligada à SojaCoin da empresa.")

    web3 = connect_to_rpc(terms.network)
    contract = _get_contract(web3, terms.contract)
    wallet = Web3.to_checksum_address(terms.participant.wallet_address_normalized)
    if contract.functions.participantRegistered(wallet).call():
        raise ValidationError("Este participante já está registrado na SojaCoin.")
    if not contract.functions.participantRegistrationPending(wallet).call():
        raise ValidationError("A proposta não está pendente no contrato.")
    stored_hash = Web3.to_hex(
        contract.functions.participationTermsHashByParticipant(wallet).call()
    ).lower()
    if stored_hash != terms.terms_hash:
        raise ValidationError("O termo pendente no contrato mudou; solicite uma nova proposta.")
    nonce = int(contract.functions.participationTermsNonces(wallet).call())
    return web3, contract, nonce, wallet


def _require_individual_wallet(web3: Web3, wallet_address: str) -> None:
    """Reject contract wallets until the SaaS supports their signature rules."""

    if web3.eth.get_code(Web3.to_checksum_address(wallet_address)):
        raise ValidationError(
            "Carteiras de contrato, como Safe, ainda não são compatíveis com este aceite. "
            "Use uma carteira individual compatível com MetaMask."
        )


def _build_acceptance_typed_data(
    *,
    participant_address: str,
    terms_hash: str,
    nonce: int,
    deadline: int,
    chain_id: int,
    contract_address: str,
) -> dict[str, Any]:
    """Build the EIP-712 structure matching the Solidity struct and domain."""

    return {
        "domain": {
            "name": "SojaCoin",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(contract_address),
        },
        "primaryType": "ParticipationTermsAcceptance",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "ParticipationTermsAcceptance": [
                {"name": "participant", "type": "address"},
                {"name": "termsHash", "type": "bytes32"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "message": {
            "participant": Web3.to_checksum_address(participant_address),
            "termsHash": terms_hash,
            "nonce": nonce,
            "deadline": deadline,
        },
    }


def _typed_data_digest(typed_data: dict[str, Any]) -> tuple[SignableMessage, str]:
    """Return eth-account's EIP-712 message and its final 32-byte digest."""

    try:
        signable = encode_typed_data(full_message=typed_data)
    except (TypeError, ValueError) as error:
        raise ValidationError("Não foi possível montar a assinatura EIP-712.") from error
    digest = Web3.to_hex(
        Web3.keccak(b"\x19" + bytes(signable.version) + signable.header + signable.body)
    ).lower()
    return signable, digest


def create_participant_acceptance_challenge(
    *,
    token: str,
    wallet_address: str,
    wallet_chain_id: int,
) -> AcceptanceChallengeResult:
    """Create a short-lived EIP-712 challenge after checking the live proposal."""

    link = get_participant_acceptance_link(token)
    participant_address = normalize_wallet_address(wallet_address)
    if participant_address != link.terms.participant.wallet_address_normalized:
        raise PermissionDenied("Conecte a carteira informada na proposta do participante.")
    if not is_valid_wallet_address(participant_address):
        raise ValidationError("Conecte uma carteira EVM válida.")

    web3, contract, nonce, wallet = _check_live_proposal(link)
    _require_individual_wallet(web3, wallet)
    network = link.terms.network
    _ensure_wallet_chain(network, wallet_chain_id)
    deadline = int(timezone.now().timestamp() + ACCEPTANCE_SIGNATURE_LIFETIME.total_seconds())
    typed_data = _build_acceptance_typed_data(
        participant_address=wallet,
        terms_hash=link.terms.terms_hash,
        nonce=nonce,
        deadline=deadline,
        chain_id=network.chain_id,
        contract_address=link.terms.contract.address_normalized,
    )
    _signable, local_digest = _typed_data_digest(typed_data)
    chain_digest = Web3.to_hex(
        contract.functions.participationTermsAcceptanceHash(
            Web3.to_checksum_address(wallet), deadline
        ).call()
    ).lower()
    if local_digest != chain_digest:
        raise ValidationError(
            "A assinatura calculada não corresponde ao contrato. Nenhum aceite foi preparado."
        )

    now = timezone.now()
    with db_transaction.atomic():
        locked_link = ParticipantAcceptanceLink.objects.select_for_update().get(pk=link.pk)
        if not locked_link.is_usable:
            raise ValidationError("Este link de aceite deixou de estar ativo.")
        # Only one unsigned challenge remains pending for a link. Previous
        # challenge records stay available for audit but cannot be replayed.
        ParticipantAcceptanceChallenge.objects.filter(
            link=locked_link,
            status=ParticipantAcceptanceChallenge.Status.PENDING,
        ).update(status=ParticipantAcceptanceChallenge.Status.INVALIDATED)
        challenge = ParticipantAcceptanceChallenge.objects.create(
            link=locked_link,
            chain_nonce=nonce,
            deadline=deadline,
            digest=local_digest,
            typed_data=typed_data,
        )
    return AcceptanceChallengeResult(challenge=challenge, typed_data=typed_data)


def _recover_typed_data_signer(
    typed_data: dict[str, Any],
    signature: str,
) -> str:
    """Recover one EOA signer; contract-wallet signatures are not supported yet."""

    if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
        raise ValidationError("A assinatura não tem o formato EIP-712 esperado.")
    signable, _digest = _typed_data_digest(typed_data)
    try:
        return normalize_wallet_address(
            Account.recover_message(signable, signature=signature)
        )
    except (BadSignature, EthSignatureValidationError, TypeError, ValueError) as error:
        raise ValidationError("A assinatura EIP-712 não pôde ser validada.") from error


def prepare_participant_acceptance(
    *,
    token: str,
    challenge_id: int,
    signature: str,
    wallet_address: str,
    wallet_chain_id: int,
) -> PreparedParticipantTransaction:
    """Validate the participant signature and save the acceptance call before send."""

    link = get_participant_acceptance_link(token)
    wallet = normalize_wallet_address(wallet_address)
    if wallet != link.terms.participant.wallet_address_normalized:
        raise PermissionDenied("A carteira conectada não é a carteira da proposta.")
    if wallet_chain_id != link.terms.network.chain_id:
        raise ValidationError("Conecte a carteira à mesma rede da proposta.")

    try:
        challenge = ParticipantAcceptanceChallenge.objects.select_related(
            "link__terms__participant", "link__terms__network", "link__terms__contract"
        ).get(pk=challenge_id, link=link)
    except ParticipantAcceptanceChallenge.DoesNotExist as error:
        raise ValidationError("O desafio de assinatura não pertence a este link.") from error
    if challenge.status != ParticipantAcceptanceChallenge.Status.PENDING:
        raise ValidationError("Este desafio já foi usado ou invalidado. Solicite outro.")
    now_timestamp = int(timezone.now().timestamp())
    if challenge.deadline <= now_timestamp:
        challenge.status = ParticipantAcceptanceChallenge.Status.INVALIDATED
        challenge.save(update_fields=("status", "updated_at"))
        raise ValidationError("A assinatura expirou. Solicite um novo desafio.")
    if len(signature) > MAX_SIGNATURE_LENGTH:
        raise ValidationError("A assinatura é maior do que o formato aceito.")
    recovered = _recover_typed_data_signer(challenge.typed_data, signature)
    if recovered != wallet:
        raise PermissionDenied(
            "A assinatura foi feita por outra carteira. Conecte a carteira indicada no termo."
        )

    web3, contract, current_nonce, expected_wallet = _check_live_proposal(link)
    _require_individual_wallet(web3, expected_wallet)
    if current_nonce != challenge.chain_nonce:
        raise ValidationError("O nonce mudou desde a assinatura. Solicite uma nova assinatura.")
    if challenge.deadline <= int(timezone.now().timestamp()):
        raise ValidationError("A assinatura expirou antes de preparar a transação.")
    chain_digest = Web3.to_hex(
        contract.functions.participationTermsAcceptanceHash(
            Web3.to_checksum_address(expected_wallet), challenge.deadline
        ).call()
    ).lower()
    if chain_digest != challenge.digest:
        raise ValidationError(
            "O termo, o nonce, a rede ou o contrato mudou. Peça ao participante uma nova assinatura."
        )
    _ensure_wallet_chain(link.terms.network, wallet_chain_id)

    signature_bytes = bytes.fromhex(signature[2:])
    call_data = contract.functions.acceptParticipationTerms(
        Web3.to_checksum_address(expected_wallet),
        challenge.deadline,
        signature_bytes,
    )._encode_transaction_data().lower()

    with db_transaction.atomic():
        locked_link = ParticipantAcceptanceLink.objects.select_for_update().get(pk=link.pk)
        if not locked_link.is_usable:
            raise ValidationError("Este link de aceite expirou ou foi usado.")
        locked_challenge = ParticipantAcceptanceChallenge.objects.select_for_update().get(
            pk=challenge.pk
        )
        if locked_challenge.status != ParticipantAcceptanceChallenge.Status.PENDING:
            raise ValidationError("Este desafio já foi usado. Solicite outro.")
        if int(timezone.now().timestamp()) >= locked_challenge.deadline:
            raise ValidationError("A assinatura expirou. Solicite um novo desafio.")
        existing = _active_attempt(
            link.terms,
            ParticipantRegistrationAttempt.Kind.ACCEPTANCE,
        )
        if existing is not None:
            if existing.acceptance_challenge_id == locked_challenge.pk:
                return _serialize_prepared_attempt(existing)
            raise ValidationError(
                "Já existe um aceite em andamento. Use essa tentativa para conferir a hash."
            )

        locked_challenge.signature = signature.lower()
        locked_challenge.status = ParticipantAcceptanceChallenge.Status.SIGNED
        locked_challenge.signed_at = timezone.now()
        locked_challenge.save(
            update_fields=("signature", "status", "signed_at", "updated_at")
        )
        try:
            attempt = _create_transaction_attempt(
                terms=link.terms,
                network=link.terms.network,
                contract=link.terms.contract,
                kind=ParticipantRegistrationAttempt.Kind.ACCEPTANCE,
                sender_address=wallet,
                prepared_data=call_data,
                acceptance_link=locked_link,
                challenge=locked_challenge,
                signature=signature.lower(),
                deadline=locked_challenge.deadline,
            )
        except IntegrityError as error:
            raise ValidationError(
                "Outro pedido de aceite começou ao mesmo tempo. Atualize a página."
            ) from error
        terms = link.terms
        terms.status = ParticipantTerms.Status.ACCEPTANCE_PREPARED
        terms.save(update_fields=("status", "updated_at"))
    return _serialize_prepared_attempt(attempt)


def create_acceptance_link_url(
    token: str,
    *,
    base_url: str,
) -> str:
    """Build the absolute public page URL without persisting its bearer token."""

    return f"{base_url.rstrip('/')}/participants/accept/{token}/"


def get_participant_attempt_for_company(
    *,
    company_id: int,
    participant_id: int,
    attempt_id: int,
) -> ParticipantRegistrationAttempt:
    """Load a company-owned attempt for authenticated operator endpoints."""

    try:
        return ParticipantRegistrationAttempt.objects.select_related(
            "terms__participant__company", "transaction__network", "transaction__contract"
        ).get(
            pk=attempt_id,
            terms__participant_id=participant_id,
            terms__participant__company_id=company_id,
        )
    except ParticipantRegistrationAttempt.DoesNotExist as error:
        raise ValidationError("A tentativa não pertence a este participante e empresa.") from error


def list_participant_attempts(
    participant: Participant,
) -> list[ParticipantRegistrationAttempt]:
    """Return every proposal and acceptance attempt in visible timeline order."""

    return list(
        ParticipantRegistrationAttempt.objects.filter(
            terms__participant=participant
        )
        .select_related("transaction__network", "acceptance_link")
        .order_by("created_at", "pk")
    )


def current_acceptance_link(
    terms: ParticipantTerms,
) -> ParticipantAcceptanceLink | None:
    """Return the newest active public link, if one remains usable."""

    for link in terms.acceptance_links.order_by("-created_at", "-pk"):
        if link.is_usable:
            return link
    return None


def get_acceptance_attempt_for_link(
    link: ParticipantAcceptanceLink,
) -> ParticipantRegistrationAttempt | None:
    """Return the open or most recent acceptance attempt for the public page."""

    return (
        ParticipantRegistrationAttempt.objects.filter(
            acceptance_link=link,
            kind=ParticipantRegistrationAttempt.Kind.ACCEPTANCE,
        )
        .select_related("transaction__network", "transaction__contract")
        .order_by("-created_at", "-pk")
        .first()
    )
