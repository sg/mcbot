"""Configuration (mcbot.conf) loading and logging setup for mcbot."""

import configparser
import hashlib
import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from protocol import parse_contact_types

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
    # path of the config file actually loaded (None if none was found);
    # recorded so the startup banner can show where settings came from.
    config_path: Optional[str] = None
    # human-readable warnings about the config file (e.g. unrecognized keys),
    # collected during load and logged loudly at startup.
    config_warnings: list = field(default_factory=list)
    commands_dir: Path = Path("./commands")
    privkey_path: Optional[Path] = None  # default: <db>.privkey
    log_channels: str = "all"
    max_channel_messages: int = 1000
    max_dms: int = 1000
    max_packets: int = 1000
    max_contacts: int = 500
    auto_reconnect: bool = True
    commands_enabled: bool = True
    # Track whether repeaters rebroadcast the bot's own sent messages. When a
    # sent DM/channel message is heard being repeated on RF (RX_LOG_DATA), log
    # each repeater and surface it on the web Packets screen; if none is heard
    # within repeat_timeout seconds, log + surface that.
    repeat_tracking: bool = True
    repeat_timeout: float = 5.0
    # When a CHANNEL message the bot sent gets no repeater heard within
    # repeat_timeout, resend it up to this many times (0 = off, default 2). The
    # retry is an idempotent retransmit — MeshCore keys a channel message by
    # SHA256(timestamp||text), so reusing the original timestamp makes the retry
    # byte-identical: nodes that already heard it de-dupe it, only repeaters
    # that missed it pick it up. Requires repeat_tracking. DMs are excluded
    # (they have their own ACK-driven retry). SEED only: first run writes it to
    # bot_meta, then the DB value is authoritative and runtime-managed (via
    # '!adm command retry N' and the web Manage->Commands page).
    channel_retry_max: int = 2
    # !path collision disambiguation: when a path-hop hash matches more than one
    # repeater, a candidate is accepted only if it lies within this many miles of
    # a trusted anchor (the bot's own location, the sender, or an unambiguous
    # neighbouring hop). This is a single-hop RF sanity bound that rejects a far
    # repeater sharing the hash (e.g. a tropospheric-ducting node) while keeping
    # a correctly-located local hop. Applies ONLY to collisions; unique hops are
    # always trusted. 0 disables it (collisions then resolve only when >= 2
    # candidates are located). Takes effect whenever the path has at least one
    # anchor (any of the three above); the bot's own location helps but is not
    # required if a neighbouring hop resolves unambiguously.
    path_collision_radius_miles: float = 150.0
    debug: bool = False
    # idx -> (name, 16-byte secret)
    channels: dict[int, tuple[str, bytes]] = field(default_factory=dict)
    # pubkey strings are inserted into the 'owner' group on startup
    owner_pubkeys: list[str] = field(default_factory=list)
    # dm-send retry settings
    dm_max_attempts: int = 3
    dm_flood_after: int = 2
    dm_max_flood_attempts: int = 2
    # --- radio contact-table rollover ---
    # The radio's contact table (max_contacts, ~350) doesn't age out old
    # contacts, so once full it drops new nodes (CONTACTS_FULL). When enabled,
    # the bot evicts the stalest radio contacts to keep `radio_evict_headroom`
    # free slots, on startup and whenever CONTACTS_FULL fires. The bot's own
    # sqlite contacts table is the long-term archive and is never touched by
    # eviction. evict_enabled/evict_headroom are also adjustable at runtime via
    # the web Manage->Radio page (runtime-only; this file is the startup truth).
    radio_evict_enabled: bool = True
    radio_evict_headroom: int = 8
    # contact types (per CONTACT_TYPENAMES: none/cli/repeater/room/sensor) that
    # are never evicted, in addition to bot users, owners, and recent DM peers.
    radio_evict_protect_types: set = field(default_factory=set)
    radio_evict_max_per_run: int = 50      # safety cap on removals per run
    radio_evict_min_interval: float = 120  # seconds debounce for auto runs
    # Periodic flood-advert interval in hours (0 = disabled). This is only the
    # SEED: on first run it is written to bot_meta, after which the DB value is
    # authoritative and runtime-managed (via '!adm advert interval N' and the
    # web Manage->Radio page). Edit this only to change the first-run default.
    advert_interval_hours: int = 0
    # Delay (seconds) applied right before transmitting each command response,
    # after its text is built — lookups/web queries are NOT delayed. 0 =
    # disabled; otherwise clamped to 0.1–2.0. SEED only: first run writes it to
    # bot_meta, then the DB value is authoritative and runtime-managed (via
    # '!adm command delay N' and the web Manage->Commands page).
    command_delay: float = 0.0
    # Radio-link watchdog interval in seconds (0 = disabled). Every N seconds
    # the bot pings the radio with a device query; two consecutive missed
    # replies trigger a full reconnect/resync. Catches silently dead TCP links
    # (e.g. a power-cycled radio) that never signal a disconnect. SEED only:
    # first run writes it to bot_meta, then the DB value is authoritative and
    # runtime-managed ('!adm setting watchdog_interval N' / web Manage->Radio).
    watchdog_interval: int = 180
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


