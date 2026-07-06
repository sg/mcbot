"""Command-response delay: config seeds the DB on first run, then the DB value
is authoritative and runtime-managed (clamped, validated, audited). The delay
is applied in _dispatch_command AFTER the handler runs and BEFORE the reply is
sent."""

import asyncio
from types import SimpleNamespace

import pytest

import mcbot
from management import MgmtError


async def meta(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='command_delay'"
    )
    return row["value"] if row else None


async def test_seed_from_config_when_db_empty(bot_factory):
    bot = bot_factory(command_delay=0.3)
    await bot.load_runtime_settings()
    assert bot.command_delay == 0.3, "seeded delay from config"
    assert await meta(bot) == "0.3", "config value written to bot_meta"


async def test_db_is_authoritative_over_config(bot_factory):
    bot = bot_factory(command_delay=0.3)
    await bot.db.execute(
        "INSERT INTO bot_meta(key,value) VALUES('command_delay','1.5')"
    )
    await bot.load_runtime_settings()
    assert bot.command_delay == 1.5, "DB value wins over config"


async def test_set_clamps_and_persists(bot_factory):
    bot = bot_factory()
    await bot.set_runtime_setting("command_delay", 0.5)
    assert bot.command_delay == 0.5, "in-memory value updated"
    assert await meta(bot) == "0.5", "value persisted to bot_meta"
    await bot.set_runtime_setting("command_delay", 5.0)
    assert bot.command_delay == 2.0, "clamps above 2.0"
    await bot.set_runtime_setting("command_delay", -1)
    assert bot.command_delay == 0.0, "clamps below 0 (disabled)"


async def test_mgmt_validation_and_audit(bot_factory):
    bot = bot_factory()
    r = await bot.mgmt.setting_set("command_delay", 1.0)
    assert r == {"key": "command_delay", "value": 1.0}
    g = await bot.mgmt.setting_get("command_delay")
    assert g == {"key": "command_delay", "value": 1.0}
    r0 = await bot.mgmt.setting_set("command_delay", 0)
    assert r0 == {"key": "command_delay", "value": 0.0}, "0 disables (accepted)"
    for bad in (0.05, 2.1, -0.5, "abc"):
        with pytest.raises(MgmtError):
            await bot.mgmt.setting_set("command_delay", bad)
    row = await bot.db.fetchone(
        "SELECT detail FROM bot_audit_log WHERE action='command.delay' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and "delay=" in row["detail"], "set is audited"


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
        allow_dm=True, dm_only=False, process_queued=False,
        handle=fake_handle,
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
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        events.append(("sleep", d))

    asyncio.sleep = fake_sleep
    try:
        await bot._dispatch_command(_ctx(bot))
    finally:
        asyncio.sleep = real_sleep
    return events, sleeps


async def test_dispatch_delays_after_handler_before_send(bot_factory):
    bot = bot_factory()
    await bot.set_runtime_setting("command_delay", 0.5)
    events, sleeps = await _run_dispatch(bot)
    assert sleeps == [0.5], f"slept once for command_delay (got {sleeps})"
    assert [e[0] for e in events] == ["handle", "sleep", "send"], \
        f"delay applied after handler, before send (got {events})"


async def test_dispatch_no_delay_when_disabled(bot_factory):
    bot = bot_factory()  # delay defaults to 0
    events, sleeps = await _run_dispatch(bot)
    assert sleeps == [], "no sleep when delay disabled"
    assert [e[0] for e in events] == ["handle", "send"], \
        f"handler then send, no delay (got {events})"
