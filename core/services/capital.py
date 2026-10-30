"""Capital movements by the finance manager, the senior manager or the super administrator.

  injection   Dr the company account (cash drawer, UVE, ER, MTN, bank — cashier side or shop)   Cr owner's capital
  withdrawal  Dr owner's capital                                                               Cr the company account
  transfer    Dr the receiving account                                                         Cr the sending account

Every movement records a description, the source of the funds (or, for a withdrawal, who / where it goes to) and
its purpose. It is posted in the business day of the cashier subzone that holds the account (so it shows on that
cashier's side and in the closing), on the shop when a shop account is involved, and on the bank otherwise.
Owner's capital (credit balance) is the net capital the owners have put into the business.
"""
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import Account, TxType, User
from core.money import MoneyError, to_money

from . import permissions as perm
from .days import douala_today, zone_day
from .errors import BusinessError
from .ledger import Ctx, TxSpec, balance, post

K = Account.Kind
R = User.Role
CAPITAL_ROLES = (R.MANAGER, R.FINANCE, R.SUPERADMIN)
CAPITAL_TYPES = (TxType.CAPITAL_IN, TxType.CAPITAL_OUT, TxType.CAPITAL_MOVE)
MOVABLE_KINDS = (K.CASH_DRAWER, K.UVE, K.ER, K.MTN, K.BANK)
# what may be transferred into what: same product, or cash <-> bank (deposit / withdrawal at the bank)
COMPATIBLE = {K.CASH_DRAWER: {K.CASH_DRAWER, K.BANK}, K.BANK: {K.BANK, K.CASH_DRAWER},
              K.UVE: {K.UVE}, K.ER: {K.ER}, K.MTN: {K.MTN}}


def capital_account():
    return Account.system(K.OWNER_CAPITAL)


def net_capital():
    """Capital the owners have put in, net of withdrawals (owner's capital is a credit balance)."""
    return -balance(capital_account())


def _amount(v):
    try:
        a = to_money(v)
    except MoneyError as exc:
        raise BusinessError(_(str(exc)), "amount") from exc
    if a <= 0:
        raise BusinessError(_("Amount must be greater than zero."), "amount")
    return a


def _account(acct, field):
    if acct is None or acct.kind not in MOVABLE_KINDS or not acct.active:
        raise BusinessError(_("Choose an active company account (cash drawer, UVE, ER, MTN float or bank)."), field)
    return acct


def _required(value, field, message):
    value = (value or "").strip()
    if not value:
        raise BusinessError(message, field)
    return value


def _zone_of(acct):
    if acct.custodian_id and acct.custodian.subzone_id:
        return acct.custodian.subzone
    return acct.zone


def _ctx(user, accounts):
    """The business day of the first cashier-side account involved (so the cashier sees it and the closing
    counts it); otherwise today."""
    for a in accounts:
        z = _zone_of(a) if not a.shop_id else None
        if z is not None:
            day = zone_day(z)
            return Ctx(entered_by=user, cashier=None, day=day, business_date=day.business_date)
    return Ctx(entered_by=user, cashier=None, day=None, business_date=douala_today())


def _shop(accounts):
    return next((a.shop for a in accounts if a.shop_id), None)


def _post(user, type_, lines, accounts, idem, **fields):
    perm.require_role(user, CAPITAL_ROLES, "capital")
    ctx = _ctx(user, accounts)
    with transaction.atomic():
        return post([TxSpec(type=type_, lines=lines, fields=dict(shop=_shop(accounts), **fields), receipt=True)],
                    ctx, idem)[0]


def inject(user, *, account, amount, source, purpose, description="", idempotency_key=None):
    """New money put into the business by the owners (cash or UVE, or ER, MTN float, bank)."""
    acct, a = _account(account, "account"), _amount(amount)
    source = _required(source, "source", _("Say where the money comes from."))
    purpose = _required(purpose, "purpose", _("Say what the money is for."))
    return _post(user, TxType.CAPITAL_IN, [(acct, a), (capital_account(), -a)], [acct], idempotency_key,
                 amount=a, dest_account=acct, fund_source=source, fund_purpose=purpose, description=description,
                 product=_product(acct))


def withdraw(user, *, account, amount, beneficiary, purpose, description="", idempotency_key=None):
    """Money taken out of the business by the owners."""
    acct, a = _account(account, "account"), _amount(amount)
    beneficiary = _required(beneficiary, "beneficiary", _("Say who receives the money or where it goes."))
    purpose = _required(purpose, "purpose", _("Say why the capital is withdrawn."))
    return _post(user, TxType.CAPITAL_OUT, [(capital_account(), a), (acct, -a)], [acct], idempotency_key,
                 amount=a, source_account=acct, fund_source=beneficiary, fund_purpose=purpose,
                 description=description, product=_product(acct))


def transfer(user, *, source, destination, amount, purpose, description="", idempotency_key=None):
    """Move capital between any two company accounts (cashier subzones, master SIMs, banks, shops)."""
    src, dst, a = _account(source, "source"), _account(destination, "destination"), _amount(amount)
    if src.pk == dst.pk:
        raise BusinessError(_("Choose two different accounts."), "destination")
    if dst.kind not in COMPATIBLE[src.kind]:
        raise BusinessError(_("%(a)s cannot be transferred into %(b)s. Transfer the same product (cash ↔ bank is "
                              "allowed).") % {"a": K(src.kind).label, "b": K(dst.kind).label}, "destination")
    purpose = _required(purpose, "purpose", _("Say why the capital is moved."))
    return _post(user, TxType.CAPITAL_MOVE, [(dst, a), (src, -a)], [src, dst], idempotency_key,
                 amount=a, source_account=src, dest_account=dst, fund_purpose=purpose, description=description,
                 product=_product(src))


def _product(acct):
    return {K.UVE: "UVE", K.ER: "ER", K.CASH_DRAWER: "CASH"}.get(acct.kind, "")


def account_choices():
    """All accounts capital can move in or out of, with where they are."""
    return Account.objects.filter(kind__in=MOVABLE_KINDS, active=True).select_related(
        "zone__parent", "custodian__subzone", "shop", "sim", "bank").order_by("shop__name", "zone__name", "kind", "id")


def account_label(a):
    where = (_("Shop %(s)s") % {"s": a.shop.name}) if a.shop_id else (
        str(_zone_of(a)) if _zone_of(a) else (_("Bank") if a.kind == K.BANK else _("Company")))
    return f"{where} · {a}"


def totals():
    from django.db.models import Sum

    from core.models import Transaction

    agg = {r["type"]: r["s"] for r in Transaction.objects.filter(type__in=CAPITAL_TYPES, correction__isnull=True)
           .values("type").annotate(s=Sum("amount"))}
    return dict(injected=agg.get(TxType.CAPITAL_IN) or Decimal(0), withdrawn=agg.get(TxType.CAPITAL_OUT) or Decimal(0),
                moved=agg.get(TxType.CAPITAL_MOVE) or Decimal(0), net=net_capital())
