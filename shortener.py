"""URL shortening for command plugins.

!path and !topo both have to fit a link into a mesh message of ~180
characters -- a geojson.io map payload alone runs to several KB. Which
service does the shortening is an admin choice ([bot] url_shortener),
because the alternative to the public da.gd service is a self-hosted Sink
instance whose API token can't live in a git-tracked command script.

Provider 'sink' falls back to da.gd rather than failing: a Sink instance
enforces its own maximum target-URL length (2048 characters by default), so
a long enough route would otherwise lose its map link entirely.
"""

import hashlib
import os
import time

import requests

PROVIDERS = ("dagd", "sink", "none")

_DAGD_URL = "https://da.gd/s"
_TIMEOUT = 8
# Sink slugs match /^[a-z0-9]+(?:-[a-z0-9]+)*$/i, so hex needs no encoding.
_SINK_SLUG_PREFIX = "mc"
_SINK_SLUG_CHARS = 10
_SINK_TOKEN_ENV = "SINK_API_TOKEN"


def sink_slug(long_url):
    # Deterministic slug: re-shortening the same map upserts onto the same
    # short link instead of minting a row per !path invocation.
    digest = hashlib.sha256(long_url.encode("utf-8")).hexdigest()
    return _SINK_SLUG_PREFIX + digest[:_SINK_SLUG_CHARS]


def _shorten_dagd(long_url):
    r = requests.get(_DAGD_URL, params={"url": long_url}, timeout=_TIMEOUT)
    r.raise_for_status()
    s = r.text.strip()
    if not s.startswith("http"):
        raise ValueError(f"shortener error: {s[:60]}")
    return s


def _shorten_sink(long_url, base, token, ttl_days, tag):
    payload = {"url": long_url, "slug": sink_slug(long_url)}
    if tag:
        payload["tags"] = ["mcbot", tag]
        payload["comment"] = f"mcbot !{tag}"
    if ttl_days > 0:
        payload["expiration"] = int(time.time()) + int(ttl_days) * 86400
    # upsert, not create: an identical map re-uses the existing link and
    # returns status='existing'. An expired link is not considered existing,
    # so the same route is re-created after its TTL lapses.
    r = requests.post(
        base.rstrip("/") + "/api/link/upsert",
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
