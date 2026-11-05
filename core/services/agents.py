"""Commercial agent portal: supplies to clients, client payments, paybacks to the company.

Zone rule: an agent's zone is the master zone of the agent (set from the agent's microzone). The agent sees the full
information of every POS connected to that master zone; a POS outside it can still be supplied (found by its business
SIM) but the agent only sees its name.

Paybacks: UVE goes to the master SIM of the desk, cash and MTN float to its cashier. A payback stays pending until a
cashier of the chosen desk confirms it; it is then posted as an ordinary company payment (COLLECTION) by the agent, so
it reduces what the agent owes exactly like a payment the cashier records himself."""
import uuid
from collections import OrderedDict
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import (
    AgentClientEntry, AgentExchange, AgentPayback, ClientKind, Counterparty, PaymentMethod, Product, Transaction, TxType, Zone,
)
from core.money import MoneyError, fmt, to_money

from .days import douala_today
from .errors import BusinessError, PermissionDenied

Z = Decimal(0)
E = AgentClientEntry
P = AgentPayback


# ---------------------------------------------------------------- who and where
def agent_of(user):
    if not (user.is_authenticated and user.is_agent and user.counterparty_id):
        raise PermissionDenied(_("Only commercial agents use the agent portal."))
    return user.counterparty


def zone_of(agent):
    """The agent's master zone."""
    if agent.master_zone_id:
        return agent.master_zone
    mz = agent.controlled_microzones.select_related("subzone__parent").first()
    return mz.subzone.parent if mz else None


def zone_pos(agent):
    """POS connected to the agent's zone: the agent sees all their information."""
    z = zone_of(agent)
    if z is None:
        return Counterparty.objects.none()
    return Counterparty.objects.filter(kind=Counterparty.Kind.POS, master_zone=z) \
        .select_related("microzone__subzone", "microzone__agent").order_by("name")


def in_zone(agent, pos):
    z = zone_of(agent)
    return z is not None and pos.master_zone_id == z.pk


def desks_for(agent):
    """The cashier desks of the agent's zone: its subzones (or the master zone itself when it has none)."""
    from .operations import transfer_desks

    z = zone_of(agent)
    if z is None:
        return Zone.objects.none()
    return transfer_desks().filter(Q(pk=z.pk) | Q(parent=z))


def find_pos(sim):
    from core.simnumbers import SimNumberError, normalize_sim

    try:
        s = normalize_sim(sim)
    except SimNumberError as exc:
        raise BusinessError(_(str(exc)), "pos_sim") from exc
    pos = Counterparty.objects.filter(kind=Counterparty.Kind.POS, business_sim=s).first()
    if pos is None:
        raise BusinessError(_("No POS has the business SIM %(sim)s. Ask the office to register it.") % {"sim": sim},
                            "pos_sim")
    if not pos.active:
        raise BusinessError(_("This POS is inactive."), "pos_sim")
    return pos


# ---------------------------------------------------------------- money helpers
def _amt(v, field):
    if v in (None, ""):
        return Z
    try:
        a = to_money(v)
    except MoneyError as exc:
        raise BusinessError(_(str(exc)), field) from exc
    if a < 0:
        raise BusinessError(_("Amounts cannot be negative."), field)
    return a


def _sim(raw, field):
    from core.simnumbers import SimNumberError, normalize_sim

    try:
        return normalize_sim(raw)
    except SimNumberError as exc:
        raise BusinessError(_(str(exc)), field) from exc


def find_agent(sim, me):
    s = _sim(sim, "agent_sim")
    other = Counterparty.objects.filter(kind=Counterparty.Kind.AGENT, business_sim=s, active=True).first()
    if other is None:
        raise BusinessError(_("No active commercial agent has the business SIM %(sim)s.") % {"sim": sim}, "agent_sim")
    if other.pk == me.pk:
        raise BusinessError(_("Choose another agent, not yourself."), "agent_sim")
    return other


