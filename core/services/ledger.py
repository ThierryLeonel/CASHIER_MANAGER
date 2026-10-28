"""Posting engine: the only code path that writes ledger transactions.

post() runs in one database transaction:
  1. re-checks the idempotency key,
  2. locks every affected account row (SELECT ... FOR UPDATE, id order,
     so concurrent postings on the same drawer/wallet serialise without
     deadlocks),
  3. verifies each transaction balances and that no company asset account
     goes negative (net effect of all specs together),
  4. inserts transactions + entries.
PostgreSQL triggers re-verify balance/non-negativity at commit.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import Account, Entry, Transaction
from core.money import fmt

from .errors import BusinessError, DuplicateSubmission, InsufficientFunds


@dataclass
class TxSpec:
    type: str
    lines: list  # [(Account, Decimal signed)]
    fields: dict = field(default_factory=dict)
    receipt: bool = False


@dataclass
class Ctx:
    """Who acts, under whose authority, and on which business day."""

    entered_by: object
    cashier: object = None  # cashier whose permissions/zone apply
    day: object = None  # CashierDay or None
    business_date: object = None
    exclude_txn_id: int | None = None  # original being replaced (corrections)
    rate_override: Decimal | None = None


def _nextval(seq):
    with connection.cursor() as cur:
        cur.execute("SELECT nextval(%s)", [seq])
        return cur.fetchone()[0]


def next_reference():
    return f"TX{_nextval('core_txn_ref_seq'):08d}"


def next_receipt_no():
    return f"RC{_nextval('core_receipt_seq'):08d}"


def next_exchange_reference():
    return f"EX{_nextval('core_exchange_ref_seq'):06d}"


def balance(account, upto_entry_id=None, before_date=None, upto_date=None):
    qs = Entry.objects.filter(account=account)
    if upto_entry_id is not None:
        qs = qs.filter(id__lte=upto_entry_id)
    if before_date is not None:
        qs = qs.filter(business_date__lt=before_date)
    if upto_date is not None:
        qs = qs.filter(business_date__lte=upto_date)
    return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")


def lock_accounts(ids):
    return {a.id: a for a in Account.objects.select_for_update().filter(id__in=sorted(set(ids))).order_by("id")}


def post(specs, ctx, idempotency_key=None):
    """Post one or more balanced transactions atomically. Returns list of
    Transaction in the order of specs."""
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            raise DuplicateSubmission(existing)
    try:
        with transaction.atomic():
            return _post_locked(specs, ctx, idempotency_key)
    except IntegrityError:
        if idempotency_key:
            existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                raise DuplicateSubmission(existing)
        raise


def _post_locked(specs, ctx, idempotency_key):
    ids = [acct.id for s in specs for acct, _amt in s.lines]
    locked = lock_accounts(ids)
    if idempotency_key and Transaction.objects.filter(idempotency_key=idempotency_key).exists():
        raise DuplicateSubmission(Transaction.objects.get(idempotency_key=idempotency_key))

    net = defaultdict(Decimal)
    for spec in specs:
        if len(spec.lines) < 2:
            raise BusinessError("A ledger transaction needs at least two entries")
        total = Decimal("0")
        for acct, amt in spec.lines:
            if not isinstance(amt, Decimal):
                raise BusinessError("Ledger amounts must be Decimal")
            if amt == 0:
                raise BusinessError(_("Amount must be greater than zero."))
            if not locked[acct.id].active and spec.type != "REVERSAL":
                raise BusinessError(_("Account %(acct)s is inactive.") % {"acct": locked[acct.id]})
            total += amt
            net[acct.id] += amt
        if total != 0:
            raise BusinessError(f"Unbalanced posting for {spec.type}: {total}")

    for acct_id, delta in net.items():
        acct = locked[acct_id]
        if acct.kind in Account.ASSET_KINDS and delta < 0:
            available = balance(acct)
            if available + delta < 0:
                raise InsufficientFunds(
                    _("Insufficient balance in %(acct)s: available %(avail)s FCFA, required %(req)s FCFA.")
                    % {"acct": acct, "avail": fmt(available), "req": fmt(-delta)}
                )

    now = timezone.now()
    results = []
    for i, spec in enumerate(specs):
        fields = dict(spec.fields)
        fields.setdefault("amount", max(abs(a) for _x, a in spec.lines))
        txn = Transaction.objects.create(
            reference=next_reference(),
            receipt_no=next_receipt_no() if spec.receipt else None,
            type=spec.type,
            business_date=ctx.business_date,
            posted_at=now,
            entered_by=ctx.entered_by,
            cashier=ctx.cashier if ctx.cashier and ctx.cashier.is_cashier else None,
            zone=ctx.day.zone if ctx.day else (ctx.cashier.subzone if ctx.cashier and ctx.cashier.is_cashier else None),
            cashier_day=ctx.day,
            idempotency_key=idempotency_key if i == 0 else None,
            **fields,
        )
        Entry.objects.bulk_create([
            Entry(transaction=txn, account_id=acct.id, amount=amt, business_date=ctx.business_date, posted_at=now)
            for acct, amt in spec.lines
        ])
        results.append(txn)
    return results
