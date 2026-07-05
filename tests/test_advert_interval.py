"""Periodic flood-advert interval: config seeds the DB on first run, then the
DB value is authoritative and runtime-managed (persisted, validated, audited)."""

from types import SimpleNamespace

import pytest

from management import MgmtError


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='advert_interval_hours'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty(bot_factory):
    bot = bot_factory(advert_interval_hours=3)
    await bot.load_runtime_settings()
    assert bot.advert_interval_hours == 3, "seeded interval from config"
    assert await meta(bot) == "3", "config value written to bot_meta"


async def test_db_is_authoritative_over_config(bot_factory):
    bot = bot_factory(advert_interval_hours=3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('advert_interval_hours','5')"
    )
    await bot.load_runtime_settings()
    assert bot.advert_interval_hours == 5, "DB value wins over config"


async def test_set_persists_and_resets_schedule(bot_factory):
    bot = bot_factory()
    bot._last_flood_advert = 0.0
    await bot.set_runtime_setting("advert_interval_hours", 7)
    assert bot.advert_interval_hours == 7, "in-memory value updated"
    assert await meta(bot) == "7", "value persisted to bot_meta"
    assert bot._last_flood_advert > 0, "schedule baseline reset to now"
    await bot.set_runtime_setting("advert_interval_hours", 0)
    assert bot.advert_interval_hours == 0 and await meta(bot) == "0", "disable persists"


async def test_mgmt_validation_and_audit(bot_factory):
    bot = bot_factory()
    r = await bot.mgmt.setting_set("advert_interval_hours", 4)
    assert r == {"key": "advert_interval_hours", "value": 4}
    assert await meta(bot) == "4", "mgmt set persisted"
    g = await bot.mgmt.setting_get("advert_interval_hours")
    assert g == {"key": "advert_interval_hours", "value": 4}
    for bad in (-1, 169, "abc"):
        with pytest.raises(MgmtError):
            await bot.mgmt.setting_set("advert_interval_hours", bad)
    # audit row written for the successful set
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='radio.advert_interval' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and "interval=4h" in row["detail"], "set is audited"


async def test_send_advert_anchors_schedule(bot_factory):
    bot = bot_factory()

    async def fake_send_advert(flood=False):
        return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_advert=fake_send_advert))
    bot._last_flood_advert = 0.0
    await bot.send_advert(flood=False)
    assert bot._last_flood_advert == 0.0, "zero-hop advert does not anchor schedule"
    await bot.send_advert(flood=True)
    assert bot._last_flood_advert > 0, "flood advert anchors schedule"
