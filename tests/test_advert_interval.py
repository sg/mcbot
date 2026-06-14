#!/usr/bin/env python3
"""Periodic flood-advert interval: config seeds the DB on first run, then the
DB value is authoritative and runtime-managed (persisted, validated, audited).

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_advert_interval.py
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402
from management import MgmtError  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def make_bot(*, advert_interval_hours=0):
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    cfg.advert_interval_hours = advert_interval_hours
    log = mcbot.logging.getLogger("test-advert")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='advert_interval_hours'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty():
    print("test_seed_from_config_when_db_empty")
    bot = make_bot(advert_interval_hours=3)
    await bot._load_advert_interval()
    check(bot.advert_interval_hours == 3, "seeded interval from config")
    check(await meta(bot) == "3", "config value written to bot_meta")
    bot.db.close()


async def test_db_is_authoritative_over_config():
    print("test_db_is_authoritative_over_config")
    bot = make_bot(advert_interval_hours=3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('advert_interval_hours','5')"
    )
    await bot._load_advert_interval()
    check(bot.advert_interval_hours == 5, "DB value wins over config")
    bot.db.close()


async def test_set_persists_and_resets_schedule():
    print("test_set_persists_and_resets_schedule")
    bot = make_bot()
    bot._last_flood_advert = 0.0
    await bot.set_advert_interval(7)
    check(bot.advert_interval_hours == 7, "in-memory value updated")
    check(await meta(bot) == "7", "value persisted to bot_meta")
    check(bot._last_flood_advert > 0, "schedule baseline reset to now")
    await bot.set_advert_interval(0)
    check(bot.advert_interval_hours == 0 and await meta(bot) == "0", "disable persists")
    bot.db.close()


async def test_mgmt_validation_and_audit():
    print("test_mgmt_validation_and_audit")
    bot = make_bot()
    r = await bot.mgmt.radio_set_advert_interval(4)
    check(r == {"interval_hours": 4}, "valid set returns interval")
    check(await meta(bot) == "4", "mgmt set persisted")
    g = await bot.mgmt.radio_advert_interval()
    check(g == {"interval_hours": 4}, "get returns current interval")
    for bad in (-1, 169, "abc"):
        try:
            await bot.mgmt.radio_set_advert_interval(bad)
            check(False, f"invalid interval {bad!r} should raise")
        except MgmtError:
            check(True, f"invalid interval {bad!r} rejected")
    # audit row written for the successful set
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='radio.advert_interval' "
        "ORDER BY id DESC LIMIT 1"
    )
    check(row is not None and "interval=4h" in row["detail"], "set is audited")
    bot.db.close()


async def test_send_advert_anchors_schedule():
    print("test_send_advert_anchors_schedule")
    bot = make_bot()

    async def fake_send_advert(flood=False):
        return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_advert=fake_send_advert))
    bot._last_flood_advert = 0.0
    await bot.send_advert(flood=False)
    check(bot._last_flood_advert == 0.0, "zero-hop advert does not anchor schedule")
    await bot.send_advert(flood=True)
    check(bot._last_flood_advert > 0, "flood advert anchors schedule")
    bot.db.close()


async def main():
    for t in (
        test_seed_from_config_when_db_empty,
        test_db_is_authoritative_over_config,
        test_set_persists_and_resets_schedule,
        test_mgmt_validation_and_audit,
        test_send_advert_anchors_schedule,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
