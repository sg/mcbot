#!/usr/bin/env python3
"""Runtime-settings registry: generic list/get/set semantics shared by all
tunables (per-setting behavior is covered by the per-setting test files).

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_settings_registry.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402
from management import MgmtError  # noqa: E402
from settings import SETTINGS  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def make_bot():
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    log = mcbot.logging.getLogger("test-settings")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


async def test_list_covers_registry():
    print("test_list_covers_registry")
    bot = make_bot()
    rows = await bot.mgmt.setting_list()
    check({r["key"] for r in rows} == set(SETTINGS), "one row per registry entry")
    for r in rows:
        check(
            all(k in r for k in ("value", "label", "group", "max", "step", "description")),
            f"{r['key']} carries UI metadata",
        )
    bot.db.close()


async def test_unknown_key_rejected():
    print("test_unknown_key_rejected")
    bot = make_bot()
    for call in (bot.mgmt.setting_get("nope"), bot.mgmt.setting_set("nope", 1)):
        try:
            await call
            check(False, "unknown key should raise")
        except MgmtError as e:
            check(e.code == "not_found", "unknown key raises not_found")
    bot.db.close()


async def test_int_setting_rejects_fraction():
    print("test_int_setting_rejects_fraction")
    bot = make_bot()
    try:
        await bot.mgmt.setting_set("channel_retry_max", 2.5)
        check(False, "fractional value for int setting should raise")
    except MgmtError:
        check(True, "fractional value for int setting rejected")
    bot.db.close()


async def test_string_input_accepted():
    print("test_string_input_accepted")
    # '!adm setting <key> <value>' passes the raw token through
    bot = make_bot()
    r = await bot.mgmt.setting_set("command_delay", "0.5")
    check(r == {"key": "command_delay", "value": 0.5}, "string float parsed")
    r = await bot.mgmt.setting_set("advert_interval_hours", "12")
    check(r == {"key": "advert_interval_hours", "value": 12}, "string int parsed")
    bot.db.close()


async def test_load_seeds_every_setting():
    print("test_load_seeds_every_setting")
    bot = make_bot()
    await bot.load_runtime_settings()
    for key in SETTINGS:
        row = await bot.db.fetchone(
            "SELECT value FROM bot_meta WHERE key=?", (key,)
        )
        check(row is not None, f"{key} seeded into bot_meta")
    bot.db.close()


async def main():
    for t in (
        test_list_covers_registry,
        test_unknown_key_rejected,
        test_int_setting_rejects_fraction,
        test_string_input_accepted,
        test_load_seeds_every_setting,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
