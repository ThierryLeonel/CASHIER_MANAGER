"""Customers, transaction detail, receipts, corrections, responsibility transfers."""
import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from core import forms as F
from core.models import Correction, Counterparty, Entry, MicroZone, Transaction, TxType
from core.money import fmt
from core.pdf import receipt_pdf
from core.reports.engine import FilterForm, Filters
from core.services import corrections as corr
from core.services import operations as ops
from core.services.ledger import balance
from core.simnumbers import display_sim

from .base import R, STAFF, SUPERVISORS, can_view_txn, roles_required, run_service

TX_RELATED = ["cashier__subzone", "zone__parent", "entered_by", "cashier_day__zone", "counterparty", "counterparty_2", "source_account__sim",
              "dest_account__sim", "expense_category", "cash_manager", "exchange", "reverses", "replaces",
              "related_supply"]


# ---------------------------------------------------------------- customers
@roles_required(*STAFF)
def customers(request):
    q = (request.GET.get("q") or "").strip()
    kind = request.GET.get("kind") or ""
    mz = request.GET.get("mz") or ""
    qs = Counterparty.objects.select_related("microzone__subzone", "microzone__agent")
    microzones = MicroZone.objects.select_related("subzone", "agent")
    if request.user.scope_zone is not None:  # junior manager: his zone only
        microzones = microzones.filter(subzone__parent=request.user.scope_zone)
    if mz.isdigit():
        qs = qs.filter(Q(microzone_id=mz) | Q(controlled_microzones=mz)).distinct()
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:])
        qs = qs.filter(cond)
    if kind:
        qs = qs.filter(kind=kind)
    bals = dict(Entry.objects.filter(account__counterparty__in=qs).values_list("account__counterparty")
                .annotate(s=Sum("amount")))
    page = Paginator(qs.order_by("kind", "business_sim"), 50).get_page(request.GET.get("page"))
    rows = [(c, bals.get(c.id, 0)) for c in page]
    return render(request, "core/customers.html", {"rows": rows, "page": page, "q": q, "kind": kind,
                                                   "kinds": Counterparty.Kind.choices, "mz": mz,
                                                   "microzones": microzones})


@roles_required(*STAFF)
def customer_new(request):
    form = F.CounterpartyForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.instance.created_by = request.user
        cp = form.save()
        ops.log_event(request.user, "counterparty_created", id=cp.id, sim=cp.business_sim, kind=cp.kind)
        messages.success(request, _("%(cp)s registered.") % {"cp": cp})
        return redirect("customer_detail", pk=cp.pk)
    return render(request, "core/simple_form.html", {"form": form, "title": _("Register an agent or POS"),
                                                     "agent_form": True,
                                                     "intro": _("The business SIM number is the unique identifier "
                                                                "and cannot be changed later.")})


@roles_required(R.SUPERADMIN, R.MANAGER, R.JUNIOR, R.FINANCE)
def customer_edit(request, pk):
    cp = get_object_or_404(Counterparty, pk=pk)
    form = F.CounterpartyEditForm(request.POST or None, instance=cp, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        ops.log_event(request.user, "counterparty_edited", id=cp.id, changed=form.changed_data)
        messages.success(request, _("Saved."))
        return redirect("customer_detail", pk=cp.pk)
    return render(request, "core/simple_form.html", {"form": form, "title": str(cp), "agent_form": True})


@roles_required(*STAFF)
def customer_detail(request, pk):
    cp = get_object_or_404(Counterparty, pk=pk)
    return redirect(f"/reports/statement/?preset=all&customer_sim={cp.business_sim}")


# ---------------------------------------------------------------- transactions
@roles_required(*STAFF)
def txn_list(request):
    form = FilterForm(request.user, request.GET or None)
    f = Filters.from_form(form, request.user)
    qs = Transaction.objects.filter(f.txn_q()).select_related(*TX_RELATED).order_by("-posted_at", "-id")
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "core/txn_list.html", {"form": form, "page": page, "filters": f,
                                                  "qs_params": params.urlencode(),
                                                  "customers": Counterparty.objects.filter(active=True)})


def _get_visible_txn(request, pk):
    t = get_object_or_404(Transaction.objects.select_related(*TX_RELATED), pk=pk)
    if not can_view_txn(request.user, t):
        raise Http404
    return t


