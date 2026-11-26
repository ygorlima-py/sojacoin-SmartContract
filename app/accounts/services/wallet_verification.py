"""Services for linking a user's EVM wallet and proving address ownership."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature
from eth_utils.exceptions import ValidationError as EthSignatureValidationError
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from web3 import Web3

from blockchain.models import Network
from utils.identifiers import is_valid_wallet_address, normalize_wallet_address

from ..models import UserWallet, Wallet, WalletVerificationChallenge
from ..rules.account_access import is_active_account
from ..rules.wallet_access import consume_wallet_challenge

CHALLENGE_LIFETIME = timedelta(minutes=5)


def create_wallet_verification_challenge(
    user: AbstractBaseUser,
    address: str,
    chain_id: int,
    *,
    domain: str = "SojaCoin",
    at: datetime | None = None,
) -> WalletVerificationChallenge:
    """Create a short-lived, one-use challenge for the user's public address.

    The selected chain must be configured and active in the SaaS. A new request
    cancels older pending challenges for the same user-wallet pair, preserving
    their records and preventing multiple usable signatures.
    """

    if not is_active_account(user):
        raise ValidationError("Sua conta não está ativa.")

    normalized_address = normalize_wallet_address(address)
    if not is_valid_wallet_address(normalized_address):
        raise ValidationError({"address": "Informe um endereço EVM válido."})
    if not isinstance(chain_id, int) or isinstance(chain_id, bool) or chain_id <= 0:
        raise ValidationError({"chain_id": "Selecione uma rede válida."})

    network = Network.objects.filter(chain_id=chain_id, active=True).first()
    if network is None:
        raise ValidationError({"chain_id": "A rede selecionada não está ativa no SaaS."})

    issued_at = at or timezone.now()
    expires_at = issued_at + CHALLENGE_LIFETIME
    nonce = uuid.uuid4()
    normalized_domain = (domain or "").strip() or "SojaCoin"

    # Keep the full readable message in the database so signature recovery
    # always checks the exact text presented to the wallet.
    message = "\n".join(
        (
            "SojaCoin — verificação de carteira",
            "",
            "Assine esta mensagem para confirmar que você controla este endereço.",
            "Esta assinatura não envia uma transação e não cobra taxa de rede.",
            "",
            f"Aplicação: {normalized_domain}",
            f"Endereço: {normalized_address}",
            f"Rede: {network.name} (chain ID {network.chain_id})",
            f"Código único: {nonce}",
            f"Emitida em: {issued_at.isoformat()}",
            f"Expira em: {expires_at.isoformat()}",
        )
    )
    digest = Web3.to_hex(Web3.keccak(text=message))

    with transaction.atomic():
        # Address uniqueness is global, while the user-wallet link is specific
        # to this account. Existing wallets can therefore be shared explicitly.
        wallet = Wallet.objects.filter(address_normalized=normalized_address).first()
        if wallet is None:
            try:
                # A nested savepoint lets us recover cleanly if another request
                # creates this globally unique address at the same time.
                with transaction.atomic():
                    wallet = Wallet.objects.create(address=normalized_address)
            except IntegrityError:
                wallet = Wallet.objects.get(address_normalized=normalized_address)

        if not wallet.active:
            raise ValidationError({"address": "Esta carteira está desativada."})

        user_wallet, _created = UserWallet.objects.get_or_create(
            user=user,
            wallet=wallet,
            defaults={"verification_status": UserWallet.VerificationStatus.PENDING},
        )
        if user_wallet.verification_status == UserWallet.VerificationStatus.VERIFIED:
            raise ValidationError({"address": "Esta carteira já está verificada na sua conta."})

        # Keep revoked links immutable from this self-service flow. Linking a
        # replacement address creates a separate UserWallet record and retains
        # the original revocation state for review.
        if user_wallet.verification_status == UserWallet.VerificationStatus.REVOKED:
            raise ValidationError(
                {"address": "Esta carteira foi revogada. Vincule outra carteira."}
            )

        pending_challenges = WalletVerificationChallenge.objects.filter(
            user_wallet=user_wallet,
            status=WalletVerificationChallenge.Status.PENDING,
        )
        for old_challenge in pending_challenges:
            old_challenge.status = WalletVerificationChallenge.Status.CANCELLED
            old_challenge.cancellation_reason = "Substituído por um novo desafio."
            old_challenge.cancelled_at = issued_at
            old_challenge.save()

        challenge = WalletVerificationChallenge.objects.create(
            user_wallet=user_wallet,
            message=message,
            digest=digest,
            domain=normalized_domain,
            chain_id=network.chain_id,
            expires_at=expires_at,
        )

    return challenge


def verify_wallet_challenge_signature(
    challenge: WalletVerificationChallenge,
    signature: str,
) -> bool:
    """Recover the signer of the exact MetaMask ``personal_sign`` message."""

    try:
        recovered_address = Account.recover_message(
            encode_defunct(text=challenge.message),
            signature=signature,
        )
    except (BadSignature, EthSignatureValidationError, TypeError, ValueError):
        return False

    return normalize_wallet_address(recovered_address) == challenge.user_wallet.wallet.address_normalized


def consume_wallet_verification_challenge(
    user: AbstractBaseUser,
    challenge_id: int,
    signature: str,
    *,
    at: datetime | None = None,
) -> WalletVerificationChallenge:
    """Verify and consume a challenge through the existing account rule."""

    return consume_wallet_challenge(
        user,
        challenge_id,
        signature,
        signature_validator=verify_wallet_challenge_signature,
        at=at,
    )
