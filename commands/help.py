#
# !help — list the commands you can run.
#
# hybrid command: on the allowed channels it only tells the sender to
# to send it to the bot as a DM instead, so the full help listing won't
# spam a channel. sent as a DM it returns the real listing, but only the
# commands available to the sender. this would be any public commands plus
# any granted by the sender's group memberships.
# 
# a command is listed only if the caller could actually run it — i.e. the
# same groups-only authorization check the dispatcher uses
# is_authorized_for_command on the command's own name.
#
# a command module may tune its visibility with optional attributes:
# 
#   HELP_HIDDEN = True          never list this command (e.g. internal
#                               admin harnesses)
#   HELP_DETAIL = [..lines..]   multi-line detail shown by '!help <cmd>';
#                              falls back to DESCRIPTION when absent

NAME = "help"
TRIGGERS = ["!help"]
DESCRIPTION = "List the commands you can run ('!help <cmd>' for detail)"
COOLDOWN_DEFAULT = 5
# the help command is allowed on these channels where it only tells
# the sender to send the command as a DM.
#
# DM_ONLY stays False so channel invocations reach handle().
# the DM-vs-channel split is done there.
#
ALLOWED_CHANNELS = ["#bot"]
ALLOW_DM = True
DM_ONLY = False


async def _visible(ctx, cs) -> bool:
    # True if the caller could actually run cs (matching the dispatcher's
    # enabled + authorization gates), and the command isn't HELP_HIDDEN
    mod = getattr(cs, "module", None)
    if mod is not None and getattr(mod, "HELP_HIDDEN", False):
        return False

    # effective enabled comes from command_config (the same source the
    # dispatcher trusts), falling back to the script default (enabled).
    row = await ctx.bot.db.fetchone(
        "SELECT enabled FROM command_config WHERE command=?",
        (cs.name,),
    )
    if row is not None and row["enabled"] is not None and not row["enabled"]:
        return False

    # authorization is groups-only: list the command iff the caller could
    # run it. the same gate the dispatcher applies, keyed on the command
    # name. (owners hold '*'; "public"-granted commands list for everyone.)
    return await ctx.bot.is_authorized_for_command(ctx.sender_pubkey, cs.name)


def _trigger(cs) -> str:
    return cs.triggers[0] if cs.triggers else f"!{cs.name}"


async def handle(ctx):
    # on channels, never send the help listing. tell them to DM instead.
    if not ctx.is_dm:
        return "Send !help to me in a DM."

    cmds = ctx.bot.loader.commands
    parts = ctx.message_text.split(maxsplit=1)
    arg = parts[1].strip().lower().lstrip("!") if len(parts) > 1 else ""

    if arg:
        cs = cmds.get(arg)
        if cs is None:
            for c in cmds.values():
                if any(t.lstrip("!") == arg for t in c.triggers):
                    cs = c
                    break
        # treat "unknown" and "not visible to you" identically so help
        # doesn't reveal the existence of commands the caller can't run.
        if cs is None or not await _visible(ctx, cs):
            return f"No such command: {arg!r}"
        mod = getattr(cs, "module", None)
        detail = getattr(mod, "HELP_DETAIL", None) if mod is not None else None
        if detail:
            return list(detail)
        return f"{_trigger(cs)} — {cs.description or '(no description)'}"

    visible = [cs for cs in cmds.values() if await _visible(ctx, cs)]
    if not visible:
        return "No commands available to you."
    visible.sort(key=lambda c: c.name)
    out = ["Commands you can run:"]
    for cs in visible:
        line = f"  {_trigger(cs)} — {cs.description or ''}".rstrip()
        out.append(line)
    out.append("Use '!help <cmd>' for detail.")
    return out
