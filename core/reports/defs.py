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


def station(t):
    if t.cashier_day_id:
        return str(t.cashier_day.cashier)
    return str(t.entered_by)


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


TX_RELATED = ["entered_by", "cashier_day__cashier", "counterparty", "counterparty_2", "source_account__sim",
              "dest_account__sim", "reverses", "replaces", "expense_category", "related_supply", "correction",
              "cash_manager", "exchange"]

TX_COLUMNS = [
    Col("posted_at", _("Recorded at"), "datetime"), Col("business_date", _("Business date"), "date"),
    Col("reference", _("Reference")), Col("type", _("Type")), Col("customer", _("Customer")),
    Col("details", _("Details")), Col("cashier", _("Cashier")), Col("amount", _("Amount"), "money", True),
]


def txn_row(t):
    return dict(posted_at=t.posted_at, business_date=t.business_date, reference=t.reference,
                receipt=t.receipt_no or "", type=str(t.display_type),
                customer=str(t.counterparty) if t.counterparty_id else "", details=describe(t), cashier=station(t),
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
    qs = Account.objects.filter(kind__in=kinds).select_related("sim", "custodian", "bank")
    if f.cashier:
        qs = qs.filter(Q(custodian=f.cashier) | Q(grants__user=f.cashier)).distinct()
    if f.subzone:
        qs = qs.filter(custodian__subzone=f.subzone)
    if f.master_zone:
        qs = qs.filter(Q(zone=f.master_zone) | Q(custodian__subzone__parent=f.master_zone))
    return qs.order_by("kind", "id")


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
                         cashier=station(t), debit=e.amount if e.amount > 0 else Z,
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
        Col("details", _("Details")), Col("cashier", _("Cashier")),
        Col("debit", _("Supplied / owed +"), "money", True), Col("credit", _("Paid / credited −"), "money", True),
        Col("balance", _("Balance"), "money"),
    ], rows))
    return rep


def _customer_rows(f, kind=None, only=None):
    cps = Counterparty.objects.all().order_by("kind", "business_sim")
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
        row = dict(sim=cp.sim_display, name=cp.name, kind=cp.get_kind_display(), opening=money(r.get("opening")),
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
    Col("sim", _("Business SIM")), Col("name", _("Name")), Col("kind", _("Type")),
    Col("opening", _("Opening"), "money", True), Col("supplied", _("Supplied"), "money", True),
    Col("paid", _("Paid"), "money", True), Col("transfers", _("Resp. transfers"), "money", True),
    Col("other", _("Corrections / opening"), "money", True), Col("closing", _("Closing"), "money", True),
    Col("owed", _("Owed to company"), "money", True), Col("due", _("Supply due to customer"), "money", True),
    Col("current", _("Current"), "money", True),
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


def transfers(f):
    rep = Report("transfers", _("Internal transfers"), f.text())
    qs = txns(f, [TxType.INTERNAL_TRANSFER])
    rep.tables.append(Table(_("Transfers between company accounts"), TX_COLUMNS, [txn_row(t) for t in qs]))
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
                            direction=ex.get_direction_display(), amount=ex.amount, given=give, received=recv,
                            status=ex.status, by=str(ex.created_by)))
    rep.tables.append(Table(_("Exchange agreements (no money moves until a leg is recorded)"), [
        Col("reference", _("Reference")), Col("business_date", _("Business date"), "date"),
        Col("cm", _("Cash manager")), Col("direction", _("Direction")), Col("amount", _("Agreed"), "money", True),
        Col("given", _("Company gave"), "money", True), Col("received", _("Company received"), "money", True),
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
        rows.append(dict(account=str(a), kind=a.get_kind_display(), custodian=str(a.custodian or ""), opening=o,
                         inflow=dr, outflow=cr, closing=cl, current=cur))
    rep.tables.append(Table(_("Balances"), [
        Col("account", _("Account")), Col("kind", _("Type")), Col("custodian", _("Custodian")),
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
        cq &= Q(original__cashier_day__cashier=f.cashier) | Q(original__entered_by=f.cashier)
    if f.subzone:
        cq &= Q(original__cashier_day__cashier__subzone=f.subzone)
    if f.master_zone:
        cq &= Q(original__cashier_day__cashier__subzone__parent=f.master_zone)
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
    if f.cashier:
        lq &= Q(closing__cashier_day__cashier=f.cashier)
    if f.subzone:
        lq &= Q(closing__cashier_day__cashier__subzone=f.subzone)
    if f.master_zone:
        lq &= Q(closing__cashier_day__cashier__subzone__parent=f.master_zone)
    rows = []
    for ln in ClosingLine.objects.filter(lq).select_related("closing__cashier_day__cashier", "account__sim") \
            .order_by("closing__cashier_day__business_date", "closing__cashier_day__cashier", "account__kind"):
        rows.append(dict(business_date=ln.closing.cashier_day.business_date,
                         cashier=str(ln.closing.cashier_day.cashier), closed_at=ln.closing.closed_at,
                         account=str(ln.account), opening=ln.opening_book, movements=ln.movements,
                         expected=ln.expected, actual=ln.actual, difference=ln.difference,
                         carried=ln.carried_unresolved, new=ln.new_difference))
    rep.tables.append(Table(_("Closing lines (book balance carries forward)"), [
        Col("business_date", _("Business date"), "date"), Col("cashier", _("Cashier")),
        Col("closed_at", _("Closed at"), "datetime"), Col("account", _("Account")),
        Col("opening", _("Opening (book)"), "money"), Col("movements", _("Movements"), "money"),
        Col("expected", _("Expected"), "money"), Col("actual", _("Actual"), "money"),
        Col("difference", _("Difference"), "money", True), Col("carried", _("Carried unresolved"), "money"),
        Col("new", _("New difference"), "money", True)], rows))
    dq = Q(status=Discrepancy.Status.OPEN)
    if f.cashier:
        dq &= Q(closing_line__closing__cashier_day__cashier=f.cashier)
    if f.subzone:
        dq &= Q(closing_line__closing__cashier_day__cashier__subzone=f.subzone)
    if f.master_zone:
        dq &= Q(closing_line__closing__cashier_day__cashier__subzone__parent=f.master_zone)
    drows = [dict(date=d.closing_line.closing.cashier_day.business_date,
                  cashier=str(d.closing_line.closing.cashier_day.cashier), account=str(d.account),
                  kind=str(d.kind_label), amount=d.amount, notes=d.notes.count(), _disc=d.id)
             for d in Discrepancy.objects.filter(dq).select_related("closing_line__closing__cashier_day__cashier",
                                                                     "account__sim")]
    rep.tables.append(Table(_("Unresolved differences (all dates)"), [
        Col("date", _("Business date"), "date"), Col("cashier", _("Cashier")), Col("account", _("Account")),
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
]


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
    "transfers": (_l("Internal transfers"), transfers),
    "conversions": (_l("Conversions and rates"), conversions),
    "supply_due": (_l("Supply due to customers"), supply_due),
    "corrections": (_l("Corrections and responsibility transfers"), corrections),
    "closings": (_l("Daily closings and differences"), closings),
    "reconciliation": (_l("Reconciliation (daily/weekly/monthly/custom/overall)"), reconciliation),
}
