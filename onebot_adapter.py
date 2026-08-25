"""Compatibility adapter for OneBot actions used by the plugin."""

from __future__ import annotations

from typing import Any


class OneBotAdapter:
    def __init__(self, logger: Any):
        self.logger = logger

    @staticmethod
    async def call_action(client: Any, action: str, **kwargs: Any) -> Any:
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

    async def recall_message(self, event: Any) -> None:
        """Recall a command message when supported; failures are non-fatal."""
        try:
            platform_name = event.get_platform_name()
        except Exception:
            platform_name = (
                getattr(getattr(event, "platform_meta", None), "name", "") or ""
            )
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
                    await self.call_action(
                        client, "delete_msg", message_id=candidate
                    )
                    self.logger.info(f"新宿：已撤回偷偷上机指令消息 {message_id}")
                    return
                except Exception as exc:
                    last_error = exc
                    self.logger.debug(
                        f"新宿撤回指令消息失败（{candidate}）: {exc}"
                    )
            self.logger.error(f"新宿：撤回偷偷上机指令消息失败: {last_error}")
        except Exception as exc:
            self.logger.error(f"新宿：撤回偷偷上机指令消息失败: {exc}")
