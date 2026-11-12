"""Report definitions. Amounts in transaction listings are signed: a
correction reversal appears as a negative line on the business day it was
posted, so closed periods stay as they were and later periods carry the
correction."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum, Value
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core.models import (
    Account, CashManager, CashManagerExchange, ClosingLine, ConversionRate, Correction, Counterparty, Discrepancy,
    Entry, ExpenseCategory, PaymentMethod, Transaction, TxType,
)
from core.services import operations as ops

from .engine import Col, Report, Table, drop_empty_columns, group_rows

Z = Decimal("0")
K = Account.Kind


def money(v):
    return v if v is not None else Z


def _desk_user(t):
    """The recording cashier; for entries posted by finance / managers into a desk's day (capital, float exchange),
    the person who recorded them."""
    day_cashier = t.cashier_day.cashier if t.cashier_day_id else None
    return t.cashier or day_cashier or t.entered_by


def station(t):
    return str(_desk_user(t))


def station_id(t):
    u = _desk_user(t)
    return u.staff_code or str(u.pk)


def signed(t):
    return -t.amount if t.type == TxType.REVERSAL else t.amount


def effective_type(t):
    return t.reverses.type if t.type == TxType.REVERSAL else t.type


def describe(t):
    T = TxType
    et = effective_type(t)
    parts = []
    if t.type == T.REVERSAL:
        parts.append(_("Reversal of %(ref)s (%(date)s)") % {"ref": t.reverses.reference,
                                                           "date": t.original_business_date})
    if t.replaces_id:
        parts.append(_("Replaces %(ref)s") % {"ref": t.replaces.reference})
    if et == T.SUPPLY:
        parts.append(f"{t.product}")
        if t.counterparty_2_id:
            parts.append(_("received by POS %(pos)s") % {"pos": t.counterparty_2})
    elif et == T.DESTOCKAGE:
        parts.append(_("cash to POS %(pos)s") % {"pos": t.counterparty_2})
    elif et == T.COLLECTION:
        parts.append(PaymentMethod(t.payment_method).label if t.payment_method else "")
        if t.bank_reference:
            parts.append(f"ref {t.bank_reference}")
    elif et == T.RESPONSIBILITY_TRANSFER:
        parts.append(_("from %(a)s to %(b)s") % {"a": t.counterparty_2, "b": t.counterparty})
        if t.related_supply_id:
            parts.append(_("for %(ref)s") % {"ref": t.related_supply.reference})
    elif et == T.EXPENSE and t.expense_category_id:
        parts.append(str(t.expense_category))
    elif et == T.CONVERSION:
        parts.append(_("rate +%(r)s%% → ER %(er)s") % {"r": (t.rate_used * 100).normalize(), "er": t.amount_received})
    if t.source_account_id:
        parts.append(_("from %(acct)s") % {"acct": t.source_account})
    if t.dest_account_id:
        parts.append(_("into %(acct)s") % {"acct": t.dest_account})
    if t.description:
        parts.append(t.description)
    if hasattr(t, "correction"):
        parts.append(_("CORRECTED: %(reason)s") % {"reason": t.correction.reason})
    return " · ".join(str(p) for p in parts if p)


TX_RELATED = ["cashier", "zone", "entered_by", "cashier_day__zone", "counterparty", "counterparty_2", "source_account__sim",
              "dest_account__sim", "reverses", "replaces", "expense_category", "related_supply", "correction",
              "cash_manager", "exchange"]

TX_COLUMNS = [
    Col("posted_at", _l("Recorded at"), "datetime"), Col("business_date", _l("Business date"), "date"),
    Col("reference", _l("Reference")), Col("type", _l("Type")), Col("customer", _l("Agent / POS")),
    Col("details", _l("Details")), Col("cashier", _l("Cashier")), Col("cashier_id", _l("Cashier ID")),
    Col("amount", _l("Amount"), "money", True),
]


def txn_row(t):
    return dict(posted_at=t.posted_at, business_date=t.business_date, reference=t.reference,
                receipt=t.receipt_no or "", type=str(t.display_type),
                customer=str(t.counterparty) if t.counterparty_id else "", details=describe(t), cashier=station(t),
                cashier_id=station_id(t),
                amount=signed(t), corrected=hasattr(t, "correction"), _id=t.id)


# detail lists grouped by the cashier's desk: the desk heads its lines, so the cashier columns are not repeated
DETAIL_COLUMNS = [c for c in TX_COLUMNS if c.key not in ("cashier", "cashier_id")]


def _desk_of(t):
    desk, who, ident = _desk_label(_desk_user(t)), station(t), station_id(t)
    name = desk if not who or who == desk else f"{desk} · {who}"
    return f"{name} ({ident})" if ident and ident not in name else name


def detail_table(title, items, cols=None, row=txn_row, note=""):
    """Transactions as a pivot: a heading per cashier's desk, its lines, the desk's subtotal, the grand total."""
    cols = cols or DETAIL_COLUMNS
    rows = []
    for t in items:
        r = row(t)
        r["_g"] = _desk_of(t)
        rows.append(r)
    return Table(title, cols, group_rows(rows, cols, key=lambda r: r["_g"], sub_label=lambda g: g.split(" · ")[0]),
                 note=note)


def txns(f, types, extra_q=Q()):
    """Transactions of `types`, plus reversals of those types, in scope."""
    q = Q(type__in=types) | Q(type=TxType.REVERSAL, reverses__type__in=types)
    base = Q(f.date_q()) & f.station_q() & extra_q
    if f.method:
        base &= Q(payment_method=f.method)
    if f.customer:
        base &= Q(counterparty=f.customer) | Q(counterparty_2=f.customer)
    return Transaction.objects.filter(q & base).select_related(*TX_RELATED).order_by("business_date", "posted_at",
                                                                                      "id")


def scoped_asset_accounts(f, kinds=Account.ASSET_KINDS):
    """Company accounts in the filter scope. Accounts belong to zones; a
    master-level account (master SIM) counts in its master zone and in the
    subzone of its cashier in charge."""
    from core.services import permissions as perm

    # the cashiers' side only: shops have their own space (overview, daily reconciliation)
    qs = Account.objects.filter(kind__in=kinds, shop__isnull=True).select_related("sim", "custodian", "bank",
                                                                                 "zone__parent")
    if f.cashier:
        qs = qs.filter(perm.zone_accounts_q(f.cashier))
    if f.subzone:
        qs = qs.filter(perm.zone_closing_accounts_q(f.subzone))
    if f.master_zone:
        qs = qs.filter(Q(zone=f.master_zone) | Q(zone__parent=f.master_zone))
    return qs.distinct().order_by("kind", "id")


def balances_by_account(accounts, f):
    """{account_id: (opening, debit_in_period, credit_in_period, closing, current)}"""
    ids = [a.id for a in accounts]
    out = {}
    agg = Entry.objects.filter(account_id__in=ids).values("account_id").annotate(
        cur=Sum("amount"),
        opening=Sum("amount", filter=Q(business_date__lt=f.date_from)) if f.date_from else Value(Z),
        dr=Sum("amount", filter=f.date_q() & Q(amount__gt=0)),
        cr=Sum("amount", filter=f.date_q() & Q(amount__lt=0)),
    )
    for r in agg:
        opening = money(r["opening"])
        dr, cr = money(r["dr"]), money(r["cr"])
        out[r["account_id"]] = (opening, dr, -cr, opening + dr + cr, money(r["cur"]))
    return {i: out.get(i, (Z, Z, Z, Z, Z)) for i in ids}


# ------------------------------------------------------------------ reports
def statement(f):
    rep = Report("statement", _("Agent / POS account statement"), f.text())
    cp = f.customer
    if cp is None:
        rep.tables.append(Table(_("Select an agent or POS business SIM to see the statement."), []))
        return rep
    acct = cp.account
    entries = Entry.objects.filter(account=acct).filter(f.date_q()).select_related(
        *[f"transaction__{r}" for r in TX_RELATED]).order_by("business_date", "transaction__posted_at", "id")
    opening = money(Entry.objects.filter(account=acct, business_date__lt=f.date_from).aggregate(
        s=Sum("amount"))["s"]) if f.date_from else Z
    rows, running = [], opening
    by_product = defaultdict(Decimal)
    by_method = defaultdict(Decimal)
    for e in entries:
        t = e.transaction
        running += e.amount
        et = effective_type(t)
        sign = -1 if t.type == TxType.REVERSAL else 1
        if et == TxType.SUPPLY:
            by_product[t.product] += sign * t.amount
        elif et == TxType.DESTOCKAGE:
            by_product["DESTOCKAGE"] += sign * t.amount
        elif et == TxType.FLOAT_EXCHANGE:  # float converted and sent to the agent: a float supply
            by_product["FLOAT"] += sign * t.amount
        elif et == TxType.COLLECTION:
            by_method[t.payment_method] += sign * t.amount
        rows.append(dict(posted_at=t.posted_at, business_date=e.business_date, reference=t.reference,
                         receipt=t.receipt_no or "", type=str(t.display_type), details=describe(t),
                         cashier=station(t), cashier_id=station_id(t), debit=e.amount if e.amount > 0 else Z,
                         credit=-e.amount if e.amount < 0 else Z, balance=running,
                         corrected=hasattr(t, "correction"), _id=t.id))
    current = ops.customer_balance(cp)
    rep.summary = [
        (_("Agent / POS"), f"{cp} ({cp.get_kind_display()})", "text"),
        (_("Opening balance"), opening, "money"),
        (_("Closing balance (end of period)"), running, "money"),
        (_("Current balance (now)"), current, "money"),
        (_("Supplied UVE"), by_product["UVE"], "money"), (_("Supplied ER"), by_product["ER"], "money"),
        (_("Supplied cash"), by_product["CASH"], "money"), (_("Float supply"), by_product["FLOAT"], "money"),
        (_("Destockage"), by_product["DESTOCKAGE"], "money"),
    ] + [(_("Paid by %(m)s") % {"m": PaymentMethod(m).label}, v, "money") for m, v in sorted(by_method.items())]
    piv = [{"label": _("Supplied"), "_head": True}]
    for k, lbl in [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash")), ("FLOAT", _("Float supply")),
                   ("DESTOCKAGE", _("Destockage"))]:
        if by_product[k]:
            piv.append({"label": lbl, "amount": by_product[k]})
    piv.append({"label": _("Total supplied"), "_sub": True, "amount": sum(by_product.values(), Z)})
    piv.append({"label": _("Paid"), "_head": True})
    for m, v in sorted(by_method.items()):
        piv.append({"label": PaymentMethod(m).label, "amount": v})
    piv.append({"label": _("Total paid"), "_sub": True, "amount": sum(by_method.values(), Z)})
    rep.tables.append(Table(_("Supplied and paid in the period"),
                            [Col("label", _("Product / method")), Col("amount", _("Amount"), "money")], piv))
    mcols = [
        Col("posted_at", _("Recorded at"), "datetime"),
        Col("reference", _("Reference")), Col("receipt", _("Receipt")), Col("type", _("Type")),
        Col("details", _("Details")), Col("cashier", _("Cashier")),
        Col("debit", _("Supplied / owed +"), "money", True), Col("credit", _("Paid / credited −"), "money", True),
        Col("balance", _("Balance"), "money"),
    ]
    rep.tables.append(Table(_("Movements by day (positive = owed to company, negative balance = supply due to the agent / POS)"),
                            mcols, group_rows(rows, mcols, key=lambda r: r["business_date"],
                                              label=lambda d: d.strftime("%d/%m/%Y"), sort=lambda d: d)))
    return rep


# what adds to an agent's / POS's debt, and what pays it (cashiers, finance's float sent to agents, shops)
SUPPLIED_TYPES = [TxType.SUPPLY, TxType.DESTOCKAGE, TxType.FLOAT_EXCHANGE, TxType.SHOP_POS_UVE_OUT]
PAID_TYPES = [TxType.COLLECTION, TxType.SHOP_AGENT_PAYBACK, TxType.SHOP_POS_UVE_IN]


def _customer_rows(f, kind=None, only=None):
    cps = Counterparty.objects.select_related("microzone__subzone", "microzone__agent").order_by("kind", "business_sim")
    if kind:
        cps = cps.filter(kind=kind)
    if f.customer:
        cps = cps.filter(pk=f.customer.pk)
    by_cp = {c.pk: c for c in cps}
    accts = {aid: by_cp[cid] for aid, cid in Account.objects.filter(
        kind=Account.Kind.CUSTOMER, counterparty_id__in=by_cp.keys()).values_list("id", "counterparty_id")}
    tq = f.date_q()
    agg = Entry.objects.filter(account_id__in=accts.keys()).values("account_id").annotate(
        cur=Sum("amount"),
        opening=Sum("amount", filter=Q(business_date__lt=f.date_from)) if f.date_from else Value(Z),
        upto=Sum("amount", filter=Q(business_date__lte=f.date_to)) if f.date_to else Sum("amount"),
        supplied=Sum("amount", filter=tq & Q(transaction__type__in=SUPPLIED_TYPES)),
        paid=Sum("amount", filter=tq & Q(transaction__type__in=PAID_TYPES)),
        transfers=Sum("amount", filter=tq & Q(transaction__type=TxType.RESPONSIBILITY_TRANSFER)),
        other=Sum("amount", filter=tq & Q(transaction__type__in=[TxType.REVERSAL, TxType.OPENING_BALANCE])),
    )
    data = {r["account_id"]: r for r in agg}
    rows = []
    for aid, cp in accts.items():
        r = data.get(aid, {})
        closing = money(r.get("upto"))
        mz = cp.microzone
        row = dict(_cp=cp, customer=f"{cp.name} · {cp.sim_display}" + (" (POS)" if cp.kind == "POS" else ""),
                   sim=cp.sim_display, name=cp.name, kind=cp.get_kind_display(),
                   microzone=f"{mz.subzone.name} › {mz.name}" if mz else "",
                   agent=(mz.agent.name if mz and mz.agent_id else ""), opening=money(r.get("opening")),
                   supplied=money(r.get("supplied")), paid=-money(r.get("paid")),
                   transfers=money(r.get("transfers")), other=money(r.get("other")),
                   closing=closing, owed=max(closing, Z), due=max(-closing, Z), current=money(r.get("cur")))
        if only == "due" and closing >= 0:
            continue
        if only == "nonzero" and not any(row[k] for k in ("opening", "supplied", "paid", "closing", "current")):
            continue
        rows.append(row)
    return rows


CUSTOMER_COLS = [
    Col("sim", _l("Business SIM")), Col("name", _l("Name")), Col("kind", _l("Type")),
    Col("microzone", _l("Microzone")), Col("agent", _l("Agent in charge")),
    Col("opening", _l("Opening"), "money", True), Col("supplied", _l("Supplied"), "money", True),
    Col("paid", _l("Paid"), "money", True), Col("transfers", _l("Resp. transfers"), "money", True),
    Col("other", _l("Corrections / opening"), "money", True), Col("closing", _l("Closing"), "money", True),
    Col("owed", _l("Owed to company"), "money", True), Col("due", _l("Supply due to the agent / POS"), "money", True),
    Col("current", _l("Current"), "money", True),
]


PIVOT_CUSTOMER_COLS = [
    Col("customer", _l("Agent / POS")), Col("microzones", _l("Microzones")),
    Col("opening", _l("Opening"), "money", True), Col("supplied", _l("Supplied"), "money", True),
    Col("paid", _l("Paid"), "money", True), Col("adjusted", _l("Transfers / corrections"), "money", True),
    Col("closing", _l("Closing (+ owes, − credit)"), "money", True),
    Col("current", _l("Balance now (+ owes, − credit)"), "money", True),
]


def _customer_pivot(rep, title, rows, cols=PIVOT_CUSTOMER_COLS):
    for r in rows:
        r["adjusted"] = (r.get("transfers") or Z) + (r.get("other") or Z)
    t = group_by_subzone(title, rows, cols)
    rep.tables.append(t)
    tot = t.totals() or {}
    rep.summary = [(c.label, tot.get(c.key, Z), "money") for c in cols if c.total]
    return t


def customers(f):
    rep = Report("customers", _("Commercial agent and POS balances"), f.text())
    _customer_pivot(rep, _("Balances by subzone"), _customer_rows(f, only="nonzero"))
    return rep


def pos(f):
    rep = Report("pos", _("Exceptional direct POS transactions and outstanding amounts"), f.text())
    _customer_pivot(rep, _("POS balances by subzone"), _customer_rows(f, kind=Counterparty.Kind.POS, only="nonzero"))
    pos_ids = Counterparty.objects.filter(kind=Counterparty.Kind.POS).values("id")
    qs = Transaction.objects.filter(f.txn_q()).filter(Q(counterparty__in=pos_ids) | Q(counterparty_2__in=pos_ids)) \
        .select_related(*TX_RELATED).order_by("business_date", "posted_at")
    rep.tables.append(detail_table(_("Transactions involving POS"), qs))
    return rep


def supply_due(f):
    rep = Report("supply_due", _("Supply due to agents and POS (prepaid / overpaid balances)"), f.text())
    rows = _customer_rows(f, only="due")
    _customer_pivot(rep, _("Agents and POS with supply due (as of end of period)"), rows, [
        Col("customer", _l("Agent / POS")), Col("microzones", _l("Microzones")),
        Col("supplied", _l("Supplied"), "money", True), Col("paid", _l("Paid"), "money", True),
        Col("due", _l("Supply due to the agent / POS"), "money", True),
        Col("current", _l("Balance now (+ owes, − credit)"), "money", True)])
    return rep


# ------------------------------------------------------------------ pivot layout shared by the reports
def _txn_place(t):
    """(master zone, desk) of a transaction: the master zone of its subzone, and the recording cashier's desk (or the
    finance / manager who recorded it)."""
    z = t.zone
    master = (z.parent.name if z.parent_id else z.name) if z else str(_("Company"))
    return master, _desk_label(_desk_user(t))


def zone_pivot(title, items, cats, *, label=None, total=True, count=False, note=""):
    """A grouped table: a heading per master zone, one line per cashier's desk, the zone's subtotal, the grand total.
    `items` = [(master, desk, {cat: value})]; `cats` = [(key, label)]."""
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    for master, desk, vals in items:
        line = data[master][desk]
        for k, v in vals.items():
            line[k] += v
        line["_n"] += 1
    keys = [k for k, _l2 in cats]
    rows = []
    for master in sorted(data):
        rows.append({"label": master, "_head": True})
        sub = defaultdict(Decimal)
        for desk in sorted(data[master]):
            v = data[master][desk]
            r = {"label": desk, **{k: v[k] for k in keys}}
            if total:
                r["total"] = sum((v[k] for k in keys), Z)
            if count:
                r["count"] = int(v["_n"])
            rows.append(r)
            for k in keys + ["_n"]:
                sub[k] += v[k]
        r = {"label": _("Total %(sub)s") % {"sub": master}, "_sub": True, **{k: sub[k] for k in keys}}
        if total:
            r["total"] = sum((sub[k] for k in keys), Z)
        if count:
            r["count"] = int(sub["_n"])
        rows.append(r)
    cols = [Col("label", label or _("Master zone / cashier"))]
    if count:
        cols.append(Col("count", _("Count"), "int", True))
    cols += [Col(k, lbl, "money", True) for k, lbl in cats]
    if total:
        cols.append(Col("total", _("Total"), "money", True))
    return Table(title, cols, rows, note=note)


def pivot_summary(table, cats, total=True):
    """Summary tiles from a pivot table's grand totals (one per column, then the total)."""
    tot = table.totals() or {}
    out = [(lbl, tot.get(k, Z), "money") for k, lbl in cats]
    if total:
        out.append((_("Total"), tot.get("total", Z), "money"))
    return out


