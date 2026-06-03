"""Boilerplate template for a mcbot command script.

USAGE
-----
1. Copy this file into ./commands/  with a new name, e.g.
       cp example-bot-command.py commands/mycmd.py
2. Edit the module-level attributes (NAME, TRIGGERS, …) to suit.
3. Implement your logic in `handle(ctx)`.
4. From the bot owner's DM, run `!adm reload` to pick it up without
   restarting the bot.

On first load the bot inserts a row into the `command_config` SQLite
table seeded from these module-level defaults. After that, operators
can retune anything (cooldown, channel allowlist, dm_only, …) by
updating that row — the dispatcher reads it fresh on every invocation,
no reload required. To force a re-seed of the row from script defaults,
DELETE the row from command_config first, then `!adm reload`.

CONTRACT
--------
The loader expects two things from every command module:

(a) A set of optional module-level attributes (below). Anything not
    declared falls back to its hard-coded default.

(b) An `async def handle(ctx)` coroutine that returns one of:
        - str         → sent as a single reply
        - list[str]   → joined into 120-char chunks (auto-paginated)
                        for DMs / 100 for channel replies
        - None        → no reply
    Exceptions inside handle() are caught by the dispatcher, logged
    with full traceback, and swallowed — they will not kill the bot.

ctx FIELDS
----------
ctx.sender_name           str | None    — adv_name from contacts, or
                                          a "Name: " prefix parsed from
                                          a channel message
ctx.sender_pubkey         str | None    — 64-hex full pubkey, resolved
                                          from the contacts table
ctx.sender_pubkey_prefix  str | None    — 12-hex (6-byte) prefix from
                                          the DM envelope
ctx.message_text          str           — the inbound text. For channel
                                          messages the "Name: " prefix
                                          is already stripped here.
ctx.is_dm                 bool          — True for DMs, False for
                                          channel messages
ctx.channel_idx           int  | None   — channel slot (if !is_dm)
ctx.channel_name          str  | None   — channel name (if !is_dm)
ctx.path                  str  | None   — hex path the inbound packet
                                          took (no commas)
ctx.path_len              int  | None   — number of hops
ctx.path_hash_mode        int  | None   — 0/1/2 → 1/2/3 bytes per hop
ctx.snr                   float | None
ctx.rssi                  int   | None
ctx.sender_timestamp      int   | None  — unix timestamp the sender set
ctx.bot                   MCBot         — the running bot instance
                                          (see "ctx.bot helpers" below)

ctx.bot HELPERS
---------------
ctx.bot.db                                    — async SQLite wrapper
  ctx.bot.db.fetchone(sql, params=())
  ctx.bot.db.fetchall(sql, params=())
  ctx.bot.db.execute(sql, params=())
ctx.bot.logger                                — standard logging.Logger
ctx.bot.is_authorized_for_command(pubkey, command)
                                              — group-based auth check
ctx.bot.is_user_blocked(pubkey)
ctx.bot.resolve_target_user(name_or_pubkey)   — returns (pubkey, name, err)
ctx.bot.resolve_prefix(prefix_12hex)           — returns (pubkey, name)
ctx.bot.audit_log(actor_pubkey, actor_name, action, target, detail)
ctx.bot.paginate(lines, max_chars=120)         — greedy newline packer
ctx.bot.my_pubkey                              — bot's own 64-hex pubkey
ctx.bot.cfg                                    — Config dataclass
                                                 (host, port, channels, …)
"""

# Pure-stdlib imports here. For network/blocking calls, wrap them
# with `asyncio.to_thread(...)` so the bot's event loop stays
# responsive (see EXAMPLE 2 below).
import asyncio


# ---------------------------------------------------------------------------
# Module-level attributes — used by the loader at script load time, and
# seeded into command_config on first load. After seeding, operator edits
# in the DB override these.
# ---------------------------------------------------------------------------

# Command identifier. Used as the DB key in command_config and as the
# permission name in bot_group_commands. Must be unique across scripts.
NAME = "example"

# Message prefixes that match this command. Case-insensitive. The
# matcher uses startswith() with whitespace-stripped text.
TRIGGERS = ["!example", "!ex"]