def _client(agent, client_kind, pos_sim="", shop=None, client_phone="", agent_sim="", client_name=""):
    out = dict(client_kind=client_kind, pos=None, shop=None, to_agent=None, client_name=(client_name or "").strip(),
               client_phone="")
    if client_kind == ClientKind.POS:
        out["pos"] = find_pos(pos_sim)
    elif client_kind == ClientKind.SHOP:
        if shop is None or not shop.active:
            raise BusinessError(_("Choose the shop."), "shop")
        out["shop"] = shop
    elif client_kind == ClientKind.OTHER:  # identified by the phone number only
        if not (client_phone or "").strip():
            raise BusinessError(_("Enter the client's phone number."), "client_phone")
        out["client_phone"] = _sim(client_phone, "client_phone")
    elif client_kind == ClientKind.AGENT:
        out["to_agent"] = find_agent(agent_sim, agent)
    else:
        raise BusinessError(_("Choose who the client is."), "client_kind")
    return out


def _create(model, idem, **fields):
    if idem:
        prev = model.objects.filter(idem=idem).first()
        if prev:
            return prev
    try:
        with transaction.atomic():
            return model.objects.create(idem=idem, **fields)
    except IntegrityError:
        prev = model.objects.filter(idem=idem).first() if idem else None
        if prev:
            return prev
        raise


# ---------------------------------------------------------------- recording
def _paid(paid, paid_cash, paid_uve, paid_mtn):
    """What the client paid, by how: cash, UVE, MTN float. `paid` alone (older callers) is cash."""
    parts = {"paid_cash": _amt(paid_cash, "paid_cash"), "paid_uve": _amt(paid_uve, "paid_uve"),
             "paid_mtn": _amt(paid_mtn, "paid_mtn")}
    if not any(parts.values()) and paid not in (None, ""):
        parts["paid_cash"] = _amt(paid, "paid_cash")
    parts["paid"] = sum(parts.values(), Z)
    return parts


def record_supply(user, *, client_kind, pos_sim="", shop=None, client_name="", client_phone="", agent_sim="",
                  uve=None, er=None, cash=None, paid=None, paid_cash=None, paid_uve=None, paid_mtn=None, note="",
                  idempotency_key=None):
    """A supply to a POS, a shop or another client, or a transfer to another commercial agent (client_kind AGENT).

    Stock: what is supplied leaves the agent's stock (cash, UVE or ER); what the client pays comes in, in the way he
    paid — a cash supply is usually paid back in UVE, a UVE / ER supply in cash or MTN float."""
    agent = agent_of(user)
    client = _client(agent, client_kind, pos_sim, shop, client_phone, agent_sim, client_name)
    vals = {k: _amt(v, k) for k, v in (("uve", uve), ("er", er), ("cash", cash))}
    vals.update(_paid(paid, paid_cash, paid_uve, paid_mtn))
    if client_kind == ClientKind.AGENT:  # a transfer between agents moves stock; nothing is owed through the portal
        vals.update(paid=Z, paid_cash=Z, paid_uve=Z, paid_mtn=Z)
    total = vals["uve"] + vals["er"] + vals["cash"]
    if total <= 0:
        raise BusinessError(_("Enter what you supplied in UVE, ER and / or cash."), "amount")
    if vals["paid"] > total:
        raise BusinessError(_("The client cannot pay more than what was supplied now (%(amt)s FCFA). Record the "
                              "rest as a payment from the client.") % {"amt": fmt(total)}, "paid_cash")
    return _create(E, idempotency_key, agent=agent, kind=E.Kind.SUPPLY, **client, **vals, note=(note or "").strip(),
                   business_date=douala_today(), recorded_by=user)


