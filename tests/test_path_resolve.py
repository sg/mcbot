#!/usr/bin/env python3
"""Collision-aware path-hop resolution for !path.

A path hop is the leading bytes of a repeater's pubkey and can collide across
contacts. The resolver must never let a far repeater that merely shares the
hash supply a bogus location (the tropospheric-ducting bug), while still
trusting unambiguous hops and disambiguating multi-located collisions by
proximity to the bot.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_path_resolve.py
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcbot  # noqa: E402

# commands/ is not a package (the bot loads plugins dynamically), so load the
# module straight from its file.
_spec = importlib.util.spec_from_file_location(
    "pathcmd", ROOT / "commands" / "path.py"
)
pathcmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pathcmd)

_failures = 0


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def make_bot():
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    log = mcbot.logging.getLogger("test-path")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


def pk(prefix):
    # full 64-hex pubkey whose leading bytes are `prefix`
    return prefix + "0" * (64 - len(prefix))


async def add_contact(bot, public_key, *, type_=2, lat=None, lon=None, name=""):
    await bot.db.execute(
        "INSERT INTO contacts (public_key, adv_name, type, adv_lat, adv_lon) "
        "VALUES (?,?,?,?,?)",
        (public_key, name, type_, lat, lon),
    )


async def set_bot_location(bot, lat, lon):
    # device_info values are JSON-encoded, mirroring _upsert_device_info
    for k, v in (("self_info.adv_lat", lat), ("self_info.adv_lon", lon)):
        await bot.db.execute(
            "INSERT INTO device_info(key,value,last_updated) VALUES(?,?,0)",
            (k, json.dumps(v)),
        )


def ctx_for(bot, sender_pubkey=None):
    return SimpleNamespace(bot=bot, sender_pubkey=sender_pubkey)


async def test_collision_only_far_has_geo_is_unlocated():
    print("test_collision_only_far_has_geo_is_unlocated")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)  # Texas
    # the reported bug: local hop (correct) has no geo; a far repeater sharing
    # the 2-byte hash has geo. The far one must NOT be used.
    await add_contact(bot, pk("abcd0"), name="local", lat=None, lon=None)
    await add_contact(bot, pk("abcd1"), name="louisiana", lat=30.0, lon=-92.0)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res == [None], f"colliding hop with one geo'd impostor -> unlocated (got {res})")
    bot.db.close()


async def test_collision_two_geo_picks_nearest_to_bot():
    print("test_collision_two_geo_picks_nearest_to_bot")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    await add_contact(bot, pk("abcd0"), name="near", lat=32.1, lon=-96.1)
    await add_contact(bot, pk("abcd1"), name="far", lat=30.0, lon=-92.0)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res[0] is not None and res[0][2] == "near",
          f"nearest-to-bot candidate wins (got {res[0]})")
    bot.db.close()


async def test_unique_hop_trusted_even_if_far():
    print("test_unique_hop_trusted_even_if_far")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    # a single repeater matches the hash -> unambiguous, trust it even if far
    await add_contact(bot, pk("ef01"), name="solo", lat=12.3, lon=45.6)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["ef01"])
    check(res[0] is not None and res[0][2] == "solo", "unique hop resolved")
    bot.db.close()


async def test_non_repeater_excluded():
    print("test_non_repeater_excluded")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    # a companion (type 1) with geo must not satisfy a repeater hop
    await add_contact(bot, pk("1234"), type_=1, name="client", lat=31.0, lon=-95.0)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["1234"])
    check(res == [None], "companion-type contact not used as a hop")
    # add a real repeater sharing the prefix -> now it (not the client) wins
    await add_contact(bot, pk("12349"), type_=2, name="rep", lat=31.5, lon=-95.5)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["1234"])
    check(res[0] is not None and res[0][2] == "rep", "repeater chosen over client")
    bot.db.close()


async def test_unique_geoless_hop_is_unlocated():
    print("test_unique_geoless_hop_is_unlocated")
    bot = make_bot()
    await add_contact(bot, pk("5678"), name="noloc", lat=None, lon=None)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["5678"])
    check(res == [None], "unique repeater with no geo -> unlocated")
    bot.db.close()


async def test_sender_anchor_used_when_no_bot_location():
    print("test_sender_anchor_used_when_no_bot_location")
    bot = make_bot()  # no bot location set
    sender = pk("9999")
    await add_contact(bot, sender, type_=1, name="sender", lat=30.0, lon=-92.0)
    # collision with two geo'd candidates; only the sender anchors the choice
    await add_contact(bot, pk("abcd0"), name="near-sender", lat=30.2, lon=-92.2)
    await add_contact(bot, pk("abcd1"), name="far", lat=40.0, lon=-110.0)
    res = await pathcmd._resolve_hops(ctx_for(bot, sender_pubkey=sender), ["abcd"])
    check(res[0] is not None and res[0][2] == "near-sender",
          f"sender anchors disambiguation when bot loc absent (got {res[0]})")
    bot.db.close()


async def test_collision_lone_local_within_radius_accepted():
    print("test_collision_lone_local_within_radius_accepted")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    # collision where the only located candidate is the correct LOCAL hop (a
    # few miles from the bot); a geoless repeater shares the hash. Within the
    # radius it must be kept (the regression the radius restores).
    await add_contact(bot, pk("abcd0"), name="local", lat=32.05, lon=-96.05)
    await add_contact(bot, pk("abcd1"), name="noloc", lat=None, lon=None)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res[0] is not None and res[0][2] == "local",
          f"lone local hop within radius is kept (got {res[0]})")
    bot.db.close()


async def test_collision_radius_gates_acceptance():
    print("test_collision_radius_gates_acceptance")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    # lone located candidate ~69 mi north of the bot, colliding with a geoless one
    await add_contact(bot, pk("abcd0"), name="hop", lat=33.0, lon=-96.0)
    await add_contact(bot, pk("abcd1"), name="noloc", lat=None, lon=None)

    bot.cfg.path_collision_radius_miles = 150
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res[0] is not None and res[0][2] == "hop", "within 150mi radius -> accepted")

    bot.cfg.path_collision_radius_miles = 50
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res == [None], "beyond 50mi radius -> rejected")

    bot.cfg.path_collision_radius_miles = 0
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res == [None], "radius 0 -> lone candidate never trusted (conservative)")
    bot.db.close()


async def test_neighbour_hop_anchors_without_bot_or_sender():
    print("test_neighbour_hop_anchors_without_bot_or_sender")
    bot = make_bot()  # no bot location, no sender
    # hop 1 matches a single located repeater -> resolves and becomes an anchor
    await add_contact(bot, pk("aaaa"), name="anchorhop", lat=32.0, lon=-96.0)
    # hop 2 collides: a nearby located hop + a geoless one. The only anchor is
    # the neighbouring hop, which must be enough to keep the local candidate.
    await add_contact(bot, pk("abcd0"), name="near", lat=32.1, lon=-96.1)
    await add_contact(bot, pk("abcd1"), name="noloc", lat=None, lon=None)
    res = await pathcmd._resolve_hops(ctx_for(bot), ["aaaa", "abcd"])
    check(res[0] is not None and res[0][2] == "anchorhop", "unambiguous hop resolved")
    check(res[1] is not None and res[1][2] == "near",
          "neighbouring located hop anchors the collision (no bot/sender loc)")
    bot.db.close()


async def test_radius_zero_still_disambiguates_two_located():
    print("test_radius_zero_still_disambiguates_two_located")
    bot = make_bot()
    await set_bot_location(bot, 32.0, -96.0)
    await add_contact(bot, pk("abcd0"), name="near", lat=32.1, lon=-96.1)
    await add_contact(bot, pk("abcd1"), name="far", lat=30.0, lon=-92.0)
    bot.cfg.path_collision_radius_miles = 0
    res = await pathcmd._resolve_hops(ctx_for(bot), ["abcd"])
    check(res[0] is not None and res[0][2] == "near",
          "radius 0 still picks nearest when >=2 located")
    bot.db.close()


async def main():
    for t in (
        test_collision_only_far_has_geo_is_unlocated,
        test_collision_two_geo_picks_nearest_to_bot,
        test_unique_hop_trusted_even_if_far,
        test_non_repeater_excluded,
        test_unique_geoless_hop_is_unlocated,
        test_sender_anchor_used_when_no_bot_location,
        test_collision_lone_local_within_radius_accepted,
        test_collision_radius_gates_acceptance,
        test_neighbour_hop_anchors_without_bot_or_sender,
        test_radius_zero_still_disambiguates_two_located,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
