"""Shop summary reports: daily, weekly and monthly (and custom dates), for all shops or one shop.

Daily   — the chosen day, one line per shop.
Weekly  — Monday to Sunday of the chosen date, one line per day, plus the shops' totals.
Monthly — the month of the chosen date, one line per week, plus the shops' totals.
Custom  — the chosen dates, one line per day, plus the shops' totals.
Every line shows the activity (cash-in/out, commissions, airtime normal / good deal, conversions, agents,
sales, services, SIM retrievals, capital), the staff hours, the counted closing differences and the closing
balances (cash, UVE, ER) at the end of the line's period."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.db.models import Count, Sum
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core.models import (
    Account, Entry, ServiceCount, Shop, ShopShift, SimRetrieval, StockUnit, Transaction, TxType,
)
from core.services.days import douala_today
from core.simnumbers import display_sim

from .engine import Col, Report, Table

K = Account.Kind
Z = Decimal(0)
T = TxType
KINDS = ((K.CASH_DRAWER, "cash"), (K.UVE, "uve"), (K.ER, "er"))
PERIODS = [("day", _l("Daily")), ("week", _l("Weekly")), ("month", _l("Monthly")), ("custom", _l("Custom dates"))]
MONTHS = [_l("January"), _l("February"), _l("March"), _l("April"), _l("May"), _l("June"), _l("July"),
          _l("August"), _l("September"), _l("October"), _l("November"), _l("December")]


KINDS_OF_REPORT = [("summary", _l("Activity summary")), ("income", _l("Income statement"))]


class ShopReportForm(forms.Form):
    kind = forms.ChoiceField(label=_l("Which report"), choices=KINDS_OF_REPORT, required=False, initial="summary")
    period = forms.ChoiceField(label=_l("Report"), choices=PERIODS, required=False, initial="day")
    day = forms.DateField(label=_l("Date"), required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_from = forms.DateField(label=_l("From (custom)"), required=False,
                                widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label=_l("To (custom)"), required=False, widget=forms.DateInput(attrs={"type": "date"}))
    shop = forms.ModelChoiceField(label=_l("Shop"), required=False, queryset=Shop.objects.none(),
                                  empty_label=_l("All shops"))

    def __init__(self, shops, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shop"].queryset = shops
        if shops.count() == 1:
            self.fields.pop("shop")


def resolve(form, shops):
    """-> (period, d_from, d_to, shops, buckets [(label, start, end)])"""
    cd = form.cleaned_data if form.is_bound and form.is_valid() else {}
    period = cd.get("period") or "day"
    day = cd.get("day") or douala_today()
    if cd.get("shop"):
        shops = shops.filter(pk=cd["shop"].pk)
    if period == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
        buckets = [(_day_label(start + timedelta(days=i)), start + timedelta(days=i), start + timedelta(days=i))
                   for i in range(7)]
    elif period == "month":
        start = day.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1)) - timedelta(days=1)
        buckets, s = [], start
        while s <= end:
            e = min(s + timedelta(days=6 - s.weekday()), end)
            buckets.append((_("Week %(a)s–%(b)s") % {"a": s.strftime("%d/%m"), "b": e.strftime("%d/%m")}, s, e))
            s = e + timedelta(days=1)
    elif period == "custom":
        start = cd.get("date_from") or day
        end = cd.get("date_to") or start
        if end < start:
            start, end = end, start
        if (end - start).days > 92:
            end = start + timedelta(days=92)
        buckets = [(_day_label(start + timedelta(days=i)), start + timedelta(days=i), start + timedelta(days=i))
                   for i in range((end - start).days + 1)]
    else:
        start = end = day
        buckets = []
    return period, start, end, shops, buckets


def _day_label(d):
    return d.strftime("%d/%m/%Y")


def _period_title(period, start, end):
    if period == "day":
        return _("Daily shop report — %(d)s") % {"d": start.strftime("%d/%m/%Y")}
    if period == "week":
        return _("Weekly shop report — %(a)s to %(b)s") % {"a": start.strftime("%d/%m/%Y"),
                                                           "b": end.strftime("%d/%m/%Y")}
    if period == "month":
        return _("Monthly shop report — %(m)s %(y)s") % {"m": MONTHS[start.month - 1], "y": start.year}
    return _("Shop report — %(a)s to %(b)s") % {"a": start.strftime("%d/%m/%Y"), "b": end.strftime("%d/%m/%Y")}


METRICS = ["cashin", "cashin_n", "cashout", "cashout_n", "commission", "airtime", "good_deal", "convert", "appro",
           "er_supply", "destock", "agent_paid", "agent_repaid", "to_cashiers", "from_cashiers", "capital", "expenses",
           "purchases", "sales",
           "units", "services", "retrievals", "hours", "diff_cash", "diff_uve", "diff_er"]
TYPE_METRIC = {T.SHOP_CASHIN: "cashin", T.SHOP_CASHOUT: "cashout", T.SHOP_CONVERT: "convert",
               T.SHOP_APPRO: "appro", T.SHOP_ER_SUPPLY: "er_supply", T.SHOP_DESTOCK: "destock",
               T.SHOP_AGENT_PAY: "agent_paid", T.SHOP_AGENT_REPAY: "agent_repaid", T.SHOP_TO_DESK: "to_cashiers",
               T.DESK_TO_SHOP: "from_cashiers", T.SHOP_SALE: "sales", T.SHOP_EXPENSE: "expenses",
               T.SHOP_PURCHASE: "purchases"}


def _daily(shops, d_from, d_to, hours_of=None):
    """{(shop_id, date): {metric: value}} for the whole range in a handful of queries."""
    data = defaultdict(lambda: defaultdict(lambda: Z))
    shop_ids = [s.pk for s in shops]
    txq = Transaction.objects.filter(shop_id__in=shop_ids, business_date__gte=d_from, business_date__lte=d_to,
                                     correction__isnull=True).exclude(type=T.REVERSAL)
    for r in txq.values("shop_id", "business_date", "type", "subtype").annotate(
            n=Count("id"), amt=Sum("amount"), com=Sum("commission")):
        d = data[(r["shop_id"], r["business_date"])]
        t, amt = r["type"], r["amt"] or Z
        if t == T.SHOP_AIRTIME:
            d["airtime"] += amt
            if r["subtype"] == "GOOD_DEAL":
                d["good_deal"] += amt
        elif t in TYPE_METRIC:
            d[TYPE_METRIC[t]] += amt
            if t in (T.SHOP_CASHIN, T.SHOP_CASHOUT):
                d[TYPE_METRIC[t] + "_n"] += r["n"]
                d["commission"] += r["com"] or Z
    # capital: net effect on the shop's cash, UVE and ER (in − out)
    for r in Entry.objects.filter(account__shop_id__in=shop_ids, account__shop_agent__isnull=True,
                                  account__kind__in=[k for k, _x in KINDS], business_date__gte=d_from,
                                  business_date__lte=d_to,
                                  transaction__type__in=[T.CAPITAL_IN, T.CAPITAL_OUT, T.CAPITAL_MOVE],
                                  transaction__correction__isnull=True) \
            .values("account__shop_id", "business_date").annotate(s=Sum("amount")):
        data[(r["account__shop_id"], r["business_date"])]["capital"] += r["s"]
    for r in ServiceCount.objects.filter(shop_id__in=shop_ids, business_date__gte=d_from, business_date__lte=d_to) \
            .values("shop_id", "business_date").annotate(n=Sum("count")):
        data[(r["shop_id"], r["business_date"])]["services"] += r["n"]
    for r in SimRetrieval.objects.filter(shop_id__in=shop_ids, business_date__gte=d_from, business_date__lte=d_to) \
            .values("shop_id", "business_date").annotate(n=Count("id")):
        data[(r["shop_id"], r["business_date"])]["retrievals"] += r["n"]
    for r in StockUnit.objects.filter(sale__shop_id__in=shop_ids, sale__transaction__business_date__gte=d_from,
                                      sale__transaction__business_date__lte=d_to,
                                      sale__transaction__correction__isnull=True) \
            .values("sale__shop_id", "sale__transaction__business_date").annotate(n=Count("id")):
        data[(r["sale__shop_id"], r["sale__transaction__business_date"])]["units"] += r["n"]
    last_close = {}
    shifts = ShopShift.objects.filter(shop_id__in=shop_ids, business_date__gte=d_from, business_date__lte=d_to)
    if hours_of is not None:  # a staff member with some duties: only their own hours
        shifts = shifts.filter(user=hours_of)
    for s in shifts:
        d = data[(s.shop_id, s.business_date)]
        d["hours"] += Decimal(str(s.hours))
        if s.status == ShopShift.Status.CLOSED and s.close_cash is not None:
            key = (s.shop_id, s.business_date)
            if key not in last_close or s.closed_at > last_close[key].closed_at:
                last_close[key] = s
    for key, s in last_close.items():
        diffs = s.close_diffs
        for k in ("cash", "uve", "er"):
            data[key][f"diff_{k}"] += diffs[k] or Z
    return data


def _closing(shops, upto):
    """{shop_id: {"cash": .., "uve": .., "er": ..}} balances at the end of `upto`."""
    out = defaultdict(lambda: {"cash": Z, "uve": Z, "er": Z})
    names = dict(KINDS)
    for r in Entry.objects.filter(account__shop__in=shops, account__shop_agent__isnull=True,
                                  account__kind__in=list(names), business_date__lte=upto) \
            .values("account__shop_id", "account__kind").annotate(s=Sum("amount")):
        out[r["account__shop_id"]][names[r["account__kind"]]] = r["s"] or Z
    return out


def _cols_activity(first_key, first_label):
    return [Col(first_key, first_label),
            Col("cashin", _("Cash-in"), "money", True), Col("cashin_n", _("Nb"), "int", True),
            Col("cashout", _("Cash-out"), "money", True), Col("cashout_n", _("Nb"), "int", True),
            Col("commission", _("Commission"), "money", True),
            Col("airtime", _("Airtime"), "money", True), Col("good_deal", _("of which good deal"), "money", True),
            Col("convert", _("UVE→ER converted"), "money", True),
            Col("sales", _("Item sales"), "money", True), Col("units", _("Units sold"), "int", True),
            Col("services", _("Services done"), "int", True), Col("retrievals", _("SIM retrievals"), "int", True),
            Col("hours", _("Staff hours"), "money", True)]


def _cols_control(first_key, first_label):
    return [Col(first_key, first_label),
            Col("appro", _("Appro (UVE from agents)"), "money", True),
            Col("er_supply", _("ER supply"), "money", True), Col("destock", _("Destockage"), "money", True),
            Col("agent_paid", _("Paid to agents"), "money", True),
            Col("agent_repaid", _("Paid by agents"), "money", True),
            Col("to_cashiers", _("UVE to cashiers"), "money", True),
            Col("from_cashiers", _("UVE from cashiers"), "money", True),
            Col("capital", _("Capital (net)"), "money", True),
            Col("expenses", _("Expenses"), "money", True), Col("purchases", _("Purchases of items"), "money", True),
            Col("diff_cash", _("Cash difference (counted)"), "money", True),
            Col("diff_uve", _("UVE difference"), "money", True), Col("diff_er", _("ER difference"), "money", True),
            Col("cash_c", _("Cash at closing"), "money"), Col("uve_c", _("UVE at closing"), "money"),
            Col("er_c", _("ER at closing"), "money")]


def _row(first_key, first_value, metrics, closing):
    r = {first_key: first_value}
    for m in METRICS:
        v = metrics.get(m, Z)
        r[m] = int(v) if m in ("cashin_n", "cashout_n", "units", "services", "retrievals") else v
    r["hours"] = Decimal(r["hours"]).quantize(Decimal("0.1"))
    r.update(cash_c=closing["cash"], uve_c=closing["uve"], er_c=closing["er"])
    return r


# column -> duty needed to see it (segmented shops); columns not listed are for everyone, "ALL" = every duty
COL_DUTY = {"cashin": "OM", "cashin_n": "OM", "cashout": "OM", "cashout_n": "OM", "commission": "OM",
            "airtime": "AIRTIME", "good_deal": "AIRTIME", "convert": "AIRTIME", "sales": "SALES", "units": "SALES",
            "services": "SERVICES", "retrievals": "SERVICES", "appro": "AGENTS", "er_supply": "AGENTS",
            "destock": "AGENTS", "agent_paid": "AGENTS", "agent_repaid": "AGENTS", "to_cashiers": "AGENTS",
            "from_cashiers": "AGENTS", "capital": "ALL", "expenses": "EXPENSES", "purchases": "EXPENSES"}


def _allowed(viewer):
    """None = everything; otherwise the duties of a staff member who does not have them all."""
    from core.services.shops import is_restricted

    if viewer is None or not is_restricted(viewer):
        return None
    return set(viewer.shop_duty_set)


def _keep(cols, duties):
    if duties is None:
        return cols
    return [c for c in cols if COL_DUTY.get(c.key) is None or COL_DUTY[c.key] in duties]


def shop_summary(form, visible_shops, viewer=None):
    from core.services import shop_shifts as ss

    duties = _allowed(viewer)
    period, d_from, d_to, shops_qs, buckets = resolve(form, visible_shops)
    shops = list(shops_qs.filter(active=True).order_by("name"))
    data = _daily(shops, d_from, d_to, hours_of=viewer if duties is not None else None)
    rep = Report("shops", _period_title(period, d_from, d_to),
                 ", ".join(s.name for s in shops) if len(shops) < 6 else _("%(n)s shops") % {"n": len(shops)})
    if duties is not None:
        rep.filters_text += " · " + _("Your duties only: %(d)s") % {"d": viewer.shop_duties_display}
    # one line per shop over the whole period
    end_bal = _closing(shops, d_to)
    per_shop = []
    totals = defaultdict(lambda: Z)
    for s in shops:
        m = defaultdict(lambda: Z)
        d = d_from
        while d <= d_to:
            for k, v in data.get((s.pk, d), {}).items():
                m[k] += v
            d += timedelta(days=1)
        for k, v in m.items():
            totals[k] += v
        per_shop.append(_row("shop", s.name, m, end_bal[s.pk]))

    def add(title, cols, rows, note=""):
        cols = _keep(cols, duties)
        if len(cols) > 1:
            rep.tables.append(Table(title, cols, rows, note=note))

    # one line per day / week
    if buckets:
        lines = []
        for label, a, b in buckets:
            m = defaultdict(lambda: Z)
            for s in shops:
                d = a
                while d <= b:
                    for k, v in data.get((s.pk, d), {}).items():
                        m[k] += v
                    d += timedelta(days=1)
            bal = _closing(shops, b)
            agg = {"cash": sum((bal[s.pk]["cash"] for s in shops), Z), "uve": sum((bal[s.pk]["uve"] for s in shops), Z),
                   "er": sum((bal[s.pk]["er"] for s in shops), Z)}
            lines.append(_row("period", label, m, agg))
        title = _("By day") if period in ("week", "custom") else _("By week")
        add(title + " — " + _("activity"), _cols_activity("period", _("Period")), lines)
        add(title + " — " + _("agents, cash control and balances"), _cols_control("period", _("Period")), lines,
            note=_("Closing balances are at the end of each line's period."))
    add(_("By shop") + " — " + _("activity"), _cols_activity("shop", _("Shop")), per_shop)
    add(_("By shop") + " — " + _("agents, cash control and balances"), _cols_control("shop", _("Shop")), per_shop)
    if duties is None or "SERVICES" in duties:
        svc = ServiceCount.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
            .values("shop__name", "service__name").annotate(n=Sum("count")).order_by("shop__name", "service__name")
        rep.tables.append(Table(_("Services done by type"),
                                [Col("shop", _("Shop")), Col("service", _("Service")),
                                 Col("n", _("Count"), "int", True)],
                                [dict(shop=r["shop__name"], service=r["service__name"], n=r["n"]) for r in svc]))
        ret = SimRetrieval.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
            .select_related("shop", "recorded_by").order_by("business_date", "recorded_at")
        rep.tables.append(Table(
            _("SIM retrievals"),
            [Col("date", _("Date"), "date"), Col("shop", _("Shop")), Col("sim", _("SIM number")),
             Col("iccid", _("New SIM serial (ICCID)")), Col("holder", _("Name of the person")),
             Col("id", _("Identification")), Col("phone", _("Contact phone")), Col("by", _("Recorded by"))],
            [dict(date=r.business_date, shop=r.shop.name, sim=display_sim(r.sim_number), iccid=r.iccid or "—",
                  holder=r.holder_name, id=f"{r.get_id_type_display()} {r.id_number}", phone=r.phone or "—",
                  by=str(r.recorded_by)) for r in ret]))
    # attendance summary (staff with some duties: their own shifts only)
    _q, people = ss.attendance(shops, d_from, d_to, user=viewer if duties is not None else None)
    rep.tables.append(Table(
        _("Attendance summary"),
        [Col("staff", _("Staff")), Col("shop", _("Shop")), Col("duties", _("Duties")),
         Col("days", _("Days worked"), "int", True), Col("shifts", _("Shifts"), "int", True),
         Col("hours", _("Hours"), "money", True), Col("open", _("Still open"), "int", True),
         Col("forced", _("Closed by the senior"), "int", True)],
        [dict(staff=p["user"].label_with_id, shop=p["shop"].name, duties=str(p["user"].shop_duties_display),
              days=p["days"], shifts=p["shifts"], hours=Decimal(str(p["hours"])).quantize(Decimal("0.1")),
              open=p["open"], forced=p["forced"]) for p in people]))
    tiles = [("OM", _("Cash-in"), totals["cashin"], "money"), ("OM", _("Cash-out"), totals["cashout"], "money"),
             ("OM", _("Commission"), totals["commission"], "money"),
             ("AIRTIME", _("Airtime"), totals["airtime"], "money"),
             ("SALES", _("Item sales"), totals["sales"], "money"),
             ("SERVICES", _("Services done"), int(totals["services"]), "int"),
             ("SERVICES", _("SIM retrievals"), int(totals["retrievals"]), "int"),
             (None, _("Staff hours"), Decimal(totals["hours"]).quantize(Decimal("0.1")), "int"),
             (None, _("Cash in shops at closing"), sum((end_bal[s.pk]["cash"] for s in shops), Z), "money"),
             (None, _("UVE in shops at closing"), sum((end_bal[s.pk]["uve"] for s in shops), Z), "money"),
             (None, _("ER in shops at closing"), sum((end_bal[s.pk]["er"] for s in shops), Z), "money")]
    rep.summary = [(label, v, kind) for duty, label, v, kind in tiles
                   if duties is None or duty is None or duty in duties]
    return rep, period, d_from, d_to


# ---------------------------------------------------------------- income statement per shop
def income_statement(form, visible_shops):
    """Income statement of each shop over the period (columns: shops, then the total).

    Income: item sales − cost of the items sold (purchase price of the units sold) = margin on items,
    + Orange Money commissions (as recorded with each cash-in / cash-out),
    + UVE→ER conversion gain (ER received above the UVE converted).
    Costs: expenses by category, stock written off (units removed that had a cost).
    Result = income − costs. Airtime is sold at face value (no margin); capital, transfers and agent dealings
    are not income or costs."""
    from core.models import Account as A, ExpenseCategory

    period, d_from, d_to, shops_qs, _buckets = resolve(form, visible_shops)
    shops = list(shops_qs.filter(active=True).order_by("name"))
    ids = [s.pk for s in shops]
    tx = Transaction.objects.filter(shop_id__in=ids, business_date__gte=d_from, business_date__lte=d_to,
                                    correction__isnull=True).exclude(type=T.REVERSAL)
    val = defaultdict(lambda: defaultdict(lambda: Z))  # line -> shop id -> amount
    for r in tx.filter(type=T.SHOP_SALE).values("shop_id").annotate(s=Sum("amount")):
        val["sales"][r["shop_id"]] = r["s"] or Z
    for r in tx.filter(type__in=[T.SHOP_CASHIN, T.SHOP_CASHOUT]).values("shop_id").annotate(s=Sum("commission")):
        val["commission"][r["shop_id"]] = r["s"] or Z
    for t in tx.filter(type=T.SHOP_CONVERT).only("shop_id", "amount", "amount_received"):
        val["conversion"][t.shop_id] += (t.amount_received or t.amount) - t.amount
    for kind, line in ((A.Kind.COST_OF_SALES, "cogs"), (A.Kind.STOCK_LOSS, "stock_loss")):
        for r in Entry.objects.filter(account__kind=kind, transaction__in=tx).values("transaction__shop_id") \
                .annotate(s=Sum("amount")):
            val[line][r["transaction__shop_id"]] = r["s"] or Z
    cats = {}
    for r in tx.filter(type=T.SHOP_EXPENSE).values("shop_id", "expense_category_id").annotate(s=Sum("amount")):
        val[f"exp_{r['expense_category_id']}"][r["shop_id"]] = r["s"] or Z
        cats[r["expense_category_id"]] = True
    cat_names = {c.pk: str(c) for c in ExpenseCategory.objects.filter(pk__in=list(cats))}

    def line(label, key=None, values=None, bold=False):
        row = {"line": label, "_bold": bold}
        tot = Z
        for s in shops:
            v = values[s.pk] if values is not None else val[key][s.pk]
            row[f"s{s.pk}"] = v
            tot += v
        row["total"] = tot
        return row

    def combine(*parts):
        out = defaultdict(lambda: Z)
        for sign, key in parts:
            for s in shops:
                out[s.pk] += sign * (val[key][s.pk] if isinstance(key, str) else key[s.pk])
        return out

    margin = combine((1, "sales"), (-1, "cogs"))
    income = combine((1, margin), (1, "commission"), (1, "conversion"))
    exp_keys = [f"exp_{cid}" for cid in sorted(cats, key=lambda c: cat_names.get(c, ""))]
    costs = combine(*[(1, k) for k in exp_keys], (1, "stock_loss"))
    result = combine((1, income), (-1, costs))
    rows = [line(_("Item sales"), "sales"), line(_("Cost of the items sold"), "cogs"),
            line(_("Margin on items"), values=margin, bold=True),
            line(_("Orange Money commissions"), "commission"), line(_("UVE→ER conversion gain"), "conversion"),
            line(_("TOTAL INCOME"), values=income, bold=True)]
    rows += [line(_("Expense: %(c)s") % {"c": cat_names.get(int(k[4:]), "?")}, k) for k in exp_keys]
    rows += [line(_("Stock written off"), "stock_loss"), line(_("TOTAL COSTS"), values=costs, bold=True),
             line(_("NET RESULT"), values=result, bold=True)]
    cols = [Col("line", "")] + [Col(f"s{s.pk}", s.name, "money") for s in shops]
    if len(shops) > 1:
        cols.append(Col("total", _("All shops"), "money"))
    rep = Report("shop-income", _("Income statement — %(a)s to %(b)s") % {"a": d_from.strftime("%d/%m/%Y"),
                                                                          "b": d_to.strftime("%d/%m/%Y")},
                 ", ".join(s.name for s in shops) if len(shops) < 6 else _("%(n)s shops") % {"n": len(shops)})
    rep.tables.append(Table(_("Income statement"), cols, rows,
                            note=_("Cost of the items sold uses the purchase price of each unit sold (units received "
                                   "without a purchase have no cost). Commissions are those recorded with each "
                                   "cash-in and cash-out. Airtime is sold at face value.")))
    tot = lambda key: sum((key[s.pk] for s in shops), Z)  # noqa: E731
    rep.summary = [(_("Total income"), tot(income), "money"), (_("Total costs"), tot(costs), "money"),
                   (_("Net result"), tot(result), "money")]
    return rep, period, d_from, d_to


__all__ = ["date"]