def record_client_payment(user, *, client_kind, pos_sim="", shop=None, client_name="", client_phone="", amount=None,
                          cash=None, uve=None, mtn=None, note="", idempotency_key=None):
    """A client pays the agent for earlier supplies: in cash, UVE and / or MTN float (`amount` alone = cash)."""
    agent = agent_of(user)
    if client_kind == ClientKind.AGENT:
        raise BusinessError(_("Choose who the client is."), "client_kind")
    client = _client(agent, client_kind, pos_sim, shop, client_phone, "", client_name)
    parts = _paid(amount, cash, uve, mtn)
    if parts["paid"] <= 0:
        raise BusinessError(_("Enter the amount the client paid you."), "cash")
    return _create(E, idempotency_key, agent=agent, kind=E.Kind.PAYMENT, **client, **parts,
                   note=(note or "").strip(), business_date=douala_today(), recorded_by=user)


EX = AgentExchange


def record_exchange(user, *, direction, amount, other_phone="", note="", idempotency_key=None):
    """Float exchange, 1:1: send float and receive UVE or cash, or receive float and send UVE."""
    agent = agent_of(user)
    if direction not in EX.Direction.values:
        raise BusinessError(_("Choose the exchange."), "direction")
    a = _amt(amount, "amount")
    if a <= 0:
        raise BusinessError(_("Amount must be greater than zero."), "amount")
    phone = _sim(other_phone, "other_phone") if (other_phone or "").strip() else ""
    if direction == EX.Direction.RECEIVE_FLOAT_UVE and not phone:
        raise BusinessError(_("Enter the number you send the UVE to."), "other_phone")
    return _create(EX, idempotency_key, agent=agent, direction=direction, amount=a, other_phone=phone,
                   note=(note or "").strip(), business_date=douala_today(), recorded_by=user)


def cancel_exchange(user, x, reason):
    agent = agent_of(user)
    if x.agent_id != agent.pk:
        raise PermissionDenied(_("This is not your record."))
    if x.is_cancelled:
        raise BusinessError(_("This record is already cancelled."))
    if x.business_date != douala_today():
        raise BusinessError(_("Only today's records can be cancelled. Record a correcting entry instead."))
    if not (reason or "").strip():
        raise BusinessError(_("Say why you cancel this record."), "reason")
    x.cancelled_at, x.cancel_reason = timezone.now(), reason.strip()
    x.save(update_fields=["cancelled_at", "cancel_reason"])
    return x


def exchange_code(x):
    """Sending UVE in an exchange dials the UVE code to the other party's number."""
    return _code("UVE", x.other_phone, x.amount) if x.gives == "UVE" else ""


def cancel_entry(user, entry, reason):
    """The agent cancels a mistaken entry of today (it then counts for nothing)."""
    agent = agent_of(user)
    if entry.agent_id != agent.pk:
        raise PermissionDenied(_("This is not your record."))
    if entry.is_cancelled:
        raise BusinessError(_("This record is already cancelled."))
    if entry.business_date != douala_today():
        raise BusinessError(_("Only today's records can be cancelled. Record a correcting entry instead."))
    if not (reason or "").strip():
        raise BusinessError(_("Say why you cancel this record."), "reason")
    entry.cancelled_at = timezone.now()
    entry.cancel_reason = reason.strip()
    entry.save(update_fields=["cancelled_at", "cancel_reason"])
    return entry


def request_payback(user, *, desk=None, shop=None, cash=None, uve=None, mtn=None, reference="", note="",
                    idempotency_key=None):
    """Pay the company back at a cashier of the zone, or at a shop."""
    agent = agent_of(user)
    if shop is not None:
        if not shop.active:
            raise BusinessError(_("Choose the shop."), "shop")
        desk = None
    elif desk is None or not desks_for(agent).filter(pk=desk.pk).exists():
        raise BusinessError(_("Choose a cashier desk of your zone."), "desk")
    vals = {k: _amt(v, k) for k, v in (("cash", cash), ("uve", uve), ("mtn", mtn))}
    if sum(vals.values(), Z) <= 0:
        raise BusinessError(_("Enter what you pay back in cash, UVE and / or MTN float."), "amount")
    if shop is not None and vals["mtn"]:
        raise BusinessError(_("A shop receives cash or UVE, not MTN float."), "method")
    return _create(P, idempotency_key, agent=agent, desk=desk, shop=shop, **vals,
                   reference=(reference or "").strip(), note=(note or "").strip(), recorded_by=user)


