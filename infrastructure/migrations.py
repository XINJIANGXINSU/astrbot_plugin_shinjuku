"""Database initialization and one-time schema migrations."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

try:
    from ..core.constants import (
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
        CURRENCY_ASSET_TYPE,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        MONEY_MIGRATION_KEY,
        TICKET_ASSET_TYPE,
    )
    from ..core.errors import ShinjukuError
    from ..core.money import MONEY_SCALE, RATE_SCALE, amount_to_cents
    from .schema import SCHEMA_SQL
    from .storage import DBConn
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from core.constants import (
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
        CURRENCY_ASSET_TYPE,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        MONEY_MIGRATION_KEY,
        TICKET_ASSET_TYPE,
    )
    from core.errors import ShinjukuError
    from core.money import MONEY_SCALE, RATE_SCALE, amount_to_cents
    from infrastructure.schema import SCHEMA_SQL
    from infrastructure.storage import DBConn


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


class DatabaseMigrator:
    def __init__(self, now: Callable[[], datetime] = datetime.now):
        self.now = now

    async def initialize(self, conn: DBConn) -> None:
        await conn._conn.executescript(SCHEMA_SQL)
        await self._ensure_legacy_session_columns(conn)
        await self._migrate_money_to_integer_cents(conn)
        await self._ensure_identity_session_constraints(conn)
        await self._ensure_asset_redeem_constraints(conn)

    async def _ensure_legacy_session_columns(self, conn: DBConn) -> None:
        raw = conn._conn
        columns = {
            row[1]
            for row in await (
                await raw.execute('PRAGMA table_info("Session")')
            ).fetchall()
        }
        additions = {
            "CHECKCODE": 'ALTER TABLE "Session" ADD COLUMN "CHECKCODE" TEXT',
            "doorOpened": (
                'ALTER TABLE "Session" ADD COLUMN "doorOpened" '
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "ENTRY_TYPE": (
                'ALTER TABLE "Session" ADD COLUMN "ENTRY_TYPE" '
                "TEXT NOT NULL DEFAULT 'normal'"
            ),
            "closeReason": 'ALTER TABLE "Session" ADD COLUMN "closeReason" TEXT',
        }
        for column, statement in additions.items():
            if column not in columns:
                await raw.execute(statement)
        await raw.execute(
            'CREATE INDEX IF NOT EXISTS idx_session_checkcode ON "Session"("CHECKCODE")'
        )
        await raw.execute(
            'CREATE INDEX IF NOT EXISTS idx_session_user_history '
            'ON "Session"("userId", "createdAt" DESC)'
        )
        await raw.commit()

    async def _is_applied(self, conn: DBConn, key: str) -> bool:
        return bool(
            await conn.fetchval(
                'SELECT 1 FROM "SchemaMigration" WHERE key=?', key
            )
        )

    async def _mark_applied(self, conn: DBConn, key: str) -> None:
        await conn._conn.execute(
            'INSERT INTO "SchemaMigration" (key,"appliedAt") VALUES (?,?)',
            (key, self.now().isoformat()),
        )

    async def _ensure_asset_redeem_constraints(self, conn: DBConn) -> None:
        if await self._is_applied(conn, ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY):
            return
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
                    'SELECT code,count(*) FROM "Redeem" '
                    'GROUP BY code HAVING count(*)>1 LIMIT 1'
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
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_type_asset_id '
                'ON "Asset"(type,"assetId")'
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
            await self._mark_applied(conn, ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY)
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise

    async def _ensure_identity_session_constraints(self, conn: DBConn) -> None:
        if await self._is_applied(conn, IDENTITY_CONSTRAINTS_MIGRATION_KEY):
            return
        raw = conn._conn
        await raw.execute("BEGIN IMMEDIATE")
        try:
            duplicate_bind = await (
                await raw.execute(
                    'SELECT type,bid,count(*) FROM "Bind" '
                    'GROUP BY type,bid HAVING count(*)>1 LIMIT 1'
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
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_session_active_user '
                'ON "Session"("userId") WHERE "isActive"=1'
            )
            await raw.execute(
                '''CREATE TRIGGER IF NOT EXISTS trg_active_session_checkcode_immutable
                   BEFORE UPDATE OF "CHECKCODE" ON "Session"
                   WHEN OLD."isActive"=1 AND OLD."CHECKCODE" IS NOT NEW."CHECKCODE"
                   BEGIN
                       SELECT RAISE(ABORT, 'active session checkcode is immutable');
                   END'''
            )
            await self._mark_applied(conn, IDENTITY_CONSTRAINTS_MIGRATION_KEY)
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise

    async def _migrate_money_to_integer_cents(self, conn: DBConn) -> None:
        if await self._is_applied(conn, MONEY_MIGRATION_KEY):
            return
        raw = conn._conn
        await raw.execute("BEGIN IMMEDIATE")
        try:
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
            await raw.execute(
                'CREATE INDEX idx_userasset_user ON "UserAsset"("userId", "assetType")'
            )

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
                row[1]
                for row in await (
                    await raw.execute('PRAGMA table_info("Session")')
                ).fetchall()
            }
            if "costOverwrite" in session_columns:
                await raw.execute(
                    'UPDATE "Session" SET "costOverwrite"='
                    'CAST(ROUND("costOverwrite" * ?) AS INTEGER) '
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

            present_rows = await (
                await raw.execute(
                    'SELECT id, body FROM "Present" WHERE body IS NOT NULL'
                )
            ).fetchall()
            for present_id, body_text in present_rows:
                body = _json_value(body_text) or []
                changed = False
                for item in body:
                    if (
                        str(item.get("assetType")) == CURRENCY_ASSET_TYPE
                        and "count" in item
                    ):
                        item["count"] = amount_to_cents(item["count"])
                        changed = True
                if changed:
                    await raw.execute(
                        'UPDATE "Present" SET body=? WHERE id=?',
                        (json.dumps(body, ensure_ascii=False), present_id),
                    )

            effect_rows = await (
                await raw.execute(
                    'SELECT id, "billingEffect" FROM "Asset" '
                    'WHERE type=? AND "billingEffect" IS NOT NULL',
                    (TICKET_ASSET_TYPE,),
                )
            ).fetchall()
            for asset_id, effect_text in effect_rows:
                effect = _json_value(effect_text) or {}
                if effect.get("type") != "RATE" or "rateBps" in effect:
                    continue
                rate = Decimal(str(effect.pop("value", 1)))
                effect["rateBps"] = int(
                    (rate * RATE_SCALE).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                await raw.execute(
                    'UPDATE "Asset" SET "billingEffect"=? WHERE id=?',
                    (json.dumps(effect, ensure_ascii=False), asset_id),
                )

            await self._mark_applied(conn, MONEY_MIGRATION_KEY)
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise
