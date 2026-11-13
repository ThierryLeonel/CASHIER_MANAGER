"""Commercial agent portal, and the cashier's / shop's confirmation of agents' paybacks."""
import uuid
from functools import wraps
from urllib.parse import quote
from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied as Forbidden
from django.db import transaction
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from core import forms_agent as AF
from core.models import AgentClientEntry, AgentExchange, AgentPayback, Counterparty
from core.money import fmt
from core.services import agents as ag
from core.services import operations as ops
from core.services.days import douala_today
from core.services.errors import BusinessError

from .base import R, STAFF, roles_required

E, P = AgentClientEntry, AgentPayback


def seller_required(view):
    """Agent portal pages: commercial agents, and market supervisors linked to their own agent account (SIM)."""
    @roles_required(R.AGENT, R.MARKET)
    @wraps(view)
    def inner(request, *args, **kwargs):
        if not request.user.sells:
            raise Forbidden(_("Ask the administrator to link your login to your own agent account (your business "
                              "SIM) to supply POS."))
        return view(request, *args, **kwargs)

    return inner


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
    out = ag.own_pos(agent).filter(er_stock__lt=limit).count()
    return render(request, "core/agent/home.html", {
        "cp": agent, "s": ag.summary(agent), "rec": ag.daily_reconciliation(agent, today), "out_of_stock": out,
        "limit": limit})


@seller_required
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

    limit, low = company().out_of_stock_er, company().low_stock_er
    mine = request.GET.get("mine") == "1"
    if mine:
        qs = qs.filter(pk__in=ag.own_pos(agent).values("pk"))
    level = request.GET.get("level") or ("OUT" if request.GET.get("out") == "1" else "")
    if level == "OUT":
        qs = qs.filter(er_stock__lt=limit)
    elif level == "LOW":
        qs = qs.filter(er_stock__gte=limit, er_stock__lt=low)
    elif level == "IN":
        qs = qs.filter(er_stock__gte=low)
    out = level == "OUT"
    from django.core.paginator import Paginator
    from django.db.models import F

    # every POS of the zone by name; a stock tab orders from the lowest ER stock to the highest; 100 per page
    qs = qs.order_by(F("er_stock").asc(nulls_last=True), "name") if level else qs.order_by("name")
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    ids = [p.pk for p in page]
    live = ag.live_entries(agent).filter(pos_id__in=ids)
    last = dict(live.filter(kind=E.Kind.SUPPLY).values("pos").annotate(d=Max("business_date"))
                .values_list("pos", "d"))
    owed = {r["pos"]: (r["t"] or 0) + (r["e"] or 0) + (r["c"] or 0) - (r["p"] or 0)
            for r in live.values("pos").annotate(t=Sum("uve"), e=Sum("er"), c=Sum("cash"), p=Sum("paid"))}
    rows = [(p, owed.get(p.pk, 0), last.get(p.pk), p.microzone_id and p.microzone.agent_id == agent.pk,
             p.stock_level(limit, low)) for p in page]
    keep = request.GET.copy()
    keep.pop("page", None)
    base = keep.copy()
    base.pop("level", None)
    base.pop("out", None)
    return render(request, "core/agent/pos_list.html", {"rows": rows, "q": q, "mine": mine, "out": out,
                                                        "level": level, "limit": limit, "zone": ag.zone_of(agent),
                                                        "page": page, "keep": keep.urlencode(),
                                                        "base": base.urlencode()})


SUPERVISORS = (R.MANAGER, R.FINANCE, R.SUPERADMIN, R.MARKET)


def _followed_pos(request):
    """The POS of the OOS / possible destocash lists: an agent's own POS (his microzones); for the supervisors
    every POS, a market supervisor those of his zone."""
    qs = Counterparty.objects.filter(kind=Counterparty.Kind.POS, active=True)
    if request.user.sells:
        return ag.own_pos(request.user.counterparty)
    return qs.filter(request.user.pos_scope_q())