def cancel_payback(user, p):
    agent = agent_of(user)
    if p.agent_id != agent.pk:
        raise PermissionDenied(_("This is not your payback."))
    if p.status != P.Status.PENDING:
        raise BusinessError(_("Only a payback still waiting for the cashier can be withdrawn."))
    p.delete()


# ---------------------------------------------------------------- cashier side
def can_decide(user, p):
    if p.status != P.Status.PENDING:
        return False
    if p.shop_id:
        return (user.is_shop_manager and user.shop_id == p.shop_id) or user.is_shop_senior or user.is_superadmin
    return user.is_cashier and user.subzone_id == p.desk_id


def pending_for_desk(zone):
    if zone is None:
        return P.objects.none()
    return P.objects.filter(desk=zone, status=P.Status.PENDING).select_related("agent", "desk").order_by("created_at")


def pending_for_shop(shop):
    if shop is None:
        return P.objects.none()
    return P.objects.filter(shop=shop, status=P.Status.PENDING).select_related("agent", "shop").order_by("created_at")


def confirm_payback(user, p):
    """The cashier confirms receiving the payback: it is posted as the agent's payment to the company."""
    from . import operations as ops
    from . import shops as sh

    with transaction.atomic():
        p = P.objects.select_for_update().get(pk=p.pk)
        if not can_decide(user, p):
            raise PermissionDenied(_("Only %(who)s confirms this payback, while it is pending.")
                                   % {"who": p.receiver})
        desc = _("Agent payback %(code)s") % {"code": p.code}
        if p.reference:
            desc += f" · {p.reference}"
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"agent-payback-{p.pk}")
        if p.shop_id:
            txns = [sh.receive_agent_payback(user, shop=p.shop, agent=p.agent, cash=p.cash or None,
                                             uve=p.uve or None, description=desc, idempotency_key=key)]
        else:
            parts = {PaymentMethod.CASH: p.cash, PaymentMethod.UVE: p.uve, PaymentMethod.MTN: p.mtn}
            accounts = {}  # no default set for payments: the desk's account used to collect
            for m, amt in parts.items():
                if amt and ops._desk_account(ops.cashier_ctx(user), ops.METHOD_ACCOUNT_KIND[m], True) is None:
                    own = ops.perm.usable_accounts(user, [ops.METHOD_ACCOUNT_KIND[m]], receiving=True) \
                        .filter(shop__isnull=True)
                    acct = ops._by_use(own.filter(shared=False) or own, ops.COLLECT_USES).first()
                    if acct is not None:
                        accounts[m] = acct
            txns = ops.record_payment(user, payer=p.agent, parts=parts, accounts=accounts, description=desc,
                                      idempotency_key=key)
        p.status = P.Status.CONFIRMED
        p.decided_by, p.decided_at, p.transaction = user, timezone.now(), txns[0]
        p.save(update_fields=["status", "decided_by", "decided_at", "transaction"])
        return p


def reject_payback(user, p, reason):
    if not can_decide(user, p):
        raise PermissionDenied(_("Only %(who)s rejects this payback, while it is pending.") % {"who": p.receiver})
    if not (reason or "").strip():
        raise BusinessError(_("Say why the payback is rejected (e.g. nothing received)."), "reason")
    p.status, p.reject_reason = P.Status.REJECTED, reason.strip()
    p.decided_by, p.decided_at = user, timezone.now()
    p.save(update_fields=["status", "reject_reason", "decided_by", "decided_at"])
    return p


# ---------------------------------------------------------------- figures
def live_entries(agent):
    return E.objects.filter(agent=agent, cancelled_at__isnull=True)


