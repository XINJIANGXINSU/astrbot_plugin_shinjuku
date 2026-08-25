"""Pricing table presentation."""

from __future__ import annotations

from typing import Any

try:
    from ..shinjuku_service import amount_to_cents
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from shinjuku_service import amount_to_cents

from .common import money


def format_pricing(config: dict[str, Any], currency: str) -> str:
    day_price = amount_to_cents(config.get("day_price") or 12)
    day_price_pass = amount_to_cents(config.get("day_price_pass") or 11)
    day_cap = amount_to_cents(config.get("day_cap") or 69)
    day_cap_pass = amount_to_cents(config.get("day_cap_pass") or 59)
    night_price = amount_to_cents(config.get("night_price") or 13)
    night_price_pass = amount_to_cents(config.get("night_price_pass") or 12)
    night_cap = amount_to_cents(config.get("night_cap") or 69)
    night_cap_pass = amount_to_cents(config.get("night_cap_pass") or 59)
    cap_24h = amount_to_cents(config.get("cap_24h") or 99)
    cap_24h_pass = amount_to_cents(config.get("cap_24h_pass") or 88)
    day_start = str(config.get("day_start") or "11:30")
    day_end = str(config.get("day_end") or "00:00")
    night_start = str(config.get("night_start") or "00:00")
    night_end = str(config.get("night_end") or "12:00")
    late_day_start = str(config.get("late_day_start") or "23:00")
    night_cap_cover_start = str(config.get("night_cap_cover_start") or "23:30")

    lines = [
        "--- 新宿定价表 ---",
        f"【白天】{day_start} - {day_end}",
        f"  普通用户：{money(day_price)} {currency}/小时，封顶 {money(day_cap)} {currency}",
        f"  月卡用户：{money(day_price_pass)} {currency}/小时，封顶 {money(day_cap_pass)} {currency}",
        f"【夜晚】{night_start} - {night_end}",
        f"  普通用户：{money(night_price)} {currency}/小时，封顶 {money(night_cap)} {currency}",
        f"  月卡用户：{money(night_price_pass)} {currency}/小时，封顶 {money(night_cap_pass)} {currency}",
        f"【深夜衔接】{late_day_start} - {day_end} 入场首小时按白天计费",
        f"  {night_cap_cover_start} 后入场时，首小时白天费用纳入包夜封顶，不再额外叠加",
        f"【连续 24 小时】封顶 {money(cap_24h)} {currency}（月卡 {money(cap_24h_pass)} {currency}）",
    ]
    return "\n".join(lines)
