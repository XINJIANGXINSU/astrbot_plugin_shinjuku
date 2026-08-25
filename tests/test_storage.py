import asyncio
from datetime import datetime

from storage import SQLitePool, row_to_dict


def test_row_to_dict_normalizes_known_datetime_columns():
    result = row_to_dict(
        {
            "id": 1,
            "createdAt": "2026-08-25T14:00:00",
            "unknown": "2026-08-25T14:00:00",
        }
    )

    assert result["createdAt"] == datetime(2026, 8, 25, 14, 0)
    assert result["unknown"] == "2026-08-25T14:00:00"


def test_sqlite_pool_initializes_once_and_can_reconnect(tmp_path):
    async def run():
        pool = SQLitePool(str(tmp_path / "pool.db"), size=2)
        initialization_count = 0

        async def initialize(conn):
            nonlocal initialization_count
            initialization_count += 1
            await conn._conn.execute(
                "CREATE TABLE IF NOT EXISTS Sample (id INTEGER PRIMARY KEY, value TEXT)"
            )
            await conn._conn.commit()

        await pool.connect(initialize)
        await pool.connect(initialize)
        assert initialization_count == 1

        async with pool.acquire() as conn:
            async with conn.transaction(immediate=True):
                await conn.execute("INSERT INTO Sample (value) VALUES (?)", "ok")

        await pool.close()
        await pool.connect(initialize)
        assert initialization_count == 2
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM Sample") == 1
        await pool.close()

    asyncio.run(run())
