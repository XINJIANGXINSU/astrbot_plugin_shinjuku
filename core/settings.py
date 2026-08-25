"""Normalized plugin settings independent from AstrBot runtime types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _int_setting(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PluginSettings:
    database_path: str
    currency: str
    admins: frozenset[str]
    redeem_code_on_register: str
    points_per_amount: int
    max_active_checkcodes: int
    self_open_door_enabled: bool
    self_open_door_points_threshold: int
    login_grace_minutes: int
    sneak_login_enabled: bool
    sneak_login_points_threshold: int
    billing: dict[str, Any]

    @classmethod
    def from_config(cls, config: Any, default_database_path: str) -> "PluginSettings":
        max_active_checkcodes = _int_setting(config.get("max_active_checkcodes"), 20)

        return cls(
            database_path=(
                str(config.get("database_path", "") or "") or default_database_path
            ),
            currency=str(config.get("currency", "馕") or "馕"),
            admins=frozenset(str(item) for item in (config.get("admins", []) or [])),
            redeem_code_on_register=str(
                config.get("redeem_code_on_register", "") or ""
            ),
            points_per_amount=max(
                0, _int_setting(config.get("points_per_amount"), 10)
            ),
            # Preserve the existing meaning of zero as "use the default capacity".
            max_active_checkcodes=max(1, max_active_checkcodes or 20),
            self_open_door_enabled=config.get("self_open_door_enabled") is not False,
            self_open_door_points_threshold=max(
                0, _int_setting(config.get("self_open_door_points_threshold"), 10)
            ),
            login_grace_minutes=max(
                0, _int_setting(config.get("login_grace_minutes"), 3)
            ),
            sneak_login_enabled=config.get("sneak_login_enabled") is True,
            sneak_login_points_threshold=max(
                0, _int_setting(config.get("sneak_login_points_threshold"), 10)
            ),
            billing=dict(config.get("billing", {}) or {}),
        )
