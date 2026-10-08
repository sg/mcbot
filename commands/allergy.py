# !allergy / !air / !pollen — current airborne pollen levels for a city or US
# zip, or for the area around the first located repeater in the message path.
#
# usage:
#   !allergy <zip>          US 5-digit zip              (!allergy 78701)
#   !allergy <city> [CC]    city, optional country code (!allergy Austin US)
#   !allergy                no argument: the first located repeater in the
#                           inbound path; a direct message (no path) uses
#                           the bot's own location
#   !air / !pollen ...      aliases for !allergy
#
# data sources:
#   - Google Pollen API when POLLEN_GOOGLE_API_KEY is set: 80+ countries,
#     tree/grass/weed index 0-5 plus the top plants per type. 5,000 free
#     requests/month, but the Google Cloud project needs billing enabled.
#   - pollen.com (IQVIA) when no key is set or Google fails: US-only, one
#     0-12 index plus the top trigger plants. this is the undocumented
#     endpoint behind pollen.com's own site, so it may break without notice.
#   - Open-Meteo geocoding turns a city or zip into coordinates (no key); the
#     US Census geocoder turns repeater coordinates into a zip for pollen.com.
#

import asyncio
import importlib.util
import os
import re
from pathlib import Path

import requests

NAME = "allergy"
TRIGGERS = ["!allergy", "!air", "!pollen"]
DESCRIPTION = "Pollen levels: !allergy <city|zip> (no arg = nearest repeater)"
COOLDOWN_DEFAULT = 30
ALLOWED_CHANNELS = [
    "#wx",
    "#bot",
]
ALLOW_DM = True

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_GOOGLE_URL = "https://pollen.googleapis.com/v1/forecast:lookup"
_POLLENCOM_URL = "https://www.pollen.com/api/forecast/current/pollen/{zip}"
_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

# Google Pollen API key, or set the POLLEN_GOOGLE_API_KEY env var (preferred —
# this file is tracked in git, so a key written here would be committed).
# Unset = pollen.com only.
_GOOGLE_API_KEY = ""

_HEADERS = {
    "Accept-Encoding": "gzip",
    "user-agent": "mcbot-allergy/1.0",
}
# pollen.com answers 405 unless the request looks like it came from its site.
_POLLENCOM_HEADERS = {**_HEADERS, "Referer": "https://www.pollen.com"}

# channel replies get the tighter cap (the radio prepends "<name>: " on TX),
# so a reply that fits this fits everywhere as one line.
_ONE_LINE_MAX = 100

_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")

_GOOGLE_CAT = {
    "None": "none",
    "Very Low": "vlow",
    "Low": "low",
    "Moderate": "mod",
    "High": "high",
    "Very High": "vhigh",
}
# plant -> pollen type when a plant entry carries no plantDescription.type;
# every other plant Google reports is a tree.
_GOOGLE_PLANT_TYPE = {
    "GRAMINALES": "GRASS",
    "RAGWEED": "WEED",
    "MUGWORT": "WEED",
}

_PATHCMD = None


def _google_key() -> str | None:
    k = os.environ.get("POLLEN_GOOGLE_API_KEY") or _GOOGLE_API_KEY
    return (k or "").strip() or None


def _parse_args(text: str) -> tuple[str, str | None, str | None]:
    # returns (mode, value, country_code) with mode in {"path", "zip", "city"}.
    # a trailing 2-uppercase-letter token on a city is its country code.
    body = re.sub(
        r"^!(allergy|air|pollen)\b\s*", "", text.strip(), count=1, flags=re.IGNORECASE,
    ).strip()
    if not body:
        return "path", None, None
    m = _ZIP_RE.match(body)
    if m:
        return "zip", m.group(1), "US"
    parts = body.rsplit(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z]{2}", parts[1]):
        return "city", parts[0], parts[1]
    return "city", body, None


def _fit(label: str, parts: list[str]):
    # one line when it fits the channel cap; otherwise segments the
    # dispatcher packs into as few messages as it can.
    line = f"{label}: {', '.join(parts)}"
    if len(line) <= _ONE_LINE_MAX:
        return line
    return [f"{label}:", *parts]


# --- geocoding ---------------------------------------------------------------

