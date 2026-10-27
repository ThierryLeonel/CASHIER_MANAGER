"""Cashier data-entry screens. Every POST is idempotent via a per-form UUID."""

from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms as F
from core.models import CashManager, CashManagerExchange, ConversionRate, Counterparty
from core.money import fmt
from core.services import operations as ops

from .base import R, run_service, roles_required


def _entry(request, form_class, op, title, intro="", template="core/entry_form.html", extra=None):
    form = form_class(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        params = form.params()
        resp = run_service(
            request, form, lambda: op(request.user, idempotency_key=form.cleaned_data["idem"], **params),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("txn_detail", pk=t.pk),
        )
        if resp:
            return resp
    ctx = {"form": form, "title": title, "intro": intro, "customers": Counterparty.objects.filter(active=True)}
    ctx.update(extra or {})
    return render(request, template, ctx)


@roles_required(R.CASHIER)
def supply_new(request):
    return _entry(request, F.SupplyForm, ops.record_supply, _("New supply"),
                  _("Record a completed UVE, ER or cash supply. ER must come from your master zone's master SIM."))


@roles_required(R.CASHIER)
def destockage_new(request):
    return _entry(request, F.DestockageForm, ops.record_destockage, _("Destockage"),
                  _("You gave cash to a POS, and the POS sent the same value in UVE to an agent. The agent owes the "
                    "company; company UVE does not change; the POS owes nothing."))


@roles_required(R.CASHIER)
def collection_new(request):
    return _entry(request, F.CollectionForm, ops.record_collection, _("Collect a payment"),
                  _("Record a confirmed payment. It reduces the customer's single combined balance."))


@roles_required(R.CASHIER)
def expense_new(request):
    return _entry(request, F.ExpenseForm, ops.record_expense, _("Record an expense"),
                  _("Cash paid out of your drawer. Expenses never reduce an agent's debt."))


@roles_required(R.CASHIER)
def transfer_new(request):
    return _entry(request, F.InternalTransferForm, ops.record_internal_transfer, _("Internal transfer"),
                  _("A completed move of cash or electronic value between company accounts. No customer is affected."))


@roles_required(R.CASHIER)
def conversion_new(request):
    rate = ConversionRate.current()
    intro = _("Current uplift: +%(pct)s%% (rate stored on each conversion; ER rounded half-up to whole FCFA).") % {
        "pct": rate.percent if rate else "?"}
    return _entry(request, F.ConversionForm, ops.record_conversion, _("Convert UVE to ER"), intro,
                  extra={"rate": rate.uplift_rate if rate else None})


# ---------------------------------------------------------------- cash managers
@roles_required(R.CASHIER, R.MANAGER, R.FINANCE, R.SUPERADMIN)
def exchanges(request):
    from core.services.ledger import balance

    cms = [(c, balance(c.account)) for c in CashManager.objects.all()]
    exs = CashManagerExchange.objects.select_related("cash_manager", "created_by")[:200]
    return render(request, "core/exchanges.html", {"cms": cms, "exchanges": exs})


@roles_required(R.CASHIER)
def exchange_new(request):
    form = F.ExchangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: ops.create_exchange(request.user, idempotency_key=form.cleaned_data["idem"],
                                                       **form.params()),
            success=lambda ex: _("Exchange agreement %(ref)s recorded. Record each side when it actually happens.")
            % {"ref": ex.reference},
            on_success=lambda ex: redirect("exchange_detail", pk=ex.pk))
        if resp:
            return resp
    return render(request, "core/entry_form.html", {
        "form": form, "title": _("New cash-manager exchange"),
        "intro": _("An agreement moves no money. Company balances change only when you record each completed side.")})


@roles_required(R.CASHIER, R.MANAGER, R.FINANCE, R.SUPERADMIN)
def exchange_detail(request, pk):
    ex = get_object_or_404(CashManagerExchange.objects.select_related("cash_manager"), pk=pk)
    form = F.ExchangeLegForm(request.user, request.POST or None) if request.user.is_cashier else None
    if form is not None and request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: ops.record_exchange_leg(request.user, exchange=ex,
                                                           idempotency_key=form.cleaned_data["idem"], **form.params()),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("exchange_detail", pk=ex.pk))
        if resp:
            return resp
    give, recv = ex.side_totals()
    legs = ex.legs.select_related("entered_by", "source_account__sim", "dest_account__sim").order_by("posted_at")
    return render(request, "core/exchange_detail.html", {"ex": ex, "form": form, "give": give, "recv": recv,
                                                         "legs": legs, "give_kind": ex.give_kind(),
                                                         "receive_kind": ex.receive_kind()})
