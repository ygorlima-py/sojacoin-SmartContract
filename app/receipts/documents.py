"""Private PDF validation and byte-encoding helpers for load receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from web3 import Web3


class SeekableBinaryDocument(Protocol):
    """Describe the bounded, seekable file API used by uploaded PDFs."""

    name: str
    size: int

    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def tell(self) -> int: ...


DEFAULT_MAX_RECEIPT_PDF_SIZE = 10 * 1024 * 1024
ZERO_BYTES32 = "0x" + "00" * 32


def receipt_pdf_max_size() -> int:
    """Return the configured PDF limit and reject invalid deployment settings."""

    maximum_size = int(
        getattr(
            settings,
            "RECEIPT_DOCUMENT_MAX_PDF_SIZE",
            DEFAULT_MAX_RECEIPT_PDF_SIZE,
        )
    )
    if maximum_size <= 0:
        raise ImproperlyConfigured("RECEIPT_DOCUMENT_MAX_PDF_SIZE must be positive.")
    return maximum_size


def read_valid_receipt_pdf(document: SeekableBinaryDocument) -> bytes:
    """Read an uploaded PDF after bounded size, extension and signature checks.

    The function restores the original file position. It does not alter or
    normalize the uploaded bytes, because the stored Keccak digest must identify
    the exact file the operator supplied.
    """

    safe_name = Path((document.name or "").replace("\\", "/")).name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise ValidationError("Envie este documento em um arquivo PDF.")

    maximum_size = receipt_pdf_max_size()
    if document.size <= 0 or document.size > maximum_size:
        raise ValidationError("O PDF está vazio ou excede o limite permitido.")

    original_position = document.tell()
    try:
        document.seek(0)
        content = document.read(maximum_size + 1)
    finally:
        document.seek(original_position)

    if len(content) > maximum_size:
        raise ValidationError("O PDF excede o limite permitido.")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValidationError("O arquivo enviado não parece ser um PDF válido.")
    return content


def receipt_pdf_hash(pdf_bytes: bytes) -> str:
    """Return the lowercase EVM-compatible Keccak-256 digest of exact PDF bytes."""

    return Web3.to_hex(Web3.keccak(primitive=pdf_bytes)).lower()


def crop_year_to_bytes32(crop_year: str) -> str:
    """Encode a crop-year identifier as UTF-8 right-padded to exactly 32 bytes.

    Solidity compares crop-year values as ``bytes32``. Storing the encoded form
    alongside the human-readable value ensures the later wallet request uses
    the same deterministic representation.
    """

    normalized = (crop_year or "").strip()
    if not normalized:
        raise ValidationError({"crop_year": "Informe a safra."})

    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValidationError({"crop_year": "A safra não pode ser codificada em UTF-8."}) from error

    if b"\x00" in encoded or len(encoded) > 32:
        raise ValidationError(
            {"crop_year": "A safra precisa ocupar de 1 a 32 bytes UTF-8 e não pode conter NUL."}
        )
    return "0x" + encoded.ljust(32, b"\x00").hex()
