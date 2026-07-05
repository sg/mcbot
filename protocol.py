"""On-air protocol constants and packet parsing for mcbot.

Pure data/functions shared by the bot core, the web API's packet
inspector, and tests — no radio, database, or asyncio dependencies.
"""

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

# EventType.name -> human-readable packet_type used in received_packets.type
PACKET_TYPE_MAP = {
    "ADVERTISEMENT": "ADVERT",
    "CONTACT_MSG_RECV": "DM",
    "CHANNEL_MSG_RECV": "GRPCHAT",
    "MSG_SENT": "DM_SENT",
    "NO_MORE_MSGS": "NO_MORE_MSGS",
    "ACK": "ACK",
    "PATH_UPDATE": "PATH_UPDATE",
    "PATH_RESPONSE": "PATH_RESPONSE",
    "TRACE_DATA": "TRACE",
    "TELEMETRY_RESPONSE": "TELEMETRY",
    "MMA_RESPONSE": "MMA",
    "ACL_RESPONSE": "ACL",
    "LOGIN_SUCCESS": "LOGIN_OK",
    "LOGIN_FAILED": "LOGIN_FAIL",
    "STATUS_RESPONSE": "STATUS",
    "BATTERY": "BATTERY",
    "SELF_INFO": "SELF_INFO",
    "DEVICE_INFO": "DEVICE_INFO",
    "CHANNEL_INFO": "CHANNEL_INFO",
    "CONTACTS": "CONTACTS_SYNC",
    "NEXT_CONTACT": "CONTACT",
    "CURRENT_TIME": "TIME",
    "RAW_DATA": "RAW",
    "RX_LOG_DATA": "RX_LOG",
    "LOG_DATA": "LOG",
    "BINARY_RESPONSE": "BINARY",
    "OK": "CMD_ACK",
    "ERROR": "CMD_ERR",
    "CONNECTED": "CONNECTION",
    "DISCONNECTED": "CONNECTION",
    "CUSTOM_VARS": "CUSTOM_VARS",
    "SIGN_START": "SIGN_START",
    "SIGNATURE": "SIGNATURE",
    "MESSAGES_WAITING": "MSG_WAIT",
    "NEW_CONTACT": "NEW_CONTACT",
    "CONTROL_DATA": "CONTROL_DATA",
    "DISCOVER_RESPONSE": "DISCOVER_RESPONSE",
    "NEIGHBOURS_RESPONSE": "NEIGHBOURS",
    "AUTOADD_CONFIG": "AUTOADD_CFG",
    "STATS_CORE": "STATS_CORE",
    "STATS_RADIO": "STATS_RADIO",
    "STATS_PACKETS": "STATS_PACKETS",
    "ALLOWED_REPEAT_FREQ": "REPEAT_FREQ",
    "DEFAULT_FLOOD_SCOPE": "FLOOD_SCOPE",
    "CONTACT_DELETED": "CONTACT_DEL",
    "CONTACTS_FULL": "CONTACTS_FULL",
    "TUNING_PARAMS": "TUNING",
    "CONTACT_URI": "CONTACT_URI",
    "ADVERT_PATH": "ADVERT_PATH",
    "PRIVATE_KEY": "PRIVATE_KEY",
    "DISABLED": "DISABLED",
}

# channel messages typically come through as "Name: text"
CHANNEL_SENDER_RE = re.compile(r"^([^:\n]{1,32}):\s+(.*)$", re.DOTALL)

# contact "type" integer -> the names a user can write in config/UI. Mirrors
# the library's CONTACT_TYPENAMES (none/cli/repeater/room/sensor); extra
# aliases ('rep','sens','companion') are accepted for convenience.
CONTACT_TYPE_NAMES = {0: "none", 1: "cli", 2: "repeater", 3: "room", 4: "sensor"}
_CONTACT_TYPE_ALIASES = {
    "none": 0, "cli": 1, "companion": 1, "client": 1,
    "repeater": 2, "rep": 2, "room": 3, "roomserver": 3, "room_server": 3,
    "sensor": 4, "sens": 4,
}


def parse_contact_types(raw: str, on_error=None) -> set:
    """Parse a comma/space list of contact-type names or ints into a set of
    type integers. Unknown tokens invoke on_error(token) (if given) and are
    skipped."""
    out: set = set()
    for tok in re.split(r"[,\s]+", raw or ""):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok.isdigit():
            out.add(int(tok))
            continue
        if tok in _CONTACT_TYPE_ALIASES:
            out.add(_CONTACT_TYPE_ALIASES[tok])
        elif on_error:
            on_error(tok)
    return out


def format_path(path_hex: Optional[str], hash_size: Optional[int]) -> str:
    """insert commas between routing-path hops in a hex string.
    """
    if not path_hex:
        return path_hex or ""
    if not hash_size or hash_size < 1:
        return path_hex
    chars_per_hop = hash_size * 2
    if len(path_hex) % chars_per_hop != 0:
        return path_hex
    return ",".join(
        path_hex[i:i + chars_per_hop]
        for i in range(0, len(path_hex), chars_per_hop)
    )

#------------------------------------------------------------------------
# Packet decoder
# Borrowed parts of REmote-Term's app/decoder.py and app/path_utils.py so
# the bot can decrypt DMs/channel messages from RX_LOG_DATA events on
# radios that don't queue them for the companion via get_msg().
# (https://github.com/jkingsman/Remote-Terminal-for-MeshCore )

class PayloadType(IntEnum):
    REQUEST = 0x00
    RESPONSE = 0x01
    TEXT_MESSAGE = 0x02
    ACK = 0x03
    ADVERT = 0x04
    GROUP_TEXT = 0x05
    GROUP_DATA = 0x06
    ANON_REQUEST = 0x07
    PATH = 0x08
    TRACE = 0x09
    MULTIPART = 0x0A
    CONTROL = 0x0B
    RAW_CUSTOM = 0x0F


MAX_PATH_SIZE = 64


@dataclass(frozen=True)
class ParsedEnvelope:
    header: int
    route_type: int
    payload_type: int
    hop_count: int
    hash_size: int
    path: bytes
    payload: bytes


def _decode_path_byte(path_byte: int) -> tuple[int, int]:
    hash_mode = (path_byte >> 6) & 0x03
    if hash_mode == 3:
        raise ValueError("reserved path hash mode 3")
    return path_byte & 0x3F, hash_mode + 1


def parse_packet_envelope(raw: bytes) -> Optional[ParsedEnvelope]:
    if len(raw) < 2:
        return None
    try:
        header = raw[0]
        route_type = header & 0x03
        payload_type = (header >> 2) & 0x0F
        offset = 1
        if route_type in (0x00, 0x03):
            if len(raw) < offset + 4:
                return None
            offset += 4  # skip transport code
        if len(raw) < offset + 1:
            return None
        path_byte = raw[offset]
        offset += 1
        hop_count, hash_size = _decode_path_byte(path_byte)
        plen = hop_count * hash_size
        if plen > MAX_PATH_SIZE or len(raw) < offset + plen + 1:
            return None
        path = raw[offset:offset + plen]
        offset += plen
        return ParsedEnvelope(
            header=header,
            route_type=route_type,
            payload_type=payload_type,
            hop_count=hop_count,
            hash_size=hash_size,
            path=path,
            payload=raw[offset:],
        )
    except (IndexError, ValueError):
        return None

