"""Forms for the shops."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import CustomerField, MoneyField
from core.models import Account, Counterparty, Shop, ShopItem, ShopService, SimRetrieval

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
        "destockage": [],
    }

    def __init__(self, user, *args, op="appro", **kwargs):
        super().__init__(user, *args, **kwargs)
        choices = self.PAY_CHOICES[op]
        if choices:
            self.fields["pay"].choices = choices
        else:
            del self.fields["pay"]


class ToCashierForm(ShopForm):
    kind = forms.ChoiceField(label=_("What is sent"), choices=[("CASH", _("Cash")), ("UVE", _("UVE"))])
    destination = forms.ModelChoiceField(label=_("Cashier account receiving it"), queryset=Account.objects.none())
    amount = MoneyField(label=_("Amount (FCFA)"))
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        from core.forms import AccountSelect

        f = self.fields["destination"]
        f.widget = AccountSelect()
        f.queryset = Account.objects.filter(kind__in=[K.CASH_DRAWER, K.UVE], active=True, zone__isnull=False,
                                            shop__isnull=True).select_related("zone", "sim").order_by("zone__name")
        f.label_from_instance = lambda a: f"{a.zone.name} · {a}"


class SaleForm(ShopForm):
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True))
    quantity = forms.IntegerField(label=_("Quantity"), min_value=1, initial=1)
    unit_price = MoneyField(label=_("Unit price (FCFA)"), required=False,
                            help_text=_("Leave empty to use the item's price."))
    client_phone = forms.CharField(label=_("Client phone (optional)"), required=False, max_length=30)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["item"].label_from_instance = lambda i: f"{i.name} — {i.price:,.0f} FCFA".replace(",", " ")


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
    item = forms.ModelChoiceField(label=_("Item"), queryset=ShopItem.objects.filter(active=True))
    quantity = forms.IntegerField(label=_("Quantity"), help_text=_("Positive = received; negative = adjustment."))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if user.is_shop_manager:
            self.fields["shop"].queryset = Shop.objects.filter(pk=user.shop_id)
            self.fields["shop"].initial = user.shop_id


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
