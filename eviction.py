"""Radio contact-table eviction: staleness ordering and victim selection."""

# plausible-timestamp window for a contact's last_advert (sender-clock, often
# garbage): between 2020-01-01 and a day in the future. Outside this, treat as
# unknown (0) for staleness ordering.
_TS_MIN = 1577836800   # 2020-01-01 UTC


def _contact_staleness_key(c: dict, db_synced_at: dict, now: int) -> tuple:
    """Lower sorts first (= evicted first). Fuse the most trustworthy
    last-seen signal available for a radio contact:
      1. the bot DB's last_synced_at (bot clock; incremental sync makes this
         ≈ last time the contact changed on the radio),
      2. else the radio's lastmod (radio clock; absolute value may be off but
         relative order is consistent),
      3. else last_advert if it's a plausible timestamp,
    with (lastmod, pubkey) as a deterministic tie-break."""
    pk = (c.get("public_key") or "").lower()
    synced = db_synced_at.get(pk)
    lastmod = c.get("lastmod") or 0
    last_advert = c.get("last_advert") or 0
    if not (_TS_MIN < last_advert < now + 86400):
        last_advert = 0
    primary = synced if synced else (lastmod if lastmod > 0 else last_advert)
    return (primary or 0, lastmod, pk)


def select_eviction_victims(
    contacts: list, *, protected_pubkeys: set, protected_types: set,
    db_synced_at: dict, need: int, now: int,
) -> list:
    """Pick up to `need` contacts to evict from a fresh radio dump, stalest
    first. Pure (clock injected) so it is unit-testable. `contacts` is a list
    of radio contact dicts; returns the chosen dicts."""
    if need <= 0:
        return []
    eligible = [
        c for c in contacts
        if (c.get("public_key") or "").lower() not in protected_pubkeys
        and c.get("type") not in protected_types
    ]
    eligible.sort(key=lambda c: _contact_staleness_key(c, db_synced_at, now))
    return eligible[:need]
