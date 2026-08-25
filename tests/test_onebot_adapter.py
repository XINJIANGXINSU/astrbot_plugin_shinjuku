import asyncio

from onebot_adapter import OneBotAdapter


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def debug(self, message):
        self.messages.append(("debug", message))

    def error(self, message):
        self.messages.append(("error", message))


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"status": "ok"}


class MessageObject:
    message_id = "123"


class FakeEvent:
    def __init__(self, client):
        self.bot = client
        self.message_obj = MessageObject()

    def get_platform_name(self):
        return "aiocqhttp"


def test_onebot_adapter_calls_direct_client_action():
    async def run():
        client = FakeClient()
        result = await OneBotAdapter.call_action(
            client, "delete_msg", message_id=123
        )

        assert result == {"status": "ok"}
        assert client.calls == [("delete_msg", {"message_id": 123})]

    asyncio.run(run())


def test_onebot_adapter_recalls_aiocqhttp_message():
    async def run():
        logger = FakeLogger()
        client = FakeClient()
        adapter = OneBotAdapter(logger)

        await adapter.recall_message(FakeEvent(client))

        assert client.calls == [("delete_msg", {"message_id": 123})]
        assert logger.messages[0][0] == "info"

    asyncio.run(run())
