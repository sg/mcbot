"""Raw-packet breakout for the web inspector.

Reuses the bot's in-tree decoder (mcbot.parse_packet_envelope and the
decryptors) to turn an on-air packet into a list of fields with byte
offsets/lengths — so the UI can map a selected hex span to its element —
plus a best-effort higher-level decode (channel text / DM / advert) when
the relevant keys are available.
"""

from __future__ import annotations

import mcbot

_ROUTE_TYPES = {
    0: "transport_flood",
    1: "flood",
    2: "direct",
    3: "transport_direct",
}


def _payload_type_name(pt: int) -> str:
    try:
        return mcbot.PayloadType(pt).name
    except ValueError:
        return f"UNKNOWN_{pt:#04x}"


async def decode_packet(raw: bytes, bot) -> dict:
    """Decode an on-air packet into {valid, length, fields[], envelope, decoded}.
    Never raises — malformed input is reported via `error`."""
    out: dict = {
        "raw_hex": raw.hex(),
        "length": len(raw),
        "valid": False,
        "fields": [],
        "envelope": None,
        "decoded": None,
        "error": None,
    }
    fields = out["fields"]

    def add(name, offset, length, value=None, description=None):
        seg = raw[offset:offset + length]
        fields.append({
            "name": name, "offset": offset, "length": len(seg),
            "hex": seg.hex(), "value": value, "description": description,
        })

    if len(raw) < 2:
        out["error"] = "packet too short (need >= 2 bytes)"
        return out

    header = raw[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F
    add("header", 0, 1, value={
        "route_type": route_type,
        "route_type_name": _ROUTE_TYPES.get(route_type, "?"),
        "payload_type": payload_type,
        "payload_type_name": _payload_type_name(payload_type),
    }, description="route_type = bits 0-1, payload_type = bits 2-5")

    offset = 1
    if route_type in (0, 3):  # transport_flood / transport_direct carry a code
        if len(raw) < offset + 4:
            out["error"] = "truncated transport code"
            return out
        add("transport_code", offset, 4,
            description="present on transport routes")
        offset += 4

    if len(raw) < offset + 1:
        out["error"] = "truncated before path byte"
        return out
    path_byte = raw[offset]
    hop_count = path_byte & 0x3F
    hash_mode = (path_byte >> 6) & 0x03
    hash_size = hash_mode + 1
    add("path_byte", offset, 1, value={
        "hop_count": hop_count,
        "hash_size": hash_size,
        "hash_mode": hash_mode,
    }, description="hop_count = bits 0-5, hash_mode = bits 6-7")
    offset += 1

    if hash_mode == 3:
        out["error"] = "reserved path hash mode 3"
        return out
    plen = hop_count * hash_size
    if plen > mcbot.MAX_PATH_SIZE or len(raw) < offset + plen:
        out["error"] = "truncated path"
        return out
    path = raw[offset:offset + plen]
    hops = [path[i:i + hash_size].hex() for i in range(0, plen, hash_size)]
    add("path", offset, plen, value=hops,
        description=f"{hop_count} hop(s) x {hash_size} byte(s)")
    offset += plen

    payload = raw[offset:]
    add("payload", offset, len(payload),
        value={"payload_type_name": _payload_type_name(payload_type)})

    out["valid"] = True
    out["error"] = None
    out["envelope"] = {
        "header": header,
        "route_type": route_type,
        "route_type_name": _ROUTE_TYPES.get(route_type, "?"),
        "payload_type": payload_type,
        "payload_type_name": _payload_type_name(payload_type),
        "hop_count": hop_count,
        "hash_size": hash_size,
        "path": hops,
    }
    out["decoded"] = await _decode_payload(payload_type, payload, bot)
    return out


async def _decode_payload(payload_type: int, payload: bytes, bot) -> dict:
    """Best-effort higher-level decode using available keys."""
    if payload_type == mcbot.PayloadType.GROUP_TEXT.value:
        return await _decode_channel(payload, bot)
    if payload_type == mcbot.PayloadType.TEXT_MESSAGE.value:
        return await _decode_dm(payload, bot)
    if payload_type == mcbot.PayloadType.ADVERT.value:
        res = {"type": "advert"}
        if len(payload) >= 32:
            res["public_key"] = payload[:32].hex()
        return res
    return {"type": _payload_type_name(payload_type), "note": "no decoder"}


async def _decode_channel(payload: bytes, bot) -> dict:
    if not payload:
        return {"type": "channel_text", "decrypted": False}
    chash = payload[0]
    entry = bot.channels_by_hash.get(chash)
    res = {"type": "channel_text", "channel_hash": f"{chash:02x}"}
    if not entry:
        res["decrypted"] = False
        res["note"] = "no channel key matches this hash"
        return res
    idx, name, secret = entry
    dec = mcbot.decrypt_group_text(payload, secret)
    res["channel_idx"] = idx
    res["channel_name"] = name
    if dec is None:
        res["decrypted"] = False
        res["note"] = "HMAC/decrypt failed"
        return res
    res.update({
        "decrypted": True,
        "sender": dec.sender,
        "message": dec.message,
        "timestamp": dec.timestamp,
    })
    return res


async def _decode_dm(payload: bytes, bot) -> dict:
    res = {"type": "direct_message"}
    if len(payload) < 2:
        res["decrypted"] = False
        return res
    dest_byte, src_byte = payload[0], payload[1]
    res["dest_byte"] = f"{dest_byte:02x}"
    res["src_byte"] = f"{src_byte:02x}"
    if not bot.my_private_key or bot.my_pubkey_byte is None:
        res["decrypted"] = False
        res["note"] = "bot private key unavailable"
        return res
    if dest_byte != bot.my_pubkey_byte:
        res["decrypted"] = False
        res["note"] = "not addressed to this bot"
        return res
    rows = await bot.db.fetchall(
        "SELECT public_key, adv_name FROM contacts WHERE substr(public_key,1,2)=?",
        (f"{src_byte:02x}",),
    )
    for r in rows:
        try:
            their_pk = bytes.fromhex(r["public_key"])
        except ValueError:
            continue
        dec = mcbot.try_decrypt_dm(
            payload, bot.my_private_key, their_pk, bot.my_pubkey_byte
        )
        if dec is not None:
            res.update({
                "decrypted": True,
                "sender_pubkey": r["public_key"],
                "sender_name": r["adv_name"],
                "message": dec.message,
                "timestamp": dec.timestamp,
            })
            return res
    res["decrypted"] = False
    res["note"] = "no known contact key decrypted this DM"
    return res
