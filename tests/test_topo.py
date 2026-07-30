"""!topo — plot a contact's location on OpenTopoMap.

Covers: help, short prefix, no match, single match (URL + name), single match
without geo, ambiguous-prefix list, sender's own location, and the
no-location-for-sender case. The shortener is monkeypatched so the tests
never touch the network."""

from types import SimpleNamespace

from conftest import load_command

topo = load_command("topo")

_captured = {}


def _fake_shorten(url, cfg, logger=None, tag=""):
    _captured["url"] = url
    _captured["tag"] = tag
    return "https://da.gd/test"


topo.shorten = _fake_shorten  # no network in tests


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


async def test_help(bot_factory):
    bot = bot_factory()
    r = await topo.handle(ctx_for(bot, "!topo help"))
    assert r == "@[bob] !topo [pub-key prefix] (4+ chars)"


async def test_short_prefix(bot_factory):
    bot = bot_factory()
    r = await topo.handle(ctx_for(bot, "!topo abc"))
    assert r == "@[bob] !topo [pub-key prefix] (4+ chars)", "<4 char prefix -> usage"


async def test_no_match(bot_factory):
    bot = bot_factory()
    r = await topo.handle(ctx_for(bot, "!topo dead"))
    assert r == "@[bob] no contact matches 'dead'"


async def test_single_match_with_geo(bot_factory):
    bot = bot_factory()
    await add_contact(bot, pk("a1b2c3"), name="HillTop", lat=30.31023, lon=-97.84505)
    r = await topo.handle(ctx_for(bot, "!topo a1b2"))
    assert r == "@[bob] HillTop https://da.gd/test", "name + short url"
    assert _captured.get("url") == \
        "https://opentopomap.org/#marker=16/30.31023/-97.84505", \
        "OpenTopoMap url passed to shortener"


async def test_single_match_no_geo(bot_factory):
    bot = bot_factory()
    await add_contact(bot, pk("beef99"), name="NoFix", lat=None, lon=None)
    r = await topo.handle(ctx_for(bot, "!topo beef"))
    assert r == "@[bob] NoFix has no location"


async def test_conflict_list(bot_factory):
    bot = bot_factory()
    await add_contact(bot, pk("abcd1"), name="Alpha", lat=30.0, lon=-97.0)
    await add_contact(bot, pk("abcd2"), name="Bravo", lat=None, lon=None)
    r = await topo.handle(ctx_for(bot, "!topo abcd"))
    assert isinstance(r, list), "conflict returns a list of lines"
    assert r[0] == "@[bob] 2 matches:", "header line"
    assert any(line.startswith("abcd10000") and "Alpha" in line for line in r[1:]), \
        "lists pubkey prefix + name"


async def test_conflict_cap_overflow(bot_factory):
    bot = bot_factory()
    for i in range(12):
        await add_contact(bot, pk("dddd" + f"{i:02d}"), name=f"n{i}", lat=1.0, lon=2.0)
    r = await topo.handle(ctx_for(bot, "!topo dddd"))
    assert isinstance(r, list), "overflow returns a list"
    assert r[0] == "@[bob] 12 matches:", "count reflects all matches"
    # 1 header + 10 entries + 1 overflow line
    assert len(r) == 12, "capped to 10 entries + overflow line"
    assert r[-1] == "…2 more (use a longer prefix)", "overflow line"


async def test_own_location_known(bot_factory):
    bot = bot_factory()
    me = pk("5e1f")
    await add_contact(bot, me, name="Me", lat=12.34567, lon=-76.54321)
    r = await topo.handle(ctx_for(bot, "!topo", sender_pubkey=me))
    assert r == "@[bob] Me https://da.gd/test", "own location plotted"


async def test_own_location_unknown(bot_factory):
    bot = bot_factory()
    r = await topo.handle(ctx_for(bot, "!topo", sender_pubkey=pk("9999")))
    assert r == "@[bob] can't find your location"
