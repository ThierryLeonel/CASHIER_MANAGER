"""Organisation: users/roles, zones, company SIMs, counterparties, cash
managers, banks, expense categories, conversion rates."""
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.simnumbers import SimNumberError, display_sim, normalize_sim

MONEY = dict(max_digits=18, decimal_places=0)


class Zone(models.Model):
    """Master zone (parent is NULL) or subzone (parent = master zone)."""

    code = models.SlugField(max_length=30, unique=True)
    name = models.CharField(_("name"), max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="subzones",
        verbose_name=_("master zone"),
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name

    @property
    def is_master(self):
        return self.parent_id is None

    @property
    def master(self):
        return self if self.parent_id is None else self.parent

    def clean(self):
        if self.parent_id and self.parent.parent_id:
            raise ValidationError(_("A subzone's parent must be a master zone."))


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPERADMIN = "SUPERADMIN", _("Super administrator")
        MANAGER = "MANAGER", _("Senior manager")
        JUNIOR = "JUNIOR", _("Junior manager")
        FINANCE = "FINANCE", _("Finance manager")
        CASHIER = "CASHIER", _("Cashier")
        AGENT = "AGENT", _("Commercial agent")

    role = models.CharField(_("role"), max_length=12, choices=Role.choices, default=Role.CASHIER)
    subzone = models.ForeignKey(
        Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="cashiers",
        verbose_name=_("zone"),
        help_text="Cashiers: the subzone they work in. Junior managers: the zone they supervise (a master zone covers "
                  "all its subzones).",
    )
    counterparty = models.OneToOneField(
        "Counterparty", null=True, blank=True, on_delete=models.PROTECT, related_name="login_user",
        help_text="Required for commercial-agent logins: the agent account they may view.",
    )
    staff_code = models.CharField(_("staff ID"), max_length=20, unique=True, null=True, blank=True,
                                  help_text="Shown on every transaction and receipt. Generated if left empty.")
    language = models.CharField(_("language"), max_length=2, blank=True, choices=[("en", "English"), ("fr", "Français")])

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="agent_user_has_counterparty",
                condition=~Q(role="AGENT") | Q(counterparty__isnull=False),
            ),
            models.CheckConstraint(
                name="cashier_has_subzone",
                condition=~Q(role="CASHIER") | Q(subzone__isnull=False),
            ),
            models.CheckConstraint(
                name="junior_has_zone",
                condition=~Q(role="JUNIOR") | Q(subzone__isnull=False),
            ),
        ]

    def __str__(self):
        return self.get_full_name() or self.username

    PREFIX = {"SUPERADMIN": "ADM", "MANAGER": "MGR", "JUNIOR": "JMG", "FINANCE": "FIN", "CASHIER": "CSH",
              "AGENT": "AGT"}

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.staff_code:
            code = f"{self.PREFIX.get(self.role, 'USR')}-{self.pk:04d}"
            User.objects.filter(pk=self.pk).update(staff_code=code)
            self.staff_code = code

    @property
    def label_with_id(self):
        return f"{self} [{self.staff_code}]" if self.staff_code else str(self)

    # role helpers
    @property
    def is_superadmin(self):
        return self.role == self.Role.SUPERADMIN

    @property
    def is_cashier(self):
        return self.role == self.Role.CASHIER

    @property
    def is_agent(self):
        return self.role == self.Role.AGENT

    @property
    def is_supervisor(self):
        """Senior, junior or finance manager."""
        return self.role in (self.Role.MANAGER, self.Role.JUNIOR, self.Role.FINANCE)

    @property
    def is_junior(self):
        return self.role == self.Role.JUNIOR

    @property
    def sees_all_zones(self):
        return self.role in (self.Role.MANAGER, self.Role.FINANCE, self.Role.SUPERADMIN)

    @property
    def scope_zone(self):
        """Zone a junior manager supervises (None = everything)."""
        return self.subzone if self.is_junior else None

    def zone_ids_in_scope(self):
        """Ids of the zones (master + subzones) a junior manager may see."""
        z = self.scope_zone
        if z is None:
            return None
        return [z.id] + list(z.subzones.values_list("id", flat=True))

    @property
    def master_zone(self):
        return self.subzone.master if self.subzone_id else None

    def clean(self):
        if self.role == self.Role.AGENT:
            if not self.counterparty_id:
                raise ValidationError(_("An agent login must be linked to an agent account."))
            if self.counterparty.kind != Counterparty.Kind.AGENT:
                raise ValidationError(_("Only commercial agents (not POS) may have a login."))
        if self.role == self.Role.JUNIOR and not self.subzone_id:
            raise ValidationError(_("A junior manager must be assigned the zone they supervise."))
        if self.role == self.Role.CASHIER:
            if not self.subzone_id:
                raise ValidationError(_("A cashier must belong to a subzone."))
            if self.subzone.is_master and self.subzone.subzones.exists():
                raise ValidationError(_("Assign the cashier to a subzone, not to a master zone with subzones."))


