from datetime import datetime, timedelta

from presentation.assets import format_items, mahjong_rank_name
from presentation.billing import format_billing, format_leave_billing
from presentation.common import date_time, duration, money, number, time_range
from presentation.history import format_history
from presentation.presence import format_players
from presentation.pricing import format_pricing
from presentation.wallet import format_wallet


START = datetime(2026, 8, 25, 11, 30)
END = START + timedelta(hours=1)


def test_common_presenters_keep_public_value_formats():
    assert money(1234) == "12.34"
    assert number(None) == "0"
    assert date_time("2026-08-25T11:30:00") == "2026/08/25 11:30:00"
    assert date_time(None) == "永不过期"
    assert duration(75) == "1小时15分钟"
    assert time_range(START, END) == "08/25 11:30:00 - 08/25 12:30:00"


def test_wallet_presenter_contract():
    wallet = {
        "total": {"available": 1234, "all": 2234},
        "paid": {"available": 1000},
        "free": {"available": 234, "all": 1234},
        "points": {"available": 7},
        "tickets": {"available": 1, "all": 2},
        "passes": {"available": 1, "all": 1},
    }

    assert format_wallet(wallet, "馕") == (
        "--- 钱包 ---\n"
        "总余额: 12.34/22.34 馕\n"
        "付费余额: 10 馕\n"
        "免费余额: 2.34/12.34 馕\n"
        "积分: 7\n"
        "优惠券: 1/2 张\n"
        "通行证: 1/1 个"
    )


def test_assets_presenter_contract_includes_rank():
    assets = [
        {
            "id": 3,
            "assetType": "CURRENCY",
            "assetDefId": 10001,
            "count": 1250,
            "activeAt": START,
            "expireAt": None,
            "asset": {"name": "付费余额"},
        }
    ]
    rank = {
        "games": 2,
        "rankPoints": 500,
        "rating": 1512.5,
        "firstCount": 1,
        "secondCount": 0,
        "thirdCount": 1,
        "fourthCount": 0,
    }

    assert mahjong_rank_name(500) == "雀士 I"
    assert format_items(assets, "馕", rank) == (
        "--- 资产 ---\n"
        "[3] 付费余额 x12.50 馕｜生效: 2026/08/25 11:30:00｜过期: 永不过期\n"
        "--- 日麻段位 ---\n"
        "雀士 I｜段位分 500｜Rate 1512.50\n"
        "总对局 2｜平均顺位 2.00"
    )
    assert format_items([], "馕") == "暂无资产。"


def test_history_presenter_contract():
    sessions = [
        {
            "id": 9,
            "createdAt": START,
            "closedAt": END,
            "finalCost": 1200,
            "isActive": None,
            "ENTRY_TYPE": "sneak",
        }
    ]

    assert format_history(sessions, "馕") == (
        "--- 历史记录 ---\n"
        "[9] 已结束（偷偷上机）｜2026/08/25 11:30:00 -> "
        "2026/08/25 12:30:00｜12 馕"
    )
    assert format_history([], "馕") == "暂无历史记录。"


def test_presence_presenter_contract_masks_sneak_players():
    users = [
        {
            "id": 1,
            "binds": [{"type": "QQ", "bid": "12345"}],
            "sessions": [{"createdAt": START, "ENTRY_TYPE": "normal"}],
        },
        {
            "id": 2,
            "binds": [{"type": "QQ", "bid": "67890"}],
            "sessions": [{"createdAt": END, "ENTRY_TYPE": "sneak"}],
        },
    ]

    assert format_players(users, {"12345": "Alice"}, True) == (
        "👥 店内目前共有 2 人\n\n"
        "玩家: Alice\n"
        "入场时间: 2026/08/25 11:30:00\n\n"
        "玩家: 未知玩家\n"
        "入场时间: 2026/08/25 12:30:00"
    )


def test_pricing_presenter_contract_uses_defaults():
    assert format_pricing({}, "馕") == (
        "--- 新宿定价表 ---\n"
        "【白天】11:30 - 00:00\n"
        "  普通用户：12 馕/小时，封顶 69 馕\n"
        "  月卡用户：11 馕/小时，封顶 59 馕\n"
        "【夜晚】00:00 - 12:00\n"
        "  普通用户：13 馕/小时，封顶 69 馕\n"
        "  月卡用户：12 馕/小时，封顶 59 馕\n"
        "【深夜衔接】23:00 - 00:00 入场首小时按白天计费\n"
        "  23:30 后入场时，首小时白天费用纳入包夜封顶，不再额外叠加\n"
        "【连续 24 小时】封顶 99 馕（月卡 88 馕）"
    )


def _billing_result() -> dict:
    segment = {
        "ruleName": "白天",
        "ruleId": 1,
        "startTime": START,
        "endTime": END,
        "durationMinutes": 60,
        "cost": 1200,
        "isCapped": False,
    }
    return {
        "session": {"createdAt": START, "ENTRY_TYPE": "normal", "costOverwrite": None},
        "billing": {
            "endTime": END,
            "totalCost": 1200,
            "segments": [segment],
            "blocks": [],
            "overnightCaps": [],
        },
        "wallet": {
            "total": {"available": 5000},
            "passes": {"details": {"available": []}},
        },
    }


def test_billing_presenter_contract():
    assert format_billing(_billing_result(), "馕") == (
        "--- 账单详情 ---\n"
        "入场: 2026/08/25 11:30:00\n"
        "结算: 2026/08/25 12:30:00\n"
        "时长: 1小时0分钟\n"
        "---\n"
        "计费价: 12 馕\n"
        "结算价: 12 馕\n"
        "---\n"
        "当前余额: 50 馕\n"
        "扣款后: 38 馕\n"
        "---\n"
        "计费区间:\n"
        "- 白天\n"
        "  时段: 08/25 11:30:00 - 08/25 12:30:00\n"
        "  时长: 1小时0分钟\n"
        "  费用: 12 馕"
    )


def test_leave_billing_presenter_contract():
    result = _billing_result()
    result["walletBefore"] = {"total": {"available": 5000}}
    result["walletAfter"] = {"total": {"available": 3800}}
    result["pointsEarned"] = 1

    text = format_leave_billing(result, "馕", "Alice (12345)")

    assert text.startswith(
        "✅ 已为用户 Alice (12345) 退场\n"
        "离开时请带走随身垃圾及手套，确认房门关好，欢迎您再次光临新宿。"
    )
    assert "当前余额: 50 馕\n扣款后: 38 馕" in text
    assert "🎁 本次游玩获得 1 积分" in text
    assert text.endswith("  费用: 12 馕")
