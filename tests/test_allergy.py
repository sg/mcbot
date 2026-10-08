"""!allergy / !air: argument parsing, reply formatting for both backends,
backend selection (Google when keyed, pollen.com otherwise or on failure),
and locating the sender from the first located repeater in the path."""

import json
from types import SimpleNamespace

import pytest
from conftest import load_command

allergy = load_command("allergy")

GOOGLE = {
    "regionCode": "US",
    "dailyInfo": [{
        "date": {"year": 2026, "month": 10, "day": 8},
        "pollenTypeInfo": [
            {"code": "GRASS", "displayName": "Grass", "inSeason": True,
             "indexInfo": {"code": "UPI", "value": 1, "category": "Very Low"}},
            {"code": "TREE", "displayName": "Tree", "inSeason": True,
             "indexInfo": {"code": "UPI", "value": 2, "category": "Low"}},
            {"code": "WEED", "displayName": "Weed", "inSeason": True,
             "indexInfo": {"code": "UPI", "value": 4, "category": "High"}},
        ],
        "plantInfo": [
            {"code": "OAK", "displayName": "Oak", "inSeason": True,
             "indexInfo": {"value": 1, "category": "Very Low"},
             "plantDescription": {"type": "TREE"}},
            {"code": "ELM", "displayName": "Elm", "inSeason": True,
             "indexInfo": {"value": 2, "category": "Low"},
             "plantDescription": {"type": "TREE"}},
            {"code": "JUNIPER", "displayName": "Juniper", "inSeason": False},
            # no plantDescription: type must come from the static map
            {"code": "RAGWEED", "displayName": "Ragweed", "inSeason": True,
             "indexInfo": {"value": 4, "category": "High"}},
            {"code": "GRAMINALES", "displayName": "Grasses", "inSeason": True,
             "indexInfo": {"value": 1, "category": "Very Low"},
             "plantDescription": {"type": "GRASS"}},
        ],
    }],
}

# trimmed from a live pollen.com response for 78701
POLLENCOM = {
    "Type": "pollen",
    "ForecastDate": "2026-10-08T00:00:00-04:00",
    "Location": {
        "ZIP": "78701", "City": "AUSTIN", "State": "TX",
        "periods": [
            {"Type": "Yesterday", "Index": 9.4, "Triggers": []},
            {"Type": "Today", "Index": 9.0, "Triggers": [
                {"Name": "Ragweed", "PlantType": "Ragweed"},
                {"Name": "Elm", "PlantType": "Tree"},
                {"Name": "Grasses", "PlantType": "Grass"},
            ]},
            {"Type": "Tomorrow", "Index": 8.9, "Triggers": []},
        ],
    },
}
POLLENCOM_EMPTY = {"Type": "pollen", "Location": {"ZIP": "00000", "periods": []}}

GEO_AUSTIN = {
    "lat": 30.26715, "lon": -97.74306, "name": "Austin", "admin1": "Texas",
    "cc": "US", "postcodes": ["73301", "78701"],
}


def pk(prefix):
    return prefix + "0" * (64 - len(prefix))


async def add_contact(bot, public_key, *, type_=2, lat=None, lon=None, name=""):
    await bot.db.execute(
        "INSERT INTO contacts (public_key, adv_name, type, adv_lat, adv_lon) "
        "VALUES (?,?,?,?,?)",
        (public_key, name, type_, lat, lon),
    )


async def set_bot_location(bot, lat, lon):
    for k, v in (("self_info.adv_lat", lat), ("self_info.adv_lon", lon)):
        await bot.db.execute(
            "INSERT INTO device_info(key,value,last_updated) VALUES(?,?,0)",
            (k, json.dumps(v)),
        )


