"""Register deployed contract addresses for a company in the local database."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from companies.models import Company

from ..models import ContractInstance, Network


def register_contract(
    *,
    company: Company,
    network: Network,
    kind: str,
    address: str,
    version: str,
    deployment_transaction_hash: str = "",
) -> ContractInstance:
    """Save one deployed contract's address for a company and network.

    The caller must authorize the user before calling this service. Address,
    kind, version and duplicate constraints are validated by ContractInstance.
    This local registration does not verify the deployment through RPC.
    """

    if company.pk is None or network.pk is None:
        raise ValidationError("A empresa e a rede devem estar cadastradas.")

    try:
        with transaction.atomic():
            # Read current state from the database rather than trusting an old object.
            saved_company = Company.objects.select_for_update().get(pk=company.pk)
            saved_network = Network.objects.select_for_update().get(pk=network.pk)
            if not saved_company.active:
                raise ValidationError("A empresa está inativa.")
            if not saved_network.active:
                raise ValidationError("A rede está inativa.")

            contract = ContractInstance(
                company=saved_company,
                network=saved_network,
                kind=kind,
                address=address,
                version=version,
                deployment_transaction_hash=deployment_transaction_hash,
            )
            # save() normalizes the address and runs model and uniqueness checks.
            contract.save()
            return contract
    except (Company.DoesNotExist, Network.DoesNotExist) as error:
        raise ValidationError("Empresa ou rede não encontrada.") from error
    except IntegrityError as error:
        # The database remains the final protection against concurrent inserts.
        raise ValidationError(
            "Não foi possível cadastrar o contrato: verifique os dados e duplicidades."
        ) from error
