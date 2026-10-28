"""Double-entry ledger.

Sign convention for Entry.amount: positive = debit, negative = credit.
  * Asset accounts (drawer, UVE, ER, MTN, bank): balance = sum(entries) >= 0.
  * Customer account: balance > 0 = customer owes the company;
                      balance < 0 = supply due to the customer (prepaid).
  * Cash-manager account: balance > 0 = cash manager owes the company;
                          balance < 0 = company owes the cash manager.
  * Expense accounts: positive = expense incurred.
  * Conversion uplift: negative = uplift gained (ER received above UVE spent).
  * Reconciliation adjustments: positive = shortage written off,
                                negative = surplus recognised.
  * Opening equity: counter-entry for launch balances.

Every transaction's entries sum to zero (enforced by a deferred
PostgreSQL constraint trigger). Entries and transactions are append-only
(UPDATE/DELETE blocked by triggers); corrections post reversals.
"""
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .org import MONEY, BankAccount, CashManager, CompanySim, Counterparty, ExpenseCategory, User, Zone


class Account(models.Model):
    class Kind(models.TextChoices):
        CASH_DRAWER = "CASH_DRAWER", _("Cash drawer")
        UVE = "UVE", _("UVE wallet")
        ER = "ER", _("ER wallet")
        MTN = "MTN", _("MTN float wallet")
        BANK = "BANK", _("Bank account")
        CUSTOMER = "CUSTOMER", _("Customer account")
        CASH_MANAGER = "CASH_MANAGER", _("Cash manager position")
        EXPENSE = "EXPENSE", _("Expense")
        CONVERSION_UPLIFT = "CONVERSION_UPLIFT", _("Conversion uplift")
        RECON_ADJUSTMENT = "RECON_ADJUSTMENT", _("Reconciliation adjustments")
        OPENING_EQUITY = "OPENING_EQUITY", _("Opening balance equity")

    ASSET_KINDS = (Kind.CASH_DRAWER, Kind.UVE, Kind.ER, Kind.MTN, Kind.BANK)
    CLOSING_KINDS = (Kind.CASH_DRAWER, Kind.UVE, Kind.ER, Kind.MTN)
    SYSTEM_KINDS = (Kind.CONVERSION_UPLIFT, Kind.RECON_ADJUSTMENT, Kind.OPENING_EQUITY)

    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(_("name"), max_length=150)
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="accounts")
    custodian = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="custody_accounts", verbose_name=_("cashier in charge"),
                                  help_text="Only for accounts held at master-zone level (e.g. the master SIM): the "
                                            "single cashier who uses it. Subzone accounts are shared by all the "
                                            "subzone's cashiers.")
    sim = models.ForeignKey(CompanySim, null=True, blank=True, on_delete=models.PROTECT, related_name="accounts")
    counterparty = models.OneToOneField(Counterparty, null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="ledger_account")
    cash_manager = models.OneToOneField(CashManager, null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="ledger_account")
    bank = models.OneToOneField(BankAccount, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="ledger_account")
    expense_category = models.OneToOneField(ExpenseCategory, null=True, blank=True, on_delete=models.PROTECT,
                                            related_name="ledger_account")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "sim"], condition=Q(sim__isnull=False), name="one_wallet_per_kind_per_sim"),
            models.UniqueConstraint(fields=["kind"], condition=Q(kind__in=["CONVERSION_UPLIFT", "RECON_ADJUSTMENT", "OPENING_EQUITY"]),
                                    name="singleton_system_accounts"),
            models.CheckConstraint(name="wallet_has_sim",
                                   condition=~Q(kind__in=["UVE", "ER", "MTN"]) | Q(sim__isnull=False)),
            models.CheckConstraint(name="drawer_has_zone",
                                   condition=~Q(kind="CASH_DRAWER") | Q(zone__isnull=False)),
            models.CheckConstraint(name="customer_has_counterparty",
                                   condition=~Q(kind="CUSTOMER") | Q(counterparty__isnull=False)),
            models.CheckConstraint(name="cm_has_cash_manager",
                                   condition=~Q(kind="CASH_MANAGER") | Q(cash_manager__isnull=False)),
            models.CheckConstraint(name="bank_has_bank", condition=~Q(kind="BANK") | Q(bank__isnull=False)),
            models.CheckConstraint(name="expense_has_category",
                                   condition=~Q(kind="EXPENSE") | Q(expense_category__isnull=False)),
        ]

    def __str__(self):
        if self.kind in self.SYSTEM_KINDS:
            return str(self.Kind(self.kind).label)
        if self.kind == self.Kind.EXPENSE and self.expense_category_id:
            return f"{self.Kind(self.kind).label} · {self.expense_category}"
        if self.sim_id:
            from core.simnumbers import display_sim

            return f"{self.kind} · {self.sim.label} ({display_sim(self.sim.msisdn)})"
        return self.name

    @property
    def is_asset(self):
        return self.kind in self.ASSET_KINDS

    @classmethod
    def system(cls, kind):
        acct, _ = cls.objects.get_or_create(kind=kind, defaults=dict(name=cls.Kind(kind).label))
        return acct


