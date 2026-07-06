"""Contact-sync aging integrity: the incremental watermark must never move
backward (an empty incremental response's CONTACT_END reports lastmod=0), and
last_synced_at only refreshes for contacts whose radio lastmod actually
changed — so a full dump cannot flatten every contact's observed age (which
would blind eviction staleness and the Contacts UI)."""

from types import SimpleNamespace


def contacts_event(contacts, lastmod):
    return SimpleNamespace(
        type=SimpleNamespace(name="CONTACTS"),
        payload=contacts,
        attributes={"lastmod": lastmod},
    )


def wire(bot, events):
    """get_contacts stub yielding scripted responses; records watermarks."""
    calls = []

    async def get_contacts(lastmod=0, timeout=10):
        calls.append(lastmod)
        return events.pop(0)

    bot.mc = SimpleNamespace(commands=SimpleNamespace(get_contacts=get_contacts))
    return calls


async def watermark(bot):
    row = await bot.db.fetchone(
        "SELECT value FROM bot_meta WHERE key='contacts_lastmod'"
    )
    return row["value"] if row else None


async def synced_at(bot, pk):
    row = await bot.db.fetchone(
        "SELECT last_synced_at FROM contacts WHERE public_key=?", (pk,)
    )
    return row["last_synced_at"] if row else None


def c(pk, lastmod, name="n"):
    return {pk: {"adv_name": name, "type": 1, "lastmod": lastmod,
                 "last_advert": 0}}


async def test_empty_sync_does_not_reset_watermark(bot_factory):
    # the field bug: eviction/advert marks contacts dirty, the sync finds
    # nothing changed, CONTACT_END carries lastmod=0, the bot stored it —
    # and the NEXT sync became a full 342-contact dump.
    bot = bot_factory()
    calls = wire(bot, [
        contacts_event(c("aa" * 32, 5000), 5000),  # initial: one contact
        contacts_event({}, 0),                     # nothing changed since
    ])
    await bot.sync_contacts()
    assert await watermark(bot) == "5000"
    await bot.sync_contacts()
    assert await watermark(bot) == "5000", "empty response must not zero it"
    assert calls == [0, 5000], "next request stays incremental"


async def test_watermark_advances_on_changes(bot_factory):
    bot = bot_factory()
    wire(bot, [
        contacts_event(c("aa" * 32, 5000), 5000),
        contacts_event(c("aa" * 32, 7000), 7000),
    ])
    await bot.sync_contacts()
    await bot.sync_contacts()
    assert await watermark(bot) == "7000"


async def test_full_dump_does_not_flatten_ages(bot_factory):
    bot = bot_factory()
    pk_old, pk_fresh = "aa" * 32, "bb" * 32
    both = {**c(pk_old, 100, "old"), **c(pk_fresh, 200, "fresh")}
    wire(bot, [contacts_event(both, 200)])
    await bot.sync_contacts()
    # backdate the observed ages, as if days had passed
    await bot.db.execute(
        "UPDATE contacts SET last_synced_at=1000 WHERE public_key=?", (pk_old,)
    )
    await bot.db.execute(
        "UPDATE contacts SET last_synced_at=2000 WHERE public_key=?", (pk_fresh,)
    )
    # a full dump arrives in which only pk_fresh actually changed
    dump = {**c(pk_old, 100, "old"), **c(pk_fresh, 300, "fresh")}
    wire(bot, [contacts_event(dump, 300)])
    await bot.sync_contacts()
    assert await synced_at(bot, pk_old) == 1000, \
        "unchanged contact keeps its observed age through a full dump"
    fresh = await synced_at(bot, pk_fresh)
    assert fresh > 2000, "changed contact's observed age refreshes"


async def test_new_contact_gets_current_synced_at(bot_factory):
    bot = bot_factory()
    wire(bot, [contacts_event(c("cc" * 32, 50), 50)])
    await bot.sync_contacts()
    assert (await synced_at(bot, "cc" * 32)) > 0, "insert stamps sync time"
