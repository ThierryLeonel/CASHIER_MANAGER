"""What the commercial agents do in the field, seen by the office (senior manager, finance, super admin): every
supply / payment / transfer they recorded with their POS, shops and other clients, and the clients who still owe
them (their outstanding debtors). These are the agents' own records, not the company ledger."""
import io
from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core.models import AgentClientEntry as E
from core.models import ClientKind, Counterparty
from core.services.days import douala_today

from .base import R, roles_required

FIELD_ROLES = (R.MANAGER, R.FINANCE, R.SUPERADMIN)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _date(raw, default):
    from datetime import date

    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


def _agent_q(q, prefix="agent__"):
    q = (q or "").strip()
    if not q:
        return Q()
    digits = "".join(ch for ch in q if ch.isdigit())
    out = Q(**{f"{prefix}name__icontains": q})
    if digits:
        out |= Q(**{f"{prefix}business_sim__contains": digits[-9:]})
    return out


def _client_q(q):
    q = (q or "").strip()
    if not q:
        return Q()
    digits = "".join(ch for ch in q if ch.isdigit())
    out = Q(pos__name__icontains=q) | Q(shop__name__icontains=q) | Q(client_name__icontains=q)
    if digits:
        out |= Q(pos__business_sim__contains=digits[-9:]) | Q(client_phone__contains=digits[-9:])
    return out