def office_txns(agent):
    """Company transactions concerning the agent: supplies from the office, payments, destockages, transfers of
    responsibility, shop dealings."""
    return Transaction.objects.filter(entries__account__counterparty=agent, entries__account__kind="CUSTOMER") \
        .distinct() \
        .select_related("entered_by", "counterparty", "counterparty_2", "zone", "shop", "correction") \
        .order_by("-posted_at")


def received_by_product(agent):
    """Supplied to the agent by the office, per product (corrections excluded)."""
    rows = Transaction.objects.filter(type=TxType.SUPPLY, counterparty=agent, counterparty_2__isnull=True,
                                      correction__isnull=True).values("product").annotate(s=Sum("amount"))
    out = {p: Z for p in (Product.UVE, Product.ER, Product.CASH)}
    for r in rows:
        out[r["product"]] = r["s"] or Z
    return out


def supplied_by_product(agent, **filters):
    agg = live_entries(agent).filter(kind=E.Kind.SUPPLY, **filters).aggregate(u=Sum("uve"), e=Sum("er"), c=Sum("cash"))
    return {Product.UVE: agg["u"] or Z, Product.ER: agg["e"] or Z, Product.CASH: agg["c"] or Z}


def stock_estimate(agent):
    """In hand now, per product: [(label, in hand)]."""
    f = _flows(agent)
    return [(product_label(p), _net(f, p)) for p in PRODUCTS]


def clients_owing(agent):
    """What each client owes the agent: [(label, kind, owed, last entry)] for clients with a balance."""
    groups = OrderedDict()
    for e in live_entries(agent).exclude(client_kind=ClientKind.AGENT).select_related("pos", "shop") \
            .order_by("created_at"):
        g = groups.setdefault(e.client_key, {"label": e.client_label, "kind": e.get_client_kind_display(),
                                             "kind_code": e.client_kind, "owed": Z, "last": e, "pos": e.pos,
                                             "shop": e.shop, "phone": e.client_phone,
                                             "number": client_number(e)})
        g["owed"] += e.balance_effect
        g["last"] = e
    rows = [g for g in groups.values() if g["owed"] != 0]
    rows.sort(key=lambda g: -g["owed"])
    for g in rows:
        g["in_zone"] = g["pos"] is not None and in_zone(agent, g["pos"])
        g["last_supply"] = live_entries(agent).filter(kind=E.Kind.SUPPLY, **_client_filter(g)) \
            .order_by("-created_at").values_list("business_date", flat=True).first()
    return rows


def _client_filter(g):
    if g["kind_code"] == ClientKind.POS:
        return {"pos": g["pos"]}
    if g["kind_code"] == ClientKind.SHOP:
        return {"shop": g["shop"]}
    return {"client_kind": ClientKind.OTHER, "client_phone": g["phone"]}


def pos_owed(agent, pos):
    agg = live_entries(agent).filter(pos=pos).aggregate(t=Sum("uve"), e=Sum("er"), c=Sum("cash"), p=Sum("paid"))
    return (agg["t"] or Z) + (agg["e"] or Z) + (agg["c"] or Z) - (agg["p"] or Z)


def summary(agent):
    from .ledger import balance

    owed_company = balance(agent.account)
    pend = P.objects.filter(agent=agent, status=P.Status.PENDING)
    pend_total = sum((p.total for p in pend), Z)
    clients = clients_owing(agent)
    today = douala_today()
    return {
        "owed_company": owed_company,
        "pending": pend.count(), "pending_total": pend_total,
        "after_pending": owed_company - pend_total,
        "clients_owe": sum((g["owed"] for g in clients), Z),
        "clients": clients,
        "stock": stock_estimate(agent),
        "today_supplied": sum(supplied_by_product(agent, business_date=today).values(), Z),
        "zone": zone_of(agent),
    }


# ---------------------------------------------------------------- Orange dial codes
USSD = {"UVE": "#149*5*{number}*{amount}#", "ER": "#189*2*{number}*{amount}#"}


def _norm(raw):
    from core.simnumbers import SimNumberError, normalize_sim

    try:
        return normalize_sim(raw)
    except SimNumberError:
        return ""


