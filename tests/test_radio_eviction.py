"""Radio contact-table rollover (eviction): victim selection is a pure
function; evict_radio_contacts is exercised against a stubbed radio."""

from types import SimpleNamespace

from mcbot import parse_contact_types, select_eviction_victims


class StubEvent:
    def __init__(self, name, payload):
        self.type = SimpleNamespace(name=name)
        self.payload = payload
        self.attributes = {}


class StubCommands:
    def __init__(self, contacts_payload, *, max_contacts=350, remove_script=None):
        self._contacts = contacts_payload
        self.max_contacts = max_contacts
        self.removed = []
        self.get_calls = 0
        self._remove_script = remove_script or {}

    async def get_contacts(self, lastmod=0, timeout=10):
        self.get_calls += 1
        return StubEvent("CONTACTS", dict(self._contacts))

    async def send_device_query(self):
        return StubEvent("DEVICE_INFO", {"max_contacts": self.max_contacts})

    async def remove_contact(self, pk):
        self.removed.append(pk)
        return StubEvent(self._remove_script.get(pk, "OK"), {})


def pk(i):
    return f"{i:064x}"


def make_contacts(n, *, type_=1, base_lastmod=1000):
    # contact i is stalest at i=0 (lowest lastmod). payload keyed by pubkey,
    # mirroring the radio dump (the value dict has no public_key field).
    return {
        pk(i): {
            "type": type_, "lastmod": base_lastmod + i,
            "last_advert": 0, "adv_name": f"node{i}",
        }
        for i in range(n)
    }


# ----- unit: parse_contact_types ----------------------------------------
def test_parse_contact_types():
    bad = []
    s = parse_contact_types("repeater, room", on_error=bad.append)
    assert s == {2, 3}, "names map to type ints"
    assert parse_contact_types("2,3") == {2, 3}, "ints pass through"
    assert parse_contact_types("rep sens") == {2, 4}, "aliases + whitespace"
    parse_contact_types("bogus", on_error=bad.append)
    assert bad == ["bogus"], "unknown token reported"
    assert parse_contact_types("") == set(), "empty -> empty set"


# ----- unit: select_eviction_victims ------------------------------------
def test_select_basic_order():
    contacts = [{**c, "public_key": k} for k, c in make_contacts(10).items()]
    v = select_eviction_victims(
        contacts, protected_pubkeys=set(), protected_types=set(),
        db_synced_at={}, need=3, now=2_000_000_000,
    )
    assert [c["public_key"] for c in v] == [pk(0), pk(1), pk(2)], \
        "stalest (lowest lastmod) chosen first"
    assert select_eviction_victims(
        contacts, protected_pubkeys=set(), protected_types=set(),
        db_synced_at={}, need=0, now=1) == [], "need<=0 -> []"


def test_select_db_synced_overrides_lastmod():
    contacts = [{**c, "public_key": k} for k, c in make_contacts(5).items()]
    # pk(4) has the highest lastmod but the OLDEST db sync -> evicted first
    synced = {pk(0): 9000, pk(1): 9001, pk(2): 9002, pk(3): 9003, pk(4): 10}
    v = select_eviction_victims(
        contacts, protected_pubkeys=set(), protected_types=set(),
        db_synced_at=synced, need=1, now=2_000_000_000,
    )
    assert v[0]["public_key"] == pk(4), "db last_synced_at drives staleness"


def test_select_garbage_last_advert():
    # no lastmod, no db sync; last_advert is garbage (yr 2000 / 2102) -> 0
    contacts = [
        {"public_key": pk(0), "type": 1, "lastmod": 0, "last_advert": 946684800},
        {"public_key": pk(1), "type": 1, "lastmod": 0, "last_advert": 4170000000},
        {"public_key": pk(2), "type": 1, "lastmod": 0, "last_advert": 1700000000},
    ]
    v = select_eviction_victims(
        contacts, protected_pubkeys=set(), protected_types=set(),
        db_synced_at={}, need=2, now=1_750_000_000,
    )
    picked = {c["public_key"] for c in v}
    # pk(2) has a plausible recent advert -> kept; the two garbage ones go
    assert picked == {pk(0), pk(1)}, "garbage last_advert treated as stalest"


