#!/usr/bin/env python3
# mcbot.py — MeshCore companion radio bot: CLI entry point.
#
# The implementation lives in focused modules:
#   protocol.py  on-air constants + packet envelope parsing
#   crypto.py    key derivation + DM/channel decryption
#   config.py    Config dataclass, mcbot.conf loading, logging setup
#   db.py        sqlite schema + lock-guarded async DB wrapper
#   plugins.py   command plugin loader / dispatch context
#   eviction.py  radio contact-table eviction policy
#   bot.py       the MCBot class
#
# This file keeps argument parsing and startup, and re-exports the public
# names so `import mcbot` keeps working for tests and tools.

import argparse
import asyncio
import logging  # noqa: F401  (re-exported: tests use mcbot.logging)
import signal
import sys
from pathlib import Path

from bot import MCBot, RepeatWatch  # noqa: F401
from config import (  # noqa: F401
    Config,
    effective_log_level,
    load_config,
    setup_logging,
)
from crypto import (  # noqa: F401
    DecryptedChannel,
    DecryptedDM,
    decrypt_direct_message,
    decrypt_group_text,
    derive_public_key,
    derive_shared_secret,
    try_decrypt_dm,
)
from db import DB, SCHEMA  # noqa: F401
from eviction import select_eviction_victims  # noqa: F401
from plugins import CommandContext, CommandLoader, CommandSpec  # noqa: F401
from protocol import (  # noqa: F401
    CHANNEL_SENDER_RE,
    CONTACT_TYPE_NAMES,
    MAX_PATH_SIZE,
    PACKET_TYPE_MAP,
    ParsedEnvelope,
    PayloadType,
    format_path,
    parse_contact_types,
    parse_packet_envelope,
)

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
    # outer loop so '!adm restart' and radio-loss recovery can fully tear
    # down and rebuild without exiting the process. on normal shutdown
    # (Ctrl-C, SIGTERM) we exit after the current iteration — signals set
    # shutdown_event, which overrides any pending internal restart.
    shutdown_event = asyncio.Event()
    backoff = 5.0
    while True:
        args = parse_args(argv)
        cfg = load_config(args)
        log = setup_logging(cfg)
        bot = MCBot(cfg, log)

        loop = asyncio.get_running_loop()

        def _signal_handler():
            log.info("signal received, stopping")
            shutdown_event.set()
            bot.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                pass

        rc = await bot.run()
        if shutdown_event.is_set() or not bot.restart_requested:
            return rc
        if bot.connect_failed:
            # radio unreachable (rebooting / unplugged / WiFi down): retry
            # with capped exponential backoff instead of hammering or exiting.
            log.warning("radio unavailable — retrying connect in %.0fs", backoff)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
                return rc
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
        else:
            backoff = 5.0
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