def group_by_subzone(title, rows, cols, note=""):
    """Agents / POS rows (each with `_cp`) grouped by subzone: heading, members, subtotal; grand total at the end.
    An agent running several microzones is listed once with all of them."""
    groups = defaultdict(list)
    for r in rows:
        sub, mzs = _agent_place(r["_cp"])
        r["microzones"] = mzs
        groups[sub].append(r)
    money = [c.key for c in cols if c.total]
    out = []
    for sub in sorted(groups):
        out.append({"customer": sub, "_head": True})
        tot = defaultdict(Decimal)
        for r in sorted(groups[sub], key=lambda x: (x["_cp"].kind != "AGENT", x["_cp"].name.lower())):
            out.append(r)
            for k in money:
                tot[k] += r.get(k) or Z
        out.append({"customer": _("Total %(sub)s") % {"sub": sub}, "_sub": True, **tot})
    return Table(title, cols, out, note=note)


def _summary_table(title, label, groups):
    rows = [dict(group=k, count=v[0], amount=v[1]) for k, v in groups.items()]
    return Table(title, [Col("group", label), Col("count", _("Count"), "int", True),
                         Col("amount", _("Net amount"), "money", True)], rows)


def supply_txns(f):
    """Supplies, destockages and float converted and sent to a commercial agent (a float supply), with their
    reversals, in scope."""
    def keep(t):
        b = t.reverses if t.type == TxType.REVERSAL else t
        return b.type != TxType.FLOAT_EXCHANGE or bool(b.counterparty_id)
    out = [t for t in txns(f, [TxType.SUPPLY, TxType.DESTOCKAGE, TxType.FLOAT_EXCHANGE]) if keep(t)]
    if f.cashier:  # finance records float supplies: they belong to the desk whose float account the float left
        from core.services import permissions as perm

        held = Account.objects.filter(perm.zone_accounts_q(f.cashier), kind=K.MTN).values("id")
        seen = {t.pk for t in out}
        extra = Transaction.objects.filter(
            Q(type=TxType.FLOAT_EXCHANGE, counterparty__isnull=False, source_account_id__in=held)
            | Q(type=TxType.REVERSAL, reverses__type=TxType.FLOAT_EXCHANGE, reverses__counterparty__isnull=False,
                reverses__source_account_id__in=held)).filter(f.date_q()).select_related(*TX_RELATED)
        if f.customer:
            extra = extra.filter(Q(counterparty=f.customer) | Q(reverses__counterparty=f.customer))
        out += [t for t in extra if t.pk not in seen]
        out.sort(key=lambda t: (t.business_date, t.posted_at, t.pk))
    return out


