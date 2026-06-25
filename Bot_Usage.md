# mcbot — Usage and Architecture

This is the detailed guide. See [`README.md`](README.md) for a high-level
overview.

## Contents

1. [Architecture overview](#1-architecture-overview)
2. [First-run setup](#2-first-run-setup)
3. [Configuration reference](#3-configuration-reference)
4. [Running the bot](#4-running-the-bot)
5. [Day-to-day management](#5-day-to-day-management)
6. [Authorization model](#6-authorization-model)
7. [Database schema](#7-database-schema)
8. [Plugin command development](#8-plugin-command-development)
9. [Command reference](#9-command-reference)
10. [Web admin UI / API](#10-web-admin-ui--api)

---

## 1. Architecture Overview

mcbot is a single-file Python `asyncio` application built on top of the
`meshcore` (a.k.a. `meshcore_py`) library. The high-level data flow:

```
radio link — TCP (WiFi) or USB-serial
    ↓
meshcore_py event dispatcher
    ↓
firehose handler  ────────────→ received_packets table + one log line per event
RX_LOG_DATA handler  ─────────→ client-side decrypt (DM/channel) → ingest_*
CONTACT_MSG_RECV handler  ────→ ingest_dm  (radio-queued DM path)
CHANNEL_MSG_RECV handler  ────→ ingest_channel_msg
ADVERTISEMENT / ACK / ...     → additional handlers (contacts re-sync, log)

ingest_dm / ingest_channel_msg
    ↓
direct_messages / channel_messages tables (capped circular buffers)
    ↓
dispatch_command  →  enabled / dm_only / allow_dm / channel filter /
                     block-list / authorization / cooldown gates
    ↓
plugin handle(ctx)
    ↓
str | list[str] | None  →  send_reply  →  send_msg_with_retry
```

### Why two ingest paths?

MeshCore companion firmware (including the WiFi-patched variant this
bot was developed against) exposes inbound messages two ways
simultaneously:

- **`CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV` path** — the radio queues
  decoded inbound messages and signals `MESSAGES_WAITING`. The lib's
  auto-fetcher (`start_auto_message_fetching`) calls `get_msg()` in
  response, and the already-decrypted message arrives as an event.
- **`RX_LOG_DATA` decrypt path** — the radio also passes through raw
  RF observations (the on-air encrypted packet) as a separate event.
  mcbot parses the envelope and runs X25519 ECDH (DMs) or
  HMAC-checked AES (channels) locally using known keys.

Because mcbot programs every configured channel secret into the radio's
slots (`_program_channels_on_radio` via `set_channel`), the radio decrypts
**both** DMs and channel messages itself and delivers them via the queued
path — so on this firmware the same logical message arrives via *both*
paths. The ingest funnel dedupes: DMs by `(sender_pubkey,
sender_timestamp)`, channel messages by `(channel_idx, sender_timestamp,
text)`. Whichever path arrives first stores and dispatches; the other is
silently suppressed. Sender-side retries (which bump the attempt counter
and change the ciphertext / pkt_hash) are caught by the same keys.

`GET_CHANNEL` returns empty for all slots on this firmware, so the radio
won't *report* its channel secrets back — but that only means mcbot can't
auto-discover them, not that it can't receive channel messages. Channel
keys are operator-managed via `mcbot.conf [channels]` (first-run seed) and
`!adm channel add/remove` (runtime), and pushed into the radio from there.

So the client-side `RX_LOG_DATA` decrypt path is **redundant** for message
capture on message-queueing firmware (and is deduped away). It is always on
because it is the only source of raw on-air bytes for the packet monitor and
routing paths, it observes traffic the radio doesn't decrypt for us, and it
still decodes messages on firmware that doesn't queue (older versions, or
non-companion roles like repeater/room-server) and on channels not programmed
into the radio (e.g. beyond its slot capacity).

### Client-side decryption

**DMs** — On first launch, mcbot calls `mc.commands.export_private_key()`
to retrieve the radio's 64-byte Ed25519 private key, then writes it to
`mcbot.privkey` (mode 0600). Subsequent runs load it from disk. Each
inbound DM RF packet is matched against contacts by the 1-byte sender
hash; for each candidate the bot derives the X25519 ECDH shared secret
and attempts AES-128-ECB decryption with HMAC verification. Only the
sender whose pubkey actually matches will MAC-verify successfully.

**Channel messages** — The operator configures channel secrets in
`mcbot.conf [channels]` or at runtime via `!adm channel add`. On each
inbound channel RF packet, the 1-byte channel hash is matched against
the configured secret table, then HMAC + AES decryption confirms the
match.

### Plugin command system

Each command is a separate Python file in `commands/`. At startup
`CommandLoader.load_all()` scans the directory, imports each file via
`importlib`, validates that it exposes `NAME`, `TRIGGERS`, and an
`async def handle(ctx)`, and registers a `CommandSpec` per file.

On first load each command's script-level defaults are seeded into the
`command_config` SQLite table. After that, the dispatcher reads
`command_config` fresh on every invocation — operator edits in the
DB take effect immediately, no reload required.

`!adm reload` re-scans the directory and re-imports — pulls in newly
created or edited plugin files without restarting the bot.

### Database

SQLite at `mcbot.db`, opened in WAL mode for concurrent reads while a
writer holds the lock. All writes go through an `asyncio.Lock` so
async tasks serialize cleanly. See §7 for the schema.

---

## 2. First-Run Setup

### Prerequisites

- Python 3.12+
- A MeshCore companion radio, reachable either over TCP (WiFi) at a
  host:port or over USB-serial at a device path
- Your owner client/radio's full 64-hex public key

### Install

```bash
git clone <your-source>  mcbot
cd mcbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

Open `mcbot.conf` and set the radio connection plus your owner key. Pick
**one** transport.

Over **TCP (WiFi)** — the default:

```ini
[radio]
transport = tcp
host = 192.168.1.100
port = 5000

[bot]
owner_pubkeys = <your 64-hex pubkey from your owner client>
```

…or over **USB-serial** — set `transport = serial` and a device path
(no host/port needed):

```ini
[radio]
transport = serial
serial_port = /dev/serial/by-id/usb-...   ; or /dev/ttyACM0
serial_baud = 115200

[bot]
owner_pubkeys = <your 64-hex pubkey from your owner client>
```

Serial is a single exclusive link — you can't also use TCP (WiFi) to the
same radio at the same time. `pyserial` ships as a dependency of
`meshcore`, so no extra install is needed. See §3 `[radio]` for the full
option list and the USB-serial device-path / `dialout` permission notes.

Optionally add channels:

```ini
[channels]
0 = Public:8b3387e9c5cdea6ac9e5edbaa115cd72
1 = #bot
2 = #testing
3 = MyPrivateChan:a5e907b6ce4030df22a6b2df54f79ce4
```

After first run, channel state is stored in the `channels` DB table —
the `[channels]` section is consulted only when the table is empty.

### First launch

```bash
./mcbot.py
```

mcbot will:
1. Open `mcbot.db`, creating the schema on first run
2. Connect to the radio (TCP or USB-serial, per `[radio] transport`)
3. Sync contacts from the radio
4. Read or export the radio's private key
5. Sync radio-stored channels (usually empty on repeater firmware)
6. Cache the radio's identity (name, pubkey, firmware version, radio params)
7. Seed channel definitions from `mcbot.conf` to the DB (first run only)
8. Program channel slots on the radio
9. Create system groups (owner / admin / public / blocked / user)
10. Add `owner_pubkeys` from conf to the `owner` group
11. Seed `command_config` rows from each plugin's defaults
12. Subscribe to events and start the message loop

Look for these log lines:
```
firmware: model=... ver=... build=...
identity: name=... pubkey=...
admin bootstrap: N owner(s) from config
seeded command_config for 'foo' from script defaults (one per command on first run)
bot running; press Ctrl-C to stop
```

### Verify

DM `!whoami`. Expected (the pubkey is abbreviated first6...last6 so the
reply fits one message):
```
name: <your name> | pubkey: 643848...ccd6e9 | groups: owner
```

Then `!help` to list the commands available to you (`!help adm` for the
admin subcommand reference).

---

## 3. Configuration Reference

`mcbot.conf` uses INI syntax (`configparser`). CLI flags override conf
values where applicable. Unrecognized sections/keys (typos, or settings
removed in an upgrade) are ignored, but each is logged loudly as a
`WARNING` at startup (e.g. `mcbot.conf: [bot] unrecognized key 'foo'
(ignored)`). The `[env]` and `[channels]` sections accept arbitrary keys
and are not checked.

### `[radio]`

| Key | Default | Description |
|-----|---------|-------------|
| `transport` | `tcp` | `tcp` or `serial`. If `serial_port` is set and no `host`, serial is inferred. |
| `host` | (required for tcp) | Radio TCP host/IP |
| `port` | 4000 | Radio TCP port (typical: 5000) |
| `serial_port` | (required for serial) | Serial device path, e.g. `/dev/ttyACM0` or a stable `/dev/serial/by-id/usb-...` |
| `serial_baud` | 115200 | Serial baud rate |
| `device_pin` | (empty) | Optional radio PIN |

CLI: `--transport {tcp,serial}`, `--host`, `--port`, `--serial-port`, `--serial-baud`, `--device-pin`

**USB-serial notes**:
- Heltec V3 (ESP32-S3) usually enumerates as `/dev/ttyACM0` or `/dev/ttyUSB0`
  depending on its USB-serial chip. Those numbers can change across
  reboot/replug — prefer the stable `/dev/serial/by-id/usb-...` symlink.
- The bot's user might need to be in the `dialout` group (Linux) to open `/dev/tty*`,
  to prevent getting a permission error.
- MeshCore companion serial runs at 115200 baud.
- Serial is a single exclusive connection — you can't run TCP (WiFi) and
  serial to the same radio at once. Choosing serial means the WiFi-companion
  link isn't used.

### `[storage]`

| Key | Default | Description |
|-----|---------|-------------|
| `db` | `./mcbot.db` | SQLite path |
| `max_channel_messages` | 1000 | Per-channel msg retention |
| `max_dms` | 1000 | DM retention |
| `max_contacts` | 500 | contacts retention; rolls off oldest by `last_synced_at` |
| `max_packets` | 1000 | received_packets firehose cap |

CLI: `--db`, `--max-channel-messages`, `--max-dms`, `--max-contacts`, `--max-packets`

### `[channel_logging]`

| Key | Default | Description |
|-----|---------|-------------|
| `channels` | `all` | Either `all` or a comma list of channel names/indexes whose messages get **stored** in `channel_messages` (separate from the key-list in `[channels]`) |

CLI: `--log-channels`

This filter controls **storage only** — it does NOT gate command dispatch.
A channel that the bot can decrypt (it has a key in `[channels]` / the
channels table) will dispatch commands per each command's `allowed_channels`,
even if the channel isn't in `[channel_logging] channels`. Use
`[channel_logging]` to limit which channels' chatter fills `channel_messages`;
use a command's `allowed_channels` (via `!adm command channel add/remove`) to
control where that command responds.

### `[logging]`

| Key | Default | Description |
|-----|---------|-------------|
| `logs_dir` | `./logs` | Log file directory |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, ... |

CLI: `--logs-dir`, `--log-level`, `--debug` (sets meshcore lib to DEBUG)

### `[bot]`

| Key | Default | Description |
|-----|---------|-------------|
| `commands_dir` | `./commands` | Plugin directory |
| `enabled` | `true` | If false, dispatch is suspended (sync/log only) |
| `privkey_path` | `<db>.privkey` | Where to cache the radio's private key |
| `owner_pubkeys` | (none) | Comma list of 64-hex pubkeys bootstrapped into the `owner` group at startup |
| `dm_max_attempts` | 3 | `send_msg_with_retry` max attempts |
| `dm_flood_after` | 2 | Switch to flood routing after N direct attempts |
| `dm_max_flood_attempts` | 2 | Max flood-mode retries |

CLI: `--commands-dir`, `--privkey-path`, `--disable-commands`,
`--dm-max-attempts`, `--dm-flood-after`, `--dm-max-flood-attempts`,
`--no-auto-reconnect`

### `[channels]`

```ini
[channels]
<idx> = <name>[:<hex_key>]
```

- `<idx>` is the radio's channel slot (0..39)
- For `#`-prefixed names, omit the key — it's auto-derived as `SHA256(name)[:16]`
- For non-`#` names, supply the 16-byte (32 hex char) key after `:`
- Used only as a first-run seed; after that the `channels` table is
  authoritative (manage via `!adm channel add/remove`)

To re-seed from conf: `sqlite3 mcbot.db "DELETE FROM channels;"` then
restart.

---

## 4. Running the Bot

### Foreground

```bash
./mcbot.py
```

Stops cleanly on Ctrl-C (SIGINT) or `kill -TERM <pid>`.

### systemd unit (example)

```ini
[Unit]
Description=mcbot
After=network.target

[Service]
Type=simple
User=steve
WorkingDirectory=/home/steve/dev/meshcore/meshcore-bot
ExecStart=/home/steve/dev/meshcore/meshcore-bot/venv/bin/python mcbot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Restart matrix

| What changed | How to apply |
|--------------|--------------|
| `commands/*.py` (new or edited plugin) | DM `!adm reload` |
| `mcbot.conf` | DM `!adm restart` (re-reads conf, re-opens DB, reconnects) |
| `command_config` table edit | Nothing — read fresh on each dispatch |
| `mcbot.py` itself | Kill process, restart from shell |
| Radio firmware upgrade | Kill process (radio reboots), restart |

`!adm restart` schedules teardown ~5 seconds after the acknowledgment
reply so the ACK has time to be sent and confirmed before the disconnect.

---

## 5. Day-to-Day Management

### Adding a new command

1. `cp example-bot-command.py commands/mycmd.py`
2. Edit it (`NAME`, `TRIGGERS`, `handle()`)
3. DM `!adm reload`
4. DM `!mycmd` to test

The reload also seeds a `command_config` row from the script defaults.

### Tuning a command at runtime

Each command has a row in `command_config`. The dispatcher reads it on
every invocation, so these take effect immediately:

```sql
-- via sqlite3 mcbot.db
UPDATE command_config SET cooldown_seconds=60 WHERE command='wx';
UPDATE command_config SET dm_only=1 WHERE command='mycmd';
UPDATE command_config SET enabled=0 WHERE command='example';
UPDATE command_config SET allowed_channels='["#bot","#testing"]' WHERE command='path';
```

`allowed_channels` semantics:
- `NULL` → fall back to script default
- Empty JSON array `[]` → "no restriction" (override script)
- Non-empty JSON array `["#bot",...]` → restriction list

Note: **authorization is not in `command_config`** — who may run a
command is controlled by group grants (`!adm group grant/revoke`, see §6),
not by an edit here.

To re-seed a row from script defaults: `DELETE FROM command_config WHERE
command='X';` then `!adm reload`. (This does not change grants.)

### Managing channels at runtime

| Op | Command |
|----|---------|
| List | `!adm channel list` |
| Add (#-channel) | `!adm channel add #foo` |
| Add (custom key) | `!adm channel add Custom 8b3387e1...0` |
| Remove | `!adm channel remove #foo` |

`!adm channel add` allocates the lowest unused slot 0..39, programs the
radio via `set_channel`, writes the DB row, and updates in-memory
caches so inbound messages on that channel decrypt immediately.
`remove` clears the radio slot (empty name + zero key), deletes the DB
row, and removes the in-memory entry.

### Managing users and groups

See §9 for full syntax. Common flow:

```
!adm group add wxbot_users pws,wx
!adm user add Alice wxbot_users
!adm group grant admin contacts
!adm user add Bob admin
!adm user block Mallory
```

### Inspecting state

```
!whoami               your identity + groups (its own command)
!help                 commands you can run (DM-only; '!help adm' for admin)
!adm status           bot health snapshot
!adm command list     loaded plugins + their config
!adm user list
!adm group list
!adm channel list
!adm contacts search <string>
!adm log [N]          last N audit entries (default 10)
```

### Log file

`logs/mcbot.log` — 10 MB rotating × 5 backups. Useful patterns:

```bash
tail -f logs/mcbot.log
grep "running command" logs/mcbot.log    # command invocations
grep "DM ACKed\|DM not ACKed" logs/mcbot.log    # outbound DM delivery
grep "DM duplicate suppressed" logs/mcbot.log   # cross-path dedupes
```

### Database direct access

```bash
sqlite3 mcbot.db
> .tables
> .schema bot_audit_log
> SELECT * FROM bot_audit_log ORDER BY id DESC LIMIT 20;
> SELECT command, cooldown_seconds, dm_only FROM command_config;
```

---

## 6. Authorization Model

### Identifiers

- A **user** is identified by a 64-hex Ed25519 public key.
- A **group** is a named bag of allowed command names (the literal `*`
  matches all commands).
- Membership is many-to-many. A group can also be flagged **all-users**
  (the `*` member): every user is then implicitly a member of it.

### Allow rules

Authorization is **groups-only and fail-closed**: a command runs only if
it has been granted to a group the caller belongs to. There is no
per-command "skip auth" flag — the command name *is* the permission.

A sender is authorized for command `X` if **either** of:
1. An **all-users** group (the `*` member — everyone belongs) grants `X`
   or `*`. This is how a command is made **open to everyone**: grant it to
   `public`, which is an all-users group by default. It also works for
   senders whose pubkey couldn't be resolved, e.g. some channel messages.
2. The sender's pubkey is an explicit member of any group whose command
   list contains `X` or `*`. Owners hold `*`, so they can run anything.

If neither holds, the command is **denied** (fail-closed). A freshly
added command is therefore runnable only by owners until you grant it —
see "Making a command available" below.

The **block check** runs first: anyone in the `blocked` group is silently
denied any command. This applies regardless of group memberships above.

### All-users groups (the `*` member)

A group flagged all-users includes **every** user without per-user
membership rows. `public` is seeded this way, so `public`'s command
grants are open to all and `public` shows up in everyone's `!whoami`.

```
!adm group allow-all <name>     # add the '*' member (all users)
!adm group restrict <name>      # remove it (explicit members only)
```

`public` defaults to all-users but you can `!adm group restrict public`
to turn it off, or flag any other group all-users. `blocked` may not be
made all-users (a block is always explicit). The web UI Groups tab has an
"all users" checkbox per group.

### System groups

| Group | Seeded with | Editable? | Deletable? |
|-------|-------------|-----------|------------|
| `owner` | `*` grant | Add/remove members + grants | No (system) |
| `admin` | (empty) | Yes | No (system) |
| `public` | all-users (`*` member) | Yes — grant commands to make them open to all | No (system) |
| `blocked` | (empty) | Add/remove via `!adm user block/unblock` | No (system) |
| `user` | (empty) | Yes | Yes |

### Bootstrap

`[bot] owner_pubkeys` in `mcbot.conf` is read on every startup; each
listed pubkey is upserted into `bot_users` and added to the `owner`
group. This is idempotent and ensures you can't lock yourself out: as
long as your pubkey is in conf, you have owner access after restart.

### Per-command auth gate

The dispatcher always calls `is_authorized_for_command(pubkey, NAME)`
before invoking `handle()`, and denies silently if it returns False. A
command is gated purely by which groups have been granted it — there is
no per-command auth flag.

### Making a command available

Grant the command to a group (via `!adm` or the web UI's Groups tab):

```
!adm group grant public <cmd>      # open to everyone
!adm group grant <group> <cmd>     # limit to members of <group>
!adm group revoke <group> <cmd>    # take it away again
```

Owners (the `owner` group, holding `*`) can always run every command, so
admin-only commands need no explicit grant — just don't grant them to
anyone else.

### Admin commands

`commands/adm.py` declares no special auth flag; the dispatcher gates it
by its own name (`adm`). Since only `owner` holds `*`, `!adm` is
owner-only out of the box. To delegate it, grant `adm` to another group
(`!adm group grant <group> adm`). The general command listing and
identity lookup are their own commands (`!help`, `!whoami`).

### DM_ONLY

A separate plugin-level flag. When `True`, channel invocations are
silently dropped even if `allowed_channels` would otherwise permit them.
Recommended for any command whose authorization decision matters,
because channel-message sender names are *not* cryptographically
authenticated — anyone with the channel key can claim any sender name in
the plaintext prefix. Only DMs bind identity to a private key via ECDH.

---

## 7. Database Schema

Full DDL lives in `mcbot.py`'s `SCHEMA` constant. Summary of key tables:

### `contacts`

Mirrors the radio's contact list. Synced at startup and incrementally
when adverts arrive.

```
public_key TEXT PK     (64-hex)
adv_name, type, flags
out_path, out_path_len, out_path_hash_mode
adv_lat, adv_lon
last_advert, lastmod
first_seen_at, last_synced_at
```

### `channels`

Operator-managed channel-secret list (post-first-run; conf is just the
seed).

```
channel_idx INTEGER PK (0..39)
name TEXT
secret_hex TEXT (32 hex chars = 16 bytes)
last_synced_at
```

### `direct_messages`, `channel_messages`

Capped circular buffers (per `[storage] max_dms` and
`max_channel_messages`).

```
id INTEGER PK AUTOINCREMENT
sender_pubkey_prefix / sender_pubkey / sender_name
text
sender_timestamp, path, path_len, path_hash_mode
txt_type, snr, rssi, recv_time, attempt
received_at
```

### `received_packets`

The firehose. Every dispatched event gets a row, capped by `max_packets`.

```
id, received_at
event_type (raw EventType.name)
packet_type (human-readable: ADVERT / DM / GRPCHAT / ACK / PATH / ...)
sender_pubkey_prefix, sender_pubkey, sender_name
path, path_len, snr, rssi
channel_idx, text
payload_json, attributes_json
```

### `command_config`

```
command TEXT PK
enabled INTEGER (0/1)
cooldown_seconds INTEGER
allowed_channels TEXT (JSON array)
triggers TEXT (JSON array; informational, not used for runtime matching)
description TEXT
allow_dm INTEGER (0/1)
dm_only INTEGER (0/1)
```

Read fresh on every command dispatch.

### Auth tables

```
bot_users(pubkey PK, name, added_by, added_at, notes)
bot_groups(name PK, description, is_system, created_at, all_users)
bot_group_commands(group_name, command, PK(group_name, command))
bot_user_groups(pubkey, group_name, PK(pubkey, group_name))
  -- all_users=1 → every user is an implicit member (the "*" member)
```

### `bot_audit_log`

```
id, ts, actor_pubkey, actor_name, action, target, detail
```

Every state-changing `!adm` op writes a row. Inspect via `!adm log` or
direct SQL.

### `command_cooldowns`

Runtime state, not user-managed:

```
pubkey, command (PK)
last_used_at REAL
```

### `device_info`, `bot_meta`

Key-value tables for cached radio device info and bot internal state
(e.g., last contacts `lastmod` for incremental sync).

---

## 8. Plugin Command Development

The full template with worked examples lives at
`example-bot-command.py`. Minimum viable plugin:

```python
NAME = "hello"
TRIGGERS = ["!hello"]

async def handle(ctx):
    return f"Hello {ctx.sender_name or 'there'}"
```

Drop in `commands/hello.py`, DM `!adm reload`, then grant it — a new
command is owner-only until granted (fail-closed). To use it as an owner,
just DM `!hello`; to open it to everyone, DM `!adm group grant public
hello` first.

### Module attributes

| Attr | Type | Default | Effect |
|------|------|---------|--------|
| `NAME` | str | (required) | Identifier; key in `command_config` |
| `TRIGGERS` | list[str] | (required) | Message prefixes, case-insensitive |
| `DESCRIPTION` | str | `""` | Shown by `!help` / `!adm command list` |
| `COOLDOWN_DEFAULT` | int | 30 | Per-user seconds between invocations |
| `ALLOWED_CHANNELS` | list / None | None | None = any decryptable channel; a list restricts to those |
| `ALLOW_DM` | bool | True | Accept DM invocations |
| `DM_ONLY` | bool | False | Channel invocations silently dropped |

These are seeded into `command_config` on first load. After that, the DB
row is the runtime authority — script-level values are fallbacks when the
corresponding DB column is NULL. (`NAME`/`TRIGGERS` are not
runtime-editable.) Note that **authorization is not a module attribute** —
it lives entirely in group grants (see §6).

#### Optional `!help` integration

These are read by the `!help` command only; they are *not* stored in
`command_config`:

| Attr | Type | Effect |
|------|------|--------|
| `HELP_HIDDEN` | bool | Never list this command in `!help` (e.g. internal admin harnesses) |
| `HELP_DETAIL` | list[str] | Multi-line detail shown by `!help <cmd>`; falls back to `DESCRIPTION` when absent |

`!help` lists a command when it is enabled and the caller would pass its
authorization gate — i.e. the caller could actually run it (the same
groups-only check the dispatcher uses, keyed on the command name).
Commands granted to `public` show to everyone; everything else shows only
to callers in a group that grants it. `!help <cmd>` for a command the
caller can't run returns the same "no such command" as a typo, so the
listing never reveals commands the caller lacks access to.

### `handle(ctx)` return

| Return | Effect |
|--------|--------|
| `str` | Sent as a single reply |
| `list[str]` | Auto-paginated: ~120 chars per DM, 100 per channel |
| `None` | No reply |

Exceptions are caught and logged; they will not kill the bot.

### `ctx` fields

```
ctx.sender_name             adv_name from contacts, or parsed "Name:"
ctx.sender_pubkey           64-hex full pubkey
ctx.sender_pubkey_prefix    12-hex (6-byte) DM envelope prefix
ctx.message_text            the inbound text (channel "Name: " stripped)
ctx.is_dm                   True/False
ctx.channel_idx             channel slot if !is_dm
ctx.channel_name            channel name if !is_dm
ctx.path                    hex path (no commas; use bot.paginate or
                            split by bytes-per-hop yourself if needed)
ctx.path_len, ctx.path_hash_mode
ctx.snr, ctx.rssi
ctx.sender_timestamp        sender's unix-sec at first transmit
ctx.bot                     the MCBot instance (helpers below)
```

### `ctx.bot` helpers

- `ctx.bot.db.fetchone(sql, params)` / `fetchall` / `execute`
- `ctx.bot.logger` (standard `logging.Logger`)
- `ctx.bot.is_authorized_for_command(pubkey, command)`
- `ctx.bot.is_user_blocked(pubkey)`
- `ctx.bot.resolve_target_user(name_or_pubkey)` → `(pubkey, name, err)`
- `ctx.bot.resolve_prefix(prefix_12hex)` → `(pubkey, name)`
- `ctx.bot.audit_log(actor_pubkey, actor_name, action, target, detail)`
- `ctx.bot.paginate(lines, max_chars=120)`
- `ctx.bot.my_pubkey` / `ctx.bot.my_pubkey_byte`
- `ctx.bot.cfg` (the runtime `Config` dataclass)

### Common patterns

- **Blocking I/O**: wrap with `await asyncio.to_thread(sync_fn, ...)`
  so the event loop stays responsive.
- **Arg parsing**: `ctx.message_text` is the full text starting with the
  trigger; `split(maxsplit=N)` is usually enough.
- **DB writes**: prefer `INSERT OR IGNORE` / `ON CONFLICT DO UPDATE` to
  keep handlers idempotent.
- **Long output**: just return a list of strings; the framework
  auto-paginates.

---

## 9. Command Reference

### `!pws`

Returns the current observation from one Weather Underground PWS station
via the weather.com API. You configure two things (see the top of
`commands/pws.py`):

- **Station ID** — set `PWS_STATION_ID` in the environment, or edit
  `_STATION_ID` in `commands/pws.py`.
- **API key** — a weather.com API key: set `PWS_API_KEY` in the
  environment, or edit `_API_KEY` in `commands/pws.py`. Prefer the env var
  — `pws.py` is tracked in git, so a key written into `_API_KEY` would be
  committed.

Until both are set, the command replies with a short "not configured"
hint instead of weather.

Reply (typical — the prefix is your configured station ID):
```
[KXXYYYY1234] Temp: 82F, Humidity: 65%, Rain: 0.0, Wind: SSE 5mph
```

- 30s per-user cooldown
- Works in DM and any allowed channel (a new command is owner-only until
  granted; `!adm group grant public pws` to open it to everyone)

### `!wx <city> [CC]`

Open-Meteo geocoding + forecast lookup. No API key required.

Examples:
```
!wx Austin US
!wx London
!wx Saint Mary
!wx Buenos Aires AR
```

Country code (CC) is optional and must be 2 uppercase letters; if
omitted, the geocoder picks the top match.

Reply:
```
Austin [US] (30.2672,-97.7431): 82F, Humid: 65%, Rain: 0.0in, Wind: SSE 5mph
```

- 30s per-user cooldown
- No auth required
- Default allowed channels: `#wx`, `#wxbot`, `#wx-alert`, `#bot`


### `!path` / `!path k` / `!path <hops>`

Reports the routing path your message took to reach the bot, the routed and
direct distance over the located hops, and a link to a map of the route — all
in one reply. Arguments:

- `k` — kilometers (default is miles).
- An explicit path string (comma-separated hex hops) — reports on that path
  instead of your message's path. Hops must be uniform 1/2/3-byte hex
  (e.g. `d690,abcd,4f3d`).

```
!path                   → @[Alice] [4h] d690,da1c,34de,81bb route: ~8.5mi, direct: ~6.5mi, https://da.gd/abcd
!path k                 → @[Alice] [4h] d690,da1c,34de,81bb route: ~13.7km, direct: ~10.5km, https://da.gd/abcd
!path d690,da1c,81bb    → @[Alice] [3h] d690,da1c,81bb route: ~11.2mi, direct: ~10.7mi, https://da.gd/abcd
!path d690,da1c,81bb k  → @[Alice] [3h] d690,da1c,81bb route: ~18.1km, direct: ~17.2km, https://da.gd/abcd
```

- `[Nh]` is the hop count; the comma list is the per-hop pubkey-prefix hashes.
- **route** = great-circle distance summed between consecutive *located* hops
  (gaps are bridged, so route ≥ direct). **direct** = great-circle distance
  between the first and last located hop. 0.1 resolution. `mi` = miles, `km` = km.
- The map is a [geojson.io](https://geojson.io) link drawing a LineString
  through the located hops plus a labeled marker per hop, shortened via da.gd
  so the reply fits a mesh message. Open it in any browser with internet.
- If some hops can't be located, a `(located/total)` count is shown and the
  distances/map use only the located hops:
  ```
  @[Alice] [4h] d6,37,da,81 route: ~3.1mi, direct: ~3.1mi (2/4), https://da.gd/wxyz
  ```
- One located hop → distance can't be computed, but a map with that single
  pin is still generated: `dist unavailable (1/total), <url>`.
- Zero located hops → `dist/map unavailable (0/total)`; a direct (no-hop)
  message → `direct (no path)`.
- Reliability note: path hashes can collide (several repeaters share the same
  leading byte(s)), so results are most reliable on 2- and 3-byte paths. Hops
  resolve only against repeater/room contacts. When a hash matches more than
  one repeater, the hop is placed only if a candidate is clearly the closest
  fit to a known anchor (the bot's own location, the sender, or an
  unambiguous neighbouring hop); otherwise it is left unlocated rather than
  risk using a far repeater that merely shares the hash. Setting the bot's own
  location on the radio improves this disambiguation.

- 10s per-user cooldown; no auth; works in DM and any allowed channel.
- Makes a da.gd shortener call per invocation (when ≥2 hops are located).

### `!whoami`

Shows your name, abbreviated pubkey, and group memberships in a single
message. Open to anyone; runs in DM and on the channels specified in
ALLOWED_CHANNELS constant (edit with `!adm command channel add/remove whoami
<channel>`).

```
!whoami → name: Alice | pubkey: 643848...ccd6e9 | groups: owner
```

The pubkey is shortened to its first 6 and last 6 hex characters (bridged
with `...`) so the whole reply fits one mesh message.

In a channel the identity is derived from the message's *unauthenticated*
sender prefix; only the DM form cryptographically binds the name to a key.

### `!help` / `!help <cmd>`

Hybrid command. On the allowed channels it only replies with 
`Send !help to me in a DM.` — so the full multi-line listing never
spams a channel. Sent as a DM it lists the commands the caller can actually
run — truly public commands plus anything the caller's group memberships
grant. `!help <cmd>` (in DM) shows detail for one command (e.g. `!help adm`
renders the admin subcommand reference, but only for callers holding the
`adm` permission).

```
!help (channel) → Send !help to me in a DM.
!help (DM)      → Commands you can run:
                    !help — ...
                    !path — ...
                    !pws — ...
                    !whoami — ...
                    !wx — ...
                  Use '!help <cmd>' for detail.
!help adm (DM)  → (owner/admin only) the !adm subcommand table
```

Commands set `HELP_HIDDEN` / `HELP_DETAIL` to tune how they appear here
(see §8).

### `!adm` — administrative commands

DM-only (`DM_ONLY=True`). Every subcommand requires the `adm` permission
(held by the `owner` group via `*`). The command listing and identity
lookup that used to live here are now the separate `!help` and `!whoami`
commands; `!help adm` shows the full subcommand reference.

Each state-changing subcommand writes to `bot_audit_log`. Review with
`!adm log [N]`.

#### User management

```
!adm user list
!adm user show <name|pubkey>
!adm user add <name|pubkey> <group>
!adm user remove <name|pubkey> <group>
!adm user delete <name|pubkey>
!adm user rename <name|pubkey> <new_alias>
!adm user block <name|pubkey>
!adm user unblock <name|pubkey>
```

Name resolution: accepts a contact `adv_name` (exact match; refuses if
ambiguous), a 12-hex prefix, or a full 64-hex pubkey.

Safeguards:
- Refuses to remove or delete the last `owner`
- Refuses `user add ... blocked` (use `user block`)
- Refuses `user add ... public` (`public` is a command-list group, not
  a membership target)
- Refuses to block a user who is in `owner`

#### Group management

```
!adm group list
!adm group show <name>
!adm group add <name> [cmd1,cmd2,...]
!adm group grant <name> <command>
!adm group revoke <name> <command>
!adm group delete <name>
```

Safeguards:
- Refuses to delete a system group (`owner`, `admin`, `public`, `blocked`)
- Refuses to `group revoke owner *`

#### Channel management

```
!adm channel list
!adm channel add <name> [hex_key]
!adm channel remove <name>
```

- `#`-prefixed names: key auto-derived as `SHA256(name)[:16]`; do not
  supply a key (refused)
- Other names: supply the 16-byte key as 32 hex chars
- `add` allocates the lowest unused slot 0..39, programs the radio,
  writes the DB row, updates in-memory caches
- `remove` clears the radio slot, deletes the DB row, drops the
  in-memory entry
- `list` format: `chans: Public,#bot`, paginated across multiple
  messages each prefixed with `chans: ` if too long

#### Contacts

```
!adm contacts search <string>
```

Case-insensitive substring match against `adv_name` or `public_key`.
Returns a count plus the top 3 most-recently-heard matches, each with
name, first 6 hex chars of pubkey, and ISO-format `last_advert` time.

#### Command management

```
!adm command list                          list loaded commands, each with
                                           its auth/dm/channel config
!adm command channel add <cmd> <channel>   add a channel the command will
                                           respond on (turns on the channel
                                           allowlist for that command)
!adm command channel remove <cmd> <channel>  remove a channel; removing the
                                           last one clears the allowlist so
                                           the command responds on any channel
!adm command delay <seconds>               delay before transmitting any
                                           command reply (0 disables, else
                                           0.1–2.0); persists in the database
!adm command retry <count>                 resend a channel reply if no repeater
                                           rebroadcast is heard (0 disables,
                                           max 5); persists in the database
```

`!adm command delay` inserts a fixed pause right before each command response
is transmitted — *after* the handler has finished its lookups/web queries, so
only the radio TX is held back. It exists to test whether nearby repeaters miss
the bot's sends when it replies too quickly. The value is seeded from
`[bot] command_delay` in `mcbot.conf` on first run, then DB-authoritative and
also settable on the web **Manage → Commands** page (changes persist across
restarts).

`!adm command retry` controls how many times a **channel** message the bot sent
is resent when no repeater rebroadcast is heard within `repeat_timeout`. The
resend reuses the original message timestamp so it is byte-identical — MeshCore
keys a channel message by `SHA256(timestamp‖text)`, so nodes that already heard
it de-dupe it and only repeaters that missed it pick it up (no duplicates). It
stops early as soon as a repeat is heard, and each attempt shows as a `RETRY`
row on the web Packets screen. Requires `repeat_tracking = true`; DMs are
excluded (they have ACK-driven retry). Seeded from `[bot] channel_retry_max`
(default 2) on first run, then DB-authoritative and also settable on the web
**Manage → Commands** page.

`!adm command channel add/remove` edits `command_config.allowed_channels`
(a JSON array). Because the dispatcher reads that column fresh on every
invocation, changes take effect immediately — no reload. Note the
allowlist semantics: a command with no listed channels responds on any
channel the bot can decrypt; adding the first channel restricts it to only
the listed channels.

#### Radio settings

```
!adm radio pathhash             show the radio's outgoing path-hash width
!adm radio pathhash <1|2|3>     set it (bytes per hop; mode = bytes-1)
```

IMPORTANT: a radio's path-hash width only governs the encoding of packets
**that radio originates**. The width of a *received* packet's path is set
by whoever sent it, so changing this does **not** improve `!path dist`
accuracy on inbound messages — those are mostly 1-byte because most
senders on the network are on mode 0. This setting only matters if
something downstream analyzes paths of messages the bot sends.

#### Diagnostics

```
!adm status             bot snapshot: radio name, battery, event count,
                        commands loaded, contacts, channels, stored DMs,
                        channel msgs, and the radio's outgoing path-hash width
!adm log [N]            last N audit-log entries (default 10, max 50)
```

#### Lifecycle

```
!adm reload             rescan commands/ directory and re-import plugins
                        also seeds command_config for any newly-added commands
!adm restart            in-process restart: tears down radio + DB, then
                        rebuilds from a fresh config read. Acknowledgment
                        is sent before the disconnect (5s delay). Does NOT
                        pick up changes to mcbot.py itself — that needs a
                        process restart.
```

---

For protocol-level notes (ACK packet format, what a firmware patch
would need to support outbound ACKs), see [`docs/ack-research.md`](docs/ack-research.md).

For the plugin template with worked examples, see
[`example-bot-command.py`](example-bot-command.py).

---

## 10. Web Admin UI / API

An optional in-process web interface and REST/WebSocket API, served by the
bot itself (FastAPI on uvicorn). Disabled by default. It runs in the bot's
asyncio loop, so it has direct access to the live radio connection, the DB,
the packet decoder, and the event stream.

> **Security:** the API has the bot's full authority. Enable only with a
> session secret + hashed admin password + strong API token(s), keep
> `mcbot.conf` out of git, and use TLS (built-in or via a proxy) if it's
> reachable beyond localhost. `host = 0.0.0.0` exposes it on the LAN.

### Enabling

```ini
[web]
enabled = true
host = 0.0.0.0
port = 8080
api_tokens = <long-random-token>
admin_user = admin
admin_password_hash = <output of ./mcbot.py --hash-password>
session_secret = <long-random-string>
;tls_cert = ./web.crt
;tls_key  = ./web.key
```

Generate the password hash:

```bash
./mcbot.py --hash-password
```

Then run the bot. Look for `web admin UI/API on http://… (docs at /api/docs)`.

### Auth

- **Browser:** log in at the UI with `admin_user` / password; the server
  issues a signed session token (cookie + bearer).
- **Apps/scripts:** send `Authorization: Bearer <api_token>`. WebSocket
  clients pass `?token=<token>` (browsers can't set WS headers).
- Interactive REST docs: **`/api/docs`** (OpenAPI at `/api/openapi.json`).

### UI sections

- **Messages** — channel list + live message stream + searchable contacts
  pane. Pick a channel (or click a contact to open a DM thread) and use the
  compose box to send a manually-crafted message. Outgoing messages are
  recorded with an `is_outgoing` flag and echoed back over the live feed.
- **Packets** — live raw-packet table; select a packet to see its hex dump
  with a field-by-field breakout. Hovering a field highlights its bytes and
  vice-versa; `RX_LOG_DATA` packets are decoded down to the decrypted
  channel/DM text when keys are available.
- **Manage** — view and **edit** the tracked DB tables: add/remove channels,
  manage user group membership (add/remove/rename/block/delete), create and
  delete groups and grant/revoke their commands (grant a command to the
  `public` group to make it open to everyone), and toggle per-command
  config (enabled, allow_dm, dm_only, cooldown, allowed_channels).
  Contacts and the audit log are read-only.

### Shared mutation service

Every state change — whether it comes from `!adm` or the web API — runs
through `management.py` (`Management`, reachable as `bot.mgmt`). It owns the
invariants (e.g. "refuse to remove the last owner", "system groups can't be
deleted", channel-key validation) and writes the `bot_audit_log` row, so the
two front-ends can't drift. API-originated changes are audited with the
caller's auth identity (`web:user:…` / `web:token:…`).

### Key endpoints

Reads:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stats` | counts + identity |
| GET | `/api/contacts` `/channels` `/users` `/groups` `/command-config` `/audit` | DB tables |
| GET | `/api/channel-messages` `/direct-messages` | message history |
| GET | `/api/packets`, `/api/packets/{id}` | packet firehose |
| POST | `/api/packets/decode` | break a packet (id or hex) into fields + decode |
| WS | `/api/ws/packets` `/api/ws/messages` | live feeds |

Writes (all audited via `bot.mgmt`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/send/channel` `/api/send/dm` | send a manual message |
| POST/DELETE | `/api/users/{pubkey}/groups[/{group}]` | add / remove group membership |
| PATCH/DELETE | `/api/users/{pubkey}` | rename / delete a user |
| POST/DELETE | `/api/users/{pubkey}/block` | block / unblock a user |
| POST/DELETE | `/api/groups[/{name}]` | create / delete a group |
| POST/DELETE | `/api/groups/{name}/commands[/{cmd}]` | grant / revoke a command |
| POST/DELETE | `/api/channels[/{name}]` | add / remove a channel |
| PATCH | `/api/command-config/{command}` | edit a command's config |

Errors map by kind: `404` not found, `409` conflict, `400` refused/invalid;
the JSON `detail` carries the human-readable reason.

### Frontend (Vue 3 + Vite)

The built bundle is committed under `webapi/static/`, so the bot serves the
UI without Node installed. Source lives in `web-ui/`:

```bash
cd web-ui
npm install
npm run dev      # dev server, proxies /api → bot on :8080
npm run build    # rebuild bundle into webapi/static/
```
