"""Shared value formatting used by presentation modules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from ..core.money import cents_to_text
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.money import cents_to_text


def money(value: Any) -> str:
    return cents_to_text(value)


def number(value: Any) -> str:
    return str(int(value or 0))


def date_time(value: Any) -> str:
    if not value:
        return "永不过期"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%Y/%m/%d %H:%M:%S")


def duration(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60}小时{minutes % 60}分钟"
    return f"{minutes}分钟"


def time_range(start: datetime, end: datetime) -> str:
    return f"{start:%m/%d %H:%M:%S} - {end:%m/%d %H:%M:%S}"