def supply_kind(t):
    """UVE / ER / CASH for a supply, DESTOCK for a destockage, FLOAT for float sent to an agent."""
    b = t.reverses if t.type == TxType.REVERSAL else t
    if b.type == TxType.DESTOCKAGE:
        return "DESTOCK"
    if b.type == TxType.FLOAT_EXCHANGE:
        return "FLOAT"
    return b.product


def supplies(f):
    rep = Report("supplies", _("Supplies: UVE, ER, cash, float (and destockage)"), f.text())
    qs = supply_txns(f)
    cats = [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash")), ("FLOAT", _("Float (converted to UVE)")),
            ("DESTOCK", _("Destockage"))]
    piv = zone_pivot(_("By zone and cashier"), [(*_txn_place(t), {supply_kind(t): signed(t)}) for t in qs], cats)
    rep.tables.append(piv)
    rep.summary = pivot_summary(piv, cats)
    ch_rows = []
    for ch, label in [("AGENT", _("Commercial agents")), ("POS", _("Points of sale (POS)"))]:
        row = {"label": label, **{k: Z for k, _l2 in cats}}
        for t in qs:
            b = _base(t)
            is_pos = b.type == TxType.SUPPLY and t.supply_channel == "POS"
            if (ch == "POS") == is_pos:
                row[supply_kind(t)] += signed(t)
        row["total"] = sum((row[k] for k, _l2 in cats), Z)
        ch_rows.append(row)
    rep.tables.append(Table(_("Agents versus POS"), [Col("label", _("Supplied to"))]
                            + [Col(k, lbl, "money", True) for k, lbl in cats]
                            + [Col("total", _("Total"), "money", True)], ch_rows))
    rep.tables.append(detail_table(_("Detail by cashier"), qs))
    return rep


def collections(f):
    rep = Report("collections", _("Collections by payment method"), f.text())
    qs = list(txns(f, [TxType.COLLECTION]))
    cats = [(m.value, m.label) for m in PaymentMethod]
    piv = zone_pivot(_("By zone and cashier"),
                     [(*_txn_place(t), {_base(t).payment_method: signed(t)}) for t in qs], cats)
    rep.tables.append(piv)
    rep.summary = pivot_summary(piv, cats)
    rep.tables.append(detail_table(_("Detail by cashier"), qs))
    return rep


def destockage(f):
    rep = Report("destockage", _("Destockage"), f.text())
    qs = list(txns(f, [TxType.DESTOCKAGE]))
    def row(t):
        r = txn_row(t)
        r["pos"] = str(t.counterparty_2)
        return r
    cats = [("amount", _("Destockage"))]
    piv = zone_pivot(_("By zone and cashier"), [(*_txn_place(t), {"amount": signed(t)}) for t in qs], cats,
                     total=False, count=True)
    rep.tables.append(piv)
    rep.summary = pivot_summary(piv, cats, total=False)
    cols = DETAIL_COLUMNS[:4] + [Col("pos", _("POS (received cash)")), Col("customer", _("Agent (owes)"))] \
        + DETAIL_COLUMNS[5:]
    rep.tables.append(detail_table(_("Destockage operations (cash out of drawer → agent debt; company UVE unchanged)"),
                                   qs, cols, row))
    return rep


def expenses(f):
    rep = Report("expenses", _("Expenses by category"), f.text())
    qs = list(txns(f, [TxType.EXPENSE]))
    used = {_base(t).expense_category_id for t in qs}
    cats = [(f"c{c.id}", str(c)) for c in ExpenseCategory.objects.filter(id__in=used).order_by("code")]
    piv = zone_pivot(_("By zone and cashier"),
                     [(*_txn_place(t), {f"c{_base(t).expense_category_id}": signed(t)}) for t in qs], cats)
    rep.tables.append(piv)
    rep.summary = pivot_summary(piv, cats)
    rep.tables.append(detail_table(_("Detail by cashier"), qs))
    return rep


TRANSFER_TYPES = (TxType.INTERNAL_TRANSFER, TxType.DESK_TO_SHOP, TxType.SHOP_TO_DESK, TxType.SHOP_TO_SHOP,
                  TxType.CAPITAL_MOVE)


def _party(a):
    """Who holds an account: a shop, a cashier desk (subzone; a master SIM counts for its cashier in charge's desk),
    or the bank."""
    if a is None:
        return "—"
    if a.shop_id:
        return str(_("Shop %(s)s") % {"s": a.shop.name})
    if a.custodian_id and a.custodian.subzone_id:
        return str(a.custodian.subzone)
    if a.zone_id:
        return str(a.zone)
    return str(Account.Kind(a.kind).label) if a.kind == K.BANK else str(a)


def transfers(f):
    """Transfer reconciliation: every internal transfer (cashier <-> cashier, cashier <-> shop, shop <-> shop and
    capital transfers) in the period — who sent what to whom, what each desk or shop sent and received, and the
    check that what was sent equals what was received, per product."""
    qs = Transaction.objects.filter(f.date_q(), type__in=TRANSFER_TYPES, correction__isnull=True) \
        .select_related("source_account__shop", "source_account__zone", "source_account__custodian__subzone",
                        "dest_account__shop", "dest_account__zone", "dest_account__custodian__subzone", "entered_by") \
        .order_by("business_date", "posted_at", "id")
    if f.cashier or f.subzone or f.master_zone:  # a desk or a zone: transfers it sent or received
        ids = list(scoped_asset_accounts(f).values_list("id", flat=True))
        qs = qs.filter(Q(source_account_id__in=ids) | Q(dest_account_id__in=ids))
    return transfer_reconciliation(qs, f.text())


