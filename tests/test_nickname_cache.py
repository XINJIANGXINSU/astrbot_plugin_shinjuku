from adapters.nickname_cache import NicknameCache


def test_nicknames_are_isolated_by_scope():
    cache = NicknameCache()
    cache.set("qq:group:100", "12345", "一群昵称")
    cache.set("qq:group:200", "12345", "二群昵称")

    assert cache.get("qq:group:100", "12345") == "一群昵称"
    assert cache.get("qq:group:200", "12345") == "二群昵称"
    assert cache.snapshot("qq:group:100") == {"12345": "一群昵称"}


def test_nickname_expires_after_three_days_and_can_refresh():
    clock = {"now": 0.0}
    cache = NicknameCache(ttl_seconds=3 * 24 * 60 * 60, clock=lambda: clock["now"])
    cache.set("qq:group:100", "12345", "旧昵称")

    clock["now"] = 3 * 24 * 60 * 60 - 1
    assert cache.get("qq:group:100", "12345") == "旧昵称"

    clock["now"] += 1
    assert cache.get("qq:group:100", "12345") is None

    cache.set("qq:group:100", "12345", "新昵称")
    assert cache.get("qq:group:100", "12345") == "新昵称"


def test_capacity_evicts_the_oldest_entry():
    clock = {"now": 0.0}
    cache = NicknameCache(max_entries=2, clock=lambda: clock["now"])
    cache.set("qq:group:100", "1", "一")
    clock["now"] += 1
    cache.set("qq:group:100", "2", "二")
    clock["now"] += 1
    cache.set("qq:group:100", "3", "三")

    assert len(cache) == 2
    assert cache.get("qq:group:100", "1") is None
    assert cache.get("qq:group:100", "2") == "二"
    assert cache.get("qq:group:100", "3") == "三"
