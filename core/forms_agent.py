"""Forms of the commercial agent portal, and of the cashier confirming an agent's payback."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import MoneyField, amount_boxes
from core.models import ClientKind, Shop, Zone
from core.services import agents as ag


class _AgentForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d


class _ClientForm(_AgentForm):
    client_kind = forms.ChoiceField(label=_("Client"), choices=ClientKind.choices, widget=forms.RadioSelect,
                                    initial=ClientKind.POS)
    pos_sim = forms.CharField(label=_("POS business SIM"), required=False, max_length=20,
                              widget=forms.TextInput(attrs={"list": "zone-pos", "inputmode": "tel",
                                                            "autocomplete": "off", "placeholder": "6XX XX XX XX"}),
                              help_text=_("Type or pick the POS. A POS outside your zone can also be supplied: type "
                                          "its business SIM."))
    shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.none(), required=False)
    client_name = forms.CharField(label=_("Client name"), required=False, max_length=120)
    client_phone = forms.CharField(label=_("Client phone"), required=False, max_length=30)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["shop"].queryset = Shop.objects.filter(active=True).order_by("name")


class AgentSupplyForm(_ClientForm):
    uve = MoneyField(label=_("UVE"), required=False)
    er = MoneyField(label=_("ER"), required=False)
    cash = MoneyField(label=_("Cash"), required=False)
    paid = MoneyField(label=_("Paid now by the client"), required=False,
                      help_text=_("What the client paid you on the spot. The rest is owed to you."))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        amount_boxes(self, ["uve", "er", "cash"], _("Fill what you supplied, in one or several products."),
                     str(_("Total supplied")))


class ClientPaymentForm(_ClientForm):
    amount = MoneyField(label=_("Amount received (FCFA)"))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)


class PaybackForm(_AgentForm):
    desk = forms.ModelChoiceField(label=_("Cashier of your zone"), queryset=Zone.objects.none(),
                                  help_text=_("UVE goes to this desk's master SIM, cash and MTN float to its cashier."))
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    mtn = MoneyField(label=_("MTN float"), required=False)
    reference = forms.CharField(label=_("Transfer reference"), required=False, max_length=60,
                                help_text=_("E.g. the Orange Money or MTN transaction ID, to help the cashier find it."))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        qs = ag.desks_for(user.counterparty)
        self.fields["desk"].queryset = qs
        if not self.is_bound and qs.count() == 1:
            self.initial["desk"] = qs.first()
        amount_boxes(self, ["cash", "uve", "mtn"], _("Fill what you send back, in one or several products. The "
                                                     "cashier confirms it when received."), str(_("Total paid back")))


class CancelForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=255)