def transfer_reconciliation(qs, filters_text):
    """The report itself, for any set of transfers (the cashiers' reports, or the shops' reports)."""
    from collections import OrderedDict

    rep = Report("transfers", _("Transfer reconciliation"), filters_text)
    qs = qs.prefetch_related("entries", "transit_entries")
    rows, pairs, parties, kind_of = [], OrderedDict(), OrderedDict(), {}
    products = []
    sent_total, received_total = Z, Z
    for t in qs:
        src, dst = t.source_account, t.dest_account
        prod = str(Account.Kind(src.kind).label) if src else "—"
        if prod not in products:
            products.append(prod)
        a, b = _party(src), _party(dst)
        for acc, who in ((src, a), (dst, b)):
            kind_of[who] = 1 if acc is not None and acc.shop_id else (0 if acc is not None and (
                acc.custodian_id or acc.zone_id) else 2)
        # what really moved: a transfer recorded by its sender moves only the sender's account, an entry only the
        # receiver's; older transfers moved both
        out = -sum((e.amount for e in t.entries.all() if e.account_id == t.source_account_id), Z)
        inn = sum((e.amount for e in t.entries.all() if e.account_id == t.dest_account_id), Z)
        side_txt = _("entry by the receiver") if t.is_entry else (
            _("sent, waiting for the entry") if t.in_transit and not t.transit_entries.exists() else
            (_("sent, entered") if t.in_transit else ""))
        rows.append(dict(business_date=t.business_date, reference=t.reference, _id=t.pk, type=t.display_type,
                         sender=a, receiver=b, product=prod, amount=out, entered=inn, by=str(t.entered_by),
                         description=" · ".join(x for x in (str(side_txt), t.description) if x)))
        key = (a, b, prod)
        pr = pairs.setdefault(key, {"sender": a, "receiver": b, "product": prod, "n": 0, "amount": Z,
                                    "entered": Z, "waiting": Z})
        pr["n"] += 1
        pr["amount"] += out
        pr["entered"] += inn
        pr["waiting"] = pr["amount"] - pr["entered"]
        for who, side, v in ((a, "sent", out), (b, "received", inn)):
            p = parties.setdefault(who, defaultdict(Decimal))
            p[f"{side}_{prod}"] += v
            p[side] += v
        sent_total += out
        received_total += inn
    by_party = []
    group_names = [_("Cashier desks"), _("Shops"), _("Company (bank, finance)")]
    for g, gname in enumerate(group_names):
        members = [(who, p) for who, p in sorted(parties.items()) if kind_of.get(who, 2) == g]
        if not members:
            continue
        by_party.append({"party": gname, "_head": True})
        sub = defaultdict(Decimal)
        for who, p in members:
            r = {"party": who}
            for prod in products:
                r[f"sent_{prod}"] = p.get(f"sent_{prod}", Z)
                r[f"received_{prod}"] = p.get(f"received_{prod}", Z)
            r.update(sent=p["sent"], received=p["received"], net=p["received"] - p["sent"])
            by_party.append(r)
            for k, v in r.items():
                if k != "party":
                    sub[k] += v
        by_party.append({"party": _("Total %(sub)s") % {"sub": gname}, "_sub": True, **sub})
    cols = [Col("party", _("Desk / shop"))]
    for prod in products:
        cols += [Col(f"sent_{prod}", f"{prod} · " + str(_("sent")), "money", True),
                 Col(f"received_{prod}", f"{prod} · " + str(_("received")), "money", True)]
    cols += [Col("sent", _("Total sent"), "money", True), Col("received", _("Total received"), "money", True),
             Col("net", _("Net (received − sent)"), "money", True)]
    rep.tables.append(Table(_("By desk and shop"), cols, by_party,
                            note=_("Sent = what each one recorded leaving his own accounts; received = what he entered himself. The difference is still in transit.")))
    wcols = [Col("receiver", _("To")), Col("product", _("Product")),
             Col("n", _("Records"), "int", True), Col("amount", _("Sent (sender's records)"), "money", True),
             Col("entered", _("Entered by the receiver"), "money", True),
             Col("waiting", _("In transit (sent − entered)"), "money", True)]
    rep.tables.append(Table(_("Who sent what to whom"), wcols,
                            group_rows(list(pairs.values()), wcols, key=lambda r: r["sender"],
                                       label=lambda g: _("From %(s)s") % {"s": g})))
    acols = [Col("business_date", _("Date"), "date"), Col("reference", _("Reference")),
             Col("type", _("Type")), Col("receiver", _("To")),
             Col("product", _("Product")), Col("amount", _("Sent (sender's records)"), "money", True),
             Col("entered", _("Entered by the receiver"), "money", True),
             Col("by", _("Recorded by")), Col("description", _("Note"))]
    rep.tables.append(Table(_("All transfers, by sender"), acols,
                            group_rows(rows, acols, key=lambda r: r["sender"],
                                       label=lambda g: _("From %(s)s") % {"s": g})))
    ok = sent_total == received_total
    rep.summary = [(_("Transfers"), str(len(rows)), "text"), (_("Total sent"), sent_total, "money"),
                   (_("Total received"), received_total, "money"),
                   (_("In transit (sent, not yet entered)"), sent_total - received_total, "money"),
                   (_("Check"), _("OK — everything sent was entered") if ok else
                    _("Some transfers are not entered yet (see In transit)"), "text")]
    return rep


def conversions(f):
    rep = Report("conversions", _("UVE → ER conversions and rates used"), f.text())
    rows = []
    for t in txns(f, [TxType.CONVERSION]):
        s = -1 if t.type == TxType.REVERSAL else 1
        src = t.reverses if t.type == TxType.REVERSAL else t
        rows.append(dict(posted_at=t.posted_at, business_date=t.business_date, reference=t.reference,
                         type=str(t.display_type), sim=str(src.source_account.sim), cashier=station(t),
                         rate=src.rate_used, uve=s * src.amount, er=s * src.amount_received,
                         uplift=s * (src.amount_received - src.amount), corrected=hasattr(t, "correction"),
                         _id=t.id, _g=_desk_of(t)))
    cats = [("uve", _("UVE converted")), ("er", _("ER received")), ("uplift", _("Conversion uplift"))]
    items = []
    for t in txns(f, [TxType.CONVERSION]):
        sgn, src = (-1 if t.type == TxType.REVERSAL else 1), _base(t)
        items.append((*_txn_place(t), {"uve": sgn * src.amount, "er": sgn * src.amount_received,
                                       "uplift": sgn * (src.amount_received - src.amount)}))
    piv = zone_pivot(_("By zone and cashier"), items, cats, total=False, count=True)
    rep.tables.append(piv)
    rep.summary = pivot_summary(piv, cats, total=False)
    ccols = [
        Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
        Col("reference", _("Reference")), Col("type", _("Type")), Col("sim", _("Orange SIM")),
        Col("rate", _("Uplift rate used"), "rate"),
        Col("uve", _("UVE converted"), "money", True), Col("er", _("ER received"), "money", True),
        Col("uplift", _("Conversion uplift"), "money", True)]
    rep.tables.append(Table(_("Conversions by cashier"), ccols, group_rows(rows, ccols, key=lambda r: r["_g"],
                                                                         sub_label=lambda g: g.split(" · ")[0])))
    rate_rows = [dict(effective_from=r.effective_from, rate=r.uplift_rate, by=str(r.created_by), note=r.note)
                 for r in ConversionRate.objects.all()]
    rep.tables.append(Table(_("Rate history (changes apply to future conversions only)"), [
        Col("effective_from", _("Effective from"), "datetime"), Col("rate", _("Uplift rate"), "rate"),
        Col("by", _("Set by")), Col("note", _("Note"))], rate_rows))
    return rep


def cash_managers(f):
    rep = Report("cash_managers", _("Cash-manager exchanges and positions"), f.text())
    cms = list(CashManager.objects.all())
    accts = [c.account for c in cms]
    bals = balances_by_account(accts, f)
    rows = []
    for c, a in zip(cms, accts):
        o, dr, cr, cl, cur = bals[a.id]
        rows.append(dict(name=str(c), opening=o, given=dr, received=cr, closing=cl,
                         owes_company=max(cl, Z), company_owes=max(-cl, Z), current=cur))
    rep.tables.append(Table(_("Positions (positive = cash manager owes the company)"), [
        Col("name", _("Cash manager")), Col("opening", _("Opening"), "money", True),
        Col("given", _("Company gave"), "money", True), Col("received", _("Company received"), "money", True),
        Col("closing", _("Closing"), "money", True), Col("owes_company", _("Owes company"), "money", True),
        Col("company_owes", _("Company owes"), "money", True), Col("current", _("Current"), "money", True)], rows))
    tot = rep.tables[-1].totals() or {}
    rep.summary = [(_("Company gave"), tot.get("given", Z), "money"),
                   (_("Company received"), tot.get("received", Z), "money"),
                   (_("Owes company"), tot.get("owes_company", Z), "money"),
                   (_("Company owes"), tot.get("company_owes", Z), "money"),
                   (_("Current"), tot.get("current", Z), "money")]
    exq = Q()
    if f.date_from:
        exq &= Q(business_date__gte=f.date_from)
    if f.date_to:
        exq &= Q(business_date__lte=f.date_to)
    if f.cashier:
        exq &= Q(created_by=f.cashier)
    ex_rows = []
    for ex in CashManagerExchange.objects.filter(exq).select_related("cash_manager", "created_by"):
        give, recv = ex.side_totals()
        ex_rows.append(dict(reference=ex.reference, business_date=ex.business_date, cm=str(ex.cash_manager),
                            direction=ex.external_ref, amount=ex.amount, given=give, received=recv,
                            to_serve=ex.to_serve,
                            status=ex.status, by=str(ex.created_by)))
    xcols = [
        Col("reference", _("Reference")), Col("business_date", _("Business date"), "date"),
        Col("direction", _("Ref number")), Col("amount", _("Amount"), "money", True),
        Col("given", _("Company gave"), "money", True), Col("received", _("Amount served"), "money", True),
        Col("to_serve", _("Balance to be served"), "money", True),
        Col("status", _("Status")), Col("by", _("Recorded by"))]
    rep.tables.append(Table(_("Exchange agreements by cash manager (no money moves until a leg is recorded)"), xcols,
                            group_rows(ex_rows, xcols, key=lambda r: r["cm"])))
    legs = txns(f, [TxType.CM_GIVE, TxType.CM_RECEIVE])
    rep.tables.append(detail_table(_("Completed legs"), legs))
    return rep