@roles_required(R.MARKET)
def market_sim(request):
    """A market supervisor starts supplying: his business SIM becomes his own agent account (created, or an agent
    account with that SIM that no other login uses), linked to his login."""
    nxt = _safe_next(request) or reverse("agent_oos")
    if request.method != "POST":
        return redirect(nxt)
    from core.models import User
    from core.simnumbers import SimNumberError, normalize_sim

    user = request.user
    try:
        sim = normalize_sim(request.POST.get("sim") or "")
    except SimNumberError as e:
        messages.error(request, _(str(e)))
        return redirect(nxt)
    cp = Counterparty.objects.filter(business_sim=sim).first()
    if cp is not None and cp.kind != Counterparty.Kind.AGENT:
        messages.error(request, _("%(sim)s is registered as a POS, not as your agent SIM.") % {"sim": sim})
        return redirect(nxt)
    if cp is not None and User.objects.filter(counterparty=cp).exclude(pk=user.pk).exists():
        messages.error(request, _("%(sim)s is already the account of another login.") % {"sim": sim})
        return redirect(nxt)
    with transaction.atomic():
        if cp is None:
            cp = Counterparty.objects.create(kind=Counterparty.Kind.AGENT, business_sim=sim, name=str(user)[:150],
                                             master_zone=user.subzone.master if user.subzone_id else None,
                                             created_by=user)
            ops.log_event(user, "counterparty_created", id=cp.id, sim=sim, kind=cp.kind, via="market_sim")
        user.counterparty = cp
        user.save(update_fields=["counterparty"])
        ops.log_event(user, "market_sim_linked", counterparty=cp.id)
    messages.success(request, _("Your SIM %(sim)s is ready: you can now supply and destocash the POS.") % {"sim": sim})
    return redirect(nxt)


@roles_required(R.AGENT, *SUPERVISORS)
def agent_oos(request):
    """OOS: only the logged-in agent's POS (those of the microzones he is in charge of), from out of stock to the
    highest ER stock; POS without an imported stock come last."""
    from django.core.paginator import Paginator
    from django.db.models import F

    from core.services.branding import company

    limit, low = company().out_of_stock_er, company().low_stock_er
    mine = _followed_pos(request).select_related("microzone__agent")
    q = (request.GET.get("q") or "").strip()
    qs = mine
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(microzone__name__icontains=q) | Q(om_account_name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:])
        qs = qs.filter(cond)
    level = request.GET.get("level") or ("OUT" if request.GET.get("out") == "1" else "")
    if level == "OUT":
        qs = qs.filter(er_stock__lt=limit)
    elif level == "LOW":
        qs = qs.filter(er_stock__gte=limit, er_stock__lt=low)
    elif level == "IN":
        qs = qs.filter(er_stock__gte=low)
    qs = qs.order_by(F("er_stock").asc(nulls_last=True), F("uve_stock").asc(nulls_last=True), "name")
    page = Paginator(qs, 200).get_page(request.GET.get("page"))
    tot = mine.aggregate(er=Sum("er_stock"), uve=Sum("uve_stock"), com=Sum("commission_balance"))
    last = mine.exclude(stock_at=None).order_by("-stock_at").values_list("stock_at", flat=True).first()
    keep = request.GET.copy()
    keep.pop("page", None)
    return render(request, "core/agent/oos.html", {
        "page": page, "rows": [(p, p.stock_level(limit, low)) for p in page], "limit": limit, "low": low, "q": q,
        "level": level, "out": level == "OUT", "keep": keep.urlencode(), "last": last, "tot": tot,
        "n_pos": mine.count(), "n_out": mine.filter(er_stock__lt=limit).count(),
        "n_low": mine.filter(er_stock__gte=limit, er_stock__lt=low).count(),
        "n_in": mine.filter(er_stock__gte=low).count(), "n_unknown": mine.filter(er_stock=None).count(),
        "supervise": not request.user.sells, "zone": request.user.market_zone})


