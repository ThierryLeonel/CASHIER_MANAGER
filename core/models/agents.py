"""Commercial agent portal.

A commercial agent receives UVE, ER and cash from the office (company SUPPLY transactions on the agent's account) and
supplies them on to end-user POS, shops and other clients. Those supplies are the agent's own business: they are kept
in the agent's portal (`AgentClientEntry`), not in the company ledger. The agent pays the company back (UVE to the
master SIM, cash and MTN float to the cashier of the zone) with an `AgentPayback`, which becomes a company payment
(COLLECTION) only once that cashier confirms receiving it."""
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .org import MONEY, Counterparty, User, Zone

Z = Decimal(0)


class ClientKind(models.TextChoices):
    POS = "POS", _("End-user POS")
    SHOP = "SHOP", _("Shop")
    OTHER = "OTHER", _("Other client")
    AGENT = "AGENT", _("Commercial agent")


class AgentClientEntry(models.Model):
    """One supply by the agent to a client, or one payment by a client to the agent."""

    class Kind(models.TextChoices):
        SUPPLY = "SUPPLY", _("Supply to a client")
        PAYMENT = "PAYMENT", _("Payment from a client")

    agent = models.ForeignKey(Counterparty, on_delete=models.PROTECT, related_name="client_entries",
                              limit_choices_to={"kind": "AGENT"})
    kind = models.CharField(max_length=7, choices=Kind.choices)
    client_kind = models.CharField(_("client"), max_length=5, choices=ClientKind.choices)
    pos = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.PROTECT,
                            related_name="agent_supplies_received", limit_choices_to={"kind": "POS"})
    shop = models.ForeignKey("Shop", null=True, blank=True, on_delete=models.PROTECT, related_name="agent_entries")
    to_agent = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="agent_transfers_received", limit_choices_to={"kind": "AGENT"})
    client_name = models.CharField(_("client name"), max_length=120, blank=True)
    client_phone = models.CharField(_("client phone"), max_length=30, blank=True)
    uve = models.DecimalField(_("UVE"), default=Z, **MONEY)
    er = models.DecimalField(_("ER"), default=Z, **MONEY)
    cash = models.DecimalField(_("cash"), default=Z, **MONEY)
    paid = models.DecimalField(_("amount received"), default=Z, **MONEY)  # total of the three below
    paid_cash = models.DecimalField(_("received in cash"), default=Z, **MONEY)
    paid_uve = models.DecimalField(_("received in UVE"), default=Z, **MONEY)
    paid_mtn = models.DecimalField(_("received in float"), default=Z, **MONEY)
    note = models.CharField(_("note"), max_length=255, blank=True)
    business_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    idem = models.UUIDField(null=True, blank=True, unique=True, editable=False)  # one form submission, once

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(name="agent_entry_pos_has_pos", condition=~Q(client_kind="POS") | Q(pos__isnull=False)),
            models.CheckConstraint(name="agent_entry_shop_has_shop",
                                   condition=~Q(client_kind="SHOP") | Q(shop__isnull=False)),
            models.CheckConstraint(name="agent_entry_agent_has_agent",
                                   condition=~Q(client_kind="AGENT") | Q(to_agent__isnull=False)),
        ]

    def __str__(self):
        return f"{self.reference} {self.client_label}"

    @property
    def reference(self):
        prefix = "AT" if self.client_kind == ClientKind.AGENT else "AS" if self.kind == self.Kind.SUPPLY else "AC"
        return f"{prefix}-{self.pk:06d}"

    @property
    def product(self):
        return "UVE" if self.uve else "ER" if self.er else "CASH" if self.cash else ""

    @property
    def total(self):
        """What was supplied (zero for a payment)."""
        return self.uve + self.er + self.cash

    @property
    def balance_effect(self):
        """How much this entry adds to what the client owes the agent (a transfer to another agent: nothing)."""
        if self.client_kind == ClientKind.AGENT:
            return Z
        return self.total - self.paid

    @property
    def is_cancelled(self):
        return self.cancelled_at is not None

    @property
    def client_label(self):
        if self.client_kind == ClientKind.POS and self.pos_id:
            return self.pos.name
        if self.client_kind == ClientKind.SHOP and self.shop_id:
            return self.shop.name
        if self.client_kind == ClientKind.AGENT and self.to_agent_id:
            return self.to_agent.name
        return self.client_name or self.client_phone or "—"

    @property
    def client_key(self):
        if self.client_kind == ClientKind.POS:
            return ("POS", self.pos_id)
        if self.client_kind == ClientKind.SHOP:
            return ("SHOP", self.shop_id)
        if self.client_kind == ClientKind.AGENT:
            return ("AGENT", self.to_agent_id)
        phone = "".join(ch for ch in (self.client_phone or "") if ch.isdigit())[-9:]
        return ("OTHER", phone or (self.client_name or "").strip().lower())