def accounts(f):
    rep = Report("accounts", _("Cash drawer, SIM wallet and bank movements"), f.text())
    accs = list(scoped_asset_accounts(f))
    bals = balances_by_account(accs, f)
    groups = defaultdict(list)
    for a in accs:
        o, dr, cr, cl, cur = bals[a.id]
        z = a.zone
        master = (z.parent.name if z.parent_id else z.name) if z else (
            str(_("Shop %(s)s") % {"s": a.shop.name}) if a.shop_id else str(_("Company")))
        groups[master].append(dict(account=str(a), kind=a.get_kind_display(),
                                   custodian=(f"{a.zone} · {a.custodian}" if a.custodian_id else str(a.zone or "")),
                                   opening=o, inflow=dr, outflow=cr, closing=cl, current=cur, _k=a.kind))
    rows, by_kind = [], defaultdict(Decimal)
    for master in sorted(groups):
        rows.append({"account": master, "_head": True})
        sub = defaultdict(Decimal)
        for r in groups[master]:
            rows.append(r)
            by_kind[r["_k"]] += r["current"]
            for k in ("opening", "inflow", "outflow", "closing", "current"):
                sub[k] += r[k]
        rows.append({"account": _("Total %(sub)s") % {"sub": master}, "_sub": True, **sub})
    rep.summary = [(str(Account.Kind(k).label), v, "money") for k, v in sorted(by_kind.items())] + [
        (_("Total balance now"), sum(by_kind.values(), Z), "money")]
    rep.tables.append(Table(_("Balances by zone"), [
        Col("account", _("Account")), Col("kind", _("Type")), Col("custodian", _("Zone / in charge")),
        Col("opening", _("Opening balance"), "money", True), Col("inflow", _("In"), "money", True),
        Col("outflow", _("Out"), "money", True), Col("closing", _("Closing balance"), "money", True),
        Col("current", _("Current balance"), "money", True)], rows))
    ent = Entry.objects.filter(account__in=accs).filter(f.date_q()).select_related(
        "account__sim", *[f"transaction__{r}" for r in TX_RELATED]).order_by("account__kind", "account_id",
                                                                             "business_date", "id")
    if f.tx_type:
        ent = ent.filter(transaction__type=f.tx_type)
    erows = [dict(account=str(e.account), business_date=e.business_date, posted_at=e.transaction.posted_at,
                  reference=e.transaction.reference, type=str(e.transaction.display_type),
                  details=describe(e.transaction), cashier=station(e.transaction),
                  inflow=e.amount if e.amount > 0 else Z, outflow=-e.amount if e.amount < 0 else Z,
                  corrected=hasattr(e.transaction, "correction"), _id=e.transaction_id) for e in ent]
    mcols = [
        Col("business_date", _("Business date"), "date"),
        Col("posted_at", _("Recorded at"), "datetime"), Col("reference", _("Reference")), Col("type", _("Type")),
        Col("details", _("Details")), Col("cashier", _("Cashier")), Col("inflow", _("In"), "money", True),
        Col("outflow", _("Out"), "money", True)]
    order = {a: i for i, a in enumerate(dict.fromkeys(r["account"] for r in erows))}
    rep.tables.append(Table(_("Movements by account"), mcols,
                            group_rows(erows, mcols, key=lambda r: r["account"], sort=lambda a: order[a])))
    return rep


def corrections(f):
    rep = Report("corrections", _("Corrections and responsibility transfers"), f.text())
    cq = Q()
    if f.date_from:
        cq &= Q(reversal_txn__business_date__gte=f.date_from)
    if f.date_to:
        cq &= Q(reversal_txn__business_date__lte=f.date_to)
    if f.cashier:
        cq &= Q(original__cashier=f.cashier) | Q(original__cashier__isnull=True, original__entered_by=f.cashier)
    if f.subzone:
        cq &= Q(original__zone=f.subzone)
    if f.master_zone:
        cq &= Q(original__zone=f.master_zone) | Q(original__zone__parent=f.master_zone)
    if f.customer:
        cq &= Q(original__counterparty=f.customer) | Q(original__counterparty_2=f.customer)
    rows = []
    for c in Correction.objects.filter(cq).select_related("original", "corrected_by", "reversal_txn",
                                                          "replacement_txn"):
        rows.append(dict(created_at=c.created_at, original=c.original.reference,
                         original_date=c.original.business_date, type=c.original.get_type_display(),
                         kind=c.get_kind_display(), reason=c.reason,
                         changes="; ".join(f"{k}: {v[0]} → {v[1]}" for k, v in c.changes.items()),
                         reversal=c.reversal_txn.reference,
                         replacement=c.replacement_txn.reference if c.replacement_txn else "",
                         posted_date=c.reversal_txn.business_date, by=str(c.corrected_by), _id=c.original_id))
    counts = defaultdict(int)
    for r in rows:
        counts[r["kind"]] += 1
    rep.summary = [(k, str(n), "text") for k, n in sorted(counts.items())] + [
        (_("Corrections"), str(len(rows)), "text")]
    kcols = [
        Col("created_at", _("Corrected at"), "datetime"), Col("original", _("Original")),
        Col("original_date", _("Original business date"), "date"), Col("type", _("Type")),
        Col("reason", _("Reason")), Col("changes", _("Changes")),
        Col("reversal", _("Reversal")), Col("replacement", _("Replacement")),
        Col("posted_date", _("Posted on business date"), "date"), Col("by", _("Corrected by"))]
    rep.tables.append(Table(_("Corrections by kind"), kcols, group_rows(rows, kcols, key=lambda r: r["kind"])))
    tr = txns(f, [TxType.RESPONSIBILITY_TRANSFER])
    trows = []
    for t in tr:
        r = txn_row(t)
        r["from_party"] = str(t.counterparty_2)
        r["supply"] = t.related_supply.reference if t.related_supply_id else (
            t.reverses.related_supply.reference if t.reverses_id and t.reverses.related_supply_id else "")
        r["by"] = str(t.entered_by)
        r["_g"] = _desk_of(t)
        trows.append(r)
    rcols = [
        Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
        Col("reference", _("Reference")), Col("supply", _("Supply")), Col("from_party", _("From")),
        Col("customer", _("To")), Col("details", _("Reason / details")), Col("by", _("Changed by")),
        Col("amount", _("Amount"), "money", True)]
    rep.tables.append(Table(_("Responsibility transfers by cashier"), rcols,
                            group_rows(trows, rcols, key=lambda r: r["_g"],
                                       sub_label=lambda g: g.split(" · ")[0])))
    return rep


def closings(f):
    rep = Report("closings", _("Daily closings and unresolved differences"), f.text())
    lq = Q()
    if f.date_from:
        lq &= Q(closing__cashier_day__business_date__gte=f.date_from)
    if f.date_to:
        lq &= Q(closing__cashier_day__business_date__lte=f.date_to)
    lq &= f.day_zone_q("closing__cashier_day")
    flat = []
    for ln in ClosingLine.objects.filter(lq).select_related("closing__cashier_day__zone", "closing__closed_by",
                                                            "account__sim") \
            .order_by("closing__cashier_day__business_date", "closing__cashier_day__zone__name", "account__kind"):
        flat.append(dict(_g=str(ln.closing.cashier_day.zone or "—"), business_date=ln.closing.cashier_day.business_date,
                         cashier=f"{ln.closing.cashier_day.zone or '—'} · {ln.closing.closed_by}",
                         closed_at=ln.closing.closed_at,
                         account=str(ln.account), opening=ln.opening_book, movements=ln.movements,
                         expected=ln.expected, actual=ln.actual, difference=ln.difference,
                         carried=ln.carried_unresolved, new=ln.new_difference))
    rows, grouped = [], defaultdict(list)
    for r in flat:
        grouped[r["_g"]].append(r)
    for g in sorted(grouped):
        rows.append({"business_date": g, "_head": True})
        rows += grouped[g]
        rows.append({"business_date": _("Total %(sub)s") % {"sub": g}, "_sub": True,
                     "difference": sum((r["difference"] for r in grouped[g]), Z),
                     "new": sum((r["new"] for r in grouped[g]), Z)})
    rep.tables.append(Table(_("Closing lines (book balance carries forward)"), [
        Col("business_date", _("Business date"), "date"), Col("cashier", _("Subzone · closed by")),
        Col("closed_at", _("Closed at"), "datetime"), Col("account", _("Account")),
        Col("opening", _("Opening (book)"), "money"), Col("movements", _("Movements"), "money"),
        Col("expected", _("Expected"), "money"), Col("actual", _("Actual"), "money"),
        Col("difference", _("Difference"), "money", True), Col("carried", _("Carried unresolved"), "money"),
        Col("new", _("New difference"), "money", True)], rows))
    dq = Q(status=Discrepancy.Status.OPEN)
    dq &= f.day_zone_q("closing_line__closing__cashier_day")
    drows = [dict(date=d.closing_line.closing.cashier_day.business_date,
                  cashier=str(d.closing_line.closing.cashier_day.zone or "—"), account=str(d.account),
                  kind=str(d.kind_label), amount=d.amount, notes=d.notes.count(), _disc=d.id)
             for d in Discrepancy.objects.filter(dq).select_related("closing_line__closing__cashier_day__zone",
                                                                     "account__sim")]
    rep.summary = [(_("Closing lines"), str(len(flat)), "text"),
                   (_("Difference"), sum((r["difference"] for r in flat), Z), "money"),
                   (_("New difference"), sum((r["new"] for r in flat), Z), "money"),
                   (_("Unresolved differences (all dates)"), sum((d["amount"] for d in drows), Z), "money")]
    ucols = [Col("date", _("Business date"), "date"), Col("account", _("Account")),
             Col("kind", _("Type")), Col("amount", _("Amount"), "money", True),
             Col("notes", _("Investigation notes"), "int")]
    rep.tables.append(Table(_("Unresolved differences by subzone (all dates)"), ucols,
                            group_rows(drows, ucols, key=lambda r: r["cashier"])))
    return rep


