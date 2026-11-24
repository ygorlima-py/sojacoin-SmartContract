"""Rules for preparing a unit registration hash.

The unit registry contract stores a ``bytes32`` registration hash, not the
registration form itself. This module defines the canonical representation so
the same form data always produces the same Ethereum Keccak-256 hash.
"""

import json
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from ..models import Unit
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_cnpj,
    is_valid_wallet_address,
    normalize_cnpj,
    normalize_wallet_address,
)

if TYPE_CHECKING:
    from blockchain.models import BlockchainTransaction
    from blockchain.services.transaction_confirmation import DecodedContractEvent


_MASK_64 = (1 << 64) - 1
_RATE_BYTES = 136  # Keccak-256 rate: 1088 bits.
_REGISTRATION_FIELDS = (
    "address",
    "cnpj",
    "code",
    "name",
    "state_registration",
)
UNIT_REGISTRATION_OPERATION = "unit_registration"
UNIT_REGISTRY_CONTRACT_KIND = "unit_registry"

# Rotation offsets indexed as [x][y] for the 5x5 Keccak state.
_ROTATION_OFFSETS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_ROUND_CONSTANTS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)


def _rotate_left(value: int, amount: int) -> int:
    """Rotate one 64-bit Keccak lane to the left."""
    if amount == 0:
        return value & _MASK_64
    return ((value << amount) | (value >> (64 - amount))) & _MASK_64


def _keccak_f(state: list[int]) -> None:
    """Apply the Keccak-f[1600] permutation in place."""
    for round_constant in _ROUND_CONSTANTS:
        # Theta: mix each column into every lane in that column.
        column_parity = [
            state[x]
            ^ state[x + 5]
            ^ state[x + 10]
            ^ state[x + 15]
            ^ state[x + 20]
            for x in range(5)
        ]
        correction = [
            column_parity[(x - 1) % 5]
            ^ _rotate_left(column_parity[(x + 1) % 5], 1)
            for x in range(5)
        ]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= correction[x]

        # Rho and Pi: rotate lanes and move them to their new coordinates.
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                new_x = y
                new_y = (2 * x + 3 * y) % 5
                moved[new_x + 5 * new_y] = _rotate_left(
                    state[x + 5 * y], _ROTATION_OFFSETS[x][y]
                )

        # Chi: non-linear row transformation.
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = moved[x + 5 * y] ^ (
                    (~moved[(x + 1) % 5 + 5 * y])
                    & moved[(x + 2) % 5 + 5 * y]
                )
                state[x + 5 * y] &= _MASK_64

        # Iota: inject the round constant into the first lane.
        state[0] ^= round_constant


