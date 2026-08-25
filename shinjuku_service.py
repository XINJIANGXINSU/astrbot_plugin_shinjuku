from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

try:
    from .asset_service import AssetService
    from .billing_engine import BillingEngine
    from .billing_service import BillingService
    from .constants import (
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        MONEY_MIGRATION_KEY,
        MONTHLY_PASS_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_ID,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from .errors import ShinjukuError
    from .migrations import DatabaseMigrator
    from .money import (
        MONEY_SCALE,
        RATE_SCALE,
        amount_to_cents,
        cents_to_text,
        discounted_cents as _discounted_cents,
        discount_tenths_text as _discount_tenths_text,
        discount_tenths_to_bps as _discount_tenths_to_bps,
    )
    from .present_service import PresentService
    from .session_service import SessionService
    from .storage import (
        DBConn,
        SQLitePool,
    )
    from .user_service import UserService
    from .wallet_service import WalletService
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from asset_service import AssetService
    from billing_engine import BillingEngine
    from billing_service import BillingService
    from constants import (
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
        CURRENCY_ASSET_TYPE,
        FREE_CURRENCY_ASSET_ID,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        MONEY_MIGRATION_KEY,
        MONTHLY_PASS_ASSET_ID,
        PAID_CURRENCY_ASSET_ID,
        PASS_ASSET_TYPE,
        POINTS_ASSET_ID,
        POINTS_ASSET_TYPE,
        TICKET_ASSET_TYPE,
    )
    from errors import ShinjukuError
    from migrations import DatabaseMigrator
    from money import (
        MONEY_SCALE,
        RATE_SCALE,
        amount_to_cents,
        cents_to_text,
        discounted_cents as _discounted_cents,
        discount_tenths_text as _discount_tenths_text,
        discount_tenths_to_bps as _discount_tenths_to_bps,
    )
    from present_service import PresentService
    from session_service import SessionService
    from storage import (
        DBConn,
        SQLitePool,
    )
    from user_service import UserService
    from wallet_service import WalletService


def _now() -> datetime:
    return datetime.now()


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
        self.storage = SQLitePool(db_path)
        self.migrations = DatabaseMigrator()
        self.users = UserService(lambda: _now())
        self.assets = AssetService(self.currency, self.users.find_user, lambda: _now())
        self.wallets = WalletService(self.assets, self.users.find_user, lambda: _now())
        self.presents = PresentService(self.assets, self.users.find_user, lambda: _now())
        self.sessions = SessionService(
            self.users,
            self.wallets,
            self.currency,
            self.max_active_checkcodes,
            self.self_open_door_enabled,
            lambda: _now(),
        )
        self.billings = BillingService(
            self.billing_engine,
            self.billing_config,
            self.points_per_amount,
            self.login_grace_minutes,
            self.sessions,
            self.wallets,
            self.assets,
            lambda: _now(),
        )

    async def connect(self) -> None:
        if not self.db_path:
            raise ShinjukuError("请先在插件配置中填写 database_path。")
        await self.storage.connect(self._init_schema)

    async def close(self) -> None:
        await self.storage.close()

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[DBConn]:
        await self.connect()
        async with self.storage.acquire() as conn:
            yield conn

    async def _init_schema(self, conn: DBConn) -> None:
        await self.migrations.initialize(conn)

    async def _generate_checkcode(self, conn: DBConn) -> str:
        return await self.sessions.generate_checkcode(conn)

    async def _active_session_count(self, conn: DBConn) -> int:
        return await self.sessions.active_session_count(conn)

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
        return self.billings.cap_points(cap_value)

    async def find_user(self, uid: str | int, conn: DBConn | None = None) -> dict[str, Any] | None:
        if conn is None:
            async with self._acquire() as acquired:
                return await self.find_user(uid, acquired)
        return await self.users.find_user(uid, conn)

    async def register(self, platform_id: str, register_code: str = "") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                registration = await self.users.register(platform_id, conn)
                user = registration["user"]
                if not registration["created"]:
                    return {"user": user, "created": False, "gift": None}
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
                return await self.sessions.login(
                    uid,
                    entry_type,
                    generate_checkcode,
                    conn,
                )

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
                return await self.sessions.door_verify(sender_uid, code_str, conn)

    async def logout(self, uid: str) -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.billings.logout(uid, conn)

    @staticmethod
    def _has_pass_for_billing(wallet: dict[str, Any]) -> bool:
        return BillingService.has_pass_for_billing(wallet)

    async def force_logout(self, uid: str) -> dict[str, Any]:
        """管理员强制退场：直接关闭会话，不做结算、不发积分。"""
        async with self._acquire() as conn:
            async with conn.transaction():
                return await self.sessions.force_logout(uid, conn)

    async def billing(self, uid: str, conn: DBConn | None = None) -> dict[str, Any]:
        if conn is None:
            async with self._acquire() as acquired:
                return await self.billing(uid, acquired)
        return await self.billings.billing(uid, conn)

    @staticmethod
    def _best_coupon(wallet: dict[str, Any]) -> dict[str, Any] | None:
        return BillingService.best_coupon(wallet)

    async def history(self, uid: str, limit: int = 5) -> list[dict[str, Any]]:
        async with self._acquire() as conn:
            return await self.sessions.history(uid, limit, conn)

    async def active_session(self, uid: str, conn: DBConn) -> dict[str, Any] | None:
        return await self.sessions.active_session(uid, conn)

    async def is_sneak_active(self, uid: str) -> bool:
        """用户当前是否处于偷偷上机会话（ENTRY_TYPE=sneak 且 isActive=1）。"""
        async with self._acquire() as conn:
            return await self.sessions.is_sneak_active(uid, conn)

    async def logged_in_users(self) -> list[dict[str, Any]]:
        async with self._acquire() as conn:
            return await self.sessions.logged_in_users(conn)

    async def wallet(self, uid: str, details: bool = False, conn: DBConn | None = None) -> dict[str, Any]:
        if conn is None:
            async with self._acquire() as acquired:
                return await self.wallet(uid, details, acquired)
        return await self.wallets.wallet(uid, conn, details)

    async def user_assets(
        self,
        uid: str,
        with_asset: bool = True,
        conn: DBConn | None = None,
        asset_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            async with self._acquire() as acquired:
                return await self.user_assets(uid, with_asset, acquired, asset_types)
        return await self.assets.user_assets(uid, conn, with_asset, asset_types)

    async def mahjong_rank(self, uid: str, conn: DBConn | None = None) -> dict[str, Any] | None:
        """读取日麻插件写入同一新宿数据库的段位资料；未联动或未参赛时返回 None。"""
        if conn is None:
            async with self._acquire() as acquired:
                return await self.mahjong_rank(uid, acquired)
        return await self.users.mahjong_rank(uid, conn)

    async def add_paid_currency(self, uid: str, amount_cents: int, comment: str = "admin add") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.wallets.add_paid_currency(uid, amount_cents, comment, conn)

    async def add_points(
        self,
        uid: str,
        amount: int,
        comment: str = "游玩积分",
        conn: DBConn | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.add_points(uid, amount, comment, acquired)
        return await self.wallets.add_points(uid, amount, comment, conn)

    async def charge_wallet(self, uid: str, amount_cents: int, comment: str = "admin charge") -> dict[str, Any]:
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.wallets.charge_wallet(uid, amount_cents, comment, conn)

    async def add_pass(self, uid: str, days: int = 30, comment: str = "admin member") -> dict[str, Any]:
        """为用户发放通行证，与已有通行证自动续期叠加。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.wallets.add_pass(uid, days, comment, conn)

    async def _ensure_asset_definition(
        self,
        conn: DBConn,
        asset_id: int,
        asset_type: str,
        name: str,
        description: str,
        billing_effect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.assets.ensure_asset_definition(
            conn,
            asset_id,
            asset_type,
            name,
            description,
            billing_effect,
        )

    async def ensure_currency_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self.assets.ensure_currency_asset(conn)

    async def ensure_points_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self.assets.ensure_points_asset(conn)

    async def ensure_pass_asset(self, conn: DBConn) -> dict[str, Any]:
        return await self.assets.ensure_pass_asset(conn)

    async def grant_coupon(self, uid: str, discount_tenths: Any, days: int = 30, comment: str = "admin coupon") -> dict[str, Any]:
        """发放优惠券：discount_tenths 为 0-10 折（8 表示 8 折，付 80%），默认有效期 30 天。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.wallets.grant_coupon(uid, discount_tenths, days, comment, conn)

    async def ensure_coupon_asset(self, conn: DBConn, rate_bps: int) -> dict[str, Any]:
        return await self.assets.ensure_coupon_asset(conn, rate_bps)

    async def deduct_wallet(self, uid: str, amount_cents: int, comment: str, conn: DBConn) -> None:
        await self.wallets.deduct_wallet(uid, amount_cents, comment, conn)

    async def delete_user_assets(self, uid: str, ids: list[int], conn: DBConn) -> None:
        await self.assets.delete_user_assets(uid, ids, conn)

    async def log_asset_change(
        self,
        conn: DBConn,
        asset: dict[str, Any],
        original_count: int,
        action: str,
        comment: str,
    ) -> None:
        await self.assets.log_asset_change(conn, asset, original_count, action, comment)

    async def upsert_present_by_id(self, uid: str | int, present_id: int, conn: DBConn | None = None) -> Any:
        if conn is None:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.upsert_present_by_id(uid, present_id, acquired)
        return await self.presents.upsert_present_by_id(uid, present_id, conn)

    async def redeem(self, uid: str | int, code: str, conn: DBConn | None = None) -> dict[str, Any]:
        if conn is None:
            async with self._acquire() as acquired:
                async with acquired.transaction(immediate=True):
                    return await self.redeem(uid, code, acquired)
        return await self.presents.redeem(uid, code, conn)

    async def create_gift_code(self, present_id: int, currency_amount_cents: int, max_use_count: int) -> dict[str, Any]:
        """基于现有礼包生成兑换码：货币数量按参数覆盖，每人限领一次，总数封顶 max_use_count。"""
        async with self._acquire() as conn:
            async with conn.transaction(immediate=True):
                return await self.presents.create_gift_code(
                    present_id,
                    currency_amount_cents,
                    max_use_count,
                    conn,
                )

    @staticmethod
    async def _generate_code(conn: DBConn) -> str:
        return await PresentService.generate_code(conn)

    async def _upsert_present(
        self,
        conn: DBConn,
        user: dict[str, Any],
        present: dict[str, Any],
        comment_prefix: str,
    ) -> list[dict[str, Any]]:
        return await self.presents.upsert_present(conn, user, present, comment_prefix)

    async def _ensure_standard_asset(self, conn: DBConn, asset_id: int, asset_type: str) -> dict[str, Any] | None:
        return await self.assets.ensure_standard_asset(conn, asset_id, asset_type)

    async def add_asset_by_def(
        self,
        uid: str | int,
        asset: dict[str, Any],
        amount: int,
        comment: str,
        conn: DBConn,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.assets.add_asset_by_def(uid, asset, amount, comment, conn, options)