def _customer_balance_after(t):
    cp = t.counterparty
    if not cp:
        return None
    return Entry.objects.filter(account=cp.account, transaction_id__lte=t.id).aggregate(s=Sum("amount"))["s"] or 0


def receipt_rows(t):
    T = TxType
    rows = [(_("Type"), t.display_type),
            (_("Recorded at (Africa/Douala)"), timezone.localtime(t.posted_at).strftime("%d/%m/%Y %H:%M:%S")),
            (_("Business date"), t.business_date.strftime("%d/%m/%Y"))]
    cashier = t.cashier or (t.cashier_day.cashier if t.cashier_day_id else t.entered_by)
    station = f" ({cashier.subzone})" if getattr(cashier, "subzone_id", None) else ""
    rows.append((_("Cashier"), f"{cashier}{station}"))
    rows.append((_("Cashier ID"), cashier.staff_code or str(cashier.pk)))
    if t.entered_by_id != cashier.id:
        rows.append((_("Entered by"), t.entered_by.label_with_id))
    if t.type in (T.SUPPLY,):
        rows.append((_("Product"), t.get_product_display()))
        rows.append((_("Responsible customer"), f"{t.counterparty.name} — {_('business SIM')} "
                                                f"{display_sim(t.counterparty.business_sim)} "
                                                f"({t.counterparty.get_kind_display()})"))
        if t.counterparty_2_id:
            rows.append((_("Received by POS"), f"{t.counterparty_2.name} — {_('business SIM')} "
                                               f"{display_sim(t.counterparty_2.business_sim)}"))
        rows.append((_("Sent from company account"), str(t.source_account)))
    elif t.type == T.DESTOCKAGE:
        rows.append((_("Cash given to POS"), f"{t.counterparty_2.name} — {_('business SIM')} "
                                             f"{display_sim(t.counterparty_2.business_sim)}"))
        rows.append((_("UVE received by agent (owes)"), f"{t.counterparty.name} — {_('business SIM')} "
                                                        f"{display_sim(t.counterparty.business_sim)}"))
        rows.append((_("Cash drawer"), str(t.source_account)))
    elif t.type == T.COLLECTION:
        rows.append((_("Paid by"), f"{t.counterparty.name} — {_('business SIM')} "
                                   f"{display_sim(t.counterparty.business_sim)} ({t.counterparty.get_kind_display()})"))
        rows.append((_("Payment method"), t.get_payment_method_display()))
        rows.append((_("Received into"), str(t.dest_account)))
        if t.bank_reference:
            rows.append((_("Bank deposit reference"), t.bank_reference))
    else:
        if t.counterparty_id:
            rows.append((_("Customer"), str(t.counterparty)))
        if t.source_account_id:
            rows.append((_("From"), str(t.source_account)))
        if t.dest_account_id:
            rows.append((_("To"), str(t.dest_account)))
    if t.replaces_id:
        rows.append((_("Replaces"), f"{t.replaces.reference} ({t.replaces.receipt_no or ''})"))
    if t.description:
        rows.append((_("Note"), t.description))
    if hasattr(t, "correction"):
        c = t.correction
        rows.append((_("CORRECTED"), f"{c.reason} — {c.corrected_by}, "
                                     f"{timezone.localtime(c.created_at).strftime('%d/%m/%Y %H:%M')}"
                                     + (f" — {_('replacement')} {c.replacement_txn.reference}"
                                        if c.replacement_txn else "")))
    return rows


@roles_required(*STAFF, R.AGENT)
def txn_detail(request, pk):
    t = _get_visible_txn(request, pk)
    entries = t.entries.select_related("account__sim", "account__counterparty").order_by("id") \
        if not request.user.is_agent else None
    transfers = t.responsibility_transfers.select_related("counterparty", "counterparty_2", "entered_by")
    ctx = {"t": t, "entries": entries, "rows": receipt_rows(t), "transfers": transfers,
           "current_responsible": ops.current_responsible(t) if t.type in ops.TRANSFERABLE_TYPES else None,
           "can_correct": request.user.role in SUPERVISORS and t.type not in corr.NOT_CORRECTABLE
           and not hasattr(t, "correction"),
           "can_admin": request.user.is_superadmin,
           "can_edit": request.user.is_superadmin and t.type in corr.BUILDERS and t.type != TxType.REVERSAL
           and not t.replaces_id and not hasattr(t, "correction"),
           "can_transfer": request.user.role in (R.CASHIER, R.MANAGER, R.FINANCE, R.SUPERADMIN)
           and t.type in ops.TRANSFERABLE_TYPES and t.counterparty_id is not None
           and not hasattr(t, "correction") and not ops.active_transfer(t)}
    return render(request, "core/txn_detail.html", ctx)


