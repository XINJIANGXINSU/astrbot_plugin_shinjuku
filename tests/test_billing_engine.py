from datetime import datetime, timedelta
from pathlib import Path

from billing_engine import BillingEngine
from shinjuku_service import ShinjukuService


BILLING_CONFIG = {
    "day_start": "11:30",
    "day_end": "00:00",
    "day_price": 12,
    "day_cap": 69,
    "night_start": "00:00",
    "night_end": "12:00",
    "night_price": 13,
    "night_cap": 69,
    "grace_minutes": 15,
}


def test_billing_engine_calculates_without_service_or_database():
    engine = BillingEngine(BILLING_CONFIG)
    start = datetime(2026, 8, 25, 13, 0)

    result = engine.calculate(start, start + timedelta(hours=1, minutes=16))

    assert result["totalCost"] == 2400
    assert result["segments"][0]["durationMinutes"] == 76


def test_service_calculate_billing_remains_a_compatible_facade(tmp_path):
    service = ShinjukuService(
        str(tmp_path / "shinjuku.db"), billing_config=BILLING_CONFIG
    )
    start = datetime(2026, 8, 25, 13, 0)
    end = start + timedelta(hours=2)

    assert service.calculate_billing(start, end) == service.billing_engine.calculate(
        start, end
    )


def test_billing_engine_has_no_storage_or_service_dependency():
    source = (
        Path(__file__).parents[1] / "billing_engine.py"
    ).read_text(encoding="utf-8")

    assert "shinjuku_service" not in source
    assert "aiosqlite" not in source
    assert "sqlite3" not in source
