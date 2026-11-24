"""Sign-in protection: after MAX_WRONG wrong passwords in a row an account is suspended until an administrator resets
its password. Super administrators are never suspended (someone must be able to reset the others): they get the
15-minute lock of the sign-in page instead. Counted for every sign-in page (the app and Configuration)."""
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db.models import F
from django.dispatch import receiver
from django.utils import timezone

MAX_WRONG = 6


class SuspensionAwareBackend(ModelBackend):
    """A suspended account cannot sign in, even with the right password."""

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and getattr(user, "suspended_at", None) is None


def can_be_suspended(user):
    return user.role != user.Role.SUPERADMIN


@receiver(user_login_failed)
def count_wrong_password(sender, credentials, request=None, **kwargs):
    from core.models import User

    name = (credentials or {}).get("username")
    if not name:
        return
    u = User.objects.filter(username=name, is_active=True, suspended_at__isnull=True).first()
    if u is None:
        return
    User.objects.filter(pk=u.pk).update(failed_logins=F("failed_logins") + 1)
    u.refresh_from_db(fields=["failed_logins"])
    if u.failed_logins >= MAX_WRONG and can_be_suspended(u):
        User.objects.filter(pk=u.pk, suspended_at__isnull=True).update(suspended_at=timezone.now())
        from core.services.operations import log_event

        log_event(None, "account_suspended", user_id=u.pk, username=u.username, wrong_passwords=u.failed_logins,
                  ip=(request.META.get("REMOTE_ADDR") if request is not None else ""))


@receiver(user_logged_in)
def reset_wrong_passwords(sender, user, request=None, **kwargs):
    if getattr(user, "failed_logins", 0):
        type(user).objects.filter(pk=user.pk).update(failed_logins=0)
