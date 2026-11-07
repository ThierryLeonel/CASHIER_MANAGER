"""Super-administrator tools: hub, delete/edit rows, clear data, import, audit log."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms as F
from core.models import AuditEvent, Counterparty, ImportBatch, Transaction, User
from core.services import admin as adm
from core.services import corrections as corr
from core.services import importer
from core.services import operations as ops
from core.services.errors import BusinessError

from .base import R, acting_cashier, roles_required
from .records import TX_RELATED, receipt_rows

ADMIN = (R.SUPERADMIN,)


@roles_required(*ADMIN)
def admin_home(request):
    stats = {"users": User.objects.filter(is_active=True).count(), "customers": Counterparty.objects.count(),
             "transactions": Transaction.objects.count(), "audit": AuditEvent.objects.count()}
    return render(request, "core/admin_home.html", {"stats": stats, "acting": acting_cashier(request)})


@roles_required(*ADMIN)
def txn_delete(request, pk):
    t = get_object_or_404(Transaction.objects.select_related(*TX_RELATED), pk=pk)
    form = F.AdminDeleteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            snaps = adm.delete_transaction(request.user, t, reason=form.cleaned_data["reason"])
            messages.success(request, _("Deleted: %(refs)s. A full copy is kept in the audit log.")
                             % {"refs": ", ".join(s["reference"] for s in snaps)})
            return redirect("txn_list")
        except BusinessError as e:
            form.add_error(None, e.message)
    return render(request, "core/admin_delete.html", {"t": t, "form": form, "rows": receipt_rows(t)})


@roles_required(*ADMIN)
def txn_edit(request, pk):
    t = get_object_or_404(Transaction.objects.select_related(*TX_RELATED), pk=pk)
    if t.type == "REVERSAL":
        raise Http404
    if t.type not in corr.BUILDERS:  # any other entry, whoever recorded it: amount, date, texts
        form = F.AdminSimpleEditForm(t, request.POST or None, amount_editable=adm.simple_editable_amount(t))
        if request.method == "POST" and form.is_valid():
            cd = form.cleaned_data
            try:
                adm.edit_transaction_simple(request.user, t, amount=cd.get("amount"), description=cd["description"],
                                            business_date=cd.get("business_date"),
                                            client_phone=cd.get("client_phone"), account_name=cd.get("account_name"),
                                            reason=cd["reason"])
                messages.success(request, _("%(ref)s updated. The previous values are kept in the audit log.")
                                 % {"ref": t.reference})
                return redirect("txn_detail", pk=t.pk)
            except BusinessError as e:
                form.add_error(e.field if e.field in form.fields else None, e.message)
        return render(request, "core/admin_edit.html", {"t": t, "form": form, "rows": receipt_rows(t),
                                                        "customers": []})
    form = F.AdminEditForm(t, request.POST or None, initial={"business_date": t.business_date})
    if request.method == "POST" and form.is_valid():
        try:
            adm.edit_transaction(request.user, t, new_values=form.new_values(), reason=form.cleaned_data["reason"])
            messages.success(request, _("%(ref)s updated. The previous values are kept in the audit log.")
                             % {"ref": t.reference})
            return redirect("txn_detail", pk=t.pk)
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
    return render(request, "core/admin_edit.html", {"t": t, "form": form, "rows": receipt_rows(t),
                                                    "customers": True})


@roles_required(*ADMIN)
def clear_data(request):
    form = F.ClearDataForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            counts = adm.clear_data(request.user, scope=cd["scope"], password=cd["password"],
                                    confirm_text=cd["confirm_text"], agent=cd.get("agent"),
                                    target_user=cd.get("target_user"))
            messages.success(request, _("Data cleared (%(n)s transactions and %(a)s agent records removed). This "
                                        "action is recorded in the audit log.")
                             % {"n": counts.get("transactions", 0), "a": counts.get("agent_records", 0)})
            return redirect("admin_home")
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
    return render(request, "core/admin_clear.html", {"form": form})


@roles_required(*ADMIN)
def import_data(request):
    form = F.ImportForm(request.POST or None, request.FILES or None,
                        initial={"kind": request.GET.get("kind")} if request.GET.get("kind") else None)
    if request.method == "POST" and form.is_valid():
        f = form.cleaned_data["file"]
        if f.size > 15 * 1024 * 1024:
            form.add_error("file", _("The file is too large (maximum 15 MB)."))
        else:
            try:
                rows = importer.read_rows(f.name, f.read())
                b = importer.start_batch(request.user, form.cleaned_data["kind"], f.name, rows)
                return redirect("import_batch", b.pk)
            except BusinessError as e:
                form.add_error("file", e.message)
    recent = list(ImportBatch.objects.filter(created_by=request.user).defer("rows")[:8])
    labels = dict(F.ImportForm.KINDS)
    for r in recent:
        r.kind_label = labels.get(r.kind, r.kind)
    return render(request, "core/admin_import.html", {"form": form, "kinds": F.ImportForm.KINDS, "recent": recent,
                                                      "templates": importer.TEMPLATES})


def _batch_state(b):
    return {"stage": b.stage, "stage_label": str(b.get_stage_display()), "position": b.position, "total": b.total,
            "imported": b.imported, "errors": len(b.errors),
            "running": b.stage in (ImportBatch.Stage.CHECK, ImportBatch.Stage.IMPORT)}


@roles_required(*ADMIN)
def import_batch(request, pk):
    b = get_object_or_404(ImportBatch.objects.defer("rows"), pk=pk, created_by=request.user)
    label = dict(F.ImportForm.KINDS).get(b.kind, b.kind)
    return render(request, "core/admin_import_batch.html", {"b": b, "label": label, "state": _batch_state(b)})


@roles_required(*ADMIN)
def import_history_clear(request):
    """Remove import records from the list (one, or all of mine). What was imported stays imported."""
    if request.method == "POST":
        qs = ImportBatch.objects.filter(created_by=request.user)
        pk = request.POST.get("pk")
        if pk and pk.isdigit():
            qs = qs.filter(pk=pk)
        n = qs.count()
        qs.delete()
        messages.success(request, _("%(n)s import record(s) removed from the list. The data already imported is "
                                    "not changed.") % {"n": n})
    return redirect("import_data")


@roles_required(*ADMIN)
def import_step(request, pk):
    if request.method != "POST":
        return redirect("import_batch", pk)
    b = get_object_or_404(ImportBatch, pk=pk, created_by=request.user)
    action = request.POST.get("action")
    try:
        if action in ("confirm", "confirm_skip"):
            importer.confirm_batch(request.user, b, skip_errors=action == "confirm_skip")
        elif b.stage in (ImportBatch.Stage.CHECK, ImportBatch.Stage.IMPORT):
            importer.step_batch(request.user, b)
    except BusinessError as e:
        if request.headers.get("X-Requested-With") == "fetch":
            return JsonResponse({"ok": False, "error": e.message}, status=400)
        messages.error(request, e.message)
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, **_batch_state(b)})
    return redirect("import_batch", pk)


@roles_required(*ADMIN)
def import_template(request, kind):
    if kind not in importer.TEMPLATES:
        raise Http404
    resp = HttpResponse(importer.template_xlsx(kind),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="template-{kind}.xlsx"'
    return resp


@roles_required(*ADMIN)
def audit_log(request):
    qs = AuditEvent.objects.select_related("user")
    action = request.GET.get("action") or ""
    if action:
        qs = qs.filter(action=action)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    actions = AuditEvent.objects.values_list("action", flat=True).distinct().order_by("action")
    return render(request, "core/admin_audit.html", {"page": page, "actions": actions, "action": action})


@roles_required(*ADMIN)
def company_profile(request):
    from core.services import branding

    prof = branding.company()
    form = F.CompanyProfileForm(request.POST or None, request.FILES or None, instance=prof)
    if request.method == "POST" and form.is_valid():
        prof = form.save(commit=False)
        logo = form.cleaned_data.get("logo_file")
        fields = list(form.Meta.fields) + ["updated_at"]
        if logo:
            branding.save_logo(prof, logo)
            fields += ["logo", "logo_updated"]
        elif form.cleaned_data.get("remove_logo"):
            branding.save_logo(prof, None)
            fields += ["logo", "logo_updated"]
        prof.save(update_fields=fields)
        ops.log_event(request.user, "company_profile_changed", changed=form.changed_data
                      + (["logo"] if logo or form.cleaned_data.get("remove_logo") else []))
        messages.success(request, _("Company profile saved."))
        return redirect("company_profile")
    return render(request, "core/company_profile.html", {"form": form, "prof": prof})


def company_logo(request):
    """The logo image. Public (shown on the login page); cached by the browser, the URL changes with each upload."""
    from core.services import branding

    data = branding.logo_bytes()
    if not data:
        raise Http404
    resp = HttpResponse(data, content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable" if request.GET.get("v") else "no-cache"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def favicon(request):
    """Browser icon: the company logo, or the first letter of the company name until a logo is uploaded."""
    from django.utils.html import escape

    from core.services import branding

    data = branding.logo_bytes()
    if data:
        resp = HttpResponse(data, content_type="image/png")
    else:
        letter = escape(branding.company().initial)
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
               'fill="#ef7d12"/><text x="32" y="44" font-family="Arial,Helvetica,sans-serif" font-size="36" '
               f'font-weight="700" fill="#fff" text-anchor="middle">{letter}</text></svg>')
        resp = HttpResponse(svg, content_type="image/svg+xml")
    resp["Cache-Control"] = "public, max-age=31536000, immutable" if request.GET.get("v") else "public, max-age=300"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
