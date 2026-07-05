"""Runtime-settings registry: DB-persisted tunables editable while running.

Every entry follows the same lifecycle: the mcbot.conf value seeds bot_meta
on first run (row absent), after which the database copy is authoritative
and managed at runtime via '!adm setting' (and per-setting aliases) or the
web UI. Adding a tunable means adding a Config field and one RuntimeSetting
here — loading, persistence, validation, audit, the REST API, and the
Manage UI all derive from the registry.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RuntimeSetting:
    key: str            # bot_meta key, MCBot/Config attribute, and API id
    kind: type          # int or float
    max: float          # highest value an admin may set (min is always 0)
    label: str
    audit_action: str   # audit-log action (pre-registry names kept)
    audit_detail: str   # audit detail template, .format(v=value)
    description: str    # web UI InfoTip / help text
    nonzero_min: float = 0.0        # if >0: value must be 0 (off) or >= this
    clamp_max: Optional[float] = None  # load-time cap for raw DB values
    on_set: Optional[str] = None    # MCBot method name invoked after a set
    unit: str = ""
    group: str = ""     # web UI tab that shows the setting

    def clamp(self, value):
        """Coerce a stored/parsed value into range (used at load and set;
        raises ValueError/TypeError on unparseable input)."""
        v = max(self.kind(0), self.kind(value))
        if self.clamp_max is not None:
            v = min(self.kind(self.clamp_max), v)
        return v

    def parse(self, value):
        """Admin input -> typed value; rejects non-integral input for int
        settings. Raises ValueError/TypeError."""
        v = float(value)
        if self.kind is int:
            if v != int(v):
                raise ValueError(value)
            v = int(v)
        return v

    def parse_error(self) -> str:
        noun = "a whole number" if self.kind is int else "a number"
        return f"{self.label} must be {noun}"

    def validate(self, v) -> Optional[str]:
        """Range check for admin-supplied values; None if ok."""
        if not (0 <= v <= self.max):
            if self.nonzero_min:
                return (
                    f"{self.label} must be 0 (off) or between "
                    f"{self.nonzero_min:g} and {self.max:g}"
                )
            return f"{self.label} must be between 0 and {self.max:g}"
        if self.nonzero_min and v != 0 and v < self.nonzero_min:
            return (
                f"{self.label} must be 0 (off) or between "
                f"{self.nonzero_min:g} and {self.max:g}"
            )
        return None

    def describe(self) -> dict:
        """Metadata for the API/UI (everything but the live value)."""
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "group": self.group,
            "description": self.description,
            "min": 0,
            "max": self.max,
            "step": 1 if self.kind is int else 0.1,
        }


SETTINGS = {s.key: s for s in (
    RuntimeSetting(
        key="advert_interval_hours", kind=int, max=168,
        on_set="reset_advert_schedule",
        label="Flood advert interval", unit="hours", group="radio",
        audit_action="radio.advert_interval", audit_detail="interval={v}h",
        description=(
            "0 = disabled. Bot sends a flood advert every N hours; persists "
            "in the database (mcbot.conf only seeds the first-run default)."
        ),
    ),
    RuntimeSetting(
        key="command_delay", kind=float, max=2.0,
        nonzero_min=0.1, clamp_max=2.0,
        label="Response delay", unit="seconds", group="commands",
        audit_action="command.delay", audit_detail="delay={v:.1f}s",
        description=(
            "0 = disabled, otherwise 0.1–2.0s. Held right before each reply "
            "is transmitted (after lookups/queries), to test whether nearby "
            "repeaters miss replies sent too quickly. Persists in the "
            "database (mcbot.conf only seeds the first-run default)."
        ),
    ),
    RuntimeSetting(
        key="channel_retry_max", kind=int, max=5, clamp_max=5,
        label="Channel resend on no-repeat", unit="", group="commands",
        audit_action="command.retry", audit_detail="retries={v}",
        description=(
            "0 = disabled, otherwise up to 5. If a channel message the bot "
            "sent gets no repeater rebroadcast within the repeat window, "
            "resend it this many times — an identical retransmit (same "
            "timestamp) that only repeaters which missed it pick up, so no "
            "duplicates. Needs repeat tracking on. Persists in the database "
            "(mcbot.conf only seeds the first-run default)."
        ),
    ),
)}
