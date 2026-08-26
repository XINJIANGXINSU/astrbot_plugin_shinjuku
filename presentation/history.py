"""Session history presentation."""

from __future__ import annotations

from typing import Any

try:
    from ..core.constants import (
        SESSION_CLOSE_FORCE_CLOSED,
        SESSION_CLOSE_GRACE_CANCELLED,
        SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
    )
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.constants import (
        SESSION_CLOSE_FORCE_CLOSED,
        SESSION_CLOSE_GRACE_CANCELLED,
        SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
    )

from .common import date_time, money


CLOSE_REASON_LABELS = {
    SESSION_CLOSE_GRACE_CANCELLED: "宽限内取消",
    SESSION_CLOSE_LOGIN_GRACE_CANCELLED: "开门宽限内取消",
    SESSION_CLOSE_FORCE_CLOSED: "管理员强制退场",
}


def format_history(sessions: list[dict[str, Any]], currency: str) -> str:
    if not sessions:
        return "暂无历史记录。"
    lines = ["--- 历史记录 ---"]
    for item in sessions:
        start = item.get("createdAt")
        end = item.get("closedAt")
        cost = item.get("finalCost")
        active = "进行中" if item.get("isActive") else "已结束"
        markers = []
        if item.get("ENTRY_TYPE") == "sneak":
            markers.append("偷偷上机")
        close_reason = CLOSE_REASON_LABELS.get(item.get("closeReason"))
        if close_reason:
            markers.append(close_reason)
        marker = f"（{'，'.join(markers)}）" if markers else ""
        lines.append(
            f"[{item['id']}] {active}{marker}｜{date_time(start)} -> "
            f"{date_time(end) if end else '现在'}｜{money(cost)} {currency}"
        )
    return "\n".join(lines)
