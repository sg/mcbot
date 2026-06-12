#!/usr/bin/env python3
"""Tests for repeater-repeat tracking in mcbot.py.

Run with a Python that has meshcore + pynacl + pycryptodome installed, e.g.:
    /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_repeat_tracking.py

These build synthetic on-air frames using the SAME crypto helpers the bot uses
to decrypt them, so the round-trip is self-consistent without real hardware.
"""

import asyncio
import hashlib
import hmac
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Crypto.Cipher import AES  # noqa: E402

import mcbot  # noqa: E402
from mcbot import (  # noqa: E402
    Config, MCBot, PayloadType,
    derive_public_key, derive_shared_secret,
)

GROUP = PayloadType.GROUP_TEXT.value      # 0x05
TEXT = PayloadType.TEXT_MESSAGE.value     # 0x02

_failures = 0


def check(cond, msg):
    global _failures
    if cond:
        print(f"  ok: {msg}")
    else:
        _failures += 1
        print(f"  FAIL: {msg}")


def _pad16(b: bytes) -> bytes:
    if len(b) % 16:
        b += b"\x00" * (16 - len(b) % 16)
    return b


def build_channel_pkt(secret: bytes, text: str, ts: int, flags: int = 0) -> bytes:
    plain = _pad16(ts.to_bytes(4, "little") + bytes([flags]) + text.encode())
    ct = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2]
    chash = hashlib.sha256(secret).digest()[0]
    return bytes([chash]) + mac + ct


def build_dm_pkt(our_priv, their_pub, our_byte, their_byte, text, ts, flags=0):
    shared = derive_shared_secret(our_priv, their_pub)
    plain = _pad16(ts.to_bytes(4, "little") + bytes([flags]) + text.encode())
    ct = AES.new(shared[:16], AES.MODE_ECB).encrypt(plain)
    mac = hmac.new(shared, ct, hashlib.sha256).digest()[:2]
    # outgoing repeat: payload[0]=recipient byte, payload[1]=our byte
    return bytes([their_byte, our_byte]) + mac + ct


def rx_payload(pkt, ptype, *, path="ab", path_hash_size=1, pkt_hash=1):
    path_len = len(path) // (2 * path_hash_size) if path else 0
    return {
        "payload_type": ptype,
        "payload_typename": "GRP_TXT" if ptype == GROUP else "TEXT_MSG",
        "pkt_payload": pkt,
        "path": path,
        "path_len": path_len,
        "path_hash_size": path_hash_size,
        "pkt_hash": pkt_hash,
        "snr": -7.5,
        "rssi": -110,
    }


def make_bot(*, repeat_tracking=True, repeat_timeout=5.0):
    cfg = Config()
    cfg.db_path = Path(":memory:")
    cfg.repeat_tracking = repeat_tracking
    cfg.repeat_timeout = repeat_timeout
    log = mcbot.logging.getLogger("test-repeat")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    bot = MCBot(cfg, log)
    # identity
    our_priv = os.urandom(64)
    our_pub = derive_public_key(our_priv)
    bot.my_private_key = our_priv
    bot.my_public_key_bytes = our_pub
    bot.my_pubkey = our_pub.hex()
    bot.my_pubkey_byte = our_pub[0]
    return bot, our_priv, our_pub


async def count_rows(bot, packet_type):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM received_packets WHERE packet_type=?",
        (packet_type,),
    )
    return row["n"]


async def test_channel_round_trip():
    print("test_channel_round_trip")
    bot, *_ = make_bot()
    secret = os.urandom(16)
    chash = hashlib.sha256(secret).digest()[0]
    bot.channels_by_hash[chash] = (3, "#test", secret)
    w = bot._register_repeat_watch(kind="channel", text="hello", channel_idx=3)
    check(w is not None, "channel watch registered")
    pkt = build_channel_pkt(secret, "hello", ts=1000)
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=111))
    check(w.repeat_count == 1, "repeat counted once")
    check(await count_rows(bot, "REPEAT") == 1, "one REPEAT row emitted")
    bot.db.close()


