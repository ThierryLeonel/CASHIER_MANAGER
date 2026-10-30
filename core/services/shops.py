"""Shop operations. Every money movement goes through the same double-entry ledger as the cashiers, on the
shop's own accounts (cash drawer, UVE, ER), with Transaction.shop set and no cashier/zone/business day.

Effects on the shop's balances (agreed with the owner):
  cash-in        UVE −, cash +           (commission recorded, not posted)
  cash-out       UVE +, cash −           (commission recorded, not posted)
  airtime sale   ER −,  cash +
  conversion     UVE −, ER + (UVE × (1 + shop rate))
  appro (agent)  UVE +, cash −  — or owed to the agent when on credit
  ER supply      ER +,  cash − or UVE −  — or owed to the agent when on credit
  destockage     UVE −, cash +           (or the agent owes the shop when on credit)
  shop pays agent  position +, cash − or UVE −
  agent pays shop  cash + or UVE +, position −
  to a cashier   shop UVE −, the cashier subzone's UVE wallet + (in that subzone's business day)
  cashier → shop the cashier subzone's UVE wallet −, shop UVE +
  item sale      cash +, shop sales −; each unit sold (IMEI/serial) leaves the stock

The position with an agent (one account per shop and agent): negative = the shop owes the agent,
positive = the agent owes the shop.
"""
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import (
    SHOP_DUTIES, Account, Counterparty, ItemSale, ServiceCount, Shop, ShopConversionRate, ShopItem, SimRetrieval, StockMove,
    StockUnit, TxType, User,
)
from core.money import MoneyError, conversion_er_amount, fmt, to_money
from core.simnumbers import SimNumberError, normalize_sim

from .days import douala_today
from .errors import BusinessError, PermissionDenied
from .ledger import Ctx, TxSpec, post

K = Account.Kind
PAY_KINDS = {"CASH": K.CASH_DRAWER, "UVE": K.UVE}


# ---------------------------------------------------------------- who may do what
DUTY_LABEL = dict(SHOP_DUTIES)
# shop page / operation -> duty needed (segmented shops: each staff member has some of the duties)
OP_DUTY = {"cashin": "OM", "cashout": "OM", "airtime": "AIRTIME", "convert": "AIRTIME", "sale": "SALES",
           "stock": "SALES", "service": "SERVICES", "retrieve": "SERVICES", "appro": "AGENTS", "er_supply": "AGENTS",
           "destockage": "AGENTS", "pay_agent": "AGENTS", "agent_repay": "AGENTS", "to_cashier": "AGENTS"}


def check_duty(user, duty):
    if duty and not user.has_shop_duty(duty):
        raise PermissionDenied(_("This is not one of your duties in the shop (%(d)s). Ask the senior shop manager.")
                               % {"d": DUTY_LABEL[duty]})


def shop_for(user, shop=None, duty=None):
    """The shop a user records for: a shop manager's own shop; a senior shop manager / super admin choose one.
    `duty` is checked for shop managers whose duties are restricted (segmented shops)."""
    if not user.is_authenticated:
        raise PermissionDenied(_("Sign in first."))
    if user.is_shop_manager:
        check_duty(user, duty)
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
    shop = shop_for(user, shop, "OM")
    a = _amt(amount)
    com = _amt(commission, "commission", allow_zero=True) if commission not in (None, "") else None
    return _record(user, [_spec(TxType.SHOP_CASHIN, shop, [(shop.account(K.CASH_DRAWER), a), (shop.account(K.UVE), -a)],
                                a, commission=com, client_phone=_phone(client_phone), description=description)],
                   idempotency_key)[0]


def cash_out(user, *, shop=None, amount, commission=None, client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop, "OM")
    a = _amt(amount)
    com = _amt(commission, "commission", allow_zero=True) if commission not in (None, "") else None
    return _record(user, [_spec(TxType.SHOP_CASHOUT, shop, [(shop.account(K.UVE), a), (shop.account(K.CASH_DRAWER), -a)],
                                a, commission=com, client_phone=_phone(client_phone), description=description)],
                   idempotency_key)[0]


AIRTIME_KINDS = {"NORMAL": _("Normal airtime"), "GOOD_DEAL": _("Good deal")}


