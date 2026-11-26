"""Validation and hashing helpers for company-approved participant term PDFs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from web3 import Web3


class BinaryDocument(Protocol):
    """Describe the seekable file interface used by Django uploads and storage."""

    name: str
    size: int

    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def tell(self) -> int: ...


DEFAULT_MAX_TERMS_PDF_SIZE = 10 * 1024 * 1024


def participant_terms_pdf_max_size() -> int:
    """Return the configured upload limit, rejecting an unusable setting."""

    maximum_size = int(
        getattr(settings, "PARTICIPANT_TERMS_MAX_PDF_SIZE", DEFAULT_MAX_TERMS_PDF_SIZE)
    )
    if maximum_size <= 0:
        raise ImproperlyConfigured("PARTICIPANT_TERMS_MAX_PDF_SIZE must be positive.")
    return maximum_size


def participant_terms_pdf_max_size_label() -> str:
    """Format the configured upload limit in megabytes for the interface."""

    megabytes = participant_terms_pdf_max_size() / (1024 * 1024)
    return f"{megabytes:g}".replace(".", ",")


def read_valid_participant_terms_pdf(document: BinaryDocument) -> bytes:
    """Read an unchanged PDF after checking its extension, size and file markers."""

    # Treat Windows separators as separators too, even when the server runs on
    # Linux, so a client-provided path is never mistaken for a clean filename.
    filename = Path((document.name or "").replace("\\", "/")).name
    if Path(filename).suffix.lower() != ".pdf":
        raise ValidationError("Envie o termo da empresa em um arquivo PDF.")

    maximum_size = participant_terms_pdf_max_size()
    if document.size <= 0 or document.size > maximum_size:
        raise ValidationError("O PDF está vazio ou excede o limite configurado.")

    original_position = document.tell()
    try:
        document.seek(0)
        # Read one byte past the configured limit so untrusted uploads cannot
        # cause an unbounded in-memory read if their reported size is wrong.
        content = document.read(maximum_size + 1)
    finally:
        document.seek(original_position)
    if len(content) > maximum_size:
        raise ValidationError("O PDF excede o limite configurado.")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValidationError("O arquivo enviado não parece ser um PDF válido.")
    return content


def participant_terms_pdf_hash(pdf_bytes: bytes) -> str:
    """Return the EVM-compatible Keccak-256 hash of the exact PDF bytes."""

    return Web3.to_hex(Web3.keccak(primitive=pdf_bytes)).lower()