class AgentExchange(models.Model):
    """The agent exchanges MTN float 1:1: he sends float and receives UVE or cash, or receives float and sends UVE."""

    class Direction(models.TextChoices):
        SEND_FLOAT_UVE = "SEND_FLOAT_UVE", _("I send float, I receive UVE")
        SEND_FLOAT_CASH = "SEND_FLOAT_CASH", _("I send float, I receive cash")
        RECEIVE_FLOAT_UVE = "RECEIVE_FLOAT_UVE", _("I receive float, I send UVE")

    # (product given, product received)
    MOVES = {"SEND_FLOAT_UVE": ("MTN", "UVE"), "SEND_FLOAT_CASH": ("MTN", "CASH"), "RECEIVE_FLOAT_UVE": ("UVE", "MTN")}

    agent = models.ForeignKey(Counterparty, on_delete=models.PROTECT, related_name="float_exchanges",
                              limit_choices_to={"kind": "AGENT"})
    direction = models.CharField(_("exchange"), max_length=17, choices=Direction.choices)
    amount = models.DecimalField(_("amount"), **MONEY)
    other_phone = models.CharField(_("other party's number"), max_length=30, blank=True)
    note = models.CharField(_("note"), max_length=255, blank=True)
    business_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    idem = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference

    @property
    def reference(self):
        return f"AX-{self.pk:06d}"

    @property
    def gives(self):
        return self.MOVES[self.direction][0]

    @property
    def receives(self):
        return self.MOVES[self.direction][1]

    @property
    def is_cancelled(self):
        return self.cancelled_at is not None


class AgentPayback(models.Model):
    """The agent pays the company back: UVE to the master SIM, cash and MTN float to the cashier of the zone. It is a
    company payment only once that cashier confirms receiving it."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Waiting for the shop")
        RECORDED = "RECORDED", _("Recorded by the agent (the cashier records the payment)")
        CONFIRMED = "CONFIRMED", _("Confirmed")
        REJECTED = "REJECTED", _("Rejected")

    agent = models.ForeignKey(Counterparty, on_delete=models.PROTECT, related_name="paybacks",
                              limit_choices_to={"kind": "AGENT"})
    desk = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="agent_paybacks",
                             verbose_name=_("cashier desk"))
    shop = models.ForeignKey("Shop", null=True, blank=True, on_delete=models.PROTECT, related_name="agent_paybacks",
                             verbose_name=_("shop"))
    cash = models.DecimalField(_("cash"), default=Z, **MONEY)
    uve = models.DecimalField(_("UVE"), default=Z, **MONEY)
    mtn = models.DecimalField(_("MTN float"), default=Z, **MONEY)
    reference = models.CharField(_("transfer reference"), max_length=60, blank=True)
    note = models.CharField(_("note"), max_length=255, blank=True)
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=255, blank=True)
    transaction = models.ForeignKey("Transaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    idem = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(name="agent_payback_one_receiver",
                                              condition=Q(desk__isnull=False, shop__isnull=True)
                                              | Q(desk__isnull=True, shop__isnull=False))]

    def __str__(self):
        return f"{self.code} {self.agent}"

    @property
    def receiver(self):
        return self.shop if self.shop_id else self.desk

    @property
    def method(self):
        return "UVE" if self.uve else "MTN" if self.mtn else "CASH"

    @property
    def code(self):
        return f"AP-{self.pk:06d}"

    @property
    def total(self):
        return self.cash + self.uve + self.mtn
