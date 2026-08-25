"""Billing and checkout response presentation."""

from __future__ import annotations

from typing import Any

from .common import date_time, duration, money, number, time_range


def _format_segment_lines(
    segment: dict[str, Any], currency: str, indent: str = ""
) -> list[str]:
    rule_name = segment["ruleName"]
    if segment.get("reason") == "late_entry_first_hour":
        rule_name += "（深夜入场首小时）"
    suffix = ""
    if segment.get("overnightCapCovered"):
        suffix = "（计入包夜封顶）"
    elif segment["isCapped"]:
        suffix = " (已封顶)"
    fee = f"{money(segment['cost'])} {currency}{suffix}"
    return [
        f"{indent}- {rule_name}",
        f"{indent}  时段: {time_range(segment['startTime'], segment['endTime'])}",
        f"{indent}  时长: {duration(segment['durationMinutes'])}",
        f"{indent}  费用: {fee}",
    ]


def _format_billing_blocks(billing: dict[str, Any], currency: str) -> list[str]:
    segments = billing.get("segments") or []
    blocks = billing.get("blocks") or []
    lines: list[str] = []
    if not segments:
        lines.append("  (无)")
        return lines
    if not blocks:
        for segment in segments:
            lines.extend(_format_segment_lines(segment, currency))
        for cap in billing.get("overnightCaps") or []:
            lines.append(
                f"包夜封顶: {money(cap['rawCost'])} {currency} → "
                f"{money(cap['cappedCost'])} {currency}"
            )
        return lines
    grouped: dict[int, list[dict[str, Any]]] = {}
    for segment in segments:
        grouped.setdefault(segment.get("blockIndex", 0), []).append(segment)
    for index, block in enumerate(blocks, start=1):
        lines.append(
            f"[24小时块 {index}/{len(blocks)}] "
            f"{time_range(block['startTime'], block['endTime'])}"
        )
        for segment in grouped.get(index - 1, []):
            lines.extend(_format_segment_lines(segment, currency, indent="  "))
        overnight_cap = block.get("overnightCap")
        if overnight_cap:
            lines.append(
                f"  包夜封顶: {money(overnight_cap['rawCost'])} {currency} → "
                f"{money(overnight_cap['cappedCost'])} {currency}"
            )
        if block.get("isCapped"):
            lines.append(
                f"  小计: {money(block['rawCost'])} {currency} → "
                f"封顶 {money(block['cappedCost'])} {currency}"
            )
        else:
            lines.append(f"  小计: {money(block['cappedCost'])} {currency}")
    return lines


def format_billing(result: dict[str, Any], currency: str) -> str:
    billing = result["billing"]
    session = result["session"]
    discount = result.get("discount")
    original_cost = discount["originalCost"] if discount else billing["totalCost"]
    final_cost = discount["finalCost"] if discount else billing["totalCost"]
    if session.get("costOverwrite") is not None:
        final_cost = session["costOverwrite"]

    total_minutes = int((billing["endTime"] - session["createdAt"]).total_seconds() // 60)
    current_balance = result["wallet"]["total"]["available"]
    lines = [
        "--- 账单详情 ---",
        f"入场: {date_time(session['createdAt'])}"
        f"{'（偷偷上机）' if session.get('ENTRY_TYPE') == 'sneak' else ''}",
        f"结算: {date_time(billing['endTime'])}",
        f"时长: {duration(total_minutes)}",
        "---",
        f"计费价: {money(original_cost)} {currency}",
    ]
    if discount and discount.get("appliedLogs"):
        for item in discount["appliedLogs"]:
            lines.append(f"  -「{item['asset']}」 -{money(item['saved'])} {currency}")
    lines.extend(
        [
            f"结算价: {money(final_cost)} {currency}",
            "---",
            f"当前余额: {money(current_balance)} {currency}",
            f"扣款后: {money(current_balance - final_cost)} {currency}",
            "---",
            "计费区间:",
        ]
    )
    if billing["segments"]:
        lines.extend(_format_billing_blocks(billing, currency))
    else:
        lines.append("  (无)")

    passes = result["wallet"].get("passes", {}).get("details", {}).get("available", [])
    if passes and passes[0].get("expireAt"):
        lines.extend(["---", f"您的月卡将于 {date_time(passes[0]['expireAt'])} 到期。"])
    return "\n".join(lines)


def format_leave_billing(
    result: dict[str, Any], currency: str, user_label: str
) -> str:
    billing = result["billing"]
    session = result["session"]
    discount = result.get("discount")
    original_cost = discount["originalCost"] if discount else billing["totalCost"]
    final_cost = discount["finalCost"] if discount else billing["totalCost"]
    if session.get("costOverwrite") is not None:
        final_cost = session["costOverwrite"]
    forced_short = bool(result.get("loginGraceForced"))
    grace_minutes = int(result.get("loginGraceMinutes") or 0)

    wallet_before = result.get("walletBefore") or result["wallet"]
    wallet_after = result.get("walletAfter")
    balance_before = wallet_before["total"]["available"]
    balance_after = (
        wallet_after["total"]["available"]
        if wallet_after
        else balance_before - final_cost
    )
    total_minutes = int((billing["endTime"] - session["createdAt"]).total_seconds() // 60)

    lines = [
        f"✅ 已为用户 {user_label} 退场",
        "离开时请带走随身垃圾及手套，确认房门关好，欢迎您再次光临新宿。",
    ]
    if forced_short:
        lines.insert(1, f"（{grace_minutes}分钟内离场，本次不参与结算）")
    lines.extend(
        [
            "--- 账单详情 ---",
            f"入场: {date_time(session['createdAt'])}"
            f"{'（偷偷上机）' if session.get('ENTRY_TYPE') == 'sneak' else ''}",
            f"结束: {date_time(billing['endTime'])}",
            f"时长: {duration(total_minutes)}",
            "---",
            f"计费价: {money(original_cost)} {currency}",
        ]
    )
    if balance_after < 0:
        lines.insert(1, f"⚠️ 本次结算后欠费 {money(-balance_after)} {currency}，请联系主理人补款。")
    if discount and discount.get("appliedLogs"):
        for item in discount["appliedLogs"]:
            lines.append(f"  -「{item['asset']}」 -{money(item['saved'])} {currency}")
    lines.extend(
        [
            f"结算价: {money(final_cost)} {currency}",
            "---",
            f"当前余额: {money(balance_before)} {currency}",
            f"扣款后: {money(balance_after)} {currency}",
        ]
    )
    points_earned = result.get("pointsEarned")
    if points_earned:
        lines.append(f"🎁 本次游玩获得 {number(points_earned)} 积分")
    lines.extend(["---", "计费区间:"])
    if billing["segments"]:
        lines.extend(_format_billing_blocks(billing, currency))
    else:
        lines.append("  (无)")
    return "\n".join(lines)
