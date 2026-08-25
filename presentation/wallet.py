"""Wallet response presentation."""

from __future__ import annotations

from typing import Any

from .common import money, number


def format_wallet(wallet: dict[str, Any], currency: str) -> str:
    lines = [
        "--- 钱包 ---",
        f"总余额: {money(wallet['total']['available'])}/{money(wallet['total']['all'])} {currency}",
        f"付费余额: {money(wallet['paid']['available'])} {currency}",
        f"免费余额: {money(wallet['free']['available'])}/{money(wallet['free']['all'])} {currency}",
        f"积分: {number(wallet['points']['available'])}",
        f"优惠券: {wallet['tickets']['available']}/{wallet['tickets']['all']} 张",
        f"通行证: {wallet['passes']['available']}/{wallet['passes']['all']} 个",
    ]
    return "\n".join(lines)
