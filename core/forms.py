import uuid

from django.db import transaction

from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import (
    Account, CashManager, Correction, Counterparty, Discrepancy, ExpenseCategory, PaymentMethod,
    MicroZone, Product, TxType, User, Zone,
)
from core.money import MoneyError, fmt, to_money
from core.services import permissions as perm
from core.simnumbers import SimNumberError, display_sim, normalize_sim

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
                                                           "placeholder": _("Microzone, name or SIM number")}))
        kwargs.setdefault("help_text", _("Type the microzone, the name or the business SIM number, then pick."))
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

    # Account fields filled automatically from another field when the subzone has exactly one matching account.
    AUTO_ACCOUNT = {
        "account": ("method", {"CASH": K.CASH_DRAWER, "UVE": K.UVE, "MTN": K.MTN, "BANK": K.BANK}),
        "source_account": ("product", {"UVE": K.UVE, "ER": K.ER, "CASH": K.CASH_DRAWER}),
    }

    def clean(self):
        cd = super().clean()
        for name, field in self.fields.items():
            if not isinstance(field, AccountChoiceField) or cd.get(name) or name not in self.errors:
                continue
            if (self.data.get(self.add_prefix(name)) or "").strip():  # a value was sent: keep its error
                continue
            qs = field.queryset
            if name in self.AUTO_ACCOUNT:
                driver, kinds = self.AUTO_ACCOUNT[name]
                if driver in self.fields:
                    kind = kinds.get(cd.get(driver))
                    if kind is None:
                        continue
                    qs = qs.filter(kind=kind)
            if qs.count() == 1:  # the only possible account: take it, no need to choose
                del self.errors[name]
                cd[name] = qs.first()
        return cd

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


SPLIT_PRODUCTS = (Product.UVE, Product.CASH, Product.ER)


def add_split_fields(form, allow_negative=False, help_text=None):
    for prod in SPLIT_PRODUCTS:
        f = MoneyField(label=Product(prod).label, required=False, allow_negative=allow_negative)
        f.widget.attrs.update({"data-split": prod, "placeholder": "0"})
        form.fields[f"split_{prod}"] = f
    if help_text:
        form.fields[f"split_{SPLIT_PRODUCTS[0]}"].help_text = help_text


def read_split(form, cd):
    """Pop the split fields from cleaned data; return {product: amount} (may be empty)."""
    out = {}
    for prod in SPLIT_PRODUCTS:
        v = cd.pop(f"split_{prod}", None)
        if v:
            out[prod] = v
    return out


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
        add_split_fields(self, help_text=_("Which debt this payment settles: UVE, Cash and/or ER. The parts add up "
                                           "to the amount received. Leave empty if not known."))
        self.order_fields(["payer", "split_UVE", "split_CASH", "split_ER", "amount", "method", "account",
                           "bank_reference", "description"])
        self.fields["amount"].help_text = _("Filled automatically from the parts above.")

    def clean(self):
        cd = super().clean()
        parts = {p: cd.get(f"split_{p}") for p in SPLIT_PRODUCTS if cd.get(f"split_{p}")}
        if parts and not cd.get("amount") and "amount" in self.errors:
            del self.errors["amount"]
            cd["amount"] = sum(parts.values())
        if parts and cd.get("amount") and sum(parts.values()) != cd["amount"]:
            self.add_error("amount", _("UVE + Cash + ER (%(sum)s FCFA) must equal the amount (%(amt)s FCFA).")
                           % {"sum": fmt(sum(parts.values())), "amt": fmt(cd["amount"])})
        return cd

    def params(self):
        d = super().params()
        d["split"] = read_split(self, d) or None
        return d


EXPENSE_SOURCE_KINDS = (K.CASH_DRAWER, K.UVE, K.MTN)


