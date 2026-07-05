"""Repeater-repeat tracking.

These build synthetic on-air frames using the SAME crypto helpers the bot uses
to decrypt them, so the round-trip is self-consistent without real hardware."""

import asyncio
import hashlib
import hmac
import os
from types import SimpleNamespace

import pytest
from Crypto.Cipher import AES

from mcbot import PayloadType, derive_public_key, derive_shared_secret

GROUP = PayloadType.GROUP_TEXT.value      # 0x05
TEXT = PayloadType.TEXT_MESSAGE.value     # 0x02


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


@pytest.fixture
def repeat_bot(bot_factory):
    """(bot, our_priv, our_pub) with repeat tracking on and a fixed identity."""
    def make(*, repeat_tracking=True, repeat_timeout=5.0):
        bot = bot_factory(
            repeat_tracking=repeat_tracking, repeat_timeout=repeat_timeout,
        )
        our_priv = os.urandom(64)
        our_pub = derive_public_key(our_priv)
        bot.my_private_key = our_priv
        bot.my_public_key_bytes = our_pub
        bot.my_pubkey = our_pub.hex()
        bot.my_pubkey_byte = our_pub[0]
        return bot, our_priv, our_pub
    return make


async def count_rows(bot, packet_type):
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM received_packets WHERE packet_type=?",
        (packet_type,),
    )
    return row["n"]


async def test_channel_round_trip(repeat_bot):
    bot, *_ = repeat_bot()
    secret = os.urandom(16)
    chash = hashlib.sha256(secret).digest()[0]
    bot.channels_by_hash[chash] = (3, "#test", secret)
    w = bot._register_repeat_watch(kind="channel", text="hello", channel_idx=3)
    assert w is not None, "channel watch registered"
    pkt = build_channel_pkt(secret, "hello", ts=1000)
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=111))
    assert w.repeat_count == 1, "repeat counted once"
    assert await count_rows(bot, "REPEAT") == 1, "one REPEAT row emitted"


async def test_dm_round_trip_and_inversion(repeat_bot):
    bot, our_priv, our_pub = repeat_bot()
    their_priv = os.urandom(64)
    their_pub = derive_public_key(their_priv)
    w = bot._register_repeat_watch(
        kind="dm", text="pong", dest_pubkey=their_pub.hex(), disp_name="bob"
    )
    assert w is not None and w.dest_byte == their_pub[0], "dm watch registered"
    pkt = build_dm_pkt(our_priv, their_pub, our_pub[0], their_pub[0], "pong", 2000)
    await bot._match_repeat(rx_payload(pkt, TEXT, path="cd", pkt_hash=222))
    assert w.repeat_count == 1, "dm repeat matched"
    # inversion: a frame addressed TO us (payload[1] != our byte) must NOT match
    inbound = bytes([our_pub[0], their_pub[0]]) + pkt[2:]
    before = w.repeat_count
    await bot._match_repeat(rx_payload(inbound, TEXT, path="ef", pkt_hash=333))
    assert w.repeat_count == before, "inbound-to-us frame not counted as repeat"


async def test_multi_repeater_and_frame_dedup(repeat_bot):
    bot, *_ = repeat_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (1, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="hi", channel_idx=1)
    pkt = build_channel_pkt(secret, "hi", ts=500)
    # same pkt_hash, different path => two distinct repeaters
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=7))
    await bot._match_repeat(rx_payload(pkt, GROUP, path="cd", pkt_hash=7))
    assert w.repeat_count == 2, "two repeaters counted"
    assert len(w.repeater_keys) == 2, "two distinct repeater keys"
    # exact same (pkt_hash, path) again => deduped
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=7))
    assert w.repeat_count == 2, "identical frame not double-counted"
    assert await count_rows(bot, "REPEAT") == 2, "two REPEAT rows total"


