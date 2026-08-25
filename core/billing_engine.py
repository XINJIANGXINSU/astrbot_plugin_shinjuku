"""Pure billing segmentation and cap calculation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

try:
    from .money import amount_to_cents
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.money import amount_to_cents


class BillingEngine:
    """Calculate billing segments without database or AstrBot dependencies."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config if config is not None else {}

    @staticmethod
    def _clock_minutes(text: str) -> int:
        hour, minute = (int(part) for part in str(text).split(":"))
        return hour * 60 + minute

    @staticmethod
    def _next_boundary_at(current: datetime, boundary_minutes: int) -> datetime:
        candidate = current.replace(
            hour=boundary_minutes // 60,
            minute=boundary_minutes % 60,
            second=0,
            microsecond=0,
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _minutes_in_window(current: int, start: int, end: int) -> bool:
        """Return whether current is in a possibly overnight [start, end) window."""
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _entry_rule(self, entry_at: datetime) -> str:
        """Prefer the day rule for new entries where day and night overlap."""
        current = entry_at.hour * 60 + entry_at.minute
        day_start = self._clock_minutes(str(self.config.get("day_start") or "11:30"))
        day_end = self._clock_minutes(str(self.config.get("day_end") or "00:00"))
        night_start = self._clock_minutes(str(self.config.get("night_start") or "00:00"))
        night_end = self._clock_minutes(str(self.config.get("night_end") or "12:00"))
        if self._minutes_in_window(current, day_start, day_end):
            return "day"
        if self._minutes_in_window(current, night_start, night_end):
            return "night"
        return "day" if current >= day_start else "night"

    def _is_late_day_entry(
        self, entry_at: datetime, config_key: str, default: str
    ) -> bool:
        current = entry_at.hour * 60 + entry_at.minute
        window_start = self._clock_minutes(str(self.config.get(config_key) or default))
        day_end = self._clock_minutes(str(self.config.get("day_end") or "00:00"))
        return self._minutes_in_window(current, window_start, day_end)

    def calculate(
        self,
        start: datetime,
        end: datetime,
        pass_override: bool = False,
        session_start: datetime | None = None,
    ) -> dict[str, Any]:
        """Split a billing block into day/night segments and apply segment caps."""
        day_price = amount_to_cents(
            self.config.get("day_price_pass" if pass_override else "day_price") or 12
        )
        day_cap = amount_to_cents(
            self.config.get("day_cap_pass" if pass_override else "day_cap") or 69
        )
        night_price = amount_to_cents(
            self.config.get("night_price_pass" if pass_override else "night_price") or 13
        )
        night_cap = amount_to_cents(
            self.config.get("night_cap_pass" if pass_override else "night_cap") or 69
        )
        day_end_min = self._clock_minutes(str(self.config.get("day_end") or "00:00"))
        night_end_min = self._clock_minutes(str(self.config.get("night_end") or "12:00"))
        grace_minutes = int(self.config.get("grace_minutes") or 0)

        segments: list[dict[str, Any]] = []
        session_start = session_start or start

        def append_segment(
            rule: str,
            segment_start: datetime,
            segment_end: datetime,
            reason: str = "",
        ) -> None:
            duration_minutes = int((segment_end - segment_start).total_seconds() // 60)
            if duration_minutes <= 0:
                return
            rate = day_price if rule == "day" else night_price
            cap = day_cap if rule == "day" else night_cap
            units = duration_minutes // 60
            if duration_minutes % 60 > grace_minutes:
                units += 1
            raw_cost = units * rate
            cost = min(raw_cost, cap)
            segment = {
                "ruleId": 1 if rule == "day" else 2,
                "ruleName": "白天计费" if rule == "day" else "夜晚计费",
                "startTime": segment_start,
                "endTime": segment_end,
                "durationMinutes": duration_minutes,
                "rawCost": raw_cost,
                "cost": cost,
                "isCapped": raw_cost > cap,
                "reachedCap": raw_cost >= cap,
            }
            if reason:
                segment["reason"] = reason
            segments.append(segment)

        current = start
        first_block = start == session_start
        bridge_segment: dict[str, Any] | None = None

        if first_block and self._is_late_day_entry(
            session_start, "late_day_start", "23:00"
        ):
            bridge_end = min(session_start + timedelta(hours=1), end)
            append_segment("day", current, bridge_end, "late_entry_first_hour")
            bridge_segment = segments[-1] if segments else None
            current = bridge_end
            rule = "night"
        else:
            rule = self._entry_rule(current)

        while current < end:
            boundary_minutes = day_end_min if rule == "day" else night_end_min
            segment_end = min(self._next_boundary_at(current, boundary_minutes), end)
            append_segment(rule, current, segment_end)
            current = segment_end
            rule = "night" if rule == "day" else "day"

        cover_eligible = first_block and self._is_late_day_entry(
            session_start,
            "night_cap_cover_start",
            "23:30",
        )
        overnight_cap: dict[str, Any] | None = None
        if bridge_segment is not None and cover_eligible:
            bridge_index = segments.index(bridge_segment)
            covered_segments = [bridge_segment]
            for segment in segments[bridge_index + 1 :]:
                if segment["ruleId"] != 2:
                    break
                covered_segments.append(segment)
            if len(covered_segments) > 1:
                bundle_cost = sum(segment["cost"] for segment in covered_segments)
                if bundle_cost >= night_cap:
                    for segment in covered_segments:
                        segment["overnightCapCovered"] = True
                    overnight_cap = {
                        "startTime": covered_segments[0]["startTime"],
                        "endTime": covered_segments[-1]["endTime"],
                        "rawCost": bundle_cost,
                        "cappedCost": night_cap,
                        "saved": bundle_cost - night_cap,
                        "isCapped": True,
                    }

        total_cost = sum(segment["cost"] for segment in segments)
        if overnight_cap is not None:
            total_cost -= overnight_cap["saved"]
        return {
            "totalCost": total_cost,
            "startTime": start,
            "endTime": end,
            "segments": segments,
            "overnightCap": overnight_cap,
        }
