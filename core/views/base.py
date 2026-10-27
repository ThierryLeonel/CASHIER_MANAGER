from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.models import Account, Discrepancy, Entry, Transaction, User
from core.services import permissions as perm
from core.services.days import douala_today, peek_open_day
from core.services.errors import BusinessError, DuplicateSubmission
from core.services.ledger import balance

R = User.Role


def roles_required(*roles):
    def deco(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.user.role not in roles:
                raise DjangoPermissionDenied(_("You are not allowed to perform this action."))
            return view(request, *args, **kwargs)

        return wrapper

    return deco


STAFF = (R.SUPERADMIN, R.MANAGER, R.FINANCE, R.CASHIER)
SUPERVISORS = (R.MANAGER, R.FINANCE)


def run_service(request, form, fn, *, success, on_success):
    """Call a service, map BusinessError to form errors, handle duplicates."""
    try:
        obj = fn()
    except DuplicateSubmission as exc:
        messages.info(request, exc.message)
        return on_success(exc.existing)
    except BusinessError as exc:
        if exc.field and exc.field in form.fields:
            form.add_error(exc.field, exc.message)
        else:
            form.add_error(None, exc.message)
        return None
    messages.success(request, success(obj))
    return on_success(obj)


def can_view_txn(user, t):
    if user.sees_all_zones:
        return True
    if user.is_agent:
        return Entry.objects.filter(transaction=t, account__counterparty=user.counterparty).exists()
    if user.is_cashier:
        if t.entered_by_id == user.id or (t.cashier_day_id and t.cashier_day.cashier_id == user.id):
            return True
        if t.counterparty_id or t.counterparty_2_id:
            return True  # customer accounts are shared across cashiers
        held = Account.objects.filter(Q(custodian=user) | Q(grants__user=user)).values("id")
        return Entry.objects.filter(transaction=t, account_id__in=held).exists()
    return False


# ---------------------------------------------------------------- auth
def _lock_key(request, username):
    return f"login-fail:{(username or '').lower()}:{request.META.get('REMOTE_ADDR', '')}"


def login_view(request):
    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        key = _lock_key(request, username)
        fails = cache.get(key, 0)
        if fails >= settings.LOGIN_MAX_FAILURES:
            error = _("Too many failed attempts. Try again in 15 minutes.")
        else:
            user = authenticate(request, username=username, password=request.POST.get("password", ""))
            if user is None:
                cache.set(key, fails + 1, settings.LOGIN_LOCKOUT_SECONDS)
                error = _("Invalid username or password.")
            else:
                cache.delete(key)
                login(request, user)
                from core.services.operations import log_event

                log_event(user, "login")
                nxt = request.POST.get("next") or ""
                if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
                    nxt = ""
                return HttpResponseRedirect(nxt or reverse("dashboard"))
    return render(request, "core/login.html", {"error": error, "next": request.GET.get("next", "")})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


# ---------------------------------------------------------------- dashboard
@login_required
def dashboard(request):
    user = request.user
    ctx = {"today": douala_today()}
    if user.is_agent:
        cp = user.counterparty
        ctx["cp"] = cp
        ctx["balance"] = balance(cp.account)
        ctx["recent"] = Transaction.objects.filter(entries__account=cp.account).select_related(
            "entered_by", "counterparty", "counterparty_2").order_by("-posted_at")[:15]
        return render(request, "core/dashboard_agent.html", ctx)
    if user.is_cashier:
        accts = perm.usable_accounts(user, Account.ASSET_KINDS).order_by("kind", "id")
        ctx["accounts"] = [(a, balance(a), a.custodian_id == user.id) for a in accts]
        ctx["day"] = peek_open_day(user)
        ctx["recent"] = Transaction.objects.filter(entered_by=user).select_related(
            "counterparty", "counterparty_2", "source_account__sim", "dest_account__sim").order_by("-posted_at")[:15]
        ctx["open_disc"] = Discrepancy.objects.filter(status="OPEN", account__custodian=user).count()
        return render(request, "core/dashboard_cashier.html", ctx)
    # supervisors / admin
    accts = Account.objects.filter(kind__in=Account.ASSET_KINDS, active=True).select_related("sim", "custodian")
    ctx["accounts"] = [(a, balance(a)) for a in accts.order_by("kind", "id")]
    cust = Entry.objects.filter(account__kind=Account.Kind.CUSTOMER).values("account_id").annotate(s=Sum("amount"))
    ctx["owed"] = sum((r["s"] for r in cust if r["s"] > 0), 0)
    ctx["due"] = -sum((r["s"] for r in cust if r["s"] < 0), 0)
    ctx["open_disc"] = Discrepancy.objects.filter(status="OPEN").select_related(
        "account__sim", "closing_line__closing__cashier_day__cashier")[:20]
    ctx["recent"] = Transaction.objects.select_related("entered_by", "counterparty").order_by("-posted_at")[:15]
    from core.models import CashierDay

    ctx["days"] = CashierDay.objects.filter(status="OPEN").select_related("cashier__subzone")
    return render(request, "core/dashboard_staff.html", ctx)
