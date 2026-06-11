"""Shared management service for mcbot.

Owns the *invariants*, *mutations*, and *audit-log writes* for the bot's
administrative state (users, groups, channels, command config) and for
manually-originated outbound messages. Both entry points go through it:

  - commands/adm.py  — the in-mesh '!adm' command (DM-only)
  - webapi/          — the REST/WebSocket admin API

Keeping the rules in one place means '!adm' and the API can't drift on
things like "refuse to remove the last owner" or "system groups can't be
deleted", and every state change lands in bot_audit_log exactly once.

Callers are responsible for *authorization* (who may act) and for turning
results into their own presentation (mesh text vs. JSON). This service
assumes the caller is already authorized; it only enforces data invariants.

Invariant violations raise MgmtError; the human-readable .message is safe
to surface directly. Methods that are no-ops (e.g. removing a membership
that didn't exist) return a small dict with a boolean flag rather than
raising, so callers can phrase "was not a member" without try/except.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional


class MgmtError(Exception):
    """A management invariant was violated. `.message` is user-safe.

    `code` lets API callers map to an HTTP status without string-matching:
      not_found -> 404, conflict -> 409, refused/invalid -> 400.
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def _actor(actor_pubkey: Optional[str]) -> Optional[str]:
    return (actor_pubkey or "").lower() or None


