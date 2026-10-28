import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import (
    Account, CashManager, CashManagerExchange, Correction, Counterparty, Discrepancy, ExpenseCategory, PaymentMethod,
    Product, TxType, User, Zone,
)
from core.money import MoneyError, to_money
from core.services import permissions as perm
from core.simnumbers import SimNumberError, normalize_sim

K = Account.Kind


class MoneyField(forms.CharField):
    def __init__(self, *args, allow_negative=False, **kwargs):
        self.allow_negative = allow_negative
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off",
                                                           "class": "money", "placeholder": "0"}))
        super().__init__(*args, **kwargs)

    def clean(self, value):
        value = super().clean(value)
        if value in (None, ""):
            return None
        try:
            d = to_money(value)
        except MoneyError as exc:
            raise forms.ValidationError(_(str(exc)))
        if d < 0 and not self.allow_negative:
            raise forms.ValidationError(_("Amount must be greater than zero."))
        return d


class CustomerField(forms.CharField):
    """Business SIM number -> Counterparty (the unique business identifier)."""

    def __init__(self, *args, kind=None, **kwargs):
        self.kind = kind
        kwargs.setdefault("widget", forms.TextInput(attrs={"list": "customer-list", "autocomplete": "off",
                                                           "inputmode": "tel", "placeholder": "6XX XX XX XX"}))
        kwargs.setdefault("help_text", _("Business SIM number"))
        super().__init__(*args, **kwargs)
        self.widget.attrs["data-customer-search"] = kind or ""

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        raw = value.split("–")[0].split(" - ")[0]
        try:
            sim = normalize_sim(raw)
        except SimNumberError as exc:
            raise forms.ValidationError(_(str(exc)))
        cp = Counterparty.objects.filter(business_sim=sim).first()
        if cp is None:
            raise forms.ValidationError(_("No agent or POS is registered with business SIM %(sim)s. Register it "
                                          "first.") % {"sim": sim})
        if self.kind and cp.kind != self.kind:
            raise forms.ValidationError(_("%(cp)s is not a %(kind)s.") % {"cp": cp,
                                                                         "kind": Counterparty.Kind(self.kind).label})
        return cp