RECON_GROUPS = [
    ("launch", _l("Launch opening"), [TxType.OPENING_BALANCE]),
    ("supplies", _l("Supplies"), [TxType.SUPPLY]),
    ("destockage", _l("Destockage"), [TxType.DESTOCKAGE]),
    ("collections", _l("Collections"), [TxType.COLLECTION]),
    ("cash_mgr", _l("Cash managers"), [TxType.CM_GIVE, TxType.CM_RECEIVE]),
    ("conversion", _l("Conversions"), [TxType.CONVERSION]),
    ("expenses", _l("Expenses"), [TxType.EXPENSE]),
    ("transfers", _l("Internal transfers"), [TxType.INTERNAL_TRANSFER]),
    ("adjust", _l("Reconciliation adj."), [TxType.RECON_ADJUSTMENT]),
    ("reversal", _l("Correction reversals"), [TxType.REVERSAL]),
    ("capital", _l("Capital movements"), [TxType.CAPITAL_IN, TxType.CAPITAL_OUT, TxType.CAPITAL_MOVE]),
    ("float_ex", _l("Float exchanged for UVE"), [TxType.FLOAT_EXCHANGE]),
    ("shops", _l("Transfers with shops"), [t for t in TxType.values if t.startswith("SHOP_")] + [TxType.DESK_TO_SHOP]),
]


def _zone_summary(f):
    """Per zone: opening and closing Cash/UVE/ER/MTN, subzones under their
    master zone, master-level accounts (master SIM) on their own line, and
    the company total. Respects the zone scope of the filters."""
    from core.services import permissions as perm

    kinds = [K.CASH_DRAWER, K.UVE, K.ER, K.MTN]
    accts = list(scoped_asset_accounts(f, kinds).filter(zone__isnull=False))
    bals = balances_by_account(accts, f)
    groups = {}
    for a in accts:
        m = a.zone.master
        if a.zone.is_master and (a.custodian_id or not m.subzones.exists()):
            sub_key, label = ("m", a.custodian_id), (_("Master SIM") + (f" · {a.custodian}" if a.custodian_id else ""))
            level = _("Master SIM")
        else:
            sub_key, label, level = ("s", a.zone_id), str(a.zone), _("Subzone")
        g = groups.setdefault(m.id, {"name": str(m), "lines": {}, "tot": defaultdict(Decimal)})
        line = g["lines"].setdefault(sub_key, {"name": label, "level": level, "tot": defaultdict(Decimal)})
        o, _dr, _cr, cl, _cur = bals[a.id]
        for tgt in (line["tot"], g["tot"]):
            tgt[f"{a.kind}_o"] += o
            tgt[f"{a.kind}_c"] += cl
    rows, company = [], defaultdict(Decimal)
    for g in sorted(groups.values(), key=lambda x: x["name"]):
        rows.append(dict(zone=g["name"], _head=True))
        for ln in sorted(g["lines"].values(), key=lambda x: (x["level"] != _("Subzone"), x["name"])):
            rows.append(dict(zone=ln["name"], level=ln["level"], **{k: ln["tot"][k] for k in ln["tot"]}))
        rows.append(dict(zone=_("Total %(sub)s") % {"sub": g["name"]}, level=_("Master zone"),
                         **{k: g["tot"][k] for k in g["tot"]}, _sub=True))
        for k, v in g["tot"].items():
            company[k] += v
    if f.master_zone is None and f.subzone is None and f.cashier is None:
        rows.append(dict(zone=_("CASHIERS TOTAL"), level=_("Company"), **company, _bold=True))
    cols = [Col("zone", _("Zone")), Col("level", _("Level"))]
    for k in kinds:
        lbl = Account.Kind(k).label
        cols += [Col(f"{k}_o", f"{lbl} · " + str(_l("opening")), "money"),
                 Col(f"{k}_c", f"{lbl} · " + str(_l("closing")), "money")]
    for r in rows:
        if not r.get("_head"):
            for c in cols[2:]:
                r.setdefault(c.key, Z)
    return Table(_("Reconciliation by zone (opening and closing balances of the period)"), cols, rows)


def reconciliation(f):
    rep = Report("reconciliation", _("Reconciliation"), f.text())
    accs = list(scoped_asset_accounts(f))
    bals = balances_by_account(accs, f)
    agg = Entry.objects.filter(account__in=accs).filter(f.date_q()).values("account_id", "transaction__type") \
        .annotate(s=Sum("amount"))
    pivot = defaultdict(lambda: defaultdict(Decimal))
    for r in agg:
        pivot[r["account_id"]][r["transaction__type"]] += r["s"]
    rows, all_ok = [], True
    for a in accs:
        o, dr, cr, cl, cur = bals[a.id]
        row = dict(account=str(a).split(" · ", 1)[-1], opening=o, closing=cl, current=cur, _k=a.kind)
        total = Z
        for key, _label, types in RECON_GROUPS:
            v = sum((pivot[a.id][t] for t in types), Z)
            row[key] = v
            total += v
        row["check"] = _("OK") if o + total == cl else _("MISMATCH")
        all_ok &= o + total == cl
        rows.append(row)
    cols = [Col("account", _("Account")), Col("opening", _("Opening balance"), "money", True)]
    cols += [Col(k, lbl, "money", True) for k, lbl, _t in RECON_GROUPS]
    cols += [Col("closing", _("Closing balance"), "money", True), Col("current", _("Current balance"), "money", True),
             Col("check", _("Check"))]
    kind_order = [K.CASH_DRAWER, K.UVE, K.ER, K.MTN, K.BANK]
    grouped = group_rows(rows, cols, key=lambda r: r["_k"], label=lambda k: str(Account.Kind(k).label),
                         sort=lambda k: kind_order.index(k) if k in kind_order else 99)
    rep.tables.append(_zone_summary(f))
    rep.tables.append(drop_empty_columns(
        Table(_("Company assets by product: opening + period movements = closing"), cols, grouped,
              note=_("Only the kinds of movement that happened in the period are shown.")),
        keep=("opening", "closing", "current")))

    # Whole-ledger position (all zones for supervisors; cashiers see shared customer totals too)
    def tot(kind, q=Q()):
        return money(Entry.objects.filter(account__kind=kind).filter(q).aggregate(s=Sum("amount"))["s"])

    upto = Q(business_date__lte=f.date_to) if f.date_to else Q()
    period = f.date_q()
    cust = Entry.objects.filter(account__kind=K.CUSTOMER).filter(upto).values("account_id").annotate(s=Sum("amount"))
    owed = sum((r["s"] for r in cust if r["s"] > 0), Z)
    due = -sum((r["s"] for r in cust if r["s"] < 0), Z)
    period_sum = money(Entry.objects.filter(period).aggregate(s=Sum("amount"))["s"])
    rep.summary = [
        (_("Agents and POS owe the company (end of period)"), owed, "money"),
        (_("Supply due to agents and POS (end of period)"), due, "money"),
        (_("Cash managers net position (end of period, + = they owe)"), tot(K.CASH_MANAGER, upto), "money"),
        (_("Expenses in period"), tot(K.EXPENSE, period), "money"),
        (_("Conversion uplift in period"), -tot(K.CONVERSION_UPLIFT, period), "money"),
        (_("Reconciliation adjustments in period (+ = shortage written off)"), tot(K.RECON_ADJUSTMENT, period),
         "money"),
        (_("Open (unresolved) closing differences, all dates"),
         money(Discrepancy.objects.filter(status="OPEN").aggregate(s=Sum("amount"))["s"]), "money"),
        (_("Ledger balanced in period (debits = credits)"), _("Yes") if period_sum == 0 else _("NO"), "text"),
        (_("Asset rows reconcile"), _("Yes") if all_ok else _("NO"), "text"),
    ]
    return rep



# ------------------------------------------------------------------ daily reports for the cashiers
DAILY_KINDS = [(K.CASH_DRAWER, _l("Cash")), (K.UVE, "UVE"), (K.ER, "ER"), (K.MTN, _l("MTN float"))]


def _days(f, qs_dates):
    """The business days to show: the filter's range, or the days that have entries when it is open-ended."""
    from datetime import timedelta

    if f.date_from and f.date_to and (f.date_to - f.date_from).days <= 92:
        d, out = f.date_from, []
        while d <= f.date_to:
            out.append(d)
            d += timedelta(days=1)
        return out
    return sorted(set(qs_dates))


def daily_reconciliation(f):
    """Day by day, per product (cash, UVE, ER, MTN float): opening + in − out = closing, next to what was counted at
    the closing of that day and the difference."""
    rep = Report("daily_reconciliation", _("Daily reconciliation"), f.text())
    accs = list(scoped_asset_accounts(f, [k for k, _l2 in DAILY_KINDS]).filter(zone__isnull=False))
    ids = [a.id for a in accs]
    kind_of = {a.id: a.kind for a in accs}
    ent = Entry.objects.filter(account_id__in=ids)
    moves = defaultdict(lambda: [Z, Z])  # (day, kind) -> [in, out]
    for r in ent.filter(f.date_q()).values("business_date", "account_id").annotate(
            i=Sum("amount", filter=Q(amount__gt=0)), o=Sum("amount", filter=Q(amount__lt=0))):
        m = moves[(r["business_date"], kind_of[r["account_id"]])]
        m[0] += money(r["i"])
        m[1] -= money(r["o"])
    days = _days(f, [d for d, _k in moves])
    running = {}
    for k, _l2 in DAILY_KINDS:
        q = ent.filter(account__kind=k)
        if days:
            q = q.filter(business_date__lt=days[0])
        running[k] = money(q.aggregate(s=Sum("amount"))["s"]) if days else Z
    counted = defaultdict(lambda: [Z, Z, False])  # (day, kind) -> [actual, difference, closed]
    for ln in ClosingLine.objects.filter(account_id__in=ids).filter(f.date_q("closing__cashier_day__business_date")) \
            .values("closing__cashier_day__business_date", "account__kind").annotate(a=Sum("actual"),
                                                                                    d=Sum("difference")):
        c = counted[(ln["closing__cashier_day__business_date"], ln["account__kind"])]
        c[0], c[1], c[2] = money(ln["a"]), money(ln["d"]), True
    rows, totals = [], {k: [Z, Z] for k, _l2 in DAILY_KINDS}
    for d in days:
        for k, label in DAILY_KINDS:
            i, o = moves.get((d, k), (Z, Z))
            opening = running[k]
            closing = opening + i - o
            running[k] = closing
            c = counted.get((d, k))
            if not (i or o or opening or c):
                continue
            totals[k][0] += i
            totals[k][1] += o
            rows.append(dict(day=d, product=str(label), opening=opening, money_in=i, money_out=o, closing=closing,
                             counted=c[0] if c else None, difference=c[1] if c else None,
                             status=(_("closed") if c else _("not closed"))))
    dcols = [Col("product", _("Product")),
             Col("opening", _("Opening"), "money"), Col("money_in", _("In"), "money", True),
             Col("money_out", _("Out"), "money", True), Col("closing", _("Closing (book)"), "money"),
             Col("counted", _("Counted at closing"), "money"), Col("difference", _("Difference"), "money", True),
             Col("status", _("Day"))]
    rep.tables.append(Table(_("Day by day: opening + in − out = closing"), dcols,
                            group_rows(rows, dcols, key=lambda r: r["day"], label=lambda d: d.strftime("%d/%m/%Y"),
                                       sort=lambda d: d, subtotal=False)))
    from core.money import fmt

    rep.summary = [(_("%(p)s at the end (in %(i)s, out %(o)s)") % {"p": label, "i": fmt(totals[k][0]),
                                                                    "o": fmt(totals[k][1])}, running[k], "money")
                   for k, label in DAILY_KINDS]
    return rep


