"""Shop shifts (work attendance + till counts) and the shop's daily reconciliation.

Shift rules:
  * a shop staff member opens a shift before recording anything and closes it at the end (attendance);
  * one open shift per person; shifts of a segmented shop run side by side;
  * whoever takes over the till (no other shift open in the shop) counts the cash and reads UVE and ER on the
    shop SIM; whoever hands it back (last shift open) counts again. Others may add counts but need not;
  * each count is stored with the system balance at that moment; the difference is shown, never posted — it is
    for the senior shop manager to investigate and sign off in the daily reconciliation;
  * the senior shop manager (or the super admin) may close a shift someone forgot, with a reason.

Daily reconciliation (per shop, per business date), for cash, UVE and ER:
  system opening (end of the previous day) + money in − money out = system closing,
  compared with the first opening count and the last closing count of the day.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import Account, Entry, ServiceCount, ShopDayReview, ShopShift, Transaction, TxType
from core.money import MoneyError, to_money

from .days import douala_today
from .errors import BusinessError, PermissionDenied
from .ledger import balance
from .operations import log_event

K = Account.Kind
KINDS = (("cash", K.CASH_DRAWER), ("uve", K.UVE), ("er", K.ER))


def current_shift(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return ShopShift.objects.filter(user=user, status=ShopShift.Status.OPEN).select_related("shop").first()


def _counts(values, required):
    out = {}
    for k, _kind in KINDS:
        v = values.get(k)
        if v in (None, ""):
            if required:
                raise BusinessError(_("Count the cash and read the UVE and ER balances on the shop SIM."), k)
            out[k] = None
            continue
        try:
            a = to_money(v)
        except MoneyError as exc:
            raise BusinessError(_(str(exc)), k) from exc
        if a < 0:
            raise BusinessError(_("A balance cannot be negative."), k)
        out[k] = a
    return out


def _system(shop):
    return {k: balance(shop.account(kind)) for k, kind in KINDS}


def others_open(shop, exclude=None):
    qs = ShopShift.objects.filter(shop=shop, status=ShopShift.Status.OPEN)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs


def counts_required_to_open(shop):
    return not others_open(shop).exists()


def open_shift(user, *, cash=None, uve=None, er=None, note=""):
    if not user.is_shop_manager:
        raise PermissionDenied(_("Only shop staff open shifts."))
    shop = user.shop
    if shop is None or not shop.active:
        raise PermissionDenied(_("Your account is not linked to an open shop."))
    if current_shift(user):
        raise BusinessError(_("Your shift is already open."))
    with transaction.atomic():
        counts = _counts({"cash": cash, "uve": uve, "er": er}, required=counts_required_to_open(shop))
        system = _system(shop)
        s = ShopShift(shop=shop, user=user, business_date=douala_today(), open_note=note)
        for k, _kind in KINDS:
            setattr(s, f"open_{k}", counts[k])
            setattr(s, f"expected_open_{k}", system[k] if counts[k] is not None else None)
        try:
            s.save()
        except IntegrityError:
            raise BusinessError(_("Your shift is already open."))
    log_event(user, "shop_shift_opened", shift_id=s.pk, shop=shop.name)
    return s


def close_shift(user, *, shift=None, cash=None, uve=None, er=None, note=""):
    """Close one's own shift, or (senior shop manager / super admin) a shift someone forgot to close."""
    own = shift is None or shift.user_id == user.pk
    if own:
        shift = current_shift(user)
        if shift is None:
            raise BusinessError(_("You have no open shift."))
    else:
        if not (user.is_shop_senior or user.is_superadmin):
            raise PermissionDenied(_("Only the senior shop manager closes someone else's shift."))
        if shift.status != ShopShift.Status.OPEN:
            raise BusinessError(_("This shift is already closed."))
        if not (note or "").strip():
            raise BusinessError(_("Give the reason for closing this shift."), "note")
    last = not others_open(shift.shop, exclude=shift).exists()
    with transaction.atomic():
        counts = _counts({"cash": cash, "uve": uve, "er": er}, required=own and last)
        system = _system(shift.shop)
        for k, _kind in KINDS:
            setattr(shift, f"close_{k}", counts[k])
            setattr(shift, f"expected_close_{k}", system[k] if counts[k] is not None else None)
        shift.status, shift.closed_at, shift.closed_by, shift.close_note = (
            ShopShift.Status.CLOSED, timezone.now(), user, note)
        shift.save()
    log_event(user, "shop_shift_closed", shift_id=shift.pk, shop=shift.shop.name, own=own)
    return shift


