"""Radio-link recovery: disconnect-triggered full restart, post-reconnect
resync, the liveness watchdog, and startup connect-retry flags."""

import asyncio
from types import SimpleNamespace


def ev(payload):
    return SimpleNamespace(payload=payload)


async def test_disconnect_triggers_restart(bot_factory):
    bot = bot_factory()
    await bot._on_disconnected(ev({"reason": "tcp_disconnect", "reconnect_failed": True}))
    assert bot.restart_requested, "non-manual disconnect requests a restart"
    assert bot.stop_event.is_set(), "stop_event set so run() unwinds"


async def test_disconnect_trigger_fires_once(bot_factory):
    bot = bot_factory()
    await bot._on_disconnected(ev({"reason": "serial_disconnect"}))
    bot.stop_event.clear()  # simulate a racing second event mid-teardown
    await bot._on_disconnected(ev({"reason": "serial_disconnect"}))
    assert bot._reconnect_restart_armed, "armed flag latches"
    assert not bot.stop_event.is_set(), "second event is a no-op"


async def test_manual_disconnect_ignored(bot_factory):
    bot = bot_factory()
    await bot._on_disconnected(ev({"reason": "manual_disconnect"}))
    assert not bot.restart_requested and not bot.stop_event.is_set(), \
        "own shutdown/restart teardown must not re-trigger a restart"


async def test_disconnect_during_shutdown_ignored(bot_factory):
    bot = bot_factory()
    bot.stop_event.set()  # already shutting down (e.g. Ctrl-C)
    await bot._on_disconnected(ev({"reason": "tcp_disconnect"}))
    assert not bot.restart_requested, "no restart once shutdown is underway"


async def test_reconnected_resyncs_state(bot_factory):
    bot = bot_factory()
    calls = []

    async def fake_refresh():
        calls.append(1)

    bot.refresh_self_info = fake_refresh
    bot._contacts_dirty = False
    await bot._on_connected(ev({"reconnected": True}))
    assert bot._contacts_dirty, "contact re-sync forced after in-place reconnect"
    assert calls == [1], "self_info refreshed after in-place reconnect"

    # the initial CONNECTED (fresh startup) must not trigger the resync
    bot._contacts_dirty = False
    calls.clear()
    await bot._on_connected(ev({}))
    assert not bot._contacts_dirty and calls == []


async def test_watchdog_restarts_after_two_misses(bot_factory):
    bot = bot_factory()
    bot.watchdog_interval = 0.05
    pings = []

    async def dead_query():
        pings.append(1)
        raise asyncio.TimeoutError

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_device_query=dead_query))
    await asyncio.wait_for(bot._watchdog_runner(), timeout=2)
    assert len(pings) == 2, "gives the radio a second chance before acting"
    assert bot.restart_requested and bot.stop_event.is_set(), \
        "two silent probes trigger the reconnect/restart path"


async def test_watchdog_error_reply_counts_as_alive(bot_factory):
    bot = bot_factory()
    bot.watchdog_interval = 0.05
    n = 0

    async def err_query():
        nonlocal n
        n += 1
        return SimpleNamespace(type=SimpleNamespace(name="ERROR"), payload={})

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_device_query=err_query))
    task = asyncio.create_task(bot._watchdog_runner())
    await asyncio.sleep(0.3)
    bot.stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert n >= 2, "kept probing"
    assert not bot.restart_requested, "any reply — even ERROR — is a live link"


async def test_watchdog_single_miss_recovers(bot_factory):
    bot = bot_factory()
    bot.watchdog_interval = 0.05
    n = 0

    async def flaky_query():
        nonlocal n
        n += 1
        if n == 1:
            return None  # one lost probe
        return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_device_query=flaky_query))
    task = asyncio.create_task(bot._watchdog_runner())
    await asyncio.sleep(0.3)
    bot.stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert n >= 3, "kept probing after the miss"
    assert not bot.restart_requested, "a single miss followed by a reply resets"


async def test_watchdog_disabled_never_pings(bot_factory):
    bot = bot_factory()
    bot.watchdog_interval = 0
    pinged = False

    async def query():
        nonlocal pinged
        pinged = True

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_device_query=query))
    task = asyncio.create_task(bot._watchdog_runner())
    await asyncio.sleep(0.1)
    bot.stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert not pinged, "0 = disabled"


async def test_connect_failure_sets_retry_flags(bot_factory):
    # 127.0.0.1:1 refuses immediately — exercises run()'s connect-error path
    bot = bot_factory(transport="tcp", host="127.0.0.1", port=1,
                      auto_reconnect=True)
    rc = await bot.run()
    assert rc == 1
    assert bot.connect_failed and bot.restart_requested, \
        "amain() gets the signal to rebuild + back off"


async def test_connect_failure_no_retry_when_disabled(bot_factory):
    bot = bot_factory(transport="tcp", host="127.0.0.1", port=1,
                      auto_reconnect=False)
    rc = await bot.run()
    assert rc == 1
    assert not bot.restart_requested, "auto_reconnect=false exits as before"
