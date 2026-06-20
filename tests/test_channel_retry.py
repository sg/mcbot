#!/usr/bin/env python3
"""Channel no-repeat retry budget: config seeds the DB on first run, then the
DB value is authoritative and runtime-managed (clamped 0–5, validated, audited).

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_channel_retry.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcbot  # noqa: E402
from management import MgmtError  # noqa: E402

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def make_bot(*, channel_retry_max=2):
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    cfg.channel_retry_max = channel_retry_max
    log = mcbot.logging.getLogger("test-chan-retry")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='channel_retry_max'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty():
    print("test_seed_from_config_when_db_empty")
    bot = make_bot(channel_retry_max=3)
    await bot._load_channel_retry_max()
    check(bot.channel_retry_max == 3, "seeded retries from config")
    check(await meta(bot) == "3", "config value written to bot_meta")
    bot.db.close()


async def test_db_is_authoritative_over_config():
    print("test_db_is_authoritative_over_config")
    bot = make_bot(channel_retry_max=3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('channel_retry_max','1')"
    )
    await bot._load_channel_retry_max()
    check(bot.channel_retry_max == 1, "DB value wins over config")
    bot.db.close()


async def test_set_clamps_and_persists():
    print("test_set_clamps_and_persists")
    bot = make_bot()
    await bot.set_channel_retry_max(4)
    check(bot.channel_retry_max == 4, "in-memory value updated")
    check(await meta(bot) == "4", "value persisted to bot_meta")
    await bot.set_channel_retry_max(99)
    check(bot.channel_retry_max == 5, "clamps above 5")
    await bot.set_channel_retry_max(-1)
    check(bot.channel_retry_max == 0, "clamps below 0 (disabled)")
    bot.db.close()


async def test_mgmt_validation_and_audit():
    print("test_mgmt_validation_and_audit")
    bot = make_bot()
    r = await bot.mgmt.set_channel_retry(3)
    check(r == {"retries": 3}, "valid set returns retries")
    g = await bot.mgmt.channel_retry()
    check(g == {"retries": 3}, "get returns current retries")
    r0 = await bot.mgmt.set_channel_retry(0)
    check(r0 == {"retries": 0}, "0 disables (accepted)")
    for bad in (-1, 6, "abc"):
        try:
            await bot.mgmt.set_channel_retry(bad)
            check(False, f"invalid retries {bad!r} should raise")
        except MgmtError:
            check(True, f"invalid retries {bad!r} rejected")
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='command.retry' "
        "ORDER BY id DESC LIMIT 1"
    )
    check(row is not None and "retries=" in row["detail"], "set is audited")
    bot.db.close()


async def main():
    for t in (
        test_seed_from_config_when_db_empty,
        test_db_is_authoritative_over_config,
        test_set_clamps_and_persists,
        test_mgmt_validation_and_audit,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
