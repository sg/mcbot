import asyncio
import time

# ---------------------------------------------------------------------------
# Module-level attributes — used by the loader at script load time, and
# seeded into command_config on first load. After seeding, operator edits
# in the DB override these.
# ---------------------------------------------------------------------------

# Command identifier. Used as the DB key in command_config and as the
# permission name in bot_group_commands. Must be unique across scripts.
NAME = "ping"

# Message prefixes that match this command. Case-insensitive. The
# matcher uses startswith() with whitespace-stripped text.
TRIGGERS = ["!ping", "ping"]

# Human-readable one-liner. Shown by `!adm command list` and `!adm help`.
DESCRIPTION = "Ping command — checks message reception"

# Per-user cooldown in seconds. 0 disables. The dispatcher records the
# last invocation in command_cooldowns keyed by (pubkey, command).
COOLDOWN_DEFAULT = 10

# Authorization is groups-only and fail-closed — there is no per-command
# auth flag. Before invoking handle() the dispatcher calls
# is_authorized_for_command(pubkey, NAME), which passes when the caller
# belongs to a group whose command list contains NAME or '*', OR when NAME
# (or '*') is granted to the 'public' group. A freshly-added command is
# therefore runnable only by owners (who hold '*') until you grant it:
#   !adm group grant public <NAME>     # open to everyone
#   !adm group grant <group> <NAME>    # limit to a group

# Channels where this command may run. None = any monitored channel.
# Names match with or without leading '#'. Empty list = also "any".
# Example: ["#bot", "#testing"]
ALLOWED_CHANNELS = ["#bot"]

# Whether DM invocations are accepted.
ALLOW_DM = True

# If True, channel invocations are silently dropped regardless of
# ALLOWED_CHANNELS. Use for commands whose security needs cryptographic
# proof of sender identity — channel "Name: " prefixes are NOT
# authenticated (anyone with the channel key can claim any sender).
# DMs bind identity to a private key via ECDH so they're trustworthy.
DM_ONLY = False

# If True, also run this command for messages the radio queued while the
# bot was offline (drained right after startup). Default False: those
# commands are often hours old, arrive in a burst, and carry no routing
# path (e.g. !path would answer 'direct (no path)'). Operators can
# override per command at runtime via the web Manage->Commands page.
PROCESS_QUEUED = False


# ---------------------------------------------------------------------------
# The handler. Called by the dispatcher after the message has cleared
# the enabled / dm_only / allow_dm / channel-allowlist / block-list /
# authorization / cooldown gates.
# ---------------------------------------------------------------------------
async def handle(ctx):
    # EXAMPLE 1: trivial reply using ctx fields.
    name = ctx.sender_name or "stranger"
    where = "DM" if ctx.is_dm else f"#{ctx.channel_name}"
    sender_ts = float(ctx.sender_timestamp)
    received_ts = time.time()
    delta_ts = received_ts - sender_ts
    return f"[@{name}] pong! {str(ctx.path_len)} hops traversed, took {delta_ts:.4f} seconds."
