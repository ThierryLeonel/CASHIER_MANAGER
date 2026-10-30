from django.conf import settings


def app_context(request):
    from core.services.branding import company

    co = company()
    ctx = {"company": co, "COMPANY_NAME": co.display_name, "company_address": co.address_line,
           "IDLE_SECONDS": settings.SESSION_IDLE_MINUTES * 60}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_cashier:
        from core.services.days import peek_open_day

        from core.services.days import douala_today

        ctx["open_day"] = peek_open_day(user)
        ctx["today_douala"] = douala_today()
    if user is not None and user.is_authenticated:
        ctx.update(_space(request, user))
    if user is not None and user.is_authenticated and user.is_superadmin:
        from core.views.base import acting_cashier

        ctx["acting_cashier"] = acting_cashier(request)
    return ctx


NEUTRAL_PREFIXES = ("/capital/", "/transactions/", "/account/", "/administration/", "/setup/", "/act-as/", "/admin/")


def _space(request, user):
    """Finance, the senior manager and the super admin work in two separate spaces: the cashiers (zones,
    agents, closings, reports) and the shops. The sidebar shows one space at a time; /shops/ pages are the
    shops space, the other pages the cashiers' space, shared pages keep the last space used."""
    if user.is_shop_user:
        return {"space": "shops", "show_cash": False, "show_shops": True, "can_switch": False}
    if not user.sees_all_shops:
        return {"space": "cash", "show_cash": True, "show_shops": False, "can_switch": False}
    path = request.path
    if path.startswith("/shops/"):
        space = "shops"
    elif path.startswith(NEUTRAL_PREFIXES):
        space = request.session.get("space", "cash")
    else:
        space = "cash"
    if request.session.get("space") != space:
        request.session["space"] = space
    return {"space": space, "show_cash": space == "cash", "show_shops": space == "shops", "can_switch": True}
