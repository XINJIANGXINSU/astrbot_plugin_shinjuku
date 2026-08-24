import asyncio
from datetime import datetime, timedelta

import pytest

import shinjuku_service as service_module
from shinjuku_service import ShinjukuService


BILLING_CONFIG = {
    "day_start": "11:30",
    "day_end": "00:00",
    "day_price": 12,
    "day_price_pass": 11,
    "day_cap": 69,
    "day_cap_pass": 59,
    "night_start": "00:00",
    "night_end": "12:00",
    "late_day_start": "23:00",
    "night_cap_cover_start": "23:30",
    "night_price": 13,
    "night_price_pass": 12,
    "night_cap": 69,
    "night_cap_pass": 59,
    "cap_24h": 99,
    "cap_24h_pass": 88,
    "grace_minutes": 15,
}


def make_service(tmp_path) -> ShinjukuService:
    return ShinjukuService(
        str(tmp_path / "shinjuku.db"),
        billing_config=BILLING_CONFIG,
    )


@pytest.mark.parametrize(
    ("duration", "expected_cost", "expected_hours"),
    [
        pytest.param(timedelta(minutes=2), 0, 0, id="2-minutes"),
        pytest.param(timedelta(minutes=15), 0, 0, id="15-minutes"),
        pytest.param(timedelta(minutes=15, seconds=59), 0, 0, id="15m59s"),
        pytest.param(timedelta(minutes=16), 1200, 1, id="16-minutes"),
        pytest.param(timedelta(minutes=59, seconds=59), 1200, 1, id="59m59s"),
        pytest.param(timedelta(hours=1), 1200, 1, id="1-hour"),
        pytest.param(timedelta(hours=1, minutes=15, seconds=59), 1200, 1, id="1h15m59s"),
        pytest.param(timedelta(hours=1, minutes=16), 2400, 2, id="1h16m"),
        pytest.param(timedelta(hours=2), 2400, 2, id="2-hours"),
    ],
)
def test_basic_hourly_billing_boundaries(tmp_path, duration, expected_cost, expected_hours):
    service = make_service(tmp_path)
    start = datetime(2026, 8, 24, 13, 0, 0)

    result = service.calculate_billing(start, start + duration)

    assert result["totalCost"] == expected_cost
    assert sum(segment["rawCost"] for segment in result["segments"]) == expected_hours * 1200


@pytest.mark.parametrize(
    ("entry_at", "expected_rule", "expected_cost", "first_segment_minutes"),
    [
        pytest.param(datetime(2026, 8, 24, 11, 29), "夜晚计费", 2500, 31, id="entry-11h29"),
        pytest.param(datetime(2026, 8, 24, 11, 30), "白天计费", 1200, 60, id="entry-11h30"),
        pytest.param(datetime(2026, 8, 24, 23, 0), "白天计费", 1200, 60, id="entry-23h00"),
        pytest.param(datetime(2026, 8, 24, 23, 29), "白天计费", 1200, 60, id="entry-23h29"),
        pytest.param(datetime(2026, 8, 24, 23, 30), "白天计费", 1200, 60, id="entry-23h30"),
        pytest.param(datetime(2026, 8, 24, 23, 59), "白天计费", 1200, 60, id="entry-23h59"),
    ],
)
def test_entry_time_selects_expected_billing_rule(
    tmp_path,
    entry_at,
    expected_rule,
    expected_cost,
    first_segment_minutes,
):
    service = make_service(tmp_path)

    result = service.calculate_billing(entry_at, entry_at + timedelta(hours=1))

    assert result["totalCost"] == expected_cost
    assert result["segments"][0]["ruleName"] == expected_rule
    assert result["segments"][0]["durationMinutes"] == first_segment_minutes


def test_session_crossing_midnight_switches_from_late_entry_hour_to_night(tmp_path):
    service = make_service(tmp_path)
    start = datetime(2026, 8, 24, 23, 0)

    result = service.calculate_billing(start, start + timedelta(hours=1, minutes=30))

    assert result["totalCost"] == 2500
    assert [segment["ruleName"] for segment in result["segments"]] == ["白天计费", "夜晚计费"]
    assert [segment["durationMinutes"] for segment in result["segments"]] == [60, 30]


