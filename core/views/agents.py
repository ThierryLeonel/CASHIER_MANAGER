"""Commercial agent portal, and the cashier's / shop's confirmation of agents' paybacks."""
import uuid
from datetime import date, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Max, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from core import forms_agent as AF
from core.models import AgentClientEntry, AgentExchange, AgentPayback, Counterparty
from core.money import fmt
from core.services import agents as ag
from core.services.days import douala_today
from core.services.errors import BusinessError

from .base import R, STAFF, roles_required

E, P = AgentClientEntry, AgentPayback


def _period(request, days=30):
    today = douala_today()
    try:
        d_from = date.fromisoformat(request.GET.get("from") or "")
    except ValueError:
        d_from = today - timedelta(days=days)
    try:
        d_to = date.fromisoformat(request.GET.get("to") or "")
    except ValueError:
        d_to = today
    return d_from, d_to


def agent_home(request):
    """Portal home (the agent's dashboard): kept short — today's figures and the actions."""
    agent = request.user.counterparty
    today = douala_today()
    from core.services.branding import company

    limit = company().out_of_stock_er
    out = ag.zone_pos(agent).filter(microzone__agent=agent, er_stock__lt=limit).count()
    return render(request, "core/agent/home.html", {
        "cp": agent, "s": ag.summary(agent), "rec": ag.daily_reconciliation(agent, today), "out_of_stock": out,
        "limit": limit})


@roles_required(R.AGENT)
def agent_pos(request):
    agent = request.user.counterparty
    qs = ag.zone_pos(agent)
    q = (request.GET.get("q") or "").strip()
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(microzone__name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits)
        qs = qs.filter(cond)
    from core.services.branding import company

    limit = company().out_of_stock_er
    mine = request.GET.get("mine") == "1"
    if mine:
        qs = qs.filter(microzone__agent=agent)
    out = request.GET.get("out") == "1"
    if out:
        qs = qs.filter(er_stock__lt=limit)
    from django.core.paginator import Paginator
    from django.db.models import Case, F, IntegerField, When

    # out of stock first (lowest ER first), then the others by name; 100 per page
    qs = qs.annotate(_oos=Case(When(er_stock__lt=limit, then=0), default=1, output_field=IntegerField())) \
        .order_by("_oos", F("er_stock").asc(nulls_last=True) if out else "name", "name")
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    ids = [p.pk for p in page]
    live = ag.live_entries(agent).filter(pos_id__in=ids)
    last = dict(live.filter(kind=E.Kind.SUPPLY).values("pos").annotate(d=Max("business_date"))
                .values_list("pos", "d"))
    owed = {r["pos"]: (r["t"] or 0) + (r["e"] or 0) + (r["c"] or 0) - (r["p"] or 0)
            for r in live.values("pos").annotate(t=Sum("uve"), e=Sum("er"), c=Sum("cash"), p=Sum("paid"))}
    rows = [(p, owed.get(p.pk, 0), last.get(p.pk), p.microzone_id and p.microzone.agent_id == agent.pk,
             p.out_of_stock(limit)) for p in page]
    keep = request.GET.copy()
    keep.pop("page", None)
    return render(request, "core/agent/pos_list.html", {"rows": rows, "q": q, "mine": mine, "out": out,
                                                        "limit": limit, "zone": ag.zone_of(agent), "page": page,
                                                        "keep": keep.urlencode()})


@roles_required(R.AGENT)
def agent_oos(request):
    """OOS: only the logged-in agent's POS (those of the microzones he is in charge of), from out of stock to the
    highest ER stock; POS without an imported stock come last."""
    from django.core.paginator import Paginator
    from django.db.models import F

    from core.services.branding import company

    agent = request.user.counterparty
    limit = company().out_of_stock_er
    mine = Counterparty.objects.filter(kind=Counterparty.Kind.POS, active=True, microzone__agent=agent) \
        .select_related("microzone")
    q = (request.GET.get("q") or "").strip()
    qs = mine
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(microzone__name__icontains=q) | Q(om_account_name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:])
        qs = qs.filter(cond)
    if request.GET.get("out") == "1":
        qs = qs.filter(er_stock__lt=limit)
    qs = qs.order_by(F("er_stock").asc(nulls_last=True), F("uve_stock").asc(nulls_last=True), "name")
    page = Paginator(qs, 200).get_page(request.GET.get("page"))
    tot = mine.aggregate(er=Sum("er_stock"), uve=Sum("uve_stock"), com=Sum("commission_balance"))
    last = mine.exclude(stock_at=None).order_by("-stock_at").values_list("stock_at", flat=True).first()
    keep = request.GET.copy()
    keep.pop("page", None)
    return render(request, "core/agent/oos.html", {
        "page": page, "rows": [(p, p.out_of_stock(limit)) for p in page], "limit": limit, "q": q,
        "out": request.GET.get("out") == "1", "keep": keep.urlencode(), "last": last, "tot": tot,
        "n_pos": mine.count(), "n_out": mine.filter(er_stock__lt=limit).count(),
        "n_unknown": mine.filter(stock_at=None).count()})


