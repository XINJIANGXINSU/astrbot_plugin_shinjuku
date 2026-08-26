import asyncio
from datetime import datetime, timedelta

import shinjuku_service as service_module
from core.constants import (
    SESSION_CLOSE_FORCE_CLOSED,
    SESSION_CLOSE_GRACE_CANCELLED,
    SESSION_CLOSE_LOGIN_GRACE_CANCELLED,
    SESSION_CLOSE_SETTLED,
)
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


def test_history_hides_grace_cancellations_before_applying_limit(
    tmp_path, monkeypatch
):
    clock = {"now": datetime(2026, 8, 26, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "history.db"),
            billing_config=BILLING_CONFIG,
            login_grace_minutes=3,
        )
        await service.register("12345")
        await service.add_paid_currency("QQ:12345", 5000)

        settled_login = await service.login("QQ:12345")
        clock["now"] += timedelta(hours=1)
        settled = await service.logout("QQ:12345")
        assert settled["session"]["closeReason"] == SESSION_CLOSE_SETTLED

        clock["now"] += timedelta(minutes=1)
        grace_login = await service.login("QQ:12345")
        clock["now"] += timedelta(minutes=15, seconds=59)
        grace = await service.logout("QQ:12345")
        assert grace["session"]["closeReason"] == SESSION_CLOSE_GRACE_CANCELLED

        clock["now"] += timedelta(minutes=1)
        door_login = await service.login("QQ:12345")
        await service.door_verify(
            "QQ:12345", door_login["session"]["CHECKCODE"]
        )
        clock["now"] += timedelta(minutes=3)
        door_grace = await service.logout("QQ:12345")
        assert (
            door_grace["session"]["closeReason"]
            == SESSION_CLOSE_LOGIN_GRACE_CANCELLED
        )

        visible = await service.history("QQ:12345", 1)
        assert [item["id"] for item in visible] == [settled_login["session"]["id"]]

        complete = await service.history(
            "QQ:12345", 3, include_cancelled=True
        )
        assert [item["id"] for item in complete] == [
            door_login["session"]["id"],
            grace_login["session"]["id"],
            settled_login["session"]["id"],
        ]
        await service.close()

    asyncio.run(run())


def test_zero_cost_coupon_remains_a_visible_settlement(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 26, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(
            str(tmp_path / "free-settlement.db"),
            billing_config=BILLING_CONFIG,
        )
        await service.register("12345")
        await service.grant_coupon("QQ:12345", "0", 30)
        await service.login("QQ:12345")
        clock["now"] += timedelta(hours=1)

        result = await service.logout("QQ:12345")
        assert result["session"]["finalCost"] == 0
        assert result["session"]["closeReason"] == SESSION_CLOSE_SETTLED
        assert len(await service.history("QQ:12345", 5)) == 1
        await service.close()

    asyncio.run(run())


def test_force_logout_is_auditable_and_visible(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 8, 26, 13, 0, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = ShinjukuService(str(tmp_path / "force.db"))
        await service.register("12345")
        await service.login("QQ:12345")

        result = await service.force_logout("QQ:12345")
        assert result["session"]["closeReason"] == SESSION_CLOSE_FORCE_CLOSED
        history = await service.history("QQ:12345", 5)
        assert [item["id"] for item in history] == [result["session"]["id"]]
        await service.close()

    asyncio.run(run())
