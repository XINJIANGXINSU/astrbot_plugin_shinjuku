import asyncio
from datetime import datetime, timedelta

import pytest

import shinjuku_service as service_module
from shinjuku_service import ShinjukuError, ShinjukuService


async def create_present(service: ShinjukuService, name: str = "基础礼包") -> int:
    async with service._acquire() as conn:
        async with conn.transaction(immediate=True):
            created = await conn.execute(
                'INSERT INTO "Present" (name,"oncePerUser",body) VALUES (?,0,?)',
                name,
                "[]",
            )
            return int(created.lastrowid)


def test_create_gift_code_can_be_redeemed_into_wallet(tmp_path):
    async def scenario():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        try:
            await service.register("12345")
            present_id = await create_present(service)

            gift = await service.create_gift_code(present_id, 1234, 2)
            redeemed = await service.redeem("QQ:12345", gift["code"])
            wallet = await service.wallet("QQ:12345")

            assert gift["currency_amount"] == 1234
            assert redeemed["present"]["oncePerUser"] == 1
            assert wallet["paid"]["available"] == 1234
        finally:
            await service.close()

    asyncio.run(scenario())


def test_redeem_respects_activation_and_expiration(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 25, 12, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def scenario():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        try:
            await service.register("12345")
            present_id = await create_present(service)
            async with service._acquire() as conn:
                async with conn.transaction(immediate=True):
                    await conn.execute(
                        'INSERT INTO "Redeem" (code,"presentId","activeAt","maxUseCount") VALUES (?,?,?,1)',
                        "FUTURE",
                        present_id,
                        clock["now"] + timedelta(minutes=1),
                    )
                    await conn.execute(
                        'INSERT INTO "Redeem" (code,"presentId","expireAt","maxUseCount") VALUES (?,?,?,1)',
                        "EXPIRED",
                        present_id,
                        clock["now"] - timedelta(minutes=1),
                    )

            with pytest.raises(ShinjukuError, match="尚未生效") as future_error:
                await service.redeem("QQ:12345", "FUTURE")
            assert future_error.value.code == "REDEEM_NOT_ACTIVE"

            with pytest.raises(ShinjukuError, match="已过期") as expired_error:
                await service.redeem("QQ:12345", "EXPIRED")
            assert expired_error.value.code == "REDEEM_EXPIRED"
        finally:
            await service.close()

    asyncio.run(scenario())