class Management:
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def _audit(self, actor_pubkey, actor_name, action, target, detail):
        await self.bot.audit_log(
            _actor(actor_pubkey), actor_name, action, target, detail
        )

    # ==================================================================
    # Users
    # ==================================================================
    async def user_add_to_group(
        self, pubkey: str, name: Optional[str], group: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        group = group.lower()
        if group == "blocked":
            raise MgmtError(
                "Use block() to put a user in the blocked group", "refused"
            )
        if group == "public":
            raise MgmtError(
                "'public' is a command-list group, not for user membership",
                "refused",
            )
        exists = await self.db.fetchone(
            "SELECT 1 FROM bot_groups WHERE name=?", (group,)
        )
        if not exists:
            raise MgmtError(f"unknown group: {group}", "not_found")
        pk = pubkey.lower()
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO bot_users(pubkey, name, added_by, added_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(pubkey) DO UPDATE SET "
            "name=COALESCE(excluded.name, bot_users.name)",
            (pk, name, _actor(actor_pubkey), now),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_user_groups(pubkey, group_name) "
            "VALUES(?, ?)",
            (pk, group),
        )
        await self._audit(
            actor_pubkey, actor_name, "user.add", pk, f"group={group}"
        )
        return {"pubkey": pk, "group": group}

    async def user_create(
        self, pubkey: str, name: Optional[str], group: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Create a brand-new bot user from a contact, with one initial
        group. Unlike user_add_to_group (which upserts), this *fails* if a
        bot_users row already exists for the pubkey, so the UI can refuse to
        create a duplicate."""
        pk = pubkey.lower()
        existing = await self.db.fetchone(
            "SELECT 1 FROM bot_users WHERE pubkey=?", (pk,)
        )
        if existing:
            raise MgmtError(
                f"a bot user already exists for {pk[:12]}", "conflict"
            )
        # Reuse the membership logic (group validation + the blocked/public
        # guards + audit); the row doesn't exist yet, so its INSERT creates it.
        return await self.user_add_to_group(
            pk, name, group, actor_pubkey=actor_pubkey, actor_name=actor_name
        )

    async def _last_owner(self, pubkey: str) -> bool:
        """True if `pubkey` is currently the sole member of the owner group."""
        is_owner = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups WHERE pubkey=? AND group_name='owner'",
            (pubkey,),
        )
        if not is_owner:
            return False
        n = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM bot_user_groups WHERE group_name='owner'"
        )
        return bool(n and n["n"] <= 1)

    async def user_remove_from_group(
        self, pubkey: str, group: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        group = group.lower()
        pk = pubkey.lower()
        if group == "owner" and await self._last_owner(pk):
            raise MgmtError("Refusing to remove the last owner", "refused")
        cur = await self.db.execute(
            "DELETE FROM bot_user_groups WHERE pubkey=? AND group_name=?",
            (pk, group),
        )
        if cur.rowcount == 0:
            return {"removed": False, "group": group}
        await self._audit(
            actor_pubkey, actor_name, "user.remove", pk, f"group={group}"
        )
        return {"removed": True, "group": group}

    async def user_delete(
        self, pubkey: str, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        pk = pubkey.lower()
        if await self._last_owner(pk):
            raise MgmtError("Refusing to delete the last owner", "refused")
        cur = await self.db.execute(
            "DELETE FROM bot_users WHERE pubkey=?", (pk,)
        )
        if cur.rowcount == 0:
            return {"deleted": False}
        await self._audit(actor_pubkey, actor_name, "user.delete", pk, None)
        return {"deleted": True}

    async def user_rename(
        self, pubkey: str, new_alias: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        pk = pubkey.lower()
        cur = await self.db.execute(
            "UPDATE bot_users SET name=? WHERE pubkey=?", (new_alias, pk)
        )
        if cur.rowcount == 0:
            return {"renamed": False}
        await self._audit(
            actor_pubkey, actor_name, "user.rename", pk, f"alias={new_alias}"
        )
        return {"renamed": True, "name": new_alias}

    async def user_block(
        self, pubkey: str, name: Optional[str],
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        pk = pubkey.lower()
        is_owner = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups WHERE pubkey=? AND group_name='owner'",
            (pk,),
        )
        if is_owner:
            raise MgmtError("Refusing to block an owner", "refused")
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO bot_users(pubkey, name, added_by, added_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(pubkey) DO UPDATE SET "
            "name=COALESCE(excluded.name, bot_users.name)",
            (pk, name, _actor(actor_pubkey), now),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_user_groups(pubkey, group_name) "
            "VALUES(?, 'blocked')",
            (pk,),
        )
        await self._audit(actor_pubkey, actor_name, "user.block", pk, None)
        return {"blocked": True}

    async def user_unblock(
        self, pubkey: str, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        pk = pubkey.lower()
        cur = await self.db.execute(
            "DELETE FROM bot_user_groups WHERE pubkey=? AND group_name='blocked'",
            (pk,),
        )
        if cur.rowcount == 0:
            return {"unblocked": False}
        await self._audit(actor_pubkey, actor_name, "user.unblock", pk, None)
        return {"unblocked": True}

    # ==================================================================
    # Groups
    # ==================================================================
    async def group_add(
        self, name: str, commands: Optional[list[str]] = None,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        name = name.lower()
        now = int(time.time())
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_groups(name, created_at) VALUES(?, ?)",
            (name, now),
        )
        added = 0
        for cmd in [c.strip() for c in (commands or []) if c.strip()]:
            cur = await self.db.execute(
                "INSERT OR IGNORE INTO bot_group_commands(group_name, command) "
                "VALUES(?, ?)",
                (name, cmd),
            )
            if cur.rowcount:
                added += 1
        await self._audit(
            actor_pubkey, actor_name, "group.add", name, f"cmds=+{added}"
        )
        return {"name": name, "added": added}

    async def group_grant(
        self, name: str, command: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        name = name.lower()
        exists = await self.db.fetchone(
            "SELECT 1 FROM bot_groups WHERE name=?", (name,)
        )
        if not exists:
            raise MgmtError(f"unknown group: {name}", "not_found")
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO bot_group_commands(group_name, command) "
            "VALUES(?, ?)",
            (name, command),
        )
        if cur.rowcount == 0:
            return {"granted": False, "command": command}
        await self._audit(
            actor_pubkey, actor_name, "group.grant", name, f"cmd={command}"
        )
        return {"granted": True, "command": command}

    async def group_revoke(
        self, name: str, command: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        name = name.lower()
        if name == "owner" and command == "*":
            raise MgmtError(
                "Refusing to revoke '*' from the owner group", "refused"
            )
        cur = await self.db.execute(
            "DELETE FROM bot_group_commands WHERE group_name=? AND command=?",
            (name, command),
        )
        if cur.rowcount == 0:
            return {"revoked": False, "command": command}
        await self._audit(
            actor_pubkey, actor_name, "group.revoke", name, f"cmd={command}"
        )
        return {"revoked": True, "command": command}

    async def group_set_all_users(
        self, name: str, enabled: bool,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Toggle a group's all-users flag (the '*' membership): when on,
        every user is implicitly a member, so commands granted to the group
        are open to all and the group shows in !whoami. Refused for the
        'blocked' group (a block is always explicit)."""
        name = name.lower()
        row = await self.db.fetchone(
            "SELECT 1 FROM bot_groups WHERE name=?", (name,)
        )
        if not row:
            raise MgmtError(f"unknown group: {name}", "not_found")
        if name == "blocked":
            raise MgmtError(
                "the blocked group can't include all users", "refused"
            )
        await self.db.execute(
            "UPDATE bot_groups SET all_users=? WHERE name=?",
            (1 if enabled else 0, name),
        )
        await self._audit(
            actor_pubkey, actor_name, "group.all_users", name,
            "on" if enabled else "off",
        )
        return {"name": name, "all_users": bool(enabled)}

    async def group_delete(
        self, name: str, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        name = name.lower()
        row = await self.db.fetchone(
            "SELECT is_system FROM bot_groups WHERE name=?", (name,)
        )
        if not row:
            raise MgmtError(f"unknown group: {name}", "not_found")
        if row["is_system"]:
            raise MgmtError(
                f"Refusing to delete system group {name!r}", "refused"
            )
        await self.db.execute("DELETE FROM bot_groups WHERE name=?", (name,))
        await self._audit(actor_pubkey, actor_name, "group.delete", name, None)
        return {"deleted": True, "name": name}

    # ==================================================================
    # Channels
    # ==================================================================
    @staticmethod
    def channel_secret(name: str, key_hex: str = "") -> bytes:
        """Resolve a channel name + optional hex key into a 16-byte secret.

        '#'-prefixed names auto-derive SHA256(name)[:16] and must NOT be
        given an explicit key; any other name requires a 16-byte hex key.
        Raises MgmtError on a bad combination — shared by '!adm channel add'
        and the API so both validate identically.
        """
        if name.startswith("#"):
            if key_hex:
                raise MgmtError(
                    f"'{name}' starts with '#' so the key is auto-derived; "
                    "don't supply one",
                    "invalid",
                )
            return hashlib.sha256(name.encode("utf-8")).digest()[:16]
        if not key_hex:
            raise MgmtError(
                f"{name!r} is not a '#' channel — supply a 16-byte hex key",
                "invalid",
            )
        try:
            secret = bytes.fromhex(key_hex)
        except ValueError:
            raise MgmtError(f"Invalid hex key: {key_hex!r}", "invalid")
        if len(secret) != 16:
            raise MgmtError(
                "Channel key must be exactly 16 bytes (32 hex chars)",
                "invalid",
            )
        return secret

    async def channel_add(
        self, name: str, secret: bytes,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        idx, err = await self.bot.add_channel(name, secret)
        if err:
            raise MgmtError(f"Add failed: {err}", "conflict")
        await self._audit(
            actor_pubkey, actor_name, "channel.add", name, f"idx={idx}"
        )
        return {"name": name, "idx": idx}

    async def channel_remove(
        self, name: str, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        idx, err = await self.bot.remove_channel(name)
        if err:
            raise MgmtError(f"Remove failed: {err}", "not_found")
        await self._audit(
            actor_pubkey, actor_name, "channel.remove", name, f"idx={idx}"
        )
        return {"name": name, "idx": idx}

    # ==================================================================
    # Command config
    # ==================================================================
    _CFG_BOOL = ("enabled", "allow_dm", "dm_only")
    _CFG_INT = ("cooldown_seconds",)

    async def command_config_update(
        self, command: str, fields: dict[str, Any],
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Patch a command_config row. Accepts any of the bool flags,
        cooldown_seconds (int), and allowed_channels (list[str] -> JSON, or
        None/[] to clear the restriction). Unknown keys are rejected."""
        row = await self.db.fetchone(
            "SELECT command FROM command_config WHERE command=?", (command,)
        )
        if not row:
            raise MgmtError(
                f"no config row for command {command!r} (is it loaded?)",
                "not_found",
            )
        sets, params, changed = [], [], []
        for k, v in fields.items():
            if k in self._CFG_BOOL:
                sets.append(f"{k}=?")
                params.append(1 if v else 0)
            elif k in self._CFG_INT:
                sets.append(f"{k}=?")
                params.append(int(v))
            elif k == "allowed_channels":
                if v in (None, []):
                    val = None
                elif isinstance(v, list):
                    val = json.dumps([str(c) for c in v])
                else:
                    raise MgmtError(
                        "allowed_channels must be a list or null", "invalid"
                    )
                sets.append("allowed_channels=?")
                params.append(val)
            else:
                raise MgmtError(f"unknown config field: {k}", "invalid")
            changed.append(k)
        if not sets:
            raise MgmtError("no fields to update", "invalid")
        params.append(command)
        await self.db.execute(
            f"UPDATE command_config SET {', '.join(sets)} WHERE command=?",
            tuple(params),
        )
        await self._audit(
            actor_pubkey, actor_name, "command.config", command,
            ",".join(sorted(changed)),
        )
        updated = await self.db.fetchone(
            "SELECT * FROM command_config WHERE command=?", (command,)
        )
        return {k: updated[k] for k in updated.keys()}

    async def command_channel_change(
        self, command: str, channel: str, add: bool,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Add/remove a single channel from a command's allowed_channels
        list (read-modify-write). Empty list clears the restriction."""
        row = await self.db.fetchone(
            "SELECT allowed_channels FROM command_config WHERE command=?",
            (command,),
        )
        if row is None:
            raise MgmtError(
                f"no config row for command {command!r} (is it loaded?)",
                "not_found",
            )
        chans: list[str] = []
        if row["allowed_channels"]:
            try:
                chans = json.loads(row["allowed_channels"])
            except Exception:
                chans = [
                    s.strip()
                    for s in str(row["allowed_channels"]).split(",")
                    if s.strip()
                ]
        if not isinstance(chans, list):
            chans = []

        if add:
            if channel in chans:
                return {"changed": False, "channels": chans}
            chans.append(channel)
        else:
            if channel not in chans:
                return {"changed": False, "channels": chans}
            chans.remove(channel)

        new_value = json.dumps(chans) if chans else None
        await self.db.execute(
            "UPDATE command_config SET allowed_channels=? WHERE command=?",
            (new_value, command),
        )
        verb = "add" if add else "remove"
        await self._audit(
            actor_pubkey, actor_name, f"command.chan.{verb}", command, channel
        )
        return {"changed": True, "channels": chans}

    # ==================================================================
    # Outbound messages
    # ==================================================================
    async def send_channel(
        self, channel_idx: int, text: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Send a manually-composed message to a channel, record it as an
        outgoing row, publish it to the live feed, and audit it."""
        text = (text or "").strip()
        if not text:
            raise MgmtError("message text is required", "invalid")
        ch = await self.db.fetchone(
            "SELECT name FROM channels WHERE channel_idx=?", (channel_idx,)
        )
        if not ch:
            raise MgmtError(f"unknown channel idx {channel_idx}", "not_found")
        ch_name = ch["name"]
        ev = await self.bot.send_channel_text(channel_idx, text)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            raise MgmtError("radio rejected the channel send", "conflict")
        now = int(time.time())
        me = getattr(self.bot, "my_name", None) or "(me)"
        cur = await self.db.execute(
            "INSERT INTO channel_messages "
            "(channel_idx, channel_name, sender_name, sender_pubkey, text, "
            " received_at, is_outgoing) VALUES (?,?,?,?,?,?,1)",
            (channel_idx, ch_name, me, self.bot.my_pubkey, text, now),
        )
        mid = cur.lastrowid
        await self.bot._trim_channel_messages(
            channel_idx, self.bot.cfg.max_channel_messages
        )
        if self.bot.web_message_feed.has_subscribers():
            self.bot.web_message_feed.publish({
                "kind": "channel",
                "id": mid,
                "channel_idx": channel_idx,
                "channel_name": ch_name,
                "sender_name": me,
                "sender_pubkey": self.bot.my_pubkey,
                "text": text,
                "received_at": now,
                "is_outgoing": 1,
            })
        await self._audit(
            actor_pubkey, actor_name, "send.channel",
            f"ch={channel_idx}", text[:60],
        )
        return {"id": mid, "channel_idx": channel_idx}

    # ==================================================================
    # Contacts
    # ==================================================================
    async def contact_delete(
        self, pubkey: str, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Remove a contact from the radio and the local contacts table.
        Returns the deleted contact's name (if any) so callers can echo it."""
        pk = pubkey.lower()
        row = await self.db.fetchone(
            "SELECT adv_name FROM contacts WHERE public_key=?", (pk,)
        )
        if not row:
            raise MgmtError(
                f"no contact with pubkey {pk[:12]}", "not_found"
            )
        name = row["adv_name"]
        ev = await self.bot.remove_contact_remote(pk)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            raise MgmtError("radio rejected the remove", "conflict")
        await self.db.execute(
            "DELETE FROM contacts WHERE public_key=?", (pk,)
        )
        await self._audit(
            actor_pubkey, actor_name, "contact.delete", pk, name
        )
        return {"deleted": True, "pubkey": pk, "name": name}

    async def send_advert(
        self, flood: bool,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Originate an advertisement broadcast (zero-hop or flood). Audited
        as 'radio.advert' with detail 'flood' or 'zero'."""
        ev = await self.bot.send_advert(flood)
        ok = ev is not None and getattr(ev.type, "name", "") != "ERROR"
        if not ok:
            raise MgmtError("radio rejected the advert", "conflict")
        await self._audit(
            actor_pubkey, actor_name, "radio.advert", None,
            "flood" if flood else "zero",
        )
        return {"flood": flood, "ok": ok}

    # ==================================================================
    # Radio contact-table rollover
    # ==================================================================
    def _evict_policy(self) -> dict:
        import mcbot  # local import avoids any import-time cycle
        bot = self.bot
        return {
            "enabled": bot.evict_enabled,
            "headroom": bot.evict_headroom,
            "protect_types": sorted(
                mcbot.CONTACT_TYPE_NAMES.get(t, str(t))
                for t in bot.cfg.radio_evict_protect_types
            ),
            "max_per_run": bot.cfg.radio_evict_max_per_run,
            "min_interval": bot.cfg.radio_evict_min_interval,
        }

    async def radio_contacts_status(
        self, *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Report the radio contact table's usage + current eviction policy.
        Read-only (drives a fresh radio dump via a dry-run eviction); not
        audited. Includes the raw firmware autoadd byte for diagnostics."""
        try:
            preview = await self.bot.evict_radio_contacts(
                target_free=self.bot.evict_headroom, dry_run=True
            )
        except Exception as e:
            raise MgmtError(f"could not read radio contacts: {e}", "conflict")
        autoadd_raw = None
        try:  # best-effort; absent on older firmware
            ev = await self.bot.mc.commands.get_autoadd_config()
            if ev and isinstance(ev.payload, dict):
                autoadd_raw = ev.payload.get("config")
        except Exception:
            pass
        return {
            "used": preview["used"],
            "max": preview["max"],
            "free": preview["free_before"],
            "headroom_target": preview["target_free"],
            "protected": preview["protected"],
            "eligible": preview["eligible"],
            "would_evict": len(preview["evicted"]),
            "autoadd_raw": autoadd_raw,
            "policy": self._evict_policy(),
        }

    async def radio_evict_contacts(
        self, count=None, dry_run=False,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Evict stale contacts from the radio now. With `count`, remove up to
        that many; otherwise free slots up to the configured headroom. Audited
        as 'radio.evict' (skipped for dry runs)."""
        if count is not None:
            try:
                count = int(count)
            except (TypeError, ValueError):
                raise MgmtError("count must be an integer", "invalid")
            if count < 1:
                raise MgmtError("count must be >= 1", "invalid")
        try:
            res = await self.bot.evict_radio_contacts(
                count=count, dry_run=dry_run,
            )
        except Exception as e:
            raise MgmtError(f"radio eviction failed: {e}", "conflict")
        if not dry_run:
            await self._audit(
                actor_pubkey, actor_name, "radio.evict", None,
                f"evicted={len(res['evicted'])} failed={res['failed']} "
                f"shortfall={res['shortfall']}",
            )
        return res

    async def radio_set_evict_policy(
        self, enabled=None, headroom=None,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Change the runtime eviction policy (auto on/off, headroom). Runtime
        only — mcbot.conf is the source of truth at next startup. Audited as
        'radio.evict_policy'."""
        if enabled is not None:
            self.bot.evict_enabled = bool(enabled)
        if headroom is not None:
            try:
                headroom = int(headroom)
            except (TypeError, ValueError):
                raise MgmtError("headroom must be an integer", "invalid")
            if not (1 <= headroom <= 100):
                raise MgmtError("headroom must be between 1 and 100", "invalid")
            self.bot.evict_headroom = headroom
        await self._audit(
            actor_pubkey, actor_name, "radio.evict_policy", None,
            f"enabled={self.bot.evict_enabled} headroom={self.bot.evict_headroom}",
        )
        return self._evict_policy()

    async def send_dm(
        self, pubkey: str, text: str,
        *, actor_pubkey=None, actor_name=None,
    ) -> dict:
        """Send a manually-composed DM, record it as an outgoing row keyed
        by the counterparty pubkey (so it threads with that conversation),
        publish to the live feed, and audit it."""
        text = (text or "").strip()
        if not text:
            raise MgmtError("message text is required", "invalid")
        pk = pubkey.lower()
        row = await self.db.fetchone(
            "SELECT adv_name FROM contacts WHERE public_key=?", (pk,)
        )
        peer_name = row["adv_name"] if row else None
        ev = await self.bot.send_dm_to(pk, text)
        acked = ev is not None and getattr(ev.type, "name", "") != "ERROR"
        now = int(time.time())
        # sender_pubkey holds the *counterparty* on outgoing rows so a
        # conversation filtered by that pubkey shows both directions.
        cur = await self.db.execute(
            "INSERT INTO direct_messages "
            "(sender_pubkey, sender_name, text, received_at, is_outgoing) "
            "VALUES (?,?,?,?,1)",
            (pk, peer_name, text, now),
        )
        mid = cur.lastrowid
        await self.bot._trim_global("direct_messages", self.bot.cfg.max_dms)
        if self.bot.web_message_feed.has_subscribers():
            self.bot.web_message_feed.publish({
                "kind": "dm",
                "id": mid,
                "sender_pubkey": pk,
                "sender_name": peer_name,
                "text": text,
                "received_at": now,
                "is_outgoing": 1,
            })
        await self._audit(
            actor_pubkey, actor_name, "send.dm", pk, text[:60]
        )
        return {"id": mid, "pubkey": pk, "acked": acked}
