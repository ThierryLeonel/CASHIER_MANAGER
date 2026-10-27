"""Daily closing and discrepancy handling.

Rules (decision #2 default):
* Each cashier closes independently. Closing counts every active drawer and
  wallet the cashier is custodian of. Expected = ledger (book) balance at the
  moment of closing, including postings by other cashiers on shared wallets.
* Book balances carry forward. The ledger is never adjusted automatically.
* difference = actual - expected. Open differences from earlier closings of
  the same account are shown as "carried unresolved"; only the change
  (new_difference) creates a new Discrepancy record.
* Only managers/finance managers resolve discrepancies: by a linked
  reconciliation adjustment, by linking a correction, or as a counting error.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import (
    Account, CashierDay, Closing, ClosingLine, Correction, Discrepancy, DiscrepancyNote, Entry, TxType,
)
from core.money import MoneyError, to_money

from . import permissions as perm
from .days import current_day, douala_today
from .errors import BusinessError, PermissionDenied
from .ledger import Ctx, TxSpec, balance, lock_accounts, post


def closing_accounts(cashier):
    return list(Account.objects.filter(custodian=cashier, kind__in=Account.CLOSING_KINDS, active=True)
                .select_related("sim").order_by("kind", "id"))


def _opening_book(account):
    prev = ClosingLine.objects.filter(account=account).order_by("-closing__closed_at", "-id").first()
    if prev:
        return prev.expected
    # First closing: the launch opening balance is the opening figure.
    return Entry.objects.filter(account=account, transaction__type=TxType.OPENING_BALANCE).aggregate(
        s=Sum("amount"))["s"] or Decimal("0")


def close_day(user, *, actuals, notes=""):
    """actuals: {account_id: amount}. Returns the Closing."""
    perm.require_role(user, perm.ENTRY_ROLES, "closing")
    accounts = closing_accounts(user)
    parsed = {}
    for acct in accounts:
        raw = actuals.get(acct.id, actuals.get(str(acct.id)))
        if raw in (None, ""):
            raise BusinessError(_("Enter the actual balance for %(acct)s.") % {"acct": acct})
        try:
            val = to_money(raw)
        except MoneyError as exc:
            raise BusinessError(_(str(exc))) from exc
        if val < 0:
            raise BusinessError(_("Actual balances cannot be negative."))
        parsed[acct.id] = val
    with transaction.atomic():
        day = current_day(user)
        day = CashierDay.objects.select_for_update().get(pk=day.pk)
        if day.status != CashierDay.Status.OPEN:
            raise BusinessError(_("This business day is already closed."))
        lock_accounts([a.id for a in accounts])
        now = timezone.now()
        closing = Closing.objects.create(
            cashier_day=day, closed_by=user, closed_at=now, notes=notes,
            last_entry_id=Entry.objects.aggregate(m=Max("id"))["m"] or 0,
        )
        for acct in accounts:
            expected = balance(acct)
            opening = _opening_book(acct)
            actual = parsed[acct.id]
            diff = actual - expected
            carried = Discrepancy.objects.filter(account=acct, status=Discrepancy.Status.OPEN).aggregate(
                s=Sum("amount"))["s"] or Decimal("0")
            new_diff = diff - carried
            line = ClosingLine.objects.create(
                closing=closing, account=acct, opening_book=opening, movements=expected - opening,
                expected=expected, actual=actual, difference=diff, carried_unresolved=carried,
                new_difference=new_diff,
            )
            if new_diff != 0:
                Discrepancy.objects.create(closing_line=line, account=acct, amount=new_diff)
        CashierDay.objects.filter(pk=day.pk).update(status=CashierDay.Status.CLOSED, closed_at=now)
        current_day(user)  # opens the next business day immediately
        return closing


def add_discrepancy_note(user, discrepancy, text):
    if not (user.is_supervisor or (user.is_cashier and discrepancy.account.custodian_id == user.id)):
        raise PermissionDenied(_("You are not allowed to perform this action."))
    if not (text or "").strip():
        raise BusinessError(_("Write the investigation note."))
    return DiscrepancyNote.objects.create(discrepancy=discrepancy, author=user, text=text.strip())


def resolve_discrepancy(user, discrepancy, *, resolution, notes, correction=None):
    perm.require_role(user, perm.RESOLUTION_ROLES, "resolve discrepancy")
    if not (notes or "").strip():
        raise BusinessError(_("Explain the resolution."), "notes")
    with transaction.atomic():
        d = Discrepancy.objects.select_for_update().get(pk=discrepancy.pk)
        if d.status != Discrepancy.Status.OPEN:
            raise BusinessError(_("This discrepancy is already resolved."))
        adj = None
        if resolution == Discrepancy.Resolution.ADJUSTMENT:
            acct = d.account
            recon = Account.system(Account.Kind.RECON_ADJUSTMENT)
            amt = abs(d.amount)
            if d.amount < 0:  # shortage: book reduced to what was counted
                lines = [(recon, amt), (acct, -amt)]
            else:  # surplus: book increased
                lines = [(acct, amt), (recon, -amt)]
            custodian = acct.custodian
            if custodian and custodian.is_cashier:
                day = current_day(custodian)
                ctx = Ctx(entered_by=user, cashier=custodian, day=day, business_date=day.business_date)
            else:
                ctx = Ctx(entered_by=user, business_date=douala_today())
            adj = post([TxSpec(
                type=TxType.RECON_ADJUSTMENT, lines=lines,
                fields=dict(amount=amt, source_account=acct if d.amount < 0 else None,
                            dest_account=acct if d.amount > 0 else None,
                            original_business_date=d.closing_line.closing.cashier_day.business_date,
                            description=notes.strip()[:255]),
            )], ctx)[0]
        elif resolution == Discrepancy.Resolution.CORRECTION:
            if not isinstance(correction, Correction):
                raise BusinessError(_("Select the correction that explains this difference."), "correction")
        elif resolution != Discrepancy.Resolution.COUNT_ERROR:
            raise BusinessError(_("Choose a resolution."), "resolution")
        d.status = Discrepancy.Status.RESOLVED
        d.resolution = resolution
        d.resolution_notes = notes.strip()
        d.resolved_by = user
        d.resolved_at = timezone.now()
        d.adjustment_txn = adj
        d.correction = correction if resolution == Discrepancy.Resolution.CORRECTION else None
        d.save()
        DiscrepancyNote.objects.create(discrepancy=d, author=user,
                                       text=f"[{d.get_resolution_display()}] {notes.strip()}")
        return d
