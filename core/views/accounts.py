"""Users & passwords (super administrator) and "My password" (everyone)."""
from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from core.models import User
from core.services import accounts as acc
from core.services.errors import BusinessError

from .base import R, roles_required


class ResetForm(forms.Form):
    mode = forms.ChoiceField(label=_l("New password"), widget=forms.RadioSelect, initial="generate", choices=[
        ("generate", _l("Generate a strong password for me")), ("set", _l("I will type the new password"))])
    new_password = forms.CharField(label=_l("Password to set"), required=False,
                                   widget=forms.PasswordInput(render_value=True, attrs={"autocomplete": "new-password"}),
                                   help_text=_l("At least 10 characters, not only digits, not a common password."))
    must_change = forms.BooleanField(label=_l("Ask the user to choose their own password at next sign-in"),
                                     required=False, initial=True)

    def clean(self):
        cd = super().clean()
        if cd.get("mode") == "set" and not cd.get("new_password"):
            self.add_error("new_password", _("Type the new password."))
        return cd


@roles_required(R.SUPERADMIN)
def users(request):
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or ""
    qs = User.objects.select_related("subzone__parent", "counterparty").order_by("-is_active", "role", "username")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                       | Q(staff_code__icontains=q))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/users.html", {"page": page, "q": q, "status": status})


@roles_required(R.SUPERADMIN)
def user_detail(request, pk):
    u = get_object_or_404(User.objects.select_related("subzone__parent", "counterparty"), pk=pk)
    form = ResetForm(request.POST or None)
    new_pw = None
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            new_pw = acc.reset_password(request.user, u, new_password=cd["new_password"] if cd["mode"] == "set"
                                        else None, must_change=cd["must_change"])
        except BusinessError as e:
            form.add_error("new_password" if cd["mode"] == "set" else None, e.message)
        else:
            if u.pk == request.user.pk:
                update_session_auth_hash(request, u)
            messages.success(request, _("Password of %(u)s replaced.") % {"u": u.username})
            form = ResetForm()
    return render(request, "core/user_detail.html", {"u": u, "form": form, "new_pw": new_pw})


@require_POST
@roles_required(R.SUPERADMIN)
def user_toggle(request, pk):
    u = get_object_or_404(User, pk=pk)
    try:
        acc.set_active(request.user, u, not u.is_active)
    except BusinessError as e:
        messages.error(request, e.message)
    else:
        messages.success(request, (_("%(u)s can sign in again.") if u.is_active else
                                   _("%(u)s is deactivated and can no longer sign in.")) % {"u": u.username})
    nxt = request.POST.get("next") or ""
    return redirect(nxt) if nxt.startswith("/") and not nxt.startswith("//") else redirect("user_detail", pk=pk)


@roles_required(R.SUPERADMIN)
def agent_logins(request):
    """Logins for many commercial agents at once: tick the agents (or take the whole filtered list); each gets the
    business SIM as username and a temporary password (generated, or the same one typed for all), to be changed at
    the first sign-in. The new usernames and passwords are shown once and can be downloaded."""
    from core.models import Counterparty, Zone

    q = (request.GET.get("q") or "").strip()
    has = request.GET.get("has") or "without"
    zone = request.GET.get("zone") or ""
    qs = Counterparty.objects.filter(kind=Counterparty.Kind.AGENT, active=True).select_related(
        "microzone__subzone", "login_user").order_by("name")
    if has == "with":
        qs = qs.filter(login_user__isnull=False)
    else:
        qs = qs.filter(login_user__isnull=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(business_sim__icontains=q.replace(" ", ""))
                       | Q(microzone__name__icontains=q))
    if zone.isdigit():
        qs = qs.filter(Q(microzone__subzone_id=zone) | Q(microzone__subzone__parent_id=zone) | Q(master_zone_id=zone))
    if request.method == "POST" and has != "with":
        pw = (request.POST.get("password") or "").strip() or None
        if request.POST.get("scope") == "all":
            agents = list(qs[:1000])
        else:
            ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
            agents = list(qs.filter(pk__in=ids))
        try:
            done = acc.create_agent_logins(request.user, agents, password=pw,
                                           must_change=bool(request.POST.get("must_change")))
        except BusinessError as e:
            messages.error(request, e.message)
        else:
            request.session["agent_logins_new"] = [[cp.name, cp.business_sim, u.username, p] for cp, u, p in done]
            messages.success(request, _("Logins created: %(n)s. Copy or download the passwords now: they are shown "
                                        "only on this page.") % {"n": len(done)})
            return redirect(request.get_full_path())
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "core/agent_logins.html", {
        "page": page, "q": q, "has": has, "zone": zone, "total": page.paginator.count,
        "zones": Zone.objects.filter(active=True).order_by("parent_id", "name"),
        "new": request.session.get("agent_logins_new") or [],
        "with_count": Counterparty.objects.filter(kind=Counterparty.Kind.AGENT, login_user__isnull=False).count()})


@roles_required(R.SUPERADMIN)
def agent_logins_download(request):
    """The logins just created (usernames and temporary passwords) as an Excel file; ?clear=1 forgets them."""
    import io

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font

    rows = request.session.get("agent_logins_new") or []
    if request.method == "POST" or request.GET.get("clear"):
        request.session.pop("agent_logins_new", None)
        return redirect("agent_logins")
    wb = Workbook()
    ws = wb.active
    ws.title = "logins"
    ws.append([_("Agent"), _("Business SIM"), _("Username"), _("Temporary password")])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for col, w in zip("ABCD", (32, 16, 18, 20)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="agent_logins.xlsx"'
    return resp


@login_required
def my_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    for f in form.fields.values():
        f.widget.attrs.setdefault("autocomplete", "new-password")
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # stay signed in
        acc.password_changed_by_user(user)
        messages.success(request, _("Your password has been changed."))
        return redirect("dashboard")
    return render(request, "core/my_password.html", {"form": form, "forced": request.user.must_change_password})
