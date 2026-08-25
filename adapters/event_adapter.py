"""AstrBot event parsing and nickname resolution."""

from __future__ import annotations

import re
import shlex
from typing import Any

try:
    from ..core.errors import ShinjukuError
    from .nickname_cache import NicknameCache
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.errors import ShinjukuError
    from adapters.nickname_cache import NicknameCache


COMMAND_NAMES = {
    "register", "login", "logout", "list", "wallet", "history", "ahistory",
    "billing", "items", "redeem", "add", "mj", "member", "coupon",
    "giftcode", "j", "入场", "上机", "出场", "下机", "离场", "退场",
    "历史记录", "账单", "b", "背包", "钱包", "xsj", "新宿几", "窝几",
    "wj", "新宿j", "死给", "开门", "偷偷上机",
}


class EventAdapter:
    def __init__(self, nicknames: NicknameCache):
        self.nicknames = nicknames

    @staticmethod
    def sender_id(event: Any) -> str:
        return str(event.get_sender_id())

    def sender_real_qq(self, event: Any) -> str:
        raw = self.sender_id(event)
        if re.fullmatch(r"\d+", raw or ""):
            return raw
        for holder_name in ("sender", "message_obj"):
            holder = getattr(event, holder_name, None)
            if not holder:
                continue
            for attr in ("qq", "user_id", "uin", "uid", "id"):
                value = getattr(holder, attr, None)
                if value is None:
                    continue
                text = str(value)
                if re.fullmatch(r"\d{5,}", text):
                    return text
        try:
            components = event.get_messages() or []
        except Exception:
            components = []
        for component in components:
            for attr in ("qq", "user_id", "uin", "uid", "id"):
                value = getattr(component, attr, None)
                if value is None:
                    continue
                text = str(value)
                if re.fullmatch(r"\d{5,}", text):
                    return text
        for getter in ("get_sender_qq", "get_user_id", "get_sender_uin"):
            method = getattr(event, getter, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    value = None
                if value is not None:
                    text = str(value)
                    if re.fullmatch(r"\d{5,}", text):
                        return text
        return raw

    def sender_uid(self, event: Any) -> str:
        return f"QQ:{self.sender_real_qq(event)}"

    def nickname_scope(self, event: Any) -> str:
        try:
            platform = str(event.get_platform_name() or "unknown")
        except Exception:
            platform = str(
                getattr(getattr(event, "platform_meta", None), "name", "")
                or "unknown"
            )
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        if group_id is not None and str(group_id):
            return f"{platform}:group:{group_id}"
        return f"{platform}:private:{self.sender_real_qq(event)}"

    def remember_sender_name(self, event: Any) -> None:
        qq = self.sender_real_qq(event)
        scope = self.nickname_scope(event)
        for name in (
            "get_sender_name",
            "get_sender_nickname",
            "get_sender_display_name",
        ):
            method = getattr(event, name, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    value = None
                if value:
                    self.nicknames.set(scope, qq, str(value))
                    return
        for holder_name in ("sender", "message_obj"):
            holder = getattr(event, holder_name, None)
            if not holder:
                continue
            for attr in ("nickname", "nick", "name", "card", "display_name"):
                value = getattr(holder, attr, None)
                if value:
                    self.nicknames.set(scope, qq, str(value))
                    return

    @staticmethod
    def args(event: Any) -> list[str]:
        text = event.message_str.strip()
        if not text:
            return []
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        if not parts:
            return []
        command = parts[0].lstrip("/").split("@", 1)[0]
        return parts[1:] if command in COMMAND_NAMES else parts

    @staticmethod
    def at_ids(event: Any) -> list[str]:
        ids: list[str] = []
        try:
            components = event.get_messages()
        except Exception:
            components = []
        for component in components:
            kind = f"{type(component).__name__} {getattr(component, 'type', '')}".lower()
            if "at" in kind or "mention" in kind:
                for attr in ("qq", "user_id", "target", "id"):
                    value = getattr(component, attr, None)
                    if value:
                        ids.append(str(value))
                        break
                continue
            text = getattr(component, "text", None)
            if text:
                for match in re.finditer(
                    r"\[CQ:at,qq=[\"']?(\d+)[\"']?\]", str(text)
                ):
                    ids.append(match.group(1))
        return ids

    def at_label(self, event: Any, uid: str) -> str:
        qq = uid.split(":", 1)[1] if uid.startswith("QQ:") else uid
        scope = self.nickname_scope(event)
        try:
            components = event.get_messages()
        except Exception:
            components = []
        for component in components:
            kind = f"{type(component).__name__} {getattr(component, 'type', '')}".lower()
            if "at" not in kind and "mention" not in kind:
                continue
            component_id = None
            for attr in ("qq", "user_id", "target", "id"):
                value = getattr(component, attr, None)
                if value:
                    component_id = str(value)
                    break
            if component_id != qq:
                continue
            for attr in ("name", "nickname", "nick", "display_name", "display"):
                value = getattr(component, attr, None)
                if value:
                    self.nicknames.set(scope, qq, str(value))
                    return f"{value} ({qq})"
        remembered = self.nicknames.get(scope, qq)
        if remembered:
            return f"{remembered} ({qq})"
        return qq

    def normalize_user(
        self, raw: str | None, event: Any, allow_self: bool = True
    ) -> str:
        if not raw:
            if allow_self:
                return self.sender_uid(event)
            raise ShinjukuError("请指定用户。")
        if raw.startswith("QQ:"):
            return raw
        match = re.search(r"\((\d+)\)", raw)
        if match:
            return f"QQ:{match.group(1)}"
        match = re.search(r"\d+", raw)
        if match:
            return f"QQ:{match.group(0)}"
        raise ShinjukuError("无法识别用户，请使用 @用户 或 QQ 号。")

    @staticmethod
    def qq_from_uid(uid: str) -> str:
        if not uid.startswith("QQ:"):
            raise ShinjukuError("只能自动注册 QQ 用户。")
        return uid.split(":", 1)[1]
