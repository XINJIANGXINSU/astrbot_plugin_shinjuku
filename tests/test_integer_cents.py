import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import shinjuku_service as service_module
from shinjuku_service import MONEY_MIGRATION_KEY, ShinjukuService, amount_to_cents, cents_to_text


def test_money_conversion_uses_decimal_half_up():
    assert amount_to_cents("0.1") == 10
    assert amount_to_cents("12.345") == 1235
    assert amount_to_cents("12.344") == 1234
    assert cents_to_text(1235) == "12.35"
    assert cents_to_text(-5) == "-0.05"


def test_discount_and_wallet_use_integer_cents(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 24, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "cents.db"),
            billing_config={
                "day_start": "11:30",
                "day_end": "00:00",
                "day_price": "1.01",
                "day_cap": 69,
                "night_start": "00:00",
                "night_end": "12:00",
                "night_price": 13,
                "night_cap": 69,
                "cap_24h": 99,
                "grace_minutes": 15,
            },
        )
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 2000)
        await service.grant_coupon("QQ:12345", "5", 30)
        await service.login("QQ:12345")

        clock["now"] += timedelta(hours=1)
        preview = await service.billing("QQ:12345")
        assert preview["billing"]["totalCost"] == 101
        assert preview["discount"]["finalCost"] == 51
        assert preview["discount"]["appliedLogs"][0]["saved"] == 50

        result = await service.logout("QQ:12345")
        assert result["walletAfter"]["total"]["available"] == 1949
        await service.close()

        raw = sqlite3.connect(tmp_path / "cents.db")
        try:
            count, storage_type = raw.execute(
                'SELECT count, typeof(count) FROM "UserAsset" WHERE "assetType"="CURRENCY"'
            ).fetchone()
            final_cost, final_type = raw.execute(
                'SELECT "finalCost", typeof("finalCost") FROM "Session"'
            ).fetchone()
            assert (count, storage_type) == (1949, "integer")
            assert (final_cost, final_type) == (51, "integer")
        finally:
            raw.close()

    asyncio.run(run())


def test_legacy_database_is_migrated_once(tmp_path):
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        '''
        CREATE TABLE "User" (id INTEGER PRIMARY KEY, "createdAt" TEXT NOT NULL);
        CREATE TABLE "Bind" (id INTEGER PRIMARY KEY, type TEXT, bid TEXT, "userId" INTEGER);
        CREATE TABLE "Session" (
            id INTEGER PRIMARY KEY, "userId" INTEGER, "createdAt" TEXT, "closedAt" TEXT,
            "isActive" INTEGER, "billingCost" INTEGER, "finalCost" INTEGER
        );
        CREATE TABLE "Asset" (
            id INTEGER PRIMARY KEY, "assetId" INTEGER, type TEXT, name TEXT,
            description TEXT, "billingEffect" TEXT, valid INTEGER
        );
        CREATE TABLE "UserAsset" (
            id INTEGER PRIMARY KEY, "userId" INTEGER, "assetDefId" INTEGER,
            "assetType" TEXT, "assetId" INTEGER, count REAL, "activeAt" TEXT, "expireAt" TEXT
        );
        CREATE TABLE "UserAssetLog" (
            id INTEGER PRIMARY KEY, "userId" INTEGER, "userAssetId" INTEGER, "assetId" INTEGER,
            "assetType" TEXT, "changeAmount" INTEGER, "countBefore" INTEGER, "countAfter" INTEGER,
            "expireAtBefore" TEXT, "expireAtAfter" TEXT, action TEXT, comment TEXT
        );
        CREATE TABLE "Present" (id INTEGER PRIMARY KEY, name TEXT, "oncePerUser" INTEGER, body TEXT);
        CREATE TABLE "Redeem" (
            id INTEGER PRIMARY KEY, code TEXT, "presentId" INTEGER, "activeAt" TEXT,
            "expireAt" TEXT, "maxUseCount" INTEGER
        );
        CREATE TABLE "RedeemRecord" (id INTEGER PRIMARY KEY, "userId" INTEGER, "redeemId" INTEGER, "presentId" INTEGER);
        '''
    )
    raw.execute('INSERT INTO "User" VALUES (1, ?)', (datetime(2026, 8, 1).isoformat(),))
    raw.execute('INSERT INTO "Asset" VALUES (1,10001,"CURRENCY","馕",NULL,NULL,1)')
    raw.execute(
        'INSERT INTO "Asset" VALUES (2,20085,"TICKET","8.5折优惠券",NULL,?,1)',
        (json.dumps({"type": "RATE", "value": 0.85}),),
    )
    raw.execute('INSERT INTO "UserAsset" VALUES (1,1,10001,"CURRENCY",1,12.34,NULL,NULL)')
    raw.execute('INSERT INTO "UserAsset" VALUES (2,1,20001,"POINTS",NULL,7,NULL,NULL)')
    raw.execute('INSERT INTO "UserAssetLog" VALUES (1,1,1,10001,"CURRENCY",2,10,12,NULL,NULL,"UPDATE","")')
    raw.execute(
        'INSERT INTO "Session" VALUES (1,1,?,?,NULL,12,9.6)',
        (datetime(2026, 8, 1).isoformat(), datetime(2026, 8, 1, 1).isoformat()),
    )
    raw.execute(
        'INSERT INTO "Present" VALUES (1,"礼包",1,?)',
        (json.dumps([{"assetType": "CURRENCY", "assetId": 10001, "count": 5.5}]),),
    )
    raw.commit()
    raw.close()

    async def migrate_twice():
        service = ShinjukuService(str(db_path))
        await service.connect()
        await service.close()
        await service.connect()
        await service.close()

    asyncio.run(migrate_twice())

    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute('SELECT count, typeof(count) FROM "UserAsset" WHERE id=1').fetchone() == (1234, "integer")
        assert raw.execute('SELECT count FROM "UserAsset" WHERE id=2').fetchone()[0] == 7
        assert raw.execute('SELECT "billingCost","finalCost" FROM "Session" WHERE id=1').fetchone() == (1200, 960)
        assert raw.execute(
            'SELECT "changeAmount","countBefore","countAfter" FROM "UserAssetLog" WHERE id=1'
        ).fetchone() == (200, 1000, 1200)
        body = json.loads(raw.execute('SELECT body FROM "Present" WHERE id=1').fetchone()[0])
        assert body[0]["count"] == 550
        effect = json.loads(raw.execute('SELECT "billingEffect" FROM "Asset" WHERE id=2').fetchone()[0])
        assert effect["rateBps"] == 8500
        assert "value" not in effect
        assert raw.execute('SELECT count(*) FROM "SchemaMigration" WHERE key=?', (MONEY_MIGRATION_KEY,)).fetchone()[0] == 1
    finally:
        raw.close()