def client_number(entry):
    """The Orange number the agent sends to: the POS business SIM, the shop SIM, the other agent's business SIM or
    the other client's phone."""
    if entry.pos_id:
        return _norm(entry.pos.business_sim)
    if entry.shop_id:
        return _norm(entry.shop.sim_number)
    if entry.to_agent_id:
        return _norm(entry.to_agent.business_sim)
    return _norm(entry.client_phone)


def _code(product, number, amount):
    if product not in USSD or not number or not amount:
        return ""
    return USSD[product].format(number=number, amount=int(amount))


def ussd_code(entry):
    """The code to dial for this supply or transfer (UVE or ER), or "" (cash, a client payment, no number)."""
    if entry.kind != E.Kind.SUPPLY:
        return ""
    return _code(entry.product, client_number(entry), entry.total)


def desk_master_sim(desk):
    """The master SIM (UVE) of a desk's master zone, where agents send UVE."""
    from core.models import CompanySim

    master = desk.parent if desk.parent_id else desk
    sim = CompanySim.objects.filter(master_zone=master, is_master=True).first()
    return sim.msisdn if sim else ""


def payback_number(p):
    return _norm(p.shop.sim_number) if p.shop_id else desk_master_sim(p.desk)


def payback_code(p):
    """A UVE payback is sent with the same code as a UVE supply, to the master SIM or the shop SIM."""
    return _code("UVE", payback_number(p), p.uve) if p.uve and not (p.cash or p.mtn) else ""


# ---------------------------------------------------------------- office supplies and daily reconciliation
OFFICE_TYPES = (TxType.SUPPLY, TxType.DESTOCKAGE, TxType.FLOAT_EXCHANGE)
PRODUCTS = ("UVE", "ER", "CASH", "MTN")
PRODUCT_LABELS = {"UVE": "UVE", "ER": "ER"}


def product_label(p):
    return PRODUCT_LABELS.get(p) or (_("Cash") if p == "CASH" else _("Float"))


def office_supplies(agent):
    """Supplies and destockages the office recorded on the agent (also those delivered straight to a POS)."""
    return Transaction.objects.filter(type__in=OFFICE_TYPES, counterparty=agent, correction__isnull=True) \
        .select_related("counterparty_2", "zone", "entered_by").order_by("-posted_at")


def _flows(agent, d_from=None, d_to=None):
    """Stock movements per product between two dates (inclusive; None = open-ended)."""
    def rng(qs, field):
        if d_from:
            qs = qs.filter(**{f"{field}__gte": d_from})
        if d_to:
            qs = qs.filter(**{f"{field}__lte": d_to})
        return qs

    f = {k: {p: Z for p in PRODUCTS} for k in ("office", "from_agents", "to_clients", "to_agents", "paid_back",
                                                 "collected", "exchanged")}
    for r in rng(Transaction.objects.filter(type__in=(TxType.SUPPLY, TxType.FLOAT_EXCHANGE), counterparty=agent,
                                            counterparty_2__isnull=True,
                                            correction__isnull=True), "business_date") \
            .values("product").annotate(s=Sum("amount")):
        f["office"][r["product"]] += r["s"] or Z
    mine = rng(live_entries(agent), "business_date")
    for key, qs in (("to_clients", mine.filter(kind=E.Kind.SUPPLY).exclude(client_kind=ClientKind.AGENT)),
                    ("to_agents", mine.filter(client_kind=ClientKind.AGENT)),
                    ("from_agents", rng(E.objects.filter(to_agent=agent, cancelled_at__isnull=True),
                                        "business_date"))):
        a = qs.aggregate(u=Sum("uve"), e=Sum("er"), c=Sum("cash"))
        f[key].update({"UVE": a["u"] or Z, "ER": a["e"] or Z, "CASH": a["c"] or Z})
    c = mine.exclude(client_kind=ClientKind.AGENT).aggregate(c=Sum("paid_cash"), u=Sum("paid_uve"),
                                                             m=Sum("paid_mtn"))
    f["collected"].update({"CASH": c["c"] or Z, "UVE": c["u"] or Z, "MTN": c["m"] or Z})
    pb = rng(P.objects.filter(agent=agent).exclude(status=P.Status.REJECTED), "created_at__date") \
        .aggregate(u=Sum("uve"), c=Sum("cash"), m=Sum("mtn"))
    f["paid_back"].update({"UVE": pb["u"] or Z, "CASH": pb["c"] or Z, "MTN": pb["m"] or Z})
    for x in rng(EX.objects.filter(agent=agent, cancelled_at__isnull=True), "business_date"):
        f["exchanged"][x.gives] -= x.amount
        f["exchanged"][x.receives] += x.amount
    return f


