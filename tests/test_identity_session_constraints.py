import asyncio
import sqlite3

import pytest

from shinjuku_service import ShinjukuError, ShinjukuService


def test_concurrent_registration_binds_qq_to_one_user(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        first, second = await asyncio.gather(
            service.register("12345"),
            service.register("12345"),
        )
        assert first["user"]["id"] == second["user"]["id"]
        assert sorted([first["created"], second["created"]]) == [False, True]

        async with service._acquire() as conn:
            assert await conn.fetchval('SELECT count(*) FROM "Bind" WHERE type=? AND bid=?', "QQ", "12345") == 1
            assert await conn.fetchval('SELECT count(*) FROM "User"') == 1
        await service.close()

    asyncio.run(run())


def test_concurrent_login_creates_only_one_active_session_and_code(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        await service.register("12345")

        results = await asyncio.gather(
            service.login("QQ:12345"),
            service.login("QQ:12345"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ShinjukuError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "USER_ALREADY_LOGGED_IN"
        assert successes[0]["session"]["CHECKCODE"]

        async with service._acquire() as conn:
            sessions = await conn.fetch(
                'SELECT * FROM "Session" WHERE "userId"=? AND "isActive"=1',
                successes[0]["session"]["userId"],
            )
            assert len(sessions) == 1
            assert sessions[0]["CHECKCODE"] == successes[0]["session"]["CHECKCODE"]
        await service.close()

    asyncio.run(run())


def test_active_session_checkcode_is_immutable_until_logout(tmp_path):
    db_path = tmp_path / "shinjuku.db"

    async def run():
        service = ShinjukuService(str(db_path))
        await service.register("12345")
        first = await service.login("QQ:12345")
        first_code = first["session"]["CHECKCODE"]

        async with service._acquire() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="checkcode is immutable"):
                async with conn.transaction():
                    await conn.execute(
                        'UPDATE "Session" SET "CHECKCODE"=? WHERE id=?',
                        "7654321",
                        first["session"]["id"],
                    )

        await service.force_logout("QQ:12345")
        second = await service.login("QQ:12345")
        assert second["session"]["id"] != first["session"]["id"]
        assert second["session"]["CHECKCODE"]
        assert first_code
        await service.close()

    asyncio.run(run())


def test_database_unique_indexes_reject_direct_duplicates(tmp_path):
    async def run():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        first = await service.register("12345")

        async with service._acquire() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                async with conn.transaction():
                    await conn.execute(
                        'INSERT INTO "Bind" (type,bid,"userId") VALUES (?,?,?)',
                        "QQ",
                        "12345",
                        first["user"]["id"],
                    )

        await service.login("QQ:12345")
        async with service._acquire() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                async with conn.transaction():
                    await conn.execute(
                        'INSERT INTO "Session" ("userId","createdAt","isActive") VALUES (?,?,1)',
                        first["user"]["id"],
                        "2026-08-24T12:00:00",
                    )
        await service.close()

    asyncio.run(run())
