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

from .engine import Col, Report, Table

K = Account.Kind
Z = Decimal(0)
T = TxType
KINDS = ((K.CASH_DRAWER, "cash"), (K.UVE, "uve"), (K.ER, "er"))
PERIODS = [("day", _l("Daily")), ("week", _l("Weekly")), ("month", _l("Monthly")), ("custom", _l("Custom dates"))]
MONTHS = [_l("January"), _l("February"), _l("March"), _l("April"), _l("May"), _l("June"), _l("July"),
          _l("August"), _l("September"), _l("October"), _l("November"), _l("December")]


class ShopReportForm(forms.Form):
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
           "er_supply", "destock", "agent_paid", "agent_repaid", "to_cashiers", "from_cashiers", "capital", "sales",
           "units", "services", "retrievals", "hours", "diff_cash", "diff_uve", "diff_er"]
TYPE_METRIC = {T.SHOP_CASHIN: "cashin", T.SHOP_CASHOUT: "cashout", T.SHOP_CONVERT: "convert",
               T.SHOP_APPRO: "appro", T.SHOP_ER_SUPPLY: "er_supply", T.SHOP_DESTOCK: "destock",
               T.SHOP_AGENT_PAY: "agent_paid", T.SHOP_AGENT_REPAY: "agent_repaid", T.SHOP_TO_DESK: "to_cashiers",
               T.DESK_TO_SHOP: "from_cashiers", T.SHOP_SALE: "sales"}


def _daily(shops, d_from, d_to):
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
    for s in ShopShift.objects.filter(shop_id__in=shop_ids, business_date__gte=d_from, business_date__lte=d_to):
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


def shop_summary(form, visible_shops):
    period, d_from, d_to, shops_qs, buckets = resolve(form, visible_shops)
    shops = list(shops_qs.filter(active=True).order_by("name"))
    data = _daily(shops, d_from, d_to)
    rep = Report("shops", _period_title(period, d_from, d_to),
                 ", ".join(s.name for s in shops) if len(shops) < 6 else _("%(n)s shops") % {"n": len(shops)})
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
        rep.tables.append(Table(title + " — " + _("activity"), _cols_activity("period", _("Period")), lines))
        rep.tables.append(Table(title + " — " + _("agents, cash control and balances"),
                                _cols_control("period", _("Period")), lines,
                                note=_("Closing balances are at the end of each line's period.")))
    rep.tables.append(Table(_("By shop") + " — " + _("activity"), _cols_activity("shop", _("Shop")), per_shop))
    rep.tables.append(Table(_("By shop") + " — " + _("agents, cash control and balances"),
                            _cols_control("shop", _("Shop")), per_shop))
    svc = ServiceCount.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
        .values("shop__name", "service__name").annotate(n=Sum("count")).order_by("shop__name", "service__name")
    rep.tables.append(Table(_("Services done by type"),
                            [Col("shop", _("Shop")), Col("service", _("Service")), Col("n", _("Count"), "int", True)],
                            [dict(shop=r["shop__name"], service=r["service__name"], n=r["n"]) for r in svc]))
    rep.summary = [
        (_("Cash-in"), totals["cashin"], "money"), (_("Cash-out"), totals["cashout"], "money"),
        (_("Commission"), totals["commission"], "money"), (_("Airtime"), totals["airtime"], "money"),
        (_("Item sales"), totals["sales"], "money"), (_("Services done"), int(totals["services"]), "int"),
        (_("Cash in shops at closing"), sum((end_bal[s.pk]["cash"] for s in shops), Z), "money"),
        (_("UVE in shops at closing"), sum((end_bal[s.pk]["uve"] for s in shops), Z), "money"),
        (_("ER in shops at closing"), sum((end_bal[s.pk]["er"] for s in shops), Z), "money"),
    ]
    return rep, period, d_from, d_to


__all__ = ["date"]
