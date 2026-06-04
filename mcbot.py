#!/usr/bin/env python3
# mcbot.py — Meshcore companion radio bot
#
# Connects to a Meshcore radio over TCP via meshcore_py, syncs contacts,
# channelsr, messages into a local sqlite database, logs every received
# packet, and handles incoming text via command scripts.
#
# See mcbot.conf for configuration. CLI flags override config-file values.
#
#

import argparse
import asyncio
import configparser
import hashlib
import hmac
import importlib.util
import json
import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional

try:
    from meshcore import MeshCore, EventType
except ImportError:
    sys.stderr.write(
        "meshcore library not installed. Run: pip install meshcore\n"
    )
    sys.exit(1)

try:
    import nacl.bindings
    from Crypto.Cipher import AES
except ImportError:
    sys.stderr.write(
        "pynacl and pycryptodome are required. "
        "run: pip install -r requirements.txt\n"
    )
    sys.exit(1)


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


def _clamp_scalar(k: bytes) -> bytes:
    b = bytearray(k[:32])
    b[0] &= 248
    b[31] &= 63
    b[31] |= 64
    return bytes(b)


def derive_public_key(private_key: bytes) -> bytes:
    return nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(private_key[:32])


def derive_shared_secret(our_private_key: bytes, their_public_key: bytes) -> bytes:
    clamped = _clamp_scalar(our_private_key[:32])
    x25519_pub = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(their_public_key)
    return nacl.bindings.crypto_scalarmult(clamped, x25519_pub)


@dataclass
class DecryptedDM:
    timestamp: int
    flags: int
    message: str
    dest_byte: int
    src_byte: int
    txt_type: int


@dataclass
class DecryptedChannel:
    timestamp: int
    flags: int
    sender: Optional[str]
    message: str
    channel_hash: int


