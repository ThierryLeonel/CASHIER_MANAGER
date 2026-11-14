"""Forms for the shops."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import CustomerField, MoneyField
from core.models import (
    ExpenseCategory, SHOP_DUTIES, SHOP_DUTY_CODES, Account, User, Zone, Counterparty, ItemCategory, ItemSale, Shop, ShopItem, ShopService, SimRetrieval, StockUnit,
)

K = Account.Kind


class ShopForm(forms.Form):
    """Base: idempotency key, and the shop to record for when the user is not a shop manager."""

    idem = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        if not user.is_shop_manager:
            self.fields["shop"] = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.filter(active=True))
            self.order_fields(["shop"] + [f for f in self.fields if f != "shop"])

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d


class CashInOutForm(ShopForm):
    """Cash-in (the client's number is needed: the Orange code is put on the dial pad) or cash-out. No commission
    is captured."""

    client_phone = forms.CharField(label=_("Client number"), required=False, max_length=30,
                                   widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}))
    amount = MoneyField(label=_("Amount (FCFA)"))
    account_name = forms.CharField(label=_("Account name (optional)"), max_length=120, required=False,
                                   help_text=_("The name on the client's Orange Money account."))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, op="cashin", **kwargs):
        super().__init__(user, *args, **kwargs)
        if op == "cashin":
            self.fields["client_phone"].required = True
        else:
            self.fields["client_phone"].label = _("Client number (optional)")

    def clean_client_phone(self):
        return _client_number(self.cleaned_data.get("client_phone"), self.fields["client_phone"].required)


def _client_number(v, required):
    from core.simnumbers import SimNumberError, normalize_sim

    v = (v or "").strip()
    if not v:
        if required:
            raise forms.ValidationError(_("Enter the client's number."))
        return ""
    try:
        return normalize_sim(v)
    except SimNumberError as e:
        raise forms.ValidationError(_(str(e)))


class AirtimeForm(ShopForm):
    """ER recharge: the client's number is needed, the Orange code is put on the dial pad."""

    kind = forms.ChoiceField(label=_("Airtime"), choices=[("NORMAL", _("Normal airtime")),
                                                         ("GOOD_DEAL", _("Good deal"))])
    client_phone = forms.CharField(label=_("Client number"), max_length=30,
                                   widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}))
    amount = MoneyField(label=_("Airtime sold (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def clean_client_phone(self):
        return _client_number(self.cleaned_data.get("client_phone"), True)


class ConvertForm(ShopForm):
    amount = MoneyField(label=_("UVE to convert (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)


class AgentDealForm(ShopForm):
    agent = CustomerField(label=_("Commercial agent"), kind=Counterparty.Kind.AGENT)
    amount = MoneyField(label=_("Amount (FCFA)"))
    pay = forms.ChoiceField(label=_("Paid with"), choices=[])
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    PAY_CHOICES = {
        "appro": [("CASH", _("Cash")), ("CREDIT", _("On credit (owed to the agent)"))],
        "er_supply": [("CASH", _("Cash")), ("UVE", _("UVE")), ("CREDIT", _("On credit (owed to the agent)"))],
        "pay_agent": [("CASH", _("Cash")), ("UVE", _("UVE"))],
        "agent_repay": [("CASH", _("Cash")), ("UVE", _("UVE"))],
        "destockage": [("CASH", _("Cash received now")), ("CREDIT", _("On credit (the agent owes the shop)"))],
    }

    def __init__(self, user, *args, op="appro", **kwargs):
        super().__init__(user, *args, **kwargs)
        choices = self.PAY_CHOICES[op]
        if choices:
            self.fields["pay"].choices = choices
        else:
            del self.fields["pay"]


class ShopAgentPaybackForm(ShopForm):
    """A commercial agent pays what he owes the company into the shop: the shop records it itself."""

    agent = CustomerField(label=_("Commercial agent"), kind=Counterparty.Kind.AGENT)
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def clean(self):
        cd = super().clean()
        if not cd.get("cash") and not cd.get("uve"):
            self.add_error("cash", _("Enter the amount received in cash and / or UVE."))
        return cd


class PosUveForm(ShopForm):
    """UVE sent to, or received from, a commercial agent or a POS."""

    pos = CustomerField(label=_("Agent or POS (business SIM)"))
    amount = MoneyField(label=_("UVE amount (FCFA)"))
    pay = forms.ChoiceField(label=_("Settled"), choices=[])
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    PAY_CHOICES = {
        "pos_uve_out": [("CASH", _("Paid in cash now")), ("CREDIT", _("On credit (owed to the company)"))],
        "pos_uve_in": [("CASH", _("The shop pays cash now")), ("CREDIT", _("Against what they owe (credit)"))],
    }

    def __init__(self, user, *args, op="pos_uve_out", **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["pay"].choices = self.PAY_CHOICES[op]


class ToCashierForm(ShopForm):
    """The shop sends cash and / or UVE to a cashier desk."""

    desk = forms.ModelChoiceField(label=_("To the cashier desk (subzone)"), queryset=Zone.objects.none())
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.forms import amount_boxes
        from core.services.operations import transfer_desks

        self.fields["desk"].queryset = transfer_desks()
        amount_boxes(self, ["cash", "uve"], _("Fill what you send: cash from the shop's drawer to the desk's drawer, "
                                              "UVE from the shop's UVE wallet to the desk's UVE wallet."),
                     str(_("Total transferred")))

    def clean(self):
        cd = super().clean()
        if not cd.get("cash") and not cd.get("uve"):
            self.add_error("cash", _("Enter the amount transferred in cash and / or UVE."))
        return cd

class ToShopForm(ShopForm):
    """A shop sends cash and / or UVE to another shop."""

    dest_shop = forms.ModelChoiceField(label=_("To the shop"), queryset=Shop.objects.none())
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.forms import amount_boxes

        qs = Shop.objects.filter(active=True)
        self.fields["dest_shop"].queryset = qs.exclude(pk=user.shop_id) if user.is_shop_manager else qs
        amount_boxes(self, ["cash", "uve"], _("Fill what you send: cash from this shop's drawer to the other shop's "
                                              "drawer, UVE from wallet to wallet."), str(_("Total transferred")))

    def clean(self):
        cd = super().clean()
        if not cd.get("cash") and not cd.get("uve"):
            self.add_error("cash", _("Enter the amount transferred in cash and / or UVE."))
        return cd


class ShopEntryForm(ShopForm):
    """Entry: cash or UVE the shop received — from a cashier desk, another shop or a company account."""

    source = forms.ChoiceField(label=_("Received from"), widget=forms.RadioSelect, initial="DESK", choices=[
        ("DESK", _("A cashier")), ("SHOP", _("Another shop")), ("COMPANY", _("A company account"))])
    desk = forms.ModelChoiceField(label=_("Cashier desk (subzone)"), queryset=Zone.objects.none(), required=False)
    from_shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.none(), required=False)
    company_account = forms.ModelChoiceField(label=_("Company account"), queryset=None, required=False)
    product = forms.ChoiceField(label=_("What you received"), initial="CASH",
                                choices=[("CASH", _("Cash")), ("UVE", "UVE"), ("ER", "ER")])
    amount = MoneyField(label=_("Amount (FCFA)"))
    reference = forms.CharField(label=_("Reference (transaction ID)"), required=False, max_length=60)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.models import Account
        from core.services.operations import transfer_desks

        self.fields["desk"].queryset = transfer_desks()
        self.fields["from_shop"].queryset = Shop.objects.filter(active=True).exclude(pk=user.shop_id or 0)
        self.fields["company_account"].queryset = Account.objects.filter(
            kind__in=Account.ASSET_KINDS, active=True, shop__isnull=True).order_by("kind", "name")

    def clean(self):
        cd = super().clean()
        f = {"DESK": "desk", "SHOP": "from_shop", "COMPANY": "company_account"}.get(cd.get("source"))
        if f and not cd.get(f):
            self.add_error(f, _("Required."))
        return cd


class UnitsWidget(forms.CheckboxSelectMultiple):
    """Units in stock, each tagged with its item and shop so the page shows only the matching ones."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        inst = getattr(value, "instance", None)
        if inst is not None:
            opt["attrs"]["data-item"] = inst.item_id
            opt["attrs"]["data-shop"] = inst.shop_id
        return opt


class ItemWidget(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        inst = getattr(value, "instance", None)
        if inst is not None:
            opt["attrs"]["data-category"] = inst.category_id
            opt["attrs"]["data-price"] = int(inst.price)
        return opt


class SaleForm(ShopForm):
    """One sale to one client: tick the units sold (any number of items, by IMEI / serial); the prices per item
    come from the basket (price_<item id>), the catalogue price otherwise."""

    units = forms.ModelMultipleChoiceField(label=_("Units sold — IMEI / serial"), queryset=StockUnit.objects.none(),
                                           widget=forms.MultipleHiddenInput,
                                           error_messages={"required": _("Choose the unit(s) sold by IMEI / serial "
                                                                         "number.")})
    client_name = forms.CharField(label=_("Client full name"), max_length=150)
    client_address = forms.CharField(label=_("Address"), max_length=255)
    client_country = forms.CharField(label=_("Country of origin"), max_length=80, initial="Cameroon")
    id_type = forms.ChoiceField(label=_("Identification"), choices=ItemSale.IdType.choices)
    id_number = forms.CharField(label=_("Identification number"), max_length=60)
    phone = forms.CharField(label=_("Telephone contact"), max_length=30)
    whatsapp = forms.CharField(label=_("WhatsApp contact"), required=False, max_length=30)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.services.shops import visible_shops

        self.stock = StockUnit.objects.filter(status=StockUnit.Status.IN_STOCK, shop__in=visible_shops(user),
                                              item__active=True).select_related("item__category", "shop")
        self.fields["units"].queryset = self.stock

    def catalogue(self):
        """[(category, [(item, [units])])] of what is in stock, for the picker."""
        cats = {}
        for u in self.stock.order_by("item__category__sort", "item__category__name", "item__name", "imei", "serial"):
            c = cats.setdefault(u.item.category_id, (u.item.category, {}))
            c[1].setdefault(u.item_id, (u.item, []))[1].append(u)
        return [(cat, list(items.values())) for cat, items in cats.values()]

    def chosen(self):
        if self.is_bound:
            return set(self.data.getlist("units"))
        return set()

    def clean(self):
        cd = super().clean()
        prices = {}
        for u in cd.get("units") or []:
            raw = (self.data.get(f"price_{u.item_id}") or "").strip()
            if raw:
                try:
                    from core.money import to_money

                    prices[u.item_id] = to_money(raw)
                except Exception:  # noqa: BLE001
                    self.add_error(None, _("Check the price of %(i)s.") % {"i": u.item.name})
        cd["prices"] = prices
        return cd


class ServiceCountForm(ShopForm):
    """All the shop's counted services on one form: type how many of each were done, record them together."""

    business_date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.services.days import douala_today

        self.services = list(ShopService.objects.filter(active=True, kind=ShopService.Kind.COUNT))
        for svc in self.services:
            self.fields[f"svc_{svc.pk}"] = forms.IntegerField(
                label=svc.name, min_value=0, max_value=100000, required=False,
                widget=forms.NumberInput(attrs={"min": 0, "inputmode": "numeric", "placeholder": "0",
                                                "class": "svc-count"}))
        self.initial.setdefault("business_date", douala_today())

    def service_rows(self):
        return [(svc, self[f"svc_{svc.pk}"]) for svc in self.services]

    def clean(self):
        cd = super().clean()
        if not any(cd.get(f"svc_{svc.pk}") for svc in self.services):
            raise forms.ValidationError(_("Type how many of at least one service were done."))
        return cd

    def params(self):
        cd = self.cleaned_data
        out = {"counts": {svc: cd.get(f"svc_{svc.pk}") or 0 for svc in self.services},
               "business_date": cd["business_date"], "note": cd["note"]}
        if "shop" in cd:
            out["shop"] = cd["shop"]
        return out


class RetrievalForm(ShopForm):
    sim_number = forms.CharField(label=_("SIM number retrieved"), max_length=20)
    iccid = forms.CharField(label=_("New SIM serial (ICCID)"), required=False, max_length=30)
    holder_name = forms.CharField(label=_("Name of the person"), max_length=150)
    id_type = forms.ChoiceField(label=_("ID type"), choices=SimRetrieval.IdType.choices)
    id_number = forms.CharField(label=_("ID number"), max_length=60)
    phone = forms.CharField(label=_("Contact phone"), required=False, max_length=30)
    note = forms.CharField(label=_("Note"), required=False, max_length=255)
    business_date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.services.days import douala_today

        self.initial.setdefault("business_date", douala_today())


class StockForm(forms.Form):
    """Receive units: say how many arrive, then type each unit's IMEI and / or serial number (unit_imei_N,
    unit_serial_N for N = 1..quantity)."""

    MAX_UNITS = 200
    shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.filter(active=True))
    category = forms.ModelChoiceField(label=_("Category"), queryset=ItemCategory.objects.filter(active=True))
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True), widget=ItemWidget)
    quantity = forms.IntegerField(label=_("Number of units received"), min_value=1, max_value=MAX_UNITS,
                                  widget=forms.NumberInput(attrs={"min": 1, "max": MAX_UNITS, "inputmode": "numeric"}),
                                  help_text=_("A line appears for each unit: type its IMEI and / or its serial "
                                              "number."))
    note = forms.CharField(label=_("Note (supplier, delivery…)"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if user.is_shop_manager:
            self.fields["shop"].queryset = Shop.objects.filter(pk=user.shop_id)
            self.fields["shop"].initial = user.shop_id

    def unit_rows(self):
        """[(n, imei, serial)] typed so far (to show the lines again after an error)."""
        if not self.is_bound:
            return []
        try:
            n = max(0, min(int(self.data.get("quantity") or 0), self.MAX_UNITS))
        except ValueError:
            n = 0
        return [(i, self.data.get(f"unit_imei_{i}", ""), self.data.get(f"unit_serial_{i}", ""))
                for i in range(1, n + 1)]

    def clean(self):
        cd = super().clean()
        cd["units"] = [(imei, ser) for _i, imei, ser in self.unit_rows()]
        return cd


class RemoveUnitForm(forms.Form):
    serial = forms.CharField(label=_("IMEI or serial number"), max_length=60)
    note = forms.CharField(label=_("Reason (returned, damaged, lost…)"), max_length=255)


class DeskToShopForm(forms.Form):
    """Cashier side: send cash and / or UVE to a shop."""

    idem = forms.UUIDField(widget=forms.HiddenInput)
    shop = forms.ModelChoiceField(label=_("Shop receiving the transfer"), queryset=Shop.objects.filter(active=True))
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        from core.forms import amount_boxes

        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        amount_boxes(self, ["cash", "uve"], _("Fill what you send: cash from your drawer to the shop's drawer, UVE "
                                              "from your UVE wallet to the shop's UVE wallet."),
                     str(_("Total transferred")))

    def clean(self):
        cd = super().clean()
        if not cd.get("cash") and not cd.get("uve"):
            self.add_error("cash", _("Enter the amount transferred in cash and / or UVE."))
        return cd

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d
class CategoryForm(forms.ModelForm):
    class Meta:
        model = ItemCategory
        fields = ["name", "sort", "active"]


class ServiceDefForm(forms.ModelForm):
    class Meta:
        model = ShopService
        fields = ["name", "kind", "active", "sort"]
        labels = {"sort": _("Order in lists")}


class ItemForm(forms.ModelForm):
    class Meta:
        model = ShopItem
        fields = ["name", "category", "price", "active"]


class ShopRateForm(forms.Form):
    percent = forms.DecimalField(label=_("Shop UVE→ER uplift (%)"), min_value=0, max_value=99, decimal_places=4)
    note = forms.CharField(label=_("Note"), required=False, max_length=255)


class ShopDefForm(forms.ModelForm):
    """Add / edit a shop (senior shop manager, super admin)."""

    class Meta:
        model = Shop
        fields = ["name", "code", "zone", "sim_number", "address", "active"]
        help_texts = {"code": _("Short unique code, e.g. shop-molyko (letters, digits, dashes)."),
                      "active": _("Untick to close the shop: it disappears from the lists, its history stays.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["zone"].required = False
        self.fields["zone"].queryset = Zone.objects.order_by("name")

    def clean_sim_number(self):
        from core.simnumbers import SimNumberError, normalize_sim

        v = self.cleaned_data.get("sim_number") or ""
        if not v:
            return ""
        try:
            return normalize_sim(v)
        except SimNumberError as exc:
            raise forms.ValidationError(str(exc))


class StaffForm(forms.Form):
    """A shop staff login: which shop, which duties (segmented shops)."""

    username = forms.CharField(label=_("Username"), max_length=150)
    first_name = forms.CharField(label=_("First name"), max_length=150)
    last_name = forms.CharField(label=_("Last name"), max_length=150, required=False)
    shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.filter(active=True))
    duties = forms.MultipleChoiceField(
        label=_("Duties"), choices=SHOP_DUTIES, widget=forms.CheckboxSelectMultiple, required=False,
        help_text=_("Tick what this person handles, e.g. only Orange Money for the cash-in / cash-out desk, and "
                    "the others for the airtime desk. Tick all (or none) for a manager who does everything."))

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        if instance is not None and not kwargs.get("data") and not (args and args[0]):
            kwargs["initial"] = {"username": instance.username, "first_name": instance.first_name,
                                 "last_name": instance.last_name, "shop": instance.shop_id,
                                 "duties": sorted(instance.shop_duty_set)}
        super().__init__(*args, **kwargs)
        if instance is not None:
            self.fields["username"].disabled = True
            self.fields["username"].required = False
            self.fields["shop"].queryset = Shop.objects.filter(active=True) | Shop.objects.filter(
                pk=instance.shop_id)
        else:
            self.initial.setdefault("duties", SHOP_DUTY_CODES)


class ShiftForm(forms.Form):
    """Till counts at the opening / closing of a shift."""

    cash = MoneyField(label=_("Cash counted in the drawer (FCFA)"), required=False)
    uve = MoneyField(label=_("UVE balance on the shop SIM (FCFA)"), required=False)
    er = MoneyField(label=_("ER balance on the shop SIM (FCFA)"), required=False)
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, *args, required=True, **kwargs):
        super().__init__(*args, **kwargs)
        for k in ("cash", "uve", "er"):
            self.fields[k].required = required
            self.fields[k].widget.attrs["autocomplete"] = "off"


class ForceCloseForm(forms.Form):
    note = forms.CharField(label=_("Reason"), max_length=255)


class DayReviewForm(forms.Form):
    note = forms.CharField(label=_("Note (explain any difference)"), required=False,
                           widget=forms.Textarea(attrs={"rows": 3}))


class ShopPositionOpeningForm(forms.Form):
    """Opening position of a shop with a commercial agent (what one owes the other at go-live)."""

    DIRECTIONS = [("AGENT_OWES", _("The agent owes the shop")), ("SHOP_OWES", _("The shop owes the agent"))]
    idem = forms.UUIDField(widget=forms.HiddenInput)
    shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.filter(active=True))
    agent = CustomerField(label=_("Commercial agent (business SIM)"), kind=Counterparty.Kind.AGENT)
    direction = forms.ChoiceField(label=_("Who owes whom"), choices=DIRECTIONS, widget=forms.RadioSelect,
                                  initial="AGENT_OWES")
    amount = MoneyField(label=_("Amount (FCFA)"))
    business_date = forms.DateField(label=_("Balance as of (go-live date)"),
                                    widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())

    def clean_amount(self):
        a = self.cleaned_data.get("amount")
        if not a or a <= 0:
            raise forms.ValidationError(_("Amount must be greater than zero."))
        return a


PAY_FROM = [("CASH", _("Cash drawer")), ("UVE", _("UVE wallet"))]


class ShopExpenseForm(ShopForm):
    category = forms.ModelChoiceField(label=_("Expense category"), queryset=ExpenseCategory.objects.filter(active=True))
    amount = MoneyField(label=_("Amount (FCFA)"))
    pay = forms.ChoiceField(label=_("Paid from"), choices=PAY_FROM, widget=forms.RadioSelect, initial="CASH")
    description = forms.CharField(label=_("Description"), max_length=255,
                                  help_text=_("What was paid, e.g. electricity bill October, transport of stock."))


class PurchaseForm(ShopForm):
    """Items bought for resale: how many units, their IMEI / serial numbers, the unit cost and how it is paid."""

    MAX_UNITS = StockForm.MAX_UNITS
    category = forms.ModelChoiceField(label=_("Category"), queryset=ItemCategory.objects.filter(active=True))
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True), widget=ItemWidget)
    quantity = forms.IntegerField(label=_("Number of units bought"), min_value=1, max_value=StockForm.MAX_UNITS,
                                  widget=forms.NumberInput(attrs={"min": 1, "inputmode": "numeric"}),
                                  help_text=_("A line appears for each unit: type its IMEI and / or its serial "
                                              "number."))
    unit_cost = MoneyField(label=_("Purchase price per unit (FCFA)"))
    pay = forms.ChoiceField(label=_("Paid from"), choices=PAY_FROM, widget=forms.RadioSelect, initial="CASH")
    supplier = forms.CharField(label=_("Supplier"), max_length=120, required=False)
    description = forms.CharField(label=_("Note (invoice number…)"), required=False, max_length=255)

    unit_rows = StockForm.unit_rows

    def clean(self):
        cd = super().clean()
        item, cat = cd.get("item"), cd.get("category")
        if item and cat and item.category_id != cat.pk:
            self.add_error("item", _("This item is not in the chosen category."))
        cd["units"] = [(imei, ser) for _i, imei, ser in self.unit_rows()]
        return cd

    def params(self):
        d = super().params()
        for k in ("category", "quantity"):
            d.pop(k, None)
        return d


