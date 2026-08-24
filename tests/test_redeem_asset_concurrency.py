import asyncio
import sqlite3

import pytest

from shinjuku_service import ShinjukuError, ShinjukuService


async def create_redeem(service: ShinjukuService, code: str, once_per_user: bool, max_use_count: int) -> int:
    async with service._acquire() as conn:
        async with conn.transaction(immediate=True):
            present = await conn.execute(
                'INSERT INTO "Present" (name,"oncePerUser",body) VALUES (?,?,?)',
                f"礼包-{code}",
                int(once_per_user),
                "[]",
            )
            await conn.execute(
                'INSERT INTO "Redeem" (code,"presentId","maxUseCount") VALUES (?,?,?)',
                code,
                present.lastrowid,
                max_use_count,
            )
            return int(present.lastrowid)


def test_concurrent_asset_definition_creation_returns_one_row(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))

        async def ensure_currency():
            async with service._acquire() as conn:
                async with conn.transaction(immediate=True):
                    return await service.ensure_currency_asset(conn)

        first, second = await asyncio.gather(ensure_currency(), ensure_currency())
        assert first["id"] == second["id"]

        async with service._acquire() as conn:
            assert await conn.fetchval(
                'SELECT count(*) FROM "Asset" WHERE type=? AND "assetId"=?',
                "CURRENCY",
                10001,
            ) == 1
            with pytest.raises(sqlite3.IntegrityError):
                async with conn.transaction():
                    await conn.execute(
                        'INSERT INTO "Asset" ("assetId",type,name,valid) VALUES (?,?,?,1)',
                        10001,
                        "CURRENCY",
                        "重复货币",
                    )
        await service.close()

    asyncio.run(run())


def test_redeem_code_is_unique_in_database(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        await create_redeem(service, "ONLYONE", False, 2)

        async with service._acquire() as conn:
            present_id = await conn.fetchval('SELECT id FROM "Present" LIMIT 1')
            with pytest.raises(sqlite3.IntegrityError):
                async with conn.transaction():
                    await conn.execute(
                        'INSERT INTO "Redeem" (code,"presentId","maxUseCount") VALUES (?,?,1)',
                        "ONLYONE",
                        present_id,
                    )
        await service.close()

    asyncio.run(run())


def test_concurrent_redeem_does_not_exceed_max_use_count(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        await service.register("10001")
        await service.register("10002")
        await create_redeem(service, "LIMITONE", False, 1)

        results = await asyncio.gather(
            service.redeem("QQ:10001", "LIMITONE"),
            service.redeem("QQ:10002", "LIMITONE"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ShinjukuError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "REDEEM_CODE_LIMIT_EXCEEDED"

        async with service._acquire() as conn:
            assert await conn.fetchval(
                'SELECT count(*) FROM "RedeemRecord" rr JOIN "Redeem" r ON r.id=rr."redeemId" WHERE r.code=?',
                "LIMITONE",
            ) == 1
        await service.close()

    asyncio.run(run())


def test_same_user_cannot_concurrently_redeem_once_per_user_present(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        registered = await service.register("10001")
        present_id = await create_redeem(service, "ONCEONLY", True, 10)

        results = await asyncio.gather(
            service.redeem("QQ:10001", "ONCEONLY"),
            service.redeem("QQ:10001", "ONCEONLY"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ShinjukuError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "REDEEM_GIFT_ONCE_PER_USER"

        async with service._acquire() as conn:
            assert await conn.fetchval(
                'SELECT count(*) FROM "RedeemRecord" WHERE "userId"=? AND "presentId"=?',
                registered["user"]["id"],
                present_id,
            ) == 1
            redeem_id = await conn.fetchval('SELECT id FROM "Redeem" WHERE code=?', "ONCEONLY")
            with pytest.raises(sqlite3.IntegrityError, match="already redeemed"):
                async with conn.transaction():
                    await conn.execute(
                        'INSERT INTO "RedeemRecord" ("userId","redeemId","presentId") VALUES (?,?,?)',
                        registered["user"]["id"],
                        redeem_id,
                        present_id,
                    )
        await service.close()

    asyncio.run(run())


def test_non_once_present_can_still_be_redeemed_more_than_once(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        await service.register("10001")
        await create_redeem(service, "REPEATOK", False, 2)

        await service.redeem("QQ:10001", "REPEATOK")
        await service.redeem("QQ:10001", "REPEATOK")

        async with service._acquire() as conn:
            assert await conn.fetchval(
                'SELECT count(*) FROM "RedeemRecord" rr JOIN "Redeem" r ON r.id=rr."redeemId" WHERE r.code=?',
                "REPEATOK",
            ) == 2
        await service.close()

    asyncio.run(run())
