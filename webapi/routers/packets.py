"""Raw packet listing + the hex-breakout decode endpoint (Phase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..decode import decode_packet
from ..util import get_bot, row_to_dict, rows_to_list

router = APIRouter(prefix="/api", tags=["packets"])

# Columns returned in the list view (omit the bulky JSON blobs).
_LIST_COLS = (
    "id, received_at, event_type, packet_type, sender_pubkey_prefix, "
    "sender_pubkey, sender_name, path, path_len, snr, rssi, channel_idx, "
    "text, (raw_hex IS NOT NULL) AS has_raw"
)


@router.get("/packets")
async def list_packets(
    bot=Depends(get_bot),
    type: str | None = Query(None, description="filter by packet_type"),
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None),
):
    clauses, params = [], []
    if type:
        clauses.append("packet_type = ?")
        params.append(type)
    if before_id:
        clauses.append("id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await bot.db.fetchall(
        f"SELECT {_LIST_COLS} FROM received_packets{where} "
        f"ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    return {"items": rows_to_list(rows)}


@router.get("/packets/{packet_id}")
async def get_packet(packet_id: int, bot=Depends(get_bot)):
    row = await bot.db.fetchone(
        "SELECT * FROM received_packets WHERE id=?", (packet_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="packet not found")
    return row_to_dict(row)


class DecodeBody(BaseModel):
    packet_id: int | None = None
    hex: str | None = None


@router.post("/packets/decode")
async def decode(body: DecodeBody, bot=Depends(get_bot)):
    """Break a packet into its fields (with byte offsets) + a key-based
    higher-level decode. Provide either a stored packet_id or raw hex."""
    raw_hex = body.hex
    if raw_hex is None:
        if body.packet_id is None:
            raise HTTPException(
                status_code=400, detail="provide packet_id or hex"
            )
        row = await bot.db.fetchone(
            "SELECT raw_hex FROM received_packets WHERE id=?",
            (body.packet_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="packet not found")
        raw_hex = row["raw_hex"]
        if not raw_hex:
            raise HTTPException(
                status_code=422,
                detail="packet has no raw bytes (only RX_LOG_DATA packets do)",
            )
    cleaned = "".join(raw_hex.split()).replace(":", "")
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid hex")
    return await decode_packet(raw, bot)