def test_existing_night_session_crossing_noon_switches_to_day(tmp_path):
    service = make_service(tmp_path)
    start = datetime(2026, 8, 24, 11, 29)

    result = service.calculate_billing(start, start + timedelta(hours=1))

    assert result["totalCost"] == 2500
    assert [segment["ruleName"] for segment in result["segments"]] == ["夜晚计费", "白天计费"]
    assert [segment["durationMinutes"] for segment in result["segments"]] == [31, 29]


@pytest.mark.parametrize(
    ("start", "duration", "has_pass", "expected_cost", "expected_rate", "expected_cap"),
    [
        pytest.param(
            datetime(2026, 8, 24, 13, 0),
            timedelta(hours=7),
            False,
            6900,
            1200,
            6900,
            id="day-regular-cap",
        ),
        pytest.param(
            datetime(2026, 8, 24, 13, 0),
            timedelta(hours=6),
            True,
            5900,
            1100,
            5900,
            id="day-pass-cap",
        ),
        pytest.param(
            datetime(2026, 8, 24, 1, 0),
            timedelta(hours=6),
            False,
            6900,
            1300,
            6900,
            id="night-regular-cap",
        ),
        pytest.param(
            datetime(2026, 8, 24, 1, 0),
            timedelta(hours=5),
            True,
            5900,
            1200,
            5900,
            id="night-pass-cap",
        ),
    ],
)
def test_day_and_night_caps(
    tmp_path,
    start,
    duration,
    has_pass,
    expected_cost,
    expected_rate,
    expected_cap,
):
    service = make_service(tmp_path)

    result = service.calculate_billing(start, start + duration, pass_override=has_pass)

    segment = result["segments"][0]
    assert result["totalCost"] == expected_cost
    assert segment["rawCost"] == int(duration.total_seconds() // 3600) * expected_rate
    assert segment["cost"] == expected_cap
    assert segment["isCapped"] is True


def test_overnight_cap_covers_late_entry_first_hour(tmp_path):
    service = make_service(tmp_path)
    start = datetime(2026, 8, 24, 23, 30)

    result = service.calculate_billing(start, datetime(2026, 8, 25, 6, 30))

    assert result["totalCost"] == 6900
    assert result["overnightCap"]["cappedCost"] == 6900
    assert result["overnightCap"]["rawCost"] == 8100
    assert all(segment.get("overnightCapCovered") for segment in result["segments"])
    assert result["segments"][0]["reason"] == "late_entry_first_hour"


@pytest.mark.parametrize(
    ("duration", "expected_total", "expected_blocks", "second_block_cost"),
    [
        pytest.param(timedelta(hours=24), 9900, 1, None, id="continuous-24-hour-cap"),
        pytest.param(timedelta(hours=25), 11100, 2, 1200, id="second-block-after-24-hours"),
    ],
)
def test_continuous_24_hour_blocks(
    tmp_path,
    monkeypatch,
    duration,
    expected_total,
    expected_blocks,
    second_block_cost,
):
    clock = {"now": datetime(2026, 8, 24, 13, 0)}
    monkeypatch.setattr(service_module, "_now", lambda: clock["now"])

    async def run():
        service = make_service(tmp_path)
        await service.register("12345")
        await service.login("QQ:12345")
        clock["now"] += duration

        result = await service.billing("QQ:12345")

        assert result["billing"]["totalCost"] == expected_total
        assert len(result["billing"]["blocks"]) == expected_blocks
        assert result["billing"]["blocks"][0]["cappedCost"] == 9900
        assert result["billing"]["blocks"][0]["isCapped"] is True
        if second_block_cost is not None:
            assert result["billing"]["blocks"][1]["cappedCost"] == second_block_cost
            assert result["billing"]["blocks"][1]["isCapped"] is False
        await service.close()

    asyncio.run(run())
