"""Capital: injections, withdrawals and transfers by the finance manager, senior manager and super admin."""
from datetime import date, timedelta

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core import forms_capital as CF
from core.models import Transaction
from core.money import fmt
from core.services import capital as cap
from core.services.days import douala_today

from .base import R, roles_required, run_service

CAPITAL_ROLES = (R.MANAGER, R.FINANCE, R.SUPERADMIN)
OPS = {
    "inject": (CF.InjectForm, cap.inject, _l("Inject capital"),
               _l("New money put into the business by the owners. It increases the chosen account and the owners' "
                  "capital."), "payment"),
    "withdraw": (CF.WithdrawForm, cap.withdraw, _l("Withdraw capital"),
                 _l("Money taken out of the business by the owners. It decreases the chosen account and the owners' "
                    "capital."), "supply"),
    "transfer": (CF.CapitalTransferForm, cap.transfer, _l("Transfer capital"),
                 _l("Move money between any two company accounts: cashiers, master SIMs, banks and shops. The total "
                    "capital does not change."), "transfer"),
}


@roles_required(*CAPITAL_ROLES)
def capital(request):
    today = douala_today()
    try:
        d_from = date.fromisoformat(request.GET.get("from") or "")
    except ValueError:
        d_from = today - timedelta(days=30)
    try:
        d_to = date.fromisoformat(request.GET.get("to") or "")
    except ValueError:
        d_to = today
    typ = request.GET.get("type") or ""
    qs = Transaction.objects.filter(type__in=cap.CAPITAL_TYPES, business_date__gte=d_from, business_date__lte=d_to) \
        .select_related("source_account", "dest_account", "entered_by", "shop", "correction").order_by("-posted_at")
    if typ in cap.CAPITAL_TYPES:
        qs = qs.filter(type=typ)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/capital.html", {
        "tot": cap.totals(), "page": page, "d_from": d_from, "d_to": d_to, "typ": typ,
        "types": [(t, t.label) for t in cap.CAPITAL_TYPES]})


@roles_required(*CAPITAL_ROLES)
def capital_op(request, op):
    if op not in OPS:
        raise Http404
    form_class, fn, title, intro, icon = OPS[op]
    form = form_class(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: fn(request.user, idempotency_key=form.cleaned_data["idem"], **form.params()),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("txn_detail", pk=t.pk))
        if resp:
            return resp
    return render(request, "core/entry_form.html", {"form": form, "title": title, "intro": intro, "icon": icon})
