from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, AsyncIterator

import aiosqlite

try:
    from .billing_engine import BillingEngine
    from .errors import ShinjukuError
    from .money import (
        MONEY_SCALE,
        RATE_SCALE,
        amount_to_cents,
        cents_to_text,
        discounted_cents as _discounted_cents,
        discount_tenths_text as _discount_tenths_text,
        discount_tenths_to_bps as _discount_tenths_to_bps,
    )
    from .schema import SCHEMA_SQL
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from billing_engine import BillingEngine
    from errors import ShinjukuError
    from money import (
        MONEY_SCALE,
        RATE_SCALE,
        amount_to_cents,
        cents_to_text,
        discounted_cents as _discounted_cents,
        discount_tenths_text as _discount_tenths_text,
        discount_tenths_to_bps as _discount_tenths_to_bps,
    )
    from schema import SCHEMA_SQL


CURRENCY_ASSET_TYPE = "CURRENCY"
TICKET_ASSET_TYPE = "TICKET"
PASS_ASSET_TYPE = "PASS"
POINTS_ASSET_TYPE = "POINTS"
PAID_CURRENCY_ASSET_ID = 10001
FREE_CURRENCY_ASSET_ID = 10002
MONTHLY_PASS_ASSET_ID = 10001
POINTS_ASSET_ID = 20001
MONEY_MIGRATION_KEY = "money_integer_cents_v1"
IDENTITY_CONSTRAINTS_MIGRATION_KEY = "identity_session_constraints_v1"
ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY = "asset_redeem_constraints_v1"


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _now() -> datetime:
    return datetime.now()


def _as_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _future_dt(duration_ms: Any) -> datetime | None:
    if not duration_ms:
        return None
    duration = float(duration_ms)
    if duration <= 0:
        return None
    return _now() + timedelta(milliseconds=duration)


