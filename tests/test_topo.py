#!/usr/bin/env python3
"""!topo — plot a contact's location on OpenTopoMap.

Covers: help, short prefix, no match, single match (URL + name), single match
without geo, ambiguous-prefix list, sender's own location, and the
no-location-for-sender case. The da.gd shortener is monkeypatched so the tests
never touch the network.

Run: /home/steve/dev/meshcore/meshcore-bot/venv/bin/python tests/test_topo.py
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcbot  # noqa: E402

_spec = importlib.util.spec_from_file_location("topocmd", ROOT / "commands" / "topo.py")
topo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(topo)

_failures = 0
_captured = {}


def check(cond, msg):
    global _failures
    print(f"  {'ok' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures += 1


def _fake_shorten(url):
    _captured["url"] = url
    return "https://da.gd/test"


topo._shorten_sync = _fake_shorten  # no network in tests


def make_bot():
    cfg = mcbot.Config()
    cfg.db_path = Path(":memory:")
    log = mcbot.logging.getLogger("test-topo")
    log.addHandler(mcbot.logging.NullHandler())
    log.propagate = False
    return mcbot.MCBot(cfg, log)


def pk(prefix):
    return prefix + "0" * (64 - len(prefix))


async def add_contact(bot, public_key, *, name="", lat=None, lon=None):
    await bot.db.execute(
        "INSERT INTO contacts (public_key, adv_name, type, adv_lat, adv_lon) "
        "VALUES (?,?,2,?,?)",
        (public_key, name, lat, lon),
    )


def ctx_for(bot, text, *, sender_name="bob", sender_pubkey=None, sender_prefix=None):
    return SimpleNamespace(
        message_text=text, sender_name=sender_name,
        sender_pubkey=sender_pubkey, sender_pubkey_prefix=sender_prefix, bot=bot,
    )


async def test_help():
    print("test_help")
    bot = make_bot()
    r = await topo.handle(ctx_for(bot, "!topo help"))
    check(r == "@[bob] !topo [pub-key prefix] (4+ chars)", f"help usage (got {r!r})")
    bot.db.close()


async def test_short_prefix():
    print("test_short_prefix")
    bot = make_bot()
    r = await topo.handle(ctx_for(bot, "!topo abc"))
    check(r == "@[bob] !topo [pub-key prefix] (4+ chars)", f"<4 char prefix -> usage (got {r!r})")
    bot.db.close()


async def test_no_match():
    print("test_no_match")
    bot = make_bot()
    r = await topo.handle(ctx_for(bot, "!topo dead"))
    check(r == "@[bob] no contact matches 'dead'", f"no match (got {r!r})")
    bot.db.close()


async def test_single_match_with_geo():
    print("test_single_match_with_geo")
    bot = make_bot()
    await add_contact(bot, pk("a1b2c3"), name="HillTop", lat=30.31023, lon=-97.84505)
    r = await topo.handle(ctx_for(bot, "!topo a1b2"))
    check(r == "@[bob] HillTop https://da.gd/test", f"name + short url (got {r!r})")
    check(_captured.get("url") == "https://opentopomap.org/#marker=16/30.31023/-97.84505",
          f"OpenTopoMap url passed to shortener (got {_captured.get('url')!r})")
    bot.db.close()


async def test_single_match_no_geo():
    print("test_single_match_no_geo")
    bot = make_bot()
    await add_contact(bot, pk("beef99"), name="NoFix", lat=None, lon=None)
    r = await topo.handle(ctx_for(bot, "!topo beef"))
    check(r == "@[bob] NoFix has no location", f"no-location reply (got {r!r})")
    bot.db.close()


async def test_conflict_list():
    print("test_conflict_list")
    bot = make_bot()
    await add_contact(bot, pk("abcd1"), name="Alpha", lat=30.0, lon=-97.0)
    await add_contact(bot, pk("abcd2"), name="Bravo", lat=None, lon=None)
    r = await topo.handle(ctx_for(bot, "!topo abcd"))
    check(isinstance(r, list), "conflict returns a list of lines")
    check(r[0] == "@[bob] 2 matches:", f"header line (got {r[0]!r})")
    check(any(line.startswith("abcd10000") and "Alpha" in line for line in r[1:]),
          "lists pubkey prefix + name")
    bot.db.close()


async def test_conflict_cap_overflow():
    print("test_conflict_cap_overflow")
    bot = make_bot()
    for i in range(12):
        await add_contact(bot, pk("dddd" + f"{i:02d}"), name=f"n{i}", lat=1.0, lon=2.0)
    r = await topo.handle(ctx_for(bot, "!topo dddd"))
    check(isinstance(r, list), "overflow returns a list")
    check(r[0] == "@[bob] 12 matches:", f"count reflects all matches (got {r[0]!r})")
    # 1 header + 10 entries + 1 overflow line
    check(len(r) == 12, f"capped to 10 entries + overflow line (got {len(r)})")
    check(r[-1] == "…2 more (use a longer prefix)", f"overflow line (got {r[-1]!r})")
    bot.db.close()


async def test_own_location_known():
    print("test_own_location_known")
    bot = make_bot()
    me = pk("5e1f")
    await add_contact(bot, me, name="Me", lat=12.34567, lon=-76.54321)
    r = await topo.handle(ctx_for(bot, "!topo", sender_pubkey=me))
    check(r == "@[bob] Me https://da.gd/test", f"own location plotted (got {r!r})")
    bot.db.close()


async def test_own_location_unknown():
    print("test_own_location_unknown")
    bot = make_bot()
    r = await topo.handle(ctx_for(bot, "!topo", sender_pubkey=pk("9999")))
    check(r == "@[bob] can't find your location", f"no own location (got {r!r})")
    bot.db.close()


async def main():
    for t in (
        test_help,
        test_short_prefix,
        test_no_match,
        test_single_match_with_geo,
        test_single_match_no_geo,
        test_conflict_list,
        test_conflict_cap_overflow,
        test_own_location_known,
        test_own_location_unknown,
    ):
        await t()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