@roles_required(R.AGENT)
def agent_pos_search(request):
    """POS of the agent's zone matching a name, microzone or SIM digits (a few, for the number picker)."""
    agent = request.user.counterparty
    q = (request.GET.get("q") or "").strip()
    qs = ag.zone_pos(agent).filter(active=True)
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(microzone__name__icontains=q) | Q(om_account_name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:])
        qs = qs.filter(cond)
    out = [{"sim": p.business_sim, "name": p.name, "microzone": p.microzone.name if p.microzone_id else "",
            "kind_label": ""} for p in qs[:20]]
    return JsonResponse({"results": out})


@roles_required(R.AGENT)
def agent_pos_detail(request, pk):
    agent = request.user.counterparty
    pos = get_object_or_404(Counterparty, pk=pk, kind=Counterparty.Kind.POS)
    if not ag.in_zone(agent, pos):
        raise Http404  # outside the agent's zone: only its name is ever shown (in the agent's own records)
    entries = ag.live_entries(agent).filter(pos=pos).select_related("recorded_by")[:100]
    from core.services.branding import company

    limit = company().out_of_stock_er
    return render(request, "core/agent/pos_detail.html", {"pos": pos, "owed": ag.pos_owed(agent, pos),
                                                          "entries": entries, "limit": limit,
                                                          "out_of_stock": pos.out_of_stock(limit),
                                                          "history": pos.stock_history.all()[:10]})


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "fetch"


def _json_errors(form, status=400):
    errors = {k: [str(m) for m in v] for k, v in form.errors.items()}
    return JsonResponse({"ok": False, "errors": errors}, status=status)


