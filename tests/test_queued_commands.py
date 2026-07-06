"""Offline-backlog handling: messages the radio queued while the bot was down
are drained on startup (everything before the first NO_MORE_MSGS); commands in
them are skipped unless the command opts in via process_queued."""

import time
from types import SimpleNamespace

import mcbot


def _ctx(bot, *, from_queue=False):
    return mcbot.CommandContext(
        sender_name="bob", sender_pubkey="ab" * 32,
        sender_pubkey_prefix="abababababab", message_text="!ping",
        is_dm=True, channel_idx=None, channel_name=None, path=None,
        path_len=None, path_hash_mode=None, snr=None, rssi=None,
        sender_timestamp=None, bot=bot, from_queue=from_queue,
    )


def _wire_dispatch(bot, *, process_queued=False):
    """Stub loader/auth/reply so _dispatch_command runs; returns sent texts."""
    sent = []

    async def fake_handle(ctx):
        return "pong"

    cs = SimpleNamespace(
        name="ping", cooldown_default=0, allowed_channels=None,
        allow_dm=True, dm_only=False, process_queued=process_queued,
        handle=fake_handle,
    )
    bot.loader = SimpleNamespace(match=lambda text: cs)

    async def fake_is_blocked(pk):
        return False

    async def fake_is_authorized(pk, name):
        return True

    async def fake_send_reply(ctx, text):
        sent.append(text)

    bot.is_user_blocked = fake_is_blocked
    bot.is_authorized_for_command = fake_is_authorized
    bot.send_reply = fake_send_reply
    return sent


# ----- drain-window state machine ----------------------------------------
async def test_drain_window_lifecycle(bot_factory):
    bot = bot_factory()
    # before run() opens the window, messages are live (tests/direct drive)
    assert not bot._is_from_queue(), "drained by default"
    bot._begin_queue_drain()
    assert bot._is_from_queue(), "window open: messages are backlog"
    await bot._on_no_more_msgs(SimpleNamespace(payload={}))
    assert not bot._is_from_queue(), "NO_MORE_MSGS closes the window"


async def test_drain_deadline_fails_open(bot_factory):
    bot = bot_factory()
    bot._begin_queue_drain()
    bot._drain_deadline = time.monotonic() - 1  # marker never arrived
    assert not bot._is_from_queue(), "deadline passed -> treated as live"
    assert bot._radio_queue_drained, "failsafe latches drained"


# ----- dispatch gate ------------------------------------------------------
async def test_queued_command_skipped_by_default(bot_factory):
    bot = bot_factory()
    sent = _wire_dispatch(bot)
    await bot._dispatch_command(_ctx(bot, from_queue=True))
    assert sent == [], "backlog command ignored when process_queued off"
    assert bot._queued_cmds_skipped == 1, "skip counted for the drain log"


async def test_live_command_processed(bot_factory):
    bot = bot_factory()
    sent = _wire_dispatch(bot)
    await bot._dispatch_command(_ctx(bot, from_queue=False))
    assert sent == ["pong"], "live command unaffected"


async def test_script_opt_in_processes_backlog(bot_factory):
    bot = bot_factory()
    sent = _wire_dispatch(bot, process_queued=True)
    await bot._dispatch_command(_ctx(bot, from_queue=True))
    assert sent == ["pong"], "PROCESS_QUEUED=True script default honored"


async def test_db_row_overrides_script_default(bot_factory):
    bot = bot_factory()
    sent = _wire_dispatch(bot, process_queued=False)
    await bot.db.execute(
        "INSERT INTO command_config (command, enabled, process_queued) "
        "VALUES ('ping', 1, 1)"
    )
    await bot._dispatch_command(_ctx(bot, from_queue=True))
    assert sent == ["pong"], "DB process_queued=1 overrides script False"
    # and the DB can turn it back off over a script True
    sent2 = _wire_dispatch(bot, process_queued=True)
    await bot.db.execute(
        "UPDATE command_config SET process_queued=0 WHERE command='ping'"
    )
    await bot._dispatch_command(_ctx(bot, from_queue=True))
    assert sent2 == [], "DB process_queued=0 overrides script True"


# ----- config plumbing ----------------------------------------------------
async def test_mgmt_patch_and_migration(bot_factory):
    bot = bot_factory()
    # the column exists on a fresh schema (and _migrate adds it to old DBs)
    cols = {
        r[1] for r in bot.db.conn.execute(
            "PRAGMA table_info(command_config)"
        ).fetchall()
    }
    assert "process_queued" in cols
    await bot.db.execute(
        "INSERT INTO command_config (command, enabled) VALUES ('ping', 1)"
    )
    r = await bot.mgmt.command_config_update("ping", {"process_queued": True})
    assert r["process_queued"] == 1, "mgmt PATCH accepts the new flag"


async def test_migration_adds_column_to_old_db(bot_factory, tmp_path):
    import sqlite3
    # build a pre-upgrade DB whose command_config lacks the column
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE command_config (command TEXT PRIMARY KEY, "
        "enabled INTEGER DEFAULT 1, cooldown_seconds INTEGER, "
        "allowed_channels TEXT, triggers TEXT, description TEXT, "
        "allow_dm INTEGER, dm_only INTEGER)"
    )
    conn.execute("INSERT INTO command_config (command) VALUES ('ping')")
    conn.commit()
    conn.close()
    bot = bot_factory(db_path=p)
    row = await bot.db.fetchone(
        "SELECT process_queued FROM command_config WHERE command='ping'"
    )
    assert row is not None and row["process_queued"] is None, \
        "migration added the column; existing rows default to NULL"
