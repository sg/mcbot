"""!pws — current observation from a Weather Underground PWS (weather.com).

Reports temperature, humidity, rain total, and wind for one Personal
Weather Station, using the weather.com PWS observations API.

Configure two things to use this — each via an environment variable or a
constant below:

- Station ID — the WU station you want to read (e.g. "KXXYYYY1234").
  Set the PWS_STATION_ID environment variable, or edit _STATION_ID below.
- API key — a weather.com API key. Set the PWS_API_KEY environment
  variable, or edit _API_KEY below. Prefer the env var: this file is
  tracked in git, so a key written into _API_KEY would be committed.
"""

import asyncio
import os

import requests

NAME = "pws"
TRIGGERS = ["!pws"]
DESCRIPTION = "Current observation from a Weather Underground PWS station"
COOLDOWN_DEFAULT = 30
ALLOWED_CHANNELS = ["#bot-cmd-test"]
ALLOW_DM = True

_API_URL = "https://api.weather.com/v2/pws/observations/current"
# Your Weather Underground station ID, or set the PWS_STATION_ID env var.
_STATION_ID = ""
# Your weather.com API key, or set the PWS_API_KEY env var (preferred — this
# file is tracked in git, so a key written here would be committed).
_API_KEY = ""
_HEADERS = {
    "authority": "api.weather.com",
    "Accept-Encoding": "gzip",
    "user-agent": "mcbot-pws/1.0",
}

_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
         "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _cardinal(deg: int) -> str:
    return _DIRS[round(deg / (360 / 16)) % 16]


def _station_id() -> str | None:
    sid = os.environ.get("PWS_STATION_ID") or _STATION_ID
    sid = (sid or "").strip()
    return sid or None


def _load_api_key() -> str | None:
    k = os.environ.get("PWS_API_KEY") or _API_KEY
    k = (k or "").strip()
    return k or None


def _fetch_sync() -> str:
    station_id = _station_id()
    if not station_id:
        return (
            "pws: no station configured "
            "(set PWS_STATION_ID env or _STATION_ID in commands/pws.py)"
        )
    api_key = _load_api_key()
    if not api_key:
        return (
            "pws: no API key (set PWS_API_KEY env or _API_KEY in commands/pws.py)"
        )
    params = {
        "stationId": station_id,
        "format": "json",
        "units": "e",
        "apiKey": api_key,
    }
    r = requests.get(_API_URL, params=params, headers=_HEADERS, timeout=5)
    obs = (r.json() or {}).get("observations", [])
    if not obs:
        return "No Data"
    o = obs[0]
    imp = o["imperial"]
    return (
        f"[{station_id}] Temp: {imp['temp']}F, "
        f"Humidity: {o['humidity']}%, "
        f"Rain: {imp['precipTotal']}, "
        f"Wind: {_cardinal(int(o['winddir']))} {imp['windSpeed']}mph"
    )


async def handle(ctx) -> str | None:
    try:
        return await asyncio.to_thread(_fetch_sync)
    except Exception as e:
        ctx.bot.logger.exception("pws command failed")
        return f"Error: {e}"
