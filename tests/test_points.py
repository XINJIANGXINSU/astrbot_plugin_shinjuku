import asyncio
from datetime import datetime, timedelta

import shinjuku_service as service_module
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
    "cap_24h": 99,
    "grace_minutes": 15,
}


def make_service(tmp_path, points_per_amount: int) -> ShinjukuService:
    return ShinjukuService(
        str(tmp_path / "points.db"),
        billing_config=BILLING_CONFIG,
        points_per_amount=points_per_amount,
    )


def _clock(monkeypatch, start: datetime) -> dict:
    clock = {"now": start}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])
    return clock


def test_zero_points_per_amount_disables_hourly_points(tmp_path, monkeypatch):
    clock = _clock(monkeypatch, datetime(2026, 11, 23, 14, 0, 0))

    async def run():
        service = make_service(tmp_path, points_per_amount=0)
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 500000)
        await service.login("QQ:12345")

        clock["now"] += timedelta(hours=2)
        preview = await service.billing("QQ:12345")
        assert preview["billing"]["totalCost"] == 2400
        assert preview["billing"]["points"] == 0

        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 2400
        assert result["billing"]["points"] == 0
        assert "pointsEarned" not in result
        assert result["walletAfter"]["points"]["available"] == 0
        await service.close()

    asyncio.run(run())


def test_zero_points_per_amount_disables_capped_points(tmp_path, monkeypatch):
    clock = _clock(monkeypatch, datetime(2026, 11, 23, 14, 0, 0))

    async def run():
        service = make_service(tmp_path, points_per_amount=0)
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 500000)
        await service.login("QQ:12345")

        clock["now"] += timedelta(hours=6)
        preview = await service.billing("QQ:12345")
        assert preview["billing"]["totalCost"] == 6900
        assert preview["billing"]["points"] == 0

        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 6900
        assert result["billing"]["points"] == 0
        assert "pointsEarned" not in result
        assert result["walletAfter"]["points"]["available"] == 0
        await service.close()

    asyncio.run(run())


def test_default_points_per_amount_awards_hourly_points(tmp_path, monkeypatch):
    clock = _clock(monkeypatch, datetime(2026, 11, 23, 14, 0, 0))

    async def run():
        service = make_service(tmp_path, points_per_amount=10)
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 500000)
        await service.login("QQ:12345")

        clock["now"] += timedelta(hours=2)
        result = await service.logout("QQ:12345")
        assert result["billing"]["points"] == 2
        assert result["pointsEarned"] == 2
        assert result["walletAfter"]["points"]["available"] == 2
        await service.close()

    asyncio.run(run())


def test_default_points_per_amount_awards_capped_points(tmp_path, monkeypatch):
    clock = _clock(monkeypatch, datetime(2026, 11, 23, 14, 0, 0))

    async def run():
        service = make_service(tmp_path, points_per_amount=10)
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 500000)
        await service.login("QQ:12345")

        clock["now"] += timedelta(hours=6)
        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 6900
        assert result["billing"]["points"] == 7
        assert result["pointsEarned"] == 7
        assert result["walletAfter"]["points"]["available"] == 7
        await service.close()

    asyncio.run(run())