@roles_required(R.AGENT, *SUPERVISORS)
def agent_destock(request):
    """Possible destocash: the logged-in agent's POS holding OM (UVE) stock, from the most to the least."""
    from django.core.paginator import Paginator

    mine = _followed_pos(request).filter(uve_stock__gt=0).select_related("microzone__agent")
    q = (request.GET.get("q") or "").strip()
    qs = mine
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q) | Q(microzone__name__icontains=q) | Q(om_account_name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:])
        qs = qs.filter(cond)
    qs = qs.order_by("-uve_stock", "name")
    page = Paginator(qs, 200).get_page(request.GET.get("page"))
    keep = request.GET.copy()
    keep.pop("page", None)
    tot = mine.aggregate(om=Sum("uve_stock"), n=Count("id"))
    return render(request, "core/agent/destock.html", {
        "page": page, "q": q, "keep": keep.urlencode(), "tot": tot, "supervise": not request.user.sells,
        "zone": request.user.market_zone,
        "last": mine.exclude(stock_at=None).order_by("-stock_at").values_list("stock_at", flat=True).first()})


@seller_required
def agent_destock_record(request):
    """Possible destocash, one tap: "Cash" = the agent gives the POS cash for its OM (a cash supply paid back in UVE
    at once, nothing owed); "Float" = the agent sends the POS float for its OM (a float exchange). The POS's OM stock
    shown in the list goes down by the amount until the next stock file."""
    if request.method != "POST":
        return redirect("agent_destock")
    from core.money import to_money

    nxt = _safe_next(request) or reverse("agent_destock")
    pos = _followed_pos(request).filter(pk=request.POST.get("pos") or 0).first()
    how = request.POST.get("how")
    if pos is None or how not in ("CASH", "FLOAT"):
        messages.error(request, _("This POS is not one of yours."))
        return redirect(nxt)
    try:
        amount = to_money(request.POST.get("amount") or "0")
        if amount <= 0:
            raise BusinessError(_("Enter the amount."), "amount")
        idem = uuid.UUID(request.POST.get("idem")) if request.POST.get("idem") else None
        note = _("Destocash %(pos)s") % {"pos": pos.name}
        model = AgentClientEntry if how == "CASH" else AgentExchange
        if idem and model.objects.filter(idem=idem).exists():  # the same tap twice: recorded once
            messages.info(request, _("Already recorded."))
            return redirect(nxt)
        with transaction.atomic():
            if how == "CASH":
                ag.record_supply(request.user, client_kind="POS", pos_sim=pos.business_sim, cash=amount,
                                 paid_uve=amount, note=note, idempotency_key=idem)
            else:
                ag.record_exchange(request.user, direction="SEND_FLOAT_UVE", amount=amount,
                                   other_phone=pos.business_sim, note=note, idempotency_key=idem)
            if pos.uve_stock is not None:
                Counterparty.objects.filter(pk=pos.pk).update(uve_stock=max(pos.uve_stock - amount, 0))
        messages.success(request, _("Destocash of %(amt)s FCFA in %(how)s recorded for %(pos)s.") % {
            "amt": fmt(amount), "how": _("cash") if how == "CASH" else _("float"), "pos": pos.name})
    except (BusinessError, ValueError) as e:
        messages.error(request, getattr(e, "message", str(e)))
    except Exception as e:  # noqa: BLE001 - the same tap twice
        from core.services.errors import DuplicateSubmission

        if not isinstance(e, DuplicateSubmission) and e.__class__.__name__ != "IntegrityError":
            raise
        messages.info(request, _("Already recorded."))
    return redirect(nxt)


@seller_required
def agent_coverage(request):
    """How many different POS the agent supplied per day, week and month (a POS supplied twice counts once)."""
    agent = request.user.counterparty
    rep = ag.pos_coverage(agent, timezone.localdate())
    return render(request, "core/agent/coverage.html", rep)