# ---------------------------------------------------------------- daily reconciliation
def day_reconciliation(shop, day, viewer=None):
    accounts = {k: shop.account(kind) for k, kind in KINDS}
    rows = []
    shifts = list(ShopShift.objects.filter(shop=shop, business_date=day).select_related("user", "closed_by")
                  .order_by("opened_at"))
    first_open = next((s for s in shifts if s.open_cash is not None or s.open_uve is not None
                       or s.open_er is not None), None)
    closed = [s for s in shifts if s.status == ShopShift.Status.CLOSED and (
        s.close_cash is not None or s.close_uve is not None or s.close_er is not None)]
    last_close = max(closed, key=lambda s: s.closed_at) if closed else None
    for k, _kind in KINDS:
        a = accounts[k]
        day_entries = Entry.objects.filter(account=a, business_date=day)
        money_in = day_entries.filter(amount__gt=0).aggregate(s=Sum("amount"))["s"] or Decimal(0)
        money_out = -(day_entries.filter(amount__lt=0).aggregate(s=Sum("amount"))["s"] or Decimal(0))
        sys_open = balance(a, before_date=day)
        sys_close = balance(a, upto_date=day)
        c_open = getattr(first_open, f"open_{k}") if first_open else None
        e_open = getattr(first_open, f"expected_open_{k}") if first_open else None
        c_close = getattr(last_close, f"close_{k}") if last_close else None
        e_close = getattr(last_close, f"expected_close_{k}") if last_close else None
        rows.append(dict(
            key=k, account=a, sys_open=sys_open, money_in=money_in, money_out=money_out, sys_close=sys_close,
            counted_open=c_open, open_diff=None if c_open is None or e_open is None else c_open - e_open,
            counted_close=c_close, close_diff=None if c_close is None or e_close is None else c_close - e_close))
    # what moved each account, by operation
    by_type = {}
    for r in Entry.objects.filter(account__in=accounts.values(), business_date=day) \
            .values("transaction__type", "account_id").annotate(s=Sum("amount")):
        d = by_type.setdefault(r["transaction__type"], {"cash": Decimal(0), "uve": Decimal(0), "er": Decimal(0)})
        for k, a in accounts.items():
            if a.pk == r["account_id"]:
                d[k] += r["s"]
    ids = set(Entry.objects.filter(account__in=accounts.values(), business_date=day)
              .values_list("transaction_id", flat=True))
    tx = Transaction.objects.filter(Q(shop=shop) | Q(pk__in=ids), business_date=day, correction__isnull=True) \
        .exclude(type=TxType.REVERSAL).values("type").annotate(n=Count("id"), amt=Sum("amount"),
                                                                com=Sum("commission"))
    counts = {r["type"]: r for r in tx}
    ops = []
    for t, eff in sorted(by_type.items(), key=lambda kv: TxType(kv[0]).label):
        c = counts.get(t) or {}
        ops.append(dict(label=TxType(t).label, n=c.get("n") or 0, amt=c.get("amt") or 0, com=c.get("com"),
                        **eff))
    services = ServiceCount.objects.filter(shop=shop, business_date=day).values("service__name") \
        .annotate(n=Sum("count")).order_by("service__name")
    still_open = [s for s in shifts if s.status == ShopShift.Status.OPEN]
    diffs = [r[f] for r in rows for f in ("open_diff", "close_diff") if r[f]]
    if still_open:
        status = "OPEN"
    elif last_close is None:
        status = "NOT_COUNTED"
    elif diffs:
        status = "DIFFERENCE"
    else:
        status = "BALANCED"
    if viewer is not None:  # a staff member with some duties only sees their operations and their own shifts
        from .shops import TYPE_DUTY, is_restricted

        if is_restricted(viewer):
            duties = viewer.shop_duty_set
            keep = {str(TxType(t).label) for t, d in TYPE_DUTY.items() if d in duties}
            ops = [o for o in ops if str(o["label"]) in keep]
            shifts = [x for x in shifts if x.user_id == viewer.pk]
            if "SERVICES" not in duties:
                services = []
    return dict(shop=shop, day=day, rows=rows, ops=ops, services=services, shifts=shifts, still_open=still_open,
                first_open=first_open, last_close=last_close, status=status,
                commission=sum((o["com"] or 0) for o in ops),
                review=ShopDayReview.objects.filter(shop=shop, business_date=day).select_related(
                    "reviewed_by").first())


STATUS_LABELS = {"OPEN": _("Shift(s) still open"), "NOT_COUNTED": _("No closing count"),
                 "DIFFERENCE": _("Difference"), "BALANCED": _("Balanced")}


def review_day(user, *, shop, day, note=""):
    if not (user.is_shop_senior or user.is_superadmin):
        raise PermissionDenied(_("Only the senior shop manager signs off a shop's day."))
    rec = day_reconciliation(shop, day)
    if rec["still_open"]:
        raise BusinessError(_("Some shifts of this day are still open. Close them first."))
    if rec["status"] == "DIFFERENCE" and not (note or "").strip():
        raise BusinessError(_("Explain the difference before signing off."), "note")
    r, _created = ShopDayReview.objects.update_or_create(
        shop=shop, business_date=day, defaults=dict(reviewed_by=user, reviewed_at=timezone.now(), note=note))
    log_event(user, "shop_day_reviewed", shop=shop.name, day=day.isoformat(), status=rec["status"])
    return r


def attendance(shops, d_from, d_to, user=None):
    qs = ShopShift.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
        .select_related("shop", "user", "closed_by")
    if user is not None:
        qs = qs.filter(user=user)
    per = {}
    for s in qs:
        p = per.setdefault(s.user_id, dict(user=s.user, shop=s.shop, days=set(), hours=0.0, shifts=0, open=0,
                                           forced=0))
        p["days"].add(s.business_date)
        p["hours"] += s.hours
        p["shifts"] += 1
        p["open"] += s.status == ShopShift.Status.OPEN
        p["forced"] += bool(s.closed_by_id and s.closed_by_id != s.user_id)
    people = sorted(per.values(), key=lambda p: (p["shop"].name, str(p["user"])))
    for p in people:
        p["days"] = len(p["days"])
        p["hours"] = round(p["hours"], 2)
    return qs, people


