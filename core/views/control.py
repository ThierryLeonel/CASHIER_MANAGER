"""Closing, discrepancies, opening balances, conversion rate, reports."""
import uuid
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.db import transaction as db_transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms as F
from core.models import Account, Closing, ConversionRate, Correction, Counterparty, Discrepancy, Transaction, TxType
from core.money import fmt
from core.pdf import report_pdf
from core.reports.defs import REPORTS
from core.reports.engine import FilterForm, Filters
from core.reports.xlsx import report_xlsx
from core.services import closing as cl
from core.services import operations as ops
from core.services.days import douala_today, peek_open_day
from core.services.errors import BusinessError, DuplicateSubmission

from .base import R, STAFF, SUPERVISORS, roles_required, run_service


# ---------------------------------------------------------------- closing
@roles_required(R.CASHIER)
def close_day(request):
    accounts = cl.closing_accounts(request.user)
    day = peek_open_day(request.user)
    form = F.ClosingForm(accounts, request.POST or None)
    if request.method == "POST" and form.is_valid():
        if request.POST.get("confirm") != "1":
            form.add_error(None, _("Tick the confirmation box to close the day."))
        else:
            resp = run_service(request, form, lambda: cl.close_day(request.user, actuals=form.actuals(),
                                                                  notes=form.cleaned_data["notes"]),
                               success=lambda c: _("Business day %(d)s closed. New entries now belong to the next "
                                                   "business day.") % {"d": c.cashier_day.business_date},
                               on_success=lambda c: redirect("closing_detail", pk=c.pk))
            if resp:
                return resp
    return render(request, "core/close_day.html", {"form": form, "day": day, "accounts": accounts})


def _zone_ok(user, zone):
    """Cashiers see their own subzone; junior managers their zone tree."""
    if user.is_cashier:
        return zone is not None and zone.id == user.subzone_id
    ids = user.zone_ids_in_scope()
    return ids is None or (zone is not None and zone.id in ids)


def _zone_filter(user, qs, field):
    if user.is_cashier:
        return qs.filter(**{field: user.subzone_id})
    ids = user.zone_ids_in_scope()
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


def _visible_closing(request, pk):
    c = get_object_or_404(Closing.objects.select_related("cashier_day__zone__parent", "closed_by"), pk=pk)
    if not _zone_ok(request.user, c.cashier_day.zone):
        raise Http404
    return c


@roles_required(*STAFF)
def closing_detail(request, pk):
    c = _visible_closing(request, pk)
    lines = c.lines.select_related("account__sim", "discrepancy")
    later_corrections = Transaction.objects.filter(type=TxType.REVERSAL, original_business_date=c.cashier_day.business_date,
                                                   reverses__zone=c.cashier_day.zone) \
        .select_related("reverses")
    return render(request, "core/closing_detail.html", {"c": c, "lines": lines,
                                                        "later_corrections": later_corrections})


@roles_required(*STAFF)
def closings(request):
    qs = Closing.objects.select_related("cashier_day__zone__parent", "closed_by").order_by("-closed_at")
    qs = _zone_filter(request.user, qs, "cashier_day__zone_id")
    return render(request, "core/closings.html", {"closings": qs[:200]})


# ---------------------------------------------------------------- discrepancies
@roles_required(*STAFF)
def discrepancies(request):
    qs = Discrepancy.objects.select_related("account__sim", "closing_line__closing__cashier_day__zone",
                                            "resolved_by")
    qs = _zone_filter(request.user, qs, "closing_line__closing__cashier_day__zone_id")
    status = request.GET.get("status", "OPEN")
    if status in ("OPEN", "RESOLVED"):
        qs = qs.filter(status=status)
    return render(request, "core/discrepancies.html", {"rows": qs[:300], "status": status})


