"""Asset inventory and Mahjong rank presentation."""

from __future__ import annotations

from typing import Any

from .common import date_time, money, number


def mahjong_rank_name(points: int) -> str:
    ranks = (
        (19000, "魂天"),
        (15500, "雀圣 III"),
        (12500, "雀圣 II"),
        (10000, "雀圣 I"),
        (8200, "雀豪 III"),
        (6600, "雀豪 II"),
        (5200, "雀豪 I"),
        (4000, "雀杰 III"),
        (3000, "雀杰 II"),
        (2200, "雀杰 I"),
        (1500, "雀士 III"),
        (1000, "雀士 II"),
        (500, "雀士 I"),
        (0, "初心者"),
    )
    return next(name for threshold, name in ranks if points >= threshold)


def format_items(
    assets: list[dict[str, Any]],
    currency: str,
    mahjong_rank: dict[str, Any] | None = None,
) -> str:
    if not assets and not mahjong_rank:
        return "暂无资产。"
    lines = ["--- 资产 ---"]
    for item in assets:
        asset = item.get("asset") or {}
        name = asset.get("name") or f"{item.get('assetType')}:{item.get('assetDefId')}"
        asset_type = item.get("assetType")
        if asset_type == "CURRENCY":
            suffix = currency
        elif asset_type == "POINTS":
            suffix = "积分"
        else:
            suffix = "个"
        count_text = money(item["count"]) if asset_type == "CURRENCY" else number(item["count"])
        lines.append(
            f"[{item['id']}] {name} x{count_text} {suffix}"
            f"｜生效: {date_time(item.get('activeAt'))}｜过期: {date_time(item.get('expireAt'))}"
        )
    if mahjong_rank:
        games = int(mahjong_rank.get("games") or 0)
        rank_points = int(mahjong_rank.get("rankPoints") or 0)
        average_place = 0.0
        if games:
            average_place = (
                int(mahjong_rank.get("firstCount") or 0)
                + int(mahjong_rank.get("secondCount") or 0) * 2
                + int(mahjong_rank.get("thirdCount") or 0) * 3
                + int(mahjong_rank.get("fourthCount") or 0) * 4
            ) / games
        lines.extend(
            [
                "--- 日麻段位 ---",
                f"{mahjong_rank_name(rank_points)}｜段位分 {rank_points}｜"
                f"Rate {float(mahjong_rank.get('rating') or 1500):.2f}",
                f"总对局 {games}｜平均顺位 {average_place:.2f}"
                if games
                else "尚未完成段位对局",
            ]
        )
    return "\n".join(lines)
