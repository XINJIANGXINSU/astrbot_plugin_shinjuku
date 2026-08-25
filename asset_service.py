"""Asset definitions, user assets, and asset change logging."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

try:
    from .constants import (
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        MONTHLY_PASS_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_ID,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from .errors import ShinjukuError
    from .money import discount_tenths_text
    from .storage import DBConn, parse_datetime, row_to_dict, rows_to_dicts
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from constants import (
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        MONTHLY_PASS_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_ID,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from errors import ShinjukuError
    from money import discount_tenths_text
    from storage import DBConn, parse_datetime, row_to_dict, rows_to_dicts


FindUser = Callable[[str | int, DBConn], Awaitable[dict[str, Any] | None]]
Clock = Callable[[], datetime]


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


class AssetService:
    """Owns asset persistence rules without owning connections or transactions."""

    def __init__(self, currency: str, find_user: FindUser, now: Clock = datetime.now):
        self.currency = currency
        self.find_user = find_user
        self.now = now

    async def user_assets(
        self,
        uid: str | int,
        conn: DBConn,
        with_asset: bool = True,
        asset_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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
        result = rows_to_dicts(rows)
        # Keep the existing public behavior: callers historically received the
        # embedded definition even when passing with_asset=False.
        _ = with_asset
        assets = {asset["id"]: asset for asset in rows_to_dicts(await conn.fetch('SELECT * FROM "Asset"'))}
        for item in result:
            item["asset"] = dict(assets.get(item.get("assetId")) or {})
            if item["asset"].get("billingEffect"):
                item["asset"]["billingEffect"] = _json(item["asset"]["billingEffect"])
        return result

    async def ensure_asset_definition(
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
        asset = row_to_dict(
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
        return await self.ensure_asset_definition(
            conn,
            PAID_CURRENCY_ASSET_ID,
            CURRENCY_ASSET_TYPE,
            self.currency,
            "AstrBot 插件自动创建的付费货币资产定义",
        )

    async def ensure_points_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self.ensure_asset_definition(
            conn,
            POINTS_ASSET_ID,
            POINTS_ASSET_TYPE,
            "积分",
            "新宿插件自动创建的积分资产定义",
        )

    async def ensure_pass_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self.ensure_asset_definition(
            conn,
            MONTHLY_PASS_ASSET_ID,
            PASS_ASSET_TYPE,
            "通行证",
            "新宿插件自动创建的通行证资产定义",
        )

    async def ensure_coupon_asset(self, conn: DBConn, rate_bps: int) -> dict[str, Any]:
        rate_bps = int(rate_bps)
        def_id = 200000 + rate_bps
        tenths_text = discount_tenths_text(rate_bps)
        name = "免费券" if rate_bps == 0 else f"{tenths_text}折优惠券"
        return await self.ensure_asset_definition(
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

    async def ensure_standard_asset(
        self,
        conn: DBConn,
        asset_id: int,
        asset_type: str,
    ) -> dict[str, Any] | None:
        """Create definitions for standard assets referenced by presents."""
        if asset_type == CURRENCY_ASSET_TYPE and asset_id == PAID_CURRENCY_ASSET_ID:
            return await self.ensure_currency_asset(conn)
        if asset_type == CURRENCY_ASSET_TYPE and asset_id == FREE_CURRENCY_ASSET_ID:
            return await self.ensure_asset_definition(
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
        now = self.now()
        active_at = parse_datetime(options.get("activeAt"))
        expire_at = parse_datetime(options.get("expireAt"))
        duration_value = options.get("durationMs")
        duration_ms = float(duration_value or 0) if not expire_at else 0.0
        if not expire_at and duration_ms > 0:
            expire_at = now + timedelta(milliseconds=duration_ms)
        merge_strategy = str(options.get("mergeStrategy") or "STACK").upper()
        if merge_strategy == "EXTEND_TIME":
            duration_ms = float(duration_value or 0)
            if duration_ms <= 0:
                raise ShinjukuError("EXTEND_TIME 策略必须提供正数 durationMs。", "INVALID_DURATION_MS")
            duration_expire_at = now + timedelta(milliseconds=duration_ms)
            existing = row_to_dict(
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
                base_time = (
                    existing["expireAt"]
                    if existing.get("expireAt") and existing["expireAt"] > now
                    else now
                )
                new_expire_at = base_time + timedelta(milliseconds=duration_ms)
                await conn.execute(
                    'UPDATE "UserAsset" SET count=1, "activeAt"=COALESCE("activeAt", ?), "expireAt"=? WHERE id=?',
                    now,
                    new_expire_at,
                    existing["id"],
                )
                updated = row_to_dict(
                    await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"])
                )
                await self.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
                return updated
            active_at = active_at or now
            expire_at = duration_expire_at
        existing = row_to_dict(
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
            updated = row_to_dict(
                await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"])
            )
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
        created_row = row_to_dict(
            await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid)
        )
        await self.log_asset_change(conn, created_row, 0, "CREATE", comment)
        return created_row

    async def delete_user_assets(self, uid: str | int, ids: list[int], conn: DBConn) -> None:
        if not ids:
            return
        assets = [asset for asset in await self.user_assets(uid, conn, False) if asset["id"] in ids]
        for asset in assets:
            await conn.execute('DELETE FROM "UserAsset" WHERE id=?', asset["id"])
            changed = dict(asset)
            changed["count"] = 0
            await self.log_asset_change(
                conn,
                changed,
                asset["count"],
                "DELETE",
                "deleteUserAssets Function",
            )

    @staticmethod
    async def log_asset_change(
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
