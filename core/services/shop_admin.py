"""Shop administration by the senior shop manager (and the super administrator): shops, shop staff and their
access (shop + duties), services, items and categories. Anything with history is never deleted — it is
deactivated instead, so past records keep their meaning."""
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils.translation import gettext as _

from core.models import SHOP_DUTY_CODES, Account, Shop, User

from . import accounts as acc
from .errors import BusinessError, PermissionDenied
from .operations import log_event


def require_shop_admin(actor):
    if not (actor.is_authenticated and (actor.is_superadmin or actor.is_shop_senior)):
        raise PermissionDenied(_("Only the senior shop manager or the super administrator manages the shops."))


def _staff(actor, user):
    require_shop_admin(actor)
    if not user.is_shop_manager:
        raise PermissionDenied(_("Only shop staff accounts are managed here."))
    return user


def _duties(duties):
    chosen = [d for d in SHOP_DUTY_CODES if d in set(duties or [])]
    return "" if len(chosen) == len(SHOP_DUTY_CODES) else ",".join(chosen)


# ---------------------------------------------------------------- shops
def save_shop(actor, form):
    require_shop_admin(actor)
    is_new = form.instance.pk is None
    shop = form.save()
    log_event(actor, "shop_created" if is_new else "shop_updated", shop_id=shop.pk, name=shop.name,
              active=shop.active)
    return shop


def delete_shop(actor, shop):
    """Delete a shop that was never used. A shop with operations, stock or staff is deactivated instead."""
    require_shop_admin(actor)
    staff = User.objects.filter(shop=shop).count()
    if staff:
        raise BusinessError(_("%(shop)s still has %(n)s staff account(s). Move them to another shop first, or "
                              "deactivate the shop.") % {"shop": shop.name, "n": staff})
    name, pk = shop.name, shop.pk
    try:
        with transaction.atomic():
            Account.objects.filter(shop=shop).delete()
            shop.delete()
    except (ProtectedError, RestrictedError):
        raise BusinessError(_("%(shop)s has recorded operations, stock or services, so it cannot be deleted. "
                              "Untick Active to close it; its history stays in the reports.") % {"shop": name})
    log_event(actor, "shop_deleted", shop_id=pk, name=name)


# ---------------------------------------------------------------- staff and their access
def create_staff(actor, *, username, first_name, last_name="", shop, duties, password=None):
    """Create a shop staff login. Returns (user, password shown once); the password must be changed at first
    sign-in."""
    require_shop_admin(actor)
    username = (username or "").strip()
    if not username:
        raise BusinessError(_("Choose a username."), "username")
    if User.objects.filter(username__iexact=username).exists():
        raise BusinessError(_("This username is already taken."), "username")
    if shop is None:
        raise BusinessError(_("Choose the shop."), "shop")
    try:
        with transaction.atomic():
            u = User(username=username, first_name=first_name, last_name=last_name,
                     role=User.Role.SHOP_MANAGER, shop=shop, shop_duties=_duties(duties))
            u.set_unusable_password()
            u.save()
            pw = acc.reset_password(actor, u, new_password=password or None, must_change=True)
    except IntegrityError:
        raise BusinessError(_("This username is already taken."), "username")
    log_event(actor, "shop_staff_created", user_id=u.pk, username=u.username, shop=shop.name,
              duties=u.shop_duties or "ALL")
    return u, pw


def update_staff(actor, user, *, first_name, last_name="", shop, duties):
    """Change a staff member's shop (transfer) and duties."""
    _staff(actor, user)
    if shop is None:
        raise BusinessError(_("Choose the shop."), "shop")
    old_shop = user.shop.name if user.shop_id else ""
    user.first_name, user.last_name, user.shop, user.shop_duties = first_name, last_name, shop, _duties(duties)
    user.save(update_fields=["first_name", "last_name", "shop", "shop_duties"])
    log_event(actor, "shop_staff_updated", user_id=user.pk, username=user.username, from_shop=old_shop,
              to_shop=shop.name, duties=user.shop_duties or "ALL")
    return user


def set_staff_active(actor, user, active):
    _staff(actor, user)
    acc.set_active(actor, user, active)


def reset_staff_password(actor, user):
    _staff(actor, user)
    return acc.reset_password(actor, user, must_change=True)


def delete_staff(actor, user):
    """Delete a staff login that never recorded anything; otherwise it must be deactivated."""
    _staff(actor, user)
    name, pk = user.username, user.pk
    try:
        with transaction.atomic():
            user.delete()
    except (ProtectedError, RestrictedError):
        raise BusinessError(_("%(u)s has recorded operations, so the account cannot be deleted. Deactivate it "
                              "instead; the records keep the name.") % {"u": name})
    log_event(actor, "shop_staff_deleted", user_id=pk, username=name)


# ---------------------------------------------------------------- services, items, categories
def delete_definition(actor, obj):
    """Delete a shop service, item or category that was never used; otherwise tell to deactivate it."""
    require_shop_admin(actor)
    label = str(obj)
    try:
        with transaction.atomic():
            obj.delete()
    except (ProtectedError, RestrictedError):
        raise BusinessError(_("%(x)s is already used in records, so it cannot be deleted. Untick Active to remove "
                              "it from the lists.") % {"x": label})
    log_event(actor, "shop_definition_deleted", kind=obj.__class__.__name__, name=label)
    return label


__all__ = ["Shop"]
