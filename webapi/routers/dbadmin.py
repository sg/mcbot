"""Endpoints over the bot's tracked DB tables.

GET endpoints (Phase 2) read; the POST/PATCH/DELETE endpoints (Phase 4)
mutate through the shared management service so they enforce the same
invariants and write the same bot_audit_log rows as the '!adm' command."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from management import Management, MgmtError
from ..util import (
    actor_kwargs, get_bot, http_for_mgmt, require_auth,
    rows_to_list, row_to_dict,
)

router = APIRouter(prefix="/api", tags=["db-admin"])


def _patch_fields(body) -> dict:
    """Provided-only fields of a pydantic patch body (v1/v2 compatible)."""
    if hasattr(body, "model_dump"):
        return body.model_dump(exclude_unset=True)
    return body.dict(exclude_unset=True)


@router.get("/contacts")
async def list_contacts(
    bot=Depends(get_bot),
    q: str | None = Query(None, description="match adv_name or public_key"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    if q:
        pat = f"%{q.lower()}%"
        rows = await bot.db.fetchall(
            "SELECT * FROM contacts "
            "WHERE lower(adv_name) LIKE ? OR lower(public_key) LIKE ? "
            "ORDER BY last_advert DESC NULLS LAST LIMIT ? OFFSET ?",
            (pat, pat, limit, offset),
        )
    else:
        rows = await bot.db.fetchall(
            "SELECT * FROM contacts "
            "ORDER BY last_advert DESC NULLS LAST LIMIT ? OFFSET ?",
            (limit, offset),
        )
    total = (await bot.db.fetchone("SELECT COUNT(*) AS n FROM contacts"))["n"]
    return {"total": total, "items": rows_to_list(rows)}


@router.get("/channels")
async def list_channels(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT * FROM channels ORDER BY channel_idx"
    )
    return {"items": rows_to_list(rows)}


@router.get("/users")
async def list_users(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT u.pubkey, u.name, u.added_by, u.added_at, u.notes, "
        "  GROUP_CONCAT(ug.group_name, ',') AS groups "
        "FROM bot_users u "
        "LEFT JOIN bot_user_groups ug ON ug.pubkey = u.pubkey "
        "GROUP BY u.pubkey ORDER BY COALESCE(u.name, u.pubkey)"
    )
    items = rows_to_list(rows)
    for it in items:
        it["groups"] = it["groups"].split(",") if it["groups"] else []
    return {"items": items}


@router.get("/groups")
async def list_groups(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT g.name, g.description, g.is_system, g.created_at, g.all_users, "
        "  (SELECT COUNT(*) FROM bot_group_commands WHERE group_name=g.name) AS ncmds, "
        "  (SELECT COUNT(*) FROM bot_user_groups   WHERE group_name=g.name) AS nusers, "
        "  (SELECT GROUP_CONCAT(command, ',') FROM "
        "     (SELECT command FROM bot_group_commands "
        "      WHERE group_name=g.name ORDER BY command)) AS commands "
        "FROM bot_groups g ORDER BY g.name"
    )
    items = rows_to_list(rows)
    for it in items:
        it["commands"] = it["commands"].split(",") if it["commands"] else []
        it["all_users"] = bool(it.get("all_users"))
    return {"items": items}


@router.get("/groups/{name}")
async def group_detail(name: str, bot=Depends(get_bot)):
    g = await bot.db.fetchone(
        "SELECT * FROM bot_groups WHERE name=?", (name.lower(),)
    )
    if not g:
        return {"error": "no such group", "name": name}
    cmds = await bot.db.fetchall(
        "SELECT command FROM bot_group_commands WHERE group_name=? "
        "ORDER BY command", (name.lower(),)
    )
    members = await bot.db.fetchall(
        "SELECT u.pubkey, u.name FROM bot_user_groups ug "
        "JOIN bot_users u ON u.pubkey=ug.pubkey WHERE ug.group_name=? "
        "ORDER BY COALESCE(u.name, u.pubkey)", (name.lower(),)
    )
    return {
        "group": row_to_dict(g),
        "commands": [c["command"] for c in cmds],
        "members": rows_to_list(members),
    }


@router.get("/command-config")
async def list_command_config(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT * FROM command_config ORDER BY command"
    )
    return {"items": rows_to_list(rows)}


@router.get("/audit")
async def list_audit(
    bot=Depends(get_bot),
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None),
):
    if before_id:
        rows = await bot.db.fetchall(
            "SELECT * FROM bot_audit_log WHERE id < ? "
            "ORDER BY id DESC LIMIT ?", (before_id, limit),
        )
    else:
        rows = await bot.db.fetchall(
            "SELECT * FROM bot_audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
    return {"items": rows_to_list(rows)}


@router.get("/cooldowns")
async def list_cooldowns(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT * FROM command_cooldowns ORDER BY last_used_at DESC LIMIT 500"
    )
    return {"items": rows_to_list(rows)}


@router.get("/device-info")
async def list_device_info(bot=Depends(get_bot)):
    rows = await bot.db.fetchall(
        "SELECT * FROM device_info ORDER BY key"
    )
    return {"items": rows_to_list(rows)}


@router.get("/stats")
async def stats(bot=Depends(get_bot)):
    async def count(table):
        return (await bot.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"]
    return {
        "identity": {
            "pubkey": bot.my_pubkey,
            "name": getattr(bot, "my_name", None),
        },
        "event_count": bot.event_count,
        "commands_loaded": len(bot.loader.commands),
        "counts": {
            "contacts": await count("contacts"),
            "channels": await count("channels"),
            "direct_messages": await count("direct_messages"),
            "channel_messages": await count("channel_messages"),
            "received_packets": await count("received_packets"),
            "bot_users": await count("bot_users"),
            "bot_groups": await count("bot_groups"),
        },
    }


# ===========================================================================
# Write endpoints (Phase 4) — all delegate to bot.mgmt (invariants + audit).
# ===========================================================================
class GroupBody(BaseModel):
    group: str


class UserCreate(BaseModel):
    pubkey: str
    group: str


class UserRename(BaseModel):
    name: str


@router.post("/users")
async def user_create(
    body: UserCreate,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    # Friendly name comes from the contact record if we know it.
    row = await bot.db.fetchone(
        "SELECT adv_name FROM contacts WHERE public_key=?",
        (body.pubkey.lower(),),
    )
    name = row["adv_name"] if row else None
    try:
        return await bot.mgmt.user_create(
            body.pubkey, name, body.group, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.post("/users/{pubkey}/groups")
async def user_add_group(
    pubkey: str, body: GroupBody,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    # Pull a friendly name from contacts if we know one (won't overwrite).
    row = await bot.db.fetchone(
        "SELECT adv_name FROM contacts WHERE public_key=?", (pubkey.lower(),)
    )
    name = row["adv_name"] if row else None
    try:
        return await bot.mgmt.user_add_to_group(
            pubkey, name, body.group, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/users/{pubkey}/groups/{group}")
async def user_remove_group(
    pubkey: str, group: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.user_remove_from_group(
            pubkey, group, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.patch("/users/{pubkey}")
async def user_rename(
    pubkey: str, body: UserRename,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    return await bot.mgmt.user_rename(
        pubkey, body.name, **actor_kwargs(identity)
    )


@router.delete("/users/{pubkey}")
async def user_delete(
    pubkey: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.user_delete(pubkey, **actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.post("/users/{pubkey}/block")
async def user_block(
    pubkey: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    row = await bot.db.fetchone(
        "SELECT adv_name FROM contacts WHERE public_key=?", (pubkey.lower(),)
    )
    name = row["adv_name"] if row else None
    try:
        return await bot.mgmt.user_block(
            pubkey, name, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/users/{pubkey}/block")
async def user_unblock(
    pubkey: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    return await bot.mgmt.user_unblock(pubkey, **actor_kwargs(identity))


class GroupCreate(BaseModel):
    name: str
    commands: list[str] | None = None


class GrantBody(BaseModel):
    command: str


@router.post("/groups")
async def group_add(
    body: GroupCreate,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    return await bot.mgmt.group_add(
        body.name, body.commands or [], **actor_kwargs(identity)
    )


@router.delete("/groups/{name}")
async def group_delete(
    name: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.group_delete(name, **actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.post("/groups/{name}/commands")
async def group_grant(
    name: str, body: GrantBody,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.group_grant(
            name, body.command, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/groups/{name}/commands/{command}")
async def group_revoke(
    name: str, command: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.group_revoke(
            name, command, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.post("/groups/{name}/all-users")
async def group_allow_all(
    name: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.group_set_all_users(
            name, True, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/groups/{name}/all-users")
async def group_restrict(
    name: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.group_set_all_users(
            name, False, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


class ChannelCreate(BaseModel):
    name: str
    key: str | None = None


@router.post("/channels")
async def channel_add(
    body: ChannelCreate,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        secret = Management.channel_secret(body.name, body.key or "")
        return await bot.mgmt.channel_add(
            body.name, secret, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/channels/{name}")
async def channel_remove(
    name: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.channel_remove(name, **actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


class CommandConfigPatch(BaseModel):
    enabled: bool | None = None
    cooldown_seconds: int | None = None
    allow_dm: bool | None = None
    dm_only: bool | None = None
    allowed_channels: list[str] | None = None


@router.patch("/command-config/{command}")
async def command_config_update(
    command: str, body: CommandConfigPatch,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    fields = _patch_fields(body)
    try:
        return await bot.mgmt.command_config_update(
            command, fields, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.delete("/contacts/{pubkey}")
async def contact_delete(
    pubkey: str,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.contact_delete(pubkey, **actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


class AdvertBody(BaseModel):
    flood: bool = False


@router.post("/radio/advert")
async def radio_advert(
    body: AdvertBody,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.send_advert(body.flood, **actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


@router.get("/radio/contacts-status")
async def radio_contacts_status(
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    # auth required (unlike other GETs): this actively drives the radio
    # (full contact dump) rather than reading the local DB.
    try:
        return await bot.mgmt.radio_contacts_status(**actor_kwargs(identity))
    except MgmtError as e:
        raise http_for_mgmt(e)


class EvictBody(BaseModel):
    count: int | None = None
    dry_run: bool = False


@router.post("/radio/evict-contacts")
async def radio_evict_contacts(
    body: EvictBody,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.radio_evict_contacts(
            count=body.count, dry_run=body.dry_run, **actor_kwargs(identity)
        )
    except MgmtError as e:
        raise http_for_mgmt(e)


class EvictPolicyBody(BaseModel):
    enabled: bool | None = None
    headroom: int | None = None


@router.post("/radio/contacts-policy")
async def radio_contacts_policy(
    body: EvictPolicyBody,
    bot=Depends(get_bot), identity: str = Depends(require_auth),
):
    try:
        return await bot.mgmt.radio_set_evict_policy(
            enabled=body.enabled, headroom=body.headroom,
            **actor_kwargs(identity),
        )
    except MgmtError as e:
        raise http_for_mgmt(e)