def test_select_protection():
    contacts = [{**c, "public_key": k} for k, c in make_contacts(6).items()]
    contacts[0]["type"] = 2  # repeater
    v = select_eviction_victims(
        contacts, protected_pubkeys={pk(1)}, protected_types={2},
        db_synced_at={}, need=10, now=2_000_000_000,
    )
    picked = {c["public_key"] for c in v}
    assert pk(0) not in picked, "protected type excluded"
    assert pk(1) not in picked, "protected pubkey excluded"
    assert picked == {pk(2), pk(3), pk(4), pk(5)}, "rest eligible"


# ----- integration: evict_radio_contacts --------------------------------
async def test_evict_headroom_and_archive(bot_factory):
    bot = bot_factory(radio_evict_headroom=8)
    payload = make_contacts(350)
    bot.mc = SimpleNamespace(
        contacts={k: {} for k in payload}, commands=StubCommands(payload),
    )
    # an archive row that coincides with a victim must survive eviction
    await bot.db.execute(
        "INSERT INTO contacts(public_key, adv_name, last_synced_at) "
        "VALUES (?,?,?)", (pk(0), "node0", 5),
    )
    res = await bot.evict_radio_contacts(target_free=8)
    assert res["used"] == 350 and res["max"] == 350, "used/max reported"
    assert len(res["evicted"]) == 8, "evicted to headroom (8)"
    assert res["evicted"][0]["pubkey"] == pk(0), "stalest evicted first"
    assert bot._contacts_dirty is True, "_contacts_dirty set"
    assert pk(0) not in bot.mc.contacts, "lib contact cache popped"
    row = await bot.db.fetchone(
        "SELECT 1 FROM contacts WHERE public_key=?", (pk(0),)
    )
    assert row is not None, "DB archive row NOT deleted"


async def test_evict_dry_run(bot_factory):
    bot = bot_factory()
    payload = make_contacts(350)
    stub = StubCommands(payload)
    bot.mc = SimpleNamespace(contacts={}, commands=stub)
    res = await bot.evict_radio_contacts(target_free=8, dry_run=True)
    assert res["dry_run"] is True and len(res["evicted"]) == 8, "dry-run lists 8"
    assert stub.removed == [], "dry-run removes nothing"
    assert bot._contacts_dirty is False, "dry-run leaves dirty flag unset"


async def test_evict_max_per_run_cap(bot_factory):
    bot = bot_factory(radio_evict_max_per_run=3)
    payload = make_contacts(350)
    stub = StubCommands(payload)
    bot.mc = SimpleNamespace(contacts={}, commands=stub)
    res = await bot.evict_radio_contacts(target_free=50)  # would want 50
    assert len(res["evicted"]) == 3, "per-run cap honored"


async def test_evict_consecutive_error_abort(bot_factory):
    bot = bot_factory()
    payload = make_contacts(350)
    # every removal fails
    script = {k: "ERROR" for k in payload}
    stub = StubCommands(payload, remove_script=script)
    bot.mc = SimpleNamespace(contacts={}, commands=stub)
    res = await bot.evict_radio_contacts(target_free=8)
    assert len(stub.removed) == 3, "aborts after 3 consecutive errors"
    assert res["evicted"] == [] and res["failed"] == 3, "nothing evicted"
    assert res["shortfall"] == 8, "shortfall reported"


async def test_evict_protection_and_shortfall(bot_factory):
    bot = bot_factory(radio_evict_headroom=5)
    payload = make_contacts(350)
    bot.mc = SimpleNamespace(contacts={}, commands=StubCommands(payload))
    # protect all but 2 contacts via bot_users -> can't reach target
    for i in range(348):
        await bot.db.execute(
            "INSERT INTO bot_users(pubkey) VALUES (?)", (pk(i),)
        )
    res = await bot.evict_radio_contacts(target_free=5)
    assert res["eligible"] == 2, "only 2 eligible after protection"
    assert len(res["evicted"]) == 2, "evicted all eligible"
    assert res["shortfall"] == 3, "shortfall when protection blocks target"


async def test_contacts_full_handler_gate(bot_factory):
    bot = bot_factory(radio_evict_enabled=False)
    stub = StubCommands(make_contacts(350))
    bot.mc = SimpleNamespace(contacts={}, commands=stub)
    await bot._on_contacts_full(SimpleNamespace(payload={}))
    assert stub.get_calls == 0, "disabled policy: handler does not evict"
