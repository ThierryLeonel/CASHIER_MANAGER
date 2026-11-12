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


NEUTRAL_PREFIXES = ("/capital/", "/transactions/", "/account/", "/act-as/")
# super administrator: the administration lives in its own "Admin" space
# commercial agents supervision space (senior manager, finance, super admin, market supervisor)
AGENT_PREFIXES = ("/supervision/agents/", "/customers/", "/stock/", "/agent/oos/", "/agent/destocash/")
ADMIN_PREFIXES = ("/administration/", "/setup/", "/capital/", "/shops/manage/", "/shops/setup/",
                  "/shops/opening-balances/")


def _space(request, user):
    """Finance, the senior manager and the super admin work in separate spaces: the cashiers (zones, agents,
    closings, reports), the shops and — super admin only — the administration. The sidebar shows one space at a
    time; shared pages (a receipt, passwords) keep the last space used."""
    off = {"show_admin": False}
    if user.is_shop_user:
        return {"space": "shops", "show_cash": False, "show_shops": True, "can_switch": False, **off}
    if not user.sees_all_shops:
        return {"space": "cash", "show_cash": True, "show_shops": False, "can_switch": False, **off}
    path = request.path
    agents_ok = user.sees_agent_supervision
    if user.is_superadmin and path.startswith(ADMIN_PREFIXES):
        space = "admin"
    elif path.startswith("/shops/"):
        space = "shops"
    elif agents_ok and path.startswith(AGENT_PREFIXES):
        space = "agents"
    elif path.startswith(NEUTRAL_PREFIXES) or path.startswith(("/administration/", "/setup/")):
        space = request.session.get("space", "cash")
    elif agents_ok and path.startswith("/reports/") and request.session.get("space") == "agents":
        space = "agents"  # an agent / POS report opened from the agents space stays there
    else:
        space = "cash"
    if user.is_market and space in ("cash", "admin"):  # the market supervisor has no cashier space
        space = "agents"
    if request.session.get("space") != space:
        request.session["space"] = space
    return {"space": space, "show_cash": space == "cash", "show_shops": space == "shops",
            "show_admin": space == "admin", "show_agents": space == "agents", "can_switch": True}
