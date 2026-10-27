"""Exact money handling.

All amounts are whole FCFA (XAF has no sub-unit in use). Values are
`decimal.Decimal`, stored as NUMERIC(18,0). Binary floats are rejected.

Rounding rule (only needed for UVE->ER conversion): ROUND_HALF_UP to the
nearest whole FCFA, applied once to the ER amount received.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
MAX_AMOUNT = Decimal("999999999999")  # < 10^12 FCFA


class MoneyError(ValueError):
    pass


def to_money(value) -> Decimal:
    """Parse user/system input into a whole-FCFA Decimal. Accepts spaces,
    commas and dots as thousands separators (e.g. "100 000", "100,000")."""
    if isinstance(value, float):
        raise MoneyError("Floating-point money values are not allowed")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        text = str(value or "").strip().replace(" ", "").replace("\xa0", "").replace(" ", "")
        text = text.replace("FCFA", "").replace("XAF", "")
        # thousands separators only: "100,000" / "100.000"
        if text.count(",") and not text.count("."):
            text = text.replace(",", "")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        try:
            d = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError("Invalid amount") from exc
    if not d.is_finite():
        raise MoneyError("Invalid amount")
    if d != d.to_integral_value():
        raise MoneyError("Amounts must be whole FCFA")
    d = d.quantize(Decimal("1"))
    if abs(d) > MAX_AMOUNT:
        raise MoneyError("Amount too large")
    return d


def round_fcfa(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def conversion_er_amount(uve_amount: Decimal, uplift_rate: Decimal) -> Decimal:
    """ER received for a UVE conversion: uve * (1 + rate), half-up to FCFA.
    100000 @ 0.04 -> 104000; 12345 @ 0.04 -> 12839 (12838.80)."""
    return round_fcfa(uve_amount * (Decimal("1") + uplift_rate))


def fmt(value) -> str:
    """Format as '1 234 567' (French/Cameroon grouping)."""
    if value is None:
        return ""
    d = Decimal(value)
    sign = "-" if d < 0 else ""
    return sign + f"{abs(d):,.0f}".replace(",", " ")
