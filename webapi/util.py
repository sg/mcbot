"""Shared helpers for the web API routers."""

from __future__ import annotations

from fastapi import HTTPException, Request

from management import MgmtError

# MgmtError.code -> HTTP status for write endpoints.
_CODE_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "refused": 400,
    "invalid": 400,
}


def get_bot(request: Request):
    """FastAPI dependency returning the live MCBot instance."""
    return request.app.state.bot


async def require_auth(request: Request) -> str:
    """FastAPI dependency: gate a route and return the actor identity.
    Lives here (not app.py) so routers can depend on it for the identity
    value without importing app — which would be a circular import."""
    ident = request.app.state.auth.identify(request)
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def row_to_dict(row) -> dict | None:
    return {k: row[k] for k in row.keys()} if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [{k: r[k] for k in r.keys()} for r in rows]


def actor_kwargs(identity: str) -> dict:
    """Audit-actor kwargs for a mgmt call originating from the web API.
    Web callers have no mesh pubkey, so the audit row records the auth
    identity (e.g. 'web:user:steve' / 'web:token:MFAP-x…')."""
    return {"actor_pubkey": None, "actor_name": f"web:{identity}"}


def http_for_mgmt(err: MgmtError) -> HTTPException:
    return HTTPException(
        status_code=_CODE_STATUS.get(err.code, 400), detail=err.message
    )
