"""Present and redeem-code operations."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Awaitable, Callable

try:
    from .asset_service import AssetService
    from .constants import CURRENCY_ASSET_TYPE, PAID_CURRENCY_ASSET_ID
    from .errors import ShinjukuError
    from .storage import DBConn, parse_datetime, row_to_dict
except ImportError:  # pragma: no cover - standalone test/import compatibility
    from asset_service import AssetService
    from constants import CURRENCY_ASSET_TYPE, PAID_CURRENCY_ASSET_ID
    from errors import ShinjukuError
    from storage import DBConn, parse_datetime, row_to_dict


FindUser = Callable[[str | int, DBConn], Awaitable[dict[str, Any] | None]]
Clock = Callable[[], datetime]


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


class PresentService:
    """Owns presents and redemption rules without owning transactions."""

    def __init__(self, assets: AssetService, find_user: FindUser, now: Clock = datetime.now):
        self.assets = assets
        self.find_user = find_user
        self.now = now

    async def upsert_present_by_id(
        self,
        uid: str | int,
        present_id: int,
        conn: DBConn,
    ) -> list[dict[str, Any]]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        present = row_to_dict(await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', present_id))
        if not present:
            raise ShinjukuError("注册礼包不存在。", "ASSET_NOT_FOUND")
        return await self.upsert_present(conn, user, present, f"present:{present_id}")

    async def redeem(
        self,
        uid: str | int,
        code: str,
        conn: DBConn,
    ) -> dict[str, Any]:
        user = await self.find_user(uid, conn)
        if not user:
            raise ShinjukuError("用户不存在。", "USER_NOT_FOUND")
        redeem = row_to_dict(await conn.fetchrow('SELECT * FROM "Redeem" WHERE code=?', code))
        if not redeem:
            raise ShinjukuError("兑换码不存在或已使用。", "REDEEM_CODE_NOT_FOUND_OR_USED")
        present_row = row_to_dict(
            await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', redeem["presentId"])
        )
        if not present_row:
            raise ShinjukuError("兑换码对应的礼包不存在。", "ASSET_NOT_FOUND")
        now = self.now()
        active_at = parse_datetime(redeem.get("activeAt"))
        expire_at = parse_datetime(redeem.get("expireAt"))
        if active_at and active_at > now:
            raise ShinjukuError("兑换码尚未生效。", "REDEEM_NOT_ACTIVE")
        if expire_at and expire_at < now:
            raise ShinjukuError("兑换码已过期。", "REDEEM_EXPIRED")
        used_count = int(
            await conn.fetchval(
                'SELECT count(*) FROM "RedeemRecord" WHERE "redeemId"=?',
                redeem["id"],
            )
            or 0
        )
        if used_count >= int(redeem.get("maxUseCount") or 1):
            raise ShinjukuError("兑换码已达到最大使用次数。", "REDEEM_CODE_LIMIT_EXCEEDED")
        present = dict(present_row)
        present["body"] = _json(present_row.get("body"))
        assets = await self.upsert_present(conn, user, present, f"redeem:{code}")
        await conn.execute(
            'INSERT INTO "RedeemRecord" ("userId","redeemId","presentId") VALUES (?,?,?)',
            user["id"],
            redeem["id"],
            redeem["presentId"],
        )
        return {"present": present, "assets": assets}

    async def create_gift_code(
        self,
        present_id: int,
        currency_amount_cents: int,
        max_use_count: int,
        conn: DBConn,
    ) -> dict[str, Any]:
        present = row_to_dict(await conn.fetchrow('SELECT * FROM "Present" WHERE id=?', present_id))
        if not present:
            raise ShinjukuError("礼包不存在。", "ASSET_NOT_FOUND")
        amount_cents = int(currency_amount_cents)
        if amount_cents <= 0:
            raise ShinjukuError("货币数量必须大于 0。", "INVALID_AMOUNT")
        uses = int(max_use_count)
        if uses <= 0:
            raise ShinjukuError("兑换次数必须大于 0。", "INVALID_USE_COUNT")
        await self.assets.ensure_currency_asset(conn)
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
        code = await self.generate_code(conn)
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
    async def generate_code(conn: DBConn) -> str:
        while True:
            code = secrets.token_hex(4).upper()
            exists = await conn.fetchval('SELECT 1 FROM "Redeem" WHERE code=?', code)
            if not exists:
                return code

    async def upsert_present(
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
                asset = row_to_dict(
                    await conn.fetchrow('SELECT * FROM "Asset" WHERE id=?', int(item["id"]))
                )
            else:
                asset = row_to_dict(
                    await conn.fetchrow(
                        'SELECT * FROM "Asset" WHERE "assetId"=? AND type=?',
                        int(item["assetId"]),
                        str(item["assetType"]),
                    )
                )
                if not asset:
                    asset = await self.assets.ensure_standard_asset(
                        conn,
                        int(item.get("assetId") or 0),
                        str(item.get("assetType") or ""),
                    )
            if not asset:
                continue
            changes.append(
                await self.assets.add_asset_by_def(
                    user["id"],
                    asset,
                    amount,
                    item.get("comment") or comment_prefix,
                    conn,
                    item,
                )
            )
        return changes
