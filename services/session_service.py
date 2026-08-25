"""Session lifecycle and presence queries."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any, Callable

try:
    from ..core.errors import ShinjukuError
    from ..core.money import cents_to_text
    from ..infrastructure.storage import DBConn, row_to_dict, rows_to_dicts
    from .user_service import UserService
    from .wallet_service import WalletService
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.errors import ShinjukuError
    from core.money import cents_to_text
    from infrastructure.storage import DBConn, row_to_dict, rows_to_dicts
    from services.user_service import UserService
    from services.wallet_service import WalletService


Clock = Callable[[], datetime]


class SessionService:
    """Owns session persistence rules without owning transactions."""

    def __init__(
        self,
        users: UserService,
        wallets: WalletService,
        currency: str,
        max_active_checkcodes: int,
        self_open_door_enabled: bool,
        now: Clock = datetime.now,
    ):
        self.users = users
        self.wallets = wallets
        self.currency = currency
        self.max_active_checkcodes = max_active_checkcodes
        self.self_open_door_enabled = self_open_door_enabled
        self.now = now

    async def generate_checkcode(self, conn: DBConn) -> str:
        """Generate a unique seven-digit code among active sessions."""
        while True:
            code = f"{secrets.randbelow(9000000) + 1000000:07d}"
            exists = await conn.fetchval(
                'SELECT 1 FROM "Session" WHERE "CHECKCODE"=? AND "isActive"=1 LIMIT 1',
                code,
            )
            if not exists:
                return code

    @staticmethod
    async def active_session_count(conn: DBConn) -> int:
        return int(await conn.fetchval('SELECT count(*) FROM "Session" WHERE "isActive"=1') or 0)

    async def active_session(self, uid: str | int, conn: DBConn) -> dict[str, Any] | None:
        user = await self.users.find_user(uid, conn)
        if not user:
            return None
        return row_to_dict(
            await conn.fetchrow(
                'SELECT * FROM "Session" WHERE "userId"=? AND "isActive"=1 LIMIT 1',
                user["id"],
            )
        )

    async def login(
        self,
        uid: str | int,
        entry_type: str,
        generate_checkcode: bool,
        conn: DBConn,
    ) -> dict[str, Any]:
        user = await self.users.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在，请先注册。", "USER_NOT_FOUND")
        if await self.active_session(uid, conn):
            raise ShinjukuError("用户已经登录。", "USER_ALREADY_LOGGED_IN")
        wallet = await self.wallets.wallet(uid, conn)
        if wallet["total"]["available"] < 0:
            debt = -wallet["total"]["available"]
            raise ShinjukuError(
                f"当前欠费 {cents_to_text(debt)} {self.currency}，请先充值后再入场。",
                "INSUFFICIENT_BALANCE_FOR_LOGIN",
            )
        if self.self_open_door_enabled and generate_checkcode:
            checkcode = await self.generate_checkcode(conn)
        else:
            checkcode = None
        created = await conn.execute(
            'INSERT INTO "Session" ("userId", "createdAt", "isActive", "CHECKCODE", "ENTRY_TYPE") '
            "VALUES (?, ?, 1, ?, ?)",
            user["id"],
            self.now(),
            checkcode,
            entry_type,
        )
        session = row_to_dict(
            await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', created.lastrowid)
        )
        active_count = await self.active_session_count(conn)
        return {
            "session": session,
            "overCapacity": active_count > self.max_active_checkcodes,
            "activeCount": active_count,
        }

    async def door_verify(self, sender_uid: str | int, code_str: str | None, conn: DBConn) -> str:
        sender_user = await self.users.find_user(sender_uid, conn)
        sender_active_session = None
        if sender_user:
            sender_active_session = await self.active_session(sender_uid, conn)
        if code_str is None:
            if sender_active_session:
                return "NO_CODE_PRESENT"
            return "NO_CODE_OFFLINE"
        code_norm = str(code_str).strip()
        code_owner_session = None
        if code_norm and re.fullmatch(r"\d{7}", code_norm):
            code_owner_row = await conn.fetchrow(
                'SELECT * FROM "Session" WHERE "CHECKCODE"=? AND "isActive"=1 LIMIT 1',
                code_norm,
            )
            code_owner_session = row_to_dict(code_owner_row) if code_owner_row else None
        if sender_active_session:
            my_code = sender_active_session.get("CHECKCODE") or ""
            if my_code and code_norm and code_norm == my_code:
                opened = int(sender_active_session.get("doorOpened") or 0)
                if not opened:
                    await conn.execute(
                        'UPDATE "Session" SET "doorOpened"=1 WHERE id=?',
                        sender_active_session["id"],
                    )
                    return "SUCCESS_FIRST"
                return "SUCCESS_AGAIN"
            return "WRONG_CODE"
        if code_owner_session:
            return "STOLEN_CODE"
        return "NOT_PRESENT"

    async def force_logout(self, uid: str | int, conn: DBConn) -> dict[str, Any]:
        session = await self.active_session(uid, conn)
        if not session:
            raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
        await conn.execute(
            'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, "billingCost"=0, "finalCost"=0 WHERE id=?',
            self.now(),
            session["id"],
        )
        closed = row_to_dict(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session["id"]))
        return {"session": closed}

    async def history(self, uid: str | int, limit: int, conn: DBConn) -> list[dict[str, Any]]:
        user = await self.users.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        return rows_to_dicts(
            await conn.fetch(
                'SELECT * FROM "Session" WHERE "userId"=? ORDER BY "createdAt" DESC LIMIT ?',
                user["id"],
                limit,
            )
        )

    async def is_sneak_active(self, uid: str | int, conn: DBConn) -> bool:
        session = await self.active_session(uid, conn)
        return bool(session and session.get("ENTRY_TYPE") == "sneak")

    @staticmethod
    async def logged_in_users(conn: DBConn) -> list[dict[str, Any]]:
        users = rows_to_dicts(
            await conn.fetch(
                'SELECT DISTINCT u.* FROM "User" u JOIN "Session" s ON s."userId"=u.id '
                'WHERE s."isActive"=1 ORDER BY u.id'
            )
        )
        for user in users:
            user["binds"] = rows_to_dicts(
                await conn.fetch('SELECT * FROM "Bind" WHERE "userId"=?', user["id"])
            )
            user["sessions"] = rows_to_dicts(
                await conn.fetch(
                    'SELECT * FROM "Session" WHERE "userId"=? AND "isActive"=1',
                    user["id"],
                )
            )
        return users