def airtime(user, *, shop=None, amount, kind="NORMAL", client_phone="", description="", idempotency_key=None):
    shop = shop_for(user, shop, "AIRTIME")
    a = _amt(amount)
    if kind not in AIRTIME_KINDS:
        raise BusinessError(_("Choose good deal or normal airtime."), "kind")
    return _record(user, [_spec(TxType.SHOP_AIRTIME, shop, [(shop.account(K.CASH_DRAWER), a), (shop.account(K.ER), -a)],
                                a, subtype=kind, client_phone=_phone(client_phone), description=description)],
                   idempotency_key)[0]


def convert(user, *, shop=None, amount, description="", idempotency_key=None):
    shop = shop_for(user, shop, "AIRTIME")
    a = _amt(amount)
    rate = ShopConversionRate.current()
    if rate is None:
        raise BusinessError(_("The shop conversion rate is not set yet (Conversion rate page › Shops' rate)."))
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
    shop, agent, a = shop_for(user, shop, "AGENTS"), _agent(agent), _amt(amount)
    if pay == "UVE":
        raise BusinessError(_("An UVE supply is paid in cash or taken on credit."), "pay")
    src, credit = _paid_with(shop, agent, pay, a)
    return _record(user, [_spec(TxType.SHOP_APPRO, shop, [(shop.account(K.UVE), a), (src, -a)], a,
                                counterparty=agent, on_credit=credit, product="UVE", description=description)],
                   idempotency_key)[0]