class CompanySim(models.Model):
    class Network(models.TextChoices):
        ORANGE = "ORANGE", _("Orange business SIM (UVE + ER)")
        MTN = "MTN", _("MTN float SIM")

    network = models.CharField(max_length=6, choices=Network.choices)
    msisdn = models.CharField(_("SIM number"), max_length=9, unique=True)
    label = models.CharField(_("label"), max_length=100)
    master_zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name="sims",
                                    verbose_name=_("master zone"))
    is_master = models.BooleanField(_("master SIM of the zone"), default=False)
    subzone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="subzone_sims",
                                verbose_name=_("subzone"),
                                help_text="Ordinary SIMs: the subzone that uses it (all its cashiers). "
                                          "Leave empty for the master SIM.")
    custodian = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="custody_sims",
        verbose_name=_("cashier in charge"),
        help_text="Required for the master SIM: the only cashier who may use it. It is counted in that "
                  "cashier's subzone closing.",
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["network", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["master_zone"], condition=Q(is_master=True), name="one_master_sim_per_zone",
            ),
            models.CheckConstraint(name="only_orange_can_be_master", condition=Q(is_master=False) | Q(network="ORANGE")),
        ]

    def __str__(self):
        return f"{self.label} ({display_sim(self.msisdn)})"

    def clean(self):
        try:
            self.msisdn = normalize_sim(self.msisdn)
        except SimNumberError as e:
            raise ValidationError({"msisdn": str(e)})
        if self.master_zone_id and not self.master_zone.is_master:
            raise ValidationError({"master_zone": _("Select a master zone.")})
        if self.custodian_id and self.custodian.role != User.Role.CASHIER:
            raise ValidationError({"custodian": _("The custodian must be a cashier.")})
        if self.subzone_id and self.master_zone_id and self.subzone.master.id != self.master_zone_id:
            raise ValidationError({"subzone": _("The subzone must belong to the selected master zone.")})
        if self.is_master and not self.custodian_id:
            raise ValidationError({"custodian": _("Choose the cashier in charge of the master SIM.")})
        if not self.is_master and not self.subzone_id and not self.custodian_id:
            raise ValidationError({"subzone": _("Choose the subzone that uses this SIM.")})

    @property
    def owner_zone(self):
        """Zone the SIM's wallets belong to: its subzone, else the master zone."""
        return self.subzone if self.subzone_id else self.master_zone

    def save(self, *args, **kwargs):
        self.msisdn = normalize_sim(self.msisdn)
        super().save(*args, **kwargs)
        from .ledger import Account

        kinds = [Account.Kind.UVE, Account.Kind.ER] if self.network == self.Network.ORANGE else [Account.Kind.MTN]
        for kind in kinds:
            zone = self.owner_zone
            custodian = None if self.subzone_id else self.custodian
            acct, created = Account.objects.get_or_create(
                kind=kind, sim=self, defaults=dict(name=f"{kind} {self.label}", zone=zone, custodian=custodian),
            )
            if not created:
                Account.objects.filter(pk=acct.pk).update(custodian=custodian, active=self.active, zone=zone)


