"""Cash managers (people / banks the company exchanges cash and UVE with), managed by the finance manager and
the super administrator. A cash manager with exchanges or a balance is never deleted, only deactivated."""
from django.db import transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils.translation import gettext as _

from core.models import Account, CashManager, User

from . import permissions as perm
from .errors import BusinessError
from .operations import log_event

R = User.Role
CM_ADMIN_ROLES = (R.FINANCE, R.SUPERADMIN)


def save_cash_manager(user, form):
    perm.require_role(user, CM_ADMIN_ROLES, "cash managers")
    is_new = form.instance.pk is None
    cm = form.save()
    Account.objects.filter(kind=Account.Kind.CASH_MANAGER, cash_manager=cm).update(
        name=f"Cash manager {cm.name}", active=cm.active)
    log_event(user, "cash_manager_created" if is_new else "cash_manager_updated", cash_manager_id=cm.pk,
              name=cm.name, active=cm.active)
    return cm


def delete_cash_manager(user, cm):
    perm.require_role(user, CM_ADMIN_ROLES, "cash managers")
    name, pk = str(cm), cm.pk
    try:
        with transaction.atomic():
            Account.objects.filter(kind=Account.Kind.CASH_MANAGER, cash_manager=cm).delete()
            cm.delete()
    except (ProtectedError, RestrictedError):
        raise BusinessError(_("%(cm)s already has exchanges, so it cannot be deleted. Untick Active to stop using "
                              "it; its history stays.") % {"cm": name})
    log_event(user, "cash_manager_deleted", cash_manager_id=pk, name=name)
