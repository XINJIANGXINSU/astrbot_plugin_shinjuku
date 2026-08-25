"""User identity, binding, and linked profile queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

try:
    from ..core.errors import ShinjukuError
    from ..infrastructure.storage import DBConn, row_to_dict
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.errors import ShinjukuError
    from infrastructure.storage import DBConn, row_to_dict


Clock = Callable[[], datetime]


class UserService:
    """Owns user and binding persistence without owning transactions."""

    def __init__(self, now: Clock = datetime.now):
        self.now = now

    async def find_user(self, uid: str | int, conn: DBConn) -> dict[str, Any] | None:
        if isinstance(uid, int):
            return row_to_dict(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', uid))

        uid_text = str(uid)
        if ":" in uid_text:
            bind_type, bind_id = uid_text.split(":", 1)
            return row_to_dict(
                await conn.fetchrow(
                    'SELECT u.* FROM "User" u JOIN "Bind" b ON b."userId"=u.id '
                    'WHERE b.type=? AND b.bid=? LIMIT 1',
                    bind_type,
                    bind_id,
                )
            )
        if uid_text.isdigit():
            return row_to_dict(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', int(uid_text)))
        return None

    async def register(self, platform_id: str, conn: DBConn) -> dict[str, Any]:
        existing = await self.find_user(f"QQ:{platform_id}", conn)
        if existing:
            return {"user": existing, "created": False}
        created = await conn.execute('INSERT INTO "User" ("createdAt") VALUES (?)', self.now())
        user = row_to_dict(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', created.lastrowid))
        await conn.execute(
            'INSERT INTO "Bind" (type, bid, "userId") VALUES (?, ?, ?)',
            "QQ",
            platform_id,
            user["id"],
        )
        return {"user": user, "created": True}

    async def mahjong_rank(self, uid: str | int, conn: DBConn) -> dict[str, Any] | None:
        """Return the MahjongRank profile when the linked plugin table exists."""
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        exists = await conn.fetchval(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='MahjongRank'"
        )
        if not exists:
            return None
        return row_to_dict(
            await conn.fetchrow(
                'SELECT * FROM "MahjongRank" WHERE "userId"=? LIMIT 1',
                user["id"],
            )
        )