@roles_required(*STAFF)
def discrepancy_detail(request, pk):
    d = get_object_or_404(Discrepancy.objects.select_related("account__sim", "closing_line__closing__cashier_day__zone",
                                                             "adjustment_txn", "correction__original"), pk=pk)
    user = request.user
    if not _zone_ok(user, d.closing_line.closing.cashier_day.zone):
        raise Http404
    note_form = F.NoteForm(request.POST if request.POST.get("action") == "note" else None)
    resolve_form = F.ResolveForm(request.POST if request.POST.get("action") == "resolve" else None) \
        if user.role in SUPERVISORS else None
    if request.method == "POST":
        try:
            if request.POST.get("action") == "note" and note_form.is_valid():
                cl.add_discrepancy_note(user, d, note_form.cleaned_data["text"])
                messages.success(request, _("Note added."))
                return redirect("discrepancy_detail", pk=d.pk)
            if request.POST.get("action") == "resolve" and resolve_form is not None and resolve_form.is_valid():
                correction = None
                ref = (resolve_form.cleaned_data.get("correction_ref") or "").strip().upper()
                if ref:
                    correction = Correction.objects.filter(original__reference=ref).first()
                    if correction is None:
                        raise BusinessError(_("No correction found for %(ref)s.") % {"ref": ref})
                cl.resolve_discrepancy(user, d, resolution=resolve_form.cleaned_data["resolution"],
                                       notes=resolve_form.cleaned_data["notes"], correction=correction)
                messages.success(request, _("Discrepancy resolved."))
                return redirect("discrepancy_detail", pk=d.pk)
        except BusinessError as exc:
            messages.error(request, exc.message)
    return render(request, "core/discrepancy_detail.html", {"d": d, "note_form": note_form,
                                                            "resolve_form": resolve_form,
                                                            "notes": d.notes.select_related("author")})


# ---------------------------------------------------------------- admin money settings
OPENING_GRID_KINDS = (Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.ER)


def opening_grid():
    """Control accounts (cash, UVE, ER) by master zone -> subzone / master SIM line, with recorded openings."""
    accts = list(Account.objects.filter(kind__in=OPENING_GRID_KINDS, active=True, zone__isnull=False)
                 .select_related("zone__parent", "custodian", "sim").order_by("id"))
    recorded = {}
    for t in Transaction.objects.filter(type=TxType.OPENING_BALANCE, opening_for_account__in=accts,
                                        correction__isnull=True):
        recorded[t.opening_for_account_id] = t
    masters, totals = {}, {k: Decimal(0) for k in OPENING_GRID_KINDS}
    for a in accts:
        m = a.zone.master
        me = masters.setdefault(m.id, {"zone": m, "rows": {}, "totals": {k: Decimal(0) for k in OPENING_GRID_KINDS}})
        if a.zone.is_master and (a.custodian_id or not m.subzones.exists()):
            key = ("m", a.custodian_id)
            label = _("Master SIM") + (f" · {a.custodian}" if a.custodian_id else "")
        else:
            key, label = ("s", a.zone_id), a.zone.name
        row = me["rows"].setdefault(key, {"label": label, "master_level": key[0] == "m",
                                          "cells": {k: [] for k in OPENING_GRID_KINDS}})
        t = recorded.get(a.id)
        row["cells"][a.kind].append({"account": a, "txn": t})
        if t is not None:
            me["totals"][a.kind] += t.amount
            totals[a.kind] += t.amount
    out = []
    for m in sorted(masters.values(), key=lambda x: x["zone"].name):
        rows = sorted(m["rows"].values(), key=lambda r: (r["master_level"], r["label"]))
        out.append({"zone": m["zone"], "totals": [m["totals"][k] for k in OPENING_GRID_KINDS],
                    "total": sum(m["totals"].values()),
                    "rows": [dict(r, cells=[r["cells"][k] for k in OPENING_GRID_KINDS]) for r in rows]})
    return out, [totals[k] for k in OPENING_GRID_KINDS], sum(totals.values()), accts, recorded


def _save_grid(request, accts, recorded):
    """Record every amount typed in the grid, all or nothing."""
    from core.money import MoneyError, to_money

    try:
        batch = uuid.UUID(request.POST.get("batch", ""))
    except ValueError:
        batch = uuid.uuid4()
    try:
        bdate = date.fromisoformat(request.POST.get("business_date") or "")
    except ValueError:
        messages.error(request, _("Enter the go-live date."))
        return None
    note = (request.POST.get("note") or "").strip()[:255]
    todo = []
    for a in accts:
        raw = (request.POST.get(f"acct_{a.id}") or "").strip()
        if not raw or a.id in recorded:
            continue
        try:
            amt = to_money(raw)
        except MoneyError:
            messages.error(request, _("%(acct)s: enter a whole FCFA amount.") % {"acct": a})
            return None
        if amt:
            todo.append((a, amt))
    if not todo:
        messages.error(request, _("Type at least one opening amount."))
        return None
    try:
        with db_transaction.atomic():
            for a, amt in todo:
                ops.record_opening_balance(request.user, account=a, amount=amt, business_date=bdate,
                                           description=note, idempotency_key=uuid.uuid5(batch, str(a.id)))
    except DuplicateSubmission:
        messages.info(request, _("Already recorded."))
        return redirect("opening_balances")
    except BusinessError as exc:
        messages.error(request, exc.message)
        return None
    total = sum(amt for _a, amt in todo)
    messages.success(request, _("%(n)s opening balances recorded, total %(amt)s FCFA.") % {"n": len(todo),
                                                                                         "amt": fmt(total)})
    return redirect("opening_balances")


