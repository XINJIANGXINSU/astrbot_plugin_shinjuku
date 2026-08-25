import sqlite3

from schema import SCHEMA_SQL


def test_schema_creates_all_core_tables():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_SQL)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert {
        "User",
        "Bind",
        "Session",
        "Asset",
        "UserAsset",
        "UserAssetLog",
        "Present",
        "Redeem",
        "RedeemRecord",
        "SchemaMigration",
    } <= tables
