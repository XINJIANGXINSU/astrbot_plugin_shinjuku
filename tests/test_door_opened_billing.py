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


def test_opened_door_uses_exact_three_minute_first_hour_grace(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 24, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "shinjuku.db"),
            billing_config=BILLING_CONFIG,
            login_grace_minutes=3,
        )
        await service.register("12345")

        login = await service.login("QQ:12345")
        assert await service.door_verify("QQ:12345", login["session"]["CHECKCODE"]) == "SUCCESS_FIRST"

        clock["now"] += timedelta(seconds=180)
        free_preview = await service.billing("QQ:12345")
        assert free_preview["billing"]["totalCost"] == 0

        clock["now"] += timedelta(seconds=1)
        charged_preview = await service.billing("QQ:12345")
        assert charged_preview["billing"]["totalCost"] == 1200
        assert charged_preview["billing"]["segments"][0]["durationMinutes"] == 60

        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 1200
        assert not result.get("loginGraceForced")
        await service.close()

    asyncio.run(run())


def test_opened_door_within_three_minutes_is_free(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 24, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "shinjuku.db"),
            billing_config=BILLING_CONFIG,
            login_grace_minutes=3,
        )
        await service.register("12345")
        login = await service.login("QQ:12345")
        await service.door_verify("QQ:12345", login["session"]["CHECKCODE"])

        clock["now"] += timedelta(seconds=180)
        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 0
        assert result["loginGraceForced"] is True
        await service.close()

    asyncio.run(run())


def test_session_without_opened_door_keeps_fifteen_minute_grace(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 24, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "shinjuku.db"),
            billing_config=BILLING_CONFIG,
            login_grace_minutes=3,
        )
        await service.register("12345")
        await service.login("QQ:12345")

        clock["now"] += timedelta(minutes=15, seconds=59)
        free_preview = await service.billing("QQ:12345")
        assert free_preview["billing"]["totalCost"] == 0

        clock["now"] += timedelta(seconds=1)
        charged_preview = await service.billing("QQ:12345")
        assert charged_preview["billing"]["totalCost"] == 1200

        result = await service.logout("QQ:12345")
        assert result["billing"]["totalCost"] == 1200
        assert not result.get("loginGraceForced")
        await service.close()

    asyncio.run(run())


def test_opened_door_keeps_later_fifteen_minute_rounding(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 24, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "shinjuku.db"),
            billing_config=BILLING_CONFIG,
            login_grace_minutes=3,
        )
        await service.register("12345")
        login = await service.login("QQ:12345")
        await service.door_verify("QQ:12345", login["session"]["CHECKCODE"])

        clock["now"] += timedelta(hours=1, minutes=15, seconds=59)
        within_grace = await service.billing("QQ:12345")
        assert within_grace["billing"]["totalCost"] == 1200

        clock["now"] += timedelta(seconds=1)
        beyond_grace = await service.billing("QQ:12345")
        assert beyond_grace["billing"]["totalCost"] == 2400
        await service.close()

    asyncio.run(run())
