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
    raw.execute(
        'INSERT INTO "Session" '
        '(id,"userId","createdAt","closedAt","isActive","billingCost","finalCost") '
        'VALUES (1,1,"2026-08-01T12:00:00","2026-08-01T13:00:00",NULL,0,0)'
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
        close_reason = raw.execute(
            'SELECT "closeReason" FROM "Session" WHERE id=1'
        ).fetchone()[0]
        history_index = raw.execute(
            'SELECT sql FROM sqlite_master '
            'WHERE type="index" AND name="idx_session_user_history"'
        ).fetchone()[0]
    finally:
        raw.close()

    assert {"CHECKCODE", "doorOpened", "ENTRY_TYPE", "closeReason"} <= columns
    assert close_reason is None
    assert '"userId", "createdAt" DESC' in history_index
    assert {
        MONEY_MIGRATION_KEY,
        IDENTITY_CONSTRAINTS_MIGRATION_KEY,
        ASSET_REDEEM_CONSTRAINTS_MIGRATION_KEY,
    } <= migration_keys