def keccak256(value: bytes) -> bytes:
    """Return the Ethereum-compatible Keccak-256 digest for ``value``.

    Ethereum uses Keccak padding (domain byte ``0x01``), which differs from
    the standardized SHA3-256 padding. The implementation is kept local so
    the business rule does not silently use the wrong digest when the wallet
    integration is not installed in the development environment.
    """
    state = [0] * 25
    padded = bytearray(value)
    padded.append(0x01)
    while len(padded) % _RATE_BYTES != _RATE_BYTES - 1:
        padded.append(0)
    padded.append(0x80)

    for offset in range(0, len(padded), _RATE_BYTES):
        block = padded[offset : offset + _RATE_BYTES]
        for lane in range(_RATE_BYTES // 8):
            start = lane * 8
            state[lane] ^= int.from_bytes(block[start : start + 8], "little")
        _keccak_f(state)

    return b"".join(lane.to_bytes(8, "little") for lane in state)[:32]


def _normalize_text(value: object) -> str:
    """Normalize human-entered text without changing its meaning."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.strip().split())


def normalize_unit_registration_data(data: Mapping[str, object]) -> dict[str, str]:
    """Return the canonical fields that participate in a unit registration.

    Database identifiers, timestamps, ``active`` and ``chain_unit_id`` are
    deliberately excluded because they describe SaaS or blockchain state, not
    the registration form signed by the company.
    """
    normalized = {
        "address": _normalize_text(data.get("address")),
        "cnpj": normalize_cnpj(_normalize_text(data.get("cnpj"))),
        "code": _normalize_text(data.get("code")).upper(),
        "name": _normalize_text(data.get("name")),
        "state_registration": _normalize_text(data.get("state_registration")).upper(),
    }

    required_errors = {
        field: "Este campo é obrigatório para o registro da unidade."
        for field in _REGISTRATION_FIELDS
        if not normalized[field]
    }
    if required_errors:
        raise ValidationError(required_errors)

    if not is_valid_cnpj(normalized["cnpj"]):
        raise ValidationError({"cnpj": "Informe um CNPJ válido."})

    return normalized


def build_unit_registration_payload(data: Mapping[str, object]) -> str:
    """Build the deterministic JSON payload used as the hash input."""
    normalized = normalize_unit_registration_data(data)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def calculate_unit_registration_hash(data: Mapping[str, object]) -> str:
    """Return the ``0x``-prefixed hash sent as ``registrationHash`` on-chain."""
    payload = build_unit_registration_payload(data)
    return f"0x{keccak256(payload.encode('utf-8')).hex()}"


def save_unit_registration(unit: "Unit", data: Mapping[str, object]) -> "Unit":
    """Normalize, hash and persist a unit registration in one application step.

    The caller must validate ``UnitForm`` before calling this service and pass
    its cleaned data. A new unit can never receive a blockchain ID here;
    existing IDs are left untouched for edits.

    This function only writes the local Django record. The separate on-chain
    flow sends ``unit.registration_hash`` to ``registerUnit``; after the
    ``UnitRegistered`` event is confirmed, its handler fills ``chain_unit_id``.
    """
    is_new_unit = unit._state.adding
    normalized = normalize_unit_registration_data(data)
    unit.name = normalized["name"]
    unit.code = normalized["code"]
    unit.address = normalized["address"]
    unit.cnpj = normalized["cnpj"]
    unit.state_registration = normalized["state_registration"]
    unit.registration_hash = calculate_unit_registration_hash(normalized)
    if is_new_unit:
        unit.chain_unit_id = None
    unit.full_clean()
    unit.save()
    return unit


def _event_bytes32(value: object, field_name: str) -> bytes:
    """Validate and return one decoded Solidity ``bytes32`` event value."""

    if not isinstance(value, bytes) or len(value) != 32:
        raise ValidationError(f"O campo {field_name} do evento deve conter 32 bytes.")
    return value


def handle_unit_registered_event(
    transaction: "BlockchainTransaction",
    event: "DecodedContractEvent",
) -> None:
    """Link a confirmed ``UnitRegistered`` event to its local unit.

    The company, unit code, registration hash, contract type and on-chain caller
    must match the submitted transaction before ``chain_unit_id`` is saved.
    Other events in the same receipt are ignored by this handler.
    """

    if event.event_name != "UnitRegistered":
        return

    if transaction.operation_type != UNIT_REGISTRATION_OPERATION:
        raise ValidationError("O evento UnitRegistered não corresponde à operação.")
    if (
        transaction.contract is None
        or transaction.contract.kind != UNIT_REGISTRY_CONTRACT_KIND
    ):
        raise ValidationError("O registro da unidade não usou um contrato UnitRegistry.")
    if (
        transaction.contract.company_id != transaction.company_id
        or transaction.contract.network_id != transaction.network_id
    ):
        raise ValidationError(
            "O contrato UnitRegistry não pertence à empresa e à rede da transação."
        )

    try:
        raw_unit_id: object = event.args["unitId"]
        raw_code: object = event.args["code"]
        raw_registration_hash: object = event.args["registrationHash"]
        raw_registered_by: object = event.args["registeredBy"]
    except KeyError as error:
        raise ValidationError("O evento UnitRegistered está incompleto.") from error

    if (
        isinstance(raw_unit_id, bool)
        or not isinstance(raw_unit_id, int)
        or raw_unit_id < 1
    ):
        raise ValidationError("O ID da unidade informado pelo evento é inválido.")

    code_bytes = _event_bytes32(raw_code, "code")
    try:
        unit_code = code_bytes.rstrip(b"\x00").decode("utf-8").strip().upper()
    except UnicodeDecodeError as error:
        raise ValidationError("O código da unidade no evento não é texto UTF-8 válido.") from error
    if not unit_code:
        raise ValidationError("O evento não contém um código de unidade.")

    registration_hash_bytes = _event_bytes32(
        raw_registration_hash,
        "registrationHash",
    )
    registration_hash = f"0x{registration_hash_bytes.hex()}"
    if not is_valid_bytes32_hash(registration_hash):
        raise ValidationError("A hash de registro informada pelo evento é inválida.")

    if not isinstance(raw_registered_by, str):
        raise ValidationError("A carteira registrada no evento é inválida.")
    registered_by = normalize_wallet_address(raw_registered_by)
    if not is_valid_wallet_address(registered_by):
        raise ValidationError("A carteira registrada no evento é inválida.")
    if registered_by != transaction.sender_address:
        raise ValidationError("A carteira do evento não corresponde à transação enviada.")

    with db_transaction.atomic():
        try:
            # Company plus code prevents an event from another company from
            # attaching its unit ID to this company's local record.
            unit = Unit.objects.select_for_update().get(
                company_id=transaction.company_id,
                code=unit_code,
            )
        except Unit.DoesNotExist as error:
            raise ValidationError(
                "Não existe unidade local correspondente ao evento da blockchain."
            ) from error

        if unit.registration_hash != registration_hash:
            raise ValidationError(
                "A hash do evento não corresponde ao cadastro local da unidade."
            )

        if unit.chain_unit_id is not None:
            if unit.chain_unit_id != raw_unit_id:
                raise ValidationError(
                    "A unidade já está vinculada a outro ID da blockchain."
                )
            return

        unit.chain_unit_id = raw_unit_id
        unit.full_clean()
        unit.save()
