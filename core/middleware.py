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
