from core.settings import PluginSettings


def test_plugin_settings_apply_defaults():
    settings = PluginSettings.from_config({}, "/tmp/default.db")

    assert settings.database_path == "/tmp/default.db"
    assert settings.currency == "馕"
    assert settings.points_per_amount == 10
    assert settings.max_active_checkcodes == 20
    assert settings.self_open_door_enabled is True
    assert settings.login_grace_minutes == 3
    assert settings.sneak_login_enabled is False
    assert settings.billing == {}


def test_plugin_settings_preserve_supported_zero_values():
    settings = PluginSettings.from_config(
        {
            "points_per_amount": 0,
            "login_grace_minutes": 0,
            "self_open_door_points_threshold": 0,
            "sneak_login_points_threshold": 0,
        },
        "/tmp/default.db",
    )

    assert settings.points_per_amount == 0
    assert settings.login_grace_minutes == 0
    assert settings.self_open_door_points_threshold == 0
    assert settings.sneak_login_points_threshold == 0


def test_plugin_settings_normalize_invalid_values_and_copy_collections():
    billing = {"day_price": 12}
    settings = PluginSettings.from_config(
        {
            "database_path": "/tmp/custom.db",
            "admins": [123, "456"],
            "points_per_amount": "invalid",
            "max_active_checkcodes": -5,
            "login_grace_minutes": -1,
            "self_open_door_enabled": False,
            "sneak_login_enabled": True,
            "billing": billing,
        },
        "/tmp/default.db",
    )
    billing["day_price"] = 99

    assert settings.database_path == "/tmp/custom.db"
    assert settings.admins == frozenset({"123", "456"})
    assert settings.points_per_amount == 10
    assert settings.max_active_checkcodes == 1
    assert settings.login_grace_minutes == 0
    assert settings.self_open_door_enabled is False
    assert settings.sneak_login_enabled is True
    assert settings.billing == {"day_price": 12}