def decrypt_direct_message(
    payload: bytes, shared_secret: bytes
) -> Optional[DecryptedDM]:
    if len(payload) < 4:
        return None
    dest_byte = payload[0]
    src_byte = payload[1]
    mac = payload[2:4]
    ciphertext = payload[4:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    if hmac.new(shared_secret, ciphertext, hashlib.sha256).digest()[:2] != mac:
        return None
    try:
        decrypted = AES.new(shared_secret[:16], AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if len(decrypted) < 5:
        return None
    ts = int.from_bytes(decrypted[0:4], "little")
    flags = decrypted[4]
    txt_type = flags >> 2
    body = decrypted[5:]
    if txt_type == 2:  # signed
        if len(body) < 4:
            return None
        body = body[4:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]
    return DecryptedDM(
        timestamp=ts, flags=flags, message=text,
        dest_byte=dest_byte, src_byte=src_byte, txt_type=txt_type,
    )


def try_decrypt_dm(
    payload: bytes, our_private_key: bytes,
    their_public_key: bytes, our_pubkey_byte: int,
) -> Optional[DecryptedDM]:
    if len(payload) < 4:
        return None
    if payload[0] != our_pubkey_byte:
        return None
    if payload[1] != their_public_key[0]:
        return None
    try:
        shared = derive_shared_secret(our_private_key, their_public_key)
    except Exception:
        return None
    return decrypt_direct_message(payload, shared)


def decrypt_group_text(
    payload: bytes, channel_secret: bytes
) -> Optional[DecryptedChannel]:
    if len(payload) < 3:
        return None
    channel_hash = payload[0]
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    # meshcore channel HMAC uses key + 16 zero bytes
    full_secret = channel_secret + bytes(16)
    if hmac.new(full_secret, ciphertext, hashlib.sha256).digest()[:2] != cipher_mac:
        return None
    try:
        decrypted = AES.new(channel_secret, AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if len(decrypted) < 5:
        return None
    ts = int.from_bytes(decrypted[0:4], "little")
    flags = decrypted[4]
    try:
        text = decrypted[5:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    nul = text.find("\x00")
    if nul >= 0:
        text = text[:nul]
    sender = None
    content = text
    colon = text.find(": ")
    if 0 < colon < 50:
        candidate = text[:colon]
        if not any(c in candidate for c in ":[]\x00"):
            sender = candidate
            content = text[colon + 2:]
    return DecryptedChannel(
        timestamp=ts, flags=flags, sender=sender,
        message=content, channel_hash=channel_hash,
    )


# ---------------------------------------------------------------------------
# Configuration
#

@dataclass
class Config:
    # Transport can be "tcp" or "serial"
    transport: str = "tcp"
    host: str = ""
    port: int = 4000
    serial_port: str = ""
    serial_baud: int = 115200
    device_pin: str = ""
    db_path: Path = Path("./mcbot.db")
    logs_dir: Path = Path("./logs")
    log_level: str = "INFO"
    commands_dir: Path = Path("./commands")
    privkey_path: Optional[Path] = None  # default: <db>.privkey
    log_channels: str = "all"
    max_channel_messages: int = 1000
    max_dms: int = 1000
    max_packets: int = 1000
    max_contacts: int = 500
    auto_reconnect: bool = True
    commands_enabled: bool = True
    # Client-side decrypt + ingest of DMs/channel msgs from RX_LOG_DATA.
    # On firmware that queues messages this duplicates the get_msg path
    # (and is deduped), so it can be turned off to run capture-only — the
    # raw packet monitor (firehose) is unaffected either way. Leave on to
    # also cover non-queueing firmware and channels not programmed into the
    # radio (e.g. beyond its slot capacity).
    rx_log_decrypt: bool = True
    debug: bool = False
    # idx -> (name, 16-byte secret)
    channels: dict[int, tuple[str, bytes]] = field(default_factory=dict)
    # pubkey strings are inserted into the 'owner' group on startup
    owner_pubkeys: list[str] = field(default_factory=list)
    # dm-send retry settings
    dm_max_attempts: int = 3
    dm_flood_after: int = 2
    dm_max_flood_attempts: int = 2
    # --- web admin UI / API ([web] section) ---
    web_enabled: bool = False
    web_host: str = "127.0.0.1" # 0.0.0.0 to expose on all interfaces
    web_port: int = 8080
    web_api_tokens: list[str] = field(default_factory=list)
    web_admin_user: str = ""
    web_admin_password_hash: str = "" # PBKDF2 hash (generate with: ./mcbot.py --hash-password)
    web_session_secret: str = "" # signs session tokens; required if enabled
    web_cors_origins: str = "" # comma list, or "*"
    web_tls_cert: Optional[Path] = None
    web_tls_key: Optional[Path] = None

    def target_desc(self) -> str:
        # human-readable connection target for logs
        if self.transport == "serial":
            return f"serial {self.serial_port} @{self.serial_baud}"
        return f"tcp {self.host}:{self.port}"


def load_config(args) -> Config:
    cfg = Config()

    config_path = args.config or Path("./mcbot.conf")
    if config_path and Path(config_path).is_file():
        # interpolation=None so a literal '%' in any value (api keys,
        # session secrets, [env] values) is passed through untouched
        # instead of being parsed as configparser interpolation syntax.
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config_path)
        if parser.has_section("radio"):
            cfg.transport = parser["radio"].get(
                "transport", cfg.transport
            ).strip().lower()
            cfg.host = parser["radio"].get("host", cfg.host)
            cfg.port = parser["radio"].getint("port", cfg.port)
            cfg.serial_port = parser["radio"].get(
                "serial_port", cfg.serial_port
            )
            cfg.serial_baud = parser["radio"].getint(
                "serial_baud", cfg.serial_baud
            )
            cfg.device_pin = parser["radio"].get("device_pin", cfg.device_pin)
        if parser.has_section("storage"):
            cfg.db_path = Path(parser["storage"].get("db", str(cfg.db_path)))
            cfg.max_channel_messages = parser["storage"].getint(
                "max_channel_messages", cfg.max_channel_messages
            )
            cfg.max_dms = parser["storage"].getint("max_dms", cfg.max_dms)
            cfg.max_contacts = parser["storage"].getint(
                "max_contacts", cfg.max_contacts
            )
            cfg.max_packets = parser["storage"].getint(
                "max_packets", cfg.max_packets
            )
        if parser.has_section("channel_logging"):
            cfg.log_channels = parser["channel_logging"].get(
                "channels", cfg.log_channels
            )
        if parser.has_section("logging"):
            cfg.logs_dir = Path(
                parser["logging"].get("logs_dir", str(cfg.logs_dir))
            )
            cfg.log_level = parser["logging"].get("log_level", cfg.log_level)
        if parser.has_section("bot"):
            cfg.commands_dir = Path(
                parser["bot"].get("commands_dir", str(cfg.commands_dir))
            )
            cfg.commands_enabled = parser["bot"].getboolean(
                "enabled", cfg.commands_enabled
            )
            cfg.rx_log_decrypt = parser["bot"].getboolean(
                "rx_log_decrypt", cfg.rx_log_decrypt
            )
            pk = parser["bot"].get("privkey_path", "")
            if pk:
                cfg.privkey_path = Path(pk)
            cfg.dm_max_attempts = parser["bot"].getint(
                "dm_max_attempts", cfg.dm_max_attempts
            )
            cfg.dm_flood_after = parser["bot"].getint(
                "dm_flood_after", cfg.dm_flood_after
            )
            cfg.dm_max_flood_attempts = parser["bot"].getint(
                "dm_max_flood_attempts", cfg.dm_max_flood_attempts
            )
            owners_raw = parser["bot"].get("owner_pubkeys", "")
            if owners_raw:
                seen = set()
                for tok in re.split(r"[,\s]+", owners_raw):
                    tok = tok.strip().lower()
                    if not tok or tok in seen:
                        continue
                    if len(tok) != 64 or not all(c in "0123456789abcdef" for c in tok):
                        sys.stderr.write(
                            f"ERROR: owner_pubkeys entry {tok!r} must be 64 hex chars\n"
                        )
                        sys.exit(2)
                    cfg.owner_pubkeys.append(tok)
                    seen.add(tok)
        if parser.has_section("env"):
            # Push [env] keys into the process environment so command
            # scripts (and anything else) can read them via os.environ,
            # e.g. commands/pws.py reads os.environ["PWS_API_KEY"].
            # Keys are uppercased to match the conventional env-var names
            # the scripts look up (configparser lowercases option names).
            # setdefault means a real shell/systemd env var takes
            # precedence over a value set here.
            for k, v in parser["env"].items():
                os.environ.setdefault(k.upper(), v)
        if parser.has_section("web"):
            w = parser["web"]
            cfg.web_enabled = w.getboolean("enabled", cfg.web_enabled)
            cfg.web_host = w.get("host", cfg.web_host)
            cfg.web_port = w.getint("port", cfg.web_port)
            cfg.web_admin_user = w.get("admin_user", cfg.web_admin_user)
            cfg.web_admin_password_hash = w.get(
                "admin_password_hash", cfg.web_admin_password_hash
            )
            cfg.web_session_secret = w.get(
                "session_secret", cfg.web_session_secret
            )
            cfg.web_cors_origins = w.get("cors_origins", cfg.web_cors_origins)
            tokens_raw = w.get("api_tokens", "")
            if tokens_raw:
                cfg.web_api_tokens = [
                    t.strip() for t in re.split(r"[,\s]+", tokens_raw)
                    if t.strip()
                ]
            cert = w.get("tls_cert", "")
            key = w.get("tls_key", "")
            if cert:
                cfg.web_tls_cert = Path(cert)
            if key:
                cfg.web_tls_key = Path(key)
        if parser.has_section("channels"):
            for key, value in parser["channels"].items():
                try:
                    idx = int(key)
                except ValueError:
                    continue
                if ":" in value:
                    name, secret_hex = value.split(":", 1)
                    name = name.strip()
                    try:
                        secret = bytes.fromhex(secret_hex.strip())
                    except ValueError:
                        sys.stderr.write(
                            f"ERROR: invalid hex secret for channel {idx}\n"
                        )
                        sys.exit(2)
                else:
                    name = value.strip()
                    if name.startswith("#"):
                        secret = hashlib.sha256(name.encode("utf-8")).digest()[:16]
                    else:
                        sys.stderr.write(
                            f"ERROR: channel {idx} ({name!r}) needs explicit "
                            f":secret_hex (only '#'-prefixed names auto-derive)\n"
                        )
                        sys.exit(2)
                if len(secret) != 16:
                    sys.stderr.write(
                        f"ERROR: channel {idx} secret must be 16 bytes\n"
                    )
                    sys.exit(2)
                cfg.channels[idx] = (name, secret)

    # CLI overrides
    if args.transport:
        cfg.transport = args.transport.strip().lower()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.serial_port:
        cfg.serial_port = args.serial_port
    if args.serial_baud is not None:
        cfg.serial_baud = args.serial_baud
    if args.device_pin:
        cfg.device_pin = args.device_pin
    if args.db:
        cfg.db_path = Path(args.db)
    if args.logs_dir:
        cfg.logs_dir = Path(args.logs_dir)
    if args.log_level:
        cfg.log_level = args.log_level
    if args.commands_dir:
        cfg.commands_dir = Path(args.commands_dir)
    if args.log_channels:
        cfg.log_channels = args.log_channels
    if args.max_channel_messages is not None:
        cfg.max_channel_messages = args.max_channel_messages
    if args.max_dms is not None:
        cfg.max_dms = args.max_dms
    if args.max_contacts is not None:
        cfg.max_contacts = args.max_contacts
    if args.max_packets is not None:
        cfg.max_packets = args.max_packets
    if args.disable_commands:
        cfg.commands_enabled = False
    if args.no_auto_reconnect:
        cfg.auto_reconnect = False
    if args.debug:
        cfg.debug = True
    if args.privkey_path:
        cfg.privkey_path = Path(args.privkey_path)
    if args.dm_max_attempts is not None:
        cfg.dm_max_attempts = args.dm_max_attempts
    if args.dm_flood_after is not None:
        cfg.dm_flood_after = args.dm_flood_after
    if args.dm_max_flood_attempts is not None:
        cfg.dm_max_flood_attempts = args.dm_max_flood_attempts
    if cfg.privkey_path is None:
        cfg.privkey_path = cfg.db_path.with_suffix(".privkey")

    # if a serial port was supplied while transport is still the default
    # 'tcp' and no host is set, assume the user meant serial.
    if cfg.transport == "tcp" and cfg.serial_port and not cfg.host:
        cfg.transport = "serial"

    if cfg.transport not in ("tcp", "serial"):
        sys.stderr.write(
            f"ERROR: transport must be 'tcp' or 'serial', got {cfg.transport!r}\n"
        )
        sys.exit(2)

    if cfg.transport == "serial":
        if not cfg.serial_port:
            sys.stderr.write(
                "ERROR: serial transport requires --serial-port "
                "(or [radio] serial_port in mcbot.conf)\n"
            )
            sys.exit(2)
    else:  # tcp
        if not cfg.host:
            sys.stderr.write(
                "ERROR: tcp transport requires --host "
                "(or [radio] host in mcbot.conf)\n"
            )
            sys.exit(2)

    return cfg


# ---------------------------------------------------------------------------
# Logging
#
def setup_logging(cfg: Config) -> logging.Logger:
    # configure (or re-configure on !adm restart) the mcbot and meshcore
    # loggers. rebuild handlers from scratch so a restart picks up a
    # fresh file handle and a fresh StreamHandler bound to current sys.stderr.
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    def _reset(name: str, level) -> logging.Logger:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        lg.propagate = False
        return lg

    bot_log = _reset("mcbot", cfg.log_level.upper())
    mc_log = _reset(
        "meshcore",
        logging.DEBUG if cfg.debug else logging.INFO,
    )

    fh = logging.handlers.RotatingFileHandler(
        cfg.logs_dir / "mcbot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    for lg in (bot_log, mc_log):
        lg.addHandler(fh)
        lg.addHandler(ch)

    return bot_log


# ---------------------------------------------------------------------------
# Database
# 
SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    public_key TEXT PRIMARY KEY,
    adv_name TEXT,
    type INTEGER,
    flags INTEGER,
    out_path TEXT,
    out_path_len INTEGER,
    out_path_hash_mode INTEGER,
    adv_lat REAL,
    adv_lon REAL,
    last_advert INTEGER,
    lastmod INTEGER,
    first_seen_at INTEGER,
    last_synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_contacts_prefix
    ON contacts(substr(public_key,1,12));
CREATE INDEX IF NOT EXISTS idx_contacts_advname ON contacts(adv_name);

CREATE TABLE IF NOT EXISTS channels (
    channel_idx INTEGER PRIMARY KEY,
    name TEXT,
    secret_hex TEXT,
    last_synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS channel_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_idx INTEGER,
    channel_name TEXT,
    sender_name TEXT,
    sender_pubkey TEXT,
    text TEXT,
    sender_timestamp INTEGER,
    path_len INTEGER,
    path_hash_mode INTEGER,
    path TEXT,
    txt_type INTEGER,
    snr REAL,
    rssi INTEGER,
    attempt INTEGER,
    recv_time INTEGER,
    received_at INTEGER,
    is_outgoing INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chanmsg_ch
    ON channel_messages(channel_idx, id);

CREATE TABLE IF NOT EXISTS direct_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_pubkey_prefix TEXT,
    sender_pubkey TEXT,
    sender_name TEXT,
    text TEXT,
    sender_timestamp INTEGER,
    path_len INTEGER,
    path_hash_mode INTEGER,
    txt_type INTEGER,
    snr REAL,
    signature TEXT,
    received_at INTEGER,
    is_outgoing INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dm_prefix
    ON direct_messages(sender_pubkey_prefix, id);

CREATE TABLE IF NOT EXISTS received_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at INTEGER,
    event_type TEXT,
    packet_type TEXT,
    sender_pubkey_prefix TEXT,
    sender_pubkey TEXT,
    sender_name TEXT,
    path TEXT,
    path_len INTEGER,
    snr REAL,
    rssi INTEGER,
    channel_idx INTEGER,
    text TEXT,
    payload_json TEXT,
    attributes_json TEXT,
    raw_hex TEXT                  -- on-air RF bytes (RX_LOG_DATA only)
);

CREATE TABLE IF NOT EXISTS device_info (
    key TEXT PRIMARY KEY,
    value TEXT,
    last_updated INTEGER
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bot_groups (
    name TEXT PRIMARY KEY,
    description TEXT,
    is_system INTEGER DEFAULT 0,
    created_at INTEGER,
    -- all_users=1 means every user is implicitly a member of this group
    -- (the "*" member). 'public' is seeded this way so public-granted
    -- commands are open to everyone
    all_users INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_group_commands (
    group_name TEXT NOT NULL,
    command TEXT NOT NULL,
    PRIMARY KEY (group_name, command),
    FOREIGN KEY (group_name) REFERENCES bot_groups(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_users (
    pubkey TEXT PRIMARY KEY,
    name TEXT,
    added_by TEXT,
    added_at INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS bot_user_groups (
    pubkey TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (pubkey, group_name),
    FOREIGN KEY (pubkey) REFERENCES bot_users(pubkey) ON DELETE CASCADE,
    FOREIGN KEY (group_name) REFERENCES bot_groups(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    actor_pubkey TEXT,
    actor_name TEXT,
    action TEXT,
    target TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON bot_audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS command_cooldowns (
    pubkey TEXT,
    command TEXT,
    last_used_at REAL,
    PRIMARY KEY (pubkey, command)
);

-- Per-command runtime config. Authorization is NOT stored here: it is
-- groups-only and fail-closed — a command runs iff it is granted to a group
-- the caller belongs to (or to an all-users group such as 'public'). See
-- is_authorized_for_command().
CREATE TABLE IF NOT EXISTS command_config (
    command TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    cooldown_seconds INTEGER,
    allowed_channels TEXT,        -- JSON array of names/indexes
    triggers TEXT,                -- JSON array of trigger strings
    description TEXT,
    allow_dm INTEGER,
    dm_only INTEGER
);
"""


class DB:
    def __init__(self, path: Path, log: logging.Logger):
        self.path = path
        self.log = log
        self.conn = sqlite3.connect(
            str(path), timeout=10.0, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.lock = asyncio.Lock()

    async def execute(self, sql: str, params: tuple = ()):
        async with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self.lock:
            return self.conn.execute(sql, params).fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Command loader / context
# ---------------------------------------------------------------------------
@dataclass
class CommandSpec:
    name: str
    triggers: list
    description: str
    cooldown_default: int
    allowed_channels: Optional[list]
    allow_dm: bool
    handle: Any
    module_name: str
    # DM_ONLY=True means refuse channel invocations even if ALLOWED_CHANNELS
    # would otherwise permit them. Intended for commands whose authorization
    # must be cryptographically anchored to a private key (i.e. sensitive
    # commands), since channel sender names are spoofable.
    dm_only: bool = False
    # The imported plugin module, kept so commands like !help can read
    # optional module attributes (HELP_DETAIL, HELP_HIDDEN).
    module: Any = None


class CommandLoader:
    def __init__(self, cmd_dir: Path, log: logging.Logger):
        self.cmd_dir = cmd_dir
        self.log = log
        self.commands: dict[str, CommandSpec] = {}

    def load_all(self) -> None:
        if not self.cmd_dir.is_dir():
            self.log.warning(
                "commands dir %s does not exist; no commands loaded",
                self.cmd_dir,
            )
            return
        for path in sorted(self.cmd_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                self._load_one(path)
            except Exception:
                self.log.exception(
                    "failed to load command script %s", path
                )

    def reload_all(self) -> tuple[int, int, list[str]]:
        # drop loaded commands and rescan the directory.
        # returns (count_before, count_after, errors).
        n_before = len(self.commands)
        errors: list[str] = []
        # discard cached module objects so imports use fresh code.
        for key in list(sys.modules.keys()):
            if key.startswith("mcbot_cmd_"):
                del sys.modules[key]
        self.commands.clear()
        if not self.cmd_dir.is_dir():
            return n_before, 0, [f"commands dir {self.cmd_dir} missing"]
        for path in sorted(self.cmd_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                self._load_one(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")
                self.log.exception("reload: failed loading %s", path)
        return n_before, len(self.commands), errors

    def _load_one(self, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(
            f"mcbot_cmd_{path.stem}", path
        )
        if not spec or not spec.loader:
            self.log.error("could not build import spec for %s", path)
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        name = getattr(mod, "NAME", path.stem)
        triggers = getattr(mod, "TRIGGERS", [f"!{name}"])
        if not callable(getattr(mod, "handle", None)):
            self.log.error(
                "command %s has no handle() function", path.name
            )
            return

        cs = CommandSpec(
            name=name,
            triggers=[str(t).lower() for t in triggers],
            description=getattr(mod, "DESCRIPTION", ""),
            cooldown_default=int(getattr(mod, "COOLDOWN_DEFAULT", 30)),
            allowed_channels=getattr(mod, "ALLOWED_CHANNELS", None),
            allow_dm=bool(getattr(mod, "ALLOW_DM", True)),
            handle=mod.handle,
            module_name=path.stem,
            dm_only=bool(getattr(mod, "DM_ONLY", False)),
            module=mod,
        )
        self.commands[name] = cs
        self.log.info(
            "loaded command '%s' (triggers=%s, cooldown=%ds, dm_only=%s) from %s",
            cs.name, cs.triggers, cs.cooldown_default,
            cs.dm_only, path.name,
        )

    def match(self, text: str) -> Optional[CommandSpec]:
        if not text:
            return None
        low = text.lower().lstrip()
        for cs in self.commands.values():
            for t in cs.triggers:
                if low.startswith(t):
                    return cs
        return None


@dataclass
class CommandContext:
    sender_name: Optional[str]
    sender_pubkey: Optional[str]
    sender_pubkey_prefix: Optional[str]
    message_text: str
    is_dm: bool
    channel_idx: Optional[int]
    channel_name: Optional[str]
    path: Optional[str]
    path_len: Optional[int]
    path_hash_mode: Optional[int]
    snr: Optional[float]
    rssi: Optional[int]
    sender_timestamp: Optional[int]
    bot: Any  # MCBot


# ---------------------------------------------------------------------------
# The bot
# 
class MCBot:
    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.logger = log
        self.db = DB(cfg.db_path, log)
        self.loader = CommandLoader(cfg.commands_dir, log)
        self.mc: Optional[MeshCore] = None
        self.stop_event = asyncio.Event()
        self.event_count = 0
        self._subs: list = []
        self._log_channel_set: Optional[set] = None
        self._contacts_dirty = False
        self.my_pubkey: Optional[str] = None
        self.my_pubkey_byte: Optional[int] = None
        # set true by !adm restart to make amain() loop and rebuild a
        # fresh MCBot instance rather than exiting after shutdown.
        self.restart_requested: bool = False
        self.my_private_key: Optional[bytes] = None  # 64 bytes
        self.my_public_key_bytes: Optional[bytes] = None  # 32 bytes
        # web admin UI/API (uvicorn server + its serve() task), or None
        self._web_server = None
        self._web_task = None
        # live fan-out feeds for the web UI (cheap no-ops with no subscribers).
        from webapi.broadcast import Broadcaster
        self.web_packet_feed = Broadcaster()
        self.web_message_feed = Broadcaster()
        # shared mutation/invariant/audit service ('!adm' + web API).
        from management import Management
        self.mgmt = Management(self)
        # channel_hash_byte -> (idx, name, 16-byte secret)
        self.channels_by_hash: dict[int, tuple[int, str, bytes]] = {}
        # pkt_hash dedupe (same-attempt retransmissions via different paths)
        self._recent_pkt_hashes: deque = deque(maxlen=200)
        self._recent_pkt_hash_set: set[int] = set()
        # dedupe any DMs with the same sender_pubkey + sender_timestamp combo.
        # since meshcore increments an attempt counter, the ciphertext and pkt_hash
        # is different for each retry, but timestamp + sender stays the same.
        # without this we'd run the same command up to 3 times.
        self._recent_msg_keys: deque = deque(maxlen=200)
        self._recent_msg_key_set: set[tuple] = set()
        # dedupe for channel messages on channel_idx, sender_timestamp, text.
        # a channel whose key is programmed into the radio is decrypted twice:
        # once by the radio (delivered via CHANNEL_MSG_RECV) and once by this
        # script using RX_LOG_DATA. without the dedupe we'd store and respond
        # to commands on each message twice.
        self._recent_chan_keys: deque = deque(maxlen=256)
        self._recent_chan_key_set: set[tuple] = set()

    # channel logging filter
    def _parse_log_channels(self) -> None:
        v = (self.cfg.log_channels or "all").strip().lower()
        if v in ("all", "*", ""):
            self._log_channel_set = None
            return
        self._log_channel_set = {x.strip() for x in v.split(",") if x.strip()}

    def _should_log_channel(
        self, channel_idx: Optional[int], channel_name: Optional[str]
    ) -> bool:
        if self._log_channel_set is None:
            return True
        s = self._log_channel_set
        if channel_idx is not None and str(channel_idx) in s:
            return True
        if channel_name:
            cn = channel_name.lower()
            if cn in s or cn.lstrip("#") in s:
                return True
        return False

    # contact resolution
    async def resolve_prefix(
        self, prefix: str
    ) -> tuple[Optional[str], Optional[str]]:
        """6-byte hex prefix -> (full_pubkey, adv_name)."""
        if not prefix:
            return None, None
        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts "
            "WHERE substr(public_key,1,12)=?",
            (prefix.lower(),),
        )
        if not rows:
            return None, None
        if len(rows) > 1:
            self.logger.warning(
                "ambiguous pubkey prefix %s (%d matches)",
                prefix, len(rows),
            )
            return None, "<ambiguous>"
        return rows[0]["public_key"], rows[0]["adv_name"]

    async def resolve_name(self, name: str) -> Optional[str]:
        if not name:
            return None
        row = await self.db.fetchone(
            "SELECT public_key FROM contacts WHERE adv_name=? LIMIT 1",
            (name,),
        )
        return row["public_key"] if row else None

    # initial sync
    async def sync_device_info(self) -> None:
        # SELF_INFO is cached by the lib during appstart (mc.self_info)
        try:
            si = getattr(self.mc, "self_info", None)
            if isinstance(si, dict) and si:
                await self._upsert_device_info(si, "self_info")
        except Exception:
            self.logger.exception("self_info capture failed")
        try:
            ev = await self.mc.commands.send_device_query()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "device_info")
        except Exception:
            self.logger.exception("send_device_query failed")
        try:
            ev = await self.mc.commands.get_bat()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "battery")
        except Exception:
            self.logger.exception("get_bat failed")

    async def _upsert_device_info(self, payload: dict, group: str) -> None:
        now = int(time.time())
        for k, v in payload.items():
            await self.db.execute(
                "INSERT INTO device_info(key,value,last_updated) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, last_updated=excluded.last_updated",
                (f"{group}.{k}", json.dumps(v, default=str), now),
            )

    async def sync_contacts(self) -> None:
        row = await self.db.fetchone(
            "SELECT value FROM bot_meta WHERE key='contacts_lastmod'"
        )
        lastmod = 0
        if row and row["value"] and str(row["value"]).isdigit():
            lastmod = int(row["value"])

        try:
            ev = await self.mc.commands.get_contacts(lastmod=lastmod, timeout=10)
        except TypeError:
            try:
                ev = await self.mc.commands.get_contacts()
            except Exception:
                self.logger.exception("get_contacts failed")
                return
        except Exception:
            self.logger.exception("get_contacts failed")
            return

        if not ev or not isinstance(ev.payload, dict):
            return
        contacts = ev.payload
        now = int(time.time())
        count = 0
        for pk, c in contacts.items():
            if not isinstance(c, dict):
                continue
            await self.db.execute(
                """INSERT INTO contacts
                (public_key, adv_name, type, flags, out_path, out_path_len,
                 out_path_hash_mode, adv_lat, adv_lon, last_advert, lastmod,
                 first_seen_at, last_synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(public_key) DO UPDATE SET
                    adv_name=excluded.adv_name, type=excluded.type,
                    flags=excluded.flags, out_path=excluded.out_path,
                    out_path_len=excluded.out_path_len,
                    out_path_hash_mode=excluded.out_path_hash_mode,
                    adv_lat=excluded.adv_lat, adv_lon=excluded.adv_lon,
                    last_advert=excluded.last_advert,
                    lastmod=excluded.lastmod,
                    last_synced_at=excluded.last_synced_at""",
                (
                    pk.lower(),
                    c.get("adv_name"),
                    c.get("type"),
                    c.get("flags"),
                    c.get("out_path"),
                    c.get("out_path_len"),
                    c.get("out_path_hash_mode"),
                    c.get("adv_lat"),
                    c.get("adv_lon"),
                    c.get("last_advert"),
                    c.get("lastmod"),
                    now,
                    now,
                ),
            )
            count += 1

        new_lastmod = None
        if hasattr(ev, "attributes") and isinstance(ev.attributes, dict):
            new_lastmod = ev.attributes.get("lastmod")
        if new_lastmod is not None:
            await self.db.execute(
                "INSERT INTO bot_meta(key,value) VALUES('contacts_lastmod',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_lastmod),),
            )
        await self._trim_contacts()
        self.logger.info("contacts sync: %d contacts upserted", count)

    async def _trim_contacts(self) -> None:
        # cap the contacts table, rolling off the oldest. using the
        # last_synced_at timestamp (the bot's own clock) rather than 
        # last_advert which is sent by the sender and is sometimes complete
        # shit (stuff like 2000 and 2102 from misconfigured node clocks)
        keep = self.cfg.max_contacts
        if keep <= 0:
            return
        cur = await self.db.execute(
            "DELETE FROM contacts WHERE public_key NOT IN ("
            "SELECT public_key FROM contacts "
            "ORDER BY COALESCE(last_synced_at,0) DESC, "
            "COALESCE(last_advert,0) DESC LIMIT ?)",
            (keep,),
        )
        if cur.rowcount:
            self.logger.info(
                "contacts trim: removed %d oldest (cap=%d)",
                cur.rowcount, keep,
            )

    async def sync_channels(self) -> None:
        row = await self.db.fetchone(
            "SELECT value FROM device_info WHERE key='device_info.max_channels'"
        )
        max_ch = 16
        if row and row["value"]:
            try:
                max_ch = int(json.loads(row["value"]))
            except Exception:
                pass
        count = 0
        attempted = 0
        errors = 0
        empty = 0
        none_resp = 0
        for i in range(max_ch):
            try:
                ev = await self.mc.commands.get_channel(i)
            except Exception:
                self.logger.warning(
                    "get_channel(%d) raised", i, exc_info=True
                )
                errors += 1
                continue
            attempted += 1
            if ev is None:
                none_resp += 1
                continue
            ev_type = getattr(ev.type, "name", str(ev.type))
            if ev_type == "ERROR":
                errors += 1
                continue
            if not isinstance(ev.payload, dict):
                self.logger.debug(
                    "get_channel(%d) returned %s with non-dict payload: %r",
                    i, ev_type, ev.payload,
                )
                continue
            p = ev.payload
            name = p.get("channel_name") or p.get("name")
            secret = p.get("channel_secret") or p.get("secret")
            if isinstance(secret, (bytes, bytearray)):
                secret_hex = secret.hex()
            elif isinstance(secret, str):
                secret_hex = secret
            else:
                secret_hex = None
            if not name:
                empty += 1
                continue
            await self.db.execute(
                """INSERT INTO channels(channel_idx,name,secret_hex,last_synced_at)
                VALUES(?,?,?,?)
                ON CONFLICT(channel_idx) DO UPDATE SET
                    name=excluded.name, secret_hex=excluded.secret_hex,
                    last_synced_at=excluded.last_synced_at""",
                (i, name, secret_hex, int(time.time())),
            )
            count += 1
            self.logger.info("channel %d = %r", i, name)
        self.logger.info(
            "channels sync: %d recorded (attempted=%d, empty=%d, errors=%d, none=%d, max=%d)",
            count, attempted, empty, errors, none_resp, max_ch,
        )

    # firehose / packet log
    async def _firehose(self, event) -> None:
        try:
            await self._record_packet(event)
        except Exception:
            self.logger.exception("firehose handler failed")
        self.event_count += 1

    async def _record_packet(self, event) -> None:
        evt_name = event.type.name if hasattr(event.type, "name") else str(
            event.type
        )
        packet_type = PACKET_TYPE_MAP.get(evt_name, evt_name)
        payload = event.payload
        if not isinstance(payload, (dict, list)):
            payload = {"value": payload}
        attrs = getattr(event, "attributes", {}) or {}

        sender_prefix = None
        sender_pubkey = None
        sender_name = None
        path = None
        path_len = None
        snr = None
        rssi = None
        channel_idx = None
        text = None

        payload_typename = None
        route_typename = None
        ack_code = None
        path_hash_size: Optional[int] = None
        extra_bits: list[str] = []
        if isinstance(payload, dict):
            sender_prefix = payload.get("pubkey_prefix")
            p = payload.get("path")
            if isinstance(p, str):
                path = p
            path_len = payload.get("path_len")
            # RX_LOG_DATA sets path_hash_size directly (1/2/3 bytes per hop)
            # other events use path_hash_mode (0/1/2 — sentinel -1 for none)
            path_hash_size = payload.get("path_hash_size")
            if path_hash_size is None:
                mode = payload.get("path_hash_mode")
                if mode is not None and mode >= 0:
                    path_hash_size = mode + 1
            snr = payload.get("SNR", payload.get("snr"))
            rssi = payload.get("RSSI", payload.get("rssi"))
            channel_idx = payload.get("channel_idx")
            text = payload.get("text") or payload.get("message")
            payload_typename = payload.get("payload_typename")
            route_typename = payload.get("route_typename")
            if evt_name == "ADVERTISEMENT":
                pk = payload.get("public_key") or payload.get("pubkey")
                if pk and isinstance(pk, str):
                    sender_pubkey = pk
                    sender_prefix = pk[:12]
            elif evt_name in ("NEXT_CONTACT", "NEW_CONTACT"):
                pk = payload.get("public_key")
                name = payload.get("adv_name")
                if pk and isinstance(pk, str):
                    sender_pubkey = pk
                    sender_prefix = pk[:12]
                if name:
                    sender_name = name
                opl = payload.get("out_path_len")
                if opl == -1:
                    extra_bits.append("route=flood")
                elif opl is not None:
                    extra_bits.append(f"hops={opl}")
                last_adv = payload.get("last_advert")
                if last_adv:
                    extra_bits.append(f"last_adv={last_adv}")
            elif evt_name == "ACK":
                ack_code = payload.get("code")

        if sender_prefix:
            full, name = await self.resolve_prefix(sender_prefix)
            sender_pubkey = sender_pubkey or full
            sender_name = name

        if evt_name == "CHANNEL_MSG_RECV" and text:
            m = CHANNEL_SENDER_RE.match(text)
            if m:
                sender_name = m.group(1)
                pk = await self.resolve_name(sender_name)
                if pk:
                    sender_pubkey = pk

        try:
            payload_json = json.dumps(payload, default=str)
        except Exception:
            payload_json = json.dumps({"_unrepr": str(payload)})
        try:
            attrs_json = json.dumps(attrs, default=str)
        except Exception:
            attrs_json = "{}"

        # only RX_LOG_DATA carries the raw packet bytes (as a hex string
        # in payload["payload"]). store it so the web inspector can break
        # a packet down without re-digging it out of payload_json.
        raw_hex = None
        if evt_name == "RX_LOG_DATA" and isinstance(payload, dict):
            rf = payload.get("payload")
            if isinstance(rf, str) and rf:
                raw_hex = rf

        now_ts = int(time.time())
        cur = await self.db.execute(
            """INSERT INTO received_packets
            (received_at, event_type, packet_type, sender_pubkey_prefix,
             sender_pubkey, sender_name, path, path_len, snr, rssi,
             channel_idx, text, payload_json, attributes_json, raw_hex)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_ts, evt_name, packet_type,
                sender_prefix, sender_pubkey, sender_name,
                path, path_len, snr, rssi, channel_idx, text,
                payload_json, attrs_json, raw_hex,
            ),
        )
        await self._trim_global("received_packets", self.cfg.max_packets)

        # live packet feed for the web UI (only build the dict if watched).
        if self.web_packet_feed.has_subscribers():
            self.web_packet_feed.publish({
                "id": cur.lastrowid,
                "received_at": now_ts,
                "event_type": evt_name,
                "packet_type": packet_type,
                "sender_pubkey_prefix": sender_prefix,
                "sender_pubkey": sender_pubkey,
                "sender_name": sender_name,
                "path": path,
                "path_len": path_len,
                "snr": snr,
                "rssi": rssi,
                "channel_idx": channel_idx,
                "text": text,
                "has_raw": raw_hex is not None,
            })

        # for TEXT_MSG packets observed via RX_LOG, the first two bytes of
        # pkt_payload are the destination and sender pubkey hashes (1 byte
        # each). check them so we can see whether a DM is addressed to us.
        dst_byte = None
        src_byte = None
        if payload_typename in ("TEXT_MSG", "ACK") and isinstance(payload, dict):
            pkt = payload.get("pkt_payload")
            if isinstance(pkt, (bytes, bytearray)) and len(pkt) >= 2:
                dst_byte = pkt[0]
                src_byte = pkt[1]

        snippet = (text[:60] + "…") if text and len(text) > 60 else (text or "")
        sender_disp = sender_name or sender_prefix or ""
        bits = [packet_type]
        if payload_typename:
            bits.append(f"pl={payload_typename}")
        if route_typename:
            bits.append(f"rt={route_typename}")
        if dst_byte is not None:
            for_us = (
                self.my_pubkey_byte is not None
                and dst_byte == self.my_pubkey_byte
            )
            bits.append(f"dst={dst_byte:02x}{'(us)' if for_us else ''}")
        if src_byte is not None:
            bits.append(f"src={src_byte:02x}")
        if sender_disp:
            bits.append(f"from={sender_disp}")
        if channel_idx is not None:
            bits.append(f"ch={channel_idx}")
        if path:
            bits.append(f"path={format_path(path, path_hash_size)}")
        if snr is not None:
            bits.append(f"snr={snr}")
        if snippet:
            bits.append(f'text="{snippet}"')
        if ack_code:
            bits.append(f"code={ack_code}")
        if extra_bits:
            bits.extend(extra_bits)
        # error responses include error_code, code_string, reason
        if evt_name == "ERROR" and isinstance(payload, dict):
            ec = payload.get("error_code")
            cs = payload.get("code_string")
            rs = payload.get("reason")
            if ec is not None:
                bits.append(f"code={ec}")
            if cs:
                bits.append(f"code_str={cs!r}")
            if rs:
                bits.append(f"reason={rs!r}")
        self.logger.info(" ".join(bits))

    # DM / channel handlers
    async def _on_dm(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        await self._ingest_dm(
            sender_pubkey_prefix=payload.get("pubkey_prefix"),
            sender_pubkey=None,
            sender_name=None,
            text=payload.get("text", "") or "",
            sender_timestamp=payload.get("sender_timestamp"),
            path_len=payload.get("path_len"),
            path_hash_mode=payload.get("path_hash_mode"),
            txt_type=payload.get("txt_type"),
            snr=payload.get("SNR"),
            signature=payload.get("signature"),
        )

    async def _ingest_dm(
        self, *,
        sender_pubkey_prefix: Optional[str],
        sender_pubkey: Optional[str],
        sender_name: Optional[str],
        text: str,
        sender_timestamp: Optional[int] = None,
        path_len: Optional[int] = None,
        path_hash_mode: Optional[int] = None,
        txt_type: Optional[int] = None,
        snr: Optional[float] = None,
        signature: Optional[str] = None,
    ) -> None:
        # fill any missing identity fields from the contacts table.
        if sender_pubkey_prefix and not sender_pubkey:
            full, name = await self.resolve_prefix(sender_pubkey_prefix)
            sender_pubkey = sender_pubkey or full
            sender_name = sender_name or name
        # two ingest entry points exist for DMs:
        # _on_rx_log_data:  _handle_inbound_dm (client-side decrypt)
        # _on_dm via CONTACT_MSG_RECV (radio's get_msg queue)
        #
        # on firmware that supports both, the same DM data arrives via
        # both routes. also, sender retries need deduping by
        # sender_pubkey + sender_timestamp. this filters reciving a DM
        # from multiple routes down to ingesting one copy of each message.
        if sender_pubkey and sender_timestamp is not None:
            if self._seen_message(sender_pubkey, sender_timestamp):
                self.logger.info(
                    "DM duplicate suppressed from=%s ts=%d",
                    sender_name or sender_pubkey[:12], sender_timestamp,
                )
                return
        now_ts = int(time.time())
        cur = await self.db.execute(
            """INSERT INTO direct_messages
            (sender_pubkey_prefix, sender_pubkey, sender_name, text,
             sender_timestamp, path_len, path_hash_mode, txt_type, snr,
             signature, received_at, is_outgoing)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                sender_pubkey_prefix, sender_pubkey, sender_name, text,
                sender_timestamp, path_len, path_hash_mode, txt_type,
                snr, signature, now_ts,
            ),
        )
        await self._trim_global("direct_messages", self.cfg.max_dms)

        if self.web_message_feed.has_subscribers():
            self.web_message_feed.publish({
                "kind": "dm",
                "id": cur.lastrowid,
                "sender_pubkey_prefix": sender_pubkey_prefix,
                "sender_pubkey": sender_pubkey,
                "sender_name": sender_name,
                "text": text,
                "sender_timestamp": sender_timestamp,
                "snr": snr,
                "received_at": now_ts,
                "is_outgoing": 0,
            })

        if self.cfg.commands_enabled:
            ctx = CommandContext(
                sender_name=sender_name,
                sender_pubkey=sender_pubkey,
                sender_pubkey_prefix=sender_pubkey_prefix,
                message_text=text,
                is_dm=True,
                channel_idx=None,
                channel_name=None,
                path=None,
                path_len=path_len,
                path_hash_mode=path_hash_mode,
                snr=snr,
                rssi=None,
                sender_timestamp=sender_timestamp,
                bot=self,
            )
            await self._dispatch_command(ctx)

    async def _on_channel_msg(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        text = payload.get("text", "") or ""
        sender_name = None
        m = CHANNEL_SENDER_RE.match(text) if text else None
        if m:
            sender_name = m.group(1)
        await self._ingest_channel_msg(
            channel_idx=payload.get("channel_idx"),
            text=text,
            sender_name=sender_name,
            sender_timestamp=payload.get("sender_timestamp"),
            path=payload.get("path"),
            path_len=payload.get("path_len"),
            path_hash_mode=payload.get("path_hash_mode"),
            txt_type=payload.get("txt_type"),
            snr=payload.get("SNR"),
            rssi=payload.get("RSSI"),
            attempt=payload.get("attempt"),
            recv_time=payload.get("recv_time"),
        )

    async def _ingest_channel_msg(
        self, *,
        channel_idx: Optional[int],
        text: str,
        sender_name: Optional[str] = None,
        sender_timestamp: Optional[int] = None,
        path: Optional[str] = None,
        path_len: Optional[int] = None,
        path_hash_mode: Optional[int] = None,
        txt_type: Optional[int] = None,
        snr: Optional[float] = None,
        rssi: Optional[int] = None,
        attempt: Optional[int] = None,
        recv_time: Optional[int] = None,
    ) -> None:
        # a channel whose key the radio holds is decrypted both by the radio
        # (CHANNEL_MSG_RECV) and by us (RX_LOG_DATA). dedupe the two so we
        # don't store or act upon the message twice.
        if self._seen_channel_message(channel_idx, sender_timestamp, text):
            self.logger.info(
                "channel msg duplicate suppressed ch=%s ts=%s",
                channel_idx, sender_timestamp,
            )
            return
        ch_row = None
        if channel_idx is not None:
            ch_row = await self.db.fetchone(
                "SELECT name FROM channels WHERE channel_idx=?",
                (channel_idx,),
            )
        ch_name = ch_row["name"] if ch_row else None
        sender_pubkey = (
            await self.resolve_name(sender_name) if sender_name else None
        )

        # [channel_logging] controls only whether we store a copy of the
        # message in channel_messages. It doesn't affect command handling.
        if self._should_log_channel(channel_idx, ch_name):
            now_ts = int(time.time())
            cur = await self.db.execute(
                """INSERT INTO channel_messages
                (channel_idx, channel_name, sender_name, sender_pubkey, text,
                 sender_timestamp, path_len, path_hash_mode, path, txt_type,
                 snr, rssi, attempt, recv_time, received_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    channel_idx, ch_name, sender_name, sender_pubkey, text,
                    sender_timestamp, path_len, path_hash_mode, path,
                    txt_type, snr, rssi, attempt, recv_time, now_ts,
                ),
            )
            await self._trim_channel_messages(
                channel_idx, self.cfg.max_channel_messages
            )
            if self.web_message_feed.has_subscribers():
                self.web_message_feed.publish({
                    "kind": "channel",
                    "id": cur.lastrowid,
                    "channel_idx": channel_idx,
                    "channel_name": ch_name,
                    "sender_name": sender_name,
                    "sender_pubkey": sender_pubkey,
                    "text": text,
                    "sender_timestamp": sender_timestamp,
                    "path": path,
                    "snr": snr,
                    "rssi": rssi,
                    "received_at": now_ts,
                })

        if self.cfg.commands_enabled:
            # channel messages typically arrive as "Name: body". strip the
            # "Name: " prefix from the command text so triggers like "!path"
            # match what the user actually typed.
            command_text = text
            if sender_name:
                p = f"{sender_name}: "
                if text.startswith(p):
                    command_text = text[len(p):]
            ctx = CommandContext(
                sender_name=sender_name,
                sender_pubkey=sender_pubkey,
                sender_pubkey_prefix=sender_pubkey[:12] if sender_pubkey else None,
                message_text=command_text,
                is_dm=False,
                channel_idx=channel_idx,
                channel_name=ch_name,
                path=path,
                path_len=path_len,
                path_hash_mode=path_hash_mode,
                snr=snr,
                rssi=rssi,
                sender_timestamp=sender_timestamp,
                bot=self,
            )
            await self._dispatch_command(ctx)

    async def _on_advertisement(self, event) -> None:
        # mark contacts table as needing a refresh; periodic task picks it up.
        self._contacts_dirty = True

    async def _on_rx_log_data(self, event) -> None:
        # RX_LOG_DATA contains every observed RF frame as raw bytes. if rx_log_decrypt
        # is set to true in the config, DMs using radio's exported private key and
        # channel messages using configured channel secrets are decrypted here.
        # on firmware that queues messages (v1.15), this duplicates the get_msg path.
        # since the radio also decrypts and delivers DMs (CONTACT_MSG_RECV) and channel
        # msgs on its programmed channels (CHANNEL_MSG_RECV), the ingest funnel dedups
        # the two. grabbing the RX_LOG_DATA is useful even if we aren't relying on it
        # to decrypt messages since it's the only source of raw on-air bytes for
        # the packet monitor. it sees traffic the radio doesn't decrypt for us, and
        # it covers channels beyond the radio's slot capacity / non-queueing firmware.
        # (the  decrypt+ingest is configured via [bot] rx_log_decrypt; the raw-byte
        # capture in _firehose runs regardless.)
        payload = event.payload
        if not isinstance(payload, dict):
            return
        rf_hex = payload.get("payload")
        if not isinstance(rf_hex, str) or not rf_hex:
            return
        try:
            rf_packet = bytes.fromhex(rf_hex)
        except ValueError:
            return
        env = parse_packet_envelope(rf_packet)
        if env is None:
            return

        pkt_hash = int.from_bytes(
            hashlib.sha256(env.payload).digest()[:4], "little"
        )
        if self._seen_packet(pkt_hash):
            return

        snr = payload.get("snr")
        rssi = payload.get("rssi")
        path_hex = env.path.hex() if env.path else None
        path_len = env.hop_count
        path_hash_mode = env.hash_size - 1

        if env.payload_type == PayloadType.TEXT_MESSAGE.value:
            await self._handle_inbound_dm(
                env.payload, snr=snr, path_hex=path_hex,
                path_len=path_len, path_hash_mode=path_hash_mode,
            )
        elif env.payload_type == PayloadType.GROUP_TEXT.value:
            await self._handle_inbound_channel(
                env.payload, snr=snr, rssi=rssi, path_hex=path_hex,
                path_len=path_len, path_hash_mode=path_hash_mode,
            )
        elif env.payload_type == PayloadType.ADVERT.value:
            # the lib parses adv_key / adv_name / adv_lat / adv_lon /
            # adv_timestamp into the RX_LOG_DATA payload dict.
            await self._update_contact_from_advert(payload)

    async def _update_contact_from_advert(self, payload) -> None:
        # refresh an existing contact's name/location/timestamp from a
        # received advert. updates the DB row only if the contact already
        # exists. new contacts are added by the radio sync per the radio's
        # own auto-add policy (avoids phantom rows here). The radio
        # maintains its own contact store and updates it when it processes
        # the advert, so no push back to the radio is needed.
        pk = payload.get("adv_key")
        if not isinstance(pk, str) or len(pk) != 64:
            return
        pk = pk.lower()
        name = payload.get("adv_name")
        lat = payload.get("adv_lat")
        lon = payload.get("adv_lon")
        ts = payload.get("adv_timestamp")
        now = int(time.time())
        cur = await self.db.execute(
            "UPDATE contacts SET "
            "adv_name=COALESCE(?, adv_name), "
            "adv_lat=COALESCE(?, adv_lat), "
            "adv_lon=COALESCE(?, adv_lon), "
            "last_advert=COALESCE(?, last_advert), "
            "last_synced_at=? "
            "WHERE public_key=?",
            (name, lat, lon, ts, now, pk),
        )
        if cur.rowcount:
            self.logger.debug(
                "advert: refreshed contact %s name=%r", pk[:12], name
            )
        else:
            # unknown to us. let the next radio sync add it
            self._contacts_dirty = True

    async def _handle_inbound_dm(
        self, packet_payload: bytes, *,
        snr: Optional[float], path_hex: Optional[str],
        path_len: Optional[int], path_hash_mode: Optional[int],
    ) -> None:
        if (
            self.my_private_key is None
            or self.my_pubkey_byte is None
        ):
            return  # haven't loaded the key yet
        if len(packet_payload) < 4:
            return
        if packet_payload[0] != self.my_pubkey_byte:
            return  # not addressed to us
        src_byte = packet_payload[1]
        src_hex = f"{src_byte:02x}"
        # find candidate sender contacts by matching pubkey first byte
        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts "
            "WHERE substr(public_key,1,2)=?",
            (src_hex,),
        )
        if not rows:
            self.logger.debug(
                "inbound DM: no contact matches src=%s", src_hex
            )
            return
        decrypted: Optional[DecryptedDM] = None
        sender_pubkey: Optional[str] = None
        sender_name: Optional[str] = None
        for row in rows:
            try:
                their_pk = bytes.fromhex(row["public_key"])
            except ValueError:
                continue
            if len(their_pk) != 32:
                continue
            dec = try_decrypt_dm(
                packet_payload,
                self.my_private_key,
                their_pk,
                self.my_pubkey_byte,
            )
            if dec is not None:
                decrypted = dec
                sender_pubkey = row["public_key"]
                sender_name = row["adv_name"]
                break
        if decrypted is None:
            self.logger.debug(
                "inbound DM from src=%s: %d contact candidate(s), "
                "none decrypted",
                src_hex, len(rows),
            )
            return
        # dedupe (sender_pubkey, sender_timestamp) now lives in
        # _ingest_dm so it filters both this RX_LOG decrypt path AND the
        # radio-queued get_msg/CONTACT_MSG_RECV path, regardless of which
        # fires first.
        self.logger.info(
            "DM decrypted from=%s (%s): %r",
            sender_name or "?", sender_pubkey[:12], decrypted.message,
        )
        await self._ingest_dm(
            sender_pubkey_prefix=sender_pubkey[:12],
            sender_pubkey=sender_pubkey,
            sender_name=sender_name,
            text=decrypted.message,
            sender_timestamp=decrypted.timestamp,
            path_len=path_len,
            path_hash_mode=path_hash_mode,
            txt_type=decrypted.txt_type,
            snr=snr,
        )

    async def _handle_inbound_channel(
        self, packet_payload: bytes, *,
        snr: Optional[float], rssi: Optional[int],
        path_hex: Optional[str], path_len: Optional[int],
        path_hash_mode: Optional[int],
    ) -> None:
        if len(packet_payload) < 3:
            return
        chash = packet_payload[0]
        ch = self.channels_by_hash.get(chash)
        if ch is None:
            return
        idx, name, secret = ch
        dec = decrypt_group_text(packet_payload, secret)
        if dec is None:
            self.logger.debug(
                "channel msg on idx=%d (%s): MAC/AES failed", idx, name
            )
            return
        self.logger.info(
            "channel msg decrypted ch=%d (%s) from=%s: %r",
            idx, name, dec.sender or "?", dec.message,
        )
        # reconstruct "Name: text" if a sender was extracted, so the
        # ingest path's existing channel-msg handling sees a consistent
        # "text" field.
        ingest_text = (
            f"{dec.sender}: {dec.message}" if dec.sender else dec.message
        )
        await self._ingest_channel_msg(
            channel_idx=idx,
            text=ingest_text,
            sender_name=dec.sender,
            sender_timestamp=dec.timestamp,
            path=path_hex,
            path_len=path_len,
            path_hash_mode=path_hash_mode,
            txt_type=dec.flags >> 2,
            snr=snr,
            rssi=rssi,
            attempt=dec.flags & 0x03,
        )

    async def _on_messages_waiting(self, event) -> None:
        self.logger.info("MSG_WAIT received (radio has queued messages)")

    async def _on_new_contact(self, event) -> None:
        # library tells us about a advertising node we didn't know.
        self._contacts_dirty = True

    async def _setup_private_key(self) -> bool:
        # export the radio's private key (or load from cache) and derive pubkey
        path = self.cfg.privkey_path
        key: Optional[bytes] = None
        # try cached file first
        if path and path.is_file():
            try:
                data = path.read_bytes()
                if len(data) == 64:
                    key = data
                    self.logger.info("loaded private key from %s", path)
                else:
                    self.logger.warning(
                        "cached key at %s is wrong length (%d); re-exporting",
                        path, len(data),
                    )
            except Exception:
                self.logger.exception("failed reading %s", path)
        if key is None:
            self.logger.info("exporting private key from radio...")
            try:
                ev = await self.mc.commands.export_private_key()
            except Exception:
                self.logger.exception("export_private_key raised")
                return False
            if not ev:
                self.logger.error("export_private_key returned no event")
                return False
            t = getattr(ev.type, "name", "")
            if t == "PRIVATE_KEY" and isinstance(ev.payload, dict):
                key = ev.payload.get("private_key")
                if not isinstance(key, (bytes, bytearray)) or len(key) != 64:
                    self.logger.error(
                        "PRIVATE_KEY payload bad: %r", ev.payload
                    )
                    return False
                key = bytes(key)
                # persist
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(key)
                    os.chmod(path, 0o600)
                    self.logger.info("saved private key to %s (mode 0600)", path)
                except Exception:
                    self.logger.exception("failed saving key to %s", path)
            elif t == "DISABLED":
                self.logger.error(
                    "private key export is disabled on this firmware; "
                    "client-side DM decryption won't work"
                )
                return False
            else:
                self.logger.error("export_private_key returned %s: %r",
                                  t, ev.payload)
                return False
        self.my_private_key = key
        try:
            self.my_public_key_bytes = derive_public_key(key)
        except Exception:
            self.logger.exception("derive_public_key failed")
            return False
        derived_hex = self.my_public_key_bytes.hex()
        if self.my_pubkey and self.my_pubkey.lower() != derived_hex.lower():
            self.logger.warning(
                "derived pubkey %s != radio-reported %s; "
                "private key may not match this radio",
                derived_hex, self.my_pubkey,
            )
        self.my_pubkey_byte = self.my_public_key_bytes[0]
        return True

    async def _seed_channels_from_conf(self) -> None:
        # one-time seed of the channels table from mcbot.conf's [channels].
        #
        # after first run the DB is the runtime authority, !adm channel
        # add/remove manages it and conf is ignored. ro re-seed
        # from conf, delete all rows from the channels table first.
        if not self.cfg.channels:
            return
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM channels")
        if not (row and row["n"] == 0):
            return
        self.logger.info(
            "first-run channel seed: writing %d conf channels to DB",
            len(self.cfg.channels),
        )
        now = int(time.time())
        for idx, (name, secret) in self.cfg.channels.items():
            try:
                await self.db.execute(
                    "INSERT INTO channels(channel_idx,name,secret_hex,"
                    "last_synced_at) VALUES(?,?,?,?)",
                    (idx, name, secret.hex(), now),
                )
            except Exception:
                self.logger.exception("seed channel %d failed", idx)

    async def _load_channels(self) -> None:
        # rebuild in-memory channel state (cfg.channels + channels_by_hash)
        # from the channels DB table, which is authoritative. run after the
        # conf seed and the radio sync so it reflects both.
        rows = await self.db.fetchall(
            "SELECT channel_idx, name, secret_hex FROM channels "
            "ORDER BY channel_idx"
        )
        # rebuild in-memory state from DB
        self.cfg.channels.clear()
        self.channels_by_hash.clear()
        for r in rows:
            idx = r["channel_idx"]
            name = r["name"] or ""
            secret_hex = r["secret_hex"] or ""
            if not name or not secret_hex:
                continue
            try:
                secret = bytes.fromhex(secret_hex)
            except ValueError:
                continue
            if len(secret) != 16:
                continue
            self.cfg.channels[idx] = (name, secret)
            chash = hashlib.sha256(secret).digest()[0]
            self.channels_by_hash[chash] = (idx, name, secret)
            self.logger.info(
                "channel cfg: idx=%d name=%r hash=%02x", idx, name, chash,
            )
        if not self.cfg.channels:
            self.logger.info("no channels configured")

    async def _program_channels_on_radio(self) -> None:
        # push each configured channel's secret into one of the radio's slots
        # so send_chan_msg(idx, ...) encrypts with the correct key. the radio
        # may not persist these across reboots, so we program them on every connect.
        if not self.cfg.channels:
            return
        ok = 0
        for idx, (name, secret) in self.cfg.channels.items():
            try:
                ev = await self.mc.commands.set_channel(idx, name, secret)
            except Exception:
                self.logger.exception(
                    "set_channel(%d, %r) raised", idx, name
                )
                continue
            t = getattr(ev.type, "name", "") if ev else ""
            if t == "OK":
                ok += 1
            else:
                self.logger.warning(
                    "set_channel(%d, %r) returned %s: %r",
                    idx, name, t, getattr(ev, "payload", None),
                )
        self.logger.info("programmed %d/%d channels into radio slots",
                         ok, len(self.cfg.channels))

    async def add_channel(
        self, name: str, secret: bytes
    ) -> tuple[Optional[int], Optional[str]]:
        # add a channel at runtime. uses the lowest unused radio
        # slot, programs the radio, writes to DB, and updates in-memory
        # state.
        # returns (idx, error). on success error is None and idx is the
        # allocated slot; on failure idx is None.
        if not name:
            return None, "name required"
        if not secret or len(secret) != 16:
            return None, "secret must be 16 bytes"
        existing = await self.db.fetchone(
            "SELECT channel_idx FROM channels WHERE name=?", (name,)
        )
        if existing:
            return None, f"already exists at idx={existing['channel_idx']}"
        used_rows = await self.db.fetchall(
            "SELECT channel_idx FROM channels"
        )
        used = {r["channel_idx"] for r in used_rows}
        max_ch = 40
        new_idx = next(
            (i for i in range(max_ch) if i not in used), None
        )
        if new_idx is None:
            return None, f"all {max_ch} slots in use"
        try:
            ev = await self.mc.commands.set_channel(new_idx, name, secret)
        except Exception as e:
            return None, f"radio set_channel raised: {e}"
        t = getattr(ev.type, "name", "") if ev else ""
        if t != "OK":
            return None, f"radio rejected (response={t})"
        await self.db.execute(
            "INSERT INTO channels(channel_idx,name,secret_hex,last_synced_at) "
            "VALUES(?,?,?,?)",
            (new_idx, name, secret.hex(), int(time.time())),
        )
        self.cfg.channels[new_idx] = (name, secret)
        chash = hashlib.sha256(secret).digest()[0]
        self.channels_by_hash[chash] = (new_idx, name, secret)
        return new_idx, None

    async def remove_channel(
        self, name: str
    ) -> tuple[Optional[int], Optional[str]]:
        # remove a channel by name. clears the radio slot, deletes the
        # DB row, and updates in-memory state.
        # returns (idx, error)
        row = await self.db.fetchone(
            "SELECT channel_idx, secret_hex FROM channels WHERE name=?",
            (name,),
        )
        if not row:
            return None, f"channel {name!r} not found"
        idx = row["channel_idx"]
        old_secret_hex = row["secret_hex"]
        # attempt radio slot clear (empty name + zero key = unset).
        try:
            await self.mc.commands.set_channel(idx, "", b"\x00" * 16)
        except Exception as e:
            self.logger.warning(
                "clear radio slot %d failed: %s", idx, e
            )
        await self.db.execute(
            "DELETE FROM channels WHERE channel_idx=?", (idx,)
        )
        self.cfg.channels.pop(idx, None)
        if old_secret_hex:
            try:
                old_secret = bytes.fromhex(old_secret_hex)
                if len(old_secret) == 16:
                    chash = hashlib.sha256(old_secret).digest()[0]
                    self.channels_by_hash.pop(chash, None)
            except Exception:
                pass
        return idx, None

    def _seen_packet(self, pkt_hash: int) -> bool:
        # return True if pkt_hash was seen recently (filters DM retries)
        if pkt_hash in self._recent_pkt_hash_set:
            return True
        if len(self._recent_pkt_hashes) == self._recent_pkt_hashes.maxlen:
            old = self._recent_pkt_hashes.popleft()
            self._recent_pkt_hash_set.discard(old)
        self._recent_pkt_hashes.append(pkt_hash)
        self._recent_pkt_hash_set.add(pkt_hash)
        return False

    def _seen_message(self, sender_pubkey: str, sender_timestamp: int) -> bool:
        # return True if we've already processed this message.
        # matches on sender_pubkey and sender_timestamp, so meshcore's 3 retries
        # of the same DM (which only differ in the attempt counter) collapse
        # into one command run.
        if not sender_pubkey or sender_timestamp is None:
            return False
        key = (sender_pubkey.lower(), int(sender_timestamp))
        if key in self._recent_msg_key_set:
            return True
        if len(self._recent_msg_keys) == self._recent_msg_keys.maxlen:
            old = self._recent_msg_keys.popleft()
            self._recent_msg_key_set.discard(old)
        self._recent_msg_keys.append(key)
        self._recent_msg_key_set.add(key)
        return False

    def _seen_channel_message(
        self, channel_idx: Optional[int], sender_timestamp: Optional[int],
        text: str,
    ) -> bool:
        # return True if this channel message was already processed by the
        # other decrypt path. matches on channel_idx + sender_timestamp + text:
        # both paths derive these from the same decrypted payload, so they
        # match. when sender_timestamp is absent we can't relliably dedupe (two
        # distinct messages could share text), so we let it through.
        if sender_timestamp is None:
            return False
        key = (channel_idx, int(sender_timestamp), text)
        if key in self._recent_chan_key_set:
            return True
        if len(self._recent_chan_keys) == self._recent_chan_keys.maxlen:
            old = self._recent_chan_keys.popleft()
            self._recent_chan_key_set.discard(old)
        self._recent_chan_keys.append(key)
        self._recent_chan_key_set.add(key)
        return False

    def _cache_identity(self) -> None:
        si = getattr(self.mc, "self_info", None)
        if isinstance(si, dict):
            pk = si.get("public_key")
            if isinstance(pk, str) and len(pk) >= 2:
                self.my_pubkey = pk.lower()
                try:
                    self.my_pubkey_byte = int(pk[0:2], 16)
                except ValueError:
                    self.my_pubkey_byte = None

    async def _log_identity(self) -> None:
        rows = await self.db.fetchall(
            "SELECT key, value FROM device_info "
            "WHERE key LIKE 'self_info.%' OR key LIKE 'device_info.%' "
            "OR key LIKE 'battery.%'"
        )
        info = {}
        for r in rows:
            try:
                info[r["key"]] = json.loads(r["value"])
            except Exception:
                info[r["key"]] = r["value"]
        name = info.get("self_info.name") or info.get("device_info.name")
        pubkey = (
            info.get("self_info.public_key")
            or info.get("device_info.public_key")
        )
        max_contacts = info.get("device_info.max_contacts")
        max_channels = info.get("device_info.max_channels")
        fw_ver = info.get("device_info.ver") or "?"
        fw_build = info.get("device_info.fw_build") or "?"
        model = info.get("device_info.model") or "?"
        self.logger.info(
            "firmware: model=%s ver=%s build=%s",
            model, fw_ver, fw_build,
        )
        freq = info.get("self_info.radio_freq")
        bw = info.get("self_info.radio_bw")
        sf = info.get("self_info.radio_sf")
        cr = info.get("self_info.radio_cr")
        bat = info.get("battery.level")
        bat_disp = (
            f"{bat} mV" if isinstance(bat, (int, float)) and bat > 100
            else f"{bat}%"
        )
        self.logger.info("identity: name=%s pubkey=%s", name, pubkey)
        self.logger.info(
            "device:   max_contacts=%s max_channels=%s battery=%s",
            max_contacts, max_channels, bat_disp,
        )
        if any(x is not None for x in (freq, bw, sf, cr)):
            self.logger.info(
                "radio:    freq=%s MHz bw=%s kHz sf=%s cr=%s",
                freq, bw, sf, cr,
            )

    async def _on_connected(self, event) -> None:
        reconnected = False
        if event and isinstance(event.payload, dict):
            reconnected = bool(event.payload.get("reconnected"))
        self.logger.info(
            "radio %s", "RECONNECTED" if reconnected else "CONNECTED"
        )

    async def _on_disconnected(self, event) -> None:
        reason = None
        if event and isinstance(event.payload, dict):
            reason = event.payload.get("reason")
        self.logger.warning("radio DISCONNECTED reason=%s", reason)

    # command dispatch
    async def _dispatch_command(self, ctx: CommandContext) -> None:
        cs = self.loader.match(ctx.message_text)
        if not cs:
            return

        # read effective config from command_config table. script-level
        # attributes (cs.allow_dm, cs.dm_only, etc.) are only fall-backs
        # used when a DB row is missing or a column is NULL. this means
        # operator edits in the DB take effect on the next invocation
        # without needing a reload or restart.
        cfg_row = await self.db.fetchone(
            "SELECT enabled, cooldown_seconds, "
            "allowed_channels, allow_dm, dm_only "
            "FROM command_config WHERE command=?",
            (cs.name,),
        )
        enabled = True
        cooldown = cs.cooldown_default
        allowed_channels = cs.allowed_channels
        allow_dm = cs.allow_dm
        dm_only = cs.dm_only
        if cfg_row:
            if cfg_row["enabled"] is not None:
                enabled = bool(cfg_row["enabled"])
            if cfg_row["cooldown_seconds"] is not None:
                cooldown = int(cfg_row["cooldown_seconds"])
            raw_chans = cfg_row["allowed_channels"]
            if raw_chans is not None:
                try:
                    parsed = json.loads(raw_chans)
                except Exception:
                    parsed = [
                        s.strip()
                        for s in str(raw_chans).split(",")
                        if s.strip()
                    ]
                # empty list/string in DB = "no restriction" (override
                # any script default). non-empty replaces the default.
                allowed_channels = parsed or None
            if cfg_row["allow_dm"] is not None:
                allow_dm = bool(cfg_row["allow_dm"])
            if cfg_row["dm_only"] is not None:
                dm_only = bool(cfg_row["dm_only"])

        if not enabled:
            return
        if ctx.is_dm and not allow_dm:
            return
        if dm_only and not ctx.is_dm:
            self.logger.info(
                "command '%s' is DM-only; ignoring channel invocation "
                "from %s in %s",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey_prefix,
                ctx.channel_name,
            )
            return

        # resolve full pubkey if we only have a prefix. needed for both the
        # block check and the group-based auth lookup
        if not ctx.sender_pubkey and ctx.sender_pubkey_prefix:
            candidates = await self._pubkey_candidates(ctx)
            if len(candidates) == 1:
                ctx.sender_pubkey = candidates[0]

        # blocked user check: silently drop anything from them
        if ctx.sender_pubkey and await self.is_user_blocked(ctx.sender_pubkey):
            self.logger.info(
                "command '%s' refused: %s is blocked",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey[:12],
            )
            return

        if not ctx.is_dm and allowed_channels:
            cn = (ctx.channel_name or "").lower()
            cn_no_hash = cn.lstrip("#")
            allow_low = [str(x).lower() for x in allowed_channels]
            if (
                cn not in allow_low
                and cn_no_hash not in allow_low
                and str(ctx.channel_idx) not in allow_low
            ):
                return

        # authorization is groups-only and fail-closed: a command runs only
        # if it is granted to a group the caller belongs to, or to the
        # 'public' group (which is also how a command is made open to all).
        # an ungranted command is denied to everyone but owners (who hold
        # the '*' grant).
        ok = await self.is_authorized_for_command(ctx.sender_pubkey, cs.name)
        if not ok:
            self.logger.info(
                "command '%s' denied (not authorized) from %s",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey_prefix,
            )
            return

        key = (
            ctx.sender_pubkey
            or ctx.sender_pubkey_prefix
            or ctx.sender_name
        )
        if not key:
            return
        if cooldown > 0:
            row = await self.db.fetchone(
                "SELECT last_used_at FROM command_cooldowns "
                "WHERE pubkey=? AND command=?",
                (key, cs.name),
            )
            now = time.time()
            if row and (now - row["last_used_at"]) < cooldown:
                remaining = cooldown - (now - row["last_used_at"])
                self.logger.info(
                    "command '%s' from %s skipped: in cooldown "
                    "(%.1fs of %ds left, no reply sent)",
                    cs.name,
                    ctx.sender_name or key,
                    remaining,
                    cooldown,
                )
                return
            await self.db.execute(
                "INSERT INTO command_cooldowns(pubkey,command,last_used_at) "
                "VALUES(?,?,?) ON CONFLICT(pubkey,command) DO UPDATE SET "
                "last_used_at=excluded.last_used_at",
                (key, cs.name, now),
            )

        self.logger.info(
            "running command '%s' for %s in %s",
            cs.name,
            ctx.sender_name or key,
            "DM" if ctx.is_dm else f"ch{ctx.channel_idx}({ctx.channel_name})",
        )
        try:
            result = await cs.handle(ctx)
        except Exception:
            self.logger.exception("command '%s' raised", cs.name)
            return
        if result is None:
            return
        replies = [result] if isinstance(result, str) else list(result)
        # auto-pack multi-line replies so a list-returning command doesn't
        # produce one DM per line. channel replies need more headroom
        # because the radio prepends "<sender_name>: " on TX.
        if ctx.is_dm:
            replies = self.paginate(replies, max_chars=120)
        else:
            replies = self.paginate(replies, max_chars=100)
        for r in replies:
            await self.send_reply(ctx, r)

    async def _pubkey_candidates(self, ctx: CommandContext) -> list[str]:
        # all full 64-hex pubkeys plausibly identifying the message sender.
        # DM senders only deliver a 6-byte prefix, expand via the contacts table.
        out: list[str] = []
        if ctx.sender_pubkey:
            out.append(ctx.sender_pubkey.lower())
        elif ctx.sender_pubkey_prefix:
            rows = await self.db.fetchall(
                "SELECT public_key FROM contacts "
                "WHERE substr(public_key,1,12)=?",
                (ctx.sender_pubkey_prefix.lower(),),
            )
            for r in rows:
                out.append(r["public_key"].lower())
        return out

    async def is_user_blocked(self, pubkey: str) -> bool:
        """Return True if the user is in the 'blocked' group."""
        if not pubkey:
            return False
        row = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups "
            "WHERE lower(pubkey)=? AND group_name='blocked' LIMIT 1",
            (pubkey.lower(),),
        )
        return row is not None

    async def is_authorized_for_command(
        self, pubkey: Optional[str], command: str
    ) -> bool:
        # True if `command` is runnable by `pubkey`. This is the single
        # authorization gate (fail-closed: no grant == denied). Two ways to
        # qualify:
        # 1. An "all users" group (all_users=1 — every user is a member, the
        #    '*' membership) lists `command` (or '*'). Anyone, even an
        #    unresolved sender, can run it. 'public' is seeded all_users=1, so
        #    granting a command to 'public' is how you make it open to all.
        #    The block check has already filtered out blocked users earlier
        #    in the dispatch pipeline.
        # 2. `pubkey` is an explicit member of any group whose command list
        #    grants `command` (or '*'). Owners hold '*', so they run anything.

        row = await self.db.fetchone(
            "SELECT 1 FROM bot_group_commands gc "
            "JOIN bot_groups g ON g.name = gc.group_name "
            "WHERE g.all_users=1 AND gc.command IN (?, '*') LIMIT 1",
            (command,),
        )
        if row:
            return True
        if not pubkey:
            return False
        row = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups ug "
            "JOIN bot_group_commands gc ON gc.group_name = ug.group_name "
            "WHERE lower(ug.pubkey)=? "
            "AND (gc.command=? OR gc.command='*') LIMIT 1",
            (pubkey.lower(), command),
        )
        return row is not None

    async def effective_groups_for_user(
        self, pubkey: Optional[str]
    ) -> list[str]:
        # all groups "pubkey" effectively belongs to: explicit memberships
        # plus every all-users group (the '*' membership). sorted, de-duped.
        # used by !whoami and the user-detail views. 'blocked' is excluded
        # from the all-users side (a block is always explicit)
        rows = await self.db.fetchall(
            "SELECT name FROM bot_groups "
            "WHERE all_users=1 AND name != 'blocked' "
            "UNION "
            "SELECT group_name FROM bot_user_groups WHERE lower(pubkey)=? "
            "ORDER BY name",
            ((pubkey or "").lower(),),
        )
        return [r["name"] for r in rows]

    async def resolve_target_user(
        self, target: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        # resolve a name or pubkey provided into (pubkey, name, error_msg).
        #
        # 64-hex string: returned as pubkey, name pulled from contacts if known.
        # 12-hex string: prefix lookup against contacts.
        # anything else: treated as an adv_name lookup against contacts.
        #
        # on error, returns (None, None, error_msg).
        if not target:
            return None, None, "missing target"
        s = target.strip()
        low = s.lower()
        is_hex = all(c in "0123456789abcdef" for c in low)

        if is_hex and len(low) == 64:
            row = await self.db.fetchone(
                "SELECT adv_name FROM contacts WHERE public_key=?", (low,)
            )
            name = row["adv_name"] if row else None
            return low, name, None

        if is_hex and len(low) == 12:
            rows = await self.db.fetchall(
                "SELECT public_key, adv_name FROM contacts "
                "WHERE substr(public_key,1,12)=?",
                (low,),
            )
            if not rows:
                return None, None, f"no contact with prefix {low}"
            if len(rows) > 1:
                names = ", ".join(r["adv_name"] or "?" for r in rows[:5])
                return None, None, f"ambiguous prefix {low}: {names}"
            return rows[0]["public_key"], rows[0]["adv_name"], None

        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts WHERE adv_name=?",
            (s,),
        )
        if not rows:
            return None, None, f"no contact named {s!r}"
        if len(rows) > 1:
            prefixes = ", ".join(r["public_key"][:12] for r in rows[:5])
            return (
                None, None,
                f"ambiguous name {s!r}: {prefixes} (use pubkey or 12-hex prefix)",
            )
        return rows[0]["public_key"], rows[0]["adv_name"], None

    @staticmethod
    def paginate(lines: list, max_chars: int = 120) -> list:
        # greedy-pack lines (joined by '\\n') into messages of up to
        # max_chars characters. single lines exceeding max_chars are kept
        # intact (the radio will reject them rather than us splitting mid-word).
        # meshcore DM payload caps around ~140 chars after framing. 120 leaves
        # room for path overhead and channel sender-name prepends.
        out: list = []
        current = ""
        for line in lines:
            if not isinstance(line, str):
                line = str(line)
            if not current:
                current = line
                continue
            candidate = current + "\n" + line
            if len(candidate) <= max_chars:
                current = candidate
            else:
                out.append(current)
                current = line
        if current:
            out.append(current)
        return out

    async def audit_log(
        self,
        actor_pubkey: Optional[str],
        actor_name: Optional[str],
        action: str,
        target: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO bot_audit_log(ts, actor_pubkey, actor_name, action, target, detail) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor_pubkey, actor_name, action, target, detail),
        )

    async def seed_command_configs(self) -> int:
        # for each loaded command, ensure a 'command_config' row exists.
        # existing rows are left untouched, operator edits in the DB are the
        # source of truth.
        #
        # note: seeding a command does mot grant it to anyone. Authorization
        # is groups-only and fail-closed, so a freshly-seeded command is
        # runnable only by owners (the '*' grant) until an operator grants it
        # to a group (use '!adm group grant public <cmd>' to make it open).
        # returns the count of rows newly inserted.
        seeded = 0
        for cs in self.loader.commands.values():
            row = await self.db.fetchone(
                "SELECT 1 FROM command_config WHERE command=?", (cs.name,)
            )
            if row:
                continue
            try:
                triggers_json = json.dumps(cs.triggers)
            except Exception:
                triggers_json = None
            try:
                allowed_json = (
                    json.dumps(cs.allowed_channels)
                    if cs.allowed_channels is not None
                    else None
                )
            except Exception:
                allowed_json = None
            await self.db.execute(
                "INSERT INTO command_config "
                "(command, enabled, cooldown_seconds, "
                " allowed_channels, triggers, description, allow_dm, dm_only) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    cs.name,
                    cs.cooldown_default,
                    allowed_json,
                    triggers_json,
                    cs.description,
                    1 if cs.allow_dm else 0,
                    1 if cs.dm_only else 0,
                ),
            )
            seeded += 1
            self.logger.info(
                "seeded command_config for '%s' from script defaults", cs.name
            )
        return seeded

    async def _bootstrap_admin_state(self) -> None:
        """Idempotent: ensure default groups exist and owners from config are
        added. Called once on startup after the DB schema is ready."""
        now = int(time.time())
        # default groups: (name, description, is_system, all_users). 'public'
        # is an all-users group (the '*' member) so commands granted to it are
        # open to everyone and it shows up in !whoami. all_users is only set on
        # first insert (it's not in the ON CONFLICT update), so an operator who
        # later runs '!adm group restrict public' isn't overridden at restart.
        defaults = [
            ("owner",   "Full administrative access",                1, 0),
            ("admin",   "Subset of administrative commands",         1, 0),
            ("blocked", "Users denied command dispatch entirely",    1, 0),
            ("public",  "Commands runnable by anyone (not blocked)", 1, 1),
            ("user",    "Regular users with non-admin commands",     0, 0),
        ]
        for name, desc, is_system, all_users in defaults:
            await self.db.execute(
                "INSERT INTO bot_groups(name, description, is_system, created_at, all_users) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, is_system=excluded.is_system",
                (name, desc, is_system, now, all_users),
            )
        # owner group always has the '*' grant
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_group_commands(group_name, command) "
            "VALUES('owner', '*')",
        )
        # seed owners from config
        for pk in self.cfg.owner_pubkeys:
            row = await self.db.fetchone(
                "SELECT adv_name FROM contacts WHERE public_key=?", (pk,)
            )
            name = row["adv_name"] if row else None
            await self.db.execute(
                "INSERT INTO bot_users(pubkey, name, added_by, added_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(pubkey) DO UPDATE SET "
                "name=COALESCE(excluded.name, bot_users.name)",
                (pk, name, "config", now),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO bot_user_groups(pubkey, group_name) "
                "VALUES(?, 'owner')",
                (pk,),
            )
        if self.cfg.owner_pubkeys:
            self.logger.info(
                "admin bootstrap: %d owner(s) from config",
                len(self.cfg.owner_pubkeys),
            )

    # send primitives (shared by command replies and manual sends)
    async def send_dm_to(self, pubkey: str, text: str, disp_name: str = ""):
        # send a DM to a pubkey with the configured retry/ACK behavior.
        # returns the final MSG_SENT Event, or None if never ACKed. logs
        # delivery outcome; raising is left to the caller's try/except."""
        pk = pubkey.lower()
        to_disp = disp_name or pk[:12]
        snippet = (text[:60] + "…") if len(text) > 60 else text
        self.logger.info("sending DM to=%s: %r", to_disp, snippet)
        contact = None
        lib_contacts = getattr(self.mc, "contacts", None)
        if isinstance(lib_contacts, dict):
            contact = lib_contacts.get(pk)
        t0 = time.monotonic()
        ev = await self.mc.commands.send_msg_with_retry(
            contact or pk, text,
            max_attempts=self.cfg.dm_max_attempts,
            max_flood_attempts=self.cfg.dm_max_flood_attempts,
            flood_after=self.cfg.dm_flood_after,
        )
        dt_ms = int((time.monotonic() - t0) * 1000)
        if ev is None:
            self.logger.warning(
                "DM not ACKed to=%s after %d attempts (%dms)",
                to_disp, self.cfg.dm_max_attempts, dt_ms,
            )
        elif getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "DM send rejected to=%s: code=%s reason=%s",
                to_disp,
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        else:
            self.logger.info("DM ACKed to=%s in %dms", to_disp, dt_ms)
        return ev

    async def remove_contact_remote(self, pubkey: str):
        self.logger.info("removing contact pk=%s from radio", pubkey[:12])
        ev = await self.mc.commands.remove_contact(pubkey)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "remove_contact rejected pk=%s: code=%s reason=%s",
                pubkey[:12],
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        return ev

    async def send_advert(self, flood: bool):
        label = "flood" if flood else "zero-hop"
        self.logger.info("sending %s advert", label)
        ev = await self.mc.commands.send_advert(flood=flood)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "advert send rejected: code=%s reason=%s",
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        return ev

    async def send_channel_text(self, channel_idx: int, text: str):
        # single-shot channel send (channel messages have no ACK). returns
        # the radio Event (or None); logs a rejection. no exception on error.
        snippet = (text[:60] + "…") if len(text) > 60 else text
        self.logger.info(
            "sending channel msg ch=%d: %r", channel_idx, snippet
        )
        ev = await self.mc.commands.send_chan_msg(channel_idx, text)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "channel send rejected: code=%s reason=%s",
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        return ev

    # reply
    async def send_reply(self, ctx: CommandContext, text: str) -> None:
        try:
            if ctx.is_dm:
                pk = ctx.sender_pubkey
                if not pk and ctx.sender_pubkey_prefix:
                    pk, _ = await self.resolve_prefix(
                        ctx.sender_pubkey_prefix
                    )
                if not pk:
                    self.logger.warning(
                        "cannot DM-reply: unresolved sender"
                    )
                    return
                await self.send_dm_to(pk, text, ctx.sender_name or "")
            else:
                if ctx.channel_idx is None:
                    return
                await self.send_channel_text(ctx.channel_idx, text)
        except Exception:
            self.logger.exception("send_reply failed")

    # retention helpers
    async def _trim_global(self, table: str, keep: int) -> None:
        if keep <= 0:
            return
        await self.db.execute(
            f"DELETE FROM {table} WHERE id NOT IN "
            f"(SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    async def _trim_channel_messages(
        self, channel_idx: Optional[int], keep: int
    ) -> None:
        if keep <= 0 or channel_idx is None:
            return
        await self.db.execute(
            "DELETE FROM channel_messages WHERE channel_idx=? AND id NOT IN "
            "(SELECT id FROM channel_messages WHERE channel_idx=? "
            "ORDER BY id DESC LIMIT ?)",
            (channel_idx, channel_idx, keep),
        )

    # lifecycle
    async def run(self) -> int:
        self._parse_log_channels()
        self.loader.load_all()

        self.logger.info("=" * 60)
        self.logger.info("mcbot starting")
        self.logger.info(
            "config: radio=%s db=%s logs_dir=%s commands_dir=%s",
            self.cfg.target_desc(), self.cfg.db_path,
            self.cfg.logs_dir, self.cfg.commands_dir,
        )
        self.logger.info(
            "retention: chan_msgs=%d dms=%d packets=%d",
            self.cfg.max_channel_messages,
            self.cfg.max_dms,
            self.cfg.max_packets,
        )
        self.logger.info(
            "log_channels=%s commands_enabled=%s rx_log_decrypt=%s",
            self.cfg.log_channels, self.cfg.commands_enabled,
            self.cfg.rx_log_decrypt,
        )
        self.logger.info("=" * 60)

        try:
            if self.cfg.transport == "serial":
                try:
                    self.mc = await MeshCore.create_serial(
                        self.cfg.serial_port,
                        baudrate=self.cfg.serial_baud,
                        debug=self.cfg.debug,
                        auto_reconnect=self.cfg.auto_reconnect,
                    )
                except TypeError:
                    self.mc = await MeshCore.create_serial(
                        self.cfg.serial_port, self.cfg.serial_baud
                    )
            else:
                try:
                    self.mc = await MeshCore.create_tcp(
                        self.cfg.host, self.cfg.port,
                        debug=self.cfg.debug,
                        auto_reconnect=self.cfg.auto_reconnect,
                    )
                except TypeError:
                    self.mc = await MeshCore.create_tcp(
                        self.cfg.host, self.cfg.port
                    )
        except Exception:
            self.logger.exception("connect failed (%s)", self.cfg.target_desc())
            self.db.close()
            return 1

        self.logger.info("connected to %s", self.cfg.target_desc())

        # enable client-side channel decryption so messages from RX_LOG packets
        # can have their text decoded once channels are populated in the parser.
        try:
            if hasattr(self.mc, "set_decrypt_channel_logs"):
                self.mc.set_decrypt_channel_logs(True)
        except Exception:
            self.logger.exception("set_decrypt_channel_logs failed")

        if self.cfg.device_pin:
            try:
                await self.mc.commands.set_devicepin(int(self.cfg.device_pin))
                self.logger.info("device PIN set")
            except Exception:
                self.logger.exception("device PIN set failed")

        await self.sync_device_info()
        self._cache_identity()
        await self.sync_contacts()
        # seed conf channels into an empty table before syncing radio-reported
        # channels, otherwise the radio's built-in Public channel makes the
        # table non-empty and suppresses the one-time conf seed.
        await self._seed_channels_from_conf()
        await self.sync_channels()
        await self._log_identity()
        ok = await self._setup_private_key()
        if not ok:
            self.logger.warning(
                "client-side DM decryption disabled — DMs received via "
                "RX_LOG_DATA will not be decoded"
            )
        await self._load_channels()
        await self._program_channels_on_radio()
        await self._bootstrap_admin_state()
        await self.seed_command_configs()

        # subscriptions
        try:
            self._subs.append(self.mc.subscribe(None, self._firehose))
            self._subs.append(
                self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
            )
            self._subs.append(
                self.mc.subscribe(
                    EventType.CHANNEL_MSG_RECV, self._on_channel_msg
                )
            )
            if hasattr(EventType, "ADVERTISEMENT"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.ADVERTISEMENT, self._on_advertisement
                    )
                )
            # the firehose (subscribed to all events above) records raw_hex
            # for the packet monitor regardless, this extra subscription only
            # adds the client-side decrypt+ingest of DMs/channel msgs, gated
            # by [bot] rx_log_decrypt.
            if self.cfg.rx_log_decrypt and hasattr(EventType, "RX_LOG_DATA"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.RX_LOG_DATA, self._on_rx_log_data
                    )
                )
            if hasattr(EventType, "NEW_CONTACT"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.NEW_CONTACT, self._on_new_contact
                    )
                )
            if hasattr(EventType, "MESSAGES_WAITING"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.MESSAGES_WAITING, self._on_messages_waiting
                    )
                )
            if hasattr(EventType, "CONNECTED"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.CONNECTED, self._on_connected
                    )
                )
            if hasattr(EventType, "DISCONNECTED"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.DISCONNECTED, self._on_disconnected
                    )
                )
        except Exception:
            self.logger.exception("subscribing to events failed")

        try:
            await self.mc.start_auto_message_fetching()
        except Exception:
            self.logger.exception("start_auto_message_fetching failed")

        async def periodic_contacts():
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=60.0
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                if self._contacts_dirty:
                    self._contacts_dirty = False
                    try:
                        await self.sync_contacts()
                    except Exception:
                        self.logger.exception("periodic contacts sync failed")

        periodic_task = asyncio.create_task(periodic_contacts())

        self._start_web()

        self.logger.info("bot running; press Ctrl-C to stop")
        try:
            await self.stop_event.wait()
        finally:
            await self.shutdown([periodic_task])
        return 0

    def _start_web(self) -> None:
        # start the in-process web admin UI/API as an asyncio task, if
        # enabled and configured. Failures here never abort the bot.
        if not self.cfg.web_enabled:
            return
        if not self.cfg.web_session_secret:
            self.logger.error(
                "web: [web] enabled but session_secret is unset — "
                "web UI/API NOT started"
            )
            return
        try:
            from webapi.app import make_server
            self._web_server = make_server(self)
            self._web_task = asyncio.create_task(self._web_server.serve())
            scheme = "https" if self.cfg.web_tls_cert else "http"
            self.logger.info(
                "web admin UI/API on %s://%s:%d (docs at /api/docs)",
                scheme, self.cfg.web_host, self.cfg.web_port,
            )
        except Exception:
            self.logger.exception("web server failed to start")
            self._web_server = None
            self._web_task = None

    async def shutdown(self, tasks=None) -> None:
        self.logger.info(
            "shutdown initiated; events_seen=%d", self.event_count
        )
        # stop the web server first so it isn't serving against a tearing-down
        # bot. should_exit makes uvicorn's serve() task return promptly.
        if self._web_server is not None:
            self._web_server.should_exit = True
            if self._web_task is not None:
                try:
                    await asyncio.wait_for(self._web_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._web_task.cancel()
                except Exception:
                    self.logger.exception("web server shutdown error")
            self._web_server = None
            self._web_task = None
        for t in (tasks or []):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                self.logger.exception("background task cleanup failed")
        if self.mc:
            try:
                await self.mc.stop_auto_message_fetching()
            except Exception:
                pass
            for sub in self._subs:
                try:
                    self.mc.unsubscribe(sub)
                except Exception:
                    pass
            try:
                await self.mc.disconnect()
            except Exception:
                pass
        self.db.close()
        self.logger.info("shutdown complete")


# ---------------------------------------------------------------------------
# Entry point
#
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="MeshCore companion-radio bot over TCP."
    )
    p.add_argument(
        "--config", type=Path,
        help="Path to mcbot.conf (default: ./mcbot.conf if present)",
    )
    p.add_argument(
        "--transport", choices=["tcp", "serial"],
        help="Connection transport (default tcp)",
    )
    p.add_argument("--host", help="Radio TCP host")
    p.add_argument("--port", type=int, help="Radio TCP port")
    p.add_argument(
        "--serial-port",
        help="Serial device path (e.g. /dev/ttyACM0 or /dev/serial/by-id/...)",
    )
    p.add_argument(
        "--serial-baud", type=int, help="Serial baud rate (default 115200)",
    )
    p.add_argument("--device-pin", help="Optional device PIN")
    p.add_argument("--db", help="SQLite database path")
    p.add_argument("--logs-dir", help="Directory for log files")
    p.add_argument("--log-level", help="Python log level (DEBUG/INFO/...)")
    p.add_argument("--commands-dir", help="Directory of command scripts")
    p.add_argument(
        "--privkey-path",
        help="Path to exported radio private key (default: <db>.privkey)",
    )
    p.add_argument(
        "--dm-max-attempts", type=int,
        help="Max DM retry attempts (default 3)",
    )
    p.add_argument(
        "--dm-flood-after", type=int,
        help="Switch to flood after N direct attempts (default 2)",
    )
    p.add_argument(
        "--dm-max-flood-attempts", type=int,
        help="Max flood-mode retry attempts (default 2)",
    )
    p.add_argument(
        "--log-channels",
        help='Channels to log to channel_messages: "all" or comma list of '
             "channel names/indexes",
    )
    p.add_argument(
        "--max-channel-messages", type=int,
        help="Per-channel retention cap",
    )
    p.add_argument("--max-dms", type=int, help="DM retention cap")
    p.add_argument(
        "--max-contacts", type=int,
        help="contacts retention cap (roll off oldest, default 500)",
    )
    p.add_argument(
        "--max-packets", type=int,
        help="received_packets retention cap",
    )
    p.add_argument(
        "--disable-commands", action="store_true",
        help="Sync/log only; skip command dispatch",
    )
    p.add_argument(
        "--no-auto-reconnect", action="store_true",
        help="Disable TCP auto-reconnect",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Verbose meshcore library logging",
    )
    p.add_argument(
        "--hash-password", action="store_true",
        help="Prompt for a password and print its hash for [web] "
             "admin_password_hash, then exit",
    )
    return p.parse_args(argv)


async def amain(argv=None) -> int:
    # outer loop so '!adm restart' can fully tear down and rebuild without
    # exiting the process. on normal shutdown (Ctrl-C, SIGTERM) we exit
    # after the first iteration.
    while True:
        args = parse_args(argv)
        cfg = load_config(args)
        log = setup_logging(cfg)
        bot = MCBot(cfg, log)

        loop = asyncio.get_running_loop()

        def _signal_handler():
            log.info("signal received, stopping")
            bot.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                pass

        rc = await bot.run()
        if not bot.restart_requested:
            return rc
        log.info("=" * 60)
        log.info("RESTART: reinitializing from fresh config")
        log.info("=" * 60)


def _hash_password_cli() -> int:
    import getpass
    from webapi.auth import hash_password
    pw = getpass.getpass("New web admin password: ")
    if pw != getpass.getpass("Confirm: "):
        sys.stderr.write("passwords did not match\n")
        return 1
    if not pw:
        sys.stderr.write("empty password\n")
        return 1
    print(hash_password(pw))
    return 0


def main() -> int:
    # handle the offline password-hash helper before touching the event loop.
    if "--hash-password" in (sys.argv[1:]):
        return _hash_password_cli()
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)
