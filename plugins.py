"""Command plugin loader and the per-message dispatch context."""

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


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
    # PROCESS_QUEUED=True means the command also runs for messages the radio
    # queued while the bot was offline (drained on startup). Default False:
    # replying to hours-old commands in rapid fire is usually noise, and
    # queued messages carry no routing path (so e.g. !path would be wrong).
    process_queued: bool = False
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
            process_queued=bool(getattr(mod, "PROCESS_QUEUED", False)),
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
    # True when this message came from the radio's offline backlog (fetched
    # during the startup queue drain, before the first NO_MORE_MSGS).
    from_queue: bool = False
