"""Shop operations. Every money movement goes through the same double-entry ledger as the cashiers, on the
shop's own accounts (cash drawer, UVE, ER), with Transaction.shop set and no cashier/zone/business day.

Effects on the shop's balances (agreed with the owner):
  cash-in        UVE −, cash +           (commission recorded, not posted)
  cash-out       UVE +, cash −           (commission recorded, not posted)
  airtime sale   ER −,  cash +
  conversion     UVE −, ER + (UVE × (1 + shop rate))
  appro (agent)  UVE +, cash −  — or owed to the agent when on credit
  ER supply      ER +,  cash − or UVE −  — or owed to the agent when on credit
  destockage     UVE −, cash +           (UVE sent to the agent, cash received)
  agent payment  owed to the agent −, cash − or UVE −
  to a cashier   shop cash/UVE −, the cashier subzone's drawer/UVE wallet +
  item sale      cash +, shop sales −; stock − quantity
"""
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import (
    Account, Counterparty, ServiceCount, Shop, ShopConversionRate, ShopItem, SimRetrieval, StockMove, TxType, User,
)
from core.money import MoneyError, conversion_er_amount, fmt, to_money
from core.simnumbers import SimNumberError, normalize_sim

from .days import douala_today
from .errors import BusinessError, PermissionDenied
from .ledger import Ctx, TxSpec, post

K = Account.Kind
PAY_KINDS = {"CASH": K.CASH_DRAWER, "UVE": K.UVE}


# ---------------------------------------------------------------- who may do what
def shop_for(user, shop=None):
    """The shop a user records for: a shop manager's own shop; a senior shop manager / super admin choose one."""
    if not user.is_authenticated:
        raise PermissionDenied(_("Sign in first."))
    if user.is_shop_manager:
        if shop is not None and shop.pk != user.shop_id:
            raise PermissionDenied(_("You can only record for your own shop."))
        if not user.shop_id:
            raise PermissionDenied(_("Your account is not linked to a shop."))
        return user.shop
    if user.is_shop_senior or user.is_superadmin:
        if shop is None:
            raise BusinessError(_("Choose the shop."), "shop")
        return shop
    raise PermissionDenied(_("Only shop managers record shop operations."))


def visible_shops(user):
    if user.sees_all_shops:
        return Shop.objects.all()
    if user.is_shop_manager and user.shop_id:
        return Shop.objects.filter(pk=user.shop_id)
    return Shop.objects.none()


def _ctx(user, business_date=None):
    return Ctx(entered_by=user, cashier=None, day=None, business_date=business_date or douala_today())


def _amt(v, field="amount", allow_zero=False):
    try:
        a = to_money(v)
    except MoneyError as exc:
        raise BusinessError(_(str(exc)), field) from exc
    if a < 0 or (a == 0 and not allow_zero):
        raise BusinessError(_("Amount must be greater than zero."), field)
    return a


def _phone(v):
    v = (v or "").strip()
    if not v:
        return ""
    try:
        return normalize_sim(v)
    except SimNumberError:
        return v[:30]


def _agent(agent):
    if agent is None or agent.kind != Counterparty.Kind.AGENT or not agent.active:
        raise BusinessError(_("Choose an active commercial agent."), "agent")
    return agent


def _spec(type_, shop, lines, amount, **fields):
    return TxSpec(type=type_, lines=lines, fields=dict(amount=amount, shop=shop, **fields), receipt=True)


def _record(user, specs, idem):
    with transaction.atomic():
        return post(specs, _ctx(user), idem)


# ---------------------------------------------------------------- Orange Money, airtime, conversion
def cash_in(user, *, shop=None, amount, commission=None, client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop)
    a = _amt(amount)
    com = _amt(commission, "commission", allow_zero=True) if commission not in (None, "") else None
    return _record(user, [_spec(TxType.SHOP_CASHIN, shop, [(shop.account(K.CASH_DRAWER), a), (shop.account(K.UVE), -a)],
                                a, commission=com, client_phone=_phone(client_phone), description=description)],
                   idempotency_key)[0]


def cash_out(user, *, shop=None, amount, commission=None, client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop)
    a = _amt(amount)
    com = _amt(commission, "commission", allow_zero=True) if commission not in (None, "") else None
    return _record(user, [_spec(TxType.SHOP_CASHOUT, shop, [(shop.account(K.UVE), a), (shop.account(K.CASH_DRAWER), -a)],
                                a, commission=com, client_phone=_phone(client_phone), description=description)],
                   idempotency_key)[0]


def airtime(user, *, shop=None, amount, client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop)
    a = _amt(amount)
    return _record(user, [_spec(TxType.SHOP_AIRTIME, shop, [(shop.account(K.CASH_DRAWER), a), (shop.account(K.ER), -a)],
                                a, client_phone=_phone(client_phone), description=description)], idempotency_key)[0]


