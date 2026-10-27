"""Business days. Each cashier has exactly one OPEN day; every entry they make
belongs to it. Closing the day opens the next one immediately, so entries
after closing belong to the next business day. Actual timestamps are kept
separately (Transaction.posted_at)."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import CashierDay


def douala_today():
    # TIME_ZONE is Africa/Douala, so localdate() is the Cameroon calendar date.
    return timezone.localdate()


def peek_open_day(cashier):
    return CashierDay.objects.filter(cashier=cashier, status=CashierDay.Status.OPEN).first()


def next_business_date(cashier):
    last = CashierDay.objects.filter(cashier=cashier).order_by("-business_date").first()
    today = douala_today()
    if last is None:
        return today
    return max(today, last.business_date + timedelta(days=1))


def current_day(cashier):
    day = peek_open_day(cashier)
    if day:
        return day
    try:
        with transaction.atomic():
            return CashierDay.objects.create(cashier=cashier, business_date=next_business_date(cashier))
    except IntegrityError:
        return peek_open_day(cashier)