async def test_dm_round_trip_and_inversion():
    print("test_dm_round_trip_and_inversion")
    bot, our_priv, our_pub = make_bot()
    their_priv = os.urandom(64)
    their_pub = derive_public_key(their_priv)
    w = bot._register_repeat_watch(
        kind="dm", text="pong", dest_pubkey=their_pub.hex(), disp_name="bob"
    )
    check(w is not None and w.dest_byte == their_pub[0], "dm watch registered")
    pkt = build_dm_pkt(our_priv, their_pub, our_pub[0], their_pub[0], "pong", 2000)
    await bot._match_repeat(rx_payload(pkt, TEXT, path="cd", pkt_hash=222))
    check(w.repeat_count == 1, "dm repeat matched")
    # inversion: a frame addressed TO us (payload[1] != our byte) must NOT match
    inbound = bytes([our_pub[0], their_pub[0]]) + pkt[2:]
    before = w.repeat_count
    await bot._match_repeat(rx_payload(inbound, TEXT, path="ef", pkt_hash=333))
    check(w.repeat_count == before, "inbound-to-us frame not counted as repeat")
    bot.db.close()


async def test_multi_repeater_and_frame_dedup():
    print("test_multi_repeater_and_frame_dedup")
    bot, *_ = make_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (1, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="hi", channel_idx=1)
    pkt = build_channel_pkt(secret, "hi", ts=500)
    # same pkt_hash, different path => two distinct repeaters
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=7))
    await bot._match_repeat(rx_payload(pkt, GROUP, path="cd", pkt_hash=7))
    check(w.repeat_count == 2, "two repeaters counted")
    check(len(w.repeater_keys) == 2, "two distinct repeater keys")
    # exact same (pkt_hash, path) again => deduped
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=7))
    check(w.repeat_count == 2, "identical frame not double-counted")
    check(await count_rows(bot, "REPEAT") == 2, "two REPEAT rows total")
    bot.db.close()


async def test_retry_attempts_same_text():
    print("test_retry_attempts_same_text")
    bot, *_ = make_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (2, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="yo", channel_idx=2)
    # two send attempts: same text, different timestamp -> different ciphertext
    p1 = build_channel_pkt(secret, "yo", ts=1)
    p2 = build_channel_pkt(secret, "yo", ts=2)
    check(p1 != p2, "different attempts produce different ciphertext")
    await bot._match_repeat(rx_payload(p1, GROUP, path="ab", pkt_hash=10))
    await bot._match_repeat(rx_payload(p2, GROUP, path="ab", pkt_hash=11))
    check(w.repeat_count == 2, "both attempts matched by content")
    bot.db.close()


async def test_no_repeat_timer():
    print("test_no_repeat_timer")
    bot, *_ = make_bot(repeat_timeout=0.05)
    w = bot._register_repeat_watch(kind="channel", text="nope", channel_idx=4)
    bot._start_repeat_timer(w)
    await asyncio.sleep(0.2)
    check(await count_rows(bot, "NO_REPEAT") == 1, "NO_REPEAT emitted on silence")
    check(w not in bot._repeat_watches, "watch removed after timeout")
    bot.db.close()