@seller_required
def agent_pos_edit(request, pk):
    """The agent corrects a POS's details (name, business name, contacts, address). The SIM, microzone and stock
    stay with the office."""
    agent = request.user.counterparty
    pos = get_object_or_404(Counterparty, pk=pk, kind=Counterparty.Kind.POS)
    if not ag.in_zone(agent, pos):
        raise Http404
    form = AF.PosInfoForm(request.POST or None, instance=pos)
    if request.method == "POST" and form.is_valid():
        before = {f: getattr(Counterparty.objects.get(pk=pos.pk), f) for f in form.fields}
        form.save()
        from core.services.operations import log_event

        log_event(request.user, "pos_edited_by_agent", pos=pos.pk,
                  changes={f: [str(before[f]), str(getattr(pos, f))] for f in form.fields
                           if before[f] != getattr(pos, f)})
        messages.success(request, _("%(pos)s updated.") % {"pos": pos.name})
        return redirect(_safe_next(request) or reverse("agent_pos_detail", args=[pos.pk]))
    return render(request, "core/agent/pos_edit.html", {"form": form, "pos": pos, "next": _safe_next(request)})


@seller_required
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


@seller_required
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


def _safe_next(request):
    """The page to go back to after recording (OOS, Global POS…): a path of this site only."""
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else ""


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
    fixed_pos = None
    pos_sim = (request.POST.get("fixed_pos") or g.get("pos") or "") if "client_kind" in form.fields else ""
    if pos_sim:  # opened from OOS / a POS page: the client is this POS, nothing to choose
        fixed_pos = Counterparty.objects.filter(kind=Counterparty.Kind.POS, business_sim=ag._norm(pos_sim)).first()
        if fixed_pos:
            from django import forms as djforms

            form.fields["client_kind"].widget = djforms.HiddenInput()
            form.fields["pos_sim"].widget = djforms.HiddenInput()
            for n in ("shop", "client_phone"):
                form.fields.pop(n, None)
            if not form.is_bound:
                form.initial.update({"client_kind": "POS", "pos_sim": fixed_pos.business_sim})
                if "product" in form.fields:  # out of stock = out of ER; Destocash = give cash for the POS's OM
                    form.initial["product"] = g.get("product") if g.get("product") in ("UVE", "ER", "CASH") else "ER"
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
                return redirect(_safe_next(request) or request.path)
        if _wants_json(request):
            return _json_errors(form)
    from core.services.branding import company

    co = company()
    ctx = {"form": form, "title": title, "icon": icon, "labels": labels or {},
           "pos_lookup": "pos_sim" in form.fields and not fixed_pos, "fixed_pos": fixed_pos,
           "next": _safe_next(request),
           "fixed_level": fixed_pos.stock_level(co.out_of_stock_er, co.low_stock_er) if fixed_pos else None}
    ctx.update(extra or {})
    return render(request, "core/agent/form.html", ctx)


def _verb(en, fr):
    # a button is a verb; some catalogue entries ("Supply", "Transfer") are nouns in French
    return fr if (get_language() or "").startswith("fr") else en