def _base(t):
    return t.reverses if t.type == TxType.REVERSAL else t


def _agent_place(cp):
    """(subzone name, microzones) of an agent / POS. An agent controlling several microzones (distribution roads)
    is listed once, under the subzone of his own microzone, with all his microzones."""
    mzs = []
    if cp.kind == "AGENT":  # his roads now (agents are switched / transferred): listed once with all of them
        mzs = list(cp.controlled_microzones.select_related("subzone").order_by("name"))
    if cp.microzone_id and not mzs:
        mzs = [cp.microzone]
    home = (cp.microzone if cp.microzone_id and cp.microzone in mzs else None) or (mzs[0] if mzs else None)
    if home is not None:
        sub = home.subzone.name
    else:
        sub = cp.master_zone.name if cp.master_zone_id else _("No subzone")
    return sub, ", ".join(m.name for m in mzs)


SP_FIXED = [("float_sup", _l("Float supplied (finance)")), ("supplied", _l("Total supplied")),
            ("paid_float", _l("Paid in float")), ("paid", _l("Total paid")),
            ("net", _l("Balance for the period (+ owes, − overpaid)"))]


def _desk_label(u):
    """Short name of a cashier's desk (his subzone); finance / managers by their name."""
    if getattr(u, "is_cashier", False) and u.subzone_id:
        return u.subzone.name
    return u.get_full_name() or u.username


def supply_payment(f):
    """Supplies against payments, as a pivot: agents and POS grouped by subzone (with subzone totals and the grand
    total). Supplies are split by cashier (desk), then float supplied by finance, then the total; payments by cashier,
    then payments in float, then the total."""
    from core.services.ledger import balance

    rep = Report("supply_payment", _("Daily supplies versus payments"), f.text())
    sup = supply_txns(f)  # cashiers' supplies and destockages, and float converted and sent to an agent by finance
    pay = list(txns(f, [TxType.COLLECTION]))
    blank = lambda: defaultdict(Decimal)  # noqa: E731
    per_cust, per_day, per_desk = defaultdict(blank), defaultdict(blank), defaultdict(blank)
    desks = {}  # user pk -> label, for the cashiers who supplied or received payments (not float)
    for t in sup:
        b, v, kind, u = _base(t), signed(t), supply_kind(t), _desk_user(t)
        if kind == "FLOAT":
            key = "float_sup"
        else:
            key = f"s_{u.pk}"
            desks[u.pk] = u
        for bucket in (per_day[t.business_date], per_cust[b.counterparty] if b.counterparty_id else None):
            if bucket is not None:
                bucket[key] += v
                bucket["supplied"] += v
        d = per_desk[u.pk]
        d[kind] += v
        d["supplied"] += v
        desks.setdefault(u.pk, u)
    for t in pay:
        b, v, u = _base(t), signed(t), _desk_user(t)
        if b.payment_method == PaymentMethod.MTN:
            key = "paid_float"
        else:
            key = f"p_{u.pk}"
            desks[u.pk] = u
        for bucket in (per_day[t.business_date], per_cust[b.counterparty] if b.counterparty_id else None):
            if bucket is not None:
                bucket[key] += v
                bucket["paid"] += v
        d = per_desk[u.pk]
        d["P_" + b.payment_method] += v
        d["paid"] += v
        desks.setdefault(u.pk, u)
    # cashiers' columns, labelled by desk (a name is added when two cashiers share a desk)
    labels = {pk: _desk_label(u) for pk, u in desks.items()}
    dup = {lbl for lbl in labels.values() if list(labels.values()).count(lbl) > 1}
    labels = {pk: (f"{lbl} ({desks[pk].first_name or desks[pk].username})" if lbl in dup else lbl)
              for pk, lbl in labels.items()}
    s_desks = sorted((pk for pk in desks if any(r.get(f"s_{pk}") for r in per_day.values())), key=lambda k: labels[k])
    p_desks = sorted((pk for pk in desks if any(r.get(f"p_{pk}") for r in per_day.values())), key=lambda k: labels[k])
    keys = [f"s_{pk}" for pk in s_desks] + [f"p_{pk}" for pk in p_desks] + [k for k, _l2 in SP_FIXED if k != "net"]

    def vals(v):
        out = {k: v[k] for k in keys}
        out["net"] = v["supplied"] - v["paid"]
        return out

    def money_cols(net_label=None):
        cols = [Col(f"s_{pk}", _("Supplied · %(c)s") % {"c": labels[pk]}, "money", True) for pk in s_desks]
        cols += [Col("float_sup", _("Float supplied (finance)"), "money", True),
                 Col("supplied", _("Total supplied"), "money", True)]
        cols += [Col(f"p_{pk}", _("Paid · %(c)s") % {"c": labels[pk]}, "money", True) for pk in p_desks]
        cols += [Col("paid_float", _("Paid in float"), "money", True), Col("paid", _("Total paid"), "money", True),
                 Col("net", net_label or _("Balance for the period (+ owes, − overpaid)"), "money", True)]
        return cols

    # group agents / POS by subzone
    groups = defaultdict(list)
    for cp, v in per_cust.items():
        sub, mzs = _agent_place(cp)
        groups[sub].append((cp, mzs, v))
    sub_rows, cust_rows, bal_all = [], [], Z
    for sub in sorted(groups):
        members = sorted(groups[sub], key=lambda x: (x[0].kind != "AGENT", x[0].name.lower()))
        tot, bal_tot = defaultdict(Decimal), Z
        cust_rows.append(dict(customer=sub, _head=True))
        for cp, mzs, v in members:
            bal = balance(cp.account)
            name = f"{cp.name} · {cp.sim_display}" + (" (POS)" if cp.kind == "POS" else "")
            cust_rows.append(dict(customer=name, microzones=mzs, balance=bal, **vals(v)))
            for k in keys:
                tot[k] += v[k]
            bal_tot += bal
        bal_all += bal_tot
        cust_rows.append(dict(customer=_("Total %(sub)s") % {"sub": sub}, balance=bal_tot, _sub=True, **vals(tot)))
        sub_rows.append(dict(subzone=sub, count=len(members), balance=bal_tot, **vals(tot)))
    bal_col = Col("balance", _("Total balance today, all desks (+ owes, − credit)"), "money", True)
    rep.tables.append(Table(_("Summary by subzone"), [Col("subzone", _("Subzone")),
                                                     Col("count", _("Agents / POS"), "int", True)]
                            + money_cols() + [bal_col], sub_rows))
    rep.tables.append(Table(_("Per agent / POS, by subzone"), [
        Col("customer", _("Agent / POS")), Col("microzones", _("Microzones"))] + money_cols() + [bal_col], cust_rows,
        note=_("Balance for the period = total supplied − total paid during the selected dates only (+ still owes, "
               "− paid more: old debts or in advance). Total balance today covers all dates and all desks "
               "(+ owes the company, − the company owes them). An agent running several microzones is listed once "
               "with all of them.")))
    day_rows = [dict(day=d, **vals(per_day[d])) for d in sorted(per_day)]
    rep.tables.append(Table(_("Day by day"), [Col("day", _("Business date"), "date")]
                            + money_cols(_("Balance of the day (+ unpaid, − overpaid)")), day_rows))
    D_COLS = [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash")), ("DESTOCK", _("Destockage")),
              ("FLOAT", _("Float supply")), ("supplied", _("Total supplied")), ("P_CASH", _("Paid cash")),
              ("P_UVE", _("Paid UVE")), ("P_MTN", _("Paid float (MTN)")), ("P_BANK", _("Paid bank")),
              ("paid", _("Total paid"))]
    desk_rows = [dict(cashier=labels.get(pk) or str(desks[pk]), **{k: v[k] for k, _l2 in D_COLS})
                 for pk, v in sorted(per_desk.items(), key=lambda x: labels.get(x[0], ""))]
    rep.tables.append(Table(_("Per cashier / finance, by product and payment method"),
                            [Col("cashier", _("Recorded by"))] + [Col(k, lbl, "money", True) for k, lbl in D_COLS],
                            desk_rows))
    t_all = defaultdict(Decimal)
    for r in day_rows:
        for k in keys + ["net"]:
            t_all[k] += r[k]
    rep.summary = [(_("Supplied · %(c)s") % {"c": labels[pk]}, t_all[f"s_{pk}"], "money") for pk in s_desks]
    rep.summary += [(_("Float supplied (finance)"), t_all["float_sup"], "money"),
                    (_("Total supplied"), t_all["supplied"], "money")]
    rep.summary += [(_("Paid · %(c)s") % {"c": labels[pk]}, t_all[f"p_{pk}"], "money") for pk in p_desks]
    rep.summary += [(_("Paid in float"), t_all["paid_float"], "money"), (_("Total paid"), t_all["paid"], "money"),
                    (_("Balance for the period (+ owes, − overpaid)"), t_all["net"], "money"),
                    (_("Total balance today, all desks (+ owes, − credit)"), bal_all, "money")]
    return rep


