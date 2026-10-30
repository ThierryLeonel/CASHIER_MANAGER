"""Cashier data-entry screens. Every POST is idempotent via a per-form UUID."""

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms as F
from core.models import CashManager, CashManagerExchange, ConversionRate, Counterparty
from core.money import fmt
from core.services import operations as ops

from .base import R, acting_cashier, run_service, roles_required

DESK = (R.CASHIER, R.SUPERADMIN)


def _desk(request):
    """(desk cashier, acting_for). For a super admin, the chosen cashier."""
    if request.user.is_superadmin:
        actor = acting_cashier(request)
        return actor, actor
    return request.user, None


def _first(t):
    return t[0] if isinstance(t, list) else t


def _entry(request, form_class, op, title, intro="", template="core/entry_form.html", extra=None):
    desk, acting = _desk(request)
    if desk is None:
        from django.contrib import messages

        messages.info(request, _("Choose the cashier you are recording for."))
        return redirect(f"/act-as/?next={request.path}")
    form = form_class(desk, request.POST or None)
    if request.method == "POST" and form.is_valid():
        params = form.params()
        resp = run_service(
            request, form, lambda: op(request.user, idempotency_key=form.cleaned_data["idem"], acting_for=acting,
                                      **params),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": _first(t).display_type, "ref": _first(t).receipt_no or _first(t).reference,
                "amt": fmt(sum(x.amount for x in t) if isinstance(t, list) else t.amount)},
            on_success=lambda t: redirect("txn_detail", pk=_first(t).pk),
        )
        if resp:
            return resp
    ctx = {"form": form, "title": title, "intro": intro, "customers": Counterparty.objects.filter(active=True)}
    ctx.update(extra or {})
    return render(request, template, ctx)


@roles_required(*DESK)
def supply_new(request):
    return redirect("supply_agent")


@roles_required(*DESK)
def supply_agent(request):
    return _entry(request, F.AgentSupplyForm, ops.record_supplies, _("Supply to a commercial agent"),
                  _("Cash, UVE and ER given to a commercial agent, in one record. The agent's balance increases by "
                    "the total."), extra={"icon": "supply"})


@roles_required(*DESK)
def supply_pos(request):
    return _entry(request, F.PosSupplyForm, ops.record_supplies, _("Supply to a point of sale (POS)"),
                  _("Exceptional direct supply to a POS. The POS owes the company, unless you name an agent who "
                    "explicitly assumes the debt."), extra={"icon": "store"})


@roles_required(*DESK)
def destockage_new(request):
    return _entry(request, F.DestockageForm, ops.record_destockage, _("Destockage"),
                  _("You gave cash to a POS, and the POS sent the same value in UVE to an agent. The agent owes the "
                    "company; company UVE does not change; the POS owes nothing."), extra={"icon": "destock"})


@roles_required(*DESK)
def collection_new(request):
    desk, acting = _desk(request)
    if desk is None:
        from django.contrib import messages

        messages.info(request, _("Choose the cashier you are recording for."))
        return redirect(f"/act-as/?next={request.path}")
    form = F.CollectionForm(desk, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form,
            lambda: ops.record_payment(request.user, idempotency_key=form.cleaned_data["idem"], acting_for=acting,
                                       **form.params()),
            success=lambda ts: _("Payment %(ref)s recorded: %(amt)s FCFA.") % {
                "ref": ts[0].receipt_no or ts[0].reference, "amt": fmt(sum(t.amount for t in ts))},
            on_success=lambda ts: redirect("txn_detail", pk=(ts[0] if isinstance(ts, list) else ts).pk))
        if resp:
            return resp
    return render(request, "core/entry_form.html", {
        "form": form, "title": _("Collect a payment"), "icon": "payment", "payment_form": True,
        "intro": _("One payment can be made in cash, UVE, MTN float and bank deposit at the same time. It reduces "
                   "the customer's single combined balance."),
        "customers": Counterparty.objects.filter(active=True)})


@roles_required(*DESK)
def expense_new(request):
    return _entry(request, F.ExpenseForm, ops.record_expense, _("Record an expense"),
                  _("Paid from your cash drawer, UVE wallet or MTN float. Expenses never reduce an agent's debt."), extra={"icon": "expense"})


@roles_required(*DESK)
def transfer_new(request):
    return _entry(request, F.InternalTransferForm, ops.record_internal_transfer, _("Internal transfer"),
                  _("A completed move between company accounts of the same kind: MTN float, UVE or bank. "
                    "No customer is affected."), extra={"icon": "transfer"})


@roles_required(*DESK)
def conversion_new(request):
    rate = ConversionRate.current()
    intro = _("Current uplift: +%(pct)s%% (rate stored on each conversion; ER rounded half-up to whole FCFA).") % {
        "pct": rate.percent if rate else "?"}
    return _entry(request, F.ConversionForm, ops.record_conversion, _("Convert UVE to ER"), intro,
                  extra={"rate": rate.uplift_rate if rate else None, "icon": "convert"})


# ---------------------------------------------------------------- cash managers
@roles_required(R.CASHIER, R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
def exchanges(request):
    from core.services.ledger import balance

    cms = [(c, balance(c.account)) for c in CashManager.objects.all()]
    exs = CashManagerExchange.objects.select_related("cash_manager", "created_by")
    zids = request.user.zone_ids_in_scope()
    if zids is not None:  # junior manager: exchanges started in his zone
        exs = exs.filter(created_by__subzone__in=zids)
    exs = exs[:200]
    return render(request, "core/exchanges.html", {"cms": cms, "exchanges": exs})


@roles_required(*DESK)
def exchange_new(request):
    desk, acting = _desk(request)
    if desk is None:
        return redirect(f"/act-as/?next={request.path}")
    form = F.ExchangeForm(desk, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: ops.record_exchange(request.user, idempotency_key=form.cleaned_data["idem"],
                                                       acting_for=acting, **form.params()),
            success=lambda ex: (_("Exchange %(ref)s recorded. Balance to be served: %(amt)s FCFA.")
                                % {"ref": ex.reference, "amt": fmt(ex.to_serve)}),
            on_success=lambda ex: redirect("exchange_detail", pk=ex.pk))
        if resp:
            return resp
    return render(request, "core/entry_form.html", {
        "form": form, "title": _("New cash-manager exchange"), "exchange_form": True,
        "intro": _("Record what the company gives and what the cash manager has served. If only part is served "
                   "now (e.g. 8M of 10M), the balance stays open and is recorded later on the exchange page.")})


@roles_required(R.CASHIER, R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
def exchange_detail(request, pk):
    ex = get_object_or_404(CashManagerExchange.objects.select_related("cash_manager", "created_by"), pk=pk)
    zids = request.user.zone_ids_in_scope()
    if zids is not None and ex.created_by.subzone_id not in zids:
        raise Http404
    desk, acting = _desk(request) if request.user.role in DESK else (None, None)
    form = F.ExchangeLegForm(desk, request.POST or None, exchange=ex) if desk is not None else None
    if form is not None and request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: ops.record_exchange_leg(request.user, exchange=ex, acting_for=acting,
                                                           idempotency_key=form.cleaned_data["idem"], **form.params()),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.display_type, "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("exchange_detail", pk=ex.pk))
        if resp:
            return resp
    give, recv = ex.side_totals()
    legs = ex.legs.select_related("entered_by", "source_account__sim", "dest_account__sim").order_by("posted_at")
    return render(request, "core/exchange_detail.html", {"ex": ex, "form": form, "give": give, "recv": recv,
                                                         "legs": legs, "give_kind": ex.give_kind() or "",
                                                         "receive_kind": ex.receive_kind() or ""})
