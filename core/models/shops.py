"""Shops: points of service run by a shop manager, separate from the cashiers' desks.

A shop has its own cash drawer, UVE and ER wallets (on its company SIM), records Orange Money cash-in/out,
airtime, UVE→ER conversions at the shop rate, dealings with commercial agents, gadget/energy-kit sales,
counted services and SIM retrievals. Finance and the senior manager see everything; shops never see the
commercial agents' balances or the cashiers' dealings."""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .org import MONEY, User, Zone


class Shop(models.Model):
    name = models.CharField(_("name"), max_length=120)
    code = models.SlugField(_("code"), max_length=30, unique=True)
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.SET_NULL, related_name="shops",
                             verbose_name=_("zone (for reports)"))
    sim_number = models.CharField(_("company SIM number"), max_length=9, blank=True,
                                  help_text="The shop's Orange business SIM (holds its UVE and ER).")
    address = models.CharField(_("address"), max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.sim_number:
            from core.simnumbers import normalize_sim

            self.sim_number = normalize_sim(self.sim_number)
        super().save(*args, **kwargs)
        self.ensure_accounts()

    def ensure_accounts(self):
        """Every shop has a cash drawer, a UVE wallet and an ER wallet."""
        from .ledger import Account

        K = Account.Kind
        for kind, label in ((K.CASH_DRAWER, "Cash drawer"), (K.UVE, "UVE wallet"), (K.ER, "ER wallet"),
                            (K.SHOP_INVENTORY, "Stock at cost")):
            acct, created = Account.objects.get_or_create(
                kind=kind, shop=self, shop_agent=None, defaults=dict(name=f"{self.name} · {label}"))
            if not created and acct.active != self.active:
                Account.objects.filter(pk=acct.pk).update(active=self.active)

    def account(self, kind):
        from .ledger import Account

        if kind == Account.Kind.SHOP_INVENTORY:  # added later: shops created before get it on first use
            return Account.objects.get_or_create(kind=kind, shop=self, shop_agent=None,
                                                 defaults=dict(name=f"{self.name} · Stock at cost"))[0]
        return Account.objects.get(kind=kind, shop=self, shop_agent=None)

    def payable_to(self, agent):
        """The shop's position with a commercial agent: negative = the shop owes the agent (supplies on credit),
        positive = the agent owes the shop (e.g. destockage on credit)."""
        from .ledger import Account

        acct, _c = Account.objects.get_or_create(
            kind=Account.Kind.SHOP_PAYABLE, shop=self, shop_agent=agent,
            defaults=dict(name=f"{self.name} · position with {agent.name}"))
        return acct


class ShopService(models.Model):
    """Configurable services. Counted services only record how many were done; a SIM retrieval records the
    person and the SIM. (Cash-in/out, airtime, conversions and sales have their own screens.)"""

    class Kind(models.TextChoices):
        COUNT = "COUNT", _("Counted service (count only)")
        RETRIEVE = "RETRIEVE", _("SIM retrieval (person and SIM registered)")

    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("type"), max_length=10, choices=Kind.choices, default=Kind.COUNT)
    active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name