def _net(f, p):
    return (f["office"][p] + f["from_agents"][p] + f["collected"][p] + f["exchanged"][p]
            - f["to_clients"][p] - f["to_agents"][p] - f["paid_back"][p])


def daily_reconciliation(agent, day):
    """Automatic reconciliation of one day: stock and cash in hand, the account with the company, clients."""
    from datetime import timedelta

    from core.models import Entry

    before = _flows(agent, None, day - timedelta(days=1))
    today = _flows(agent, day, day)
    stock = []
    for p in PRODUCTS:
        opening = _net(before, p)
        stock.append({"product": product_label(p), "code": p, "opening": opening, "exchanged": today["exchanged"][p],
                      "office": today["office"][p], "from_agents": today["from_agents"][p],
                      "collected": today["collected"][p], "to_clients": today["to_clients"][p],
                      "to_agents": today["to_agents"][p], "paid_back": today["paid_back"][p],
                      "closing": opening + _net(today, p)})
    acct = Entry.objects.filter(account=agent.account)
    opening_owed = acct.filter(business_date__lt=day).aggregate(s=Sum("amount"))["s"] or Z
    day_e = acct.filter(business_date=day)
    taken = day_e.filter(amount__gt=0).aggregate(s=Sum("amount"))["s"] or Z
    paid = -(day_e.filter(amount__lt=0).aggregate(s=Sum("amount"))["s"] or Z)
    cl = live_entries(agent).exclude(client_kind=ClientKind.AGENT)

    def owed(qs):
        a = qs.aggregate(u=Sum("uve"), e=Sum("er"), c=Sum("cash"), p=Sum("paid"))
        return (a["u"] or Z) + (a["e"] or Z) + (a["c"] or Z) - (a["p"] or Z)

    c_open = owed(cl.filter(business_date__lt=day))
    c_day = cl.filter(business_date=day)
    supplied = sum((e.total for e in c_day), Z)
    received = sum((e.paid for e in c_day), Z)
    return {
        "day": day, "stock": stock,
        "company": {"opening": opening_owed, "taken": taken, "paid": paid, "closing": opening_owed + taken - paid},
        "clients": {"opening": c_open, "supplied": supplied, "received": received,
                    "closing": c_open + supplied - received},
        "pending": P.objects.filter(agent=agent, status=P.Status.PENDING).count(),
    }


def daily_summary(agent, d_from, d_to):
    """One line per day with activity: office, supplied, transfers, collected, paid back."""
    from datetime import timedelta

    rows, d = [], d_to
    while d >= d_from:
        f = _flows(agent, d, d)
        line = {"day": d,
                "office": sum(f["office"].values(), Z),
                "to_clients": sum(f["to_clients"].values(), Z),
                "agents_in": sum(f["from_agents"].values(), Z), "agents_out": sum(f["to_agents"].values(), Z),
                "collected": sum(f["collected"].values(), Z),
                "exchanged": sum((x.amount for x in EX.objects.filter(agent=agent, business_date=d,
                                                                       cancelled_at__isnull=True)), Z),
                "paid_back": sum(f["paid_back"].values(), Z)}
        if any(v for k, v in line.items() if k != "day"):
            rows.append(line)
        d -= timedelta(days=1)
    return rows
