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


STAFF = (R.SUPERADMIN, R.MANAGER, R.JUNIOR, R.FINANCE, R.CASHIER)


def acting_cashier(request):
    """The cashier a super administrator has chosen to record for (session)."""
    if not request.user.is_authenticated or not request.user.is_superadmin:
        return None
    cid = request.session.get("acting_cashier")
    if not cid:
        return None
    return User.objects.filter(pk=cid, role=R.CASHIER, is_active=True).select_related("subzone").first()


@login_required
def act_as(request):
    if not request.user.is_superadmin:
        raise DjangoPermissionDenied(_("You are not allowed to perform this action."))
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
        nxt = ""
    if request.method == "POST":
        cid = request.POST.get("cashier")
        if cid == "":
            request.session.pop("acting_cashier", None)
        elif User.objects.filter(pk=cid, role=R.CASHIER, is_active=True).exists():
            request.session["acting_cashier"] = int(cid)
            from core.services.operations import log_event

            log_event(request.user, "act_as_cashier", cashier_id=int(cid))
        return HttpResponseRedirect(nxt or reverse("dashboard"))
    cashiers = User.objects.filter(role=R.CASHIER, is_active=True).select_related("subzone__parent").order_by(
        "subzone__parent__name", "subzone__name", "username")
    return render(request, "core/act_as.html", {"cashiers": cashiers, "next": nxt,
                                                "current": acting_cashier(request)})
SUPERVISORS = (R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)


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
    if user.is_shop_user:  # shops see only their own shop's entries
        return bool(t.shop_id) and (user.is_shop_senior or t.shop_id == user.shop_id)
    if t.shop_id and t.type not in ("SHOP_TO_DESK", "DESK_TO_SHOP", "CAPITAL_IN", "CAPITAL_OUT", "CAPITAL_MOVE"):
        return False  # shop dealings are not visible to cashiers, junior managers or agents
    if user.is_agent:
        return Entry.objects.filter(transaction=t, account__counterparty=user.counterparty).exists()
    if user.is_junior:
        ids = user.zone_ids_in_scope()
        if t.zone_id in ids:
            return True
        return Entry.objects.filter(transaction=t, account__zone_id__in=ids).exists()
    if user.is_cashier:
        if t.entered_by_id == user.id or t.cashier_id == user.id or (t.zone_id and t.zone_id == user.subzone_id):
            return True
        if t.counterparty_id or t.counterparty_2_id:
            return True  # customer accounts are shared across cashiers
        held = Account.objects.filter(perm.zone_accounts_q(user)).values("id")
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
                cookie_lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
                if not user.language and cookie_lang in dict(settings.LANGUAGES):
                    User.objects.filter(pk=user.pk).update(language=cookie_lang)
                from core.services.operations import log_event

                log_event(user, "login")
                nxt = request.POST.get("next") or ""
                if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
                    nxt = ""
                return HttpResponseRedirect(nxt or reverse("dashboard"))
    idle = request.GET.get("idle") == "1"
    return render(request, "core/login.html", {"error": error, "next": request.GET.get("next", ""), "idle": idle,
                                               "idle_minutes": settings.SESSION_IDLE_MINUTES})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


# ---------------------------------------------------------------- dashboard
KIND_ORDER = [Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.ER, Account.Kind.MTN]


def asset_balances(accounts):
    ids = [a.id for a in accounts]
    rows = Entry.objects.filter(account_id__in=ids).values("account_id").annotate(s=Sum("amount"))
    b = {r["account_id"]: r["s"] for r in rows}
    return {i: b.get(i) or 0 for i in ids}


def kind_totals(accounts, bals):
    tot = {k: 0 for k in KIND_ORDER}
    tot[Account.Kind.BANK] = 0
    for a in accounts:
        tot[a.kind] = tot.get(a.kind, 0) + bals[a.id]
    return [(k, Account.Kind(k).label, tot[k]) for k in KIND_ORDER]


def zone_matrix(zone=None):
    """Cash/UVE/ER/MTN balances by zone: master zone -> its subzones, plus the
    master-level accounts (master SIM, with its cashier in charge).
    `zone` may be a master zone or a subzone (limits the table)."""
    from core.models import Zone

    accts = list(Account.objects.filter(kind__in=KIND_ORDER, active=True, zone__isnull=False)
                 .select_related("zone__parent", "custodian", "sim"))
    if zone is not None:
        if zone.is_master:
            accts = [a for a in accts if a.zone.master.id == zone.id]
        else:
            accts = [a for a in accts if a.zone_id == zone.id or (a.custodian and a.custodian.subzone_id == zone.id)]
    bals = asset_balances(accts)
    masters = {}
    for a in accts:
        m = a.zone.master
        me = masters.setdefault(m.id, {"zone": m, "rows": {}, "totals": {k: 0 for k in KIND_ORDER}})
        if a.zone.is_master and (a.custodian_id or not m.subzones.exists()):
            key, label, sub = ("m", a.custodian_id), None, None
        else:
            key, label, sub = ("s", a.zone_id), a.zone.name, a.zone
        r = me["rows"].setdefault(key, {"label": label, "subzone": sub, "master_level": key[0] == "m",
                                        "incharge": a.custodian, "totals": {k: 0 for k in KIND_ORDER}})
        r["totals"][a.kind] += bals[a.id]
        me["totals"][a.kind] += bals[a.id]
    out = []
    for m in sorted(masters.values(), key=lambda x: x["zone"].name):
        rows = sorted(m["rows"].values(), key=lambda r: (r["master_level"], r["label"] or ""))
        out.append({"zone": m["zone"], "totals": [m["totals"][k] for k in KIND_ORDER],
                    "rows": [dict(r, totals=[r["totals"][k] for k in KIND_ORDER]) for r in rows]})
    grand = [sum(z["totals"][i] for z in out) for i in range(len(KIND_ORDER))]
    return out, grand, Zone.objects.filter(parent__isnull=True)


