"""Side-effect-free money and discount conversions."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

try:
    from .errors import ShinjukuError
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.errors import ShinjukuError


MONEY_SCALE = 100
RATE_SCALE = 10000


def amount_to_cents(value: Any) -> int:
    """Convert an amount in yuan to integer cents using ROUND_HALF_UP."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ShinjukuError("金额格式不正确。", "INVALID_AMOUNT") from exc
    if not amount.is_finite():
        raise ShinjukuError("金额格式不正确。", "INVALID_AMOUNT")
    return int((amount * MONEY_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_text(value: Any) -> str:
    cents = int(value or 0)
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), MONEY_SCALE)
    return f"{sign}{whole}" if fraction == 0 else f"{sign}{whole}.{fraction:02d}"


def discount_tenths_to_bps(value: Any) -> int:
    """Convert a 0-10 discount value to basis points."""
    try:
        tenths = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ShinjukuError("折扣必须在 0-10 折之间。", "INVALID_DISCOUNT") from exc
    if not tenths.is_finite():
        raise ShinjukuError("折扣必须在 0-10 折之间。", "INVALID_DISCOUNT")
    bps = int((tenths * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 0 <= bps <= RATE_SCALE:
        raise ShinjukuError("折扣必须在 0-10 折之间。", "INVALID_DISCOUNT")
    return bps


def discounted_cents(amount_cents: int, rate_bps: int) -> int:
    """Apply a basis-point discount and round to the nearest cent."""
    return (int(amount_cents) * int(rate_bps) + RATE_SCALE // 2) // RATE_SCALE


def discount_tenths_text(rate_bps: int) -> str:
    return format((Decimal(int(rate_bps)) / 1000).normalize(), "f")
