"""Forms of the commercial agent portal, and of the cashier / shop confirming an agent's payback."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import MoneyField
from core.models import ClientKind, Shop, Zone
from core.services import agents as ag

PRODUCT_CHOICES = [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash"))]
PRODUCT_FIELD = {"UVE": "uve", "ER": "er", "CASH": "cash"}


class NumberSelect(forms.Select):
    """A select whose options carry the Orange number to send to (data-sim), shown under the field."""

    def __init__(self, number_of, *args, **kwargs):
        self.number_of = number_of
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        obj = getattr(value, "instance", None)
        if obj is not None:
            opt["attrs"]["data-sim"] = self.number_of(obj) or ""
        return opt


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


def _shop_field(required=False):
    return forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.none(), required=required,
                                  widget=NumberSelect(lambda s: ag._norm(s.sim_number)))


class _ClientForm(_AgentForm):
    client_kind = forms.ChoiceField(label=_("Client"), choices=[c for c in ClientKind.choices if c[0] != "AGENT"],
                                    widget=forms.RadioSelect, initial=ClientKind.POS)
    pos_sim = forms.CharField(label=_("POS number"), required=False, max_length=20,
                              widget=forms.TextInput(attrs={"list": "zone-pos", "inputmode": "tel",
                                                            "autocomplete": "off", "placeholder": "6XX XX XX XX"}))
    shop = _shop_field()
    client_phone = forms.CharField(label=_("Client number"), required=False, max_length=30,
                                   widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off",
                                                                 "placeholder": "6XX XX XX XX"}))

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["shop"].queryset = Shop.objects.filter(active=True).order_by("name")


def _product_amount(form):
    d = _AgentForm.params(form)
    product, amount = d.pop("product"), d.pop("amount")
    d[PRODUCT_FIELD[product]] = amount
    return d


class AgentSupplyForm(_ClientForm):
    product = forms.ChoiceField(label=_("Product"), choices=PRODUCT_CHOICES, initial="UVE")
    amount = MoneyField(label=_("Amount (FCFA)"))
    paid = MoneyField(label=_("Paid now"), required=False,
                      help_text=_("The rest is owed to you."))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def params(self):
        return _product_amount(self)


class AgentTransferForm(_AgentForm):
    agent_sim = forms.CharField(label=_("Agent number"), max_length=20,
                                widget=forms.TextInput(attrs={"list": "agents", "inputmode": "tel",
                                                              "autocomplete": "off", "placeholder": "6XX XX XX XX"}))
    product = forms.ChoiceField(label=_("Product"), choices=PRODUCT_CHOICES, initial="UVE")
    amount = MoneyField(label=_("Amount (FCFA)"))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def params(self):
        d = _product_amount(self)
        d["client_kind"] = ClientKind.AGENT
        return d


class ClientPaymentForm(_ClientForm):
    amount = MoneyField(label=_("Amount received (FCFA)"))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)


class PaybackForm(_AgentForm):
    to = forms.ChoiceField(label=_("Pay to"), choices=[("DESK", _("A cashier of my zone")), ("SHOP", _("A shop"))],
                           widget=forms.RadioSelect, initial="DESK")
    desk = forms.ModelChoiceField(label=_("Cashier"), queryset=Zone.objects.none(), required=False,
                                  widget=NumberSelect(ag.desk_master_sim))
    shop = _shop_field()
    method = forms.ChoiceField(label=_("Paid in"), choices=[("UVE", "UVE"), ("CASH", _("Cash")),
                                                           ("MTN", _("MTN float"))], initial="UVE")
    amount = MoneyField(label=_("Amount (FCFA)"))
    reference = forms.CharField(label=_("Reference"), required=False, max_length=60)
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        qs = ag.desks_for(user.counterparty)
        self.fields["desk"].queryset = qs
        self.fields["shop"].queryset = Shop.objects.filter(active=True).order_by("name")
        if not self.is_bound and qs.count() == 1:
            self.initial["desk"] = qs.first()

    def clean(self):
        d = super().clean()
        if d.get("to") == "SHOP":
            if not d.get("shop"):
                self.add_error("shop", _("Choose the shop."))
            d["desk"] = None
        else:
            if not d.get("desk"):
                self.add_error("desk", _("Choose the cashier."))
            d["shop"] = None
        return d

    def params(self):
        d = _AgentForm.params(self)
        d.pop("to")
        method, amount = d.pop("method"), d.pop("amount")
        d[{"UVE": "uve", "CASH": "cash", "MTN": "mtn"}[method]] = amount
        return d


class CancelForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=255)