class AccountAccess(models.Model):
    """Explicit permission for a cashier to send from / receive into a
    company asset account they are not the custodian of (e.g. Fako 2 using
    the Fako master ER wallet)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="account_grants")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="grants")
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "account"], name="unique_account_grant")]

    def __str__(self):
        return f"{self.user} → {self.account}"


class CashierDay(models.Model):
    """A business day of a subzone (desk). Every cashier of the subzone works
    in the same open day; the subzone closes once."""

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        CLOSED = "CLOSED", _("Closed")

    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="days")
    cashier = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="days",
                                help_text="Cashier who opened the day.")
    business_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-business_date"]
        constraints = [
            models.UniqueConstraint(fields=["zone", "business_date"], name="one_day_per_zone_date"),
            models.UniqueConstraint(fields=["zone"], condition=Q(status="OPEN"), name="one_open_day_per_zone"),
        ]

    def __str__(self):
        return f"{self.zone} {self.business_date} ({self.status})"


class TxType(models.TextChoices):
    SUPPLY = "SUPPLY", _("Supply")
    DESTOCKAGE = "DESTOCKAGE", _("Destockage")
    COLLECTION = "COLLECTION", _("Collection (payment)")
    RESPONSIBILITY_TRANSFER = "RESP_TRANSFER", _("Responsibility transfer")
    CM_GIVE = "CM_GIVE", _("Cash manager: company gives")
    CM_RECEIVE = "CM_RECEIVE", _("Cash manager: company receives")
    CONVERSION = "CONVERSION", _("UVE → ER conversion")
    EXPENSE = "EXPENSE", _("Expense")
    INTERNAL_TRANSFER = "INT_TRANSFER", _("Internal transfer")
    OPENING_BALANCE = "OPENING", _("Opening balance")
    RECON_ADJUSTMENT = "RECON_ADJ", _("Reconciliation adjustment")
    REVERSAL = "REVERSAL", _("Correction reversal")


class Product(models.TextChoices):
    UVE = "UVE", "UVE"
    ER = "ER", "ER"
    CASH = "CASH", _("Cash")


class PaymentMethod(models.TextChoices):
    CASH = "CASH", _("Cash")
    UVE = "UVE", "UVE"
    MTN = "MTN", _("MTN float")
    BANK = "BANK", _("Bank deposit")


class Transaction(models.Model):
    reference = models.CharField(max_length=20, unique=True)
    receipt_no = models.CharField(max_length=20, unique=True, null=True, blank=True)
    type = models.CharField(max_length=14, choices=TxType.choices, db_index=True)
    business_date = models.DateField(db_index=True)
    posted_at = models.DateTimeField(default=timezone.now, db_index=True)
    entered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="entered_transactions")
    # The cashier whose desk the transaction belongs to (the recording cashier).
    cashier = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="desk_transactions",
                                db_index=True)
    # The subzone the entry belongs to (snapshot at posting time).
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions",
                             db_index=True)
    cashier_day = models.ForeignKey(CashierDay, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="transactions")
    amount = models.DecimalField(**MONEY)
    product = models.CharField(max_length=4, choices=Product.choices, blank=True)
    payment_method = models.CharField(max_length=4, choices=PaymentMethod.choices, blank=True)
    # SUPPLY/DESTOCKAGE/COLLECTION: responsible customer (owes) / payer.
    # RESP_TRANSFER: the new responsible party.
    counterparty = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="transactions")
    # SUPPLY: recipient POS when explicitly assigned to an agent;
    # DESTOCKAGE: the POS that received cash; RESP_TRANSFER: original party.
    counterparty_2 = models.ForeignKey(Counterparty, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="secondary_transactions")
    source_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    dest_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cash_manager = models.ForeignKey(CashManager, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="transactions")
    exchange = models.ForeignKey("CashManagerExchange", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="legs")
    expense_category = models.ForeignKey(ExpenseCategory, null=True, blank=True, on_delete=models.PROTECT)
    rate_used = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    amount_received = models.DecimalField(**MONEY, null=True, blank=True)  # ER received in conversions
    bank_reference = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=255, blank=True)
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversed_by")
    replaces = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="replaced_by")
    related_supply = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="responsibility_transfers")
    opening_for_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT,
                                            related_name="opening_transactions")
    original_business_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-posted_at", "-id"]
        indexes = [
            models.Index(fields=["type", "business_date"]),
            models.Index(fields=["counterparty", "business_date"]),
            models.Index(fields=["entered_by", "business_date"]),
        ]
        constraints = [
            models.CheckConstraint(name="tx_amount_positive", condition=Q(amount__gt=0)),
            models.UniqueConstraint(fields=["opening_for_account"],
                                    condition=Q(opening_for_account__isnull=False), name="one_opening_per_account"),
            models.CheckConstraint(name="reversal_links_original",
                                   condition=~Q(type="REVERSAL") | Q(reverses__isnull=False)),
            models.CheckConstraint(name="supply_has_product",
                                   condition=~Q(type="SUPPLY") | ~Q(product="")),
            models.CheckConstraint(name="collection_has_method",
                                   condition=~Q(type="COLLECTION") | ~Q(payment_method="")),
            models.CheckConstraint(name="bank_collection_has_reference",
                                   condition=~Q(type="COLLECTION", payment_method="BANK") | ~Q(bank_reference="")),
        ]

    def __str__(self):
        return f"{self.reference} {self.get_type_display()} {self.amount}"

    @property
    def is_corrected(self):
        return hasattr(self, "correction")

    @property
    def supply_channel(self):
        """'POS' for a direct supply to a point of sale, 'AGENT' otherwise."""
        src = self.reverses if self.type == TxType.REVERSAL and self.reverses_id else self
        if src.type != TxType.SUPPLY:
            return ""
        if src.counterparty_2_id or (src.counterparty_id and src.counterparty.kind == "POS"):
            return "POS"
        return "AGENT"

    @property
    def display_type(self):
        if self.type == TxType.SUPPLY:
            return _("Supply to POS") if self.supply_channel == "POS" else _("Supply to agent")
        return self.get_type_display()

    @property
    def has_receipt(self):
        return bool(self.receipt_no)


class Entry(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="entries")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="entries")
    amount = models.DecimalField(**MONEY)
    business_date = models.DateField()
    posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["account", "business_date"]),
            models.Index(fields=["account", "id"]),
        ]
        constraints = [models.CheckConstraint(name="entry_nonzero", condition=~Q(amount=0))]
        verbose_name_plural = "entries"


class CashManagerExchange(models.Model):
    """An exchange agreement with a cash manager/bank (1:1, no fees). The
    agreement itself moves no money; its legs are ledger transactions
    recorded only when each side actually happens."""

    class Direction(models.TextChoices):
        CASH_FOR_UVE = "CASH_FOR_UVE", _("Company gives cash, receives UVE")
        UVE_FOR_CASH = "UVE_FOR_CASH", _("Company gives UVE, receives cash")

    reference = models.CharField(max_length=20, unique=True)
    cash_manager = models.ForeignKey(CashManager, on_delete=models.PROTECT, related_name="exchanges")
    direction = models.CharField(max_length=14, choices=Direction.choices)
    amount = models.DecimalField(**MONEY)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    business_date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(name="exchange_amount_positive", condition=Q(amount__gt=0))]

    def __str__(self):
        return f"{self.reference} {self.cash_manager.name} {self.amount}"

    def give_kind(self):
        return Account.Kind.CASH_DRAWER if self.direction == self.Direction.CASH_FOR_UVE else Account.Kind.UVE

    def receive_kind(self):
        return Account.Kind.UVE if self.direction == self.Direction.CASH_FOR_UVE else Account.Kind.CASH_DRAWER

    def side_totals(self):
        """Net amounts delivered for each side, excluding corrected legs
        (their replacements count instead)."""
        from django.db.models import Sum

        legs = self.legs.filter(type__in=[TxType.CM_GIVE, TxType.CM_RECEIVE], correction__isnull=True)
        give = legs.filter(type=TxType.CM_GIVE).aggregate(s=Sum("amount"))["s"] or 0
        recv = legs.filter(type=TxType.CM_RECEIVE).aggregate(s=Sum("amount"))["s"] or 0
        return give, recv

    @property
    def status(self):
        give, recv = self.side_totals()
        if give == self.amount and recv == self.amount:
            return "COMPLETE"
        if give == 0 and recv == 0:
            return "AGREED"
        return "PARTIAL"