class ExpenseForm(BaseEntryForm):
    category = forms.ModelChoiceField(label=_("Category"), queryset=ExpenseCategory.objects.filter(active=True))
    amount = MoneyField(label=_("Amount (FCFA)"))
    drawer = AccountChoiceField(label=_("Paid from (cash, UVE or MTN float)"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Description"), max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["drawer"].queryset = self.accounts(list(EXPENSE_SOURCE_KINDS))


class InternalTransferForm(BaseEntryForm):
    source = AccountChoiceField(label=_("From (your account)"), queryset=Account.objects.none())
    destination = AccountChoiceField(label=_("To (company account)"), queryset=Account.objects.none())
    amount = MoneyField(label=_("Amount (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        kinds = [K.MTN, K.UVE, K.BANK]  # internal transfers: MTN float, UVE or bank only, same kind both sides
        self.fields["source"].queryset = self.accounts(kinds)
        self.fields["destination"].queryset = Account.objects.filter(
            kind__in=kinds, active=True).select_related("sim").order_by("kind", "id")
        self.fields["destination"].help_text = _("Same kind as the source: MTN float to MTN float, UVE to UVE, "
                                                 "bank to bank.")


class ConversionForm(BaseEntryForm):
    uve_account = AccountChoiceField(label=_("UVE wallet to convert (ER arrives on the same SIM)"),
                                     queryset=Account.objects.none())
    amount = MoneyField(label=_("UVE amount (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["uve_account"].queryset = self.accounts([K.UVE])


class ExchangeForm(BaseEntryForm):
    """New cash-manager exchange: what the company gives and what it is served now; the rest is served later."""

    cash_manager = forms.ModelChoiceField(label=_("Cash manager / bank"),
                                          queryset=CashManager.objects.filter(active=True))
    external_ref = forms.CharField(label=_("Ref number"), required=False, max_length=60)
    amount = MoneyField(label=_("Amount (FCFA)"), help_text=_("The amount agreed with the cash manager, 1:1."))
    give_account = AccountChoiceField(label=_("Company gives from"), queryset=Account.objects.none(),
                                      help_text=_("Cash drawer or UVE wallet."))
    amount_given = MoneyField(label=_("Amount given now (FCFA)"), required=False,
                              help_text=_("Leave empty if the company gave the full amount."))
    served_on = forms.DateField(label=_("Date served"), required=False,
                                widget=forms.DateInput(attrs={"type": "date"}))
    amount_served = MoneyField(label=_("Amount served (FCFA)"), required=False,
                               help_text=_("What the cash manager has served the company so far. The balance is "
                                           "served later from the exchange page."))
    receive_account = AccountChoiceField(label=_("Served into"), queryset=Account.objects.none(), required=False,
                                         help_text=_("Cash drawer or UVE wallet that received it."))
    note = forms.CharField(label=_("Comment"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.services.days import douala_today

        self.fields["give_account"].queryset = self.accounts([K.CASH_DRAWER, K.UVE])
        self.fields["receive_account"].queryset = self.accounts([K.CASH_DRAWER, K.UVE], receiving=True)
        if not self.is_bound:
            self.initial.setdefault("served_on", douala_today())

    def clean(self):
        cd = super().clean()
        amt = cd.get("amount")
        if amt and not cd.get("amount_given"):
            cd["amount_given"] = amt
        served = cd.get("amount_served") or 0
        if amt and served > amt:
            self.add_error("amount_served", _("The amount served cannot exceed the agreed amount."))
        if served and not cd.get("receive_account"):
            self.add_error("receive_account", _("Choose the account that received what was served."))
        return cd


class ExchangeLegForm(BaseEntryForm):
    side = forms.ChoiceField(label=_("What happened"), choices=[(TxType.CM_GIVE, _("Company gave (cash or UVE)")),
                                                                (TxType.CM_RECEIVE, _("Company received (cash or UVE)"))])
    amount = MoneyField(label=_("Amount (FCFA)"))
    account = AccountChoiceField(label=_("Company account"), queryset=Account.objects.none())
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["account"].queryset = self.accounts([K.CASH_DRAWER, K.UVE])


class CounterpartyForm(forms.ModelForm):
    """Agent or POS. A POS belongs to a microzone; an agent is put in charge of his microzone."""

    new_microzone = forms.CharField(label=_("Or create a new microzone"), required=False, max_length=100,
                                    help_text=_("Type its name if the microzone is not in the list yet."))
    new_microzone_subzone = forms.ModelChoiceField(label=_("Subzone of the new microzone"), required=False,
                                                   queryset=Zone.objects.none())

    field_order = ["kind", "business_sim", "name", "microzone", "new_microzone", "new_microzone_subzone",
                   "other_phone", "notes", "active"]

    class Meta:
        model = Counterparty
        fields = ["kind", "business_sim", "name", "microzone", "other_phone", "notes"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._old_mz = self.instance.microzone_id
        mz = MicroZone.objects.filter(active=True).select_related("subzone", "agent")
        subs = Zone.objects.filter(parent__isnull=False, active=True)
        if user is not None and user.role == User.Role.CASHIER and user.subzone_id:
            mz = mz.filter(subzone__parent=user.subzone.master)
            subs = subs.filter(pk=user.subzone_id)
        elif user is not None and getattr(user, "scope_zone", None) is not None:
            mz = mz.filter(subzone__parent=user.scope_zone)
            subs = subs.filter(parent=user.scope_zone)
        f = self.fields["microzone"]
        f.queryset = mz
        f.label = _("Microzone")
        f.help_text = _("POS: the microzone where it sells. Agent: the microzone he controls.")
        if not mz.exists():
            f.help_text = _("No microzone yet: type a name below to create the first one.")
        self.fields["new_microzone_subzone"].queryset = subs
        if subs.count() == 1:
            self.fields["new_microzone_subzone"].initial = subs.first().pk
        if not subs.exists():
            self.fields["new_microzone_subzone"].help_text = _(
                "No subzone exists yet. The super administrator creates zones in Configuration › Zones "
                "(a master zone first, then its subzones).")

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

    def clean(self):
        cd = super().clean()
        kind = cd.get("kind") or self.instance.kind
        mz, new = cd.get("microzone"), (cd.get("new_microzone") or "").strip()
        if mz and new:
            self.add_error("new_microzone", _("Choose an existing microzone or type a new one, not both."))
        if new:
            sub = cd.get("new_microzone_subzone")
            if sub is None:
                self.add_error("new_microzone_subzone", _("Choose the subzone of the new microzone."))
            elif MicroZone.objects.filter(subzone=sub, name__iexact=new).exists():
                self.add_error("new_microzone", _("This microzone already exists: choose it in the list."))
        if kind == Counterparty.Kind.POS and not mz and not new:
            self.add_error("microzone", _("A POS belongs to a microzone: choose it or create it."))
        if kind == Counterparty.Kind.AGENT and mz and mz.agent_id and mz.agent_id != self.instance.pk:
            self.add_error("microzone", _("%(mz)s is already controlled by %(agent)s.")
                           % {"mz": mz.name, "agent": mz.agent.name})
        return cd

    @transaction.atomic
    def save(self, commit=True):
        cd = self.cleaned_data
        new = (cd.get("new_microzone") or "").strip()
        if new:
            self.instance.microzone = MicroZone.objects.create(subzone=cd["new_microzone_subzone"], name=new)
        cp = super().save(commit=True)
        if cp.kind == Counterparty.Kind.AGENT and self._old_mz and self._old_mz != cp.microzone_id:
            MicroZone.objects.filter(pk=self._old_mz, agent=cp).update(agent=None)  # he left it
        if cp.kind == Counterparty.Kind.AGENT and cp.microzone_id and cp.microzone.agent_id is None:
            MicroZone.objects.filter(pk=cp.microzone_id).update(agent=cp)
        return cp


class CounterpartyEditForm(CounterpartyForm):
    """The business SIM and type are the identity: not editable after creation."""

    class Meta(CounterpartyForm.Meta):
        fields = ["name", "microzone", "other_phone", "notes", "active"]


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
    amount = MoneyField(label=_("Total (FCFA)"), allow_negative=True, required=False,
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
        ).select_related("sim", "counterparty__microzone", "cash_manager").order_by("kind", "name")
        self.fields["account"].label_from_instance = _opening_account_label
        add_split_fields(self, allow_negative=True,
                         help_text=_("Agents and POS: what they owe in UVE, Cash and ER. The total adds up by "
                                     "itself (a negative part = supply due to them)."))
        self.order_fields(["account", "split_UVE", "split_CASH", "split_ER", "amount", "business_date",
                           "description"])

    def clean(self):
        cd = super().clean()
        parts = {p: cd.get(f"split_{p}") for p in SPLIT_PRODUCTS if cd.get(f"split_{p}")}
        acct = cd.get("account")
        if parts and acct is not None and acct.kind != K.CUSTOMER:
            self.add_error("split_UVE", _("Only agents and POS openings are split by product. Use the total."))
        if parts and not cd.get("amount"):
            cd["amount"] = sum(parts.values())
        if not cd.get("amount"):
            self.add_error("amount", _("Enter the total, or the UVE, Cash and ER parts."))
        elif parts and sum(parts.values()) != cd["amount"]:
            self.add_error("amount", _("UVE + Cash + ER (%(sum)s FCFA) must equal the amount (%(amt)s FCFA).")
                           % {"sum": fmt(sum(parts.values())), "amt": fmt(cd["amount"])})
        cd["split"] = read_split(self, cd) or None
        return cd


def _opening_account_label(a):
    if a.kind == K.CUSTOMER and a.counterparty_id:
        cp = a.counterparty
        mz = f"{cp.microzone.name} — " if cp.microzone_id else ""
        return f"{mz}{cp.name} · {cp.get_kind_display()} {display_sim(cp.business_sim)}"
    return str(a)


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


class CompanyProfileForm(forms.ModelForm):
    logo_file = forms.ImageField(label=_("Logo"), required=False,
                                 help_text=_("PNG, JPEG or WEBP, up to 3 MB. A square or wide logo on a transparent "
                                             "or white background looks best."))
    remove_logo = forms.BooleanField(label=_("Remove the current logo"), required=False)

    class Meta:
        from core.models import CompanyProfile

        model = CompanyProfile
        fields = ["name", "tagline", "address", "city", "phone", "email", "tax_id", "trade_register",
                  "receipt_footer"]
        help_texts = {
            "name": _("Shown in the menu, on the login page, receipts and every report."),
            "tagline": _("Short line under the name in the menu. Leave empty for none."),
            "receipt_footer": _("Printed at the bottom of every receipt, e.g. thanks or terms."),
        }

    def clean_logo_file(self):
        from core.services.branding import LogoError, normalise_logo

        f = self.cleaned_data.get("logo_file")
        if not f:
            return None
        try:
            return normalise_logo(f)
        except LogoError as e:
            raise forms.ValidationError(_("The file is larger than 3 MB.") if str(e) == "too_big"
                                        else _("Upload a PNG, JPEG or WEBP image."))