async def test_retry_attempts_same_text(repeat_bot):
    bot, *_ = repeat_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (2, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="yo", channel_idx=2)
    # two send attempts: same text, different timestamp -> different ciphertext
    p1 = build_channel_pkt(secret, "yo", ts=1)
    p2 = build_channel_pkt(secret, "yo", ts=2)
    assert p1 != p2, "different attempts produce different ciphertext"
    await bot._match_repeat(rx_payload(p1, GROUP, path="ab", pkt_hash=10))
    await bot._match_repeat(rx_payload(p2, GROUP, path="ab", pkt_hash=11))
    assert w.repeat_count == 2, "both attempts matched by content"


def _stub_channel_sender(bot):
    # capture send_chan_msg calls; return an OK event like the real radio
    sends = []

    async def fake_send(idx, text, timestamp=None):
        sends.append((idx, text, timestamp))
        return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})

    bot.mc = SimpleNamespace(commands=SimpleNamespace(send_chan_msg=fake_send))
    return sends


async def test_channel_send_always_stamps_timestamp(repeat_bot):
    bot, *_ = repeat_bot(repeat_timeout=0.05)
    bot.channel_retry_max = 0  # explicitly disabled
    sends = _stub_channel_sender(bot)
    await bot.send_channel_text(3, "once")
    await asyncio.sleep(0.25)
    assert len(sends) == 1, "no retry when channel_retry_max=0"
    assert sends[0][2] is not None, "send carries an explicit timestamp (dedup key)"
    assert await count_rows(bot, "RETRY") == 0, "no RETRY rows when disabled"
    assert await count_rows(bot, "NO_REPEAT") == 1, "NO_REPEAT still emitted"


async def test_channel_no_repeat_retry_exhausts(repeat_bot):
    bot, *_ = repeat_bot(repeat_timeout=0.05)
    bot.channel_retry_max = 2
    sends = _stub_channel_sender(bot)
    await bot.send_channel_text(8, "ping")
    await asyncio.sleep(0.5)
    assert len(sends) == 3, f"original + 2 retries sent (got {len(sends)})"
    ts0 = sends[0][2]
    assert ts0 is not None and all(s[2] == ts0 for s in sends), \
        "every retry reuses the original timestamp"
    assert all(s[1] == "ping" for s in sends), "text unchanged across retries"
    assert await count_rows(bot, "RETRY") == 2, "two RETRY rows emitted"
    assert await count_rows(bot, "NO_REPEAT") == 1, \
        "final NO_REPEAT after retries exhausted"


async def test_channel_retry_stops_on_repeat(repeat_bot):
    bot, *_ = repeat_bot(repeat_timeout=0.1)
    bot.channel_retry_max = 3
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (9, "#c", secret)
    sends = _stub_channel_sender(bot)
    await bot.send_channel_text(9, "yo")
    ts0 = sends[0][2]
    # a repeater rebroadcast heard before the first timeout -> no retries at all
    pkt = build_channel_pkt(secret, "yo", ts=ts0)
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=55))
    await asyncio.sleep(0.4)
    assert len(sends) == 1, "no retries once a repeat is heard"
    assert await count_rows(bot, "REPEAT") == 1, "repeat recorded"
    assert await count_rows(bot, "NO_REPEAT") == 0, "no NO_REPEAT once repeated"


async def test_no_repeat_timer(repeat_bot):
    bot, *_ = repeat_bot(repeat_timeout=0.05)
    w = bot._register_repeat_watch(kind="channel", text="nope", channel_idx=4)
    bot._start_repeat_timer(w)
    await asyncio.sleep(0.2)
    assert await count_rows(bot, "NO_REPEAT") == 1, "NO_REPEAT emitted on silence"
    assert w not in bot._repeat_watches, "watch removed after timeout"


