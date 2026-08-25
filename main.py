from __future__ import annotations

import re
import sqlite3
from os import path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools, register

from .errors import ShinjukuError
from .event_adapter import EventAdapter
from .money import amount_to_cents
from .nickname_cache import NicknameCache
from .presentation import (
    date_time,
    format_billing,
    format_history,
    format_items,
    format_leave_billing,
    format_players,
    format_pricing,
    format_wallet,
    money,
)
from .settings import PluginSettings
from .shinjuku_service import ShinjukuService


@register("astrbot_plugin_shinjuku", "li", "新宿 上机计费插件", "0.3.4")
class ShinjukuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        try:
            # AstrBot 官方插件数据目录：AstrBot/data/plugin_data/astrbot_plugin_shinjuku/
            default_db = str(StarTools.get_data_dir("astrbot_plugin_shinjuku") / "shinjuku.db")
        except Exception:
            # 旧版 AstrBot 无此接口时回退到插件目录
            default_db = path.join(path.dirname(path.abspath(__file__)), "data", "shinjuku.db")
        self.settings = PluginSettings.from_config(config, default_db)
        self.currency = self.settings.currency
        self.self_open_door_enabled = self.settings.self_open_door_enabled
        self.self_open_door_points_threshold = self.settings.self_open_door_points_threshold
        self.sneak_login_enabled = self.settings.sneak_login_enabled
        self.sneak_login_points_threshold = self.settings.sneak_login_points_threshold
        self.service = ShinjukuService(
            self.settings.database_path,
            self.currency,
            self.settings.billing,
            self.settings.points_per_amount,
            self.settings.max_active_checkcodes,
            self.settings.self_open_door_enabled,
            self.settings.login_grace_minutes,
        )
        self.nicknames = NicknameCache()
        self.events = EventAdapter(self.nicknames)

    async def terminate(self):
        await self.service.close()

    def _sender_id(self, event: AstrMessageEvent) -> str:
        return self.events.sender_id(event)

    def _sender_real_qq(self, event: AstrMessageEvent) -> str:
        return self.events.sender_real_qq(event)

    def _sender_uid(self, event: AstrMessageEvent) -> str:
        return self.events.sender_uid(event)

    def _nickname_scope(self, event: AstrMessageEvent) -> str:
        return self.events.nickname_scope(event)

    def _remember_sender_name(self, event: AstrMessageEvent) -> None:
        self.events.remember_sender_name(event)

    def _admins(self) -> set[str]:
        return set(self.settings.admins)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return self._sender_real_qq(event) in self._admins()

    def _args(self, event: AstrMessageEvent) -> list[str]:
        return self.events.args(event)

    def _at_ids(self, event: AstrMessageEvent) -> list[str]:
        return self.events.at_ids(event)

    def _at_label(self, event: AstrMessageEvent, uid: str) -> str:
        return self.events.at_label(event, uid)

    def _normalize_user(self, raw: str | None, event: AstrMessageEvent, allow_self: bool = True) -> str:
        return self.events.normalize_user(raw, event, allow_self)

    def _qq_from_uid(self, uid: str) -> str:
        return self.events.qq_from_uid(uid)

    async def _ensure_registered(self, uid: str, user_label: str | None = None) -> str:
        if await self.service.find_user(uid):
            return ""
        qq = self._qq_from_uid(uid)
        register_code = self.settings.redeem_code_on_register
        result = await self.service.register(qq, register_code)
        label = user_label or qq
        if result["created"]:
            prefix = (
                f"用户不存在，尝试注册\n为用户 {label} 注册成功\n\n"
                if user_label
                else "用户不存在，尝试注册\n注册成功\n\n"
            )
            if result.get("gift_error"):
                prefix += f"注册礼包发放失败：{result['gift_error']}\n\n"
            return prefix
        return ""

    async def _masked_label(self, uid: str, label: str) -> str:
        """偷偷上机功能开启且用户当前处于偷偷上机会话时，将其身份显示为「未知玩家」。"""
        if not self.sneak_login_enabled:
            return label
        if await self.service.is_sneak_active(uid):
            return "未知玩家"
        return label

    async def _call_onebot_action(self, client: Any, action: str, **kwargs: Any) -> Any:
        """调用 OneBot API，兼容不同版本 AstrBot / aiocqhttp 的客户端接口。"""
        call_action = getattr(client, "call_action", None)
        if callable(call_action):
            try:
                return await call_action(action, **kwargs)
            except AttributeError:
                pass
        api = getattr(client, "api", None)
        if api is not None:
            api_call = getattr(api, "call_action", None)
            if callable(api_call):
                return await api_call(action, **kwargs)
            api_action = getattr(api, action, None)
            if callable(api_action):
                return await api_action(**kwargs)
        client_action = getattr(client, action, None)
        if callable(client_action):
            return await client_action(**kwargs)
        raise AttributeError(f"OneBot client 不支持 {action} API")

    async def _recall_onebot_message(self, event: AstrMessageEvent) -> None:
        """通过 OneBot API 撤回玩家发送的偷偷上机指令消息；失败仅记录日志，不影响原有功能。"""
        try:
            platform_name = event.get_platform_name()
        except Exception:
            platform_name = getattr(getattr(event, "platform_meta", None), "name", "") or ""
        if platform_name != "aiocqhttp":
            return
        try:
            message_id = getattr(event.message_obj, "message_id", None)
            if not message_id:
                return
            client = getattr(event, "bot", None)
            if client is None:
                return
            candidates: list[Any] = []
            try:
                candidates.append(int(message_id))
            except (TypeError, ValueError):
                pass
            candidates.append(str(message_id))
            last_error: Exception | None = None
            for candidate in candidates:
                try:
                    await self._call_onebot_action(client, "delete_msg", message_id=candidate)
                    logger.info(f"新宿：已撤回偷偷上机指令消息 {message_id}")
                    return
                except Exception as exc:
                    last_error = exc
                    logger.debug(f"新宿撤回指令消息失败（{candidate}）: {exc}")
            logger.error(f"新宿：撤回偷偷上机指令消息失败: {last_error}")
        except Exception as exc:
            logger.error(f"新宿：撤回偷偷上机指令消息失败: {exc}")

    def _target_from_optional_arg(self, event: AstrMessageEvent) -> str:
        self._remember_sender_name(event)
        args = self._args(event)
        if args:
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            at_ids = self._at_ids(event)
            uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event)
            self._at_label(event, uid)  # 记住被操作用户的昵称（如管理员代上机）
            return uid
        return self._sender_uid(event)

    async def _safe(self, coro):
        try:
            return await coro
        except ShinjukuError as exc:
            return f"操作失败：{exc.message}"
        except sqlite3.Error as exc:
            logger.error(f"新宿数据库错误: {exc}")
            return f"数据库错误：{exc.__class__.__name__}"
        except Exception as exc:
            logger.error(f"新宿未处理错误: {exc}")
            return f"操作失败：{exc}"

    @filter.command("register")
    async def register_cmd(self, event: AstrMessageEvent):
        """注册新宿用户"""
        self._remember_sender_name(event)
        async def run():
            args = self._args(event)
            if args:
                if not self._is_admin(event):
                    raise ShinjukuError("权限不足。")
                at_ids = self._at_ids(event)
                uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event)
                qq = uid.split(":", 1)[1]
            else:
                qq = self._sender_real_qq(event)
            register_code = self.settings.redeem_code_on_register
            result = await self.service.register(qq, register_code)
            if result["created"]:
                message = f"注册成功，用户 ID：{result['user']['id']}"
                if result.get("gift_error"):
                    message += f"（礼包发放失败：{result['gift_error']}）"
                return message
            return f"已经注册过了，用户 ID：{result['user']['id']}"

        yield event.plain_result(await self._safe(run()))
    @filter.command("login", alias={"入场", "上机"})
    async def login_cmd(self, event: AstrMessageEvent):
        """登录/入场"""
        self._remember_sender_name(event)
        sender_real_qq = self._sender_real_qq(event)
        sender_uid = self._sender_uid(event)
        async def run():
            uid = self._target_from_optional_arg(event)
            prefix = await self._ensure_registered(uid)
            can_self_open_door = False
            if self.self_open_door_enabled:
                wallet = await self.service.wallet(uid)
                points = int(wallet["points"]["available"] or 0)
                can_self_open_door = points > self.self_open_door_points_threshold
            login_result = await self.service.login(uid, generate_checkcode=can_self_open_door)
            session = login_result["session"]
            over_capacity = bool(login_result.get("overCapacity"))
            checkcode = session.get("CHECKCODE") or ""
            if self.self_open_door_enabled and uid == sender_uid and checkcode:
                components: list[Any] = []
                if prefix:
                    components.append(Plain(prefix))
                components.append(At(name="", qq=sender_real_qq))
                components.append(Plain(f" ✅ 入场成功，验证码：'{checkcode}'"))
                if over_capacity:
                    components.append(Plain("\nwoc，音趴！"))
                return components
            if self.self_open_door_enabled and checkcode:
                msg = prefix + "✅ 入场成功"
            else:
                if self._is_admin(event):
                    msg = prefix + "✅ 入场成功"
                else:
                    msg = prefix + "✅ 入场成功，请联系管理员开门"
            if over_capacity:
                msg += "\nwoc，音趴！"
            return msg

        result = await self._safe(run())
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            yield event.plain_result(result)

    @filter.command("偷偷上机")
    async def sneak_login_cmd(self, event: AstrMessageEvent):
        """偷偷上机：仅已注册且积分高于配置门槛的用户可偷偷上机，
        未注册用户回复「用户未注册」，积分不足时禁止入场；
        计费流程与 /login 一致，但会话标记为「偷偷上机」；
        处理完成后通过 OneBot API 撤回玩家发送的指令消息。
        功能开关 sneak_login_enabled 关闭时不响应（与未修改版本行为一致）。"""
        if not self.sneak_login_enabled:
            event.stop_event()
            return
        self._remember_sender_name(event)
        sender_real_qq = self._sender_real_qq(event)
        sender_uid = self._sender_uid(event)

        async def run():
            uid = self._target_from_optional_arg(event)
            if not await self.service.find_user(uid):
                return "用户未注册"
            wallet = await self.service.wallet(uid)
            points = int(wallet["points"]["available"] or 0)
            if points <= self.sneak_login_points_threshold:
                return (
                    f"（积分需高于 {self.sneak_login_points_threshold}）"
                    "条件不满足，禁止偷偷上机"
                )
            can_self_open_door = (
                self.self_open_door_enabled
                and points > self.self_open_door_points_threshold
            )
            login_result = await self.service.login(
                uid,
                "sneak",
                generate_checkcode=can_self_open_door,
            )
            session = login_result["session"]
            over_capacity = bool(login_result.get("overCapacity"))
            checkcode = session.get("CHECKCODE") or ""
            if self.self_open_door_enabled and uid == sender_uid and checkcode:
                components: list[Any] = []
                components.append(Plain("未知玩家"))
                components.append(Plain(f" ✅ 入场成功，验证码：'{checkcode}'"))
                if over_capacity:
                    components.append(Plain("\nwoc，音趴！"))
                return components
            if self.self_open_door_enabled and checkcode:
                msg = "✅ 入场成功"
            else:
                if self._is_admin(event):
                    msg = "✅ 入场成功"
                else:
                    msg = "✅ 入场成功，请联系管理员开门"
            if over_capacity:
                msg += "\nwoc，音趴！"
            return msg

        result = await self._safe(run())
        await self._recall_onebot_message(event)
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            yield event.plain_result(result)

    @filter.command("logout", alias={"出场", "下机", "离场", "退场"})
    async def logout_cmd(self, event: AstrMessageEvent):
        """登出/结算；若退场的是偷偷上机玩家，则通过 OneBot API 撤回其发送的指令消息。"""
        self._remember_sender_name(event)
        sneaked = False

        async def run():
            nonlocal sneaked
            uid = self._target_from_optional_arg(event)
            result = await self.service.logout(uid)
            sneaked = self.sneak_login_enabled and result["session"].get("ENTRY_TYPE") == "sneak"
            label = self._at_label(event, uid)
            if sneaked:
                label = "未知玩家"
            return format_leave_billing(result, self.currency, label)

        result = await self._safe(run())
        if sneaked:
            await self._recall_onebot_message(event)
        yield event.plain_result(result)

    @filter.regex(r"^/?死给(?:\s|@|$)")
    async def force_logout_cmd(self, event: AstrMessageEvent):
        """管理员强制退场（忽略结算）：死给 @某人；未 @ 人时不响应"""
        if not self._is_admin(event):
            event.stop_event()
            return
        at_ids = self._at_ids(event)
        if not at_ids:
            event.stop_event()
            return

        uid = f"QQ:{at_ids[0]}"

        async def run():
            result = await self.service.force_logout(uid)
            label = self._at_label(event, uid)
            if self.sneak_login_enabled and result["session"].get("ENTRY_TYPE") == "sneak":
                label = "未知玩家"
            return f"已强制为用户 {label} 退场"

        event.stop_event()
        yield event.plain_result(await self._safe(run()))

    @filter.command("billing", alias={"账单", "b"})
    async def billing_cmd(self, event: AstrMessageEvent):
        """查看当前账单"""
        async def run():
            uid = self._target_from_optional_arg(event)
            result = await self.service.billing(uid)
            return format_billing(result, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("wallet", alias={"钱包"})
    async def wallet_cmd(self, event: AstrMessageEvent):
        """查看钱包"""
        async def run():
            uid = self._target_from_optional_arg(event)
            wallet = await self.service.wallet(uid, False)
            return format_wallet(wallet, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("items", alias={"背包"})
    async def items_cmd(self, event: AstrMessageEvent):
        """查看资产"""
        async def run():
            uid = self._target_from_optional_arg(event)
            assets = await self.service.user_assets(uid, True)
            mahjong_rank = await self.service.mahjong_rank(uid)
            return format_items(assets, self.currency, mahjong_rank)

        yield event.plain_result(await self._safe(run()))

    @filter.command("history", alias={"历史记录"})
    async def history_cmd(self, event: AstrMessageEvent):
        """查看自己的历史记录"""
        self._remember_sender_name(event)
        async def run():
            args = self._args(event)
            limit = int(args[0]) if args and args[0].isdigit() else 5
            sessions = await self.service.history(self._sender_uid(event), limit)
            return format_history(sessions, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("ahistory")
    async def ahistory_cmd(self, event: AstrMessageEvent):
        """管理员查看指定用户历史记录"""
        self._remember_sender_name(event)
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            if not args:
                raise ShinjukuError("用法：/ahistory <用户> [数量]")
            at_ids = self._at_ids(event)
            uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event, allow_self=False)
            self._at_label(event, uid)
            limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
            sessions = await self.service.history(uid, limit)
            return format_history(sessions, self.currency)

        yield event.plain_result(await self._safe(run()))

    @filter.command("list")
    async def list_cmd(self, event: AstrMessageEvent):
        """列出当前登录用户"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            users = await self.service.logged_in_users()
            if not users:
                return "当前没有登录用户。"
            lines = ["--- 当前登录 ---"]
            for user in users:
                sessions = user.get("sessions") or []
                is_sneak = bool(sessions and sessions[0].get("ENTRY_TYPE") == "sneak")
                if is_sneak and self.sneak_login_enabled:
                    binds_display = "未知玩家"
                else:
                    binds_display = ", ".join(f"{bind['type']}:{bind['bid']}" for bind in user.get("binds", [])) or "(无绑定)"
                marker = "（偷偷上机）" if is_sneak else ""
                lines.append(f"#{user['id']} {binds_display}{marker}")
            return "\n".join(lines)

        yield event.plain_result(await self._safe(run()))

    @filter.regex(r"^(?:j|xsj|新宿几|窝几|wj|新宿j)$")
    async def j_cmd(self, event: AstrMessageEvent):
        """查询当前店内人数"""
        self._remember_sender_name(event)
        async def run():
            users = await self.service.logged_in_users()
            return format_players(
                users,
                self.nicknames.snapshot(self._nickname_scope(event)),
                self.sneak_login_enabled,
            )

        yield event.plain_result(await self._safe(run()))

    @filter.regex(r"^定价表$")
    async def pricing_table_cmd(self, event: AstrMessageEvent):
        """发送当前定价表"""
        yield event.plain_result(format_pricing(self.service.billing_config, self.currency))

    @filter.command("redeem")
    async def redeem_cmd(self, event: AstrMessageEvent):
        """兑换已有兑换码"""
        async def run():
            args = self._args(event)
            if not args:
                raise ShinjukuError("用法：/redeem <兑换码>")
            result = await self.service.redeem(self._sender_uid(event), args[0])
            present = result.get("present") or {}
            assets = result.get("assets") or []
            lines = [f"兑换成功：{present.get('name') or args[0]}"]
            if assets:
                lines.append(f"已发放 {len(assets)} 项资产，可用 /items 查看。")
            return "\n".join(lines)

        yield event.plain_result(await self._safe(run()))

    @filter.command("add")
    async def add_cmd(self, event: AstrMessageEvent):
        """管理员给用户添加货币：/add @用户 金额"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if len(args) == 1 and at_ids:
                uid = f"QQ:{at_ids[0]}"
                amount = amount_to_cents(args[0])
            elif len(args) >= 2:
                uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event, allow_self=False)
                amount = amount_to_cents(args[-1])
            else:
                raise ShinjukuError("用法：/add @用户 金额")
            if amount <= 0:
                raise ShinjukuError("添加金额必须大于 0。")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.add_paid_currency(uid, amount, f"admin add by {self._sender_id(event)}")
            label = await self._masked_label(uid, self._at_label(event, uid))
            return (
                prefix +
                f"为用户 {label} 增加{self.currency}成功\n"
                f"增加前: {money(result['originalBalance'])}\n"
                f"增加后: {money(result['finalBalance'])}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("member")
    async def member_cmd(self, event: AstrMessageEvent):
        """管理员给群成员发放 30 天通行证：/member @成员"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if at_ids:
                uid = f"QQ:{at_ids[0]}"
            elif args:
                uid = self._normalize_user(args[0], event, allow_self=False)
            else:
                raise ShinjukuError("用法：/member @成员")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.add_pass(uid, 30, f"member grant by {self._sender_id(event)}")
            label = await self._masked_label(uid, self._at_label(event, uid))
            return (
                prefix +
                f"已为用户 {label} 发放 30 天通行证\n"
                f"到期时间: {date_time(result.get('expireAt'))}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("coupon")
    async def coupon_cmd(self, event: AstrMessageEvent):
        """管理员发放折扣优惠券：/coupon @用户 8 [天数]（8 表示 8 折，默认 30 天）"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if at_ids:
                if not args:
                    raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
                uid = f"QQ:{at_ids[0]}"
                nums = [arg for arg in args if not arg.startswith("@")]
                if not nums:
                    raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
                tenths = nums[0]
                days = int(nums[1]) if len(nums) > 1 else 30
            elif len(args) >= 2:
                uid = self._normalize_user(args[0], event, allow_self=False)
                tenths = args[1]
                days = int(args[2]) if len(args) > 2 else 30
            else:
                raise ShinjukuError("用法：/coupon @用户 折扣 [天数]")
            prefix = await self._ensure_registered(uid, self._at_label(event, uid))
            result = await self.service.grant_coupon(uid, tenths, days, f"coupon grant by {self._sender_id(event)}")
            label = result["asset"].get("name") or f"{result['discount_tenths']}折优惠券"
            user_label = await self._masked_label(uid, self._at_label(event, uid))
            return (
                prefix +
                f"已为用户 {user_label} 发放 {label}\n"
                f"有效期至: {date_time(result['userAsset'].get('expireAt'))}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("giftcode")
    async def giftcode_cmd(self, event: AstrMessageEvent):
        """管理员生成兑换码：/giftcode 礼包ID 货币数量 次数"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            if len(args) != 3:
                raise ShinjukuError("用法：/giftcode 礼包ID 货币数量 次数")
            try:
                present_id = int(args[0])
                amount = amount_to_cents(args[1])
                times = int(args[2])
            except ValueError:
                raise ShinjukuError("用法：/giftcode 礼包ID 货币数量 次数")
            result = await self.service.create_gift_code(present_id, amount, times)
            return (
                f"已生成兑换码：{result['code']}\n"
                f"礼包：{result['name']}（含 {money(result['currency_amount'])} {self.currency}）\n"
                f"可领取次数：{result['max_use_count']}\n"
                "发送 /redeem <兑换码> 即可领取"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("mj")
    async def mj_cmd(self, event: AstrMessageEvent):
        """管理员扣除用户货币：/mj @用户 金额"""
        async def run():
            if not self._is_admin(event):
                raise ShinjukuError("权限不足。")
            args = self._args(event)
            at_ids = self._at_ids(event)
            if len(args) == 1 and at_ids:
                uid = f"QQ:{at_ids[0]}"
                amount = amount_to_cents(args[0])
            elif len(args) >= 2:
                uid = f"QQ:{at_ids[0]}" if at_ids else self._normalize_user(args[0], event, allow_self=False)
                amount = amount_to_cents(args[-1])
            else:
                raise ShinjukuError("用法：/mj @用户 金额")
            if amount <= 0:
                raise ShinjukuError("扣费金额必须大于 0。")
            result = await self.service.charge_wallet(uid, amount, f"mj charge by {self._sender_id(event)}")
            return (
                f"MJ 扣费成功：-{money(amount)} {self.currency}\n"
                f"余额：{money(result['originalBalance'])} -> {money(result['finalBalance'])} {self.currency}"
            )

        yield event.plain_result(await self._safe(run()))

    @filter.command("开门")
    async def door_cmd(self, event: AstrMessageEvent):
        """自助开门：/开门 [7位验证码]（或『开门+验证码』粘连，或『开门』单独发送）"""
        self._remember_sender_name(event)
        async def run():
            if not self.self_open_door_enabled:
                return "请联系管理员开门！"
            uid = self._sender_uid(event)
            if await self.service.find_user(uid):
                wallet = await self.service.wallet(uid)
                points = int(wallet["points"]["available"] or 0)
                if points <= self.self_open_door_points_threshold:
                    return (
                        f"（积分需高于 {self.self_open_door_points_threshold}）"
                        "条件不满足，无法自助开门，请联系管理员开门"
                    )
            raw = (event.message_str or "").strip()
            text = raw.lstrip("/")
            m = re.match(r"^开门\s*(\d{7})\s*$", text)
            code = m.group(1) if m else None
            if code is None:
                args = self._args(event)
                for arg in args:
                    if re.fullmatch(r"\d{7}", arg):
                        code = arg
                        break
            status = await self.service.door_verify(uid, code)
            if status == "SUCCESS_FIRST":
                return "门已开，祝您游玩愉快！"
            if status == "SUCCESS_AGAIN":
                return "门已开！"
            if status == "NOT_PRESENT":
                return "人在哪呢，怎么就要我给开门？"
            if status == "WRONG_CODE":
                return "验证码不对啦，不能给你开门哦！请检查入场时发送的验证码！"
            if status == "STOLEN_CODE":
                return "验证码是你的吗就乱用，明明都不在门口还想乱用别人验证码，叉出去！"
            if status == "NO_CODE_PRESENT":
                return "笨蛋，开门要这样用：/开门 [7位数验证码]"
            if status == "NO_CODE_OFFLINE":
                return "要先进场才会有验证码啦！"
            return "开门失败：未知错误。"

        yield event.plain_result(await self._safe(run()))