def _geocode(query: str, cc: str | None) -> dict | None:
    # Open-Meteo resolves place names and postal codes alike; the country
    # filter keeps a US zip from matching a foreign postal code.
    params = {"name": query, "count": "1", "language": "en", "format": "json"}
    if cc:
        params["countryCode"] = cc
    r = requests.get(_GEOCODE_URL, params=params, headers=_HEADERS, timeout=6)
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        return None
    g = results[0]
    return {
        "lat": g["latitude"],
        "lon": g["longitude"],
        "name": g.get("name") or query,
        "admin1": g.get("admin1"),
        "cc": g.get("country_code") or cc,
        "postcodes": g.get("postcodes") or [],
    }


def _place_label(g: dict) -> str:
    name, admin1 = g["name"], g.get("admin1")
    return f"{name}, {admin1}" if admin1 and admin1 != name else name


def _census_zip(lat: float, lon: float) -> str | None:
    # the default layer set omits ZIP Code Tabulation Areas, and the ZCTA
    # layer's name carries the census year, so ask for everything and pick
    # the row that has a ZCTA5 field.
    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "all",
        "format": "json",
    }
    r = requests.get(_CENSUS_URL, params=params, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    geos = ((r.json() or {}).get("result") or {}).get("geographies") or {}
    for rows in geos.values():
        for row in rows or []:
            if row.get("ZCTA5"):
                return str(row["ZCTA5"])
    return None


# --- Google Pollen API -------------------------------------------------------

def _google_fetch(lat: float, lon: float, key: str) -> dict:
    params = {
        "key": key,
        "location.latitude": lat,
        "location.longitude": lon,
        "days": "1",
        "languageCode": "en",
    }
    r = requests.get(_GOOGLE_URL, params=params, headers=_HEADERS, timeout=6)
    r.raise_for_status()
    return r.json() or {}


def _format_google(data: dict, label: str):
    days = data.get("dailyInfo") or []
    if not days:
        return None
    day = days[0]
    # indexInfo is absent for a type or plant that is out of season.
    idx = {}
    for t in day.get("pollenTypeInfo") or []:
        info = t.get("indexInfo") or {}
        idx[t.get("code")] = (info.get("value") or 0, info.get("category") or "None")
    plants: dict[str, list] = {"TREE": [], "GRASS": [], "WEED": []}
    for p in day.get("plantInfo") or []:
        v = (p.get("indexInfo") or {}).get("value") or 0
        if not v:
            continue
        ptype = (
            (p.get("plantDescription") or {}).get("type")
            or _GOOGLE_PLANT_TYPE.get(p.get("code"), "TREE")
        )
        name = p.get("displayName") or str(p.get("code", "?")).title()
        plants.setdefault(ptype, []).append((v, name))
    parts = []
    for code, word in (("TREE", "Tree"), ("GRASS", "Grass"), ("WEED", "Weed")):
        v, cat = idx.get(code, (0, "None"))
        seg = f"{word} {v}/5 {_GOOGLE_CAT.get(cat, cat.lower())}"
        top = [n for _, n in sorted(plants.get(code, []), key=lambda x: -x[0])[:2]]
        if top:
            seg += f" ({','.join(top)})"
        parts.append(seg)
    return _fit(label, parts)


# --- pollen.com --------------------------------------------------------------

def _pollencom_fetch(zip5: str) -> dict:
    r = requests.get(
        _POLLENCOM_URL.format(zip=zip5), headers=_POLLENCOM_HEADERS, timeout=6,
    )
    r.raise_for_status()
    return r.json() or {}


def _pollencom_category(index: float) -> str:
    # pollen.com's own legend for its 0-12 scale
    if index < 2.5:
        return "low"
    if index < 4.9:
        return "low-med"
    if index < 7.3:
        return "med"
    if index < 9.7:
        return "med-high"
    return "high"


def _format_pollencom(data: dict, label: str | None = None):
    loc = data.get("Location") or {}
    periods = loc.get("periods") or []
    if not periods:
        return None
    by_type = {p.get("Type"): p for p in periods}
    today = by_type.get("Today") or periods[0]
    idx = float(today.get("Index") or 0)
    triggers = [t.get("Name") for t in today.get("Triggers") or [] if t.get("Name")]
    if not label:
        city = (loc.get("City") or "").title()
        label = f"{city} {loc.get('State') or ''}".strip() or str(loc.get("ZIP") or "?")
    head = f"{idx:g}/12 {_pollencom_category(idx)}"
    if triggers:
        head += f" ({', '.join(triggers)})"
    parts = [head]
    tmrw = by_type.get("Tomorrow")
    if tmrw and tmrw.get("Index") is not None:
        parts.append(f"tmrw {float(tmrw['Index']):g}")
    return _fit(label, parts)


# --- location from the inbound path -----------------------------------------

def _hops(ctx) -> list[str] | None:
    # split the packed inbound path into per-hop hashes (None when direct)
    path_hex = (getattr(ctx, "path", None) or "").lower()
    n = getattr(ctx, "path_len", None)
    if not path_hex or not n or n in (0, 255):
        return None
    hm = getattr(ctx, "path_hash_mode", None)
    if hm is None or hm < 0:
        hm = 0
    cph = (hm + 1) * 2
    if len(path_hex) < cph * n:
        return None
    return [path_hex[i:i + cph] for i in range(0, cph * n, cph)]


def _pathcmd():
    # path.py owns the collision-aware hop resolver; commands/ is not a
    # package, so load it from its file the way the plugin loader does.
    global _PATHCMD
    if _PATHCMD is None:
        p = Path(__file__).with_name("path.py")
        spec = importlib.util.spec_from_file_location("mcbot_cmd_allergy_path", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PATHCMD = mod
    return _PATHCMD


async def _path_location(ctx):
    # (lat, lon, label) of the first located repeater in the inbound path,
    # the bot's own location for a direct message, or an error string.
    hops = _hops(ctx)
    pc = _pathcmd()
    if hops is None:
        loc = await pc._bot_location(ctx)
        if not loc:
            return "direct (no path) and bot location unset; try !allergy <city|zip>"
        return loc[0], loc[1], "nr bot"
    resolved = await pc._resolve_hops(ctx, hops)
    for hop, r in zip(hops, resolved):
        if r:
            lat, lon, name = r
            return lat, lon, f"nr {name or hop}"
    return f"no located repeater in path ({len(hops)}h); try !allergy <city|zip>"


# --- handler -----------------------------------------------------------------

async def _pollencom_reply(ctx, mode, zip5, postcodes, lat, lon, label):
    # a given zip is tried as-is. a city tries the geocoder's first postcode,
    # then the Census ZCTA at its centroid (the first postcode can be a
    # PO-box-only zip pollen.com has no data for). a repeater goes straight
    # to the Census lookup.
    tried: list[str] = []
    candidates = [zip5] if zip5 else list(postcodes[:1])
    for _ in range(2):
        if not candidates:
            z = await asyncio.to_thread(_census_zip, lat, lon)
            candidates = [z] if z and z not in tried else []
        if not candidates:
            break
        z = candidates.pop(0)
        tried.append(z)
        data = await asyncio.to_thread(_pollencom_fetch, z)
        out = _format_pollencom(data, f"{label} ({z})" if mode == "path" else None)
        if out:
            return out
        if mode == "zip":
            break
    if tried:
        return f"{label}: no pollen data for {tried[-1]}"
    return f"{label}: no zip found; try !allergy <zip>"


async def _run(ctx, mode, value, cc):
    zip5 = None
    postcodes: list[str] = []
    country = None
    if mode == "path":
        loc = await _path_location(ctx)
        if isinstance(loc, str):
            return loc
        lat, lon, label = loc
    else:
        g = await asyncio.to_thread(_geocode, value, cc)
        if not g:
            cc_disp = f" [{cc}]" if cc and mode == "city" else ""
            return f"No match for {value}{cc_disp}"
        lat, lon = g["lat"], g["lon"]
        label = _place_label(g)
        country = (g.get("cc") or "").upper() or None
        postcodes = g["postcodes"]
        if mode == "zip":
            zip5 = value

    key = _google_key()
    if key:
        try:
            data = await asyncio.to_thread(_google_fetch, lat, lon, key)
            out = _format_google(data, label)
            if out:
                return out
            ctx.bot.logger.warning(
                "allergy: google returned no dailyInfo for %s,%s", lat, lon,
            )
        except Exception:
            ctx.bot.logger.warning(
                "allergy: google lookup failed, trying pollen.com", exc_info=True,
            )

    if country and country != "US":
        return f"{label}: no pollen data (pollen.com is US-only; set POLLEN_GOOGLE_API_KEY)"
    return await _pollencom_reply(ctx, mode, zip5, postcodes, lat, lon, label)


async def handle(ctx):
    mode, value, cc = _parse_args(ctx.message_text)
    try:
        return await _run(ctx, mode, value, cc)
    except Exception as e:
        ctx.bot.logger.exception("allergy command failed")
        return f"Error: {e}"