def pos_stock(f):
    """Orange stock of agents and POS as last imported, grouped by subzone; POS below the ER threshold flagged."""
    from core.services.branding import company

    limit = company().out_of_stock_er
    rep = Report("pos_stock", _("POS and agent stock levels"), f.text())
    cps = Counterparty.objects.filter(stock_at__isnull=False, active=True).select_related(
        "microzone__subzone", "microzone__agent")
    if f.customer:
        cps = cps.filter(pk=f.customer.pk)
    if getattr(f, "subzone", None):
        cps = cps.filter(microzone__subzone=f.subzone)
    if getattr(f, "master_zone", None):
        cps = cps.filter(master_zone=f.master_zone)
    rows, out_n = [], 0
    for cp in cps:
        oos = cp.kind == Counterparty.Kind.POS and cp.out_of_stock(limit)
        out_n += oos
        rows.append(dict(_cp=cp, customer=f"{cp.name} · {cp.sim_display}" + (" (POS)" if cp.kind == "POS" else ""),
                         agent=cp.agent_in_charge.name if cp.kind == "POS" and cp.agent_in_charge else "",
                         er=cp.er_stock or Z, uve=cp.uve_stock or Z, commission=cp.commission_balance or Z,
                         status=str(_("OUT OF STOCK")) if oos else (str(_("in stock")) if cp.kind == "POS" else ""),
                         at=cp.stock_at))
    cols = [Col("customer", _l("Agent / POS")), Col("microzones", _l("Microzones")),
            Col("agent", _l("Agent in charge")), Col("er", _l("ER stock"), "money", True),
            Col("uve", _l("UVE stock"), "money", True), Col("commission", _l("Commission balance"), "money", True),
            Col("status", _l("Status")), Col("at", _l("Imported"), "datetime")]
    rep.tables.append(group_by_subzone(_("Stock by subzone"), rows, cols))
    oos_rows = [r for r in rows if r["status"] == str(_("OUT OF STOCK"))]
    for r in oos_rows:
        r["_k"] = r["_cp"].agent_in_charge.name if r["_cp"].agent_in_charge else str(_("No agent in charge"))
    ocols = [Col("customer", _l("POS")), Col("microzones", _l("Microzones")), Col("er", _l("ER stock"), "money", True),
             Col("uve", _l("UVE stock"), "money", True), Col("at", _l("Imported"), "datetime")]
    rep.tables.append(Table(_("POS out of stock, by agent in charge"), ocols,
                            group_rows(oos_rows, ocols, key=lambda r: r["_k"])))
    tot = rep.tables[0].totals() or {}
    rep.summary = [(_("POS out of stock"), str(out_n), "text"),
                   (_("Out of stock when ER is below"), limit, "money"),
                   (_("ER stock"), tot.get("er", Z), "money"), (_("UVE stock"), tot.get("uve", Z), "money"),
                   (_("Commission balance"), tot.get("commission", Z), "money")]
    return rep


def pos_coverage(f):
    """Per commercial agent: how many different POS he supplied on the last day of the period, in its week, its month
    and over the whole period (a POS supplied several times counts once), against the POS in his charge."""
    from datetime import timedelta

    from core.models import AgentClientEntry as E
    from core.services import agents as ag

    from core.services.days import douala_today

    day = f.date_to or douala_today()
    start = f.date_from or day.replace(day=1)
    week, month = day - timedelta(days=day.weekday()), day.replace(day=1)
    rep = Report("pos_coverage", _("POS supplied by commercial agents"), f.text())
    agents = Counterparty.objects.filter(kind=Counterparty.Kind.AGENT, active=True).select_related(
        "microzone__subzone")
    if f.customer:
        agents = agents.filter(pk=f.customer.pk)
    if getattr(f, "master_zone", None):
        agents = agents.filter(Q(master_zone=f.master_zone) | Q(microzone__subzone__parent=f.master_zone))
    ids = [a.pk for a in agents]
    charge = dict(Counterparty.objects.filter(kind=Counterparty.Kind.POS, active=True, microzone__agent_id__in=ids)
                  .values("microzone__agent").annotate(n=Count("id")).values_list("microzone__agent", "n"))
    d, w, m = (ag.unique_pos(ids, a, day) for a in (day, week, month))
    p = ag.unique_pos(ids, start, day)
    sup = dict(E.objects.filter(agent_id__in=ids, kind=E.Kind.SUPPLY, pos__isnull=False, cancelled_at__isnull=True,
                                business_date__gte=start, business_date__lte=day)
               .values("agent").annotate(n=Count("id")).values_list("agent", "n"))
    rows = []
    for a in agents:
        n = charge.get(a.pk, 0)
        rows.append(dict(_cp=a, customer=f"{a.name} · {a.sim_display}", in_charge=n, day=d.get(a.pk, 0),
                         week=w.get(a.pk, 0), month=m.get(a.pk, 0), period=p.get(a.pk, 0),
                         supplies=sup.get(a.pk, 0),
                         pct=f"{round(100 * m.get(a.pk, 0) / n)}%" if n else "—"))
    cols = [Col("customer", _l("Commercial agent")), Col("microzones", _l("Microzones")),
            Col("in_charge", _l("POS in his charge"), "int", True),
            Col("day", _("Different POS supplied on %(d)s") % {"d": day.strftime("%d/%m")}, "int", True),
            Col("week", _("This week (since %(d)s)") % {"d": week.strftime("%d/%m")}, "int", True),
            Col("month", _("This month (since %(d)s)") % {"d": month.strftime("%d/%m")}, "int", True),
            Col("pct", _l("% of his POS this month")),
            Col("period", _l("Over the period"), "int", True), Col("supplies", _l("Supplies to POS"), "int", True)]
    rep.tables.append(group_by_subzone(_("Different POS supplied, by subzone"), rows, cols))
    tot = rep.tables[0].totals() or {}
    rep.summary = [(_("POS in the agents' charge"), str(tot.get("in_charge", 0)), "text"),
                   (_("Supplied on %(d)s") % {"d": day.strftime("%d/%m")}, str(tot.get("day", 0)), "text"),
                   (_("This week"), str(tot.get("week", 0)), "text"), (_("This month"), str(tot.get("month", 0)), "text"),
                   (_("Over the period"), str(tot.get("period", 0)), "text")]
    return rep


REPORTS = {
    "statement": (_l("Agent / POS account statement"), statement),
    "customers": (_l("Agent and POS balances"), customers),
    "pos": (_l("POS transactions and outstanding"), pos),
    "supplies": (_l("Supplies (UVE, ER, cash)"), supplies),
    "collections": (_l("Collections by payment method"), collections),
    "destockage": (_l("Destockage"), destockage),
    "cash_managers": (_l("Cash-manager exchanges"), cash_managers),
    "expenses": (_l("Expenses by category"), expenses),
    "accounts": (_l("Drawer and SIM wallet movements"), accounts),
    "transfers": (_l("Transfer reconciliation"), transfers),
    "conversions": (_l("Conversions and rates"), conversions),
    "supply_due": (_l("Supply due to agents and POS"), supply_due),
    "corrections": (_l("Corrections and responsibility transfers"), corrections),
    "closings": (_l("Daily closings and differences"), closings),
    "reconciliation": (_l("Reconciliation"), reconciliation),
    "daily_reconciliation": (_l("Daily reconciliation"), daily_reconciliation),
    "supply_payment": (_l("Daily supplies versus payments"), supply_payment),
    "pos_stock": (_l("POS and agent stock levels"), pos_stock),
    "pos_coverage": (_l("POS supplied by commercial agents"), pos_coverage),
}


# Icon and one-line description for each report card on the reports page.
REPORT_CARDS = {
    "statement": ("statement", _l("Every supply and payment of one agent or POS, with running balance")),
    "customers": ("users", _l("What each agent and POS owes, or is owed")),
    "pos": ("store", _l("Direct POS supplies and their outstanding amounts")),
    "supplies": ("supply", _l("UVE, ER and cash supplies, agents versus POS")),
    "collections": ("payment", _l("Payments received: cash, UVE, MTN, bank")),
    "destockage": ("destock", _l("Cash to POS, UVE to agents")),
    "cash_managers": ("bank", _l("Exchanges and what is owed either way")),
    "expenses": ("expense", _l("Spending by category")),
    "accounts": ("wallet", _l("In and out of every drawer, SIM wallet and bank")),
    "transfers": ("transfer", _l("Every transfer between desks, shops and banks: who sent what to whom, sent = received")),
    "conversions": ("convert", _l("UVE converted to ER and the rates used")),
    "supply_due": ("percent", _l("Agents and POS who paid in advance")),
    "corrections": ("edit", _l("Corrected entries and changes of responsibility")),
    "closings": ("lock", _l("Counted versus expected, and open differences")),
    "reconciliation": ("chart", _l("Daily, weekly, monthly, custom or overall: opening + movements = closing")),
    "daily_reconciliation": ("calendar", _l("Each day, cash, UVE, ER and MTN float: opening + in − out = closing, "
                                             "counted and difference")),
    "supply_payment": ("payment", _l("Each day, what was supplied against what was paid, and per agent / POS")),
    "pos_stock": ("store", _l("ER, UVE and commission of each POS and agent as last imported; POS out of stock")),
    "pos_coverage": ("chart", _l("Different POS each commercial agent supplied per day, week and month")),
}
