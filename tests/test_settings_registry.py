"""Runtime-settings registry: generic list/get/set semantics shared by all
tunables (per-setting behavior is covered by the per-setting test files)."""

import pytest

from management import MgmtError
from settings import SETTINGS


async def test_list_covers_registry(bot_factory):
    bot = bot_factory()
    rows = await bot.mgmt.setting_list()
    assert {r["key"] for r in rows} == set(SETTINGS), "one row per registry entry"
    for r in rows:
        assert all(
            k in r for k in ("value", "label", "group", "max", "step", "description")
        ), f"{r['key']} carries UI metadata"


async def test_unknown_key_rejected(bot_factory):
    bot = bot_factory()
    with pytest.raises(MgmtError) as e1:
        await bot.mgmt.setting_get("nope")
    assert e1.value.code == "not_found"
    with pytest.raises(MgmtError) as e2:
        await bot.mgmt.setting_set("nope", 1)
    assert e2.value.code == "not_found"


async def test_int_setting_rejects_fraction(bot_factory):
    bot = bot_factory()
    with pytest.raises(MgmtError):
        await bot.mgmt.setting_set("channel_retry_max", 2.5)


async def test_string_input_accepted(bot_factory):
    # '!adm setting <key> <value>' passes the raw token through
    bot = bot_factory()
    r = await bot.mgmt.setting_set("command_delay", "0.5")
    assert r == {"key": "command_delay", "value": 0.5}, "string float parsed"
    r = await bot.mgmt.setting_set("advert_interval_hours", "12")
    assert r == {"key": "advert_interval_hours", "value": 12}, "string int parsed"


async def test_load_seeds_every_setting(bot_factory):
    bot = bot_factory()
    await bot.load_runtime_settings()
    for key in SETTINGS:
        row = await bot.db.fetchone(
            "SELECT value FROM bot_meta WHERE key=?", (key,)
        )
        assert row is not None, f"{key} seeded into bot_meta"
