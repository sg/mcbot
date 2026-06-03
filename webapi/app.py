"""FastAPI application factory + uvicorn server builder for the web API.

Phase 1 wires up the app skeleton, auth, and OpenAPI docs with a few proof
endpoints (health/login/me). Resource routers (DB admin, messaging, packets)
are added in later phases.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from fastapi import (
    Depends, FastAPI, HTTPException, Response,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import Auth
from .routers import dbadmin, messaging, packets
from .util import require_auth


class _UIStaticFiles(StaticFiles):
    """Serve the built SPA, but mark the HTML entry point as must-revalidate.

    Vite gives JS/CSS content-hashed names, so `assets/*` are safe to cache
    forever — but `index.html` (which points at the current hashes) must be
    re-checked every load, or a rebuilt bundle won't show until the user
    hard-reloads. We tag every text/html response (the root, the SPA
    deep-link fallback, and /index.html) with `Cache-Control: no-cache`,
    which tells the browser to revalidate via the ETag StaticFiles already
    sends (cheap 304s), while leaving hashed assets to cache normally.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


class LoginBody(BaseModel):
    username: str
    password: str


def create_app(bot) -> FastAPI:
    cfg = bot.cfg
    app = FastAPI(
        title="mcbot admin API",
        version="1",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.bot = bot
    app.state.auth = Auth(cfg)

    origins = [
        o.strip() for o in (cfg.web_cors_origins or "").split(",") if o.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/api/health")
    async def health():
        # Unauthenticated liveness probe (no sensitive data).
        return {"status": "ok", "service": "mcbot", "web": "phase1"}

    @app.post("/api/login")
    async def login(body: LoginBody, response: Response):
        auth: Auth = app.state.auth
        if not auth.verify_login(body.username, body.password):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = auth.make_session(body.username)
        response.set_cookie(
            auth.SESSION_COOKIE, token,
            httponly=True, samesite="lax",
            secure=bool(cfg.web_tls_cert),
            max_age=auth.session_ttl,
        )
        return {
            "token": token,
            "user": body.username,
            "expires_in": auth.session_ttl,
        }

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(app.state.auth.SESSION_COOKIE)
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(identity: str = Depends(require_auth)):
        return {"identity": identity}

    # Resource routers — all read-only in Phase 2, all auth-gated.
    for r in (dbadmin.router, messaging.router, packets.router):
        app.include_router(r, dependencies=[Depends(require_auth)])

    # --- live WebSocket feeds ---
    async def _feed_ws(ws: WebSocket, broadcaster):
        if app.state.auth.identify_ws(ws) is None:
            await ws.close(code=1008)  # policy violation
            return
        await ws.accept()
        queue = broadcaster.subscribe()

        # Detect client disconnect promptly: a feed is server→client only, so
        # without watching for incoming frames a handler blocked on queue.get()
        # would never notice a closed socket and would stall graceful shutdown.
        async def _watch_disconnect():
            try:
                while True:
                    await ws.receive()
            except Exception:
                return

        watcher = asyncio.create_task(_watch_disconnect())
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=30,
                )
                if watcher in done:
                    getter.cancel()
                    break
                if getter in done:
                    await ws.send_json(getter.result())
                else:  # timeout — re-check liveness and loop
                    getter.cancel()
        except Exception:
            pass
        finally:
            watcher.cancel()
            broadcaster.unsubscribe(queue)

    @app.websocket("/api/ws/packets")
    async def ws_packets(ws: WebSocket):
        await _feed_ws(ws, bot.web_packet_feed)

    @app.websocket("/api/ws/messages")
    async def ws_messages(ws: WebSocket):
        await _feed_ws(ws, bot.web_message_feed)

    # Serve the built Vue frontend if present. _UIStaticFiles adds
    # Cache-Control: no-cache to index.html so a rebuilt bundle is picked up
    # without a manual hard-reload; hashed assets/* still cache normally.
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount(
            "/", _UIStaticFiles(directory=str(static_dir), html=True), name="ui"
        )

    return app


def make_server(bot) -> uvicorn.Server:
    """Build a uvicorn Server bound per [web] config. The caller runs
    server.serve() as a task and sets server.should_exit to stop it."""
    cfg = bot.cfg
    kwargs = dict(
        app=create_app(bot),
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="warning",
        lifespan="off",
        access_log=False,
    )
    if cfg.web_tls_cert and cfg.web_tls_key:
        kwargs["ssl_certfile"] = str(cfg.web_tls_cert)
        kwargs["ssl_keyfile"] = str(cfg.web_tls_key)
    return uvicorn.Server(uvicorn.Config(**kwargs))
