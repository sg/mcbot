"""Channel no-repeat retry budget: config seeds the DB on first run, then the
DB value is authoritative and runtime-managed (clamped 0–5, validated, audited)."""

import pytest

from management import MgmtError


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='channel_retry_max'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty(bot_factory):
    bot = bot_factory(channel_retry_max=3)
    await bot.load_runtime_settings()
    assert bot.channel_retry_max == 3, "seeded retries from config"
    assert await meta(bot) == "3", "config value written to bot_meta"


async def test_db_is_authoritative_over_config(bot_factory):
    bot = bot_factory(channel_retry_max=3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('channel_retry_max','1')"
    )
    await bot.load_runtime_settings()
    assert bot.channel_retry_max == 1, "DB value wins over config"


async def test_set_clamps_and_persists(bot_factory):
    bot = bot_factory()
    await bot.set_runtime_setting("channel_retry_max", 4)
    assert bot.channel_retry_max == 4, "in-memory value updated"
    assert await meta(bot) == "4", "value persisted to bot_meta"
    await bot.set_runtime_setting("channel_retry_max", 99)
    assert bot.channel_retry_max == 5, "clamps above 5"
    await bot.set_runtime_setting("channel_retry_max", -1)
    assert bot.channel_retry_max == 0, "clamps below 0 (disabled)"


async def test_mgmt_validation_and_audit(bot_factory):
    bot = bot_factory()
    r = await bot.mgmt.setting_set("channel_retry_max", 3)
    assert r == {"key": "channel_retry_max", "value": 3}
    g = await bot.mgmt.setting_get("channel_retry_max")
    assert g == {"key": "channel_retry_max", "value": 3}
    r0 = await bot.mgmt.setting_set("channel_retry_max", 0)
    assert r0 == {"key": "channel_retry_max", "value": 0}, "0 disables (accepted)"
    for bad in (-1, 6, "abc"):
        with pytest.raises(MgmtError):
            await bot.mgmt.setting_set("channel_retry_max", bad)
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='command.retry' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and "retries=" in row["detail"], "set is audited"