def _entry_form(request, form_class, fn, *, title, icon, ok_msg, code_of=None, labels=None, extra=None):
    """Agent entry form. Sent by the page's script (fetch), it answers in JSON: the record is saved, the form is
    cleared and — for UVE / ER — the Orange code is handed to the phone's dialpad at once."""
    g = request.GET
    initial = {}
    if g.get("pos"):
        initial = {"client_kind": "POS", "pos_sim": g["pos"]}
    elif g.get("shop", "").isdigit():
        initial = {"client_kind": "SHOP", "shop": int(g["shop"])}
    elif g.get("phone"):
        initial = {"client_kind": "OTHER", "client_phone": g["phone"]}
    form = form_class(request.user, request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            try:
                obj = fn(request.user, idempotency_key=form.cleaned_data["idem"], **form.params())
            except BusinessError as exc:
                if exc.field and exc.field in form.fields:
                    form.add_error(exc.field, exc.message)
                else:
                    form.add_error(None, exc.message)
            else:
                msg = ok_msg(obj)
                code = code_of(obj) if code_of else ""
                if _wants_json(request):
                    return JsonResponse({"ok": True, "message": msg, "code": code, "idem": str(uuid.uuid4())})
                messages.success(request, msg + (f" · {code}" if code else ""))
                return redirect(request.path)
        if _wants_json(request):
            return _json_errors(form)
    ctx = {"form": form, "title": title, "icon": icon, "labels": labels or {},
           "pos_lookup": "pos_sim" in form.fields}
    ctx.update(extra or {})
    return render(request, "core/agent/form.html", ctx)


def _verb(en, fr):
    # a button is a verb; some catalogue entries ("Supply", "Transfer") are nouns in French
    return fr if (get_language() or "").startswith("fr") else en


@roles_required(R.AGENT)
def agent_supply(request):
    return _entry_form(
        request, AF.AgentSupplyForm, ag.record_supply, title=_("Supply a client"), icon="supply",
        ok_msg=lambda e: _("%(ref)s: %(amt)s FCFA supplied to %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.total), "who": e.client_label},
        code_of=ag.ussd_code,
        labels={"dial": _verb("Supply", "Approvisionner"), "plain": _("Record")})


@roles_required(R.AGENT)
def agent_transfer(request):
    agent = request.user.counterparty
    return _entry_form(
        request, AF.AgentTransferForm, ag.record_supply, title=_("Transfer to an agent"), icon="transfer",
        ok_msg=lambda e: _("%(ref)s: %(amt)s FCFA transferred to %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.total), "who": e.client_label},
        code_of=ag.ussd_code,
        labels={"dial": _verb("Transfer", "Transférer"), "plain": _("Record")},
        extra={"agents": Counterparty.objects.filter(kind=Counterparty.Kind.AGENT, active=True)
               .exclude(pk=agent.pk).order_by("name")})


@roles_required(R.AGENT)
def agent_collect(request):
    return _entry_form(
        request, AF.ClientPaymentForm, ag.record_client_payment, title=_("A client pays me"), icon="payment",
        ok_msg=lambda e: _("%(ref)s: %(amt)s FCFA received from %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.paid), "who": e.client_label})


@roles_required(R.AGENT)
def agent_exchange(request):
    return _entry_form(
        request, AF.ExchangeForm, ag.record_exchange, title=_("Float exchange"), icon="convert",
        ok_msg=lambda x: _("%(ref)s: %(amt)s FCFA — %(what)s.") % {
            "ref": x.reference, "amt": fmt(x.amount), "what": x.get_direction_display()},
        code_of=ag.exchange_code, labels={"dial": _verb("Exchange", "Échanger"), "plain": _("Record")},
        extra={"recent": AgentExchange.objects.filter(agent=request.user.counterparty)[:10],
               "today": douala_today()})


@roles_required(R.AGENT)
@require_POST
def agent_exchange_cancel(request, pk):
    x = get_object_or_404(AgentExchange, pk=pk, agent=request.user.counterparty)
    try:
        ag.cancel_exchange(request.user, x, request.POST.get("reason"))
        messages.success(request, _("%(ref)s cancelled.") % {"ref": x.reference})
    except BusinessError as exc:
        messages.error(request, exc.message)
    return redirect("agent_exchange")


@roles_required(R.AGENT)
def agent_debtors(request):
    """Everyone who owes the agent, with what is outstanding and a button to receive or ask for the payment."""
    agent = request.user.counterparty
    rows = [g for g in ag.clients_owing(agent) if g["owed"] > 0]
    for g in rows:
        g["collect_q"] = (f"pos={g['pos'].business_sim}" if g["pos"] else
                          f"shop={g['shop'].pk}" if g["shop"] else f"phone={g['phone']}")
        g["sms"] = _("Hello %(who)s, you still owe %(amt)s FCFA to %(agent)s (%(sim)s). Thank you for paying.") % {
            "who": g["label"], "amt": fmt(g["owed"]), "agent": agent.name, "sim": agent.business_sim}
    return render(request, "core/agent/debtors.html", {
        "rows": rows, "total": sum((g["owed"] for g in rows), 0)})


@roles_required(R.AGENT)
def agent_payback(request):
    return _entry_form(
        request, AF.PaybackForm, ag.request_payback, title=_("Pay back the company"), icon="transfer",
        ok_msg=lambda p: _("%(code)s: %(amt)s FCFA to %(who)s, waiting for confirmation.") % {
            "code": p.code, "amt": fmt(p.total), "who": p.receiver},
        code_of=ag.payback_code,
        labels={"dial": _verb("Pay back", "Rembourser"), "plain": _("Record")},
        extra={"s": ag.summary(request.user.counterparty)})


@roles_required(R.AGENT)
def agent_daily(request):
    agent = request.user.counterparty
    today = douala_today()
    try:
        day = date.fromisoformat(request.GET.get("date") or "")
    except ValueError:
        day = today
    day = min(day, today)
    d_from = day - timedelta(days=13)
    day_entries = E.objects.filter(Q(agent=agent) | Q(to_agent=agent), business_date=day) \
        .select_related("pos", "shop", "to_agent", "agent")
    return render(request, "core/agent/daily.html", {
        "rec": ag.daily_reconciliation(agent, day), "day": day, "today": today,
        "prev": day - timedelta(days=1), "next": day + timedelta(days=1) if day < today else None,
        "office": ag.office_supplies(agent).filter(business_date=day),
        "entries": day_entries, "me": agent,
        "paybacks": P.objects.filter(agent=agent, created_at__date=day).select_related("desk", "shop", "transaction"),
        "days": ag.daily_summary(agent, d_from, day)})


@roles_required(R.AGENT)
def agent_office(request):
    """Every supply and destockage the office recorded on the agent, with daily totals per product."""
    agent = request.user.counterparty
    d_from, d_to = _period(request)
    qs = ag.office_supplies(agent).filter(business_date__gte=d_from, business_date__lte=d_to)
    per_day = {}
    for t in qs:
        row = per_day.setdefault(t.business_date, {"day": t.business_date, "UVE": 0, "ER": 0, "CASH": 0,
                                                   "DESTOCK": 0, "total": 0})
        key = "DESTOCK" if t.type == "DESTOCKAGE" else t.product
        row[key] += t.amount
        row["total"] += t.amount
    days = sorted(per_day.values(), key=lambda r: r["day"], reverse=True)
    totals = {k: sum(r[k] for r in days) for k in ("UVE", "ER", "CASH", "DESTOCK", "total")}
    return render(request, "core/agent/office.html", {
        "days": days, "totals": totals, "page": Paginator(qs, 50).get_page(request.GET.get("page")),
        "d_from": d_from, "d_to": d_to})


TABS = (("clients", _l("My clients")), ("agents", _l("Agents")), ("paybacks", _l("My paybacks")),
        ("office", _l("All office records")))


@roles_required(R.AGENT)
def agent_activity(request):
    agent = request.user.counterparty
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "clients"
    d_from, d_to = _period(request)
    ctx = {"tab": tab, "tabs": TABS, "d_from": d_from, "d_to": d_to, "today": douala_today(), "me": agent}
    if tab == "office":
        qs = ag.office_txns(agent).filter(business_date__gte=d_from, business_date__lte=d_to)
    elif tab == "paybacks":
        qs = P.objects.filter(agent=agent, created_at__date__gte=d_from, created_at__date__lte=d_to) \
            .select_related("desk", "shop", "decided_by", "transaction")
    elif tab == "agents":
        qs = E.objects.filter(Q(agent=agent) | Q(to_agent=agent), client_kind="AGENT",
                              business_date__gte=d_from, business_date__lte=d_to).select_related("agent", "to_agent")
    else:
        qs = E.objects.filter(agent=agent, business_date__gte=d_from, business_date__lte=d_to) \
            .exclude(client_kind="AGENT").select_related("pos", "shop")
        kind = request.GET.get("kind")
        if kind in E.Kind.values:
            qs = qs.filter(kind=kind)
        ctx["kind"] = kind or ""
        live = qs.filter(cancelled_at__isnull=True)
        ctx["sums"] = {"supplied": sum((e.total for e in live), 0), "received": sum((e.paid for e in live), 0)}
    ctx["page"] = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/agent/activity.html", ctx)


@roles_required(R.AGENT)
@require_POST
def agent_entry_cancel(request, pk):
    e = get_object_or_404(E, pk=pk, agent=request.user.counterparty)
    try:
        ag.cancel_entry(request.user, e, request.POST.get("reason"))
        messages.success(request, _("%(ref)s cancelled.") % {"ref": e.reference})
    except BusinessError as exc:
        messages.error(request, exc.message)
    return redirect("agent_activity")


@roles_required(R.AGENT)
@require_POST
def agent_payback_cancel(request, pk):
    p = get_object_or_404(P, pk=pk, agent=request.user.counterparty)
    code = p.code
    try:
        ag.cancel_payback(request.user, p)
        messages.success(request, _("%(code)s withdrawn.") % {"code": code})
    except BusinessError as exc:
        messages.error(request, exc.message)
    return redirect(reverse("agent_activity") + "?tab=paybacks")


# ---------------------------------------------------------------- cashier / staff
@roles_required(R.CASHIER, R.SHOP_MANAGER, R.SHOP_SENIOR, *STAFF)
def paybacks(request):
    user = request.user
    qs = P.objects.select_related("agent", "desk", "shop", "decided_by", "transaction")
    if user.is_cashier:
        qs = qs.filter(desk=user.subzone)
    elif user.is_shop_manager:
        qs = qs.filter(shop_id=user.shop_id)
    elif user.is_shop_senior:
        qs = qs.filter(shop__isnull=False)
    elif user.is_junior:
        qs = qs.filter(desk_id__in=user.zone_ids_in_scope())
    status = request.GET.get("status") or ""
    pending = list(qs.filter(status=P.Status.PENDING).order_by("created_at"))
    done = qs.exclude(status=P.Status.PENDING)
    if status in (P.Status.CONFIRMED, P.Status.REJECTED):
        done = done.filter(status=status)
    for p in pending:
        p.can_decide = ag.can_decide(user, p)
    return render(request, "core/agent/paybacks.html", {
        "pending": pending, "page": Paginator(done, 50).get_page(request.GET.get("page")), "status": status})


@roles_required(R.CASHIER, R.SHOP_MANAGER, R.SHOP_SENIOR, R.SUPERADMIN)
@require_POST
def payback_decide(request, pk):
    p = get_object_or_404(P, pk=pk)
    action = request.POST.get("action")
    try:
        if action == "confirm":
            p = ag.confirm_payback(request.user, p)
            messages.success(request, _("%(code)s confirmed: %(amt)s FCFA recorded as a payment by %(agent)s (%(ref)s).")
                             % {"code": p.code, "amt": fmt(p.total), "agent": p.agent.name,
                                "ref": p.transaction.receipt_no or p.transaction.reference})
        elif action == "reject":
            ag.reject_payback(request.user, p, request.POST.get("reason"))
            messages.success(request, _("%(code)s rejected.") % {"code": p.code})
        else:
            raise Http404
    except BusinessError as exc:
        messages.error(request, exc.message)
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("paybacks")