class AccountSelect(forms.Select):
    """Adds data-kind to each option so the UI can filter by product/method."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        inst = getattr(value, "instance", None)
        if inst is not None:
            opt["attrs"]["data-kind"] = inst.kind
        return opt


class AccountChoiceField(forms.ModelChoiceField):
    widget = AccountSelect

    def label_from_instance(self, obj):
        return str(obj)


class BaseEntryForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())

    def accounts(self, kinds, receiving=False):
        return perm.usable_accounts(self.user, kinds, receiving=receiving).order_by("kind", "id")

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d


class AgentSupplyForm(BaseEntryForm):
    """Supply to a commercial agent (the normal case)."""

    product = forms.ChoiceField(label=_("Product"), choices=Product.choices)
    agent = CustomerField(label=_("Commercial agent (business SIM)"), kind=Counterparty.Kind.AGENT)
    amount = MoneyField(label=_("Amount (FCFA)"))
    source_account = AccountChoiceField(label=_("Company account sending"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["source_account"].queryset = self.accounts([K.UVE, K.ER, K.CASH_DRAWER])

    def params(self):
        d = super().params()
        d["customer"] = d.pop("agent")
        return d


class PosSupplyForm(BaseEntryForm):
    """Exceptional direct supply to a point of sale. The POS owes, unless an
    agent explicitly assumes the obligation at entry."""

    product = forms.ChoiceField(label=_("Product"), choices=Product.choices)
    pos = CustomerField(label=_("Point of sale (business SIM)"), kind=Counterparty.Kind.POS)
    assign_agent = CustomerField(label=_("Commercial agent who assumes this debt (optional)"), required=False,
                                 kind=Counterparty.Kind.AGENT,
                                 help_text=_("Leave empty: the POS owes. Fill only if an agent explicitly takes "
                                             "responsibility now."))
    amount = MoneyField(label=_("Amount (FCFA)"))
    source_account = AccountChoiceField(label=_("Company account sending"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["source_account"].queryset = self.accounts([K.UVE, K.ER, K.CASH_DRAWER])

    def params(self):
        d = super().params()
        pos, agent = d.pop("pos"), d.pop("assign_agent")
        d["customer"], d["recipient"] = (agent, pos) if agent else (pos, None)
        return d


class DestockageForm(BaseEntryForm):
    pos = CustomerField(label=_("POS that received the cash (business SIM)"), kind=Counterparty.Kind.POS)
    agent = CustomerField(label=_("Agent who received the UVE and owes (business SIM)"),
                          kind=Counterparty.Kind.AGENT)
    amount = MoneyField(label=_("Amount (FCFA)"))
    drawer = AccountChoiceField(label=_("Cash drawer"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["drawer"].queryset = self.accounts([K.CASH_DRAWER])


class CollectionForm(BaseEntryForm):
    payer = CustomerField(label=_("Paying customer (business SIM)"))
    method = forms.ChoiceField(label=_("Payment method"), choices=PaymentMethod.choices)
    amount = MoneyField(label=_("Amount received (FCFA)"))
    account = AccountChoiceField(label=_("Received into"), queryset=Account.objects.none())
    bank_reference = forms.CharField(label=_("Bank deposit reference"), required=False, max_length=60)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["account"].queryset = self.accounts([K.CASH_DRAWER, K.UVE, K.MTN, K.BANK], receiving=True)


class ExpenseForm(BaseEntryForm):
    category = forms.ModelChoiceField(label=_("Category"), queryset=ExpenseCategory.objects.filter(active=True))
    amount = MoneyField(label=_("Amount (FCFA)"))
    drawer = AccountChoiceField(label=_("Paid from drawer"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Description"), max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["drawer"].queryset = self.accounts([K.CASH_DRAWER])


class InternalTransferForm(BaseEntryForm):
    source = AccountChoiceField(label=_("From (your account)"), queryset=Account.objects.none())
    destination = AccountChoiceField(label=_("To (company account)"), queryset=Account.objects.none())
    amount = MoneyField(label=_("Amount (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["source"].queryset = self.accounts(Account.ASSET_KINDS)
        self.fields["destination"].queryset = Account.objects.filter(
            kind__in=Account.ASSET_KINDS, active=True).select_related("sim").order_by("kind", "id")


class ConversionForm(BaseEntryForm):
    uve_account = AccountChoiceField(label=_("UVE wallet to convert (ER arrives on the same SIM)"),
                                     queryset=Account.objects.none())
    amount = MoneyField(label=_("UVE amount (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["uve_account"].queryset = self.accounts([K.UVE])


class ExchangeForm(BaseEntryForm):
    cash_manager = forms.ModelChoiceField(label=_("Cash manager / bank"),
                                          queryset=CashManager.objects.filter(active=True))
    direction = forms.ChoiceField(label=_("Direction"), choices=CashManagerExchange.Direction.choices)
    amount = MoneyField(label=_("Agreed amount (FCFA, 1:1)"))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)


class ExchangeLegForm(BaseEntryForm):
    side = forms.ChoiceField(label=_("What happened"), choices=[(TxType.CM_GIVE, _("Company gave its side")),
                                                                (TxType.CM_RECEIVE, _("Company received its side"))])
    amount = MoneyField(label=_("Amount (FCFA)"))
    account = AccountChoiceField(label=_("Company account"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["account"].queryset = self.accounts([K.CASH_DRAWER, K.UVE])


class CounterpartyForm(forms.ModelForm):
    class Meta:
        model = Counterparty
        fields = ["kind", "business_sim", "name", "master_zone", "other_phone", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["master_zone"].queryset = Zone.objects.filter(parent__isnull=True)

    def clean_business_sim(self):
        try:
            sim = normalize_sim(self.cleaned_data["business_sim"])
        except SimNumberError as exc:
            raise forms.ValidationError(_(str(exc)))
        qs = Counterparty.objects.filter(business_sim=sim)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("Business SIM %(sim)s is already registered to %(cp)s.")
                                        % {"sim": sim, "cp": qs.first()})
        return sim


class CounterpartyEditForm(CounterpartyForm):
    """The business SIM and type are the identity: not editable after creation."""

    class Meta(CounterpartyForm.Meta):
        fields = ["name", "master_zone", "other_phone", "notes", "active"]


class ResponsibilityTransferForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)
    new_party = CustomerField(label=_("New responsible party (business SIM)"))
    reason = forms.CharField(label=_("Reason"), max_length=255, widget=forms.Textarea(attrs={"rows": 2}))


class CorrectionForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)
    kind = forms.ChoiceField(label=_("Correction"), choices=Correction.Kind.choices, widget=forms.RadioSelect)
    reason = forms.CharField(label=_("Reason (shown on the CORRECTED stamp)"), max_length=255)
    amount = MoneyField(label=_("Corrected amount (FCFA)"), required=False, allow_negative=True)
    customer = CustomerField(label=_("Corrected customer (business SIM)"), required=False)
    account = AccountChoiceField(label=_("Corrected company account"), required=False,
                                 queryset=Account.objects.filter(kind__in=Account.ASSET_KINDS, active=True))
    description = forms.CharField(label=_("Corrected note/description"), required=False, max_length=255)

    ACCOUNT_PARAM = {TxType.SUPPLY: "source_account", TxType.DESTOCKAGE: "drawer", TxType.COLLECTION: "account",
                     TxType.EXPENSE: "drawer", TxType.INTERNAL_TRANSFER: "source", TxType.CONVERSION: "uve_account",
                     TxType.CM_GIVE: "account", TxType.CM_RECEIVE: "account"}
    CUSTOMER_PARAM = {TxType.SUPPLY: "customer", TxType.DESTOCKAGE: "agent", TxType.COLLECTION: "payer"}

    def __init__(self, txn, *args, **kwargs):
        self.txn = txn
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        if txn.type not in self.CUSTOMER_PARAM:
            del self.fields["customer"]
        if txn.type not in self.ACCOUNT_PARAM:
            del self.fields["account"]
        if txn.type == TxType.RESPONSIBILITY_TRANSFER:
            for f in ("amount", "description"):
                del self.fields[f]
            self.fields["kind"].choices = [(Correction.Kind.VOID, Correction.Kind.VOID.label)]

    def new_values(self):
        cd = self.cleaned_data
        vals = {}
        if cd.get("amount") is not None:
            vals["amount"] = cd["amount"]
        if cd.get("customer"):
            vals[self.CUSTOMER_PARAM[self.txn.type]] = cd["customer"]
        if cd.get("account"):
            vals[self.ACCOUNT_PARAM[self.txn.type]] = cd["account"]
        if cd.get("description"):
            vals["description"] = cd["description"]
        return vals


class ResolveForm(forms.Form):
    resolution = forms.ChoiceField(label=_("Resolution"), choices=Discrepancy.Resolution.choices,
                                   widget=forms.RadioSelect)
    correction_ref = forms.CharField(label=_("Correction: reference of the corrected transaction"), required=False)
    notes = forms.CharField(label=_("Resolution notes"), widget=forms.Textarea(attrs={"rows": 3}))


class NoteForm(forms.Form):
    text = forms.CharField(label=_("Investigation note"), widget=forms.Textarea(attrs={"rows": 3}))


class OpeningBalanceForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)
    account = forms.ModelChoiceField(label=_("Account"), queryset=Account.objects.none())
    amount = MoneyField(label=_("Amount (FCFA)"), allow_negative=True,
                        help_text=_("Customers / cash managers: positive = owes the company, negative = the company "
                                    "owes them (supply due). Drawers, wallets and banks: positive only."))
    business_date = forms.DateField(label=_("Balance as of (go-live date)"), widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        self.fields["account"].queryset = Account.objects.filter(
            kind__in=Account.ASSET_KINDS + (K.CUSTOMER, K.CASH_MANAGER), active=True,
            opening_transactions__isnull=True,
        ).select_related("sim", "counterparty", "cash_manager").order_by("kind", "name")


class RateForm(forms.Form):
    percent = forms.DecimalField(label=_("UVE→ER uplift (%)"), min_value=0, max_value=99, decimal_places=4)
    note = forms.CharField(label=_("Note"), required=False, max_length=255)


class ClosingForm(forms.Form):
    notes = forms.CharField(label=_("Notes"), required=False, max_length=255)

    def __init__(self, accounts, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = accounts
        for acct in accounts:
            self.fields[f"acct_{acct.id}"] = MoneyField(label=str(acct), allow_negative=False)
        self.order_fields([f"acct_{a.id}" for a in accounts] + ["notes"])

    def actuals(self):
        return {a.id: self.cleaned_data[f"acct_{a.id}"] for a in self.accounts}


def user_choices():
    return User.objects.filter(role=User.Role.CASHIER).order_by("username")


class AdminEditForm(CorrectionForm):
    """Super-admin in-place edit. Same editable values as a correction, plus
    the business date; no reversal is posted."""

    business_date = forms.DateField(label=_("Business date"), required=False,
                                    widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, txn, *args, **kwargs):
        super().__init__(txn, *args, **kwargs)
        del self.fields["kind"]
        self.fields["reason"].label = _("Reason for the edit (kept in the audit log)")
        if txn.type == TxType.OPENING_BALANCE:
            self.fields.pop("account", None)

    def new_values(self):
        vals = super().new_values()
        if self.cleaned_data.get("business_date"):
            vals["business_date"] = self.cleaned_data["business_date"]
        return vals


class AdminDeleteForm(forms.Form):
    reason = forms.CharField(label=_("Reason for deleting (kept in the audit log)"), max_length=255)


class ClearDataForm(forms.Form):
    SCOPES = [("transactions", _("All transactions, closings and differences (keep agents, POS and configuration)")),
              ("everything", _("Everything above, plus agents, POS, agent logins and cash managers"))]
    scope = forms.ChoiceField(label=_("What to clear"), choices=SCOPES, widget=forms.RadioSelect)
    confirm_text = forms.CharField(label=_("Type DELETE to confirm"), max_length=20)
    password = forms.CharField(label=_("Your password"), widget=forms.PasswordInput)


class ImportForm(forms.Form):
    KINDS = [("customers", _("Agents and POS")), ("cash_managers", _("Cash managers")),
             ("opening", _("Opening balances")), ("transactions", _("Transactions (supplies, payments, expenses)"))]
    kind = forms.ChoiceField(label=_("What are you importing?"), choices=KINDS)
    file = forms.FileField(label=_("File (.xlsx or .csv)"))
