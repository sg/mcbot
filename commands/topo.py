# !topo — plot a contact's location on OpenTopoMap (with a da.gd short link)
#
# usage:
#   !topo <pubkey-prefix>   plot that contact's location (prefix 4+ hex chars)
#   !topo                   plot the sender's own location, if known
#   !topo help              show usage
#
# - a prefix matching more than one contact returns a disambiguation list
#   (first 10 hex of pubkey + first 15 of name, one per line) so the sender can
#   retry with a longer prefix.
# - works in channels and DMs.

import asyncio

import requests

NAME = "topo"
TRIGGERS = ["!topo"]
DESCRIPTION = "Plot a contact's location on OpenTopoMap"
COOLDOWN_DEFAULT = 10
ALLOWED_CHANNELS = None  # any decryptable channel
ALLOW_DM = True

_MIN_PREFIX = 4        # minimum pubkey-prefix length, in hex chars
_MAX_MATCHES = 10      # cap the disambiguation list
_USAGE = "!topo [pub-key prefix] (4+ chars)"


def _shorten_sync(long_url):
    # shorten via da.gd (same service !path uses)
    r = requests.get("https://da.gd/s", params={"url": long_url}, timeout=8)
    r.raise_for_status()
    s = r.text.strip()
    if not s.startswith("http"):
        raise ValueError(f"shortener error: {s[:60]}")
    return s


def _topo_url(lat, lon):
    # OpenTopoMap marker link at zoom 16
    return f"https://opentopomap.org/#marker=16/{lat:.5f}/{lon:.5f}"


def _has_geo(row):
    return (
        row["adv_lat"] is not None and row["adv_lat"] != 0
        and row["adv_lon"] is not None and row["adv_lon"] != 0
    )


async def _map_reply(ctx, name, contact_name, lat, lon):
    # build the OpenTopoMap link and shorten it; if da.gd is unreachable fall
    # back to the full URL (short enough to send) rather than failing.
    url = _topo_url(lat, lon)
    try:
        url = await asyncio.to_thread(_shorten_sync, url)
    except Exception:
        ctx.bot.logger.exception("topo: shorten failed")
    label = (contact_name or "").strip() or "(no name)"
    return f"@[{name}] {label} {url}"


def _conflict_list(name, rows):
    # ambiguous prefix: list matches (first line addresses the sender; paginate
    # joins the rest with newlines) so the sender can retry with more chars.
    shown = rows[:_MAX_MATCHES]
    out = [f"@[{name}] {len(rows)} matches:"]
    for r in shown:
        cn = (r["adv_name"] or "").strip() or "(no name)"
        out.append(f"{r['public_key'][:10]} {cn[:15]}")
    extra = len(rows) - len(shown)
    if extra > 0:
        out.append(f"…{extra} more (use a longer prefix)")
    return out


async def _own_location(ctx, name):
    pk = ctx.sender_pubkey
    if not pk and ctx.sender_pubkey_prefix:
        pk, _ = await ctx.bot.resolve_prefix(ctx.sender_pubkey_prefix)
    row = None
    if pk:
        row = await ctx.bot.db.fetchone(
            "SELECT adv_name, adv_lat, adv_lon FROM contacts WHERE public_key=?",
            (pk.lower(),),
        )
    if not row or not _has_geo(row):
        return f"@[{name}] can't find your location"
    return await _map_reply(ctx, name, row["adv_name"], row["adv_lat"], row["adv_lon"])


async def handle(ctx):
    parts = ctx.message_text.split()
    args = parts[1:]
    name = (
        ctx.sender_name
        or ctx.sender_pubkey_prefix
        or (ctx.sender_pubkey[:12] if ctx.sender_pubkey else None)
        or "unknown"
    )

    # no argument -> the sender's own location
    if not args:
        return await _own_location(ctx, name)

    arg = args[0]
    if arg.lower() == "help":
        return f"@[{name}] {_USAGE}"
    if len(arg) < _MIN_PREFIX:
        return f"@[{name}] {_USAGE}"

    prefix = arg.lower()
    rows = await ctx.bot.db.fetchall(
        "SELECT public_key, adv_name, adv_lat, adv_lon FROM contacts "
        "WHERE substr(public_key,1,?)=? ORDER BY adv_name",
        (len(prefix), prefix),
    )
    if not rows:
        return f"@[{name}] no contact matches '{arg}'"
    if len(rows) > 1:
        return _conflict_list(name, rows)

    row = rows[0]
    if not _has_geo(row):
        cn = (row["adv_name"] or "").strip() or "(no name)"
        return f"@[{name}] {cn} has no location"
    return await _map_reply(ctx, name, row["adv_name"], row["adv_lat"], row["adv_lon"])