@roles_required(*STAFF, R.AGENT)
def receipt(request, pk):
    t = _get_visible_txn(request, pk)
    if not t.receipt_no:
        raise Http404
    return render(request, "core/receipt.html", {"t": t, "rows": receipt_rows(t),
                                                 "balance_after": _customer_balance_after(t)})


@roles_required(*STAFF, R.AGENT)
def receipt_pdf_view(request, pk):
    t = _get_visible_txn(request, pk)
    if not t.receipt_no:
        raise Http404
    pdf = receipt_pdf(t, receipt_rows(t), _customer_balance_after(t))
    resp = HttpResponse(pdf, content_type="application/pdf")
    disp = "attachment" if request.GET.get("download") else "inline"
    resp["Content-Disposition"] = f'{disp}; filename="receipt-{t.receipt_no}.pdf"'
    return resp


@roles_required(*SUPERVISORS)
def txn_correct(request, pk):
    t = _get_visible_txn(request, pk)
    form = F.CorrectionForm(t, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form,
            lambda: corr.correct_transaction(request.user, t, kind=form.cleaned_data["kind"],
                                             reason=form.cleaned_data["reason"], new_values=form.new_values(),
                                             idempotency_key=form.cleaned_data["idem"]),
            success=lambda c: _("%(ref)s marked CORRECTED.") % {"ref": t.reference},
            on_success=lambda c: redirect("txn_detail", pk=(c.replacement_txn or c.original).pk
                                          if isinstance(c, Correction) else c.pk))
        if resp:
            return resp
    return render(request, "core/correct.html", {"t": t, "form": form, "rows": receipt_rows(t),
                                                 "customers": Counterparty.objects.filter(active=True)})


@roles_required(R.CASHIER, R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
def txn_transfer(request, pk):
    t = _get_visible_txn(request, pk)
    data = request.POST or None
    form = F.ResponsibilityTransferForm(data, initial={"idem": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form,
            lambda: ops.transfer_responsibility(request.user, supply=t, new_party=form.cleaned_data["new_party"],
                                                reason=form.cleaned_data["reason"],
                                                idempotency_key=form.cleaned_data["idem"]),
            success=lambda x: _("Responsibility for %(ref)s transferred to %(cp)s (%(amt)s FCFA).") % {
                "ref": t.reference, "cp": x.counterparty, "amt": fmt(x.amount)},
            on_success=lambda x: redirect("txn_detail", pk=t.pk))
        if resp:
            return resp
    cur = ops.current_responsible(t) if t.type in ops.TRANSFERABLE_TYPES else None
    return render(request, "core/transfer.html", {
        "t": t, "form": form, "current": cur, "current_balance": balance(cur.account) if cur else None,
        "customers": Counterparty.objects.filter(active=True)})


@roles_required(*STAFF)
def customer_search(request):
    """Live search for the customer pickers: business SIM digits or name."""
    from django.http import JsonResponse

    q = (request.GET.get("q") or "").strip()
    kind = request.GET.get("kind") or ""
    qs = Counterparty.objects.filter(active=True)
    if kind in Counterparty.Kind.values:
        qs = qs.filter(kind=kind)
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        cond = Q(name__icontains=q)
        if digits:
            cond |= Q(business_sim__contains=digits[-9:] if len(digits) > 9 else digits)
        qs = qs.filter(cond)
    qs = list(qs.order_by("kind", "name")[:15])
    bals = dict(Entry.objects.filter(account__counterparty__in=qs).values_list("account__counterparty")
                .annotate(s=Sum("amount")))
    out = []
    for c in qs:
        bal = bals.get(c.id) or 0
        out.append({"sim": c.business_sim, "sim_display": display_sim(c.business_sim), "name": c.name,
                    "kind": c.kind, "kind_label": str(c.get_kind_display()), "balance": int(bal),
                    "balance_display": fmt(abs(bal)),
                    "balance_label": str(_("owes") if bal > 0 else _("supply due") if bal < 0 else _("settled"))})
    return JsonResponse({"results": out})
