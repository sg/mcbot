#
# !adm — administrative commands for mcbot.
# 
# authorization model:
# - authorization is groups-only (fail-closed): the dispatcher runs '!adm'
#   only if the caller is in a group granting 'adm' (or '*'). The 'owner'
#   group is bootstrapped with '*', so owners always have it. There is no
#   separate per-command auth flag — the command name IS the permission.
# - admin commands are DM-only. Channel invocations are silently dropped.
# - the general command listing lives in the separate '!help' command;
#   '!help adm' renders the subcommand reference below (HELP_DETAIL), which
#   help.py shows only to callers who can actually run '!adm'. '!whoami' is
#   its own command too.
# 
# audit: every state-changing subcommand writes a row to bot_audit_log.

import asyncio
import json
import time

from management import Management, MgmtError

NAME = "adm"
TRIGGERS = ["!adm"]
DESCRIPTION = "Administrative commands (DM-only). '!help adm' for the list."
COOLDOWN_DEFAULT = 0
ALLOWED_CHANNELS = ["#bot-cmd-test"]  # ignored while DM_ONLY=True
ALLOW_DM = True

# DM_ONLY=True means channel invocations are silently dropped regardless
# of ALLOWED_CHANNELS. required for !adm because channel-message sender
# names are not cryptographically authenticated (anyone with the channel
# key can claim any sender name in the plaintext prefix).
# DMs bind identity to a private key via ECDH, so admin must be DM-only.
DM_ONLY = True

# subcommand help text 
_HELP = [
    ("user list",                        "list known users and their groups"),
    ("user show <name|pubkey>",          "details for one user"),
    ("user add <name|pubkey> <group>",   "add user to a group (creates user)"),
    ("user remove <name|pubkey> <grp>",  "remove user from a group"),
    ("user delete <name|pubkey>",        "delete user from bot entirely"),
    ("user rename <name|pubkey> <new>",  "set the user's friendly alias"),
    ("user block <name|pubkey>",         "deny all commands from this user"),
    ("user unblock <name|pubkey>",       "lift the block"),
    ("group list",                       "list defined groups"),
    ("group show <name>",                "show group's commands and members"),
    ("group add <name> [c1,c2,...]",     "create group; optional command list"),
    ("group grant <name> <cmd>",         "grant a command to a group"),
    ("group revoke <name> <cmd>",        "revoke a command from a group"),
    ("group allow-all <name>",           "make group include all users (* member)"),
    ("group restrict <name>",            "undo allow-all (explicit members only)"),
    ("group delete <name>",              "delete group (refuses system groups)"),
    ("contacts search <string>",         "match contacts by name or pubkey"),
    ("contacts delete <name|pubkey>",    "remove a contact from the radio + DB"),
    ("channel list",                     "list configured channel names"),
    ("channel add <name> [hex_key]",     "add channel ('#'-names auto-key)"),
    ("channel remove <name>",            "remove a channel"),
    ("command list",                     "list loaded commands + their config"),
    ("command channel add <cmd> <ch>",   "add a channel the command responds on"),
    ("command channel remove <cmd> <ch>", "stop a command responding on a channel"),
    ("command delay <seconds>",          "delay before sending replies (0 off, 0.1–2.0)"),
    ("command retry <count>",            "resend a channel reply if no repeat heard (0 off, max 5)"),
    ("radio pathhash [1|2|3]",           "show/set this radio's outgoing path-hash width"),
    ("advert <flood|zero>",              "send a flood or zero-hop advertisement"),
    ("advert interval <hours>",          "send a flood advert every N hours (0 disables)"),
    ("status",                           "bot health and runtime stats"),
    ("reload",                           "rescan commands/ and reload plugins"),
    ("restart",                          "full teardown + reinit (re-reads config)"),
    ("log [N]",                          "show last N audit-log rows (def 10)"),
]

# rendered by '!help adm' (only for callers authorized for 'adm')
HELP_DETAIL = ["!adm subcommands (DM-only):"] + [
    f"  {sig} — {desc}" for sig, desc in _HELP
]


async def handle(ctx):
    text = ctx.message_text.strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        return "Usage: !adm <subcommand> ... — try '!help adm'"

    sub = parts[1].lower()
    rest = parts[2].strip() if len(parts) > 2 else ""

    # no additional auth needed here. the dispatcher already enforced the 'adm'
    # permission before calling handle().

    handler = _SUBCOMMANDS.get(sub)
    if not handler:
        return f"Unknown subcommand: {sub!r}. Try '!help adm'."
    try:
        return await handler(ctx, rest)
    except Exception as e:
        ctx.bot.logger.exception("!adm %s failed", sub)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# user.*