# Recognized option keys per config section (lowercase — configparser folds
# option names to lowercase). Used to warn about typos / stale settings at
# startup. Keep in sync with the keys load_config actually reads below.
_KNOWN_CONFIG_KEYS = {
    "radio": {"transport", "host", "port", "serial_port", "serial_baud",
              "device_pin"},
    "storage": {"db", "max_channel_messages", "max_dms", "max_contacts",
                "max_packets"},
    "channel_logging": {"channels"},
    "logging": {"logs_dir", "log_level"},
    "bot": {"commands_dir", "enabled", "repeat_tracking", "repeat_timeout",
            "channel_retry_max", "path_collision_radius_miles",
            "privkey_path", "dm_max_attempts", "dm_flood_after",
            "dm_max_flood_attempts", "radio_evict_enabled",
            "radio_evict_headroom", "radio_evict_max_per_run",
            "radio_evict_min_interval", "radio_evict_protect_types",
            "advert_interval_hours", "command_delay", "watchdog_interval",
            "owner_pubkeys"},
    "web": {"enabled", "host", "port", "admin_user", "admin_password_hash",
            "session_secret", "cors_origins", "api_tokens", "tls_cert",
            "tls_key"},
}
# Sections whose keys are user data (env-var names, channel indexes), not a
# fixed set of option names — accept any key there.
_DYNAMIC_CONFIG_SECTIONS = {"env", "channels"}
# Options that were removed; flagged as deprecated (ignored) rather than
# "unrecognized" so upgraders get a clear, friendly nudge.
_DEPRECATED_CONFIG_KEYS = {"bot": {"rx_log_decrypt"}}


def _check_unknown_config_keys(parser) -> list:
    """Return warnings for unrecognized sections/keys in the parsed config."""
    warnings = []
    for section in parser.sections():
        if section in _DYNAMIC_CONFIG_SECTIONS:
            continue
        if section not in _KNOWN_CONFIG_KEYS:
            warnings.append(f"unrecognized section [{section}] (ignored)")
            continue
        allowed = _KNOWN_CONFIG_KEYS[section]
        deprecated = _DEPRECATED_CONFIG_KEYS.get(section, frozenset())
        for key in parser[section]:
            if key in allowed:
                continue
            if key in deprecated:
                warnings.append(
                    f"[{section}] '{key}' is deprecated and ignored "
                    "(safe to remove)"
                )
            else:
                warnings.append(f"[{section}] unrecognized key '{key}' (ignored)")
    return warnings


def load_config(args) -> Config:
    cfg = Config()

    config_path = args.config or Path("./mcbot.conf")
    if config_path and Path(config_path).is_file():
        cfg.config_path = str(config_path)
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
            cfg.repeat_tracking = parser["bot"].getboolean(
                "repeat_tracking", cfg.repeat_tracking
            )
            cfg.repeat_timeout = parser["bot"].getfloat(
                "repeat_timeout", cfg.repeat_timeout
            )
            cfg.channel_retry_max = min(5, max(0, parser["bot"].getint(
                "channel_retry_max", cfg.channel_retry_max
            )))
            cfg.path_collision_radius_miles = max(0.0, parser["bot"].getfloat(
                "path_collision_radius_miles", cfg.path_collision_radius_miles
            ))
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
            cfg.radio_evict_enabled = parser["bot"].getboolean(
                "radio_evict_enabled", cfg.radio_evict_enabled
            )
            cfg.radio_evict_headroom = parser["bot"].getint(
                "radio_evict_headroom", cfg.radio_evict_headroom
            )
            cfg.radio_evict_max_per_run = parser["bot"].getint(
                "radio_evict_max_per_run", cfg.radio_evict_max_per_run
            )
            cfg.radio_evict_min_interval = parser["bot"].getfloat(
                "radio_evict_min_interval", cfg.radio_evict_min_interval
            )
            cfg.advert_interval_hours = parser["bot"].getint(
                "advert_interval_hours", cfg.advert_interval_hours
            )
            cfg.command_delay = min(2.0, max(0.0, parser["bot"].getfloat(
                "command_delay", cfg.command_delay
            )))
            cfg.watchdog_interval = max(0, parser["bot"].getint(
                "watchdog_interval", cfg.watchdog_interval
            ))
            protect_raw = parser["bot"].get("radio_evict_protect_types", "")
            if protect_raw.strip():
                cfg.radio_evict_protect_types = parse_contact_types(
                    protect_raw, on_error=lambda tok: sys.stderr.write(
                        f"WARNING: ignoring unknown radio_evict_protect_types "
                        f"entry {tok!r}\n"
                    )
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

        # flag unrecognized sections/keys (typos, stale settings); logged
        # loudly at startup once the logger is configured.
        cfg.config_warnings = _check_unknown_config_keys(parser)

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
def effective_log_level(cfg: Config) -> str:
    """The level both the bot and meshcore library loggers should use:
    --debug forces DEBUG, otherwise [logging] log_level."""
    return "DEBUG" if cfg.debug else (cfg.log_level or "INFO").upper()


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

    # --debug is a shortcut for the most verbose logging; otherwise BOTH the
    # bot's own logger and the meshcore library logger follow [logging]
    # log_level. NOTE: MeshCore.create_*() re-sets the "meshcore" logger level
    # from its `debug` arg at connect time (overriding what we set here), so
    # run() must re-assert it after connecting — see effective_log_level usage.
    level = effective_log_level(cfg)
    bot_log = _reset("mcbot", level)
    mc_log = _reset("meshcore", level)

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

    # startup banner: make the effective level and the config source explicit,
    # so it's obvious whether log_level from mcbot.conf actually took effect
    # (and which file was read). At INFO so it shows in the default config and
    # in the common "expected DEBUG, got INFO" case, without polluting logs.
    src = "--debug" if cfg.debug else f"log_level={cfg.log_level}"
    bot_log.info(
        "logging at %s (%s); config=%s",
        level, src, cfg.config_path or "(no config file found)",
    )

    return bot_log