async def test_direct_0hop_label():
    print("test_direct_0hop_label")
    bot, _our_priv, _our_pub = make_bot(repeat_timeout=0.05)
    their_pub = derive_public_key(os.urandom(64))
    # a 0-hop direct DM has no repeater in its path -> DIRECT_0HOP, not NO_REPEAT
    w = bot._register_repeat_watch(
        kind="dm", text="hi", dest_pubkey=their_pub.hex(),
        disp_name="bob", route_mode="direct_0hop",
    )
    check(w is not None and w.route_mode == "direct_0hop", "route_mode stored")
    bot._start_repeat_timer(w)
    await asyncio.sleep(0.2)
    check(await count_rows(bot, "DIRECT_0HOP") == 1, "DIRECT_0HOP row emitted")
    check(await count_rows(bot, "NO_REPEAT") == 0, "no NO_REPEAT for 0-hop DM")

    # a flooded DM with no repeater heard is still a genuine NO_REPEAT
    w2 = bot._register_repeat_watch(
        kind="dm", text="yo", dest_pubkey=their_pub.hex(),
        disp_name="bob", route_mode="flood",
    )
    bot._start_repeat_timer(w2)
    await asyncio.sleep(0.2)
    check(await count_rows(bot, "NO_REPEAT") == 1, "flood DM -> NO_REPEAT")
    check(await count_rows(bot, "DIRECT_0HOP") == 1, "flood DM not DIRECT_0HOP")
    bot.db.close()


async def test_repeat_before_timeout_no_norepeat():
    print("test_repeat_before_timeout_no_norepeat")
    bot, *_ = make_bot(repeat_timeout=0.2)
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (5, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="seen", channel_idx=5)
    bot._start_repeat_timer(w)
    pkt = build_channel_pkt(secret, "seen", ts=9)
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=99))
    await asyncio.sleep(0.3)
    check(w.repeat_count == 1, "repeat counted")
    check(await count_rows(bot, "NO_REPEAT") == 0, "no NO_REPEAT when repeated")
    bot.db.close()


async def test_self_echo_suppression():
    print("test_self_echo_suppression")
    bot, *_ = make_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (6, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="mine", channel_idx=6)
    check(bot._matches_active_channel_watch(6, "mine"), "active watch matches")
    check(not bot._matches_active_channel_watch(6, "other"), "other text no match")
    pkt = build_channel_pkt(secret, "mine", ts=42)
    await bot._handle_inbound_channel(
        pkt, snr=-5.0, rssi=-100, path_hex="ab", path_len=1, path_hash_mode=0
    )
    n = await bot.db.fetchone("SELECT COUNT(*) AS n FROM channel_messages")
    check(n["n"] == 0, "own repeated channel msg not re-ingested")
    bot.db.close()


async def test_disabled_config():
    print("test_disabled_config")
    bot, *_ = make_bot(repeat_tracking=False)
    w = bot._register_repeat_watch(kind="channel", text="x", channel_idx=1)
    check(w is None, "register is a no-op when disabled")
    check(not bot._matches_active_channel_watch(1, "x"), "no match when disabled")
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (1, "#c", secret)
    await bot._match_repeat(
        rx_payload(build_channel_pkt(secret, "x", ts=1), GROUP, pkt_hash=1)
    )
    check(await count_rows(bot, "REPEAT") == 0, "no REPEAT rows when disabled")
    bot.db.close()


async def test_end_to_end_firehose():
    print("test_end_to_end_firehose")
    bot, *_ = make_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (7, "#c", secret)
    bot._register_repeat_watch(kind="channel", text="e2e", channel_idx=7)
    pkt = build_channel_pkt(secret, "e2e", ts=77)
    payload = rx_payload(pkt, GROUP, path="ab", pkt_hash=1234)
    event = SimpleNamespace(
        type=SimpleNamespace(name="RX_LOG_DATA"), payload=payload, attributes={}
    )
    await bot._firehose(event)
    check(await count_rows(bot, "RX_LOG") == 1, "base RX_LOG row recorded")
    check(await count_rows(bot, "REPEAT") == 1, "REPEAT matched via firehose")
    bot.db.close()


async def main():
    for t in (
        test_channel_round_trip,
        test_dm_round_trip_and_inversion,
        test_multi_repeater_and_frame_dedup,
        test_retry_attempts_same_text,
        test_no_repeat_timer,
        test_direct_0hop_label,
        test_repeat_before_timeout_no_norepeat,
        test_self_echo_suppression,
        test_disabled_config,
        test_end_to_end_firehose,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
