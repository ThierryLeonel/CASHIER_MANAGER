from django.conf import settings


def app_context(request):
    ctx = {"COMPANY_NAME": settings.COMPANY_NAME, "company_address": settings.COMPANY_ADDRESS,
           "IDLE_SECONDS": settings.SESSION_IDLE_MINUTES * 60}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_cashier:
        from core.services.days import peek_open_day

        from core.services.days import douala_today

        ctx["open_day"] = peek_open_day(user)
        ctx["today_douala"] = douala_today()
    if user is not None and user.is_authenticated and user.is_superadmin:
        from core.views.base import acting_cashier

        ctx["acting_cashier"] = acting_cashier(request)
    return ctx
