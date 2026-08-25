from event_adapter import EventAdapter
from nickname_cache import NicknameCache


class AtComponent:
    def __init__(self, qq: str, name: str = ""):
        self.qq = qq
        self.name = name


class FakeEvent:
    def __init__(self, message_str: str = "/wallet", messages=None):
        self.message_str = message_str
        self._messages = messages or []

    def get_sender_id(self):
        return "12345"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "67890"

    def get_messages(self):
        return self._messages

    def get_sender_name(self):
        return "Alice"


def test_event_adapter_normalizes_command_arguments_and_users():
    adapter = EventAdapter(NicknameCache())
    event = FakeEvent('/coupon "@Alice" 8 30')

    assert adapter.args(event) == ["@Alice", "8", "30"]
    assert adapter.sender_uid(event) == "QQ:12345"
    assert adapter.normalize_user("Alice(54321)", event) == "QQ:54321"
    assert adapter.normalize_user(None, event) == "QQ:12345"


def test_event_adapter_scopes_and_remembers_nicknames():
    cache = NicknameCache()
    adapter = EventAdapter(cache)
    event = FakeEvent()

    adapter.remember_sender_name(event)

    scope = "aiocqhttp:group:67890"
    assert adapter.nickname_scope(event) == scope
    assert cache.get(scope, "12345") == "Alice"


def test_event_adapter_reads_at_target_and_label():
    cache = NicknameCache()
    adapter = EventAdapter(cache)
    event = FakeEvent(messages=[AtComponent("54321", "Bob")])

    assert adapter.at_ids(event) == ["54321"]
    assert adapter.at_label(event, "QQ:54321") == "Bob (54321)"
