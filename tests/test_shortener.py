"""Pluggable URL shortening for !path and !topo.

Covers provider selection, the Sink upsert request the bot builds, and the
fallback chain. requests is stubbed throughout: no test touches the network.

The fallback matters more than it looks — a Sink instance enforces its own
maximum target-URL length, and a geojson.io map for a long route is one of
the few payloads big enough to hit it."""

from types import SimpleNamespace

import pytest

import mcbot
import shortener


class FakeResponse:
    def __init__(self, *, text="", payload=None, status=200, raise_for=None):
        self.text = text
        self._payload = payload
        self.status_code = status
        self._raise_for = raise_for

    def raise_for_status(self):
        if self._raise_for:
            raise self._raise_for

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def net(monkeypatch):
    """Records da.gd GETs and Sink POSTs; each can be scripted independently."""
    calls = {"get": [], "post": []}
    scripted = {"get": FakeResponse(text="https://da.gd/short"), "post": None}

    def fake_get(url, params=None, timeout=None):
        calls["get"].append({"url": url, "params": params, "timeout": timeout})
        r = scripted["get"]
        if isinstance(r, Exception):
            raise r
        return r

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["post"].append({
            "url": url, "json": json, "headers": headers, "timeout": timeout,
        })
        r = scripted["post"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(shortener.requests, "get", fake_get)
    monkeypatch.setattr(shortener.requests, "post", fake_post)
    return SimpleNamespace(calls=calls, scripted=scripted)


def cfg_for(provider="dagd", *, base="https://sht.nz", ttl=30):
    return SimpleNamespace(
        url_shortener=provider, sink_api_base=base, sink_link_ttl_days=ttl,
    )


@pytest.fixture
def sink_token(monkeypatch):
    monkeypatch.setenv("SINK_API_TOKEN", "test-token")


LONG = "https://geojson.io/#data=data:application/json;base64,AAAA"


def test_dagd_is_the_default(net):
    assert shortener.shorten(LONG, cfg_for("dagd")) == "https://da.gd/short"
    assert net.calls["get"][0]["params"] == {"url": LONG}, "url passed through"
    assert not net.calls["post"], "sink not called"


def test_none_disables_shortening(net):
    assert shortener.shorten(LONG, cfg_for("none")) is None
    assert not net.calls["get"] and not net.calls["post"], "no request at all"


def test_sink_upsert_request(net, sink_token):
    net.scripted["post"] = FakeResponse(
        payload={"shortLink": "https://sht.nz/mcabc", "status": "created"},
    )
    got = shortener.shorten(LONG, cfg_for("sink"), tag="path")
    assert got == "https://sht.nz/mcabc", "returns Sink's shortLink"

    req = net.calls["post"][0]
    assert req["url"] == "https://sht.nz/api/link/upsert", "upsert endpoint"
    assert req["headers"]["Authorization"] == "Bearer test-token", "bearer auth"
    body = req["json"]
    assert body["url"] == LONG
    assert body["slug"] == shortener.sink_slug(LONG), "deterministic slug"
    assert body["tags"] == ["mcbot", "path"], "tagged for the dashboard"
    assert body["expiration"] > 0, "expiring link"
    assert not net.calls["get"], "no da.gd call when sink succeeds"


def test_sink_slug_is_stable_and_url_safe():
    a = shortener.sink_slug(LONG)
    assert a == shortener.sink_slug(LONG), "same url -> same slug (upsert dedup)"
    assert a != shortener.sink_slug(LONG + "B"), "different url -> different slug"
    # Sink slugs must match /^[a-z0-9]+(?:-[a-z0-9]+)*$/i
    assert a.isalnum() and a.islower(), f"slug not url-safe: {a}"


def test_zero_ttl_omits_expiration(net, sink_token):
    net.scripted["post"] = FakeResponse(payload={"shortLink": "https://sht.nz/x"})
    shortener.shorten(LONG, cfg_for("sink", ttl=0))
    assert "expiration" not in net.calls["post"][0]["json"], "0 = never expires"


def test_sink_rejection_falls_back_to_dagd(net, sink_token):
    # what a target URL over Sink's own length cap looks like from here
    net.scripted["post"] = FakeResponse(
        status=400, raise_for=RuntimeError("400 Validation Error"),
    )
    assert shortener.shorten(LONG, cfg_for("sink")) == "https://da.gd/short", \
        "over-length/rejected url still gets a map link"
    assert net.calls["post"] and net.calls["get"], "tried sink, then da.gd"


def test_sink_without_token_falls_back(net, monkeypatch):
    monkeypatch.delenv("SINK_API_TOKEN", raising=False)
    assert shortener.shorten(LONG, cfg_for("sink")) == "https://da.gd/short"
    assert not net.calls["post"], "no unauthenticated sink call"


def test_sink_reply_without_shortlink_falls_back(net, sink_token):
    net.scripted["post"] = FakeResponse(payload={"status": "created"})
    assert shortener.shorten(LONG, cfg_for("sink")) == "https://da.gd/short", \
        "malformed sink reply is not trusted"


def test_both_providers_down_returns_none(net, sink_token):
    net.scripted["post"] = ConnectionError("sink down")
    net.scripted["get"] = ConnectionError("da.gd down")
    assert shortener.shorten(LONG, cfg_for("sink")) is None, "no link, no crash"


def test_dagd_error_body_is_not_a_link(net):
    net.scripted["get"] = FakeResponse(text="Error: invalid URL")
    assert shortener.shorten(LONG, cfg_for("dagd")) is None, \
        "non-http response body rejected"


def load_conf(tmp_path, bot_section):
    p = tmp_path / "test.conf"
    p.write_text(f"[radio]\nhost = 1.2.3.4\n\n[bot]\n{bot_section}\n")
    return mcbot.load_config(mcbot.parse_args(["--config", str(p)]))


def test_config_defaults_to_dagd(tmp_path):
    cfg = load_conf(tmp_path, "enabled = true")
    assert cfg.url_shortener == "dagd", "unchanged behaviour without config"


def test_config_reads_sink_settings(tmp_path):
    cfg = load_conf(
        tmp_path,
        "url_shortener = Sink\nsink_api_base = https://sht.nz/\n"
        "sink_link_ttl_days = 7",
    )
    assert cfg.url_shortener == "sink", "provider is case-insensitive"
    assert cfg.sink_api_base == "https://sht.nz/"
    assert cfg.sink_link_ttl_days == 7


def test_config_rejects_unknown_provider(tmp_path):
    # a typo must not silently fall back to a different service
    with pytest.raises(SystemExit):
        load_conf(tmp_path, "url_shortener = tinyurl")


def test_config_requires_sink_base(tmp_path):
    with pytest.raises(SystemExit):
        load_conf(tmp_path, "url_shortener = sink")


def test_failures_are_logged_not_raised(net, sink_token):
    net.scripted["post"] = ConnectionError("sink down")
    logged = []
    logger = SimpleNamespace(
        warning=lambda *a, **k: logged.append(("warning", a)),
        exception=lambda *a, **k: logged.append(("exception", a)),
    )
    assert shortener.shorten(LONG, cfg_for("sink"), logger) == "https://da.gd/short"
    assert any(lvl == "warning" for lvl, _ in logged), "fallback is logged"
