"""URL shortening for command plugins.

!path and !topo both have to fit a link into a mesh message of ~180
characters -- a geojson.io map payload alone runs to several KB. Which
service does the shortening is an admin choice ([bot] url_shortener),
because the alternative to the public da.gd service is a self-hosted Sink
instance whose API token can't live in a git-tracked command script.

Provider 'sink' falls back to da.gd rather than failing, since an instance
can be down, misconfigured, or stricter than the bot expects -- a Sink
instance enforces its own maximum target-URL length, 2048 characters unless
raised, and a long enough route would otherwise lose its map link entirely.
"""

import os
import time

import requests

PROVIDERS = ("dagd", "sink", "none")

_DAGD_URL = "https://da.gd/s"
_TIMEOUT = 8
_SINK_TOKEN_ENV = "SINK_API_TOKEN"


def _shorten_dagd(long_url):
    r = requests.get(_DAGD_URL, params={"url": long_url}, timeout=_TIMEOUT)
    r.raise_for_status()
    s = r.text.strip()
    if not s.startswith("http"):
        raise ValueError(f"shortener error: {s[:60]}")
    return s


def _shorten_sink(long_url, base, token, ttl_days, tag):
    # No slug: Sink generates its own, which is 6 characters by default
    # (slugDefaultLength) against a 30-character alphabet. Every character
    # counts in a ~180-character mesh message, so that beats a longer
    # deterministic slug that would let repeat routes share one link --
    # sink_link_ttl_days does the housekeeping instead.
    payload = {"url": long_url}
    if tag:
        payload["tags"] = ["mcbot", tag]
        payload["comment"] = f"mcbot !{tag}"
    if ttl_days > 0:
        payload["expiration"] = int(time.time()) + int(ttl_days) * 86400
    # create, not upsert: on the (remote) chance Sink's generated slug is
    # already taken, create reports 409 rather than upsert's silent
    # "here's the existing link", which would hand back someone else's map.
    r = requests.post(
        base.rstrip("/") + "/api/link/create",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    try:
        short = (r.json() or {}).get("shortLink")
    except ValueError:
        short = None
    if not short or not str(short).startswith("http"):
        raise ValueError(f"sink: no shortLink in {r.status_code} response")
    return str(short)


def shorten(long_url, cfg, logger=None, tag=""):
    """Return a shortened URL, or None if none could be produced.

    Blocking (requests), so call it via asyncio.to_thread. Never raises:
    every caller has its own idea of what an unshortened link means for its
    reply, and none of them can send a multi-KB URL over the mesh.
    """
    provider = (getattr(cfg, "url_shortener", "dagd") or "dagd").lower()
    if provider == "none":
        return None

    if provider == "sink":
        base = getattr(cfg, "sink_api_base", "") or ""
        token = os.environ.get(_SINK_TOKEN_ENV, "")
        if base and token:
            try:
                return _shorten_sink(
                    long_url, base, token,
                    getattr(cfg, "sink_link_ttl_days", 30), tag,
                )
            except Exception:
                if logger:
                    logger.warning(
                        "shorten: sink failed, falling back to da.gd",
                        exc_info=True,
                    )
        elif logger:
            missing = "sink_api_base" if not base else _SINK_TOKEN_ENV
            logger.warning(
                "shorten: url_shortener=sink but %s is unset; using da.gd",
                missing,
            )

    try:
        return _shorten_dagd(long_url)
    except Exception:
        if logger:
            logger.exception("shorten: da.gd failed")
        return None
