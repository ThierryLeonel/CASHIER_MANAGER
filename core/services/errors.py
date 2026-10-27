class BusinessError(Exception):
    """A rule violation the user can fix; message is shown as-is."""

    def __init__(self, message, field=None):
        super().__init__(str(message))
        self.message = str(message)
        self.field = field


class PermissionDenied(BusinessError):
    pass


class InsufficientFunds(BusinessError):
    pass


class DuplicateSubmission(BusinessError):
    """The same form submission (idempotency key) was already posted."""

    def __init__(self, existing):
        from django.utils.translation import gettext as _

        super().__init__(_("This submission was already recorded as %(ref)s. Nothing was posted twice.")
                         % {"ref": getattr(existing, "reference", "")})
        self.existing = existing
