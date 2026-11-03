"""Paying back transfers between desks and shops.

A transfer received from another desk or shop (cashier -> cashier, cashier -> shop, shop -> cashier, shop -> shop)
is to be paid back to the sender. The receiver records the payment with "Pay the sender": a transfer in the other
direction (cash and / or UVE, possibly in several goes) that points to the transfer it pays (`settles`). What is not
paid yet is listed automatically when the receiving shop closes its shift, with the reason, in the closing note.
"""
from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext as _

from core.models import Transaction, TxType
from core.money import fmt

from . import permissions as perm
from .errors import BusinessError, PermissionDenied

SETTLE_TYPES = (TxType.INTERNAL_TRANSFER, TxType.DESK_TO_SHOP, TxType.SHOP_TO_DESK, TxType.SHOP_TO_SHOP)
Z = Decimal(0)


def head(t):
    """The part of a (possibly multi-product) transfer that carries the receipt: payments point to it."""
    return t.receipt_txn


def total(h):
    return sum((p.amount for p in h.payment_parts if not hasattr(p, "correction")), Z)


def paid(h):
    return Transaction.objects.filter(settles=h, correction__isnull=True).aggregate(s=Sum("amount"))["s"] or Z


def outstanding(h):
    if not h.to_settle or h.type not in SETTLE_TYPES or hasattr(h, "correction"):
        return Z
    return max(total(h) - paid(h), Z)


def _desk_of(account):
    if account.custodian_id and account.custodian.subzone_id:
        return account.custodian.subzone
    return account.zone


def party(account):
    if account is None:
        return "—"
    return account.shop.name if account.shop_id else str(_desk_of(account) or account)


def _heads():
    return Transaction.objects.filter(type__in=SETTLE_TYPES, to_settle=True, correction__isnull=True,
                                      receipt_no__isnull=False) \
        .select_related("source_account__shop", "source_account__zone", "source_account__custodian__subzone",
                        "dest_account__shop", "dest_account__zone", "dest_account__custodian__subzone")


def unpaid_for_shop(shop):
    """Transfers this shop received and has not fully paid back: [(transfer, outstanding)]."""
    out = []
    for h in _heads().filter(dest_account__shop=shop).order_by("posted_at"):
        o = outstanding(h)
        if o > 0:
            out.append((h, o))
    return out


def unpaid_for_desk(zone):
    accts = perm.zone_closing_accounts_q(zone)
    from core.models import Account

    ids = list(Account.objects.filter(accts).values_list("id", flat=True))
    out = []
    for h in _heads().filter(dest_account_id__in=ids, dest_account__shop__isnull=True).order_by("posted_at"):
        o = outstanding(h)
        if o > 0:
            out.append((h, o))
    return out


def can_pay(user, h):
    """The receiver pays: staff of the receiving shop (or the senior shop manager / admin), or a cashier of the
    receiving desk."""
    if outstanding(h) <= 0:
        return False
    dest = h.dest_account
    if dest.shop_id:
        return (user.is_shop_manager and user.shop_id == dest.shop_id) or user.is_shop_senior or user.is_superadmin
    desk = _desk_of(dest)
    return user.is_cashier and desk is not None and user.subzone_id == desk.pk


def summary_line(h, o):
    return _("%(ref)s from %(who)s on %(date)s: %(amt)s FCFA not paid back yet") % {
        "ref": h.reference, "who": party(h.source_account), "date": h.business_date.strftime("%d/%m/%Y"),
        "amt": fmt(o)}


def pay(user, h, *, cash=None, uve=None, mtn=None, description="", idempotency_key=None):
    """Record the payment of a received transfer back to its sender, in cash, UVE (and MTN float between cashiers)."""
    from core.money import MoneyError, to_money

    from . import operations as ops
    from . import shops as sh

    h = head(h)
    if not can_pay(user, h):
        raise PermissionDenied(_("Only the receiver of this transfer pays it back, while something is still owed."))
    amounts = {}
    for k, v in (("cash", cash), ("uve", uve), ("mtn", mtn)):
        try:
            amounts[k] = to_money(v) if v not in (None, "") else Z
        except MoneyError as exc:
            raise BusinessError(_(str(exc)), k) from exc
    total_paid = sum(amounts.values(), Z)
    o = outstanding(h)
    if total_paid <= 0:
        raise BusinessError(_("Enter the amount paid in cash and / or UVE."), "cash")
    if total_paid > o:
        raise BusinessError(_("Only %(amt)s FCFA is still owed on %(ref)s.") % {"amt": fmt(o), "ref": h.reference},
                            "cash")
    note = description or _("Payment of transfer %(ref)s") % {"ref": h.reference}
    src, dest = h.source_account, h.dest_account  # the payment goes from the receiver back to the sender
    common = dict(description=note, idempotency_key=idempotency_key, settles=h)
    cash, uve, mtn = amounts["cash"] or None, amounts["uve"] or None, amounts["mtn"] or None
    if dest.shop_id:
        if mtn:
            raise BusinessError(_("A shop pays back in cash and / or UVE."), "mtn")
        if src.shop_id:
            return sh.to_shop(user, shop=dest.shop, dest_shop=src.shop, cash=cash, uve=uve, **common)
        return sh.to_cashier(user, shop=dest.shop, desk=_desk_of(src), cash=cash, uve=uve, **common)
    if src.shop_id:
        if mtn:
            raise BusinessError(_("A shop is paid back in cash and / or UVE."), "mtn")
        return sh.desk_to_shop(user, shop=src.shop, cash=cash, uve=uve, **common)
    return ops.record_desk_transfer(user, desk=_desk_of(src), cash=cash, uve=uve, mtn=mtn, **common)[0]

