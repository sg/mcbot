# !path — routing path with distance and a map link
# 
# reports the path of the sender's own inbound message. 
# optionally,takes a path string as an argument and reports on that instead.
# 
# response format:
#     @[<sender>] [<n>h] <hop>,<hop>,... route: ~X.Xmi, direct: ~Y.Ymi, <url>
# 
# - <n>h         hop count; <hops> are per-hop pubkey-prefix hashes.
# - route        great-circle distance summed between consecutive located hops.
# - direct       great-circle distance between the first and last located hop.
# - <url>        da.gd shortened geojson.io map drawing the route.
# - distances are miles by default. pass 'k' for kilometers:  !path k
# - if some hops can't be located, a (located/total) count is shown and the
#   distances/map use only the located hops.
# - with only one located hop, distance can't be computed but a map with
#   that single pin is still generated.
# 
# geo locations for each hop are pulled from stored contacts and cached.
#

import asyncio
import json
import math
import urllib.parse

import requests

NAME = "path"
TRIGGERS = ["!path"]
DESCRIPTION = "Routing path + distance + map link ('!path k' for kilometers)"
COOLDOWN_DEFAULT = 10
ALLOWED_CHANNELS = None  # allow in any decryptable channel
ALLOW_DM = True

# Earth radius for the great-circle (Haversine) calculation.
_EARTH_MI = 3958.7613
_EARTH_KM = 6371.0088

# https://en.wikipedia.org/wiki/Haversine_formula
def _haversine(lat1, lon1, lat2, lon2, unit):
    r = _EARTH_MI if unit == "mi" else _EARTH_KM
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


async def _resolve_hop_named(ctx, hop_hex):
    # "map a path-hop hex prefix to (lat, lon, name), or None when it
    # doesn't resolve to exactly one contact carrying a location.
    rows = await ctx.bot.db.fetchall(
        "SELECT adv_lat, adv_lon, adv_name FROM contacts "
        "WHERE substr(public_key,1,?)=? "
        "AND adv_lat IS NOT NULL AND adv_lat!=0 AND adv_lon!=0",
        (len(hop_hex), hop_hex),
    )
    if len(rows) == 1:
        return (rows[0]["adv_lat"], rows[0]["adv_lon"], rows[0]["adv_name"])
    return None


def _pin_symbol(i):
    # geojson.io marker symbol for the i-th pin (i starts at 1)
    # 1-9, then A-Z (enough for 2-byte/32-hop paths)
    # returns None past 35, so those pins just get no marker
    if 1 <= i <= 9:
        return str(i)
    idx = i - 10
    if 0 <= idx < 26:
        return chr(ord("A") + idx)
    return None