def er_supply(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """The agent supplies ER to the shop; the shop pays cash or UVE, or owes the agent (credit)."""
    shop, agent, a = shop_for(user, shop, "AGENTS"), _agent(agent), _amt(amount)
    src, credit = _paid_with(shop, agent, pay, a)
    return _record(user, [_spec(TxType.SHOP_ER_SUPPLY, shop, [(shop.account(K.ER), a), (src, -a)], a,
                                counterparty=agent, on_credit=credit, product="ER", description=description)],
                   idempotency_key)[0]


def destockage(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """Cash supply from the agent: the shop sends the agent UVE and receives cash — or, on credit, the agent owes
    the shop the cash."""
    shop, agent, a = shop_for(user, shop, "AGENTS"), _agent(agent), _amt(amount)
    if pay not in ("CASH", "CREDIT"):
        raise BusinessError(_("Choose cash received or on credit."), "pay")
    dest = shop.account(K.CASH_DRAWER) if pay == "CASH" else shop.payable_to(agent)
    return _record(user, [_spec(TxType.SHOP_DESTOCK, shop, [(dest, a), (shop.account(K.UVE), -a)], a,
                                counterparty=agent, on_credit=pay == "CREDIT", product="CASH",
                                description=description)], idempotency_key)[0]


def agent_repay(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """The agent pays back what he owes the shop, in cash or UVE."""
    shop, agent, a = shop_for(user, shop, "AGENTS"), _agent(agent), _amt(amount)
    if pay not in PAY_KINDS:
        raise BusinessError(_("Choose how it was paid: cash or UVE."), "pay")
    owes = owed_by_agent(shop, agent)
    if a > owes:
        raise BusinessError(_("%(agent)s owes the shop only %(owed)s FCFA.") % {"agent": agent.name, "owed": fmt(owes)},
                            "amount")
    return _record(user, [_spec(TxType.SHOP_AGENT_REPAY, shop, [(shop.account(PAY_KINDS[pay]), a),
                                                                (shop.payable_to(agent), -a)], a,
                                counterparty=agent, description=description)], idempotency_key)[0]


def pay_agent(user, *, shop=None, agent, amount, pay="CASH", description="", idempotency_key=None):
    """Settle what the shop owes an agent for supplies taken on credit."""
    shop, agent, a = shop_for(user, shop, "AGENTS"), _agent(agent), _amt(amount)
    if pay not in PAY_KINDS:
        raise BusinessError(_("Choose how it was paid: cash or UVE."), "pay")
    owed = owed_to_agent(shop, agent)
    if a > owed:
        raise BusinessError(_("The shop owes %(agent)s only %(owed)s FCFA.") % {"agent": agent.name, "owed": fmt(owed)},
                            "amount")
    return _record(user, [_spec(TxType.SHOP_AGENT_PAY, shop, [(shop.payable_to(agent), a),
                                                              (shop.account(PAY_KINDS[pay]), -a)], a,
                                counterparty=agent, description=description)], idempotency_key)[0]


def agent_position(shop, agent):
    """Positive = the agent owes the shop; negative = the shop owes the agent."""
    from .ledger import balance

    acct = Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop, shop_agent=agent).first()
    return balance(acct) if acct else Decimal(0)


def owed_to_agent(shop, agent):
    return max(-agent_position(shop, agent), Decimal(0))


def owed_by_agent(shop, agent):
    return max(agent_position(shop, agent), Decimal(0))


# ---------------------------------------------------------------- transfers between shops and cashiers (UVE only)
def to_cashier(user, *, shop=None, destination, amount, description="", idempotency_key=None):
    """Send UVE from the shop to a cashier subzone's UVE wallet. The entry belongs to that subzone's business day,
    so it shows on the cashier side too (lists, closing)."""
    from .days import zone_day

    shop, a = shop_for(user, shop, "AGENTS"), _amt(amount)
    if destination is None or destination.kind != K.UVE or destination.shop_id or not destination.zone_id \
            or not destination.active:
        raise BusinessError(_("Choose the cashier UVE wallet that receives it."), "destination")
    zone = destination.custodian.subzone if destination.custodian_id else destination.zone
    day = zone_day(zone)
    ctx = Ctx(entered_by=user, cashier=None, day=day, business_date=day.business_date)
    src = shop.account(K.UVE)
    with transaction.atomic():
        return post([_spec(TxType.SHOP_TO_DESK, shop, [(destination, a), (src, -a)], a, dest_account=destination,
                           source_account=src, product="UVE", description=description)], ctx, idempotency_key)[0]


def desk_to_shop(user, *, shop, amount, source=None, description="", idempotency_key=None, acting_for=None):
    """A cashier sends UVE from the subzone's UVE wallet to a shop."""
    from . import permissions as perm
    from .operations import cashier_ctx

    ctx = cashier_ctx(user, acting_for)
    a = _amt(amount)
    if shop is None or not shop.active:
        raise BusinessError(_("Choose the shop."), "shop")
    if source is None:
        qs = perm.usable_accounts(ctx.cashier, [K.UVE]).filter(shop__isnull=True)
        if qs.count() != 1:
            raise BusinessError(_("Choose the UVE wallet the UVE leaves from."), "source")
        source = qs.first()
    if source.kind != K.UVE or not perm.can_send_from(ctx.cashier, source):
        raise BusinessError(_("Choose the UVE wallet the UVE leaves from."), "source")
    dest = shop.account(K.UVE)
    with transaction.atomic():
        return post([TxSpec(type=TxType.DESK_TO_SHOP, lines=[(dest, a), (source, -a)],
                            fields=dict(amount=a, shop=shop, source_account=source, dest_account=dest, product="UVE",
                                        description=description), receipt=True)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- sales and stock (units by IMEI / serial)
def _clean_serials(text):
    raw = [x.strip() for chunk in (text or "").replace(",", "\n").replace(";", "\n").splitlines()
           for x in [chunk] if x.strip()]
    seen, dup = set(), set()
    for x in raw:
        (dup if x.upper() in seen else seen).add(x.upper())
    if dup:
        raise BusinessError(_("Typed twice: %(s)s") % {"s": ", ".join(sorted(dup))}, "serials")
    return raw


def _can_stock(user, shop):
    if user.is_shop_manager:
        check_duty(user, "SALES")
    if not (user.is_shop_senior or user.is_superadmin or (user.is_shop_manager and user.shop_id == shop.pk)):
        raise PermissionDenied(_("You cannot change this shop's stock."))


def receive_stock(user, *, shop, item, serials, note=""):
    """Receive units into a shop's stock, each with its IMEI / serial number."""
    _can_stock(user, shop)
    if item is None or not item.active:
        raise BusinessError(_("Choose an item."), "item")
    found = _clean_serials(serials)
    if not found:
        raise BusinessError(_("Enter the IMEI or serial number of each unit, one per line."), "serials")
    taken = list(StockUnit.objects.filter(serial__in=found).values_list("serial", flat=True))
    if taken:
        raise BusinessError(_("Already registered: %(s)s") % {"s": ", ".join(taken)}, "serials")
    with transaction.atomic():
        units = [StockUnit.objects.create(shop=shop, item=item, serial=x, received_by=user, note=note) for x in found]
        for u in units:
            StockMove.objects.create(shop=shop, item=item, unit=u, kind=StockMove.Kind.RECEIVED, quantity=1,
                                     recorded_by=user, note=note)
    return units


def remove_unit(user, *, serial, note):
    """Take a unit out of stock (returned, damaged, lost…)."""
    u = StockUnit.objects.filter(serial__iexact=(serial or "").strip()).select_related("shop", "item").first()
    if u is None:
        raise BusinessError(_("No unit with this IMEI / serial number."), "serial")
    _can_stock(user, u.shop)
    if u.status != StockUnit.Status.IN_STOCK:
        raise BusinessError(_("This unit is not in stock (%(s)s).") % {"s": u.get_status_display()}, "serial")
    if not (note or "").strip():
        raise BusinessError(_("Give the reason."), "note")
    with transaction.atomic():
        StockUnit.objects.filter(pk=u.pk).update(status=StockUnit.Status.REMOVED, note=note.strip())
        StockMove.objects.create(shop=u.shop, item=u.item, unit=u, kind=StockMove.Kind.ADJUST, quantity=-1,
                                 recorded_by=user, note=note.strip())
    return u


CLIENT_FIELDS = ("client_name", "client_address", "client_country", "id_type", "id_number", "phone")


def sell(user, *, shop=None, item, units, unit_price=None, client_name, client_address, client_country, id_type,
         id_number, phone, whatsapp="", description="", idempotency_key=None):
    """Sell one or more units (chosen by IMEI / serial) to an identified client."""
    shop = shop_for(user, shop, "SALES")
    if item is None or not item.active:
        raise BusinessError(_("Choose an item."), "item")
    units = list(units or [])
    if not units:
        raise BusinessError(_("Choose the unit(s) sold by IMEI / serial number."), "units")
    for f, v in (("client_name", client_name), ("client_address", client_address), ("client_country", client_country),
                 ("id_number", id_number), ("phone", phone)):
        if not (v or "").strip():
            raise BusinessError(_("This client information is required."), f)
    if id_type not in ItemSale.IdType.values:
        raise BusinessError(_("Choose the type of identification."), "id_type")
    price = _amt(unit_price, "unit_price") if unit_price not in (None, "") else Decimal(item.price)
    with transaction.atomic():
        locked = list(StockUnit.objects.select_for_update().filter(pk__in=[u.pk for u in units]))
        for u in locked:
            if u.shop_id != shop.pk or u.item_id != item.pk or u.status != StockUnit.Status.IN_STOCK:
                raise BusinessError(_("%(s)s is not in stock at %(shop)s for this item.")
                                    % {"s": u.serial, "shop": shop.name}, "units")
        total = price * len(locked)
        serials = ", ".join(u.serial for u in locked)
        t = post([_spec(TxType.SHOP_SALE, shop, [(shop.account(K.CASH_DRAWER), total),
                                                 (Account.system(K.SHOP_SALES), -total)], total,
                        client_phone=_phone(phone),
                        description=(f"{len(locked)} × {item.name} ({serials})"
                                     + (f" — {description}" if description else "")))],
                 _ctx(user), idempotency_key)[0]
        sale = ItemSale.objects.create(transaction=t, shop=shop, item=item, unit_price=price,
                                       client_name=client_name.strip(), client_address=client_address.strip(),
                                       client_country=client_country.strip(), id_type=id_type,
                                       id_number=id_number.strip(), phone=_phone(phone), whatsapp=_phone(whatsapp))
        StockUnit.objects.filter(pk__in=[u.pk for u in locked]).update(status=StockUnit.Status.SOLD, sale=sale)
        for u in locked:
            StockMove.objects.create(shop=shop, item=item, unit=u, kind=StockMove.Kind.SALE, quantity=-1,
                                     transaction=t, recorded_by=user, note=fmt(price))
    return t


# ---------------------------------------------------------------- counted services and SIM retrievals
def count_service(user, *, shop=None, service, count, business_date=None, note=""):
    shop = shop_for(user, shop, "SERVICES")
    if service is None or not service.active or service.kind != service.Kind.COUNT:
        raise BusinessError(_("Choose a counted service."), "service")
    n = int(count or 0)
    if n <= 0:
        raise BusinessError(_("The count must be at least 1."), "count")
    return ServiceCount.objects.create(shop=shop, service=service, count=n, note=note, recorded_by=user,
                                       business_date=business_date or douala_today())


def retrieve_sim(user, *, shop=None, service=None, sim_number, holder_name, id_type, id_number, phone="", iccid="",
                 note="", business_date=None):
    shop = shop_for(user, shop, "SERVICES")
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
    pos = [balance(a) for a in Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop)]
    out["owed_agents"] = sum((-p for p in pos if p < 0), Decimal(0))
    out["owed_by_agents"] = sum((p for p in pos if p > 0), Decimal(0))
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
