# mcbot

A standalone Python bot for [MeshCore](https://meshcore.io) companion radios.
Connects over TCP/IP (WiFi) or USB-serial, syncs contacts/channels/messages into a
local SQLite database, logs all observed RF activity, and responds to incoming
text commands customized via individual python script/plugins.

Built for MeshCore companion firmware, mcbot handles message delivery via both
paths the radio firmware offers:
- the standard queued-message path `MESSAGES_WAITING` followed by `get_msg()`
- and client-side decryption from the radio's `RX_LOG_DATA` event stream
(whichever arrives first wins, duplicates from the other path are discarded)


## Features

- TCP/IP (WiFi) or USB-serial connection to a MeshCore radio via the `meshcore_py` library
- SQLite-backed persistence: contacts, channels, DMs, channel messages, raw
  packet firehose, user/group/command admin, audit log
- Client-side decryption of inbound DMs (using the radio's exported Ed25519
  private key) and channel messages (operator-configured 16-byte channel secrets)
- Plugin-based command system: drop a Python file into `commands/`,
  hot-reload via `!adm reload`
- Group-based authorization with system groups `owner`, `admin`, `public`,
  `blocked`, `user`
- Built-in `!adm` admin command for user/group/channel/contact management,
  audit log inspection, hot reload, and live in-process restart
- `!help` (full listing in DM; a "DM me" nudge on channels) showing only
  the commands the caller can run, and `!whoami` showing the caller's
  identity and group memberships
- Bundled example commands: `!pws` (Weather Underground PWS),
  `!wx <city> [CC]` (Open-Meteo), `!path` (routing-path diagnostic), and more
- Configurable retention caps on stored messages, contacts, and the raw packet firehose
- INI-based configuration with CLI-flag overrides
- Detailed log file with per-packet decoded info, command activity, and outbound
  delivery confirmation
- Runtime-editable command config in the `command_config` SQLite table
  (cooldown, dm_only, allowed_channels, etc.) — changes take effect on
  the next invocation, no reload required

## Quick Start

```bash
# Python 3.12+ required
git clone https://github.com/sg/mcbot.git mcbot
cd mcbot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Edit mcbot.conf — at minimum set [radio] host/port or serial-port/baud and [bot] owner_pubkeys.
# See Bot_Usage.md for the rest.

./mcbot.py
```

From your owner client, DM the bot `!whoami` to confirm access, then
`!help` to discover the commands available to you (`!help adm` for the
admin subcommand reference).

Recommended to run mcbot.py in a `screen` or `tmux` session.

See [`Bot_Usage.md`](Bot_Usage.md) for the full architecture, configuration,
management, and command reference.

## Project Layout

```
mcbot.py                 main bot script
mcbot.conf               INI config (radio host/port|serial device/baud, owners, channels, ...)
requirements.txt         pip dependencies
commands/                plugin scripts; one file per command
    adm.py                  administrative subcommands (!adm)
    help.py                 !help (lists commands you can run; DM for full)
    whoami.py               !whoami (your identity + group memberships)
    pws.py                  !pws (PWS station observation)
    wx.py                   !wx <city> [CC] (Open-Meteo)
    path.py                 !path (routing path + distance + map link)
    topo.py                 !topo (geo-locate contact via topographic map link)
    quote.py                !quote (responds with a random 'quote' from a file)
example-bot-command.py   template for new commands; copy into commands/
Bot_Usage.md             detailed architecture + admin guide
mcbot.db                 SQLite DB (created on first run)
mcbot.privkey            cached radio private key (0600 perms)
logs/mcbot.log           rotating log file (created on first run)
```

## Requirements

- Python 3.12+
- A MeshCore companion radio reachable over TCP/IP (WiFi) or USB-serial
- Radio firmware v1.14.x or newer recommended
- Python dependencies (`requirements.txt`):
  - `meshcore` — the meshcore_py protocol library
  - `pynacl` — X25519 ECDH for client-side DM decryption
  - `pycryptodome` — AES (pulled in transitively by meshcore)
  - `requests` — used by the example weather commands


## Web admin UI / API (optional)

An optional in-process web UI + REST/WebSocket API for managing the bot,
monitoring channel messages, and inspecting raw packets. Disabled by
default; enable the `[web]` section in `mcbot.conf`.

```bash
# 1. set a session secret + admin password hash + an API token in [web]
./mcbot.py --hash-password        # prints a hash for admin_password_hash
# 2. run the bot; the UI is served at http://<host>:<port>/ and the
#    REST docs (Swagger) at /api/docs
./mcbot.py
```

The prebuilt frontend bundle is committed under `webapi/static/`, so the
bot serves the UI without needing Node installed. To modify the UI:

```bash
cd web-ui
npm install
npm run dev      # hot-reload dev server; proxies /api to the bot on :8080
npm run build    # rebuild the bundle into webapi/static/
```

