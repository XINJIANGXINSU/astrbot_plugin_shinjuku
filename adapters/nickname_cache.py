from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class NicknameCacheEntry:
    name: str
    updated_at: float


class NicknameCache:
    """按会话范围隔离、带 TTL 和容量上限的内存昵称缓存。"""

    def __init__(
        self,
        ttl_seconds: float = 3 * 24 * 60 * 60,
        max_entries: int = 2000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: dict[tuple[str, str], NicknameCacheEntry] = {}

    def set(self, scope: str, qq: str, name: str) -> None:
        scope_text = str(scope or "unknown")
        qq_text = str(qq or "")
        name_text = str(name or "").strip()
        if not qq_text or not name_text:
            return
        now = self._clock()
        self._discard_expired(now)
        key = (scope_text, qq_text)
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item].updated_at)
            del self._entries[oldest]
        self._entries[key] = NicknameCacheEntry(name_text, now)

    def get(self, scope: str, qq: str) -> str | None:
        key = (str(scope or "unknown"), str(qq or ""))
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._expired(entry, self._clock()):
            del self._entries[key]
            return None
        return entry.name

    def snapshot(self, scope: str) -> dict[str, str]:
        now = self._clock()
        self._discard_expired(now)
        scope_text = str(scope or "unknown")
        return {
            qq: entry.name
            for (entry_scope, qq), entry in self._entries.items()
            if entry_scope == scope_text
        }

    def __len__(self) -> int:
        self._discard_expired(self._clock())
        return len(self._entries)

    def _expired(self, entry: NicknameCacheEntry, now: float) -> bool:
        return now - entry.updated_at >= self.ttl_seconds

    def _discard_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if self._expired(entry, now)]
        for key in expired:
            del self._entries[key]