def ctx_for(bot, text="!allergy", path=None, path_len=None, hash_mode=None):
    return SimpleNamespace(
        bot=bot, message_text=text, sender_pubkey=None, is_dm=True,
        path=path, path_len=path_len, path_hash_mode=hash_mode,
    )


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("POLLEN_GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(allergy, "_GOOGLE_API_KEY", "")


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("POLLEN_GOOGLE_API_KEY", "test-key")


@pytest.fixture
def stub_net(monkeypatch):
    """Replace every network call with recording stubs; tests tweak the
    returned dict to shape responses."""
    calls = {"geocode": [], "google": [], "pollencom": [], "census": []}
    stubs = {
        "geocode": GEO_AUSTIN, "google": GOOGLE,
        "pollencom": POLLENCOM, "census": "78701",
    }

    def geocode(q, cc):
        calls["geocode"].append((q, cc))
        return stubs["geocode"]

    def google(lat, lon, key):
        calls["google"].append((lat, lon, key))
        if isinstance(stubs["google"], Exception):
            raise stubs["google"]
        return stubs["google"]

    def pollencom(z):
        calls["pollencom"].append(z)
        v = stubs["pollencom"]
        return v(z) if callable(v) else v

    def census(lat, lon):
        calls["census"].append((lat, lon))
        return stubs["census"]

    monkeypatch.setattr(allergy, "_geocode", geocode)
    monkeypatch.setattr(allergy, "_google_fetch", google)
    monkeypatch.setattr(allergy, "_pollencom_fetch", pollencom)
    monkeypatch.setattr(allergy, "_census_zip", census)
    return SimpleNamespace(calls=calls, stubs=stubs)


# --- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("!allergy", ("path", None, None)),
    ("!air   ", ("path", None, None)),
    ("!allergy 78701", ("zip", "78701", "US")),
    ("!air 78701-1234", ("zip", "78701", "US")),
    ("!allergy Austin", ("city", "Austin", None)),
    ("!allergy Austin US", ("city", "Austin", "US")),
    ("!ALLERGY Buenos Aires AR", ("city", "Buenos Aires", "AR")),
    ("!allergy 1234", ("city", "1234", None)),
])
def test_parse_args(text, expected):
    assert allergy._parse_args(text) == expected


# --- formatting --------------------------------------------------------------

def test_format_google_one_line_with_top_plants():
    out = allergy._format_google(GOOGLE, "Austin, Texas")
    assert out == (
        "Austin, Texas: Tree 2/5 low (Elm,Oak), Grass 1/5 vlow (Grasses), "
        "Weed 4/5 high (Ragweed)"
    )
    assert len(out) <= 100


def test_format_google_out_of_season_type_reads_zero():
    data = {"dailyInfo": [{
        "pollenTypeInfo": [{"code": "TREE", "inSeason": False}],
        "plantInfo": [],
    }]}
    assert allergy._format_google(data, "X") == (
        "X: Tree 0/5 none, Grass 0/5 none, Weed 0/5 none"
    )


def test_format_google_no_days_is_none():
    assert allergy._format_google({"dailyInfo": []}, "X") is None


def test_fit_splits_long_replies_into_segments():
    label = "A" * 90
    out = allergy._format_google(GOOGLE, label)
    assert isinstance(out, list)
    assert out[0] == label + ":"
    assert out[1].startswith("Tree 2/5")


def test_format_pollencom_uses_today_and_tomorrow():
    assert allergy._format_pollencom(POLLENCOM) == (
        "Austin TX: 9/12 med-high (Ragweed, Elm, Grasses), tmrw 8.9"
    )


def test_format_pollencom_label_override_and_empty():
    out = allergy._format_pollencom(POLLENCOM, "nr Woodson (92025)")
    assert out.startswith("nr Woodson (92025): 9/12")
    assert allergy._format_pollencom(POLLENCOM_EMPTY) is None


@pytest.mark.parametrize("idx,cat", [
    (0, "low"), (2.4, "low"), (2.5, "low-med"), (4.8, "low-med"),
    (4.9, "med"), (7.2, "med"), (7.3, "med-high"), (9.6, "med-high"),
    (9.7, "high"), (12, "high"),
])
def test_pollencom_category_boundaries(idx, cat):
    assert allergy._pollencom_category(idx) == cat


# --- backend selection -------------------------------------------------------

async def test_google_used_when_key_set(bot_factory, with_key, stub_net):
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy 78701"))
    assert out.startswith("Austin, Texas: Tree 2/5 low")
    assert stub_net.calls["geocode"] == [("78701", "US")]
    assert stub_net.calls["google"] == [(30.26715, -97.74306, "test-key")]
    assert stub_net.calls["pollencom"] == []


async def test_zip_without_key_hits_pollencom_directly(bot_factory, no_key, stub_net):
    out = await allergy.handle(ctx_for(bot_factory(), "!air 78701"))
    assert out == "Austin TX: 9/12 med-high (Ragweed, Elm, Grasses), tmrw 8.9"
    assert stub_net.calls["google"] == []
    assert stub_net.calls["pollencom"] == ["78701"]
    assert stub_net.calls["census"] == []


async def test_city_without_key_uses_first_postcode(bot_factory, no_key, stub_net):
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy Austin US"))
    assert out.startswith("Austin TX: 9/12")
    assert stub_net.calls["geocode"] == [("Austin", "US")]
    assert stub_net.calls["pollencom"] == ["73301"]


async def test_city_retries_census_zip_when_first_postcode_is_empty(
    bot_factory, no_key, stub_net,
):
    stub_net.stubs["pollencom"] = lambda z: POLLENCOM if z == "78701" else POLLENCOM_EMPTY
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy Austin"))
    assert out.startswith("Austin TX: 9/12")
    assert stub_net.calls["pollencom"] == ["73301", "78701"]
    assert stub_net.calls["census"] == [(30.26715, -97.74306)]