def _exchange_fields(form, user, own_shop_id=None, own_desk_id=None):
    from core.services.operations import transfer_desks

    form.fields["partner"] = forms.ChoiceField(label=_("Exchange with"), widget=forms.RadioSelect, initial="DESK",
                                               choices=[("DESK", _("A cashier")), ("SHOP", _("A shop"))])
    form.fields["desk"] = forms.ModelChoiceField(label=_("Cashier desk (subzone)"), required=False,
                                                 queryset=transfer_desks().exclude(pk=own_desk_id or 0))
    form.fields["partner_shop"] = forms.ModelChoiceField(label=_("Shop"), required=False,
                                                         queryset=Shop.objects.filter(active=True)
                                                         .exclude(pk=own_shop_id or 0))
    form.fields["give"] = forms.ChoiceField(label=_("What you give"), widget=forms.RadioSelect, initial="UVE",
                                            choices=[("UVE", _("I give UVE and receive cash")),
                                                     ("CASH", _("I give cash and receive UVE"))])
    form.fields["amount"] = MoneyField(label=_("Amount (FCFA)"))
    form.fields["description"] = forms.CharField(label=_("Note"), required=False, max_length=255)


def _exchange_clean(form, cd):
    if cd.get("partner") == "DESK" and not cd.get("desk"):
        form.add_error("desk", _("Choose the other cashier's desk."))
    if cd.get("partner") == "SHOP" and not cd.get("partner_shop"):
        form.add_error("partner_shop", _("Choose the other shop."))
    return cd


class ShopExchangeForm(ShopForm):
    """Stock exchange of the shop with a cashier or another shop (cash ↔ UVE)."""

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        _exchange_fields(self, user, own_shop_id=user.shop_id)

    def clean(self):
        return _exchange_clean(self, super().clean())


class DeskExchangeForm(forms.Form):
    """Cashier side: stock exchange with a shop or another cashier (cash ↔ UVE)."""

    idem = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())
        _exchange_fields(self, user, own_desk_id=user.subzone_id)

    def clean(self):
        return _exchange_clean(self, super().clean())

    def params(self):
        d = dict(self.cleaned_data)
        d.pop("idem", None)
        return d
