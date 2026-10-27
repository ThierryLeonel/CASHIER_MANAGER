"""Server-side authorisation rules (used by services and views)."""
from django.utils.translation import gettext as _

from core.models import Account, AccountAccess, User

from .errors import PermissionDenied

R = User.Role

ENTRY_ROLES = (R.CASHIER,)
TRANSFER_RESPONSIBILITY_ROLES = (R.CASHIER, R.MANAGER, R.FINANCE)
CORRECTION_ROLES = (R.MANAGER, R.FINANCE)
RESOLUTION_ROLES = (R.MANAGER, R.FINANCE)
OPENING_ROLES = (R.SUPERADMIN,)
CONFIG_ROLES = (R.SUPERADMIN,)
REPORT_ROLES = (R.SUPERADMIN, R.MANAGER, R.FINANCE, R.CASHIER)


def require_role(user, roles, action=""):
    if not user or not user.is_authenticated or not user.is_active or user.role not in roles:
        raise PermissionDenied(_("You are not allowed to perform this action.") + (f" ({action})" if action else ""))


def _has_grant(user, account):
    return AccountAccess.objects.filter(user=user, account=account).exists()


def can_send_from(user, account):
    """Money/value may leave this company account on the user's entry."""
    return bool(
        user.is_cashier and account.active and account.is_asset
        and (account.custodian_id == user.id or _has_grant(user, account))
    )


def can_receive_into(user, account):
    """The user can confirm value arriving in this account. Company bank
    accounts accept confirmed deposits from any cashier."""
    if not (user.is_cashier and account.active and account.is_asset):
        return False
    if account.kind == Account.Kind.BANK:
        return True
    return account.custodian_id == user.id or _has_grant(user, account)


def usable_accounts(user, kinds, receiving=False):
    qs = Account.objects.filter(kind__in=kinds, active=True).select_related("sim", "custodian")
    if not user.is_cashier:
        return qs.none()
    from django.db.models import Q

    q = Q(custodian=user) | Q(grants__user=user)
    if receiving:
        q |= Q(kind=Account.Kind.BANK)
    return qs.filter(q).distinct()


def visible_cashier_ids(user):
    """None = all cashiers visible; otherwise list of cashier ids."""
    if user.sees_all_zones:
        return None
    if user.is_cashier:
        return [user.id]
    return []
