"""User accounts: password resets, activation, own password change. Passwords are stored hashed:
nobody, not even the super administrator, can read them; they can only be replaced."""
import secrets

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from core.models import User

from .errors import BusinessError, PermissionDenied
from .operations import log_event

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # no 0/O, 1/l/I


def generate_password(length=12):
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def _require_admin(admin):
    if not (admin.is_authenticated and admin.is_superadmin):
        raise PermissionDenied(_("Only the super administrator manages user accounts."))


def reset_password(admin, user, *, new_password=None, must_change=True):
    """Replace a user's password. Returns the new password so it can be shown once."""
    _require_admin(admin)
    pw = new_password or generate_password()
    try:
        validate_password(pw, user)
    except ValidationError as e:
        raise BusinessError(" ".join(e.messages), "new_password")
    user.set_password(pw)
    user.must_change_password = bool(must_change) and user.pk != admin.pk
    user.save(update_fields=["password", "must_change_password"])
    log_event(admin, "password_reset", user_id=user.pk, username=user.username, generated=new_password is None,
              must_change=user.must_change_password)
    return pw


def set_active(admin, user, active):
    _require_admin(admin)
    if user.pk == admin.pk and not active:
        raise BusinessError(_("You cannot deactivate your own account."))
    if not active and user.is_superadmin and not User.objects.filter(
            role=User.Role.SUPERADMIN, is_active=True).exclude(pk=user.pk).exists():
        raise BusinessError(_("At least one active super administrator must remain."))
    user.is_active = bool(active)
    user.save(update_fields=["is_active"])
    log_event(admin, "user_activated" if active else "user_deactivated", user_id=user.pk, username=user.username)


def password_changed_by_user(user):
    if user.must_change_password:
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
    log_event(user, "password_changed", user_id=user.pk)
