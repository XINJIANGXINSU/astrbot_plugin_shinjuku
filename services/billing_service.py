"""Billing previews and transactional session settlement rules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

try:
    from .asset_service import AssetService
    from ..core.billing_engine import BillingEngine
    from ..core.constants import (
        MONTHLY_PASS_ASSET_ID,
        PASS_ASSET_TYPE,
        SESSION_CLOSE_GRACE_CANCELLED,
        SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
        SESSION_CLOSE_SETTLED,
    )
    from ..core.errors import ShinjukuError
    from ..core.money import MONEY_SCALE, RATE_SCALE, amount_to_cents, discounted_cents
    from ..infrastructure.storage import DBConn, row_to_dict
    from .session_service import SessionService
    from .wallet_service import WalletService
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from services.asset_service import AssetService
    from core.billing_engine import BillingEngine
    from core.constants import (
        MONTHLY_PASS_ASSET_ID,
        PASS_ASSET_TYPE,
        SESSION_CLOSE_GRACE_CANCELLED,
        SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
        SESSION_CLOSE_SETTLED,
    )
    from core.errors import ShinjukuError
    from core.money import MONEY_SCALE, RATE_SCALE, amount_to_cents, discounted_cents
    from infrastructure.storage import DBConn, row_to_dict
    from services.session_service import SessionService
    from services.wallet_service import WalletService


Clock = Callable[[], datetime]


class BillingService:
    """Owns billing and settlement while callers retain transaction control."""

    def __init__(
        self,
        engine: BillingEngine,
        billing_config: dict[str, Any],
        points_per_amount: int,
        login_grace_minutes: int,
        sessions: SessionService,
        wallets: WalletService,
        assets: AssetService,
        now: Clock = datetime.now,
    ):
        self.engine = engine
        self.billing_config = billing_config
        self.points_per_amount = points_per_amount
        self.login_grace_minutes = login_grace_minutes
        self.sessions = sessions
        self.wallets = wallets
        self.assets = assets
        self.now = now

    def cap_points(self, cap_value: int) -> int:
        """Convert a capped charge to points, rounding upward."""
        if self.points_per_amount <= 0 or cap_value <= 0:
            return 0
        denominator = self.points_per_amount * MONEY_SCALE
        return (cap_value + denominator - 1) // denominator

    async def logout(self, uid: str | int, conn: DBConn) -> dict[str, Any]:
        session_before = await self.sessions.active_session(uid, conn)
        if not session_before:
            raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
        now = self.now()
        played_seconds = int((now - session_before["createdAt"]).total_seconds())
        door_opened = bool(int(session_before.get("doorOpened") or 0))
        login_grace_seconds = self.login_grace_minutes * 60
        force_mode = (
            door_opened
            and played_seconds <= login_grace_seconds
            and played_seconds < 3600
        )
        if force_mode:
            wallet_before = await self.wallets.wallet(uid, conn)
            await conn.execute(
                'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, '
                '"billingCost"=0, "finalCost"=0, "closeReason"=? WHERE id=?',
                now,
                SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
                session_before["id"],
            )
            closed = row_to_dict(
                await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session_before["id"])
            )
            return {
                "session": closed,
                "billing": {
                    "totalCost": 0,
                    "startTime": session_before["createdAt"],
                    "endTime": closed.get("closedAt") or self.now(),
                    "segments": [],
                    "blocks": [],
                    "points": 0,
                },
                "wallet": wallet_before,
                "walletBefore": wallet_before,
                "walletAfter": await self.wallets.wallet(
                    str(session_before["userId"]),
                    conn,
                    True,
                ),
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
            await self.wallets.deduct_wallet(
                str(session["userId"]),
                cost,
                "会话结算: SESSION_SETTLEMENT",
                conn,
            )
        if discount and discount.get("consumedAssets"):
            await self.assets.delete_user_assets(
                str(session["userId"]),
                discount["consumedAssets"],
                conn,
            )
        points_earned = int(billing.get("points") or 0)
        if points_earned > 0:
            await self.wallets.add_points(
                str(session["userId"]),
                points_earned,
                "游玩积分: SESSION_POINTS",
                conn,
            )
            preview["pointsEarned"] = points_earned
        closed_at = self.now()
        grace_minutes = max(0, int(self.billing_config.get("grace_minutes") or 0))
        within_standard_grace = (
            not door_opened
            and played_seconds < 3600
            and played_seconds // 60 <= grace_minutes
            and int(billing["totalCost"]) == 0
        )
        close_reason = (
            SESSION_CLOSE_GRACE_CANCELLED
            if within_standard_grace
            else SESSION_CLOSE_SETTLED
        )
        await conn.execute(
            'UPDATE "Session" SET "closedAt"=?, "isActive"=NULL, '
            '"billingCost"=?, "finalCost"=?, "closeReason"=? WHERE id=?',
            closed_at,
            billing["totalCost"],
            cost,
            close_reason,
            session["id"],
        )
        closed = row_to_dict(await conn.fetchrow('SELECT * FROM "Session" WHERE id=?', session["id"]))
        preview["session"] = closed
        preview["wallet"] = wallet_before
        preview["walletBefore"] = wallet_before
        preview["walletAfter"] = await self.wallets.wallet(
            str(session["userId"]),
            conn,
            True,
        )
        return preview

    async def billing(self, uid: str | int, conn: DBConn) -> dict[str, Any]:
        session = await self.sessions.active_session(uid, conn)
        if not session:
            raise ShinjukuError("用户未登录。", "USER_NOT_LOGGED_IN")
        end = session.get("closedAt") or self.now()
        calculation_end = end
        played_seconds = max(0, int((end - session["createdAt"]).total_seconds()))
        door_opened = bool(int(session.get("doorOpened") or 0))
        login_grace_seconds = self.login_grace_minutes * 60
        if door_opened and login_grace_seconds < played_seconds < 3600:
            calculation_end = session["createdAt"] + timedelta(hours=1)
        wallet = await self.wallets.wallet(uid, conn, True)
        monthly_pass = self.has_monthly_pass(wallet)
        cap24 = amount_to_cents(
            self.billing_config.get("cap_24h_pass" if monthly_pass else "cap_24h") or 0
        )
        day_cap = amount_to_cents(
            self.billing_config.get("day_cap_pass" if monthly_pass else "day_cap") or 69
        )
        night_cap = amount_to_cents(
            self.billing_config.get("night_cap_pass" if monthly_pass else "night_cap") or 69
        )
        segments: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        overnight_caps: list[dict[str, Any]] = []
        total_cost = 0
        total_points = 0
        current = session["createdAt"]
        while current < calculation_end:
            block_end = min(current + timedelta(days=1), calculation_end)
            block = self.engine.calculate(
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
                total_points += self.cap_points(cap24)
            else:
                if overnight_cap:
                    total_points += self.cap_points(night_cap)
                for segment in block["segments"]:
                    if segment.get("overnightCapCovered"):
                        continue
                    if segment["isCapped"]:
                        cap = day_cap if segment["ruleId"] == 1 else night_cap
                        total_points += self.cap_points(cap)
                    else:
                        total_points += segment["durationMinutes"] // 60
            if cap24 > 0:
                for segment in block["segments"]:
                    segment["blockIndex"] = len(blocks)
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

        coupon = self.best_coupon(wallet)
        if coupon and coupon["rateBps"] < RATE_SCALE:
            total = response["billing"]["totalCost"]
            if total > 0:
                final_cost = discounted_cents(total, coupon["rateBps"])
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
    def has_pass_for_billing(wallet: dict[str, Any]) -> bool:
        passes = wallet.get("passes", {}).get("details", {}).get("available", []) or []
        return len(passes) > 0

    @staticmethod
    def has_monthly_pass(wallet: dict[str, Any]) -> bool:
        return any(
            asset.get("assetDefId") == MONTHLY_PASS_ASSET_ID
            and asset.get("assetType") == PASS_ASSET_TYPE
            for asset in wallet.get("passes", {}).get("details", {}).get("available", [])
        )

    @staticmethod
    def best_coupon(wallet: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [
            asset
            for asset in wallet["tickets"].get("details", {}).get("available", [])
            if asset.get("asset", {}).get("billingEffect", {}).get("type") == "RATE"
        ]
        if not candidates:
            return None
        best = min(
            candidates,
            key=lambda asset: (
                asset["asset"]["billingEffect"]["rateBps"],
                asset.get("expireAt") or datetime.max,
            ),
        )
        effect = best["asset"]["billingEffect"]
        return {
            "id": best["id"],
            "name": best["asset"].get("name") or "优惠券",
            "rateBps": int(effect["rateBps"]),
        }