# Human-readable one-liner. Shown by `!adm command list` and `!adm help`.
DESCRIPTION = "Template command — copy this file to commands/ to start"

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
ALLOWED_CHANNELS = None

# Whether DM invocations are accepted.
ALLOW_DM = True

# If True, channel invocations are silently dropped regardless of
# ALLOWED_CHANNELS. Use for commands whose security needs cryptographic
# proof of sender identity — channel "Name: " prefixes are NOT
# authenticated (anyone with the channel key can claim any sender).
# DMs bind identity to a private key via ECDH so they're trustworthy.
DM_ONLY = False


# ---------------------------------------------------------------------------
# The handler. Called by the dispatcher after the message has cleared
# the enabled / dm_only / allow_dm / channel-allowlist / block-list /
# authorization / cooldown gates.
# ---------------------------------------------------------------------------
async def handle(ctx):
    # EXAMPLE 1: trivial reply using ctx fields.
    name = ctx.sender_name or "stranger"
    where = "DM" if ctx.is_dm else f"#{ctx.channel_name}"
    return f"Hello {name}, I heard you on {where}!"

    # Other patterns you'd write instead (uncomment + adapt):
    # ----------------------------------------------------------------
    #
    # EXAMPLE 2: blocking I/O off the event loop.
    #
    #     import requests  # at module top
    #     def _fetch_sync():
    #         r = requests.get("https://example.com/api", timeout=5)
    #         return r.json()["value"]
    #     try:
    #         value = await asyncio.to_thread(_fetch_sync)
    #     except Exception as e:
    #         ctx.bot.logger.exception("example fetch failed")
    #         return f"Error: {e}"
    #     return f"value = {value}"
    #
    # EXAMPLE 3: list reply (auto-paginated by the framework).
    #
    #     return [
    #         "line 1 of three",
    #         "line 2 of three",
    #         "line 3 of three",
    #     ]
    #
    # EXAMPLE 4: parse args from the trigger.
    #
    #     # ctx.message_text is the full text, e.g. "!example foo bar"
    #     args = ctx.message_text.split(maxsplit=1)
    #     if len(args) < 2:
    #         return "Usage: !example <arg>"
    #     arg = args[1].strip()
    #     return f"You said: {arg}"
    #
    # EXAMPLE 5: DB query through the bot.
    #
    #     row = await ctx.bot.db.fetchone(
    #         "SELECT adv_name FROM contacts WHERE public_key=?",
    #         (ctx.sender_pubkey,),
    #     )
    #     return f"contact_name={row['adv_name'] if row else 'unknown'}"
    #
    # EXAMPLE 6: per-subcommand authorization. The command itself is gated
    # by NAME, but you can require a finer-grained permission for certain
    # branches by checking a separate permission string (grant it with
    # '!adm group grant <group> example.secret').
    #
    #     parts = ctx.message_text.split(maxsplit=1)
    #     sub = parts[1].split()[0].lower() if len(parts) > 1 else ""
    #     if sub == "secret":
    #         ok = await ctx.bot.is_authorized_for_command(
    #             ctx.sender_pubkey, "example.secret",
    #         )
    #         if not ok:
    #             return "Not authorized"
    #         return "(secret response)"
    #     return "public response"
    #
    # EXAMPLE 7: return None to stay silent (e.g. inbound msg matched
    # your trigger but you don't want to reply this time).
    #
    #     if not ctx.is_dm:
    #         return None
    #     return "DM-only reply"
    #
    # EXAMPLE 8: write an audit-log row when the command does something
    # state-changing that admins should be able to review later.
    #
    #     actor = (ctx.sender_pubkey or "").lower() or None
    #     await ctx.bot.audit_log(
    #         actor, ctx.sender_name, "example.action",
    #         target=None, detail="something happened",
    #     )
    #
    # EXAMPLE 9: use the bot's pagination explicitly when building a
    # long reply yourself. (For a list[str] return value the dispatcher
    # paginates automatically; use this only if you're packing your own
    # custom format.)
    #
    #     lines = [f"row {i}" for i in range(50)]
    #     return ctx.bot.paginate(lines, max_chars=120)