def _same_dt(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left.replace(tzinfo=None) == right.replace(tzinfo=None)


DATETIME_COLUMNS = {
    "createdAt",
    "closedAt",
    "activeAt",
    "expireAt",
    "billingStart",
    "billingEnd",
    "startTime",
    "endTime",
}


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in DATETIME_COLUMNS:
        value = result.get(key)
        if isinstance(value, str):
            result[key] = _as_dt(value)
    return result


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [_row(row) for row in rows]


class ExecResult:
    def __init__(self, lastrowid: int | None = None):
        self.lastrowid = lastrowid


class DBConn:
    """SQLite 连接包装：提供与旧 asyncpg 用法接近的 fetch/execute/transaction 接口。"""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @staticmethod
    def _params(params: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(value.isoformat() if isinstance(value, datetime) else value for value in params)

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return _row(await cursor.fetchone())
        finally:
            await cursor.close()

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return _rows(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchval(self, sql: str, *params: Any) -> Any:
        row = await self.fetchrow(sql, *params)
        if not row:
            return None
        return next(iter(row.values()))

    async def execute(self, sql: str, *params: Any) -> ExecResult:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return ExecResult(cursor.lastrowid)
        finally:
            await cursor.close()

    @asynccontextmanager
    async def transaction(self, immediate: bool = False) -> AsyncIterator["DBConn"]:
        try:
            if immediate:
                await self._conn.execute("BEGIN IMMEDIATE")
            yield self
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise


class ShinjukuService:
    def __init__(
        self,
        db_path: str,
        currency: str = "馕",
        billing_config: dict[str, Any] | None = None,
        points_per_amount: int = 10,
        max_active_checkcodes: int = 20,
        self_open_door_enabled: bool = True,
        login_grace_minutes: int = 3,
    ):
        self.db_path = db_path
        self.currency = currency
        self.billing_config = billing_config or {}
        self.billing_engine = BillingEngine(self.billing_config)
        self.points_per_amount = max(0, int(points_per_amount or 0))
        self.max_active_checkcodes = max(1, int(max_active_checkcodes or 20))
        self.self_open_door_enabled = bool(self_open_door_enabled)
        self.login_grace_minutes = max(0, int(login_grace_minutes or 0))
        self._queue: asyncio.Queue[DBConn] | None = None
        self._init_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._queue is not None:
            return
        async with self._init_lock:
            if self._queue is not None:
                return
            if not self.db_path:
                raise ShinjukuError("请先在插件配置中填写 database_path。")
            directory = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(directory, exist_ok=True)
            raw_conns = []
            for _ in range(5):
                raw = await aiosqlite.connect(self.db_path)
                raw.row_factory = sqlite3.Row
                await raw.execute("PRAGMA journal_mode=WAL")
                await raw.execute("PRAGMA foreign_keys=ON")
                await raw.execute("PRAGMA busy_timeout=5000")
                raw_conns.append(raw)
            wrapped = [DBConn(raw) for raw in raw_conns]
            await self._init_schema(wrapped[0])
            queue: asyncio.Queue[DBConn] = asyncio.Queue()
            for item in wrapped:
                await queue.put(item)
            self._queue = queue

    async def close(self) -> None:
        if self._queue is not None:
            while not self._queue.empty():
                item = self._queue.get_nowait()
                await item._conn.close()
            self._queue = None

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[DBConn]:
        await self.connect()
        assert self._queue is not None
        conn = await self._queue.get()
        try:
            yield conn
        finally:
            await self._queue.put(conn)

    async def _init_schema(self, conn: DBConn) -> None:
        await conn._conn.executescript(SCHEMA_SQL)
        try:
            await conn._conn.execute('ALTER TABLE "Session" ADD COLUMN "CHECKCODE" TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            await conn._conn.execute('ALTER TABLE "Session" ADD COLUMN "doorOpened" INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        try:
            await conn._conn.execute('ALTER TABLE "Session" ADD COLUMN "ENTRY_TYPE" TEXT NOT NULL DEFAULT \'normal\'')
        except sqlite3.OperationalError:
            pass
        try:
            await conn._conn.execute('CREATE INDEX IF NOT EXISTS idx_session_checkcode ON "Session"("CHECKCODE")')
        except sqlite3.OperationalError:
            pass
        await self._migrate_money_to_integer_cents(conn)
        await self._ensure_identity_session_constraints(conn)
        await self._ensure_asset_redeem_constraints(conn)

    async def _ensure_asset_redeem_constraints(self, conn: DBConn) -> None:
        """确保资产定义、兑换码和一次性礼包领取由数据库强制约束。"""
        raw = conn._conn
        await raw.execute("BEGIN IMMEDIATE")
        try:
            duplicate_asset = await (
                await raw.execute(
                    'SELECT type,"assetId",count(*) FROM "Asset" '
                    'GROUP BY type,"assetId" HAVING count(*)>1 LIMIT 1'
                )
            ).fetchone()
            if duplicate_asset:
                raise ShinjukuError(
                    f"数据库存在重复资产定义：{duplicate_asset[0]}:{duplicate_asset[1]}，请先合并重复数据。",
                    "DUPLICATE_ASSET_DEFINITION_DATA",
                )

            duplicate_code = await (
                await raw.execute(
                    'SELECT code,count(*) FROM "Redeem" GROUP BY code HAVING count(*)>1 LIMIT 1'
                )
            ).fetchone()
            if duplicate_code:
                raise ShinjukuError(
                    f"数据库存在重复兑换码：{duplicate_code[0]}，请先处理重复数据。",
                    "DUPLICATE_REDEEM_CODE_DATA",
                )

            duplicate_once = await (
                await raw.execute(
                    'SELECT rr."userId",rr."presentId",count(*) FROM "RedeemRecord" rr '
                    'JOIN "Present" p ON p.id=rr."presentId" WHERE p."oncePerUser"=1 '
                    'GROUP BY rr."userId",rr."presentId" HAVING count(*)>1 LIMIT 1'
                )
            ).fetchone()
            if duplicate_once:
                raise ShinjukuError(
                    f"用户 #{duplicate_once[0]} 已重复领取一次性礼包 #{duplicate_once[1]}，请先处理重复记录。",
                    "DUPLICATE_ONCE_PRESENT_DATA",
                )

            await raw.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_type_asset_id ON "Asset"(type,"assetId")'
            )
            await raw.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_redeem_code ON "Redeem"(code)'
            )
            await raw.execute(
                '''CREATE TRIGGER IF NOT EXISTS trg_once_present_redeem_unique
                   BEFORE INSERT ON "RedeemRecord"
                   WHEN EXISTS (
                       SELECT 1 FROM "Present" p
                       WHERE p.id=NEW."presentId" AND p."oncePerUser"=1
                   ) AND EXISTS (
                       SELECT 1 FROM "RedeemRecord" rr
                       WHERE rr."userId"=NEW."userId" AND rr."presentId"=NEW."presentId"
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'once-per-user present already redeemed');
                   END'''
            )
            await raw.execute(
                'INSERT OR IGNORE INTO "SchemaMigration" (key,"appliedAt") VALUES (?,?)',
                (ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY, _now().isoformat()),
            )
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise

    async def _ensure_identity_session_constraints(self, conn: DBConn) -> None:
        """确保 QQ 绑定、活跃会话和单次会话验证码由数据库强制约束。"""
        raw = conn._conn
        await raw.execute("BEGIN IMMEDIATE")
        try:
            duplicate_bind = await (
                await raw.execute(
                    'SELECT type,bid,count(*) FROM "Bind" GROUP BY type,bid HAVING count(*)>1 LIMIT 1'
                )
            ).fetchone()
            if duplicate_bind:
                raise ShinjukuError(
                    f"数据库存在重复绑定：{duplicate_bind[0]}:{duplicate_bind[1]}，请先合并重复用户数据。",
                    "DUPLICATE_BIND_DATA",
                )

            duplicate_session = await (
                await raw.execute(
                    'SELECT "userId",count(*) FROM "Session" WHERE "isActive"=1 '
                    'GROUP BY "userId" HAVING count(*)>1 LIMIT 1'
                )
            ).fetchone()
            if duplicate_session:
                raise ShinjukuError(
                    f"用户 #{duplicate_session[0]} 存在多个活跃会话，请先处理重复上机记录。",
                    "DUPLICATE_ACTIVE_SESSION_DATA",
                )

            await raw.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_bind_type_bid ON "Bind"(type,bid)'
            )
            await raw.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_session_active_user ON "Session"("userId") '
                'WHERE "isActive"=1'
            )
            await raw.execute(
                '''CREATE TRIGGER IF NOT EXISTS trg_active_session_checkcode_immutable
                   BEFORE UPDATE OF "CHECKCODE" ON "Session"
                   WHEN OLD."isActive"=1 AND OLD."CHECKCODE" IS NOT NEW."CHECKCODE"
                   BEGIN
                       SELECT RAISE(ABORT, 'active session checkcode is immutable');
                   END'''
            )
            await raw.execute(
                'INSERT OR IGNORE INTO "SchemaMigration" (key,"appliedAt") VALUES (?,?)',
                (IDENTITY_CONSTRAINTS_MIGRATION_KEY, _now().isoformat()),
            )
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise

    async def _migrate_money_to_integer_cents(self, conn: DBConn) -> None:
        """一次性把旧库中的元/浮点金额迁移为分/整数金额。"""
        applied = await conn.fetchval('SELECT 1 FROM "SchemaMigration" WHERE key=?', MONEY_MIGRATION_KEY)
        if applied:
            return

        raw = conn._conn
        await raw.execute("BEGIN IMMEDIATE")
        try:
            # UserAsset.count 同时存放货币和非货币数量；仅货币乘 100，其他资产取整。
            await raw.execute("DROP INDEX IF EXISTS idx_userasset_user")
            await raw.execute('ALTER TABLE "UserAsset" RENAME TO "UserAsset_money_legacy"')
            await raw.execute(
                '''CREATE TABLE "UserAsset" (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    "userId" INTEGER NOT NULL REFERENCES "User"(id),
                    "assetDefId" INTEGER NOT NULL,
                    "assetType" TEXT NOT NULL,
                    "assetId" INTEGER REFERENCES "Asset"(id),
                    count INTEGER NOT NULL DEFAULT 0,
                    "activeAt" TEXT,
                    "expireAt" TEXT
                )'''
            )
            await raw.execute(
                '''INSERT INTO "UserAsset"
                   (id,"userId","assetDefId","assetType","assetId",count,"activeAt","expireAt")
                   SELECT id,"userId","assetDefId","assetType","assetId",
                          CASE WHEN "assetType"=? THEN CAST(ROUND(count * ?) AS INTEGER)
                               ELSE CAST(ROUND(count) AS INTEGER) END,
                          "activeAt","expireAt"
                   FROM "UserAsset_money_legacy"''',
                (CURRENCY_ASSET_TYPE, MONEY_SCALE),
            )
            await raw.execute('DROP TABLE "UserAsset_money_legacy"')
            await raw.execute('CREATE INDEX idx_userasset_user ON "UserAsset"("userId", "assetType")')

            await raw.execute(
                'UPDATE "Session" SET "billingCost"=CAST(ROUND("billingCost" * ?) AS INTEGER) '
                'WHERE "billingCost" IS NOT NULL',
                (MONEY_SCALE,),
            )
            await raw.execute(
                'UPDATE "Session" SET "finalCost"=CAST(ROUND("finalCost" * ?) AS INTEGER) '
                'WHERE "finalCost" IS NOT NULL',
                (MONEY_SCALE,),
            )
            session_columns = {
                row[1] for row in await (await raw.execute('PRAGMA table_info("Session")')).fetchall()
            }
            if "costOverwrite" in session_columns:
                await raw.execute(
                    'UPDATE "Session" SET "costOverwrite"=CAST(ROUND("costOverwrite" * ?) AS INTEGER) '
                    'WHERE "costOverwrite" IS NOT NULL',
                    (MONEY_SCALE,),
                )
            await raw.execute(
                'UPDATE "UserAssetLog" SET '
                '"changeAmount"=CAST(ROUND("changeAmount" * ?) AS INTEGER), '
                '"countBefore"=CAST(ROUND("countBefore" * ?) AS INTEGER), '
                '"countAfter"=CAST(ROUND("countAfter" * ?) AS INTEGER) '
                'WHERE "assetType"=?',
                (MONEY_SCALE, MONEY_SCALE, MONEY_SCALE, CURRENCY_ASSET_TYPE),
            )

            present_rows = await (await raw.execute('SELECT id, body FROM "Present" WHERE body IS NOT NULL')).fetchall()
            for present_id, body_text in present_rows:
                body = _json(body_text) or []
                changed = False
                for item in body:
                    if str(item.get("assetType")) == CURRENCY_ASSET_TYPE and "count" in item:
                        item["count"] = amount_to_cents(item["count"])
                        changed = True
                if changed:
                    await raw.execute(
                        'UPDATE "Present" SET body=? WHERE id=?',
                        (json.dumps(body, ensure_ascii=False), present_id),
                    )

            effect_rows = await (
                await raw.execute(
                    'SELECT id, "billingEffect" FROM "Asset" WHERE type=? AND "billingEffect" IS NOT NULL',
                    (TICKET_ASSET_TYPE,),
                )
            ).fetchall()
            for asset_id, effect_text in effect_rows:
                effect = _json(effect_text) or {}
                if effect.get("type") != "RATE" or "rateBps" in effect:
                    continue
                rate = Decimal(str(effect.pop("value", 1)))
                effect["rateBps"] = int(
                    (rate * RATE_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
                await raw.execute(
                    'UPDATE "Asset" SET "billingEffect"=? WHERE id=?',
                    (json.dumps(effect, ensure_ascii=False), asset_id),
                )

            await raw.execute(
                'INSERT INTO "SchemaMigration" (key,"appliedAt") VALUES (?,?)',
                (MONEY_MIGRATION_KEY, _now().isoformat()),
            )
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise

    async def _generate_checkcode(self, conn: DBConn) -> str:
        """生成不重复的7位数字验证码（CHECKCODE），仅在活跃会话范围内查重（离场后自动作废释放）。"""
        while True:
            code = f"{secrets.randbelow(9000000) + 1000000:07d}"
            exists = await conn.fetchval(
                'SELECT 1 FROM "Session" WHERE "CHECKCODE"=? AND "isActive"=1 LIMIT 1',
                code,
            )
            if not exists:
                return code

    async def _active_session_count(self, conn: DBConn) -> int:
        return int(await conn.fetchval('SELECT count(*) FROM "Session" WHERE "isActive"=1') or 0)

    def calculate_billing(
        self,
        start: datetime,
        end: datetime,
        pass_override: bool = False,
        session_start: datetime | None = None,
    ) -> dict[str, Any]:
        """Compatibility facade for the extracted pure billing engine."""
        return self.billing_engine.calculate(
            start,
            end,
            pass_override=pass_override,
            session_start=session_start,
        )

    def _cap_points(self, cap_value: int) -> int:
        """封顶金额折算积分：每 points_per_amount 元得 1 积分（向上取整）。"""
        if self.points_per_amount <= 0 or cap_value <= 0:
            return 0
        denominator = self.points_per_amount * MONEY_SCALE
        return (cap_value + denominator - 1) // denominator

    async def find_user(self, uid: str | int, conn: DBConn | None = None) -> dict[str, Any] | None:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                return await self.find_user(uid, acquired)

        if isinstance(uid, int):
            return _row(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', uid))

        uid_text = str(uid)
        if ":" in uid_text:
            bind_type, bind_id = uid_text.split(":", 1)
            return _row(
                await conn.fetchrow(
                    'SELECT u.* FROM "User" u JOIN "Bind" b ON b."userId"=u.id WHERE b.type=? AND b.bid=? LIMIT 1',
                    bind_type,
                    bind_id,
                )
            )
        if uid_text.isdigit():
            return _row(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', int(uid_text)))
        return None

    async def register(self, platform_id: str, register_code: str = "") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                exists = await self.find_user(f"QQ:{platform_id}", conn)
                if exists:
                    return {"user": exists, "created": False, "gift": None}
                created = await conn.execute('INSERT INTO "User" ("createdAt") VALUES (?)', _now())
                user = _row(await conn.fetchrow('SELECT * FROM "User" WHERE id=?', created.lastrowid))
                await conn.execute(
                    'INSERT INTO "Bind" (type, bid, "userId") VALUES (?, ?, ?)',
                    "QQ",
                    platform_id,
                    user["id"],
                )
                gift = None
                gift_error = None
                if register_code:
                    try:
                        gift = await self.redeem(user["id"], register_code, conn)
                    except ShinjukuError as exc:
                        gift_error = exc.message
                return {"user": user, "created": True, "gift": gift, "gift_error": gift_error}

    async def login(
        self,
        uid: str,
        entry_type: str = "normal",
        generate_checkcode: bool = True,
    ) -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                user = await self.find_user(uid, conn)
                if not user:
                    raise ShinjukuError("用户不存在，请先注册。", "USER_NOT_FOUND")
                if await self.active_session(uid, conn):
                    raise ShinjukuError("用户已经登录。", "USER_ALREADY_LOGGED_IN")
                wallet = await self.wallet(uid, False, conn)
                if wallet["total"]["available"] < 0:
                    debt = -wallet["total"]["available"]
                    raise ShinjukuError(
                        f"当前欠费 {cents_to_text(debt)} {self.currency}，请先充值后再入场。",
                        "INSUFFICIENT_BALANCE_FOR_LOGIN",
                    )
                if self.self_open_door_enabled and generate_checkcode:
                    checkcode = await self._generate_checkcode(conn)
                else:
                    checkcode = None
                created = await conn.execute(
                    'INSERT INTO "Session" ("userId", "createdAt", "isActive", "CHECKCODE", "ENTRY_TYPE") VALUES (?, ?, 1, ?, ?)',
                    user["id"],
                    _now(),
                    checkcode,
                    entry_type,
                )
                session = _row(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', created.lastrowid))
                active_count = await self._active_session_count(conn)
                over_capacity = active_count > self.max_active_checkcodes
                return {"session": session, "overCapacity": over_capacity, "activeCount": active_count}

    async def door_verify(self, sender_uid: str, code_str: str | None) -> str:
        """自助开门校验。返回状态：
        SUCCESS_FIRST    在场+自己验证码正确+本次入场第一次成功开门
        SUCCESS_AGAIN    在场+自己验证码正确+非第一次
        WRONG_CODE       在场但验证码不对或不是自己的
        STOLEN_CODE      不在场但验证码是场内某人的（冒用他人验证码）
        NOT_PRESENT      不在场+验证码也不是场内任何人的
        NO_CODE_PRESENT  在场没带验证码
        NO_CODE_OFFLINE  不在场没带验证码
        """
        async with self._acquire() as conn:
            async with conn.transaction():
                sender_user = await self.find_user(sender_uid, conn)
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
                    code_owner_session = _row(code_owner_row) if code_owner_row else None
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

    async def logout(self, uid: str) -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                session_before = await self.active_session(uid, conn)
                if not session_before:
                    raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
                played_seconds = int((_now() - session_before["createdAt"]).total_seconds())
                door_opened = bool(int(session_before.get("doorOpened") or 0))
                login_grace_seconds = self.login_grace_minutes * 60
                # 只有成功自助开门的会话才启用首小时特殊门槛：
                # 入场后不超过 login_grace_minutes 分钟离场时免费，超过后首小时按 1 小时计费。
                force_mode = (
                    door_opened
                    and played_seconds <= login_grace_seconds
                    and played_seconds < 3600
                )
                if force_mode:
                    # 按强制离场处理：0 元、不扣钱、不发积分、不消耗优惠券（和 force_logout 一样）
                    wallet_before = await self.wallet(uid, False, conn)
                    await conn.execute(
                        'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, "billingCost"=0, "finalCost"=0 WHERE id=?',
                        _now(),
                        session_before["id"],
                    )
                    closed = _row(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session_before["id"]))
                    return {
                        "session": closed,
                        "billing": {
                            "totalCost": 0,
                            "startTime": session_before["createdAt"],
                            "endTime": closed.get("closedAt") or _now(),
                            "segments": [],
                            "blocks": [],
                            "points": 0,
                        },
                        "wallet": wallet_before,
                        "walletBefore": wallet_before,
                        "walletAfter": await self.wallet(str(session_before["userId"]), True, conn),
                        "loginGraceForced": True,
                        "loginGraceMinutes": self.login_grace_minutes,
                    }
                preview = await self.billing(uid, conn)
                wallet_before = preview["wallet"]
                session = preview["session"]
                billing = preview["billing"]
                discount = preview.get("discount")
                cost = session.get("costOverwrite")
                if cost is None:
                    cost = discount["finalCost"] if discount else billing["totalCost"]
                cost = int(cost)

                if cost > 0:
                    await self.deduct_wallet(str(session["userId"]), cost, "会话结算: SESSION_SETTLEMENT", conn)
                if discount and discount.get("consumedAssets"):
                    await self.delete_user_assets(str(session["userId"]), discount["consumedAssets"], conn)
                # 游玩积分：封顶段按封顶金额折算、正常段按游玩小时数 1 小时 1 积分（在 billing() 中计算）
                points_earned = int(billing.get("points") or 0)
                if points_earned > 0:
                    await self.add_points(
                        str(session["userId"]), points_earned, "游玩积分: SESSION_POINTS", conn
                    )
                    preview["pointsEarned"] = points_earned
                await conn.execute(
                    'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, "billingCost"=?, "finalCost"=? WHERE id=?',
                    _now(),
                    billing["totalCost"],
                    cost,
                    session["id"],
                )
                closed = _row(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session["id"]))
                preview["session"] = closed
                preview["wallet"] = wallet_before
                preview["walletBefore"] = wallet_before
                preview["walletAfter"] = await self.wallet(str(session["userId"]), True, conn)
                return preview

    @staticmethod
    def _has_pass_for_billing(wallet: dict[str, Any]) -> bool:
        passes = wallet.get("passes", {}).get("details", {}).get("available", []) or []
        return len(passes) > 0

    async def force_logout(self, uid: str) -> dict[str, Any]:
        """管理员强制退场：直接关闭会话，不做结算、不发积分。"""
        async with self._acquire() as conn:
            async with conn.transaction():
                session = await self.active_session(uid, conn)
                if not session:
                    raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
                await conn.execute(
                    'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, "billingCost"=0, "finalCost"=0 WHERE id=?',
                    _now(),
                    session["id"],
                )
                closed = _row(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session["id"]))
                return {"session": closed}

    async def billing(self, uid: str, conn: DBConn | None = None) -> dict[str, Any]:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                return await self.billing(uid, acquired)

        session = await self.active_session(uid, conn)
        if not session:
            raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
        end = session.get("closedAt") or _now()
        calculation_end = end
        played_seconds = max(0, int((end - session["createdAt"]).total_seconds()))
        door_opened = bool(int(session.get("doorOpened") or 0))
        login_grace_seconds = self.login_grace_minutes * 60
        if door_opened and login_grace_seconds < played_seconds < 3600:
            # 成功开门后，超过首小时特殊门槛即按完整 1 小时预览和结算。
            # 顶层 endTime 仍保留真实查询/退场时间，只有计费区间扩展到首小时末。
            calculation_end = session["createdAt"] + timedelta(hours=1)
        wallet = await self.wallet(uid, True, conn)
        monthly_pass = any(
            asset.get("assetDefId") == MONTHLY_PASS_ASSET_ID and asset.get("assetType") == PASS_ASSET_TYPE
            for asset in wallet["passes"].get("details", {}).get("available", [])
        )
        cap24 = amount_to_cents(self.billing_config.get("cap_24h_pass" if monthly_pass else "cap_24h") or 0)
        day_cap = amount_to_cents(self.billing_config.get("day_cap_pass" if monthly_pass else "day_cap") or 69)
        night_cap = amount_to_cents(self.billing_config.get("night_cap_pass" if monthly_pass else "night_cap") or 69)
        # 按 24 小时块逐块封顶：每个从入场时刻起的 24 小时块最多收 cap24
        segments: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        overnight_caps: list[dict[str, Any]] = []
        total_cost = 0
        total_points = 0
        current = session["createdAt"]
        while current < calculation_end:
            block_end = min(current + timedelta(days=1), calculation_end)
            block = self.calculate_billing(
                current,
                block_end,
                monthly_pass,
                session_start=session["createdAt"],
            )
            raw_block_cost = block["totalCost"]
            block_cost = min(raw_block_cost, cap24) if cap24 > 0 else raw_block_cost
            total_cost += block_cost
            overnight_cap = block.get("overnightCap")
            if overnight_cap:
                overnight_cap = dict(overnight_cap)
                overnight_cap["blockIndex"] = len(blocks)
                overnight_caps.append(overnight_cap)
            if cap24 > 0 and block["totalCost"] > cap24:
                # 触发 24 小时封顶：该块积分按封顶金额折算
                total_points += self._cap_points(cap24)
            else:
                if overnight_cap:
                    total_points += self._cap_points(night_cap)
                for seg in block["segments"]:
                    if seg.get("overnightCapCovered"):
                        continue
                    if seg["isCapped"]:
                        # 触发白天/夜晚时段封顶：该段积分按封顶金额折算
                        cap = day_cap if seg["ruleId"] == 1 else night_cap
                        total_points += self._cap_points(cap)
                    else:
                        # 正常消费：按游玩小时数，1 小时 1 积分（不足 1 小时不计）
                        total_points += seg["durationMinutes"] // 60
            if cap24 > 0:
                for seg in block["segments"]:
                    seg["blockIndex"] = len(blocks)
                blocks.append(
                    {
                        "startTime": current,
                        "endTime": block_end,
                        "rawCost": raw_block_cost,
                        "cappedCost": block_cost,
                        "isCapped": raw_block_cost > cap24,
                        "overnightCap": overnight_cap,
                    }
                )
            segments.extend(block["segments"])
            current = block_end
        result = {
            "totalCost": total_cost,
            "startTime": session["createdAt"],
            "endTime": end,
            "segments": segments,
            "blocks": blocks,
            "overnightCaps": overnight_caps,
            "points": total_points,
        }

        response = {"session": session, "billing": result, "wallet": wallet}

        coupon = self._best_coupon(wallet)
        if coupon and coupon["rateBps"] < RATE_SCALE:
            total = response["billing"]["totalCost"]
            if total > 0:
                final_cost = _discounted_cents(total, coupon["rateBps"])
                response["discount"] = {
                    "originalCost": total,
                    "finalCost": final_cost,
                    "consumedAssets": [coupon["id"]],
                    "appliedLogs": [
                        {
                            "asset": coupon["name"],
                            "assetId": coupon["id"],
                            "saved": total - final_cost,
                            "type": "RATE",
                            "breakdown": [],
                        }
                    ],
                }
        return response

    @staticmethod
    def _best_coupon(wallet: dict[str, Any]) -> dict[str, Any] | None:
        """从可用优惠券中选折扣最大（rateBps 最小）的一张。"""
        candidates = [
            asset
            for asset in wallet["tickets"].get("details", {}).get("available", [])
            if asset.get("asset", {}).get("billingEffect", {}).get("type") == "RATE"
        ]
        if not candidates:
            return None
        best = min(
            candidates,
            key=lambda a: (a["asset"]["billingEffect"]["rateBps"], a.get("expireAt") or datetime.max),
        )
        effect = best["asset"]["billingEffect"]
        return {
            "id": best["id"],
            "name": best["asset"].get("name") or "优惠券",
            "rateBps": int(effect["rateBps"]),
        }

    async def history(self, uid: str, limit: int = 5) -> list[dict[str, Any]]:
        async with self._acquire() as conn:
            user = await self.find_user(uid, conn)
            if not user:
                raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
            return _rows(
                await conn.fetch(
                    'SELECT * FROM "Session" WHERE "userId"=? ORDER BY "createdAt" DESC LIMIT ?',
                    user["id"],
                    limit,
                )
            )

    async def active_session(self, uid: str, conn: DBConn) -> dict[str, Any] | None:
        user = await self.find_user(uid, conn)
        if not user:
            return None
        return _row(await conn.fetchrow('SELECT * FROM "Session" WHERE "userId"=? AND "isActive"=1 LIMIT 1', user["id"]))

    async def is_sneak_active(self, uid: str) -> bool:
        """用户当前是否处于偷偷上机会话（ENTRY_TYPE=sneak 且 isActive=1）。"""
        async with self._acquire() as conn:
            session = await self.active_session(uid, conn)
            return bool(session and session.get("ENTRY_TYPE") == "sneak")

    async def logged_in_users(self) -> list[dict[str, Any]]:
        async with self._acquire() as conn:
            users = _rows(
                await conn.fetch(
                    'SELECT DISTINCT u.* FROM "User" u JOIN "Session" s ON s."userId"=u.id WHERE s."isActive"=1 ORDER BY u.id'
                )
            )
            for user in users:
                user["binds"] = _rows(await conn.fetch('SELECT * FROM "Bind" WHERE "userId"=?', user["id"]))
                user["sessions"] = _rows(
                    await conn.fetch('SELECT * FROM "Session" WHERE "userId"=? AND "isActive"=1', user["id"])
                )
            return users

    async def wallet(self, uid: str, details: bool = False, conn: DBConn | None = None) -> dict[str, Any]:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                return await self.wallet(uid, details, acquired)

        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        assets = await self.user_assets(
            uid, True, conn, [CURRENCY_ASSET_TYPE, TICKET_ASSET_TYPE, PASS_ASSET_TYPE, POINTS_ASSET_TYPE]
        )
        paid = [a for a in assets if a["assetDefId"] == PAID_CURRENCY_ASSET_ID and a["assetType"] == CURRENCY_ASSET_TYPE]
        free = [a for a in assets if a["assetDefId"] == FREE_CURRENCY_ASSET_ID and a["assetType"] == CURRENCY_ASSET_TYPE]
        tickets = [a for a in assets if a["assetType"] == TICKET_ASSET_TYPE]
        passes = [a for a in assets if a["assetType"] == PASS_ASSET_TYPE]
        points = [a for a in assets if a["assetType"] == POINTS_ASSET_TYPE]

        def available(asset: dict[str, Any]) -> bool:
            now = _now()
            return (asset.get("activeAt") is None or asset["activeAt"] <= now) and (
                asset.get("expireAt") is None or asset["expireAt"] > now
            )

        free_available = [a for a in free if available(a)]
        ticket_available = [a for a in tickets if available(a)]
        pass_available = [a for a in passes if available(a)]
        paid_amount = sum(int(a["count"]) for a in paid)
        free_available_amount = sum(int(a["count"]) for a in free_available)
        free_total_amount = sum(int(a["count"]) for a in free)
        points_amount = sum(int(a["count"]) for a in points)
        wallet = {
            "total": {"available": paid_amount + free_available_amount, "all": paid_amount + free_total_amount},
            "paid": {"available": paid_amount, "all": paid_amount},
            "free": {"available": free_available_amount, "all": free_total_amount},
            "tickets": {"available": len(ticket_available), "all": len(tickets)},
            "passes": {"available": len(pass_available), "all": len(passes)},
            "points": {"available": points_amount, "all": points_amount},
        }
        if details:
            sort_key = lambda a: a.get("expireAt") or datetime.max
            wallet["paid"]["details"] = {"available": sorted(paid, key=sort_key), "unavailable": []}
            wallet["free"]["details"] = {
                "available": sorted(free_available, key=sort_key),
                "unavailable": [a for a in free if not available(a)],
            }
            wallet["tickets"]["details"] = {
                "available": sorted(ticket_available, key=sort_key),
                "unavailable": [a for a in tickets if not available(a)],
            }
            wallet["passes"]["details"] = {
                "available": sorted(pass_available, key=sort_key),
                "unavailable": [a for a in passes if not available(a)],
            }
            wallet["points"]["details"] = {"available": sorted(points, key=sort_key), "unavailable": []}
        return wallet

    async def user_assets(
        self,
        uid: str,
        with_asset: bool = True,
        conn: DBConn | None = None,
        asset_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                return await self.user_assets(uid, with_asset, acquired, asset_types)

        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        if asset_types:
            placeholders = ",".join("?" for _ in asset_types)
            rows = await conn.fetch(
                f'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetType" IN ({placeholders}) ORDER BY id',
                user["id"],
                *asset_types,
            )
        else:
            rows = await conn.fetch('SELECT * FROM "UserAsset" WHERE "userId"=? ORDER BY id', user["id"])
        result = _rows(rows)
        assets = {asset["id"]: asset for asset in _rows(await conn.fetch('SELECT * FROM "Asset"'))}
        for item in result:
            item["asset"] = dict(assets.get(item.get("assetId")) or {})
            if item["asset"].get("billingEffect"):
                item["asset"]["billingEffect"] = _json(item["asset"]["billingEffect"])
        return result

    async def mahjong_rank(self, uid: str, conn: DBConn | None = None) -> dict[str, Any] | None:
        """读取日麻插件写入同一新宿数据库的段位资料；未联动或未参赛时返回 None。"""
        if conn is None:
            async with self._acquire() as acquired:
                return await self.mahjong_rank(uid, acquired)
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        exists = await conn.fetchval(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='MahjongRank'"
        )
        if not exists:
            return None
        return _row(
            await conn.fetchrow(
                'SELECT * FROM "MahjongRank" WHERE "userId"=? LIMIT 1', user["id"]
            )
        )

    async def add_paid_currency(self, uid: str, amount_cents: int, comment: str = "admin add") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                user = await self.find_user(uid, conn)
                if not user:
                    raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
                amount_cents = int(amount_cents)
                if amount_cents <= 0:
                    raise ShinjukuError("添加金额必须大于 0。", "INVALID_AMOUNT")
                wallet = await self.wallet(uid, False, conn)
                asset = await self.ensure_currency_asset(conn)
                existing = _row(
                    await conn.fetchrow(
                        'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetId"=? AND "assetDefId"=? '
                        'AND "assetType"=? AND "activeAt" IS NULL AND "expireAt" IS NULL LIMIT 1',
                        user["id"],
                        asset["id"],
                        PAID_CURRENCY_ASSET_ID,
                        CURRENCY_ASSET_TYPE,
                    )
                )
                if existing:
                    await conn.execute(
                        'UPDATE "UserAsset" SET count=count+? WHERE id=?',
                        amount_cents,
                        existing["id"],
                    )
                    updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"]))
                    await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
                    changed = updated
                else:
                    created = await conn.execute(
                        'INSERT INTO "UserAsset" ("userId","assetDefId","assetType","assetId",count) '
                        "VALUES (?,?,?,?,?)",
                        user["id"],
                        PAID_CURRENCY_ASSET_ID,
                        CURRENCY_ASSET_TYPE,
                        asset["id"],
                        amount_cents,
                    )
                    changed = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid))
                    await self.log_asset_change(conn, changed, 0, "CREATE", comment)
                final_wallet = await self.wallet(uid, False, conn)
                return {
                    "originalBalance": wallet["total"]["available"],
                    "finalBalance": final_wallet["total"]["available"],
                    "amount": amount_cents,
                    "changedRows": [changed],
                }

    async def add_points(
        self,
        uid: str,
        amount: int,
        comment: str = "游玩积分",
        conn: DBConn | None = None,
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.add_points(uid, amount, comment, acquired)

        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        amount = max(0, int(amount))
        if amount == 0:
            return {"changedRows": [], "amount": 0}
        asset = await self.ensure_points_asset(conn)
        existing = _row(
            await conn.fetchrow(
                'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetId"=? AND "assetDefId"=? '
                'AND "assetType"=? AND "activeAt" IS NULL AND "expireAt" IS NULL LIMIT 1',
                user["id"],
                asset["id"],
                POINTS_ASSET_ID,
                POINTS_ASSET_TYPE,
            )
        )
        if existing:
            await conn.execute('UPDATE "UserAsset" SET count=count+? WHERE id=?', amount, existing["id"])
            updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"]))
            await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
            changed = updated
        else:
            created = await conn.execute(
                'INSERT INTO "UserAsset" ("userId","assetDefId","assetType","assetId",count) VALUES (?,?,?,?,?)',
                user["id"],
                POINTS_ASSET_ID,
                POINTS_ASSET_TYPE,
                asset["id"],
                amount,
            )
            changed = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid))
            await self.log_asset_change(conn, changed, 0, "CREATE", comment)
        return {"changedRows": [changed], "amount": amount}

    async def charge_wallet(self, uid: str, amount_cents: int, comment: str = "admin charge") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                user = await self.find_user(uid, conn)
                if not user:
                    raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
                amount_cents = int(amount_cents)
                if amount_cents <= 0:
                    raise ShinjukuError("扣费金额必须大于 0。", "INVALID_AMOUNT")
                wallet = await self.wallet(uid, False, conn)
                await self.deduct_wallet(uid, amount_cents, comment, conn)
                final_wallet = await self.wallet(uid, False, conn)
                return {
                    "originalBalance": wallet["total"]["available"],
                    "finalBalance": final_wallet["total"]["available"],
                    "amount": amount_cents,
                }

    async def add_pass(self, uid: str, days: int = 30, comment: str = "admin member") -> dict[str, Any]:
        """为用户发放通行证，与已有通行证自动续期叠加。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                user = await self.find_user(uid, conn)
                if not user:
                    raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
                if int(days) <= 0:
                    raise ShinjukuError("通行证天数必须大于 0。", "INVALID_DAYS")
                asset = await self.ensure_pass_asset(conn)
                duration_ms = int(days) * 86400000
                return await self.add_asset_by_def(
                    user["id"],
                    asset,
                    1,
                    comment,
                    conn,
                    {"durationMs": duration_ms, "mergeStrategy": "EXTEND_TIME"},
                )

    async def _ensure_asset_definition(
        self,
        conn: DBConn,
        asset_id: int,
        asset_type: str,
        name: str,
        description: str,
        billing_effect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await conn.execute(
            'INSERT OR IGNORE INTO "Asset" '
            '("assetId",type,name,description,"billingEffect",valid) VALUES (?,?,?,?,?,1)',
            asset_id,
            asset_type,
            name,
            description,
            json.dumps(billing_effect) if billing_effect is not None else None,
        )
        asset = _row(
            await conn.fetchrow(
                'SELECT * FROM "Asset" WHERE type=? AND "assetId"=? LIMIT 1',
                asset_type,
                asset_id,
            )
        )
        if not asset:
            raise ShinjukuError("资产定义创建失败。", "ASSET_DEFINITION_CREATE_FAILED")
        return asset

    async def ensure_currency_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self._ensure_asset_definition(
            conn,
            PAID_CURRENCY_ASSET_ID,
            CURRENCY_ASSET_TYPE,
            self.currency,
            "AstrBot 插件自动创建的付费货币资产定义",
        )

    async def ensure_points_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self._ensure_asset_definition(
            conn,
            POINTS_ASSET_ID,
            POINTS_ASSET_TYPE,
            "积分",
            "新宿插件自动创建的积分资产定义",
        )

    async def ensure_pass_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self._ensure_asset_definition(
            conn,
            MONTHLY_PASS_ASSET_ID,
            PASS_ASSET_TYPE,
            "通行证",
            "新宿插件自动创建的通行证资产定义",
        )

    async def grant_coupon(self, uid: str, discount_tenths: Any, days: int = 30, comment: str = "admin coupon") -> dict[str, Any]:
        """发放优惠券：discount_tenths 为 0-10 折（8 表示 8 折，付 80%），默认有效期 30 天。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                user = await self.find_user(uid, conn)
                if not user:
                    raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
                rate_bps = _discount_tenths_to_bps(discount_tenths)
                if int(days) <= 0:
                    raise ShinjukuError("优惠券有效天数必须大于 0。", "INVALID_DAYS")
                asset = await self.ensure_coupon_asset(conn, rate_bps)
                duration_ms = int(days) * 86400000
                user_asset = await self.add_asset_by_def(
                    user["id"],
                    asset,
                    1,
                    comment,
                    conn,
                    {"durationMs": duration_ms, "mergeStrategy": "EXTEND_TIME"},
                )
                return {
                    "user": user,
                    "asset": asset,
                    "userAsset": user_asset,
                    "discount_tenths": _discount_tenths_text(rate_bps),
                    "days": int(days),
                }

    async def ensure_coupon_asset(self, conn: DBConn, rate_bps: int) -> dict[str, Any]:
        rate_bps = int(rate_bps)
        def_id = 200000 + rate_bps
        tenths_text = _discount_tenths_text(rate_bps)
        name = "免费券" if rate_bps == 0 else f"{tenths_text}折优惠券"
        return await self._ensure_asset_definition(
            conn,
            def_id,
            TICKET_ASSET_TYPE,
            name,
            f"管理员发放的{name}",
            {
                "type": "RATE",
                "rateBps": rate_bps,
                "priority": 50,
                "stackable": False,
                "consume": True,
                "condition": {},
            },
        )

    async def deduct_wallet(self, uid: str, amount_cents: int, comment: str, conn: DBConn) -> None:
        wallet = await self.wallet(uid, True, conn)
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        remaining = int(amount_cents)
        candidates = [
            asset
            for asset in wallet["free"]["details"]["available"] + wallet["paid"]["details"]["available"]
            if int(asset["count"]) > 0
        ]
        for asset in candidates:
            if remaining <= 0:
                break
            deduct = min(int(asset["count"]), remaining)
            new_count = int(asset["count"]) - deduct
            if new_count <= 0:
                await conn.execute('DELETE FROM "UserAsset" WHERE id=?', asset["id"])
                changed = dict(asset)
                changed["count"] = 0
                await self.log_asset_change(conn, changed, asset["count"], "DELETE", comment)
            else:
                await conn.execute('UPDATE "UserAsset" SET count=? WHERE id=?', new_count, asset["id"])
                updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', asset["id"]))
                await self.log_asset_change(conn, updated, asset["count"], "UPDATE", comment)
            remaining -= deduct
        if remaining > 0:
            # 余额不足：差额记为负数余额（欠费），挂在付费货币上，充值会自动抵扣
            currency_asset = await self.ensure_currency_asset(conn)
            existing = _row(
                await conn.fetchrow(
                    'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetId"=? AND "assetDefId"=? AND "assetType"=? '
                    'AND "activeAt" IS NULL AND "expireAt" IS NULL LIMIT 1',
                    user["id"],
                    currency_asset["id"],
                    PAID_CURRENCY_ASSET_ID,
                    CURRENCY_ASSET_TYPE,
                )
            )
            if existing:
                new_count = int(existing["count"]) - remaining
                await conn.execute('UPDATE "UserAsset" SET count=? WHERE id=?', new_count, existing["id"])
                updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"]))
                await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
            else:
                created = await conn.execute(
                    'INSERT INTO "UserAsset" ("userId","assetDefId","assetType","assetId",count) VALUES (?,?,?,?,?)',
                    user["id"],
                    PAID_CURRENCY_ASSET_ID,
                    CURRENCY_ASSET_TYPE,
                    currency_asset["id"],
                    -remaining,
                )
                created = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid))
                await self.log_asset_change(conn, created, 0, "CREATE", comment)

    async def delete_user_assets(self, uid: str, ids: list[int], conn: DBConn) -> None:
        if not ids:
            return
        assets = [asset for asset in await self.user_assets(uid, False, conn) if asset["id"] in ids]
        for asset in assets:
            await conn.execute('DELETE FROM "UserAsset" WHERE id=?', asset["id"])
            changed = dict(asset)
            changed["count"] = 0
            await self.log_asset_change(conn, changed, asset["count"], "DELETE", "deleteUserAssets Function")

    async def log_asset_change(
        self,
        conn: DBConn,
        asset: dict[str, Any],
        original_count: int,
        action: str,
        comment: str,
    ) -> None:
        await conn.execute(
            'INSERT INTO "UserAssetLog" ("userId","userAssetId","assetId","assetType","changeAmount","countBefore","countAfter","expireAtBefore","expireAtAfter",action,comment) '
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            asset["userId"],
            asset.get("id"),
            asset["assetDefId"],
            asset["assetType"],
            int(asset["count"]) - int(original_count),
            int(original_count),
            int(asset["count"]),
            None,
            asset.get("expireAt"),
            action,
            comment,
        )

    async def upsert_present_by_id(self, uid: str | int, present_id: int, conn: DBConn | None = None) -> Any:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.upsert_present_by_id(uid, present_id, acquired)
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        present = _row(await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', present_id))
        if not present:
            raise ShinjukuError("注册礼包不存在。", "ASSET_NOT_FOUND")
        return await self._upsert_present(conn, user, present, f"present:{present_id}")

    async def redeem(self, uid: str | int, code: str, conn: DBConn | None = None) -> dict[str, Any]:
        owns_conn = conn is None
        if owns_conn:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.redeem(uid, code, acquired)

        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        redeem = _row(await conn.fetchrow('SELECT * FROM "Redeem" WHERE code=?', code))
        if not redeem:
            raise ShinjukuError("兑换码不存在或已使用。", "REDEEM_CODE_NOT_FOUND_OR_USED")
        present_row = _row(await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', redeem["presentId"]))
        if not present_row:
            raise ShinjukuError("兑换码对应的礼包不存在。", "ASSET_NOT_FOUND")
        now = _now()
        active_at = _as_dt(redeem.get("activeAt"))
        expire_at = _as_dt(redeem.get("expireAt"))
        if active_at and active_at > now:
            raise ShinjukuError("兑换码尚未生效。", "REDEEM_NOT_ACTIVE")
        if expire_at and expire_at < now:
            raise ShinjukuError("兑换码已过期。", "REDEEM_EXPIRED")
        used_count = int(
            await conn.fetchval('SELECT count(*) FROM "RedeemRecord" WHERE "redeemId"=?', redeem["id"]) or 0
        )
        if used_count >= int(redeem.get("maxUseCount") or 1):
            raise ShinjukuError("兑换码已达到最大使用次数。", "REDEEM_CODE_LIMIT_EXCEEDED")
        present = dict(present_row)
        present["body"] = _json(present_row.get("body"))
        assets = await self._upsert_present(conn, user, present, f"redeem:{code}")
        await conn.execute(
            'INSERT INTO "RedeemRecord" ("userId","redeemId","presentId") VALUES (?,?,?)',
            user["id"],
            redeem["id"],
            redeem["presentId"],
        )
        return {"present": present, "assets": assets}

    async def create_gift_code(self, present_id: int, currency_amount_cents: int, max_use_count: int) -> dict[str, Any]:
        """基于现有礼包生成兑换码：货币数量按参数覆盖，每人限领一次，总数封顶 max_use_count。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                present = _row(await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', present_id))
                if not present:
                    raise ShinjukuError("礼包不存在。", "ASSET_NOT_FOUND")
                amount_cents = int(currency_amount_cents)
                if amount_cents <= 0:
                    raise ShinjukuError("货币数量必须大于 0。", "INVALID_AMOUNT")
                uses = int(max_use_count)
                if uses <= 0:
                    raise ShinjukuError("兑换次数必须大于 0。", "INVALID_USE_COUNT")
                await self.ensure_currency_asset(conn)
                body = _json(present.get("body")) or []
                new_body = [dict(item) for item in body]
                currency_item = next(
                    (
                        item
                        for item in new_body
                        if str(item.get("assetType")) == CURRENCY_ASSET_TYPE
                        and int(item.get("assetId") or 0) == PAID_CURRENCY_ASSET_ID
                    ),
                    None,
                )
                if currency_item:
                    currency_item["count"] = amount_cents
                else:
                    new_body.append(
                        {
                            "assetType": CURRENCY_ASSET_TYPE,
                            "assetId": PAID_CURRENCY_ASSET_ID,
                            "count": amount_cents,
                        }
                    )
                name = f"{present.get('name') or '礼包'}·兑换码"
                created = await conn.execute(
                    'INSERT INTO "Present" (name, "oncePerUser", body) VALUES (?,1,?)',
                    name,
                    json.dumps(new_body),
                )
                code = await self._generate_code(conn)
                await conn.execute(
                    'INSERT INTO "Redeem" (code, "presentId", "maxUseCount") VALUES (?,?,?)',
                    code,
                    created.lastrowid,
                    uses,
                )
                return {
                    "code": code,
                    "name": name,
                    "currency_amount": amount_cents,
                    "max_use_count": uses,
                }

    @staticmethod
    async def _generate_code(conn: DBConn) -> str:
        while True:
            code = secrets.token_hex(4).upper()
            exists = await conn.fetchval('SELECT 1 FROM "Redeem" WHERE code=?', code)
            if not exists:
                return code

    async def _upsert_present(
        self,
        conn: DBConn,
        user: dict[str, Any],
        present: dict[str, Any],
        comment_prefix: str,
    ) -> list[dict[str, Any]]:
        user_redeem_count = int(
            await conn.fetchval(
                'SELECT count(*) FROM "RedeemRecord" WHERE "userId"=? AND "presentId"=?',
                user["id"],
                present["id"],
            )
            or 0
        )
        if present.get("oncePerUser") and user_redeem_count >= 1:
            raise ShinjukuError("该礼物一个账号只能兑换一次。", "REDEEM_GIFT_ONCE_PER_USER")
        body = _json(present["body"]) or []
        changes = []
        for item in body:
            if item.get("oncePerUser") and user_redeem_count > 0:
                continue
            amount = int(item["count"]) if "count" in item else 1
            if item.get("id"):
                asset = _row(await conn.fetchrow('SELECT * FROM "Asset" WHERE id=?', int(item["id"])))
            else:
                asset = _row(
                    await conn.fetchrow(
                        'SELECT * FROM "Asset" WHERE "assetId"=? AND type=?',
                        int(item["assetId"]),
                        str(item["assetType"]),
                    )
                )
                if not asset:
                    asset = await self._ensure_standard_asset(
                        conn, int(item.get("assetId") or 0), str(item.get("assetType") or "")
                    )
            if not asset:
                continue
            changes.append(
                await self.add_asset_by_def(
                    user["id"],
                    asset,
                    amount,
                    item.get("comment") or comment_prefix,
                    conn,
                    item,
                )
            )
        return changes

    async def _ensure_standard_asset(self, conn: DBConn, asset_id: int, asset_type: str) -> dict[str, Any] | None:
        """为已知的标准资产（付费/免费货币、通行证）补建资产定义，避免礼包发放时被跳过。"""
        if asset_type == CURRENCY_ASSET_TYPE and asset_id == PAID_CURRENCY_ASSET_ID:
            return await self.ensure_currency_asset(conn)
        if asset_type == CURRENCY_ASSET_TYPE and asset_id == FREE_CURRENCY_ASSET_ID:
            return await self._ensure_asset_definition(
                conn,
                FREE_CURRENCY_ASSET_ID,
                CURRENCY_ASSET_TYPE,
                f"{self.currency}（免费）",
                "新宿插件自动创建的免费货币资产定义",
            )
        if asset_type == PASS_ASSET_TYPE and asset_id == MONTHLY_PASS_ASSET_ID:
            return await self.ensure_pass_asset(conn)
        return None

    async def add_asset_by_def(
        self,
        uid: str | int,
        asset: dict[str, Any],
        amount: int,
        comment: str,
        conn: DBConn,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        options = options or {}
        amount = int(amount)
        active_at = _as_dt(options.get("activeAt"))
        expire_at = _as_dt(options.get("expireAt")) or _future_dt(options.get("durationMs"))
        merge_strategy = str(options.get("mergeStrategy") or "STACK").upper()
        if merge_strategy == "EXTEND_TIME":
            duration_expire_at = _future_dt(options.get("durationMs"))
            if not duration_expire_at:
                raise ShinjukuError("EXTEND_TIME 策略必须提供正数 durationMs。", "INVALID_DURATION_MS")
            existing = _row(
                await conn.fetchrow(
                    'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetId"=? AND "assetDefId"=? AND "assetType"=? '
                    'ORDER BY "expireAt" DESC LIMIT 1',
                    user["id"],
                    asset["id"],
                    asset["assetId"],
                    asset["type"],
                )
            )
            if existing and existing["count"] > 0:
                base_time = existing.get("expireAt") if existing.get("expireAt") and existing["expireAt"] > _now() else _now()
                duration_ms = float(options["durationMs"])
                new_expire_at = base_time + timedelta(milliseconds=duration_ms)
                await conn.execute(
                    'UPDATE "UserAsset" SET count=1, "activeAt"=COALESCE("activeAt", ?), "expireAt"=? WHERE id=?',
                    _now(),
                    new_expire_at,
                    existing["id"],
                )
                updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"]))
                await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
                return updated
            active_at = active_at or _now()
            expire_at = duration_expire_at
        existing = _row(
            await conn.fetchrow(
                'SELECT * FROM "UserAsset" WHERE "userId"=? AND "assetId"=? AND "assetDefId"=? AND "assetType"=? '
                'AND "activeAt" IS ? AND "expireAt" IS ? LIMIT 1',
                user["id"],
                asset["id"],
                asset["assetId"],
                asset["type"],
                active_at,
                expire_at,
            )
        )
        if existing:
            await conn.execute('UPDATE "UserAsset" SET count=count+? WHERE id=?', amount, existing["id"])
            updated = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"]))
            await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
            return updated
        created = await conn.execute(
            'INSERT INTO "UserAsset" ("userId","assetDefId","assetType","assetId",count,"activeAt","expireAt") '
            "VALUES (?,?,?,?,?,?,?)",
            user["id"],
            asset["assetId"],
            asset["type"],
            asset["id"],
            amount,
            active_at,
            expire_at,
        )
        created = _row(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid))
        await self.log_asset_change(conn, created, 0, "CREATE", comment)
        return created
