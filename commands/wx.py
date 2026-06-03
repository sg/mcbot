# !wx — current weather for a location via Open-Meteo.
#
# usage:
#   !wx <city>
#   !wx <city> <CC>      (e.g. "!wx Austin US"; CC must be 2 uppercase letters)
#
# the country code is optional; if omitted, Open-Meteo's geocoder picks the
# top match. 
#
# it is currently a free API so no key is required.
#

import asyncio
import re
import requests

NAME = "wx"
TRIGGERS = ["!wx"]
DESCRIPTION = "Current weather for a city: !wx <city> [CC]"
COOLDOWN_DEFAULT = 30
ALLOWED_CHANNELS = [
    "#wx",
    "#bot",
]
ALLOW_DM = True

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_HEADERS = {
    "Accept-Encoding": "gzip",
    "user-agent": "mcbot-wx/1.0",
}

_DIRS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _cardinal(deg: int) -> str:
    return _DIRS[round(deg / (360 / 16)) % 16]


def _parse_args(text: str) -> tuple[str | None, str | None]:
    # strip '!wx ' and split into (location, optional country_code).
    # a trailing 2-uppercase-letter token is treated as the country code.
    # 
    body = re.sub(r"^!wx\b\s*", "", text.strip(), count=1, flags=re.IGNORECASE)
    body = body.strip()
    if not body:
        return None, None
    parts = body.rsplit(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z]{2}", parts[1]):
        return parts[0], parts[1]
    return body, None


def _fetch_sync(location: str, country_code: str | None) -> str:
    geo_params = {
        "name": location,
        "count": "1",
        "language": "en",
        "format": "json",
    }
    if country_code:
        geo_params["countryCode"] = country_code
    try:
        r = requests.get(
            _GEOCODE_URL, params=geo_params, headers=_HEADERS, timeout=5,
        )
        geo = r.json() or {}
    except Exception as e:
        return f"Error (geocode): {e}"

    results = geo.get("results") or []
    if not results:
        cc_disp = f" [{country_code}]" if country_code else ""
        return f"No match for {location}{cc_disp}"

    res = results[0]
    lat = res.get("latitude")
    lon = res.get("longitude")
    cc = res.get("country_code") or country_code or "?"
    if lat is None or lon is None:
        return "Error (geocode): no coordinates"

    fc_params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,relative_humidity_2m,precipitation,"
            "wind_speed_10m,wind_direction_10m"
        ),
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
    }
    try:
        r = requests.get(
            _FORECAST_URL, params=fc_params, headers=_HEADERS, timeout=5,
        )
        fc = r.json() or {}
    except Exception as e:
        return f"Error (forecast): {e}"

    cur = fc.get("current") or {}
    try:
        temp = cur["temperature_2m"]
        humid = cur["relative_humidity_2m"]
        precip = cur["precipitation"]
        wind = cur["wind_speed_10m"]
        wdir = int(cur.get("wind_direction_10m", 0))
    except KeyError as e:
        return f"Error (forecast): missing field {e}"

    return (
        f"{location} [{cc}] ({lat},{lon}): "
        f"{temp}F, Humid: {humid}%, Rain: {precip}in, "
        f"Wind: {_cardinal(wdir)} {wind}mph"
    )


async def handle(ctx) -> str | None:
    location, cc = _parse_args(ctx.message_text)
    if not location:
        return "usage: !wx <city> [CC]   e.g. !wx Austin US"
    try:
        return await asyncio.to_thread(_fetch_sync, location, cc)
    except Exception as e:
        ctx.bot.logger.exception("wx command failed")
        return f"Error: {e}"
