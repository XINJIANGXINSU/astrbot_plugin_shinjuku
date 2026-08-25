"""Current player presence presentation."""

from __future__ import annotations

from typing import Any

from .common import date_time


def format_players(
    users: list[dict[str, Any]],
    nicknames: dict[str, str] | None = None,
    mask_sneak: bool = False,
) -> str:
    nicknames = nicknames or {}
    lines = [f"👥 店内目前共有 {len(users)} 人"]
    for user in users:
        qq = ""
        for bind in user.get("binds", []):
            if bind.get("type") == "QQ":
                qq = str(bind.get("bid") or "")
                break
        session = (user.get("sessions") or [{}])[0]
        if mask_sneak and session.get("ENTRY_TYPE") == "sneak":
            name = "未知玩家"
        else:
            name = nicknames.get(qq) or qq or f"用户#{user['id']}"
        lines.extend(
            [
                "",
                f"玩家: {name}",
                f"入场时间: {date_time(session.get('createdAt'))}",
            ]
        )
    return "\n".join(lines)