class ServiceCount(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="service_counts")
    service = models.ForeignKey(ShopService, on_delete=models.PROTECT, related_name="counts")
    business_date = models.DateField(_("date"), default=timezone.localdate)
    count = models.PositiveIntegerField(_("count"))
    note = models.CharField(_("note"), max_length=255, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-business_date", "-recorded_at"]
        constraints = [models.CheckConstraint(name="service_count_positive", condition=Q(count__gt=0))]


class SimRetrieval(models.Model):
    class IdType(models.TextChoices):
        CNI = "CNI", _("National ID card")
        PASSPORT = "PASSPORT", _("Passport")
        RECEIPT = "RECEIPT", _("ID receipt")
        RESIDENCE = "RESIDENCE", _("Residence permit")
        OTHER = "OTHER", _("Other")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="retrievals")
    service = models.ForeignKey(ShopService, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    business_date = models.DateField(_("date"), default=timezone.localdate)
    sim_number = models.CharField(_("SIM number retrieved"), max_length=9)
    iccid = models.CharField(_("new SIM serial (ICCID)"), max_length=30, blank=True)
    holder_name = models.CharField(_("name of the person"), max_length=150)
    id_type = models.CharField(_("ID type"), max_length=10, choices=IdType.choices, default=IdType.CNI)
    id_number = models.CharField(_("ID number"), max_length=60)
    phone = models.CharField(_("contact phone"), max_length=30, blank=True)
    note = models.CharField(_("note"), max_length=255, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-business_date", "-recorded_at"]


class ItemCategory(models.Model):
    """Category of items sold in the shops (phones, tablets, internet boxes, energy kits…)."""

    name = models.CharField(_("name"), max_length=80, unique=True)
    active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(_("order in lists"), default=100)

    class Meta:
        ordering = ["sort", "name"]
        verbose_name_plural = "item categories"

    def __str__(self):
        return self.name


class ShopItem(models.Model):
    name = models.CharField(_("name"), max_length=120)
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, related_name="items",
                                 verbose_name=_("category"))
    price = models.DecimalField(_("selling price (FCFA)"), **MONEY)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__sort", "category__name", "name"]

    def __str__(self):
        return self.name

    def stock_at(self, shop):
        return self.units.filter(shop=shop, status=StockUnit.Status.IN_STOCK).count()


class ItemSale(models.Model):
    """Client details of an item sale (the money is the SHOP_SALE transaction; the units sold point here)."""

    class IdType(models.TextChoices):
        CNI = "CNI", _("National ID card")
        PASSPORT = "PASSPORT", _("Passport")
        RESIDENCE = "RESIDENCE", _("Residence permit")
        RECEIPT = "RECEIPT", _("ID receipt")
        OTHER = "OTHER", _("Other")

    transaction = models.OneToOneField("Transaction", on_delete=models.PROTECT, related_name="item_sale")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="item_sales")
    item = models.ForeignKey(ShopItem, on_delete=models.PROTECT, related_name="sales")
    unit_price = models.DecimalField(_("unit price"), **MONEY)
    client_name = models.CharField(_("full name"), max_length=150)
    client_address = models.CharField(_("address"), max_length=255)
    client_country = models.CharField(_("country of origin"), max_length=80)
    id_type = models.CharField(_("identification"), max_length=10, choices=IdType.choices)
    id_number = models.CharField(_("identification number"), max_length=60)
    phone = models.CharField(_("telephone contact"), max_length=30)
    whatsapp = models.CharField(_("WhatsApp contact"), max_length=30, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class StockUnit(models.Model):
    """One physical unit in stock, identified by its IMEI or serial number."""

    class Status(models.TextChoices):
        IN_STOCK = "IN_STOCK", _("In stock")
        SOLD = "SOLD", _("Sold")
        REMOVED = "REMOVED", _("Removed from stock")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="units")
    item = models.ForeignKey(ShopItem, on_delete=models.PROTECT, related_name="units")
    serial = models.CharField(_("serial number"), max_length=60, unique=True, null=True, blank=True)
    imei = models.CharField(_("IMEI"), max_length=20, unique=True, null=True, blank=True)
    cost = models.DecimalField(_("unit cost"), max_digits=14, decimal_places=0, null=True, blank=True,
                               help_text="Purchase price of the unit (set when it was bought through a purchase).")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.IN_STOCK, db_index=True)
    sale = models.ForeignKey(ItemSale, null=True, blank=True, on_delete=models.PROTECT, related_name="units")
    received_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    received_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["item__name", "imei", "serial"]
        constraints = [models.CheckConstraint(name="unit_has_imei_or_serial",
                                              condition=Q(imei__isnull=False) | Q(serial__isnull=False))]

    def __str__(self):
        return f"{self.item.name} · {self.ident}"

    @property
    def ident(self):
        """How the unit is shown: IMEI and / or serial number."""
        parts = []
        if self.imei:
            parts.append(f"IMEI {self.imei}")
        if self.serial:
            parts.append(f"S/N {self.serial}" if self.imei else self.serial)
        return " · ".join(parts)