class Counterparty(models.Model):
    """Commercial agent or exceptional POS, identified by business SIM."""

    class Kind(models.TextChoices):
        AGENT = "AGENT", _("Commercial agent")
        POS = "POS", _("Point of sale (POS)")

    kind = models.CharField(_("type"), max_length=5, choices=Kind.choices)
    business_sim = models.CharField(_("business SIM number"), max_length=9, unique=True)
    name = models.CharField(_("name"), max_length=120)
    master_zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT,
                                    verbose_name=_("usual master zone"),
                                    help_text="Informational only; does not create route liability.")
    other_phone = models.CharField(_("other phone"), max_length=30, blank=True)
    notes = models.CharField(_("notes"), max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["business_sim"]
        verbose_name_plural = "counterparties"

    def __str__(self):
        return f"{display_sim(self.business_sim)} – {self.name}"

    @property
    def sim_display(self):
        return display_sim(self.business_sim)

    def clean(self):
        try:
            self.business_sim = normalize_sim(self.business_sim)
        except SimNumberError as e:
            raise ValidationError({"business_sim": str(e)})

    def save(self, *args, **kwargs):
        self.business_sim = normalize_sim(self.business_sim)
        super().save(*args, **kwargs)
        from .ledger import Account

        Account.objects.get_or_create(
            kind=Account.Kind.CUSTOMER, counterparty=self,
            defaults=dict(name=f"{self.get_kind_display()} {self.business_sim}"),
        )

    @property
    def account(self):
        from .ledger import Account

        return Account.objects.get(kind=Account.Kind.CUSTOMER, counterparty=self)


class CashManager(models.Model):
    name = models.CharField(_("name"), max_length=120)
    phone = models.CharField(_("phone / SIM"), max_length=9, unique=True)
    notes = models.CharField(_("notes"), max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({display_sim(self.phone)})"

    def clean(self):
        try:
            self.phone = normalize_sim(self.phone)
        except SimNumberError as e:
            raise ValidationError({"phone": str(e)})

    def save(self, *args, **kwargs):
        self.phone = normalize_sim(self.phone)
        super().save(*args, **kwargs)
        from .ledger import Account

        Account.objects.get_or_create(kind=Account.Kind.CASH_MANAGER, cash_manager=self,
                                      defaults=dict(name=f"Cash manager {self.name}"))

    @property
    def account(self):
        from .ledger import Account

        return Account.objects.get(kind=Account.Kind.CASH_MANAGER, cash_manager=self)


class BankAccount(models.Model):
    bank_name = models.CharField(_("bank"), max_length=100)
    account_number = models.CharField(_("account number"), max_length=40, unique=True)
    label = models.CharField(_("label"), max_length=100)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.label} – {self.bank_name} {self.account_number}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .ledger import Account

        Account.objects.get_or_create(kind=Account.Kind.BANK, bank=self,
                                      defaults=dict(name=f"Bank {self.label}"))


class ExpenseCategory(models.Model):
    code = models.SlugField(max_length=30, unique=True,
                            help_text="Short code without spaces, e.g. bhb. Saving creates the expense account.")
    name_en = models.CharField("name (English)", max_length=80)
    name_fr = models.CharField("name (French)", max_length=80)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name_en"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        from django.utils.translation import get_language

        return self.name_fr if (get_language() or "en").startswith("fr") else self.name_en

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .ledger import Account

        Account.objects.get_or_create(kind=Account.Kind.EXPENSE, expense_category=self,
                                      defaults=dict(name=f"Expense {self.name_en}"))


class ConversionRate(models.Model):
    """UVE->ER uplift rate, e.g. 0.040000 = +4%. Append-only history."""

    uplift_rate = models.DecimalField(_("uplift rate"), max_digits=9, decimal_places=6)
    effective_from = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-effective_from", "-id"]
        constraints = [
            models.CheckConstraint(name="uplift_rate_range",
                                   condition=Q(uplift_rate__gte=Decimal("0")) & Q(uplift_rate__lt=Decimal("1"))),
        ]

    @classmethod
    def current(cls, at=None):
        at = at or timezone.now()
        return cls.objects.filter(effective_from__lte=at).order_by("-effective_from", "-id").first()

    @property
    def percent(self):
        return (self.uplift_rate * 100).normalize()
