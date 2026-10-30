"""Forms for the shops."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import CustomerField, MoneyField
from core.models import (
    Account, Counterparty, ItemCategory, ItemSale, Shop, ShopItem, ShopService, SimRetrieval, StockUnit,
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
    amount = MoneyField(label=_("Amount (FCFA)"))
    commission = MoneyField(label=_("Commission earned (FCFA)"), required=False)
    client_phone = forms.CharField(label=_("Client phone (optional)"), required=False, max_length=30)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)


class AirtimeForm(ShopForm):
    kind = forms.ChoiceField(label=_("Airtime"), choices=[("NORMAL", _("Normal airtime")),
                                                         ("GOOD_DEAL", _("Good deal"))])
    amount = MoneyField(label=_("Airtime sold (FCFA)"))
    client_phone = forms.CharField(label=_("Client phone (optional)"), required=False, max_length=30)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)


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


class ToCashierForm(ShopForm):
    destination = forms.ModelChoiceField(label=_("Cashier UVE wallet receiving it"), queryset=Account.objects.none())
    amount = MoneyField(label=_("UVE sent (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.forms import AccountSelect

        f = self.fields["destination"]
        f.widget = AccountSelect()
        f.queryset = Account.objects.filter(kind=K.UVE, active=True, zone__isnull=False, shop__isnull=True) \
            .select_related("zone", "sim", "custodian__subzone").order_by("zone__name")
        f.label_from_instance = lambda a: f"{(a.custodian.subzone if a.custodian_id else a.zone).name} · {a}"


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
    category = forms.ModelChoiceField(label=_("Category"), queryset=ItemCategory.objects.filter(active=True))
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True), widget=ItemWidget)
    units = forms.ModelMultipleChoiceField(label=_("Unit(s) sold — IMEI / serial"), queryset=StockUnit.objects.none(),
                                           widget=UnitsWidget)
    unit_price = MoneyField(label=_("Unit price (FCFA)"), required=False,
                            help_text=_("Leave empty to use the item's price."))
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

        self.fields["item"].label_from_instance = lambda i: f"{i.name} — {i.price:,.0f} FCFA".replace(",", " ")
        self.fields["units"].queryset = StockUnit.objects.filter(
            status=StockUnit.Status.IN_STOCK, shop__in=visible_shops(user)).select_related("item")
        self.fields["units"].label_from_instance = lambda u: u.serial

    def clean(self):
        cd = super().clean()
        item, cat = cd.get("item"), cd.get("category")
        if item and cat and item.category_id != cat.pk:
            self.add_error("item", _("This item is not in the chosen category."))
        for u in cd.get("units") or []:
            if item and u.item_id != item.pk:
                self.add_error("units", _("%(s)s is not a %(item)s.") % {"s": u.serial, "item": item.name})
        return cd

    def params(self):
        d = super().params()
        d.pop("category", None)
        return d


class ServiceCountForm(ShopForm):
    service = forms.ModelChoiceField(label=_("Service"), queryset=ShopService.objects.none())
    count = forms.IntegerField(label=_("How many"), min_value=1)
    business_date = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.services.days import douala_today

        self.fields["service"].queryset = ShopService.objects.filter(active=True, kind=ShopService.Kind.COUNT)
        self.initial.setdefault("business_date", douala_today())


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
    shop = forms.ModelChoiceField(label=_("Shop"), queryset=Shop.objects.filter(active=True))
    category = forms.ModelChoiceField(label=_("Category"), queryset=ItemCategory.objects.filter(active=True))
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True), widget=ItemWidget)
    serials = forms.CharField(label=_("IMEI / serial numbers"), widget=forms.Textarea(attrs={"rows": 5}),
                              help_text=_("One unit per line (or separated by commas). Each number is registered once."))
    note = forms.CharField(label=_("Note (supplier, delivery…)"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if user.is_shop_manager:
            self.fields["shop"].queryset = Shop.objects.filter(pk=user.shop_id)
            self.fields["shop"].initial = user.shop_id


class RemoveUnitForm(forms.Form):
    serial = forms.CharField(label=_("IMEI / serial number"), max_length=60)
    note = forms.CharField(label=_("Reason (returned, damaged, lost…)"), max_length=255)


class DeskToShopForm(forms.Form):
    """Cashier side: send UVE to a shop."""

    idem = forms.UUIDField(widget=forms.HiddenInput)
    shop = forms.ModelChoiceField(label=_("Shop receiving the UVE"), queryset=Shop.objects.filter(active=True))
    amount = MoneyField(label=_("UVE sent (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())

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