@login_required
def dashboard(request):
    user = request.user
    ctx = {"today": douala_today()}
    if user.is_shop_user:
        return redirect("shops")
    if user.is_agent:
        cp = user.counterparty
        ctx["cp"] = cp
        ctx["balance"] = balance(cp.account)
        ctx["recent"] = Transaction.objects.filter(entries__account=cp.account).select_related(
            "entered_by", "counterparty", "counterparty_2").order_by("-posted_at")[:15]
        return render(request, "core/dashboard_agent.html", ctx)
    if user.is_cashier:
        from core.services.closing import zone_closing_accounts

        accts = list(perm.usable_accounts(user, Account.ASSET_KINDS).order_by("kind", "id"))
        zone_accts = zone_closing_accounts(user.subzone)
        bals = asset_balances(list({a.id: a for a in accts + zone_accts}.values()))
        ctx["accounts"] = [(a, bals[a.id], not a.custodian_id or a.custodian_id == user.id) for a in accts]
        ctx["kind_totals"] = kind_totals(zone_accts, bals)
        ctx["zone"] = user.subzone
        ctx["day"] = peek_open_day(user)
        ctx["recent"] = Transaction.objects.filter(Q(zone=user.subzone) | Q(entered_by=user)).select_related(
            "cashier", "entered_by", "counterparty", "counterparty_2", "source_account__sim",
            "dest_account__sim").order_by("-posted_at")[:15]
        ctx["open_disc"] = Discrepancy.objects.filter(
            status="OPEN", account__in=[a.id for a in zone_accts]).count()
        return render(request, "core/dashboard_cashier.html", ctx)
    # supervisors / admin
    from core.models import Zone

    zone = Zone.objects.filter(pk=request.GET.get("zone"), parent__isnull=True).first() \
        if (request.GET.get("zone") or "").isdigit() else None
    if user.is_junior:
        zone = user.scope_zone  # junior managers see their zone only
    ctx["matrix"], grand, ctx["zones"] = zone_matrix(zone)
    ctx["zone"] = zone
    ctx["junior"] = user.is_junior
    ctx["grand"] = [(k, Account.Kind(k).label, v) for k, v in zip(KIND_ORDER, grand)]
    ctx["kind_labels"] = [Account.Kind(k).label for k in KIND_ORDER]
    banks = list(Account.objects.filter(kind=Account.Kind.BANK, active=True).select_related("bank"))
    bb = asset_balances(banks)
    ctx["banks"] = [(a, bb[a.id]) for a in banks]
    cust = Entry.objects.filter(account__kind=Account.Kind.CUSTOMER).values("account_id").annotate(s=Sum("amount"))
    ctx["owed"] = sum((r["s"] for r in cust if r["s"] > 0), 0)
    ctx["due"] = -sum((r["s"] for r in cust if r["s"] < 0), 0)
    from core.models import CashierDay

    disc = Discrepancy.objects.filter(status="OPEN").select_related(
        "account__sim", "closing_line__closing__cashier_day__zone")
    recent = Transaction.objects.select_related("cashier", "entered_by", "counterparty", "zone")
    days = CashierDay.objects.filter(status="OPEN", zone__isnull=False).select_related("zone__parent")
    ids = user.zone_ids_in_scope()
    if ids is not None:
        disc = disc.filter(closing_line__closing__cashier_day__zone_id__in=ids)
        recent = recent.filter(zone_id__in=ids)
        days = days.filter(zone_id__in=ids)
    ctx["open_disc"] = disc[:20]
    ctx["recent"] = recent.order_by("-posted_at")[:15]
    ctx["days"] = days.order_by("zone__parent__name", "zone__name")
    return render(request, "core/dashboard_staff.html", ctx)


@require_POST
def set_lang(request):
    """Switch language; remembered on the user's profile so it follows them
    on every device, and in a cookie for the sign-in page."""
    from django.conf import settings as dj
    from django.utils import translation

    lang = request.POST.get("language")
    nxt = request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
        nxt = "/"
    resp = HttpResponseRedirect(nxt)
    if lang in dict(dj.LANGUAGES):
        translation.activate(lang)
        resp.set_cookie(dj.LANGUAGE_COOKIE_NAME, lang, max_age=365 * 24 * 3600, samesite="Lax")
        if request.user.is_authenticated and request.user.language != lang:
            User.objects.filter(pk=request.user.pk).update(language=lang)
    return resp


def keepalive(request):
    """Called by the page while the user is active; refreshes the session
    (SESSION_SAVE_EVERY_REQUEST) so typing in a long form never logs out."""
    from django.http import JsonResponse

    if not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=401)
    request.session["last_seen"] = __import__("time").time()
    return JsonResponse({"ok": True})


def idle_logout(request):
    """Sign out after inactivity, then show the sign-in page with a note."""
    if request.method == "POST" and request.user.is_authenticated:
        from core.services.operations import log_event

        log_event(request.user, "auto_logout_idle")
        logout(request)
    return redirect(reverse("login") + "?idle=1")
