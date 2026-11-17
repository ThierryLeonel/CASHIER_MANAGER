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


def _require_admin(admin, user=None):
    if admin.is_authenticated and admin.is_shop_senior and user is not None and user.is_shop_manager:
        return  # the senior shop manager manages the shop staff
    if not (admin.is_authenticated and admin.is_superadmin):
        raise PermissionDenied(_("Only the super administrator manages user accounts."))


def reset_password(admin, user, *, new_password=None, must_change=True):
    """Replace a user's password. Returns the new password so it can be shown once."""
    _require_admin(admin, user)
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
    _require_admin(admin, user)
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


def _agent_username(cp):
    """The agent's business SIM as username (easy to remember); a suffix when it is taken."""
    base = cp.business_sim
    name, n = base, 1
    while User.objects.filter(username__iexact=name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def create_agent_login(admin, cp, *, username="", password=None, must_change=True):
    """One commercial agent's login, linked to his agent account. Returns (user, password shown once)."""
    from django.db import IntegrityError, transaction

    from core.models import Counterparty

    _require_admin(admin)
    if cp.kind != Counterparty.Kind.AGENT:
        raise BusinessError(_("%(a)s is a POS: only commercial agents may have a login.") % {"a": cp})
    if User.objects.filter(counterparty=cp).exists():
        raise BusinessError(_("%(a)s already has a login.") % {"a": cp})
    username = (username or "").strip() or _agent_username(cp)
    if User.objects.filter(username__iexact=username).exists():
        raise BusinessError(_("The username %(u)s is already taken.") % {"u": username})
    pw = password or generate_password()
    u = User(username=username, first_name=cp.name[:150], role=User.Role.AGENT, counterparty=cp)
    try:
        validate_password(pw, u)
    except ValidationError as e:
        raise BusinessError(" ".join(e.messages), "password")
    try:
        with transaction.atomic():
            u.set_password(pw)
            u.must_change_password = bool(must_change)
            u.save()
    except IntegrityError:
        raise BusinessError(_("%(a)s already has a login.") % {"a": cp})
    log_event(admin, "agent_login_created", user_id=u.pk, username=u.username, agent=cp.business_sim)
    return u, pw


def create_agent_logins(admin, agents, *, password=None, must_change=True):
    """Logins for several commercial agents at once (all or nothing). Returns [(agent, user, password)]."""
    from django.db import transaction

    _require_admin(admin)
    agents = list(agents)
    if not agents:
        raise BusinessError(_("Select at least one commercial agent without a login."))
    out = []
    with transaction.atomic():
        for cp in agents:
            u, pw = create_agent_login(admin, cp, password=password, must_change=must_change)
            out.append((cp, u, pw))
    return out
