from decimal import Decimal

from django import template
from django.utils import timezone

from core.money import fmt
from core.simnumbers import display_sim

register = template.Library()


@register.filter
def fcfa(value):
    if value is None or value == "":
        return ""
    return fmt(value)


@register.filter
def sim(value):
    return display_sim(value)


@register.filter
def cell(row, col):
    v = row.get(col.key)
    if v is None or v == "":
        return ""
    if col.kind == "money":
        return fmt(v)
    if col.kind == "date":
        return v.strftime("%d/%m/%Y")
    if col.kind == "datetime":
        return timezone.localtime(v).strftime("%d/%m/%Y %H:%M")
    if col.kind == "rate":
        return f"{(Decimal(v) * 100).normalize()}%"
    return v


@register.filter
def get(d, key):
    return d.get(key) if d else None


@register.filter
def negative(v):
    try:
        return Decimal(v) < 0
    except Exception:
        return False


@register.filter
def field_kind(field):
    return field.field.widget.__class__.__name__


@register.filter
def absval(v):
    try:
        return abs(Decimal(v))
    except Exception:
        return v
