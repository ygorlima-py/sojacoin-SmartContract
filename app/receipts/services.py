"""Domain services for recording complete local load receipts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from web3 import Web3

from companies.rules.role_access import require_receiving_access
from participants.models import Participant, ParticipantTerms

from .documents import ZERO_BYTES32, crop_year_to_bytes32, read_valid_receipt_pdf, receipt_pdf_hash
from .models import Receipt, ReceiptDocument, TaxInvoice, WeighingTicket


logger = logging.getLogger(__name__)
INVOICE_ACCESS_KEY_RE = re.compile(r"[0-9]{44}\Z", re.ASCII)


@dataclass(frozen=True)
class ReceiptDocumentPayload:
    """Hold a validated private PDF and its exact hash before database writes."""

    kind: str
    original_filename: str
    content: bytes
    content_hash: str


def percentage_to_basis_points(value: Decimal) -> int:
    """Convert a decimal percentage to the contract's hundredths-of-percent unit.

    For example, ``Decimal('15.50')`` becomes ``1550``. Decimal arithmetic is
    used throughout so no binary floating-point rounding reaches contract data.
    """

    try:
        normalized = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError("Informe um percentual decimal válido.") from error
    if not normalized.is_finite() or normalized < 0 or normalized > 100:
        raise ValidationError("O percentual precisa ficar entre 0% e 100%.")
    scaled = normalized * 100
    if scaled != scaled.to_integral_value():
        raise ValidationError("Use no máximo duas casas decimais no percentual.")
    return int(scaled)


def _prepare_document(
    *,
    kind: str,
    upload: UploadedFile | None,
    required: bool,
) -> ReceiptDocumentPayload | None:
    """Read, validate and hash one upload without writing it to storage."""

    if upload is None:
        if required:
            raise ValidationError("Envie todos os documentos obrigatórios em PDF.")
        return None
    content = read_valid_receipt_pdf(upload)
    original_filename = upload.name.replace("\\", "/").rsplit("/", maxsplit=1)[-1][:255]
    return ReceiptDocumentPayload(
        kind=kind,
        original_filename=original_filename,
        content=content,
        content_hash=receipt_pdf_hash(content),
    )


def _persist_document(
    *,
    receipt: Receipt,
    payload: ReceiptDocumentPayload,
    user: AbstractBaseUser,
    stored_file_names: list[str],
) -> ReceiptDocument:
    """Write one validated PDF to private storage and create its immutable row."""

    document = ReceiptDocument(
        receipt=receipt,
        kind=payload.kind,
        original_filename=payload.original_filename,
        file_hash=payload.content_hash,
        size_bytes=len(payload.content),
        uploaded_by=user,
    )
    # Save the file before its database row so its path can be tracked and
    # removed if the encompassing transaction rolls back.
    requested_name = (
        f"receipts/company_{receipt.company_id}/receipt_{receipt.pk}/{uuid4().hex}.pdf"
    )
    stored_name = default_storage.save(requested_name, ContentFile(payload.content))
    stored_file_names.append(stored_name)
    document.pdf_file = stored_name
    document.save()
    return document


def _remove_partial_files(file_names: list[str]) -> None:
    """Best-effort cleanup for storage writes rolled back by a DB error."""

    for file_name in file_names:
        try:
            default_storage.delete(file_name)
        except OSError:
            logger.exception("Could not remove a receipt PDF after database rollback.")


def create_receipt(
    *,
    user: AbstractBaseUser,
    company_id: int,
    participant_id: int,
    unit_id: int,
    crop_year: str,
    gross_weight_kg: int,
    tare_weight_kg: int,
    moisture_percent: Decimal,
    moisture_deduction_kg: int,
    impurities_percent: Decimal,
    impurities_deduction_kg: int,
    damaged_grain_percent: Decimal,
    damaged_grain_deduction_kg: int,
    other_deductions_kg: int,
    weighed_at: datetime,
    invoice_access_key: str,
    weighing_ticket_pdf: UploadedFile,
    invoice_pdf: UploadedFile,
    classification_report_pdf: UploadedFile | None = None,
) -> Receipt:
    """Create a complete company-scoped receipt without sending a blockchain transaction.

    The user must have the receiving role and active unit access. The selected
    unit must already have a confirmed on-chain unit ID, and the participant
    must have completed the SojaCoin registration flow. Database rows are
    committed together; PDF files written before a database error are removed
    on a best-effort basis because filesystem storage cannot join a SQL transaction.

    Args:
        user: Authenticated operator creating the SaaS record.
        company_id: Company taken from the authorized URL context.
        participant_id: Company participant whose registered wallet received the load.
        unit_id: Active, registered unit accessible to the operator.
        crop_year: Human-readable crop year, such as ``2026/2027``.
        gross_weight_kg: Integer gross truck weight in kilograms.
        tare_weight_kg: Integer truck tare in kilograms.
        moisture_percent: Classification percentage with up to two decimal places.
        moisture_deduction_kg: Applied moisture deduction in whole kilograms.
        impurities_percent: Classification percentage with up to two decimal places.
        impurities_deduction_kg: Applied impurities deduction in whole kilograms.
        damaged_grain_percent: Classification percentage with up to two decimal places.
        damaged_grain_deduction_kg: Applied damaged-grain deduction in whole kilograms.
        other_deductions_kg: Other applied deductions in whole kilograms.
        weighed_at: Time shown on the weighing ticket; it must not be in the future.
        invoice_access_key: Brazilian NF-e's 44-digit access key.
        weighing_ticket_pdf: Required source weighing-ticket PDF.
        invoice_pdf: Required source invoice PDF.
        classification_report_pdf: Optional classification report PDF.

    Returns:
        The saved ``Receipt`` with its ``WeighingTicket``, ``TaxInvoice`` and documents.

    Raises:
        PermissionDenied: If the user lacks the receiving role or unit grant.
        ValidationError: If company scope, participant, unit, weights or documents are invalid.
    """

    _membership, unit, _unit_access = require_receiving_access(user, company_id, unit_id)
    if not unit.chain_unit_id:
        raise ValidationError({"unit": "Registre a unidade na blockchain antes de criar recebimentos."})

    participant = (
        Participant.objects.filter(
            pk=participant_id,
            company_id=company_id,
            terms_versions__status=ParticipantTerms.Status.CONFIRMED,
        )
        .distinct()
        .first()
    )
    if participant is None:
        raise ValidationError(
            {"participant": "Selecione um participante desta empresa com cadastro confirmado."}
        )

    crop_year_to_bytes32(crop_year)
    if not INVOICE_ACCESS_KEY_RE.fullmatch(invoice_access_key.strip()):
        raise ValidationError({"invoice_access_key": "A chave da NF-e deve conter 44 dígitos."})
    if weighed_at.tzinfo is None or timezone.is_naive(weighed_at):
        raise ValidationError({"weighed_at": "A data e hora da pesagem precisam incluir o fuso local."})
    if weighed_at > timezone.now():
        raise ValidationError({"weighed_at": "A pesagem não pode estar no futuro."})

    percentages = {
        "moisture_bps": percentage_to_basis_points(moisture_percent),
        "impurities_bps": percentage_to_basis_points(impurities_percent),
        "damaged_grain_bps": percentage_to_basis_points(damaged_grain_percent),
    }
    deductions = (
        moisture_deduction_kg
        + impurities_deduction_kg
        + damaged_grain_deduction_kg
        + other_deductions_kg
    )
    if any(
        value < 0
        for value in (
            gross_weight_kg,
            tare_weight_kg,
            moisture_deduction_kg,
            impurities_deduction_kg,
            damaged_grain_deduction_kg,
            other_deductions_kg,
        )
    ):
        raise ValidationError("Pesos e descontos não podem ser negativos.")
    if gross_weight_kg <= tare_weight_kg:
        raise ValidationError({"gross_weight_kg": "O peso bruto precisa ser maior que a tara."})
    if gross_weight_kg - tare_weight_kg <= deductions:
        raise ValidationError("A tara e os descontos precisam deixar peso líquido maior que zero.")

    document_payloads = [
        _prepare_document(
            kind=ReceiptDocument.Kind.WEIGHING_TICKET,
            upload=weighing_ticket_pdf,
            required=True,
        ),
        _prepare_document(
            kind=ReceiptDocument.Kind.CLASSIFICATION_REPORT,
            upload=classification_report_pdf,
            required=False,
        ),
        _prepare_document(
            kind=ReceiptDocument.Kind.TAX_INVOICE,
            upload=invoice_pdf,
            required=True,
        ),
    ]
    prepared_documents = [payload for payload in document_payloads if payload is not None]

    stored_file_names: list[str] = []
    try:
        with transaction.atomic():
            receipt = Receipt(
                company_id=company_id,
                participant=participant,
                unit=unit,
                crop_year=crop_year,
                status=Receipt.Status.READY_FOR_BLOCKCHAIN,
                created_by=user,
            )
            receipt.save()

            document_rows: dict[str, ReceiptDocument] = {}
            for payload in prepared_documents:
                document = _persist_document(
                    receipt=receipt,
                    payload=payload,
                    user=user,
                    stored_file_names=stored_file_names,
                )
                document_rows[payload.kind] = document

            WeighingTicket.objects.create(
                receipt=receipt,
                gross_weight_kg=gross_weight_kg,
                tare_weight_kg=tare_weight_kg,
                net_weight_kg=gross_weight_kg - tare_weight_kg - deductions,
                moisture_bps=percentages["moisture_bps"],
                impurities_bps=percentages["impurities_bps"],
                damaged_grain_bps=percentages["damaged_grain_bps"],
                moisture_deduction_kg=moisture_deduction_kg,
                impurities_deduction_kg=impurities_deduction_kg,
                damaged_grain_deduction_kg=damaged_grain_deduction_kg,
                other_deductions_kg=other_deductions_kg,
                weighing_ticket_hash=document_rows[
                    ReceiptDocument.Kind.WEIGHING_TICKET
                ].file_hash,
                report_hash=(
                    document_rows[ReceiptDocument.Kind.CLASSIFICATION_REPORT].file_hash
                    if ReceiptDocument.Kind.CLASSIFICATION_REPORT in document_rows
                    else ZERO_BYTES32
                ),
                weighed_at=weighed_at,
            )
            TaxInvoice.objects.create(
                receipt=receipt,
                access_key=invoice_access_key.strip(),
                access_key_hash=Web3.to_hex(Web3.keccak(text=invoice_access_key.strip())).lower(),
                document=document_rows[ReceiptDocument.Kind.TAX_INVOICE],
            )
            return receipt
    except Exception:
        _remove_partial_files(stored_file_names)
        raise