class StockMove(models.Model):
    """Stock of items per shop: + received, − sold or adjusted."""

    class Kind(models.TextChoices):
        RECEIVED = "RECEIVED", _("Received into stock")
        SALE = "SALE", _("Sale")
        ADJUST = "ADJUST", _("Adjustment")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="stock_moves")
    item = models.ForeignKey(ShopItem, on_delete=models.PROTECT, related_name="moves")
    unit = models.ForeignKey("StockUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="moves")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    quantity = models.IntegerField()
    transaction = models.ForeignKey("Transaction", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="stock_moves")
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at"]


class ShopConversionRate(models.Model):
    """UVE→ER uplift used by the shops (different from the cashiers' rate). The latest row applies."""

    rate = models.DecimalField(max_digits=9, decimal_places=6, help_text="0.04 = 4 %")
    effective_from = models.DateTimeField(default=timezone.now)
    set_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-effective_from"]

    @classmethod
    def current(cls):
        return cls.objects.order_by("-effective_from").first()

    @property
    def percent(self):
        return (self.rate * 100).quantize(Decimal("0.01"))


class ShopShift(models.Model):
    """A staff member's shift in a shop: opening and closing times (work attendance) and the till counts.

    The counts (cash counted, UVE and ER read on the shop SIM) are required when the person takes over the till
    (no other shift open in the shop) and when they hand it back (last shift open); the system balance at that
    moment is stored next to each count so differences can be explained later."""

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        CLOSED = "CLOSED", _("Closed")

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="shifts")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="shop_shifts")
    business_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    open_cash = models.DecimalField(**MONEY, null=True, blank=True)
    open_uve = models.DecimalField(**MONEY, null=True, blank=True)
    open_er = models.DecimalField(**MONEY, null=True, blank=True)
    expected_open_cash = models.DecimalField(**MONEY, null=True, blank=True)
    expected_open_uve = models.DecimalField(**MONEY, null=True, blank=True)
    expected_open_er = models.DecimalField(**MONEY, null=True, blank=True)
    close_cash = models.DecimalField(**MONEY, null=True, blank=True)
    close_uve = models.DecimalField(**MONEY, null=True, blank=True)
    close_er = models.DecimalField(**MONEY, null=True, blank=True)
    expected_close_cash = models.DecimalField(**MONEY, null=True, blank=True)
    expected_close_uve = models.DecimalField(**MONEY, null=True, blank=True)
    expected_close_er = models.DecimalField(**MONEY, null=True, blank=True)
    open_note = models.CharField(max_length=255, blank=True)
    close_note = models.CharField(max_length=255, blank=True)
    closed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                  help_text="Who closed it (the senior shop manager may close a forgotten shift).")

    class Meta:
        ordering = ["-opened_at"]
        constraints = [models.UniqueConstraint(fields=["user"], condition=Q(status="OPEN"),
                                               name="one_open_shift_per_user")]

    def __str__(self):
        return f"{self.user} · {self.shop} · {self.business_date:%d/%m/%Y}"

    @property
    def duration(self):
        end = self.closed_at or timezone.now()
        return end - self.opened_at

    @property
    def hours(self):
        return round(self.duration.total_seconds() / 3600, 2)

    def _diff(self, side, k):
        got, exp = getattr(self, f"{side}_{k}"), getattr(self, f"expected_{side}_{k}")
        return None if got is None or exp is None else got - exp

    @property
    def open_diffs(self):
        return {k: self._diff("open", k) for k in ("cash", "uve", "er")}

    @property
    def close_diffs(self):
        return {k: self._diff("close", k) for k in ("cash", "uve", "er")}

    @property
    def has_difference(self):
        return any(v for v in list(self.open_diffs.values()) + list(self.close_diffs.values()))


class ShopDayReview(models.Model):
    """The senior shop manager's sign-off of a shop's daily reconciliation."""

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="day_reviews")
    business_date = models.DateField()
    reviewed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-business_date"]
        constraints = [models.UniqueConstraint(fields=["shop", "business_date"], name="one_review_per_shop_day")]
