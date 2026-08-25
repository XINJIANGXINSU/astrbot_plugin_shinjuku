"""Wallet aggregation and balance-changing operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable

try:
    from .asset_service import AssetService
    from .constants import (
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from .errors import ShinjukuError
    from .money import discount_tenths_text, discount_tenths_to_bps
    from .storage import DBConn, row_to_dict
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from asset_service import AssetService
    from constants import (
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from errors import ShinjukuError
    from money import discount_tenths_text, discount_tenths_to_bps
    from storage import DBConn, row_to_dict


FindUser = Callable[[str | int, DBConn], Awaitable[dict[str, Any] | None]]
Clock = Callable[[], datetime]


class WalletService:
    """Owns wallet rules while callers retain connection and transaction control."""

    def __init__(self, assets: AssetService, find_user: FindUser, now: Clock = datetime.now):
        self.assets = assets
        self.find_user = find_user
        self.now = now

    async def wallet(self, uid: str | int, conn: DBConn, details: bool = False) -> dict[str, Any]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        assets = await self.assets.user_assets(
            uid,
            conn,
            True,
            [CURRENCY_ASSET_TYPE, TICKET_ASSET_TYPE, PASS_ASSET_TYPE, POINTS_ASSET_TYPE],
        )
        paid = [
            asset
            for asset in assets
            if asset["assetDefId"] == PAID_CURRENCY_ASSET_ID
            and asset["assetType"] == CURRENCY_ASSET_TYPE
        ]
        free = [
            asset
            for asset in assets
            if asset["assetDefId"] == FREE_CURRENCY_ASSET_ID
            and asset["assetType"] == CURRENCY_ASSET_TYPE
        ]
        tickets = [a for a in assets if a["assetType"] == TICKET_ASSET_TYPE]
        passes = [a for a in assets if a["assetType"] == PASS_ASSET_TYPE]
        points = [a for a in assets if a["assetType"] == POINTS_ASSET_TYPE]
        now = self.now()

        def available(asset: dict[str, Any]) -> bool:
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
        result = {
            "total": {"available": paid_amount + free_available_amount, "all": paid_amount + free_total_amount},
            "paid": {"available": paid_amount, "all": paid_amount},
            "free": {"available": free_available_amount, "all": free_total_amount},
            "tickets": {"available": len(ticket_available), "all": len(tickets)},
            "passes": {"available": len(pass_available), "all": len(passes)},
            "points": {"available": points_amount, "all": points_amount},
        }
        if details:
            sort_key = lambda item: item.get("expireAt") or datetime.max
            result["paid"]["details"] = {"available": sorted(paid, key=sort_key), "unavailable": []}
            result["free"]["details"] = {
                "available": sorted(free_available, key=sort_key),
                "unavailable": [a for a in free if not available(a)],
            }
            result["tickets"]["details"] = {
                "available": sorted(ticket_available, key=sort_key),
                "unavailable": [a for a in tickets if not available(a)],
            }
            result["passes"]["details"] = {
                "available": sorted(pass_available, key=sort_key),
                "unavailable": [a for a in passes if not available(a)],
            }
            result["points"]["details"] = {"available": sorted(points, key=sort_key), "unavailable": []}
        return result

    async def add_paid_currency(
        self,
        uid: str | int,
        amount_cents: int,
        comment: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        if not await self.find_user(uid, conn):
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise ShinjukuError("添加金额必须大于 0。", "INVALID_AMOUNT")
        original = await self.wallet(uid, conn)
        asset = await self.assets.ensure_currency_asset(conn)
        changed = await self.assets.add_asset_by_def(uid, asset, amount_cents, comment, conn)
        final = await self.wallet(uid, conn)
        return {
            "originalBalance": original["total"]["available"],
            "finalBalance": final["total"]["available"],
            "amount": amount_cents,
            "changedRows": [changed],
        }

    async def add_points(
        self,
        uid: str | int,
        amount: int,
        comment: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        if not await self.find_user(uid, conn):
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        amount = max(0, int(amount))
        if amount == 0:
            return {"changedRows": [], "amount": 0}
        asset = await self.assets.ensure_points_asset(conn)
        changed = await self.assets.add_asset_by_def(uid, asset, amount, comment, conn)
        return {"changedRows": [changed], "amount": amount}

    async def charge_wallet(
        self,
        uid: str | int,
        amount_cents: int,
        comment: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        if not await self.find_user(uid, conn):
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise ShinjukuError("扣费金额必须大于 0。", "INVALID_AMOUNT")
        original = await self.wallet(uid, conn)
        await self.deduct_wallet(uid, amount_cents, comment, conn)
        final = await self.wallet(uid, conn)
        return {
            "originalBalance": original["total"]["available"],
            "finalBalance": final["total"]["available"],
            "amount": amount_cents,
        }

    async def add_pass(
        self,
        uid: str | int,
        days: int,
        comment: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        if int(days) <= 0:
            raise ShinjukuError("通行证天数必须大于 0。", "INVALID_DAYS")
        asset = await self.assets.ensure_pass_asset(conn)
        return await self.assets.add_asset_by_def(
            user["id"],
            asset,
            1,
            comment,
            conn,
            {"durationMs": int(days) * 86400000, "mergeStrategy": "EXTEND_TIME"},
        )

    async def grant_coupon(
        self,
        uid: str | int,
        discount_tenths: Any,
        days: int,
        comment: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        rate_bps = discount_tenths_to_bps(discount_tenths)
        if int(days) <= 0:
            raise ShinjukuError("优惠券有效天数必须大于 0。", "INVALID_DAYS")
        asset = await self.assets.ensure_coupon_asset(conn, rate_bps)
        user_asset = await self.assets.add_asset_by_def(
            user["id"],
            asset,
            1,
            comment,
            conn,
            {"durationMs": int(days) * 86400000, "mergeStrategy": "EXTEND_TIME"},
        )
        return {
            "user": user,
            "asset": asset,
            "userAsset": user_asset,
            "discount_tenths": discount_tenths_text(rate_bps),
            "days": int(days),
        }

    async def deduct_wallet(
        self,
        uid: str | int,
        amount_cents: int,
        comment: str,
        conn: DBConn,
    ) -> None:
        wallet = await self.wallet(uid, conn, True)
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
                await self.assets.log_asset_change(conn, changed, asset["count"], "DELETE", comment)
            else:
                await conn.execute('UPDATE "UserAsset" SET count=? WHERE id=?', new_count, asset["id"])
                updated = row_to_dict(await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', asset["id"]))
                await self.assets.log_asset_change(conn, updated, asset["count"], "UPDATE", comment)
            remaining -= deduct
        if remaining <= 0:
            return
        currency_asset = await self.assets.ensure_currency_asset(conn)
        existing = row_to_dict(
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
            updated = row_to_dict(
                await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', existing["id"])
            )
            await self.assets.log_asset_change(conn, updated, existing["count"], "UPDATE", comment)
            return
        created = await conn.execute(
            'INSERT INTO "UserAsset" ("userId","assetDefId","assetType","assetId",count) VALUES (?,?,?,?,?)',
            user["id"],
            PAID_CURRENCY_ASSET_ID,
            CURRENCY_ASSET_TYPE,
            currency_asset["id"],
            -remaining,
        )
        created_row = row_to_dict(
            await conn.fetchrow('SELECT * FROM "UserAsset" WHERE id=?', created.lastrowid)
        )
        await self.assets.log_asset_change(conn, created_row, 0, "CREATE", comment)