@seller_required
def agent_supply(request):
    return _entry_form(
        request, AF.AgentSupplyForm, ag.record_supply, title=_("Supply a client"), icon="supply",
        ok_msg=lambda e: _("%(ref)s: %(amt)s FCFA supplied to %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.total), "who": e.client_label},
        code_of=ag.ussd_code,
        labels={"dial": _verb("Supply", "Approvisionner"), "plain": _("Record")})


@seller_required
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


@seller_required
def agent_collect(request):
    return _entry_form(
        request, AF.ClientPaymentForm, ag.record_client_payment, title=_("A client pays me"), icon="payment",
        ok_msg=lambda e: _("%(ref)s: %(amt)s FCFA received from %(who)s.") % {
            "ref": e.reference, "amt": fmt(e.paid), "who": e.client_label})


@seller_required
def agent_exchange(request):
    return _entry_form(
        request, AF.ExchangeForm, ag.record_exchange, title=_("Float exchange"), icon="convert",
        ok_msg=lambda x: _("%(ref)s: %(amt)s FCFA — %(what)s.") % {
            "ref": x.reference, "amt": fmt(x.amount), "what": x.get_direction_display()},
        code_of=ag.exchange_code, labels={"dial": _verb("Exchange", "Échanger"), "plain": _("Record")},
        extra={"recent": AgentExchange.objects.filter(agent=request.user.counterparty)[:10],
               "today": douala_today()})


@seller_required
@require_POST
def agent_exchange_cancel(request, pk):
    x = get_object_or_404(AgentExchange, pk=pk, agent=request.user.counterparty)
    try:
        ag.cancel_exchange(request.user, x, request.POST.get("reason"))
        messages.success(request, _("%(ref)s cancelled.") % {"ref": x.reference})
    except BusinessError as exc:
        messages.error(request, exc.message)
    return redirect("agent_exchange")


@seller_required
def agent_debtors(request):
    """Everyone who owes the agent, with what is outstanding and a button to receive or ask for the payment."""
    agent = request.user.counterparty
    rows = [g for g in ag.clients_owing(agent) if g["owed"] > 0]
    for g in rows:
        g["collect_q"] = (f"pos={g['pos'].business_sim}" if g["pos"] else
                          f"shop={g['shop'].pk}" if g["shop"] else f"phone={g['phone']}")
        g["sms"] = _("Hello %(who)s, you still owe %(amt)s FCFA to %(agent)s (%(sim)s). Thank you for paying.") % {
            "who": g["label"], "amt": fmt(g["owed"]), "agent": agent.name, "sim": agent.business_sim}
        g["key"] = (f"POS:{g['pos'].business_sim}" if g["pos"] else
                    f"SHOP:{g['shop'].pk}" if g["shop"] else f"OTHER:{g['phone']}")
        g["idem"] = uuid.uuid4()  # one per debtor: a double tap never records twice
        g["wa"] = (f"https://wa.me/237{g['number']}?text=" + quote(g["sms"])) if g.get("number") else ""
    return render(request, "core/agent/debtors.html", {
        "rows": rows, "total": sum((g["owed"] for g in rows), 0)})


@seller_required
def agent_debtor_pay(request):
    """One tap on the debtors list: "Paid cash / UVE / float" closes the whole debt in that way; "Partial" records
    the amount typed (in the way chosen)."""
    if request.method != "POST":
        return redirect("agent_debtors")
    agent = request.user.counterparty
    kind, _sep, ref = (request.POST.get("client") or "").partition(":")
    g = next((x for x in ag.clients_owing(agent)
              if (kind == "POS" and x["pos"] and x["pos"].business_sim == ref)
              or (kind == "SHOP" and x["shop"] and str(x["shop"].pk) == ref)
              or (kind == "OTHER" and not x["pos"] and not x["shop"] and x["phone"] == ref)), None)
    method = request.POST.get("method")
    if g is None or g["owed"] <= 0 or method not in ("CASH", "UVE", "MTN"):
        messages.error(request, _("This client owes you nothing any more."))
        return redirect("agent_debtors")
    try:
        amount = g["owed"]
        if request.POST.get("partial"):
            from core.money import to_money

            amount = to_money(request.POST.get("amount") or "0")
            if amount <= 0:
                raise BusinessError(_("Enter the amount received."), "amount")
            if amount > g["owed"]:
                raise BusinessError(_("Only %(amt)s FCFA is owed: use Paid instead.") % {"amt": fmt(g["owed"])},
                                    "amount")
        client = {"client_kind": g["kind_code"], "pos_sim": g["pos"].business_sim if g["pos"] else "",
                  "shop": g["shop"], "client_phone": g["phone"] or "", "client_name": g["label"]}
        ag.record_client_payment(request.user, **client, **{{"CASH": "cash", "UVE": "uve", "MTN": "mtn"}[method]:
                                                             amount},
                                 note=_("Partial payment") if request.POST.get("partial") else _("Paid in full"),
                                 idempotency_key=uuid.UUID(request.POST.get("idem")) if request.POST.get("idem")
                                 else None)
        left = g["owed"] - amount
        messages.success(request, (_("%(who)s paid %(amt)s FCFA. Still owed: %(left)s FCFA.") if left > 0 else
                                   _("%(who)s paid %(amt)s FCFA: debt closed.")) % {
            "who": g["label"], "amt": fmt(amount), "left": fmt(left)})
    except (BusinessError, ValueError) as e:
        messages.error(request, getattr(e, "message", str(e)))
    except Exception as e:  # noqa: BLE001 - the same tap twice
        from core.services.errors import DuplicateSubmission

        if not isinstance(e, DuplicateSubmission):
            raise
    return redirect(reverse("agent_debtors") + f"#d-{request.POST.get('anchor', '')}")


@seller_required
def agent_payback(request):
    return _entry_form(
        request, AF.PaybackForm, ag.request_payback, title=_("Pay back the company"), icon="transfer",
        ok_msg=lambda p: (_("%(code)s: %(amt)s FCFA to %(who)s, waiting for confirmation.") if p.shop_id else
                          _("%(code)s: %(amt)s FCFA paid to %(who)s, kept in your records (the cashier records the "
                            "payment on his side).")) % {"code": p.code, "amt": fmt(p.total), "who": p.receiver},
        code_of=ag.payback_code,
        labels={"dial": _verb("Pay back", "Rembourser"), "plain": _("Record")},
        extra={"s": ag.summary(request.user.counterparty)})


@seller_required
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


@seller_required
def agent_office(request):
    """Every supply and destockage the office recorded on the agent, with daily totals per product."""
    agent = request.user.counterparty
    d_from, d_to = _period(request)
    qs = ag.office_supplies(agent).filter(business_date__gte=d_from, business_date__lte=d_to)
    pays = ag.office_payments(agent).filter(business_date__gte=d_from, business_date__lte=d_to)
    keys = ("UVE", "ER", "CASH", "DESTOCK", "supplied", "p_CASH", "p_UVE", "p_MTN", "paid", "net")
    per_day = {}

    def day(d):
        return per_day.setdefault(d, {"day": d, **{k: 0 for k in keys}})
    for t in qs:
        row = day(t.business_date)
        row["DESTOCK" if t.type == "DESTOCKAGE" else t.product] += t.amount
        row["supplied"] += t.amount
    for t in pays:
        row = day(t.business_date)
        m = t.payment_method if t.payment_method in ("CASH", "UVE", "MTN") else "CASH"
        row[f"p_{m}"] += t.amount
        row["paid"] += t.amount
    for r in per_day.values():
        r["net"] = r["supplied"] - r["paid"]
    days = sorted(per_day.values(), key=lambda r: r["day"], reverse=True)
    totals = {k: sum(r[k] for r in days) for k in keys}
    return render(request, "core/agent/office.html", {
        "days": days, "totals": totals, "page": Paginator(qs, 50).get_page(request.GET.get("page")),
        "payments": pays[:100], "s": ag.summary(agent), "d_from": d_from, "d_to": d_to})


TABS = (("clients", _l("My clients")), ("agents", _l("Agents")), ("paybacks", _l("My paybacks")),
        ("office", _l("All office records")))


@seller_required
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


@seller_required
@require_POST
def agent_entry_cancel(request, pk):
    e = get_object_or_404(E, pk=pk, agent=request.user.counterparty)
    try:
        ag.cancel_entry(request.user, e, request.POST.get("reason"))
        messages.success(request, _("%(ref)s cancelled.") % {"ref": e.reference})
    except BusinessError as exc:
        messages.error(request, exc.message)
    return redirect("agent_activity")


@seller_required
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