def convert(user, *, shop=None, amount, description="", idempotency_key=None):
    shop = shop_for(user, shop)
    a = _amt(amount)
    rate = ShopConversionRate.current()
    if rate is None:
        raise BusinessError(_("The shop conversion rate is not set yet (Administration › Shop conversion rate)."))
    er = conversion_er_amount(a, rate.rate)
    uplift = Account.system(K.CONVERSION_UPLIFT)
    lines = [(shop.account(K.ER), er), (shop.account(K.UVE), -a)]
    if er != a:
        lines.append((uplift, a - er))
    return _record(user, [_spec(TxType.SHOP_CONVERT, shop, lines, a, rate_used=rate.rate, amount_received=er,
                                description=description)], idempotency_key)[0]


# ---------------------------------------------------------------- commercial agents
def _paid_with(shop, agent, pay, amount):
    """The account that pays the agent: cash, UVE, or the shop's debt to the agent (credit)."""
    if pay == "CREDIT":
        return shop.payable_to(agent), True
    if pay not in PAY_KINDS:
        raise BusinessError(_("Choose how it was paid: cash, UVE or on credit."), "pay")
    return shop.account(PAY_KINDS[pay]), False


def appro(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """The agent supplies UVE to the shop; the shop pays cash, or owes the agent (credit)."""
    shop, agent, a = shop_for(user, shop), _agent(agent), _amt(amount)
    if pay == "UVE":
        raise BusinessError(_("An UVE supply is paid in cash or taken on credit."), "pay")
    src, credit = _paid_with(shop, agent, pay, a)
    return _record(user, [_spec(TxType.SHOP_APPRO, shop, [(shop.account(K.UVE), a), (src, -a)], a,
                                counterparty=agent, on_credit=credit, product="UVE", description=description)],
                   idempotency_key)[0]


def er_supply(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """The agent supplies ER to the shop; the shop pays cash or UVE, or owes the agent (credit)."""
    shop, agent, a = shop_for(user, shop), _agent(agent), _amt(amount)
    src, credit = _paid_with(shop, agent, pay, a)
    return _record(user, [_spec(TxType.SHOP_ER_SUPPLY, shop, [(shop.account(K.ER), a), (src, -a)], a,
                                counterparty=agent, on_credit=credit, product="ER", description=description)],
                   idempotency_key)[0]


def destockage(user, *, shop=None, agent, amount, description="", idempotency_key=None):
    """Cash supply from the agent: the shop sends the agent UVE and receives cash."""
    shop, agent, a = shop_for(user, shop), _agent(agent), _amt(amount)
    return _record(user, [_spec(TxType.SHOP_DESTOCK, shop, [(shop.account(K.CASH_DRAWER), a), (shop.account(K.UVE), -a)],
                                a, counterparty=agent, product="CASH", description=description)], idempotency_key)[0]


def pay_agent(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """Settle what the shop owes an agent for supplies taken on credit."""
    shop, agent, a = shop_for(user, shop), _agent(agent), _amt(amount)
    if pay not in PAY_KINDS:
        raise BusinessError(_("Choose how it was paid: cash or UVE."), "pay")
    owed = owed_to_agent(shop, agent)
    if a > owed:
        raise BusinessError(_("The shop owes %(agent)s only %(owed)s FCFA.") % {"agent": agent.name, "owed": fmt(owed)},
                            "amount")
    return _record(user, [_spec(TxType.SHOP_AGENT_PAY, shop, [(shop.payable_to(agent), a),
                                                              (shop.account(PAY_KINDS[pay]), -a)], a,
                                counterparty=agent, description=description)], idempotency_key)[0]


def owed_to_agent(shop, agent):
    from .ledger import balance

    acct = Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop, shop_agent=agent).first()
    return -balance(acct) if acct else Decimal(0)


# ---------------------------------------------------------------- transfers to cashiers
def to_cashier(user, *, shop=None, kind, destination, amount, description="", idempotency_key=None):
    """Move cash or UVE from the shop to a cashier subzone's drawer / UVE wallet."""
    shop, a = shop_for(user, shop), _amt(amount)
    if kind not in PAY_KINDS:
        raise BusinessError(_("Choose cash or UVE."), "kind")
    if destination is None or destination.kind != PAY_KINDS[kind] or destination.shop_id or not destination.zone_id \
            or not destination.active:
        raise BusinessError(_("Choose the cashier account that receives it."), "destination")
    return _record(user, [_spec(TxType.SHOP_TO_DESK, shop, [(destination, a), (shop.account(PAY_KINDS[kind]), -a)], a,
                                dest_account=destination, source_account=shop.account(PAY_KINDS[kind]),
                                description=description)], idempotency_key)[0]


# ---------------------------------------------------------------- sales and stock
def sell(user, *, shop=None, item, quantity, unit_price=None, client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop)
    if item is None or not item.active:
        raise BusinessError(_("Choose an item."), "item")
    q = int(quantity or 0)
    if q <= 0:
        raise BusinessError(_("Quantity must be at least 1."), "quantity")
    price = _amt(unit_price, "unit_price") if unit_price not in (None, "") else Decimal(item.price)
    in_stock = item.stock_at(shop)
    if q > in_stock:
        raise BusinessError(_("Only %(n)s × %(item)s in stock at %(shop)s.") % {"n": in_stock, "item": item.name,
                                                                              "shop": shop.name}, "quantity")
    total = price * q
    with transaction.atomic():
        t = post([_spec(TxType.SHOP_SALE, shop, [(shop.account(K.CASH_DRAWER), total),
                                                 (Account.system(K.SHOP_SALES), -total)], total,
                        client_phone=_phone(client_phone),
                        description=(f"{q} × {item.name}" + (f" — {description}" if description else "")))],
                 _ctx(user), idempotency_key)[0]
        StockMove.objects.create(shop=shop, item=item, kind=StockMove.Kind.SALE, quantity=-q, transaction=t,
                                 recorded_by=user, note=f"{q} × {fmt(price)}")
    return t


def receive_stock(user, *, shop, item, quantity, note=""):
    if not (user.is_shop_senior or user.is_superadmin or (user.is_shop_manager and user.shop_id == shop.pk)):
        raise PermissionDenied(_("You cannot change this shop's stock."))
    q = int(quantity or 0)
    if q == 0:
        raise BusinessError(_("Enter a quantity."), "quantity")
    kind = StockMove.Kind.RECEIVED if q > 0 else StockMove.Kind.ADJUST
    if q < 0 and item.stock_at(shop) + q < 0:
        raise BusinessError(_("Stock cannot go below zero."), "quantity")
    return StockMove.objects.create(shop=shop, item=item, kind=kind, quantity=q, recorded_by=user, note=note)


# ---------------------------------------------------------------- counted services and SIM retrievals
def count_service(user, *, shop=None, service, count, business_date=None, note=""):
    shop = shop_for(user, shop)
    if service is None or not service.active or service.kind != service.Kind.COUNT:
        raise BusinessError(_("Choose a counted service."), "service")
    n = int(count or 0)
    if n <= 0:
        raise BusinessError(_("The count must be at least 1."), "count")
    return ServiceCount.objects.create(shop=shop, service=service, count=n, note=note, recorded_by=user,
                                       business_date=business_date or douala_today())


def retrieve_sim(user, *, shop=None, service=None, sim_number, holder_name, id_type, id_number, phone="", iccid="",
                 note="", business_date=None):
    shop = shop_for(user, shop)
    try:
        sim = normalize_sim(sim_number)
    except SimNumberError as exc:
        raise BusinessError(_(str(exc)), "sim_number") from exc
    if not (holder_name or "").strip():
        raise BusinessError(_("Enter the name of the person retrieving the SIM."), "holder_name")
    if not (id_number or "").strip():
        raise BusinessError(_("Enter the ID number."), "id_number")
    return SimRetrieval.objects.create(shop=shop, service=service, sim_number=sim, holder_name=holder_name.strip(),
                                       id_type=id_type, id_number=id_number.strip(), phone=_phone(phone),
                                       iccid=(iccid or "").strip(), note=note, recorded_by=user,
                                       business_date=business_date or douala_today())


# ---------------------------------------------------------------- balances
def shop_balances(shop):
    from .ledger import balance

    out = {k: balance(shop.account(k)) for k in (K.CASH_DRAWER, K.UVE, K.ER)}
    out["owed_agents"] = -sum((balance(a) for a in Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop)),
                              Decimal(0))
    return out


def set_shop_rate(user, *, percent, note=""):
    if not (user.is_superadmin or user.is_shop_senior):
        raise PermissionDenied(_("Only the super administrator or the senior shop manager sets the shop rate."))
    try:
        p = Decimal(str(percent))
    except Exception as exc:  # noqa: BLE001
        raise BusinessError(_("Enter a percentage."), "percent") from exc
    if p < 0 or p >= 100:
        raise BusinessError(_("Enter a percentage between 0 and 100."), "percent")
    return ShopConversionRate.objects.create(rate=(p / 100).quantize(Decimal("0.000001")), set_by=user, note=note)


__all__ = ["User", "ShopItem"]
