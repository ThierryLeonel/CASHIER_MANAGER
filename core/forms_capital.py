"""Forms for capital movements (finance manager, senior manager, super administrator)."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import CustomerField, MoneyField
from core.models import Account, Counterparty
from core.services import capital as cap


class AccountChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return cap.account_label(obj)


class _CapitalForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        for f in self.fields.values():
            if isinstance(f, AccountChoice):
                f.queryset = cap.account_choices()

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d


class InjectForm(_CapitalForm):
    account = AccountChoice(label=_("Account receiving the capital"), queryset=None,
                            help_text=_("A cashier's cash drawer or UVE wallet, a master SIM, a bank account or a "
                                        "shop account."))
    amount = MoneyField(label=_("Amount (FCFA)"))
    source = forms.CharField(label=_("Source of the funds"), max_length=255,
                             help_text=_("Where the money comes from, e.g. owner's savings, bank loan, partner."))
    purpose = forms.CharField(label=_("Purpose"), max_length=255,
                              help_text=_("What it is for, e.g. increase the float of Fako 1."))
    description = forms.CharField(label=_("Description"), max_length=255, required=False)


class WithdrawForm(_CapitalForm):
    account = AccountChoice(label=_("Account the capital leaves from"), queryset=None)
    amount = MoneyField(label=_("Amount (FCFA)"))
    beneficiary = forms.CharField(label=_("Paid to / where it goes"), max_length=255,
                                  help_text=_("Who receives the money, e.g. the owner, a partner, a bank."))
    purpose = forms.CharField(label=_("Purpose"), max_length=255)
    description = forms.CharField(label=_("Description"), max_length=255, required=False)


class CapitalTransferForm(_CapitalForm):
    source = AccountChoice(label=_("From account"), queryset=None)
    destination = AccountChoice(label=_("To account"), queryset=None,
                                help_text=_("Same product (cash, UVE, ER, MTN), or cash ↔ bank."))
    amount = MoneyField(label=_("Amount (FCFA)"))
    purpose = forms.CharField(label=_("Purpose"), max_length=255)
    description = forms.CharField(label=_("Description"), max_length=255, required=False)


class FloatExchangeForm(_CapitalForm):
    source = AccountChoice(label=_("Float leaves from"), queryset=None)
    amount = MoneyField(label=_("Amount (FCFA)"))
    to = forms.ChoiceField(label=_("UVE goes to"), choices=[("DESK", _("A cashier")), ("AGENT", _("A commercial agent"))],
                           widget=forms.RadioSelect, initial="DESK")
    uve_account = AccountChoice(label=_("Cashier's UVE wallet"), queryset=None, required=False)
    agent = CustomerField(label=_("Commercial agent (business SIM)"), kind=Counterparty.Kind.AGENT, required=False)
    description = forms.CharField(label=_("Note"), max_length=255, required=False)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["source"].queryset = cap.account_choices().filter(kind=Account.Kind.MTN)
        self.fields["uve_account"].queryset = cap.account_choices().filter(kind=Account.Kind.UVE, shop__isnull=True)

    def clean(self):
        cd = super().clean()
        if cd.get("to") == "AGENT" and not cd.get("agent"):
            self.add_error("agent", _("Choose the commercial agent who receives the UVE."))
        if cd.get("to") == "DESK" and not cd.get("uve_account"):
            self.add_error("uve_account", _("Choose the cashier's UVE wallet that receives the UVE."))
        return cd
