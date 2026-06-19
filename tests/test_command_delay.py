#!/usr/bin/env python3
"""Command-response delay: config seeds the DB on first run, then the DB value
is authoritative and runtime-managed (clamped, validated, audited). The delay
is applied in _dispatch_command AFTER the handler runs and BEFORE the reply is
sent.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_command_delay.py
"""

import asyncio
import sys
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


def make_bot(*, command_delay=0.0):
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    cfg.command_delay = command_delay
    log = mcbot.logging.getLogger("test-cmd-delay")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='command_delay'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty():
    print("test_seed_from_config_when_db_empty")
    bot = make_bot(command_delay=0.3)
    await bot._load_command_delay()
    check(bot.command_delay == 0.3, "seeded delay from config")
    check(await meta(bot) == "0.3", "config value written to bot_meta")
    bot.db.close()


async def test_db_is_authoritative_over_config():
    print("test_db_is_authoritative_over_config")
    bot = make_bot(command_delay=0.3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('command_delay','1.5')"
    )
    await bot._load_command_delay()
    check(bot.command_delay == 1.5, "DB value wins over config")
    bot.db.close()


async def test_set_clamps_and_persists():
    print("test_set_clamps_and_persists")
    bot = make_bot()
    await bot.set_command_delay(0.5)
    check(bot.command_delay == 0.5, "in-memory value updated")
    check(await meta(bot) == "0.5", "value persisted to bot_meta")
    await bot.set_command_delay(5.0)
    check(bot.command_delay == 2.0, "clamps above 2.0")
    await bot.set_command_delay(-1)
    check(bot.command_delay == 0.0, "clamps below 0 (disabled)")
    bot.db.close()


async def test_mgmt_validation_and_audit():
    print("test_mgmt_validation_and_audit")
    bot = make_bot()
    r = await bot.mgmt.set_command_delay(1.0)
    check(r == {"delay": 1.0}, "valid set returns delay")
    g = await bot.mgmt.command_delay()
    check(g == {"delay": 1.0}, "get returns current delay")
    r0 = await bot.mgmt.set_command_delay(0)
    check(r0 == {"delay": 0.0}, "0 disables (accepted)")
    for bad in (0.05, 2.1, -0.5, "abc"):
        try:
            await bot.mgmt.set_command_delay(bad)
            check(False, f"invalid delay {bad!r} should raise")
        except MgmtError:
            check(True, f"invalid delay {bad!r} rejected")
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='command.delay' "
        "ORDER BY id DESC LIMIT 1"
    )
    check(row is not None and "delay=" in row["detail"], "set is audited")
    bot.db.close()


def _ctx(bot):
    return mcbot.CommandContext(
        sender_name="bob", sender_pubkey="ab" * 32,
        sender_pubkey_prefix="abababababab", message_text="!ping",
        is_dm=True, channel_idx=None, channel_name=None, path=None,
        path_len=None, path_hash_mode=None, snr=None, rssi=None,
        sender_timestamp=None, bot=bot,
    )


async def _run_dispatch(bot):
    """Drive _dispatch_command with stubs, returning the ordered event log and
    the list of sleep durations (asyncio.sleep is patched to not actually wait)."""
    events = []

    async def fake_handle(ctx):
        events.append(("handle", None))
        return "pong"

    cs = SimpleNamespace(
        name="ping", cooldown_default=0, allowed_channels=None,
        allow_dm=True, dm_only=False, handle=fake_handle,
    )
    bot.loader = SimpleNamespace(match=lambda text: cs)

    async def fake_is_blocked(pk):
        return False

    async def fake_is_authorized(pk, name):
        return True

    async def fake_send_reply(ctx, text):
        events.append(("send", text))

    bot.is_user_blocked = fake_is_blocked
    bot.is_authorized_for_command = fake_is_authorized
    bot.send_reply = fake_send_reply

    sleeps = []
    real_sleep = mcbot.asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        events.append(("sleep", d))

    mcbot.asyncio.sleep = fake_sleep
    try:
        await bot._dispatch_command(_ctx(bot))
    finally:
        mcbot.asyncio.sleep = real_sleep
    return events, sleeps


async def test_dispatch_delays_after_handler_before_send():
    print("test_dispatch_delays_after_handler_before_send")
    bot = make_bot()
    await bot.set_command_delay(0.5)
    events, sleeps = await _run_dispatch(bot)
    check(sleeps == [0.5], f"slept once for command_delay (got {sleeps})")
    check([e[0] for e in events] == ["handle", "sleep", "send"],
          f"delay applied after handler, before send (got {events})")
    bot.db.close()


async def test_dispatch_no_delay_when_disabled():
    print("test_dispatch_no_delay_when_disabled")
    bot = make_bot()  # delay defaults to 0
    events, sleeps = await _run_dispatch(bot)
    check(sleeps == [], "no sleep when delay disabled")
    check([e[0] for e in events] == ["handle", "send"],
          f"handler then send, no delay (got {events})")
    bot.db.close()


async def main():
    for t in (
        test_seed_from_config_when_db_empty,
        test_db_is_authoritative_over_config,
        test_set_clamps_and_persists,
        test_mgmt_validation_and_audit,
        test_dispatch_delays_after_handler_before_send,
        test_dispatch_no_delay_when_disabled,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