def _xlsx(title, head, rows, name):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([v if isinstance(v, (int, float, type(None))) or hasattr(v, "quantize") or hasattr(v, "isoformat")
                   else str(v) for v in r])
    for i, h in enumerate(head, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = max(12, min(40, len(str(h)) + 6))
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="{name}-{douala_today().isoformat()}.xlsx"'
    return resp


KINDS = [("", _l("All")), ("SUPPLY", _l("Supplies")), ("PAYMENT", _l("Payments")), ("DESTOCASH", _l("Destocash")),
         ("CREDIT", _l("On credit")), ("AGENTS", _l("Transfers between agents")), ("CANCELLED", _l("Cancelled"))]


@roles_required(*FIELD_ROLES)
def agents_field_activity(request):
    """Every record the commercial agents made in their portal, with filters, totals and an Excel download."""
    today = douala_today()
    d_to = _date(request.GET.get("to"), today)
    d_from = _date(request.GET.get("from"), d_to - timedelta(days=6))
    kind = request.GET.get("kind") or ""
    qs = E.objects.filter(business_date__gte=d_from, business_date__lte=d_to) \
        .select_related("agent", "pos", "shop", "to_agent", "recorded_by").order_by("-created_at")
    qs = qs.filter(_agent_q(request.GET.get("agent")) | (_agent_q(request.GET.get("agent"), "to_agent__")
                                                         if request.GET.get("agent") else Q()))
    qs = qs.filter(_client_q(request.GET.get("client")))
    if kind == "CANCELLED":
        qs = qs.filter(cancelled_at__isnull=False)
    else:
        if kind in ("SUPPLY", "PAYMENT"):
            qs = qs.filter(kind=kind).exclude(client_kind=ClientKind.AGENT)
        elif kind == "DESTOCASH":
            qs = qs.filter(note__istartswith="Destocash")
        elif kind == "CREDIT":
            qs = qs.filter(kind=E.Kind.SUPPLY, paid__lt=F("uve") + F("er") + F("cash")).exclude(
                client_kind=ClientKind.AGENT)
        elif kind == "AGENTS":
            qs = qs.filter(client_kind=ClientKind.AGENT)
        else:
            kind = ""
    live = qs.filter(cancelled_at__isnull=True).exclude(client_kind=ClientKind.AGENT)
    agg = live.aggregate(uve=Sum("uve"), er=Sum("er"), cash=Sum("cash"), paid=Sum("paid"),
                         n=Count("id"), agents=Count("agent", distinct=True))
    z = 0
    sums = {k: agg[k] or z for k in ("uve", "er", "cash", "paid")}
    sums["supplied"] = sums["uve"] + sums["er"] + sums["cash"]
    sums["credit"] = sums["supplied"] - sums["paid"]
    sums["n"], sums["agents"] = agg["n"], agg["agents"]
    if request.GET.get("format") == "xlsx":
        head = [_("Date"), _("Time"), _("Reference"), _("Commercial agent"), _("Agent SIM"), _("Operation"),
                _("Client"), _("Client type"), _("Client number"), _("UVE"), _("ER"), _("Cash"), _("Received"),
                _("On credit"), _("Note"), _("Cancelled")]
        rows = []
        for e in qs[:50000]:
            num = e.pos.business_sim if e.pos_id else (e.to_agent.business_sim if e.to_agent_id else e.client_phone)
            rows.append([e.business_date, timezone.localtime(e.created_at).strftime("%H:%M"), e.reference, e.agent.name,
                         e.agent.business_sim, e.get_kind_display(), e.client_label, e.get_client_kind_display(),
                         num, e.uve, e.er, e.cash, e.paid, e.balance_effect if e.kind == E.Kind.SUPPLY else 0,
                         e.note, _("yes") if e.cancelled_at else ""])
        return _xlsx("field", head, rows, "agents-field-transactions")
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    keep = request.GET.copy()
    keep.pop("page", None)
    keep.pop("kind", None)
    return render(request, "core/agents_field_activity.html", {
        "page": page, "sums": sums, "kinds": KINDS, "kind": kind, "d_from": d_from, "d_to": d_to,
        "agent": request.GET.get("agent", ""), "client": request.GET.get("client", ""),
        "keep": keep.urlencode(), "qs": request.GET.urlencode()})


def debtor_rows(agent_q=Q(), client_q=Q()):
    """[{agent, kind, pos, shop, phone, name, owed, last_supply, entries}] — every client who owes an agent, from the
    agents' live records, in one query."""
    owed = ExpressionWrapper(F("uve") + F("er") + F("cash") - F("paid"),
                             output_field=DecimalField(max_digits=18, decimal_places=0))
    groups = E.objects.filter(cancelled_at__isnull=True).exclude(client_kind=ClientKind.AGENT) \
        .filter(agent_q).filter(client_q) \
        .values("agent_id", "client_kind", "pos_id", "shop_id", "client_phone") \
        .annotate(owed=Sum(owed), last_supply=Max("business_date", filter=Q(kind=E.Kind.SUPPLY)),
                  name=Max("client_name"), entries=Count("id")) \
        .filter(owed__gt=0).order_by("-owed")
    rows = list(groups)
    cps = Counterparty.objects.in_bulk({r["agent_id"] for r in rows} | {r["pos_id"] for r in rows if r["pos_id"]})
    from core.models import Shop

    shops = Shop.objects.in_bulk({r["shop_id"] for r in rows if r["shop_id"]})
    for r in rows:
        r["agent"] = cps.get(r["agent_id"])
        r["pos"] = cps.get(r["pos_id"]) if r["pos_id"] else None
        r["shop"] = shops.get(r["shop_id"]) if r["shop_id"] else None
        if r["pos"]:
            r["label"], r["number"] = r["pos"].name, r["pos"].business_sim
        elif r["shop"]:
            r["label"], r["number"] = r["shop"].name, r["shop"].sim_number or ""
        else:
            r["label"], r["number"] = r["name"] or r["client_phone"] or "—", r["client_phone"]
        r["kind_label"] = ClientKind(r["client_kind"]).label
    return rows


@roles_required(*FIELD_ROLES)
def agents_debtors(request):
    """The clients (POS, shops, others) who still owe each commercial agent, with a summary per agent."""
    rows = debtor_rows(_agent_q(request.GET.get("agent")), _client_q(request.GET.get("client")))
    by_agent = {}
    for r in rows:
        a = by_agent.setdefault(r["agent_id"], {"agent": r["agent"], "debtors": 0, "owed": 0})
        a["debtors"] += 1
        a["owed"] += r["owed"]
    agents = sorted(by_agent.values(), key=lambda a: -a["owed"])
    total = sum((r["owed"] for r in rows), 0)
    if request.GET.get("format") == "xlsx":
        head = [_("Commercial agent"), _("Agent SIM"), _("Debtor"), _("Client type"), _("Client number"),
                _("Outstanding (FCFA)"), _("Last supply"), _("Records")]
        data = [[r["agent"].name, r["agent"].business_sim, r["label"], r["kind_label"], r["number"], r["owed"],
                 r["last_supply"], r["entries"]] for r in rows]
        return _xlsx("debtors", head, data, "agents-outstanding-debtors")
    return render(request, "core/agents_debtors.html", {
        "rows": rows[:1000], "count": len(rows), "agents": agents, "total": total,
        "agent": request.GET.get("agent", ""), "client": request.GET.get("client", ""),
        "qs": request.GET.urlencode()})
