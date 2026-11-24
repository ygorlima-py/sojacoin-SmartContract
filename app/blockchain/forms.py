"""Forms used by the assisted contract deployment flow in Django Admin."""

from django import forms
from django.core.exceptions import ValidationError

from companies.models import Company
from utils.identifiers import is_valid_wallet_address, normalize_wallet_address

from .models import Network


class NetworkChoiceField(forms.ModelChoiceField):
    """Show the chain ID beside each network to prevent selection mistakes."""

    def label_from_instance(self, network: Network) -> str:
        """Return a readable network name with its unique chain identifier."""

        return f"{network.name} — ID {network.chain_id}"


class DeploymentProcessStartForm(forms.Form):
    """Collect the company, network and public owner address for a deployment."""

    company = forms.ModelChoiceField(
        queryset=Company.objects.filter(active=True),
        label="Empresa",
        empty_label="Selecione uma empresa",
    )
    network = NetworkChoiceField(
        queryset=Network.objects.filter(active=True).exclude(rpc_url=""),
        label="Rede blockchain",
        empty_label="Selecione uma rede",
    )
    owner_address = forms.CharField(
        label="Carteira pública do administrador",
        max_length=42,
        help_text=(
            "Será definida como dona da SojaCoin e do registro de unidades. Depois de "
            "iniciar, não poderá ser alterada neste processo. Informe somente o endereço "
            "público, nunca a chave privada."
        ),
        widget=forms.TextInput(
            attrs={"class": "vTextField", "autocomplete": "off", "spellcheck": "false"}
        ),
    )

    def clean_owner_address(self) -> str:
        """Normalize the address and reject invalid or zero addresses."""

        address = normalize_wallet_address(self.cleaned_data["owner_address"])
        zero_address = "0x" + "0" * 40
        if not is_valid_wallet_address(address) or address == zero_address:
            raise ValidationError("Informe um endereço público de carteira válido.")
        return address