async def test_city_without_postcodes_falls_back_to_census(bot_factory, no_key, stub_net):
    stub_net.stubs["geocode"] = {**GEO_AUSTIN, "postcodes": []}
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy Austin"))
    assert out.startswith("Austin TX")
    assert stub_net.calls["pollencom"] == ["78701"]


async def test_google_failure_falls_back_to_pollencom(bot_factory, with_key, stub_net):
    stub_net.stubs["google"] = RuntimeError("429 quota")
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy 78701"))
    assert out.startswith("Austin TX: 9/12")
    assert stub_net.calls["pollencom"] == ["78701"]


async def test_non_us_without_key_explains(bot_factory, no_key, stub_net):
    stub_net.stubs["geocode"] = {
        "lat": 51.5, "lon": -0.12, "name": "London", "admin1": "England",
        "cc": "GB", "postcodes": [],
    }
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy London"))
    assert out.startswith("London, England: no pollen data")
    assert stub_net.calls["pollencom"] == []


async def test_no_geocode_match(bot_factory, no_key, stub_net):
    stub_net.stubs["geocode"] = None
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy Nowhereville XX"))
    assert out == "No match for Nowhereville [XX]"


async def test_zip_with_no_data(bot_factory, no_key, stub_net):
    stub_net.stubs["pollencom"] = POLLENCOM_EMPTY
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy 78701"))
    assert out == "Austin, Texas: no pollen data for 78701"
    assert stub_net.calls["census"] == []


# --- location from the path --------------------------------------------------

async def test_path_uses_first_located_repeater(bot_factory, with_key, stub_net):
    bot = bot_factory()
    await add_contact(bot, pk("aaaa"), name="unlocated")
    await add_contact(bot, pk("abcd"), name="Woodson", lat=33.0, lon=-116.9)
    await add_contact(bot, pk("ef01"), name="Far", lat=34.0, lon=-118.0)
    ctx = ctx_for(bot, "!allergy", path="aaaaabcdef01", path_len=3, hash_mode=1)
    out = await allergy.handle(ctx)
    assert out.startswith("nr Woodson: Tree 2/5")
    assert stub_net.calls["geocode"] == []
    assert stub_net.calls["google"] == [(33.0, -116.9, "test-key")]


async def test_path_without_key_reverse_geocodes_via_census(bot_factory, no_key, stub_net):
    bot = bot_factory()
    await add_contact(bot, pk("abcd"), name="Woodson", lat=33.0, lon=-116.9)
    stub_net.stubs["census"] = "92025"
    ctx = ctx_for(bot, "!air", path="abcd", path_len=1, hash_mode=1)
    out = await allergy.handle(ctx)
    assert out == "nr Woodson (92025): 9/12 med-high (Ragweed, Elm, Grasses), tmrw 8.9"
    assert stub_net.calls["census"] == [(33.0, -116.9)]
    assert stub_net.calls["pollencom"] == ["92025"]


async def test_path_with_no_located_hop(bot_factory, with_key, stub_net):
    bot = bot_factory()
    await add_contact(bot, pk("abcd"), name="unlocated")
    ctx = ctx_for(bot, "!allergy", path="abcd", path_len=1, hash_mode=1)
    out = await allergy.handle(ctx)
    assert out == "no located repeater in path (1h); try !allergy <city|zip>"
    assert stub_net.calls["google"] == []


async def test_direct_message_uses_bot_location(bot_factory, with_key, stub_net):
    bot = bot_factory()
    await set_bot_location(bot, 32.0, -96.0)
    out = await allergy.handle(ctx_for(bot, "!allergy"))
    assert out.startswith("nr bot: Tree 2/5")
    assert stub_net.calls["google"] == [(32.0, -96.0, "test-key")]


async def test_direct_message_without_bot_location(bot_factory, with_key, stub_net):
    out = await allergy.handle(ctx_for(bot_factory(), "!allergy"))
    assert out == "direct (no path) and bot location unset; try !allergy <city|zip>"


def test_hops_split_by_hash_mode():
    assert allergy._hops(SimpleNamespace(path="abcdef", path_len=3, path_hash_mode=0)) == ["ab", "cd", "ef"]
    assert allergy._hops(SimpleNamespace(path="abcdef01", path_len=2, path_hash_mode=1)) == ["abcd", "ef01"]
    assert allergy._hops(SimpleNamespace(path="", path_len=0, path_hash_mode=0)) is None
    assert allergy._hops(SimpleNamespace(path="ab", path_len=255, path_hash_mode=0)) is None
    assert allergy._hops(SimpleNamespace(path="ab", path_len=2, path_hash_mode=0)) is None
