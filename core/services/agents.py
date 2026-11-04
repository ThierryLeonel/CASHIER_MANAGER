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
    AgentClientEntry, AgentPayback, ClientKind, Counterparty, PaymentMethod, Product, Transaction, TxType, Zone,
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


def _client(agent, client_kind, pos_sim, shop, client_name, client_phone):
    out = dict(client_kind=client_kind, pos=None, shop=None, client_name="", client_phone=(client_phone or "").strip())
    if client_kind == ClientKind.POS:
        out["pos"] = find_pos(pos_sim)
    elif client_kind == ClientKind.SHOP:
        if shop is None or not shop.active:
            raise BusinessError(_("Choose the shop."), "shop")
        out["shop"] = shop
    elif client_kind == ClientKind.OTHER:
        name = (client_name or "").strip()
        if not name:
            raise BusinessError(_("Enter the client's name."), "client_name")
        out["client_name"] = name
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
def record_supply(user, *, client_kind, pos_sim="", shop=None, client_name="", client_phone="", uve=None, er=None,
                  cash=None, paid=None, note="", idempotency_key=None):
    agent = agent_of(user)
    client = _client(agent, client_kind, pos_sim, shop, client_name, client_phone)
    vals = {k: _amt(v, k) for k, v in (("uve", uve), ("er", er), ("cash", cash), ("paid", paid))}
    total = vals["uve"] + vals["er"] + vals["cash"]
    if total <= 0:
        raise BusinessError(_("Enter what you supplied in UVE, ER and / or cash."), "uve")
    if vals["paid"] > total:
        raise BusinessError(_("The client cannot pay more than what was supplied now (%(amt)s FCFA). Record the "
                              "rest as a payment from the client.") % {"amt": fmt(total)}, "paid")
    return _create(E, idempotency_key, agent=agent, kind=E.Kind.SUPPLY, **client, **vals, note=(note or "").strip(),
                   business_date=douala_today(), recorded_by=user)


def record_client_payment(user, *, client_kind, pos_sim="", shop=None, client_name="", client_phone="", amount=None,
                          note="", idempotency_key=None):
    agent = agent_of(user)
    client = _client(agent, client_kind, pos_sim, shop, client_name, client_phone)
    amount = _amt(amount, "amount")
    if amount <= 0:
        raise BusinessError(_("Enter the amount the client paid you."), "amount")
    return _create(E, idempotency_key, agent=agent, kind=E.Kind.PAYMENT, **client, paid=amount,
                   note=(note or "").strip(), business_date=douala_today(), recorded_by=user)


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


def request_payback(user, *, desk, cash=None, uve=None, mtn=None, reference="", note="", idempotency_key=None):
    agent = agent_of(user)
    if desk is None or not desks_for(agent).filter(pk=desk.pk).exists():
        raise BusinessError(_("Choose a cashier desk of your zone."), "desk")
    vals = {k: _amt(v, k) for k, v in (("cash", cash), ("uve", uve), ("mtn", mtn))}
    if sum(vals.values(), Z) <= 0:
        raise BusinessError(_("Enter what you pay back in cash, UVE and / or MTN float."), "cash")
    return _create(P, idempotency_key, agent=agent, desk=desk, **vals, reference=(reference or "").strip(),
                   note=(note or "").strip(), recorded_by=user)


def cancel_payback(user, p):
    agent = agent_of(user)
    if p.agent_id != agent.pk:
        raise PermissionDenied(_("This is not your payback."))
    if p.status != P.Status.PENDING:
        raise BusinessError(_("Only a payback still waiting for the cashier can be withdrawn."))
    p.delete()


# ---------------------------------------------------------------- cashier side
def can_decide(user, p):
    return p.status == P.Status.PENDING and user.is_cashier and user.subzone_id == p.desk_id


def pending_for_desk(zone):
    if zone is None:
        return P.objects.none()
    return P.objects.filter(desk=zone, status=P.Status.PENDING).select_related("agent", "desk").order_by("created_at")


def confirm_payback(user, p):
    """The cashier confirms receiving the payback: it is posted as the agent's payment to the company."""
    from . import operations as ops

    with transaction.atomic():
        p = P.objects.select_for_update().get(pk=p.pk)
        if not can_decide(user, p):
            raise PermissionDenied(_("Only a cashier of %(desk)s confirms this payback, while it is pending.")
                                   % {"desk": p.desk})
        parts = {PaymentMethod.CASH: p.cash, PaymentMethod.UVE: p.uve, PaymentMethod.MTN: p.mtn}
        desc = _("Agent payback %(code)s") % {"code": p.code}
        if p.reference:
            desc += f" · {p.reference}"
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"agent-payback-{p.pk}")
        txns = ops.record_payment(user, payer=p.agent, parts=parts, description=desc, idempotency_key=key)
        p.status = P.Status.CONFIRMED
        p.decided_by, p.decided_at, p.transaction = user, timezone.now(), txns[0]
        p.save(update_fields=["status", "decided_by", "decided_at", "transaction"])
        return p


def reject_payback(user, p, reason):
    if not can_decide(user, p):
        raise PermissionDenied(_("Only a cashier of %(desk)s rejects this payback, while it is pending.")
                               % {"desk": p.desk})
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
    return Transaction.objects.filter(entries__account__counterparty=agent).distinct() \
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
    rec, out = received_by_product(agent), supplied_by_product(agent)
    return [(p.label, rec[p], out[p], rec[p] - out[p]) for p in (Product.UVE, Product.ER, Product.CASH)]


def clients_owing(agent):
    """What each client owes the agent: [(label, kind, owed, last entry)] for clients with a balance."""
    groups = OrderedDict()
    for e in live_entries(agent).select_related("pos", "shop").order_by("created_at"):
        g = groups.setdefault(e.client_key, {"label": e.client_label, "kind": e.get_client_kind_display(),
                                             "owed": Z, "last": e, "pos": e.pos, "phone": e.client_phone})
        g["owed"] += e.balance_effect
        g["last"] = e
    rows = [g for g in groups.values() if g["owed"] != 0]
    rows.sort(key=lambda g: -g["owed"])
    for g in rows:
        g["in_zone"] = g["pos"] is not None and in_zone(agent, g["pos"])
    return rows


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
