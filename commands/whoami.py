# !whoami — show your pubkey, name, and group memberships.
#
# open to anyone. works in DM and on the allowed channels.
#
# caveat: in a channel the identity shown is derived from the message's
# (unauthenticated) sender prefix. anyone with the channel key can claim
# any name. only a DM cryptographically binds the name/pubkey to a private
# key via ECDH, so trust the channel form accordingly.
#

NAME = "whoami"
TRIGGERS = ["!whoami"]
DESCRIPTION = "Show your pubkey, name, and group memberships"
COOLDOWN_DEFAULT = 10
ALLOWED_CHANNELS = ["#bot", "testing"]
ALLOW_DM = True
DM_ONLY = False


async def handle(ctx):
    pk = ctx.sender_pubkey or ""
    name = ctx.sender_name or "?"
    if not pk:
        return f"pubkey: unresolved (prefix={ctx.sender_pubkey_prefix})"
    # effective membership: explicit groups + every all-users group (e.g.
    # 'public'), so an unaffiliated user still sees the groups they're in.
    groups = await ctx.bot.effective_groups_for_user(pk)
    grps = ", ".join(groups) if groups else "(none)"
    # abbreviate the 64-hex pubkey to first6...last6 so the whole reply
    # fits in a single mesh message.
    pk_short = f"{pk[:6]}...{pk[-6:]}" if len(pk) > 12 else pk
    return f"name: {name} | pubkey: {pk_short} | groups: {grps}"
