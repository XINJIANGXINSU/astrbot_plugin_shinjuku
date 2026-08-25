"""Session history presentation."""

from __future__ import annotations

from typing import Any

from .common import date_time, money


def format_history(sessions: list[dict[str, Any]], currency: str) -> str:
    if not sessions:
        return "暂无历史记录。"
    lines = ["--- 历史记录 ---"]
    for item in sessions:
        start = item.get("createdAt")
        end = item.get("closedAt")
        cost = item.get("finalCost")
        active = "进行中" if item.get("isActive") else "已结束"
        marker = "（偷偷上机）" if item.get("ENTRY_TYPE") == "sneak" else ""
        lines.append(
            f"[{item['id']}] {active}{marker}｜{date_time(start)} -> "
            f"{date_time(end) if end else '现在'}｜{money(cost)} {currency}"
        )
    return "\n".join(lines)
