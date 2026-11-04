"""Report definitions. Amounts in transaction listings are signed: a
correction reversal appears as a negative line on the business day it was
posted, so closed periods stay as they were and later periods carry the
correction."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum, Value
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core.models import (
    Account, CashManager, CashManagerExchange, ClosingLine, ConversionRate, Correction, Counterparty, Discrepancy,
    Entry, ExpenseCategory, PaymentMethod, Transaction, TxType,
)
from core.services import operations as ops

from .engine import Col, Report, Table

Z = Decimal("0")
K = Account.Kind


def money(v):
    return v if v is not None else Z


def _desk_user(t):
    return t.cashier or (t.cashier_day.cashier if t.cashier_day_id else t.entered_by)


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
    Col("reference", _l("Reference")), Col("type", _l("Type")), Col("customer", _l("Customer")),
    Col("details", _l("Details")), Col("cashier", _l("Cashier")), Col("cashier_id", _l("Cashier ID")),
    Col("amount", _l("Amount"), "money", True),
]


def txn_row(t):
    return dict(posted_at=t.posted_at, business_date=t.business_date, reference=t.reference,
                receipt=t.receipt_no or "", type=str(t.display_type),
                customer=str(t.counterparty) if t.counterparty_id else "", details=describe(t), cashier=station(t),
                cashier_id=station_id(t),
                amount=signed(t), corrected=hasattr(t, "correction"), _id=t.id)


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
    rep = Report("statement", _("Customer account statement"), f.text())
    cp = f.customer
    if cp is None:
        rep.tables.append(Table(_("Select a customer business SIM to see the statement."), []))
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
        elif et == TxType.COLLECTION:
            by_method[t.payment_method] += sign * t.amount
        rows.append(dict(posted_at=t.posted_at, business_date=e.business_date, reference=t.reference,
                         receipt=t.receipt_no or "", type=str(t.display_type), details=describe(t),
                         cashier=station(t), cashier_id=station_id(t), debit=e.amount if e.amount > 0 else Z,
                         credit=-e.amount if e.amount < 0 else Z, balance=running,
                         corrected=hasattr(t, "correction"), _id=t.id))
    current = ops.customer_balance(cp)
    rep.summary = [
        (_("Customer"), f"{cp} ({cp.get_kind_display()})", "text"),
        (_("Opening balance"), opening, "money"),
        (_("Closing balance (end of period)"), running, "money"),
        (_("Current balance (now)"), current, "money"),
        (_("Supplied UVE"), by_product["UVE"], "money"), (_("Supplied ER"), by_product["ER"], "money"),
        (_("Supplied cash"), by_product["CASH"], "money"), (_("Destockage"), by_product["DESTOCKAGE"], "money"),
    ] + [(_("Paid by %(m)s") % {"m": PaymentMethod(m).label}, v, "money") for m, v in sorted(by_method.items())]
    rep.tables.append(Table(_("Movements (positive = owed to company, negative balance = supply due to customer)"), [
        Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
        Col("reference", _("Reference")), Col("receipt", _("Receipt")), Col("type", _("Type")),
        Col("details", _("Details")), Col("cashier", _("Cashier")), Col("cashier_id", _("Cashier ID")),
        Col("debit", _("Supplied / owed +"), "money", True), Col("credit", _("Paid / credited −"), "money", True),
        Col("balance", _("Balance"), "money"),
    ], rows))
    return rep


def _customer_rows(f, kind=None, only=None):
    cps = Counterparty.objects.select_related("microzone__subzone", "microzone__agent").order_by("kind", "business_sim")
    if kind:
        cps = cps.filter(kind=kind)
    if f.customer:
        cps = cps.filter(pk=f.customer.pk)
    accts = {c.account.id: c for c in cps.select_related("ledger_account")}
    tq = f.date_q()
    agg = Entry.objects.filter(account_id__in=accts.keys()).values("account_id").annotate(
        cur=Sum("amount"),
        opening=Sum("amount", filter=Q(business_date__lt=f.date_from)) if f.date_from else Value(Z),
        upto=Sum("amount", filter=Q(business_date__lte=f.date_to)) if f.date_to else Sum("amount"),
        supplied=Sum("amount", filter=tq & Q(transaction__type__in=[TxType.SUPPLY, TxType.DESTOCKAGE])),
        paid=Sum("amount", filter=tq & Q(transaction__type=TxType.COLLECTION)),
        transfers=Sum("amount", filter=tq & Q(transaction__type=TxType.RESPONSIBILITY_TRANSFER)),
        other=Sum("amount", filter=tq & Q(transaction__type__in=[TxType.REVERSAL, TxType.OPENING_BALANCE])),
    )
    data = {r["account_id"]: r for r in agg}
    rows = []
    for aid, cp in accts.items():
        r = data.get(aid, {})
        closing = money(r.get("upto"))
        mz = cp.microzone
        row = dict(sim=cp.sim_display, name=cp.name, kind=cp.get_kind_display(),
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
    Col("owed", _l("Owed to company"), "money", True), Col("due", _l("Supply due to customer"), "money", True),
    Col("current", _l("Current"), "money", True),
]


def customers(f):
    rep = Report("customers", _("Customer balances (agents and POS)"), f.text())
    rep.tables.append(Table(_("Balances"), CUSTOMER_COLS, _customer_rows(f, only="nonzero")))
    return rep


def pos(f):
    rep = Report("pos", _("Exceptional direct POS transactions and outstanding amounts"), f.text())
    rep.tables.append(Table(_("POS balances"), CUSTOMER_COLS, _customer_rows(f, kind=Counterparty.Kind.POS,
                                                                             only="nonzero")))
    pos_ids = Counterparty.objects.filter(kind=Counterparty.Kind.POS).values("id")
    qs = Transaction.objects.filter(f.txn_q()).filter(Q(counterparty__in=pos_ids) | Q(counterparty_2__in=pos_ids)) \
        .select_related(*TX_RELATED).order_by("business_date", "posted_at")
    rep.tables.append(Table(_("Transactions involving POS"), TX_COLUMNS, [txn_row(t) for t in qs]))
    return rep


def supply_due(f):
    rep = Report("supply_due", _("Supply due to customers (prepaid / overpaid balances)"), f.text())
    rows = _customer_rows(f, only="due")
    rep.tables.append(Table(_("Customers with supply due (as of end of period)"), CUSTOMER_COLS, rows))
    return rep


def _summary_table(title, label, groups):
    rows = [dict(group=k, count=v[0], amount=v[1]) for k, v in groups.items()]
    return Table(title, [Col("group", label), Col("count", _("Count"), "int", True),
                         Col("amount", _("Net amount"), "money", True)], rows)


def supplies(f):
    rep = Report("supplies", _("Supplies: UVE, ER and cash (and destockage)"), f.text())
    qs = list(txns(f, [TxType.SUPPLY, TxType.DESTOCKAGE]))
    groups = {}
    for p, label in [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash")), ("DESTOCKAGE", _("Destockage"))]:
        sel = [t for t in qs if (effective_type(t) == TxType.DESTOCKAGE) == (p == "DESTOCKAGE")
               and (p == "DESTOCKAGE" or t.product == p)]
        groups[label] = (sum(1 for t in sel if t.type != TxType.REVERSAL), sum(signed(t) for t in sel))
    rep.tables.append(_summary_table(_("Totals by product"), _("Product"), groups))
    channels = {}
    for ch, label in [("AGENT", _("Supplies to commercial agents")), ("POS", _("Supplies to points of sale (POS)"))]:
        for p, plabel in [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash"))]:
            sel = [t for t in qs if effective_type(t) == TxType.SUPPLY and t.supply_channel == ch and t.product == p]
            channels[f"{label} · {plabel}"] = (sum(1 for t in sel if t.type != TxType.REVERSAL),
                                               sum(signed(t) for t in sel))
    rep.tables.append(_summary_table(_("Agents versus POS"), _("Supply"), channels))
    rep.tables.append(Table(_("Detail"), TX_COLUMNS, [txn_row(t) for t in qs]))
    return rep


def collections(f):
    rep = Report("collections", _("Collections by payment method"), f.text())
    qs = list(txns(f, [TxType.COLLECTION]))
    groups = {}
    for m in PaymentMethod:
        sel = [t for t in qs if t.payment_method == m]
        groups[m.label] = (sum(1 for t in sel if t.type != TxType.REVERSAL), sum(signed(t) for t in sel))
    rep.tables.append(_summary_table(_("Totals by method"), _("Payment method"), groups))
    rep.tables.append(Table(_("Detail"), TX_COLUMNS, [txn_row(t) for t in qs]))
    return rep


def destockage(f):
    rep = Report("destockage", _("Destockage"), f.text())
    qs = list(txns(f, [TxType.DESTOCKAGE]))
    rows = []
    for t in qs:
        r = txn_row(t)
        r["pos"] = str(t.counterparty_2)
        rows.append(r)
    cols = TX_COLUMNS[:4] + [Col("pos", _("POS (received cash)")), Col("customer", _("Agent (owes)"))] + TX_COLUMNS[5:]
    rep.tables.append(Table(_("Destockage operations (cash out of drawer → agent debt; company UVE unchanged)"),
                            cols, rows))
    return rep


def expenses(f):
    rep = Report("expenses", _("Expenses by category"), f.text())
    qs = list(txns(f, [TxType.EXPENSE]))
    groups = {}
    for c in ExpenseCategory.objects.all():
        sel = [t for t in qs if t.expense_category_id == c.id]
        if sel:
            groups[str(c)] = (sum(1 for t in sel if t.type != TxType.REVERSAL), sum(signed(t) for t in sel))
    rep.tables.append(_summary_table(_("Totals by category"), _("Category"), groups))
    rep.tables.append(Table(_("Detail"), TX_COLUMNS, [txn_row(t) for t in qs]))
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
    qs = qs.prefetch_related("entries")
    rows, pairs, parties = [], OrderedDict(), OrderedDict()
    products = []
    sent_total, received_total = Z, Z
    for t in qs:
        src, dst = t.source_account, t.dest_account
        prod = str(Account.Kind(src.kind).label) if src else "—"
        if prod not in products:
            products.append(prod)
        a, b = _party(src), _party(dst)
        rows.append(dict(business_date=t.business_date, reference=t.reference, _id=t.pk, type=t.display_type,
                         sender=a, receiver=b, product=prod, amount=t.amount, by=str(t.entered_by),
                         description=t.description))
        key = (a, b, prod)
        pr = pairs.setdefault(key, {"sender": a, "receiver": b, "product": prod, "n": 0, "amount": Z})
        pr["n"] += 1
        pr["amount"] += t.amount
        for who, side in ((a, "sent"), (b, "received")):
            p = parties.setdefault(who, defaultdict(Decimal))
            p[f"{side}_{prod}"] += t.amount
            p[side] += t.amount
        # each transfer is one balanced entry: what leaves the sender's account arrives in the receiver's
        sent_total += -sum((e.amount for e in t.entries.all() if e.account_id == t.source_account_id), Z)
        received_total += sum((e.amount for e in t.entries.all() if e.account_id == t.dest_account_id), Z)
    by_party = []
    for who, p in sorted(parties.items()):
        r = {"party": who}
        for prod in products:
            r[f"sent_{prod}"] = p.get(f"sent_{prod}", Z)
            r[f"received_{prod}"] = p.get(f"received_{prod}", Z)
        r.update(sent=p["sent"], received=p["received"], net=p["received"] - p["sent"])
        by_party.append(r)
    cols = [Col("party", _("Desk / shop"))]
    for prod in products:
        cols += [Col(f"sent_{prod}", f"{prod} · " + str(_("sent")), "money", True),
                 Col(f"received_{prod}", f"{prod} · " + str(_("received")), "money", True)]
    cols += [Col("sent", _("Total sent"), "money", True), Col("received", _("Total received"), "money", True),
             Col("net", _("Net (received − sent)"), "money", True)]
    rep.tables.append(Table(_("By desk and shop"), cols, by_party,
                            note=_("Across the whole company, total sent equals total received for each product.")))
    rep.tables.append(Table(_("Who sent what to whom"),
                            [Col("sender", _("From")), Col("receiver", _("To")), Col("product", _("Product")),
                             Col("n", _("Transfers"), "int", True), Col("amount", _("Amount"), "money", True)],
                            list(pairs.values())))
    rep.tables.append(Table(_("All transfers"),
                            [Col("business_date", _("Date"), "date"), Col("reference", _("Reference")),
                             Col("type", _("Type")), Col("sender", _("From")), Col("receiver", _("To")),
                             Col("product", _("Product")), Col("amount", _("Amount"), "money", True),
                             Col("by", _("Recorded by")), Col("description", _("Note"))], rows))
    ok = sent_total == received_total
    rep.summary = [(_("Transfers"), str(len(rows)), "text"), (_("Total sent"), sent_total, "money"),
                   (_("Total received"), received_total, "money"),
                   (_("Check"), _("OK — sent equals received") if ok else _("MISMATCH"), "text")]
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
                         _id=t.id))
    rep.tables.append(Table(_("Conversions"), [
        Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
        Col("reference", _("Reference")), Col("type", _("Type")), Col("sim", _("Orange SIM")),
        Col("cashier", _("Cashier")), Col("rate", _("Uplift rate used"), "rate"),
        Col("uve", _("UVE converted"), "money", True), Col("er", _("ER received"), "money", True),
        Col("uplift", _("Conversion uplift"), "money", True)], rows))
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
    rep.tables.append(Table(_("Exchange agreements (no money moves until a leg is recorded)"), [
        Col("reference", _("Reference")), Col("business_date", _("Business date"), "date"),
        Col("cm", _("Cash manager")), Col("direction", _("Ref number")), Col("amount", _("Amount"), "money", True),
        Col("given", _("Company gave"), "money", True), Col("received", _("Amount served"), "money", True),
        Col("to_serve", _("Balance to be served"), "money", True),
        Col("status", _("Status")), Col("by", _("Recorded by"))], ex_rows))
    legs = txns(f, [TxType.CM_GIVE, TxType.CM_RECEIVE])
    rep.tables.append(Table(_("Completed legs"), TX_COLUMNS, [txn_row(t) for t in legs]))
    return rep


def accounts(f):
    rep = Report("accounts", _("Cash drawer, SIM wallet and bank movements"), f.text())
    accs = list(scoped_asset_accounts(f))
    bals = balances_by_account(accs, f)
    rows = []
    for a in accs:
        o, dr, cr, cl, cur = bals[a.id]
        rows.append(dict(account=str(a), kind=a.get_kind_display(),
                         custodian=(f"{a.zone} · {a.custodian}" if a.custodian_id else str(a.zone or "")), opening=o,
                         inflow=dr, outflow=cr, closing=cl, current=cur))
    rep.tables.append(Table(_("Balances"), [
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
    rep.tables.append(Table(_("Movements"), [
        Col("account", _("Account")), Col("business_date", _("Business date"), "date"),
        Col("posted_at", _("Recorded at"), "datetime"), Col("reference", _("Reference")), Col("type", _("Type")),
        Col("details", _("Details")), Col("cashier", _("Cashier")), Col("inflow", _("In"), "money", True),
        Col("outflow", _("Out"), "money", True)], erows))
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
    rep.tables.append(Table(_("Corrections"), [
        Col("created_at", _("Corrected at"), "datetime"), Col("original", _("Original")),
        Col("original_date", _("Original business date"), "date"), Col("type", _("Type")),
        Col("kind", _("Correction")), Col("reason", _("Reason")), Col("changes", _("Changes")),
        Col("reversal", _("Reversal")), Col("replacement", _("Replacement")),
        Col("posted_date", _("Posted on business date"), "date"), Col("by", _("Corrected by"))], rows))
    tr = txns(f, [TxType.RESPONSIBILITY_TRANSFER])
    trows = []
    for t in tr:
        r = txn_row(t)
        r["from_party"] = str(t.counterparty_2)
        r["supply"] = t.related_supply.reference if t.related_supply_id else (
            t.reverses.related_supply.reference if t.reverses_id and t.reverses.related_supply_id else "")
        r["by"] = str(t.entered_by)
        trows.append(r)
    rep.tables.append(Table(_("Responsibility transfers"), [
        Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
        Col("reference", _("Reference")), Col("supply", _("Supply")), Col("from_party", _("From")),
        Col("customer", _("To")), Col("details", _("Reason / details")), Col("by", _("Changed by")),
        Col("amount", _("Amount"), "money", True)], trows))
    return rep


def closings(f):
    rep = Report("closings", _("Daily closings and unresolved differences"), f.text())
    lq = Q()
    if f.date_from:
        lq &= Q(closing__cashier_day__business_date__gte=f.date_from)
    if f.date_to:
        lq &= Q(closing__cashier_day__business_date__lte=f.date_to)
    lq &= f.day_zone_q("closing__cashier_day")
    rows = []
    for ln in ClosingLine.objects.filter(lq).select_related("closing__cashier_day__zone", "closing__closed_by",
                                                            "account__sim") \
            .order_by("closing__cashier_day__business_date", "closing__cashier_day__zone__name", "account__kind"):
        rows.append(dict(business_date=ln.closing.cashier_day.business_date,
                         cashier=f"{ln.closing.cashier_day.zone or '—'} · {ln.closing.closed_by}",
                         closed_at=ln.closing.closed_at,
                         account=str(ln.account), opening=ln.opening_book, movements=ln.movements,
                         expected=ln.expected, actual=ln.actual, difference=ln.difference,
                         carried=ln.carried_unresolved, new=ln.new_difference))
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
    rep.tables.append(Table(_("Unresolved differences (all dates)"), [
        Col("date", _("Business date"), "date"), Col("cashier", _("Subzone")), Col("account", _("Account")),
        Col("kind", _("Type")), Col("amount", _("Amount"), "money", True),
        Col("notes", _("Investigation notes"), "int")], drows))
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
        rows.append(dict(zone=g["name"], level=_("Master zone"), **{k: g["tot"][k] for k in g["tot"]}, _bold=True))
        for ln in sorted(g["lines"].values(), key=lambda x: (x["level"] != _("Subzone"), x["name"])):
            rows.append(dict(zone="   " + ln["name"], level=ln["level"], **{k: ln["tot"][k] for k in ln["tot"]}))
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
        row = dict(account=str(a), opening=o, closing=cl, current=cur)
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
    rep.tables.append(_zone_summary(f))
    rep.tables.append(Table(_("Company assets: opening + period movements = closing"), cols, rows))

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
        (_("Customers owe the company (end of period)"), owed, "money"),
        (_("Supply due to customers (end of period)"), due, "money"),
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
    rep.tables.append(Table(_("Day by day: opening + in − out = closing"), [
        Col("day", _("Business date"), "date"), Col("product", _("Product")),
        Col("opening", _("Opening"), "money"), Col("money_in", _("In"), "money", True),
        Col("money_out", _("Out"), "money", True), Col("closing", _("Closing (book)"), "money"),
        Col("counted", _("Counted at closing"), "money"), Col("difference", _("Difference"), "money", True),
        Col("status", _("Day"))], rows))
    from core.money import fmt

    rep.summary = [(_("%(p)s at the end (in %(i)s, out %(o)s)") % {"p": label, "i": fmt(totals[k][0]),
                                                                    "o": fmt(totals[k][1])}, running[k], "money")
                   for k, label in DAILY_KINDS]
    return rep


def _base(t):
    return t.reverses if t.type == TxType.REVERSAL else t


def supply_payment(f):
    """Day by day: what was supplied (UVE, ER, cash, destockage) against what was paid (cash, UVE, MTN, bank), and the
    same per agent / POS over the period."""
    rep = Report("supply_payment", _("Daily supplies versus payments"), f.text())
    sup = list(txns(f, [TxType.SUPPLY, TxType.DESTOCKAGE]))
    pay = list(txns(f, [TxType.COLLECTION]))
    S_COLS = [("UVE", "UVE"), ("ER", "ER"), ("CASH", _("Cash")), ("DESTOCK", _("Destockage"))]
    P_COLS = [("P_CASH", _("Paid cash")), ("P_UVE", _("Paid UVE")), ("P_MTN", _("Paid MTN")),
              ("P_BANK", _("Paid bank"))]
    per_day = defaultdict(lambda: defaultdict(Decimal))
    per_cust = defaultdict(lambda: defaultdict(Decimal))
    for t in sup:
        b = _base(t)
        key = "DESTOCK" if b.type == TxType.DESTOCKAGE else b.product
        per_day[t.business_date][key] += signed(t)
        per_day[t.business_date]["supplied"] += signed(t)
        if b.counterparty_id:
            per_cust[b.counterparty]["supplied"] += signed(t)
    for t in pay:
        b = _base(t)
        per_day[t.business_date]["P_" + b.payment_method] += signed(t)
        per_day[t.business_date]["paid"] += signed(t)
        if b.counterparty_id:
            per_cust[b.counterparty]["paid"] += signed(t)
    rows = []
    for d in sorted(per_day):
        v = per_day[d]
        row = dict(day=d, supplied=v["supplied"], paid=v["paid"], net=v["supplied"] - v["paid"])
        row.update({k: v[k] for k, _lbl in S_COLS + P_COLS})
        rows.append(row)
    cols = [Col("day", _("Business date"), "date")]
    cols += [Col(k, lbl, "money", True) for k, lbl in S_COLS]
    cols += [Col("supplied", _("Total supplied"), "money", True)]
    cols += [Col(k, lbl, "money", True) for k, lbl in P_COLS]
    cols += [Col("paid", _("Total paid"), "money", True), Col("net", _("Supplied − paid"), "money", True)]
    rep.tables.append(Table(_("Day by day"), cols, rows))
    from core.services.ledger import balance

    crow = []
    for cp, v in sorted(per_cust.items(), key=lambda x: -(x[1]["supplied"] - x[1]["paid"])):
        crow.append(dict(customer=str(cp), kind=cp.get_kind_display(), supplied=v["supplied"], paid=v["paid"],
                         net=v["supplied"] - v["paid"], balance=balance(cp.account)))
    rep.tables.append(Table(_("Per agent / POS over the period"), [
        Col("customer", _("Agent / POS")), Col("kind", _("Type")), Col("supplied", _("Supplied"), "money", True),
        Col("paid", _("Paid"), "money", True), Col("net", _("Supplied − paid"), "money", True),
        Col("balance", _("Owes now (all desks)"), "money", True)], crow))
    ts, tp = sum(r["supplied"] for r in rows), sum(r["paid"] for r in rows)
    rep.summary = [(_("Total supplied"), ts, "money"), (_("Total paid"), tp, "money"),
                   (_("Supplied − paid"), ts - tp, "money")]
    return rep

REPORTS = {
    "statement": (_l("Customer account statement"), statement),
    "customers": (_l("Customer balances"), customers),
    "pos": (_l("POS transactions and outstanding"), pos),
    "supplies": (_l("Supplies (UVE, ER, cash)"), supplies),
    "collections": (_l("Collections by payment method"), collections),
    "destockage": (_l("Destockage"), destockage),
    "cash_managers": (_l("Cash-manager exchanges"), cash_managers),
    "expenses": (_l("Expenses by category"), expenses),
    "accounts": (_l("Drawer and SIM wallet movements"), accounts),
    "transfers": (_l("Transfer reconciliation"), transfers),
    "conversions": (_l("Conversions and rates"), conversions),
    "supply_due": (_l("Supply due to customers"), supply_due),
    "corrections": (_l("Corrections and responsibility transfers"), corrections),
    "closings": (_l("Daily closings and differences"), closings),
    "reconciliation": (_l("Reconciliation"), reconciliation),
    "daily_reconciliation": (_l("Daily reconciliation"), daily_reconciliation),
    "supply_payment": (_l("Daily supplies versus payments"), supply_payment),
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
    "supply_due": ("percent", _l("Customers who paid in advance")),
    "corrections": ("edit", _l("Corrected entries and changes of responsibility")),
    "closings": ("lock", _l("Counted versus expected, and open differences")),
    "reconciliation": ("chart", _l("Daily, weekly, monthly, custom or overall: opening + movements = closing")),
    "daily_reconciliation": ("calendar", _l("Each day, cash, UVE, ER and MTN float: opening + in − out = closing, "
                                             "counted and difference")),
    "supply_payment": ("payment", _l("Each day, what was supplied against what was paid, and per agent / POS")),
}
