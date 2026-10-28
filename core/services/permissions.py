"""Server-side authorisation rules (used by services and views)."""
from django.utils.translation import gettext as _

from core.models import Account, AccountAccess, User

from .errors import PermissionDenied

R = User.Role

ENTRY_ROLES = (R.CASHIER,)
TRANSFER_RESPONSIBILITY_ROLES = (R.CASHIER, R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
CORRECTION_ROLES = (R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
RESOLUTION_ROLES = (R.MANAGER, R.JUNIOR, R.FINANCE, R.SUPERADMIN)
OPENING_ROLES = (R.SUPERADMIN,)
CONFIG_ROLES = (R.SUPERADMIN,)
REPORT_ROLES = (R.SUPERADMIN, R.MANAGER, R.JUNIOR, R.FINANCE, R.CASHIER)


def require_role(user, roles, action=""):
    if not user or not user.is_authenticated or not user.is_active or user.role not in roles:
        raise PermissionDenied(_("You are not allowed to perform this action.") + (f" ({action})" if action else ""))


def _has_grant(user, account):
    return AccountAccess.objects.filter(user=user, account=account).exists()


def _zone_rule(user, account):
    """Accounts belong to zones, not to cashiers:
    * an account of the cashier's own subzone is shared by all its cashiers;
    * an account held at master-zone level (e.g. the master SIM) is used only
      by its cashier in charge (Account.custodian);
    * explicit grants (configuration) add access to other accounts."""
    if account.custodian_id:
        return account.custodian_id == user.id
    return account.zone_id is not None and account.zone_id == user.subzone_id


def can_send_from(user, account):  # user = the desk cashier (also when the super admin acts for them)
    """Money/value may leave this company account on the user's entry."""
    return bool(
        user.is_cashier and account.active and account.is_asset
        and (_zone_rule(user, account) or _has_grant(user, account))
    )


def can_receive_into(user, account):
    """The user can confirm value arriving in this account. Company bank
    accounts accept confirmed deposits from any cashier."""
    if not (user.is_cashier and account.active and account.is_asset):
        return False
    if account.kind == Account.Kind.BANK:
        return True
    return _zone_rule(user, account) or _has_grant(user, account)


def zone_accounts_q(user):
    from django.db.models import Q

    return Q(custodian=user) | Q(custodian__isnull=True, zone_id=user.subzone_id) | Q(grants__user=user)


def usable_accounts(user, kinds, receiving=False):
    qs = Account.objects.filter(kind__in=kinds, active=True).select_related("sim", "custodian", "zone")
    if not user or not user.is_cashier:
        return qs.none()
    from django.db.models import Q

    q = zone_accounts_q(user)
    if receiving:
        q |= Q(kind=Account.Kind.BANK)
    return qs.filter(q).distinct()


def zone_closing_accounts_q(zone):
    """Accounts a subzone counts at closing: its own accounts plus master-level
    accounts whose cashier in charge works in this subzone."""
    from django.db.models import Q

    return Q(custodian__isnull=True, zone=zone) | Q(custodian__subzone=zone)


def visible_cashier_ids(user):
    """None = all cashiers visible; otherwise list of cashier ids."""
    if user.sees_all_zones:
        return None
    if user.is_cashier:
        return [user.id]
    return []


def in_scope(user, zone):
    """Can this user see data of `zone`? (junior managers: own zone tree)."""
    ids = user.zone_ids_in_scope() if user.is_authenticated else []
    if ids is None:
        return True
    return zone is not None and zone.id in ids


def require_zone(user, zone):
    """Junior managers act only inside their zone."""
    if user.is_authenticated and user.is_junior and not in_scope(user, zone):
        raise PermissionDenied(_("This belongs to another zone."))