@roles_required(R.SUPERADMIN)
def opening_balances(request):
    grid, kind_totals, grand, accts, recorded = opening_grid()
    if request.method == "POST" and request.POST.get("action") == "grid":
        resp = _save_grid(request, accts, recorded)
        if resp:
            return resp
    form = F.OpeningBalanceForm(request.POST if request.POST.get("action") == "single" else None,
                                initial={"business_date": douala_today()})
    form.fields["account"].queryset = form.fields["account"].queryset.exclude(kind__in=OPENING_GRID_KINDS)
    if request.method == "POST" and request.POST.get("action") == "single" and form.is_valid():
        cd = form.cleaned_data
        resp = run_service(
            request, form,
            lambda: ops.record_opening_balance(request.user, account=cd["account"], amount=cd["amount"],
                                               business_date=cd["business_date"], description=cd["description"],
                                               idempotency_key=cd["idem"], split=cd.get("split")),
            success=lambda t: _("Opening balance %(ref)s recorded for %(acct)s.") % {"ref": t.reference,
                                                                                    "acct": cd["account"]},
            on_success=lambda t: redirect("opening_balances"))
        if resp:
            return resp
    done = Transaction.objects.filter(type=TxType.OPENING_BALANCE).select_related(
        "opening_for_account__sim", "entered_by", "correction").order_by("-posted_at")
    return render(request, "core/opening.html", {
        "form": form, "done": done, "grid": grid, "kind_totals": kind_totals, "grand": grand,
        "kinds": [Account.Kind(k).label for k in OPENING_GRID_KINDS], "batch": uuid.uuid4(),
        "today": douala_today(), "posted_date": request.POST.get("business_date") or "",
        "posted": ({a.id: request.POST.get(f"acct_{a.id}", "") for a in accts}
                   if request.POST.get("action") == "grid" else {}),
        "posted_note": request.POST.get("note", "") if request.POST.get("action") == "grid" else "",
        "any_open": any(a.id not in recorded for a in accts)})


@roles_required(R.SUPERADMIN)
def conversion_rate(request):
    form = F.RateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            r = ops.set_conversion_rate(request.user, percent=form.cleaned_data["percent"],
                                        note=form.cleaned_data["note"])
            messages.success(request, _("New uplift +%(p)s%% applies to future conversions only.") % {"p": r.percent})
            return redirect("conversion_rate")
        except BusinessError as exc:
            form.add_error(None, exc.message)
    return render(request, "core/rate.html", {"form": form, "rates": ConversionRate.objects.select_related(
        "created_by")[:50], "current": ConversionRate.current()})


# ---------------------------------------------------------------- reports
AGENT_REPORTS = ("statement",)


@roles_required(*STAFF, R.AGENT)
def reports_index(request):
    if request.user.is_agent:
        return redirect("report", key="statement")
    from core.reports.defs import REPORT_CARDS

    cards = [(key, title, *REPORT_CARDS.get(key, ("chart", ""))) for key, (title, _fn) in REPORTS.items()]
    return render(request, "core/reports_index.html", {"cards": cards})


@roles_required(*STAFF, R.AGENT)
def report(request, key):
    if key not in REPORTS or (request.user.is_agent and key not in AGENT_REPORTS):
        raise Http404
    title, fn = REPORTS[key]
    data = request.GET.copy()
    data.pop("format", None)
    if request.user.is_agent:
        data["customer_sim"] = request.user.counterparty.business_sim
        data.setdefault("preset", "all")
    form = FilterForm(request.user, data or None)
    force = request.user.counterparty if request.user.is_agent else None
    f = Filters.from_form(form, request.user, force_customer=force)
    rep = fn(f)
    fmt_ = request.GET.get("format")
    who = f"{request.user} ({request.user.get_role_display()})"
    if fmt_ in ("xlsx", "pdf"):
        ops.log_event(request.user, "report_export", report=key, format=fmt_, filters=f.text())
    if fmt_ == "xlsx":
        resp = HttpResponse(report_xlsx(rep, who),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{key}-{douala_today()}.xlsx"'
        return resp
    if fmt_ == "pdf":
        resp = HttpResponse(report_pdf(rep, who), content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{key}-{douala_today()}.pdf"'
        return resp
    return render(request, "core/report.html", {
        "rep": rep, "form": form, "key": key, "query": data.urlencode(),
        "customers": Counterparty.objects.filter(active=True) if not request.user.is_agent else [],
    })
