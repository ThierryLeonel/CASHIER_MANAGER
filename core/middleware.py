from django.utils import translation


class UserLanguageMiddleware:
    """Apply the signed-in user's saved language (after LocaleMiddleware)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        lang = getattr(user, "language", "") if user is not None and user.is_authenticated else ""
        if lang:
            translation.activate(lang)
            request.LANGUAGE_CODE = lang
        response = self.get_response(request)
        if lang:
            response.headers.setdefault("Content-Language", lang)
        return response


class ForcePasswordChangeMiddleware:
    """After an administrator reset, the user must choose a new password before anything else."""

    ALLOWED = ("/account/password/", "/logout/", "/language/", "/session/", "/static/", "/favicon.ico",
               "/branding/", "/login/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and getattr(user, "must_change_password", False)
                and not request.path.startswith(self.ALLOWED)):
            from django.shortcuts import redirect

            return redirect("my_password")
        return self.get_response(request)