def _geojson_io_url(located_named):
    # build a geojson.io URL drawing a line-string through the
    # located hops plus a labeled point for each hop.
    # geojson.io understands 'title' (shown in the click popup)
    # and 'marker-symbol' (the hop index drawn inside the pin)
    coords = [[lon, lat] for lat, lon, _ in located_named]
    features = []
    # a LineString needs at least 2 poistions
    # if only one hop is located, it is drawn as just its pin
    if len(coords) >= 2:
        features.append({
            "type": "Feature",
            "properties": {"title": "route"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    for i, (lat, lon, nm) in enumerate(located_named, 1):
        props = {"title": f"{i}. {nm or '?'}"}
        sym = _pin_symbol(i)
        if sym:
            props["marker-symbol"] = sym
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })
    fc = {"type": "FeatureCollection", "features": features}
    data = json.dumps(fc, separators=(",", ":"))
    # geojson.io expects encodeURIComponent(JSON.stringify(...)). quote()'s
    # default safe="/" leaves slashes raw, so a hop name containing '/' puts a
    # bare '/' in the JSON payload and geojson.io mis-parses it ("unterminated
    # string"). safe="" matches encodeURIComponent and encodes the '/'.
    enc = urllib.parse.quote(data, safe="")
    return "https://geojson.io/#data=data:application/json," + enc


def _shorten_sync(long_url):
    # shorten via da.gd
    # 
    r = requests.get(
        "https://da.gd/s", params={"url": long_url}, timeout=8,
    )
    r.raise_for_status()
    s = r.text.strip()
    if not s.startswith("http"):
        raise ValueError(f"shortener error: {s[:60]}")
    return s


def _parse_path_arg(path_arg):
    # parse provided path string argument into path_hex, path_len, 
    # hash_mode. returns an error string on bad input.
    hops = [h.strip().lower() for h in path_arg.split(",") if h.strip()]
    if not hops:
        return "empty path"
    hop_len = len(hops[0])
    if hop_len not in (2, 4, 6) or any(len(h) != hop_len for h in hops):
        return "hops must be uniform 1/2/3-byte hex, e.g. d690,abcd,4f3d"
    for h in hops:
        try:
            bytes.fromhex(h)
        except ValueError:
            return f"invalid hex hop: {h!r}"
    bytes_per_hop = hop_len // 2
    return ("".join(hops), len(hops), bytes_per_hop - 1)


async def handle(ctx):
    # args: 'k' switches distances to kilometers. anything else is treated
    # as a comma-separated path string.
    parts = ctx.message_text.split()
    args = parts[1:]
    unit = "km" if any(t.lower() == "k" for t in args) else "mi"
    u = "mi" if unit == "mi" else "km"

    # try to identify sender using adv_name or 6-byte pubkey prefix.
    # if not found, set to "unknown".
    name = (
        ctx.sender_name
        or ctx.sender_pubkey_prefix
        or (ctx.sender_pubkey[:12] if ctx.sender_pubkey else None)
        or "unknown"
    )

    # optional path argument s the first non-'k' argument. 
    # if no path arg, them sender's own inbound message path is used.
    path_arg = next((t for t in args if t.lower() != "k"), None)
    if path_arg:
        parsed = _parse_path_arg(path_arg)
        if isinstance(parsed, str):
            return f"@[{name}] {parsed}"
        path_hex, path_len, hash_mode = parsed
    else:
        path_hex = (ctx.path or "").lower()
        path_len = ctx.path_len
        hash_mode = ctx.path_hash_mode

    # no path, direct neighbor, or path_len is 0 or 255
    if not path_hex or not path_len or path_len in (0, 255):
        return f"@[{name}] direct (no path)"

    # meshcore packs hash_mode in 2 bits of the path byte:
    # bytes_per_hop = hash_mode + 1   (1, 2, or 3)
    if hash_mode is None or hash_mode < 0:
        hash_mode = 0
    chars_per_hop = (hash_mode + 1) * 2

    expected = chars_per_hop * path_len
    if len(path_hex) < expected:
        return f"@[{name}] [{path_len}h] {path_hex} (truncated)"

    hops = [
        path_hex[i:i + chars_per_hop]
        for i in range(0, expected, chars_per_hop)
    ]
    path_str = ",".join(hops)
    nh = len(hops)

    # resolve hops with names for the map markers
    named = [await _resolve_hop_named(ctx, h) for h in hops]
    loc = [p for p in named if p]
    n_located = len(loc)

    # no located hops at all so nothing to map or measure
    if n_located == 0:
        return f"@[{name}] [{nh}h] {path_str} dist/map unavailable (0/{nh})"

    # map link for any 1 or more located hops
    try:
        short = await asyncio.to_thread(_shorten_sync, _geojson_io_url(loc))
    except Exception:
        ctx.bot.logger.exception("path map: shorten failed")
        short = None
    map_part = f", {short}" if short else " (map err)"

    # one located hop can't compute a distance, but still map the pin.
    if n_located == 1:
        return f"@[{name}] [{nh}h] {path_str} dist unavailable ({n_located}/{nh}){map_part}"

    # distances over the located hops
    rt = sum(
        _haversine(loc[i][0], loc[i][1], loc[i + 1][0], loc[i + 1][1], unit)
        for i in range(len(loc) - 1)
    )
    direct = _haversine(loc[0][0], loc[0][1], loc[-1][0], loc[-1][1], unit)
    cov = "" if n_located == nh else f" ({n_located}/{nh})"
    dist = f"route: ~{rt:.1f}{u}, direct: ~{direct:.1f}{u}{cov}"
    return f"@[{name}] [{nh}h] {path_str} {dist}{map_part}"
