"""Message history endpoints + manual send (Phase 4).

GET endpoints read stored history; POST /api/send/* originate outgoing
messages through the shared management service (recorded + audited)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from management import MgmtError
from ..util import (
    actor_kwargs, get_bot, http_for_mgmt, require_auth, rows_to_list,
)

router = APIRouter(prefix="/api", tags=["messages"])


@router.get("/channel-messages")
async def channel_messages(
    bot=Depends(get_bot),
    channel_idx: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None),
):
    clauses, params = [], []
    if channel_idx is not None:
        clauses.append("channel_idx = ?")
        params.append(channel_idx)
    if before_id:
        clauses.append("id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await bot.db.fetchall(
        f"SELECT * FROM channel_messages{where} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    return {"items": rows_to_list(rows)}


@router.get("/direct-messages")
async def direct_messages(
    bot=Depends(get_bot),
    pubkey: str | None = Query(None, description="filter by sender pubkey"),
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None),
):
    clauses, params = [], []
    if pubkey:
        clauses.append("sender_pubkey = ?")
        params.append(pubkey.lower())
    if before_id:
        clauses.append("id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await bot.db.fetchall(
        f"SELECT * FROM direct_messages{where} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    return {"items": rows_to_list(rows)}


# --- manual send (Phase 4) ---
class ChannelSendBody(BaseModel):
    channel_idx: int
    text: str


class DMSendBody(BaseModel):
    pubkey: str
    text: str


@router.post("/send/channel")
async def send_channel(
    body: ChannelSendBody,
    bot=Depends(get_bot),
    identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.send_channel(
            body.channel_idx, body.text, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.post("/send/dm")
async def send_dm(
    body: DMSendBody,
    bot=Depends(get_bot),
    identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.send_dm(
            body.pubkey, body.text, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)
