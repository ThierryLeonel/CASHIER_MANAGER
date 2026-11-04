"""Commercial agent portal, and the cashier's confirmation of agents' paybacks."""
from datetime import date, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from core import forms_agent as AF
from core.models import AgentClientEntry, AgentPayback, Counterparty
from core.money import fmt
from core.services import agents as ag
from core.services.days import douala_today
from core.services.errors import BusinessError

from .base import R, STAFF, roles_required, run_service

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
    """Portal home (the agent's dashboard)."""
    agent = request.user.counterparty
    s = ag.summary(agent)
    return render(request, "core/agent/home.html", {
        "cp": agent, "s": s,
        "paybacks": P.objects.filter(agent=agent).select_related("desk", "transaction")[:5],
        "recent": ag.live_entries(agent).select_related("pos", "shop")[:8],
        "office": ag.office_txns(agent)[:8],
    })


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
    mine = request.GET.get("mine") == "1"
    if mine:
        qs = qs.filter(microzone__agent=agent)
    last = dict(ag.live_entries(agent).filter(kind=E.Kind.SUPPLY, pos__isnull=False).values("pos")
                .annotate(d=Max("business_date")).values_list("pos", "d"))
    rows = [(p, ag.pos_owed(agent, p), last.get(p.pk), p.microzone_id and p.microzone.agent_id == agent.pk)
            for p in qs]
    return render(request, "core/agent/pos_list.html", {"rows": rows, "q": q, "mine": mine,
                                                        "zone": ag.zone_of(agent)})


@roles_required(R.AGENT)
def agent_pos_detail(request, pk):
    agent = request.user.counterparty
    pos = get_object_or_404(Counterparty, pk=pk, kind=Counterparty.Kind.POS)
    if not ag.in_zone(agent, pos):
        raise Http404  # outside the agent's zone: only its name is ever shown (in the agent's own records)
    entries = ag.live_entries(agent).filter(pos=pos).select_related("recorded_by")[:100]
    return render(request, "core/agent/pos_detail.html", {"pos": pos, "owed": ag.pos_owed(agent, pos),
                                                          "entries": entries})


def _entry_form(request, form_class, fn, title, intro, icon, ok_msg):
    initial = {}
    if request.GET.get("pos"):
        initial = {"client_kind": "POS", "pos_sim": request.GET["pos"]}
    form = form_class(request.user, request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        resp = run_service(request, form,
                           lambda: fn(request.user, idempotency_key=form.cleaned_data["idem"], **form.params()),
                           success=ok_msg, on_success=lambda o: redirect("agent_activity"))
        if resp:
            return resp
    agent = request.user.counterparty
    return render(request, "core/agent/form.html", {
        "form": form, "title": title, "intro": intro, "icon": icon,
        "zone_pos": ag.zone_pos(agent).filter(active=True) if "pos_sim" in form.fields else []})


@roles_required(R.AGENT)
def agent_supply(request):
    return _entry_form(
        request, AF.AgentSupplyForm, ag.record_supply, _("Supply a client"),
        _("UVE, ER or cash you gave to a POS, a shop or another client from your own stock."), "supply",
        lambda e: _("%(ref)s: %(amt)s FCFA supplied to %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.total), "who": e.client_label})


@roles_required(R.AGENT)
def agent_collect(request):
    return _entry_form(
        request, AF.ClientPaymentForm, ag.record_client_payment, _("A client pays me"),
        _("Money a client paid you for earlier supplies. It reduces what the client owes you."), "payment",
        lambda e: _("%(ref)s: %(amt)s FCFA received from %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.paid), "who": e.client_label})


@roles_required(R.AGENT)
def agent_payback(request):
    form = AF.PaybackForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: ag.request_payback(request.user, idempotency_key=form.cleaned_data["idem"],
                                                      **form.params()),
            success=lambda p: _("%(code)s: %(amt)s FCFA sent to %(desk)s, waiting for the cashier to confirm.") % {
                "code": p.code, "amt": fmt(p.total), "desk": p.desk},
            on_success=lambda p: redirect(reverse("agent_activity") + "?tab=paybacks"))
        if resp:
            return resp
    s = ag.summary(request.user.counterparty)
    return render(request, "core/agent/form.html", {
        "form": form, "title": _("Pay back the company"), "icon": "payment", "s": s,
        "intro": _("Send UVE to the master SIM, cash or MTN float to the cashier of your zone. It counts as paid once "
                   "the cashier confirms receiving it.")})


TABS = (("office", _l("From the office")), ("clients", _l("My clients")), ("paybacks", _l("My paybacks")))


@roles_required(R.AGENT)
def agent_activity(request):
    agent = request.user.counterparty
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "clients"
    d_from, d_to = _period(request)
    ctx = {"tab": tab, "tabs": TABS, "d_from": d_from, "d_to": d_to, "today": douala_today()}
    if tab == "office":
        qs = ag.office_txns(agent).filter(business_date__gte=d_from, business_date__lte=d_to)
        ctx["account"] = agent.account
    elif tab == "paybacks":
        qs = P.objects.filter(agent=agent, created_at__date__gte=d_from, created_at__date__lte=d_to) \
            .select_related("desk", "decided_by", "transaction")
    else:
        qs = E.objects.filter(agent=agent, business_date__gte=d_from, business_date__lte=d_to) \
            .select_related("pos", "shop")
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
@roles_required(R.CASHIER, *STAFF)
def paybacks(request):
    user = request.user
    qs = P.objects.select_related("agent", "desk", "decided_by", "transaction")
    if user.is_cashier:
        qs = qs.filter(desk=user.subzone)
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


@roles_required(R.CASHIER)
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