async def test_direct_0hop_label(repeat_bot):
    bot, _our_priv, _our_pub = repeat_bot(repeat_timeout=0.05)
    their_pub = derive_public_key(os.urandom(64))
    # a 0-hop direct DM has no repeater in its path -> DIRECT_0HOP, not NO_REPEAT
    w = bot._register_repeat_watch(
        kind="dm", text="hi", dest_pubkey=their_pub.hex(),
        disp_name="bob", route_mode="direct_0hop",
    )
    assert w is not None and w.route_mode == "direct_0hop", "route_mode stored"
    bot._start_repeat_timer(w)
    await asyncio.sleep(0.2)
    assert await count_rows(bot, "DIRECT_0HOP") == 1, "DIRECT_0HOP row emitted"
    assert await count_rows(bot, "NO_REPEAT") == 0, "no NO_REPEAT for 0-hop DM"

    # a flooded DM with no repeater heard is still a genuine NO_REPEAT
    w2 = bot._register_repeat_watch(
        kind="dm", text="yo", dest_pubkey=their_pub.hex(),
        disp_name="bob", route_mode="flood",
    )
    bot._start_repeat_timer(w2)
    await asyncio.sleep(0.2)
    assert await count_rows(bot, "NO_REPEAT") == 1, "flood DM -> NO_REPEAT"
    assert await count_rows(bot, "DIRECT_0HOP") == 1, "flood DM not DIRECT_0HOP"


async def test_repeat_before_timeout_no_norepeat(repeat_bot):
    bot, *_ = repeat_bot(repeat_timeout=0.2)
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (5, "#c", secret)
    w = bot._register_repeat_watch(kind="channel", text="seen", channel_idx=5)
    bot._start_repeat_timer(w)
    pkt = build_channel_pkt(secret, "seen", ts=9)
    await bot._match_repeat(rx_payload(pkt, GROUP, path="ab", pkt_hash=99))
    await asyncio.sleep(0.3)
    assert w.repeat_count == 1, "repeat counted"
    assert await count_rows(bot, "NO_REPEAT") == 0, "no NO_REPEAT when repeated"


async def test_self_echo_suppression(repeat_bot):
    bot, *_ = repeat_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (6, "#c", secret)
    bot._register_repeat_watch(kind="channel", text="mine", channel_idx=6)
    assert bot._matches_active_channel_watch(6, "mine"), "active watch matches"
    assert not bot._matches_active_channel_watch(6, "other"), "other text no match"
    pkt = build_channel_pkt(secret, "mine", ts=42)
    await bot._handle_inbound_channel(
        pkt, snr=-5.0, rssi=-100, path_hex="ab", path_len=1, path_hash_mode=0
    )
    n = await bot.db.fetchone("SELECT COUNT(*) AS n FROM channel_messages")
    assert n["n"] == 0, "own repeated channel msg not re-ingested"


async def test_disabled_config(repeat_bot):
    bot, *_ = repeat_bot(repeat_tracking=False)
    w = bot._register_repeat_watch(kind="channel", text="x", channel_idx=1)
    assert w is None, "register is a no-op when disabled"
    assert not bot._matches_active_channel_watch(1, "x"), "no match when disabled"
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (1, "#c", secret)
    await bot._match_repeat(
        rx_payload(build_channel_pkt(secret, "x", ts=1), GROUP, pkt_hash=1)
    )
    assert await count_rows(bot, "REPEAT") == 0, "no REPEAT rows when disabled"


async def test_end_to_end_firehose(repeat_bot):
    bot, *_ = repeat_bot()
    secret = os.urandom(16)
    bot.channels_by_hash[hashlib.sha256(secret).digest()[0]] = (7, "#c", secret)
    bot._register_repeat_watch(kind="channel", text="e2e", channel_idx=7)
    pkt = build_channel_pkt(secret, "e2e", ts=77)
    payload = rx_payload(pkt, GROUP, path="ab", pkt_hash=1234)
    event = SimpleNamespace(
        type=SimpleNamespace(name="RX_LOG_DATA"), payload=payload, attributes={}
    )
    await bot._firehose(event)
    assert await count_rows(bot, "RX_LOG") == 1, "base RX_LOG row recorded"
    assert await count_rows(bot, "REPEAT") == 1, "REPEAT matched via firehose"
