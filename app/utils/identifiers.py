"""Normalization and validation helpers for shared identifiers."""

import re


def normalize_cnpj(value: str) -> str:
    """Return the canonical CNPJ without punctuation and in uppercase."""

    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _cnpj_character_value(character: str) -> int:
    """Map an alphanumeric CNPJ character to its check-digit value."""

    # The alphanumeric CNPJ maps ASCII characters by subtracting 48:
    # digits keep values 0-9 and A-Z map to 17-42.
    return ord(character) - 48


def _cnpj_check_digit(characters: str, weights: tuple[int, ...]) -> int:
    """Calculate one CNPJ check digit from its characters and weights."""

    total = sum(
        _cnpj_character_value(character) * weight
        for character, weight in zip(characters, weights)
    )
    remainder = total % 11
    return 0 if remainder < 2 else 11 - remainder


def is_valid_cnpj(value: str) -> bool:
    """Return whether a normalized alphanumeric CNPJ has valid check digits."""

    if not re.fullmatch(r"[A-Z0-9]{12}\d{2}", value):
        return False

    first_digit = _cnpj_check_digit(value[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second_digit = _cnpj_check_digit(
        value[:12] + str(first_digit),
        (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    )
    return value[-2:] == f"{first_digit}{second_digit}"


def normalize_wallet_address(value: str) -> str:
    """Return a public EVM address in a stable lowercase representation."""

    return (value or "").strip().lower()


def is_valid_wallet_address(value: str) -> bool:
    """Return whether a value is a canonical 20-byte EVM address."""

    return bool(re.fullmatch(r"0x[0-9a-f]{40}", value))


def normalize_bytes32_hash(value: str) -> str:
    """Return a hexadecimal bytes32 value in a stable lowercase form."""

    return (value or "").strip().lower()


def is_valid_bytes32_hash(value: str) -> bool:
    """Return whether a value is a 32-byte hexadecimal hash."""

    return bool(re.fullmatch(r"0x[0-9a-f]{64}", value))


def normalize_transaction_hash(value: str) -> str:
    """Return a transaction hash in the same canonical form as bytes32."""

    return normalize_bytes32_hash(value)


def is_valid_transaction_hash(value: str) -> bool:
    """Return whether a value is a canonical 32-byte transaction hash."""

    return is_valid_bytes32_hash(value)
