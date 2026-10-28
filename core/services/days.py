"""Business days belong to a subzone (desk). Every cashier of the subzone
records into the same open day; the subzone closes once, and the next day
opens immediately so later entries belong to the next business day. Actual
timestamps are kept separately (Transaction.posted_at)."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import CashierDay


def douala_today():
    # TIME_ZONE is Africa/Douala, so localdate() is the Cameroon calendar date.
    return timezone.localdate()


def peek_zone_day(zone):
    if zone is None:
        return None
    return CashierDay.objects.filter(zone=zone, status=CashierDay.Status.OPEN).first()


def peek_open_day(cashier):
    return peek_zone_day(getattr(cashier, "subzone", None))


def next_business_date(zone):
    last = CashierDay.objects.filter(zone=zone).order_by("-business_date").first()
    today = douala_today()
    if last is None:
        return today
    return max(today, last.business_date + timedelta(days=1))


def zone_day(zone, opened_by=None):
    day = peek_zone_day(zone)
    if day:
        return day
    try:
        with transaction.atomic():
            return CashierDay.objects.create(zone=zone, cashier=opened_by, business_date=next_business_date(zone))
    except IntegrityError:
        return peek_zone_day(zone)


def current_day(cashier):
    """Open business day of the cashier's subzone."""
    return zone_day(cashier.subzone, opened_by=cashier)
