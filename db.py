"""sqlite schema and the lock-guarded async DB wrapper for mcbot."""

import asyncio
import logging
import sqlite3
from pathlib import Path

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

-- Named SQL snippets saved from the web Database console.
CREATE TABLE IF NOT EXISTS saved_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    query TEXT NOT NULL,
    created_at INTEGER,
    updated_at INTEGER
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
    dm_only INTEGER,
    process_queued INTEGER        -- NULL = script default; run commands
                                  -- fetched from the radio's offline backlog?
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
        self._migrate()
        self.conn.commit()
        self.lock = asyncio.Lock()

    def _migrate(self) -> None:
        # additive upgrades for pre-existing databases — CREATE TABLE IF NOT
        # EXISTS never alters a live table, so new columns must land here.
        cols = {
            r[1] for r in self.conn.execute(
                "PRAGMA table_info(command_config)"
            ).fetchall()
        }
        if "process_queued" not in cols:
            self.conn.execute(
                "ALTER TABLE command_config ADD COLUMN process_queued INTEGER"
            )

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

    async def run_raw(self, sql: str) -> dict:
        """Execute one arbitrary SQL statement for the web Database console.
        sqlite3 permits only a single statement per call, which conveniently
        blocks ';'-chained injection. Returns column names + rows for a result
        set (BLOBs hex-encoded so the payload is JSON-safe), else the affected
        rowcount (-1 for statements like DDL that report none)."""
        async with self.lock:
            cur = self.conn.execute(sql)
            if cur.description is not None:
                columns = [d[0] for d in cur.description]
                rows = [
                    [v.hex() if isinstance(v, (bytes, bytearray)) else v
                     for v in r]
                    for r in cur.fetchall()
                ]
                self.conn.commit()
                return {"columns": columns, "rows": rows, "rowcount": len(rows)}
            rowcount = cur.rowcount
            self.conn.commit()
            return {"columns": [], "rows": [], "rowcount": rowcount}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