# 
async def _cmd_user(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm user <list|show|add|remove|delete|rename|block|unblock> ..."
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _USER_OPS.get(op)
    if not handler:
        return f"Unknown user op: {op!r}"
    return await handler(ctx, args)


async def _user_list(ctx, _):
    rows = await ctx.bot.db.fetchall(
        "SELECT u.pubkey, u.name, GROUP_CONCAT(ug.group_name, ',') AS groups "
        "FROM bot_users u "
        "LEFT JOIN bot_user_groups ug ON ug.pubkey = u.pubkey "
        "GROUP BY u.pubkey "
        "ORDER BY COALESCE(u.name, u.pubkey)"
    )
    if not rows:
        return "no users registered"
    out = [f"Users ({len(rows)}):"]
    for r in rows:
        name = r["name"] or "?"
        grps = r["groups"] or "-"
        out.append(f"  {name} [{r['pubkey'][:12]}] {grps}")
    return out


async def _user_show(ctx, args):
    if not args:
        return "Usage: !adm user show <name|pubkey>"
    pk, name, err = await ctx.bot.resolve_target_user(args)
    if err:
        return err
    row = await ctx.bot.db.fetchone(
        "SELECT name, added_by, added_at, notes FROM bot_users WHERE pubkey=?",
        (pk.lower(),),
    )
    # effective membership is explicit groups + all-users groups (e.g. public)
    eff = await ctx.bot.effective_groups_for_user(pk)
    groups = ", ".join(eff) or "(none)"
    if row:
        alias = row["name"] or name or "?"
        added_at = row["added_at"]
        added_by = row["added_by"] or "?"
        ts = (
            time.strftime("%Y-%m-%d", time.gmtime(added_at))
            if added_at else "?"
        )
        return [
            f"name: {alias}",
            f"pubkey: {pk}",
            f"groups: {groups}",
            f"added: {ts} by {added_by[:12] if added_by != 'config' else added_by}",
        ]
    return [
        f"name: {name or '?'}",
        f"pubkey: {pk}",
        f"groups: {groups} (not in bot_users)",
    ]


def _actor(ctx):
    """(actor_pubkey, actor_name) kwargs for mgmt audit, from the DM sender."""
    return {
        "actor_pubkey": ctx.sender_pubkey,
        "actor_name": ctx.sender_name,
    }


async def _user_add(ctx, args):
    parts = args.split()
    if len(parts) < 2:
        return "Usage: !adm user add <name|pubkey> <group>"
    target = parts[0]
    group = parts[1].lower()
    pk, name, err = await ctx.bot.resolve_target_user(target)
    if err:
        return err
    try:
        await ctx.bot.mgmt.user_add_to_group(pk, name, group, **_actor(ctx))
    except MgmtError as e:
        if e.code == "not_found":  # unknown group — add the '!adm' hint
            return f"Unknown group: {group!r}. Use '!adm group add {group}' first."
        return e.message
    return f"Added {name or pk[:12]} to {group}"


async def _user_remove(ctx, args):
    parts = args.split()
    if len(parts) < 2:
        return "Usage: !adm user remove <name|pubkey> <group>"
    target = parts[0]
    group = parts[1].lower()
    pk, name, err = await ctx.bot.resolve_target_user(target)
    if err:
        return err
    try:
        r = await ctx.bot.mgmt.user_remove_from_group(pk, group, **_actor(ctx))
    except MgmtError as e:
        return e.message
    if not r["removed"]:
        return f"{name or pk[:12]} was not in {group}"
    return f"Removed {name or pk[:12]} from {group}"


async def _user_delete(ctx, args):
    if not args:
        return "Usage: !adm user delete <name|pubkey>"
    pk, name, err = await ctx.bot.resolve_target_user(args)
    if err:
        return err
    try:
        r = await ctx.bot.mgmt.user_delete(pk, **_actor(ctx))
    except MgmtError as e:
        return e.message
    if not r["deleted"]:
        return f"{name or pk[:12]} was not registered"
    return f"Deleted user {name or pk[:12]}"


async def _user_rename(ctx, args):
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: !adm user rename <name|pubkey> <new_alias>"
    target, new_alias = parts[0], parts[1].strip()
    pk, _name, err = await ctx.bot.resolve_target_user(target)
    if err:
        return err
    r = await ctx.bot.mgmt.user_rename(pk, new_alias, **_actor(ctx))
    if not r["renamed"]:
        return f"{target} is not registered (use 'user add' first)"
    return f"Alias updated to {new_alias}"


async def _user_block(ctx, args):
    if not args:
        return "Usage: !adm user block <name|pubkey>"
    pk, name, err = await ctx.bot.resolve_target_user(args)
    if err:
        return err
    try:
        await ctx.bot.mgmt.user_block(pk, name, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Blocked {name or pk[:12]}"


async def _user_unblock(ctx, args):
    if not args:
        return "Usage: !adm user unblock <name|pubkey>"
    pk, name, err = await ctx.bot.resolve_target_user(args)
    if err:
        return err
    r = await ctx.bot.mgmt.user_unblock(pk, **_actor(ctx))
    if not r["unblocked"]:
        return f"{name or pk[:12]} was not blocked"
    return f"Unblocked {name or pk[:12]}"


_USER_OPS = {
    "list":    _user_list,
    "show":    _user_show,
    "add":     _user_add,
    "remove":  _user_remove,
    "delete":  _user_delete,
    "rename":  _user_rename,
    "block":   _user_block,
    "unblock": _user_unblock,
}


# ---------------------------------------------------------------------------
# group.*
# 
async def _cmd_group(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm group <list|show|add|grant|revoke|delete> ..."
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _GROUP_OPS.get(op)
    if not handler:
        return f"Unknown group op: {op!r}"
    return await handler(ctx, args)


async def _group_list(ctx, _):
    rows = await ctx.bot.db.fetchall(
        "SELECT g.name, g.is_system, g.all_users, "
        "  (SELECT COUNT(*) FROM bot_group_commands WHERE group_name=g.name) AS ncmds, "
        "  (SELECT COUNT(*) FROM bot_user_groups   WHERE group_name=g.name) AS nusers "
        "FROM bot_groups g ORDER BY g.name"
    )
    out = [f"Groups ({len(rows)}):"]
    for r in rows:
        tag = " [system]" if r["is_system"] else ""
        # "*" appended to the user count means the group includes all users.
        users = f"{r['nusers']}{'+*' if r['all_users'] else ''}"
        out.append(
            f"  {r['name']}{tag}  cmds={r['ncmds']} users={users}"
        )
    return out


async def _group_show(ctx, args):
    if not args:
        return "Usage: !adm group show <name>"
    name = args.split()[0].lower()
    grow = await ctx.bot.db.fetchone(
        "SELECT description, is_system, all_users FROM bot_groups WHERE name=?",
        (name,),
    )
    if not grow:
        return f"unknown group: {name}"
    cmds = await ctx.bot.db.fetchall(
        "SELECT command FROM bot_group_commands WHERE group_name=? ORDER BY command",
        (name,),
    )
    members = await ctx.bot.db.fetchall(
        "SELECT u.pubkey, u.name FROM bot_user_groups ug "
        "JOIN bot_users u ON u.pubkey=ug.pubkey "
        "WHERE ug.group_name=? "
        "ORDER BY COALESCE(u.name, u.pubkey)",
        (name,),
    )
    out = [
        f"group: {name}{' [system]' if grow['is_system'] else ''}",
        f"desc: {grow['description'] or '(none)'}",
        "commands: " + (", ".join(c["command"] for c in cmds) or "(none)"),
        f"members ({len(members)}{'+*' if grow['all_users'] else ''}):",
    ]
    if grow["all_users"]:
        out.append("  * (all users)")
    for m in members:
        out.append(f"  {m['name'] or '?'} [{m['pubkey'][:12]}]")
    return out


async def _group_add(ctx, args):
    parts = args.split(maxsplit=1)
    if not parts:
        return "Usage: !adm group add <name> [cmd1,cmd2,...]"
    name = parts[0].lower()
    cmd_list = parts[1].strip() if len(parts) > 1 else ""
    commands = [c.strip() for c in cmd_list.split(",")] if cmd_list else []
    r = await ctx.bot.mgmt.group_add(name, commands, **_actor(ctx))
    return f"group {name!r}: created/exists, +{r['added']} command(s)"


async def _group_grant(ctx, args):
    parts = args.split()
    if len(parts) < 2:
        return "Usage: !adm group grant <name> <cmd>"
    name = parts[0].lower()
    cmd = parts[1]
    try:
        r = await ctx.bot.mgmt.group_grant(name, cmd, **_actor(ctx))
    except MgmtError as e:
        return e.message
    if not r["granted"]:
        return f"{cmd!r} was already granted to {name}"
    return f"Granted {cmd!r} to {name}"


async def _group_revoke(ctx, args):
    parts = args.split()
    if len(parts) < 2:
        return "Usage: !adm group revoke <name> <cmd>"
    name = parts[0].lower()
    cmd = parts[1]
    try:
        r = await ctx.bot.mgmt.group_revoke(name, cmd, **_actor(ctx))
    except MgmtError as e:
        return e.message
    if not r["revoked"]:
        return f"{cmd!r} was not granted to {name}"
    return f"Revoked {cmd!r} from {name}"


async def _group_delete(ctx, args):
    if not args:
        return "Usage: !adm group delete <name>"
    name = args.split()[0].lower()
    try:
        await ctx.bot.mgmt.group_delete(name, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Deleted group {name!r}"


async def _group_allow_all(ctx, args):
    if not args:
        return "Usage: !adm group allow-all <name>"
    name = args.split()[0].lower()
    try:
        await ctx.bot.mgmt.group_set_all_users(name, True, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Group {name!r} now includes all users (*)"


async def _group_restrict(ctx, args):
    if not args:
        return "Usage: !adm group restrict <name>"
    name = args.split()[0].lower()
    try:
        await ctx.bot.mgmt.group_set_all_users(name, False, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Group {name!r} no longer includes all users (explicit members only)"


_GROUP_OPS = {
    "list":      _group_list,
    "show":      _group_show,
    "add":       _group_add,
    "grant":     _group_grant,
    "revoke":    _group_revoke,
    "delete":    _group_delete,
    "allow-all": _group_allow_all,
    "restrict":  _group_restrict,
}


# ---------------------------------------------------------------------------
# contacts.*
# 
async def _cmd_contacts(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm contacts <search> ..."
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _CONTACTS_OPS.get(op)
    if not handler:
        return f"Unknown contacts op: {op!r}"
    return await handler(ctx, args)


async def _contacts_search(ctx, args):
    if not args:
        return "Usage: !adm contacts search <string>"
    query = args.strip()
    pattern = f"%{query.lower()}%"
    rows = await ctx.bot.db.fetchall(
        "SELECT public_key, adv_name, last_advert "
        "FROM contacts "
        "WHERE LOWER(adv_name) LIKE ? OR LOWER(public_key) LIKE ? "
        "ORDER BY last_advert DESC NULLS LAST",
        (pattern, pattern),
    )
    if not rows:
        return f"No contacts match {query!r}"
    out = [f"Matches for {query!r}: {len(rows)}"]
    for r in rows[:3]:
        name = r["adv_name"] or "?"
        pk6 = r["public_key"][:6]
        la = r["last_advert"]
        iso = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(la))
            if la else "never"
        )
        out.append(f"  {name} [{pk6}] {iso}")
    return out


async def _contacts_delete(ctx, args):
    if not args:
        return "Usage: !adm contacts delete <name|pubkey>"
    pk, name, err = await ctx.bot.resolve_target_user(args)
    if err:
        return err
    try:
        await ctx.bot.mgmt.contact_delete(pk, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Deleted contact {name or pk[:12]}"


_CONTACTS_OPS = {
    "search": _contacts_search,
    "delete": _contacts_delete,
}


# ---------------------------------------------------------------------------
# channel.*
# 
async def _cmd_channel(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm channel <list|add|remove> ..."
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _CHAN_OPS.get(op)
    if not handler:
        return f"Unknown channel op: {op!r}"
    return await handler(ctx, args)


async def _channel_add(ctx, args):
    parts = args.split(maxsplit=1)
    if not parts:
        return "Usage: !adm channel add <name> [hex_key]"
    name = parts[0]
    key_hex = parts[1].strip() if len(parts) > 1 else ""
    try:
        # hash-tag chans auto-derive their key. others require a 16-byte hex key.
        secret = Management.channel_secret(name, key_hex)
        r = await ctx.bot.mgmt.channel_add(name, secret, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Added channel {name!r} at idx={r['idx']}"


async def _channel_remove(ctx, args):
    if not args:
        return "Usage: !adm channel remove <name>"
    name = args.strip()
    try:
        r = await ctx.bot.mgmt.channel_remove(name, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Removed channel {name!r} (was idx={r['idx']})"


async def _channel_list(ctx, _):
    rows = await ctx.bot.db.fetchall(
        "SELECT name FROM channels ORDER BY channel_idx"
    )
    names = [r["name"] for r in rows if r["name"]]
    if not names:
        return "chans: (none)"
    # pack names into messages of the form "chans: a,b,c" with each
    # reply message not exceeding max_chars. each chunk starts with the "chans: "
    # prefix so if they are received out of order or not at all, the context is
    # clear.
    out: list[str] = []
    prefix = "chans: "
    cur = prefix
    max_chars = 120
    for n in names:
        sep = "," if cur != prefix else ""
        candidate = cur + sep + n
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            out.append(cur)
            cur = prefix + n
    if cur != prefix:
        out.append(cur)
    return out


_CHAN_OPS = {
    "list": _channel_list,
    "add": _channel_add,
    "remove": _channel_remove,
}


# ---------------------------------------------------------------------------
# misc: commands list, status, reload, audit log
# 
async def _cmd_command(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return await _command_list(ctx, "")
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _COMMAND_OPS.get(op)
    if not handler:
        return f"Unknown command op: {op!r}. Try: list, channel, delay, retry"
    return await handler(ctx, args)


async def _command_list(ctx, _):
    cmds = ctx.bot.loader.commands
    if not cmds:
        return "no commands loaded"
    out = [f"Loaded commands ({len(cmds)}):"]
    for cs in sorted(cmds.values(), key=lambda c: c.name):
        # allowed_channels comes from command_config (DB authority), not
        # the in-memory script default, so the listing matches dispatch.
        row = await ctx.bot.db.fetchone(
            "SELECT allowed_channels FROM command_config WHERE command=?",
            (cs.name,),
        )
        chans = None
        if row and row["allowed_channels"]:
            try:
                chans = json.loads(row["allowed_channels"])
            except Exception:
                chans = None
        # "public" if anyone can run it, otherwise restrict by group membershio
        pub = await ctx.bot.db.fetchone(
            "SELECT 1 FROM bot_group_commands "
            "WHERE group_name='public' AND command IN (?, '*') LIMIT 1",
            (cs.name,),
        )
        flags = ["public" if pub else "restricted"]
        if not cs.allow_dm:
            flags.append("no-dm")
        if cs.dm_only:
            flags.append("dm-only")
        flags.append("ch=" + (",".join(chans) if chans else "any"))
        out.append(f"  {cs.name}: {','.join(flags)}")
    return out


async def _command_channel(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm command channel <add|remove> <command> <channel>"
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    if op == "add":
        return await _command_channel_change(ctx, args, add=True)
    if op == "remove":
        return await _command_channel_change(ctx, args, add=False)
    return f"Unknown: !adm command channel {op!r} (use add|remove)"


async def _command_channel_change(ctx, args, add):
    parts = args.split()
    verb = "add" if add else "remove"
    if len(parts) < 2:
        return f"Usage: !adm command channel {verb} <command> <channel>"
    command = parts[0]
    channel = parts[1]
    try:
        r = await ctx.bot.mgmt.command_channel_change(
            command, channel, add, **_actor(ctx)
        )
    except MgmtError as e:
        if e.code == "not_found":  # add the '!adm reload' hint
            return (
                f"No config row for command {command!r} "
                "(is it loaded? try '!adm reload')"
            )
        return e.message
    if not r["changed"]:
        if add:
            return f"{command!r} already responds on {channel!r}"
        return f"{command!r} does not list {channel!r}"
    chans = r["channels"]
    if chans:
        return f"{command!r} responds on: {','.join(chans)}"
    return f"{command!r} channel restriction cleared (responds on any)"


async def _command_delay(ctx, rest):
    arg = rest.strip()
    if not arg:
        cur = ctx.bot.command_delay
        return (
            f"Command response delay: {cur:.1f}s"
            f"{' (disabled)' if cur == 0 else ''}. "
            "Set with: !adm command delay <seconds> (0 disables, 0.1–2.0)"
        )
    try:
        seconds = float(arg)
    except ValueError:
        return f"Invalid delay: {arg!r}. Use seconds (0, or 0.1–2.0)."
    try:
        res = await ctx.bot.mgmt.set_command_delay(seconds, **_actor(ctx))
    except MgmtError as e:
        return e.message
    d = res["delay"]
    if d == 0:
        return "Command response delay disabled"
    return f"Command response delay set to {d:.1f}s"


async def _command_retry(ctx, rest):
    arg = rest.strip()
    if not arg:
        cur = ctx.bot.channel_retry_max
        return (
            f"Channel no-repeat retries: {cur}"
            f"{' (disabled)' if cur == 0 else ''}. "
            "Set with: !adm command retry <count> (0 disables, max 5)"
        )
    try:
        count = int(arg)
    except ValueError:
        return f"Invalid retry count: {arg!r}. Use a whole number (0–5)."
    try:
        res = await ctx.bot.mgmt.set_channel_retry(count, **_actor(ctx))
    except MgmtError as e:
        return e.message
    n = res["retries"]
    if n == 0:
        return "Channel no-repeat retry disabled"
    return f"Channel no-repeat retries set to {n}"


_COMMAND_OPS = {
    "list": _command_list,
    "channel": _command_channel,
    "delay": _command_delay,
    "retry": _command_retry,
}


# ---------------------------------------------------------------------------
# radio.*
# 
async def _cmd_radio(ctx, rest):
    parts = rest.split(maxsplit=1)
    if not parts:
        return "Usage: !adm radio <pathhash> ..."
    op = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    handler = _RADIO_OPS.get(op)
    if not handler:
        return f"Unknown radio op: {op!r}"
    return await handler(ctx, args)


async def _radio_pathhash(ctx, args):
    # show or set the radio's path-hash width 
    mc = ctx.bot.mc
    if not args:
        try:
            mode = await mc.commands.get_path_hash_mode()
        except Exception as e:
            return f"Error querying path-hash mode: {e}"
        return f"out path-hash: {mode + 1} byte(s)/hop (mode {mode})"

    try:
        want_bytes = int(args.split()[0])
    except ValueError:
        return "Usage: !adm radio pathhash <1|2|3>"
    if want_bytes not in (1, 2, 3):
        return "path-hash width must be 1, 2, or 3 bytes"
    mode = want_bytes - 1
    try:
        ev = await mc.commands.set_path_hash_mode(mode)
    except Exception as e:
        return f"Error setting path-hash mode: {e}"
    t = getattr(ev.type, "name", "") if ev else ""
    if t != "OK":
        return f"radio rejected (response={t})"
    # Refresh the cached device_info value so !adm status reflects it.
    try:
        await ctx.bot.db.execute(
            "INSERT INTO device_info(key,value,last_updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "last_updated=excluded.last_updated",
            ("device_info.path_hash_mode", str(mode), int(time.time())),
        )
    except Exception:
        pass
    actor = (ctx.sender_pubkey or "").lower() or None
    await ctx.bot.audit_log(
        actor, ctx.sender_name, "radio.pathhash", None,
        f"{want_bytes}b (mode {mode})",
    )
    return f"out path-hash set to {want_bytes} byte(s)/hop (mode {mode})."


_RADIO_OPS = {
    "pathhash": _radio_pathhash,
}


async def _cmd_advert(ctx, rest):
    parts = rest.split()
    if not parts:
        return "Usage: !adm advert <flood|zero|interval <hours>>"
    mode = parts[0].lower()
    if mode == "interval":
        if len(parts) < 2:
            cur = ctx.bot.advert_interval_hours
            return (
                f"Flood advert interval: {cur}h"
                f"{' (disabled)' if cur == 0 else ''}. "
                "Set with: !adm advert interval <hours> (0 disables)"
            )
        try:
            hours = int(parts[1])
        except ValueError:
            return f"Invalid interval: {parts[1]!r}. Use a whole number of hours."
        try:
            res = await ctx.bot.mgmt.radio_set_advert_interval(hours, **_actor(ctx))
        except MgmtError as e:
            return e.message
        n = res["interval_hours"]
        if n == 0:
            return "Periodic flood advert disabled"
        return f"Flood advert every {n}h"
    if mode not in ("flood", "zero"):
        return f"Unknown advert mode: {mode!r}. Use 'flood', 'zero', or 'interval'."
    flood = mode == "flood"
    try:
        await ctx.bot.mgmt.send_advert(flood, **_actor(ctx))
    except MgmtError as e:
        return e.message
    return f"Sent {'flood' if flood else 'zero-hop'} advert"


async def _cmd_status(ctx, _):
    bot = ctx.bot
    name_row = await bot.db.fetchone(
        "SELECT value FROM device_info WHERE key='self_info.name'"
    )
    bat_row = await bot.db.fetchone(
        "SELECT value FROM device_info WHERE key='battery.level'"
    )
    radio_name = "?"
    if name_row:
        try:
            import json
            radio_name = json.loads(name_row["value"])
        except Exception:
            radio_name = name_row["value"]
    bat = "?"
    if bat_row:
        try:
            import json
            bat = json.loads(bat_row["value"])
        except Exception:
            bat = bat_row["value"]
    n_contacts = (
        await bot.db.fetchone("SELECT COUNT(*) AS n FROM contacts")
    )["n"]
    n_channels = (
        await bot.db.fetchone("SELECT COUNT(*) AS n FROM channels")
    )["n"]
    n_dms = (
        await bot.db.fetchone("SELECT COUNT(*) AS n FROM direct_messages")
    )["n"]
    n_chanmsgs = (
        await bot.db.fetchone("SELECT COUNT(*) AS n FROM channel_messages")
    )["n"]
    phm_row = await bot.db.fetchone(
        "SELECT value FROM device_info WHERE key='device_info.path_hash_mode'"
    )
    phm = "?"
    if phm_row and phm_row["value"] is not None:
        try:
            mode = int(json.loads(phm_row["value"]))
            phm = f"{mode + 1}b (mode {mode})"
        except Exception:
            phm = str(phm_row["value"])
    return [
        f"radio: {radio_name} battery={bat}",
        f"events: {bot.event_count}  commands: {len(bot.loader.commands)}",
        f"contacts: {n_contacts}  channels: {n_channels}",
        f"stored DMs: {n_dms}  channel msgs: {n_chanmsgs}",
        f"out path-hash: {phm}",
    ]


async def _cmd_reload(ctx, _):
    before, after, errors = ctx.bot.loader.reload_all()
    seeded = await ctx.bot.seed_command_configs()
    actor = (ctx.sender_pubkey or "").lower() or None
    await ctx.bot.audit_log(
        actor, ctx.sender_name, "reload", None,
        f"{before}->{after}, errors={len(errors)}, seeded={seeded}",
    )
    out = [f"Reloaded: {before} -> {after} commands ({seeded} new seeded)"]
    if errors:
        out.append(f"errors ({len(errors)}):")
        out.extend(f"  {e}" for e in errors)
    return out


async def _cmd_restart(ctx, _):
    bot = ctx.bot
    actor = (ctx.sender_pubkey or "").lower() or None
    await bot.audit_log(actor, ctx.sender_name, "restart", None, None)

    # defer the actual teardown so this acknowledgment has time to be
    # sent (and ideally ACKed) before the radio gets disconnected
    async def _trigger():
        await asyncio.sleep(5.0)
        bot.logger.info(
            "restart triggered by %s — tearing down",
            ctx.sender_name or "?",
        )
        bot.restart_requested = True
        bot.stop_event.set()

    asyncio.create_task(_trigger())
    return "Restarting — bot will reconnect in ~5 seconds"


async def _cmd_log(ctx, rest):
    try:
        n = int(rest.split()[0]) if rest.strip() else 10
    except ValueError:
        return "Usage: !adm log [N]"
    n = max(1, min(n, 50))
    rows = await ctx.bot.db.fetchall(
        "SELECT ts, actor_name, action, target, detail "
        "FROM bot_audit_log ORDER BY id DESC LIMIT ?",
        (n,),
    )
    if not rows:
        return "(empty audit log)"
    out = [f"Last {len(rows)} audit entries:"]
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"]))
        actor = r["actor_name"] or "?"
        tgt = r["target"] or ""
        det = r["detail"] or ""
        if tgt and len(tgt) > 12:
            tgt = tgt[:12]
        bits = [ts, actor, r["action"]]
        if tgt:
            bits.append(tgt)
        if det:
            bits.append(det)
        out.append("  " + " ".join(bits))
    return out


_SUBCOMMANDS = {
    "user":     _cmd_user,
    "group":    _cmd_group,
    "contacts": _cmd_contacts,
    "channel":  _cmd_channel,
    "command":  _cmd_command,
    "radio":    _cmd_radio,
    "advert":   _cmd_advert,
    "status":   _cmd_status,
    "reload":   _cmd_reload,
    "restart":  _cmd_restart,
    "log":      _cmd_log,
}
