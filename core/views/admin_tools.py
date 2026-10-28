"""Super-administrator tools: hub, delete/edit rows, clear data, import, audit log."""
import base64

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms as F
from core.models import AuditEvent, Counterparty, Transaction, User
from core.services import admin as adm
from core.services import corrections as corr
from core.services import importer
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
    if t.type not in corr.BUILDERS:
        raise Http404
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
                                                    "customers": Counterparty.objects.filter(active=True)})


@roles_required(*ADMIN)
def clear_data(request):
    form = F.ClearDataForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            counts = adm.clear_data(request.user, scope=cd["scope"], password=cd["password"],
                                    confirm_text=cd["confirm_text"])
            messages.success(request, _("Data cleared (%(n)s transactions removed). This action is recorded in the "
                                        "audit log.") % {"n": counts.get("transactions", 0)})
            return redirect("admin_home")
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
    return render(request, "core/admin_clear.html", {"form": form})


SESSION_KEY = "import_upload"


@roles_required(*ADMIN)
def import_data(request):
    result = None
    form = F.ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and request.POST.get("action") == "confirm":
        up = request.session.get(SESSION_KEY)
        if not up:
            messages.error(request, _("The uploaded file has expired. Upload it again."))
            return redirect("import_data")
        rows = importer.read_rows(up["name"], base64.b64decode(up["content"]))
        results, ok = importer.run_import(request.user, up["kind"], rows, commit=True)
        if ok:
            request.session.pop(SESSION_KEY, None)
            messages.success(request, _("%(n)s rows imported.") % {"n": len(results)})
            return redirect("import_data")
        result = {"results": results, "ok": ok, "kind": up["kind"], "name": up["name"], "committed": False}
    elif request.method == "POST" and form.is_valid():
        f = form.cleaned_data["file"]
        if f.size > 3 * 1024 * 1024:
            form.add_error("file", _("The file is too large (maximum 3 MB)."))
        else:
            content = f.read()
            try:
                rows = importer.read_rows(f.name, content)
                results, ok = importer.run_import(request.user, form.cleaned_data["kind"], rows, commit=False)
                request.session[SESSION_KEY] = {"name": f.name, "kind": form.cleaned_data["kind"],
                                                "content": base64.b64encode(content).decode()}
                result = {"results": results, "ok": ok, "kind": form.cleaned_data["kind"], "name": f.name}
            except BusinessError as e:
                form.add_error("file", e.message)
    return render(request, "core/admin_import.html", {"form": form, "result": result,
                                                      "kinds": F.ImportForm.KINDS,
                                                      "templates": importer.TEMPLATES})


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
