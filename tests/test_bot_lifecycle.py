"""Plugin reload and bot restart through the shared management service, and
their web endpoints (the equivalents of '!adm reload' / '!adm restart')."""

import asyncio

import httpx

from webapi.app import create_app
from webapi.routers import dbadmin

PLUGIN = '''
NAME = "hello"
TRIGGERS = ["!hello"]
async def handle(ctx):
    return "hi"
'''


def client_for(bot):
    bot.cfg.web_api_tokens = ["test-token"]
    bot.cfg.web_session_secret = "secret"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(bot)),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    )


async def last_audit(bot, action):
    return await bot.db.fetchone(
        "SELECT actor_name, detail FROM bot_audit_log WHERE action=? "
        "ORDER BY id DESC LIMIT 1",
        (action,),
    )


async def test_reload_picks_up_new_plugin_seeds_and_audits(bot_factory, tmp_path):
    bot = bot_factory(commands_dir=tmp_path)
    assert bot.loader.commands == {}
    (tmp_path / "hello.py").write_text(PLUGIN)
    r = await bot.mgmt.reload_commands(actor_name="web:user:steve")
    assert r == {"before": 0, "after": 1, "seeded": 1, "errors": []}
    assert "hello" in bot.loader.commands
    row = await bot.db.fetchone(
        "SELECT command FROM command_config WHERE command='hello'"
    )
    assert row is not None, "new plugin's config row was seeded"
    audit = await last_audit(bot, "reload")
    assert audit["actor_name"] == "web:user:steve"
    assert audit["detail"] == "0->1, errors=0, seeded=1"


async def test_reload_reports_broken_plugin(bot_factory, tmp_path):
    bot = bot_factory(commands_dir=tmp_path)
    (tmp_path / "hello.py").write_text(PLUGIN)
    (tmp_path / "broken.py").write_text("def handle(:\n")
    r = await bot.mgmt.reload_commands()
    assert r["after"] == 1
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("broken.py:")


async def test_restart_defers_then_requests_teardown(bot_factory):
    bot = bot_factory()
    r = await bot.mgmt.restart(0.05, actor_name="web:user:steve")
    assert r == {"restart_in": 0.05}
    assert not bot.restart_requested and not bot.stop_event.is_set(), "deferred"
    await asyncio.sleep(0.15)
    assert bot.restart_requested and bot.stop_event.is_set()
    audit = await last_audit(bot, "restart")
    assert audit["actor_name"] == "web:user:steve"


async def test_reload_endpoint(bot_factory, tmp_path):
    bot = bot_factory(commands_dir=tmp_path)
    (tmp_path / "hello.py").write_text(PLUGIN)
    async with client_for(bot) as c:
        res = await c.post("/api/bot/reload")
    assert res.status_code == 200
    assert res.json() == {"before": 0, "after": 1, "seeded": 1, "errors": []}
    audit = await last_audit(bot, "reload")
    assert audit["actor_name"].startswith("web:token:")


async def test_restart_endpoint(bot_factory, monkeypatch):
    bot = bot_factory()
    monkeypatch.setattr(dbadmin, "_RESTART_DELAY", 0.05)
    async with client_for(bot) as c:
        res = await c.post("/api/bot/restart")
    assert res.status_code == 200
    assert res.json() == {"restart_in": 0.05}
    assert not bot.stop_event.is_set(), "response returns before teardown"
    await asyncio.sleep(0.15)
    assert bot.restart_requested and bot.stop_event.is_set()


async def test_lifecycle_endpoints_require_auth(bot_factory):
    bot = bot_factory()
    async with client_for(bot) as c:
        c.headers.pop("Authorization")
        for path in ("/api/bot/reload", "/api/bot/restart"):
            assert (await c.post(path)).status_code == 401
    assert not bot.stop_event.is_set()
