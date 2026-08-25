import asyncio
import sqlite3

from core.constants import (
    ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
    IDENTITY_CONSTRAINTS_MIGRATION_KEY,
    MONEY_MIGRATION_KEY,
)
from shinjuku_service import ShinjukuService


def test_legacy_session_columns_are_added_explicitly(tmp_path):
    db_path = tmp_path / "legacy-columns.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        '''CREATE TABLE "Session" (
            id INTEGER PRIMARY KEY,
            "userId" INTEGER,
            "createdAt" TEXT,
            "closedAt" TEXT,
            "isActive" INTEGER,
            "billingCost" INTEGER,
            "finalCost" INTEGER
        )'''
    )
    raw.commit()
    raw.close()

    async def initialize():
        service = ShinjukuService(str(db_path))
        await service.connect()
        await service.close()

    asyncio.run(initialize())

    raw = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in raw.execute('PRAGMA table_info("Session")').fetchall()
        }
        migration_keys = {
            row[0] for row in raw.execute('SELECT key FROM "SchemaMigration"')
        }
    finally:
        raw.close()

    assert {"CHECKCODE", "doorOpened", "ENTRY_TYPE"} <= columns
    assert {
        MONEY_MIGRATION_KEY,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
    } <= migration_keys
