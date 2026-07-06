"""MCBot — the MeshCore companion-radio bot core."""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from meshcore import MeshCore, EventType
except ImportError:
    sys.stderr.write(
        "meshcore library not installed. Run: pip install meshcore\n"
    )
    sys.exit(1)

from config import Config, effective_log_level, setup_logging
from crypto import (
    DecryptedDM,
    decrypt_direct_message,
    decrypt_group_text,
    derive_public_key,
    derive_shared_secret,
    try_decrypt_dm,
)
from db import DB
from eviction import select_eviction_victims
from plugins import CommandContext, CommandLoader
from protocol import (
    CHANNEL_SENDER_RE,
    PACKET_TYPE_MAP,
    PayloadType,
    format_path,
    parse_packet_envelope,
)
from settings import SETTINGS

@dataclass
class RepeatWatch:
    # one pending "did a repeater rebroadcast this?" watch, created when the
    # bot sends a DM or channel message. correlation is by message *text*
    # (the on-air pkt_hash differs per send retry, and we never see our own
    # TX), so text is the primary key; (pkt_hash, path) dedupes individual
    # heard frames so each distinct repeater is counted once.
    kind: str                              # "dm" | "channel"
    text: str
    dest_pubkey: Optional[str] = None      # lowercase hex (dm)
    dest_byte: Optional[int] = None        # recipient pubkey first byte (dm)
    channel_idx: Optional[int] = None      # (channel)
    disp_name: str = ""
    # how the send was routed, derived from the contact's out_path_len:
    # "flood" (-1), "direct_0hop" (0, neighbor — no repeater can rebroadcast),
    # "direct_multihop" (>=1), or "unknown". Used so a 0-hop direct DM that
    # legitimately has no repeater is labelled DIRECT_0HOP, not NO_REPEAT.
    route_mode: str = "unknown"
    registered_at: float = 0.0             # monotonic, at send start
    send_completed_at: Optional[float] = None
    # message timestamp used on the send (epoch secs). reused verbatim on a
    # no-repeat retry so the retransmit is byte-identical (MeshCore de-dups by
    # SHA256(timestamp||text)). channel sends only.
    send_timestamp: Optional[int] = None
    retries_left: int = 0                   # remaining no-repeat resends (channel)
    seen_frames: set = field(default_factory=set)   # {(pkt_hash, path_hex)}
    repeat_count: int = 0
    repeater_keys: set = field(default_factory=set)  # repeater path evidence
    timer_task: Any = None                 # asyncio.Task
    done: bool = False


# ---------------------------------------------------------------------------
# The bot
#
class MCBot:
    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.logger = log
        self.db = DB(cfg.db_path, log)
        self.loader = CommandLoader(cfg.commands_dir, log)
        self.mc: Optional[MeshCore] = None
        self.stop_event = asyncio.Event()
        self.event_count = 0
        self._subs: list = []
        self._log_channel_set: Optional[set] = None
        self._contacts_dirty = False
        self.my_pubkey: Optional[str] = None
        self.my_pubkey_byte: Optional[int] = None
        # set true by !adm restart to make amain() loop and rebuild a
        # fresh MCBot instance rather than exiting after shutdown.
        self.restart_requested: bool = False
        # set when the startup connect fails and auto_reconnect is on, so
        # amain() retries with backoff instead of exiting the process.
        self.connect_failed: bool = False
        self._reconnect_restart_armed = False
        # startup queue drain: messages the radio queued while the bot was
        # offline are fetched between start_auto_message_fetching() and the
        # first NO_MORE_MSGS. True outside that window (default True so
        # directly-driven bots, e.g. in tests, treat messages as live).
        self._radio_queue_drained: bool = True
        self._drain_deadline: float = 0.0
        self._queued_cmds_skipped = 0
        self.my_private_key: Optional[bytes] = None  # 64 bytes
        self.my_public_key_bytes: Optional[bytes] = None  # 32 bytes
        # web admin UI/API (uvicorn server + its serve() task), or None
        self._web_server = None
        self._web_task = None
        # live fan-out feeds for the web UI (cheap no-ops with no subscribers).
        from webapi.broadcast import Broadcaster
        self.web_packet_feed = Broadcaster()
        self.web_message_feed = Broadcaster()
        # shared mutation/invariant/audit service ('!adm' + web API).
        from management import Management
        self.mgmt = Management(self)
        # channel_hash_byte -> (idx, name, 16-byte secret)
        self.channels_by_hash: dict[int, tuple[int, str, bytes]] = {}
        # pkt_hash dedupe (same-attempt retransmissions via different paths)
        self._recent_pkt_hashes: deque = deque(maxlen=200)
        self._recent_pkt_hash_set: set[int] = set()
        # dedupe any DMs with the same sender_pubkey + sender_timestamp combo.
        # since meshcore increments an attempt counter, the ciphertext and pkt_hash
        # is different for each retry, but timestamp + sender stays the same.
        # without this we'd run the same command up to 3 times.
        self._recent_msg_keys: deque = deque(maxlen=200)
        self._recent_msg_key_set: set[tuple] = set()
        # dedupe for channel messages on channel_idx, sender_timestamp, text.
        # a channel whose key is programmed into the radio is decrypted twice:
        # once by the radio (delivered via CHANNEL_MSG_RECV) and once by this
        # script using RX_LOG_DATA. without the dedupe we'd store and respond
        # to commands on each message twice.
        self._recent_chan_keys: deque = deque(maxlen=256)
        self._recent_chan_key_set: set[tuple] = set()
        # repeater-repeat tracking: pending watches for messages we've sent.
        # the firehose matches heard rebroadcasts against these; the decrypt
        # path (_handle_inbound_channel) also consults them to suppress
        # re-ingesting our own repeated channel message as a phantom inbound.
        self._repeat_watches: list[RepeatWatch] = []
        # radio contact-table rollover. evict_enabled/evict_headroom are the
        # runtime-mutable policy (seeded from config; the web UI can change
        # them until restart). The lock serializes runs; _last_auto_evict
        # debounces CONTACTS_FULL-triggered runs.
        self.evict_enabled: bool = cfg.radio_evict_enabled
        self.evict_headroom: int = cfg.radio_evict_headroom
        self._evict_lock = asyncio.Lock()
        self._last_auto_evict: float = 0.0  # monotonic
        # registry-managed runtime settings (settings.SETTINGS): seeded from
        # cfg here, then DB-authoritative once load_runtime_settings() runs.
        for _s in SETTINGS.values():
            setattr(self, _s.key, getattr(cfg, _s.key))
        # _last_flood_advert (monotonic) anchors the periodic-advert schedule;
        # the periodic task measures advert_interval_hours from it, and any
        # flood advert (manual or periodic) refreshes it.
        self._last_flood_advert: float = 0.0

    # channel logging filter
    def _parse_log_channels(self) -> None:
        v = (self.cfg.log_channels or "all").strip().lower()
        if v in ("all", "*", ""):
            self._log_channel_set = None
            return
        self._log_channel_set = {x.strip() for x in v.split(",") if x.strip()}

    def _should_log_channel(
        self, channel_idx: Optional[int], channel_name: Optional[str]
    ) -> bool:
        if self._log_channel_set is None:
            return True
        s = self._log_channel_set
        if channel_idx is not None and str(channel_idx) in s:
            return True
        if channel_name:
            cn = channel_name.lower()
            if cn in s or cn.lstrip("#") in s:
                return True
        return False

    # contact resolution
    async def resolve_prefix(
        self, prefix: str
    ) -> tuple[Optional[str], Optional[str]]:
        """6-byte hex prefix -> (full_pubkey, adv_name)."""
        if not prefix:
            return None, None
        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts "
            "WHERE substr(public_key,1,12)=?",
            (prefix.lower(),),
        )
        if not rows:
            return None, None
        if len(rows) > 1:
            self.logger.warning(
                "ambiguous pubkey prefix %s (%d matches)",
                prefix, len(rows),
            )
            return None, "<ambiguous>"
        return rows[0]["public_key"], rows[0]["adv_name"]

    async def resolve_name(self, name: str) -> Optional[str]:
        if not name:
            return None
        row = await self.db.fetchone(
            "SELECT public_key FROM contacts WHERE adv_name=? LIMIT 1",
            (name,),
        )
        return row["public_key"] if row else None

    # initial sync
    async def sync_device_info(self) -> None:
        # SELF_INFO is cached by the lib during appstart (mc.self_info)
        try:
            si = getattr(self.mc, "self_info", None)
            if isinstance(si, dict) and si:
                await self._upsert_device_info(si, "self_info")
        except Exception:
            self.logger.exception("self_info capture failed")
        try:
            ev = await self.mc.commands.send_device_query()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "device_info")
        except Exception:
            self.logger.exception("send_device_query failed")
        try:
            ev = await self.mc.commands.get_bat()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "battery")
        except Exception:
            self.logger.exception("get_bat failed")

    async def refresh_self_info(self) -> None:
        """Re-query the radio's SELF_INFO (appstart refreshes mc.self_info) and
        persist it, so the device_info table reflects a just-applied change
        immediately rather than on the next periodic sync."""
        try:
            ev = await self.mc.commands.send_appstart()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "self_info")
        except Exception:
            self.logger.exception("refresh_self_info failed")

    async def _upsert_device_info(self, payload: dict, group: str) -> None:
        now = int(time.time())
        for k, v in payload.items():
            await self.db.execute(
                "INSERT INTO device_info(key,value,last_updated) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, last_updated=excluded.last_updated",
                (f"{group}.{k}", json.dumps(v, default=str), now),
            )

    async def sync_contacts(self) -> None:
        row = await self.db.fetchone(
            "SELECT value FROM bot_meta WHERE key='contacts_lastmod'"
        )
        lastmod = 0
        if row and row["value"] and str(row["value"]).isdigit():
            lastmod = int(row["value"])

        try:
            ev = await self.mc.commands.get_contacts(lastmod=lastmod, timeout=10)
        except TypeError:
            try:
                ev = await self.mc.commands.get_contacts()
            except Exception:
                self.logger.exception("get_contacts failed")
                return
        except Exception:
            self.logger.exception("get_contacts failed")
            return

        if not ev or not isinstance(ev.payload, dict):
            return
        contacts = ev.payload
        now = int(time.time())
        count = 0
        for pk, c in contacts.items():
            if not isinstance(c, dict):
                continue
            await self.db.execute(
                """INSERT INTO contacts
                (public_key, adv_name, type, flags, out_path, out_path_len,
                 out_path_hash_mode, adv_lat, adv_lon, last_advert, lastmod,
                 first_seen_at, last_synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(public_key) DO UPDATE SET
                    adv_name=excluded.adv_name, type=excluded.type,
                    flags=excluded.flags, out_path=excluded.out_path,
                    out_path_len=excluded.out_path_len,
                    out_path_hash_mode=excluded.out_path_hash_mode,
                    adv_lat=excluded.adv_lat, adv_lon=excluded.adv_lon,
                    last_advert=excluded.last_advert,
                    lastmod=excluded.lastmod,
                    last_synced_at=excluded.last_synced_at""",
                (
                    pk.lower(),
                    c.get("adv_name"),
                    c.get("type"),
                    c.get("flags"),
                    c.get("out_path"),
                    c.get("out_path_len"),
                    c.get("out_path_hash_mode"),
                    c.get("adv_lat"),
                    c.get("adv_lon"),
                    c.get("last_advert"),
                    c.get("lastmod"),
                    now,
                    now,
                ),
            )
            count += 1

        new_lastmod = None
        if hasattr(ev, "attributes") and isinstance(ev.attributes, dict):
            new_lastmod = ev.attributes.get("lastmod")
        if new_lastmod is not None:
            await self.db.execute(
                "INSERT INTO bot_meta(key,value) VALUES('contacts_lastmod',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_lastmod),),
            )
        await self._trim_contacts()
        self.logger.info("contacts sync: %d contacts upserted", count)

    async def _trim_contacts(self) -> None:
        # cap the contacts table, rolling off the oldest. using the
        # last_synced_at timestamp (the bot's own clock) rather than 
        # last_advert which is sent by the sender and is sometimes complete
        # shit (stuff like 2000 and 2102 from misconfigured node clocks)
        keep = self.cfg.max_contacts
        if keep <= 0:
            return
        cur = await self.db.execute(
            "DELETE FROM contacts WHERE public_key NOT IN ("
            "SELECT public_key FROM contacts "
            "ORDER BY COALESCE(last_synced_at,0) DESC, "
            "COALESCE(last_advert,0) DESC LIMIT ?)",
            (keep,),
        )
        if cur.rowcount:
            self.logger.info(
                "contacts trim: removed %d oldest (cap=%d)",
                cur.rowcount, keep,
            )

    async def sync_channels(self) -> None:
        row = await self.db.fetchone(
            "SELECT value FROM device_info WHERE key='device_info.max_channels'"
        )
        max_ch = 16
        if row and row["value"]:
            try:
                max_ch = int(json.loads(row["value"]))
            except Exception:
                pass
        count = 0
        attempted = 0
        errors = 0
        empty = 0
        none_resp = 0
        for i in range(max_ch):
            try:
                ev = await self.mc.commands.get_channel(i)
            except Exception:
                self.logger.warning(
                    "get_channel(%d) raised", i, exc_info=True
                )
                errors += 1
                continue
            attempted += 1
            if ev is None:
                none_resp += 1
                continue
            ev_type = getattr(ev.type, "name", str(ev.type))
            if ev_type == "ERROR":
                errors += 1
                continue
            if not isinstance(ev.payload, dict):
                self.logger.debug(
                    "get_channel(%d) returned %s with non-dict payload: %r",
                    i, ev_type, ev.payload,
                )
                continue
            p = ev.payload
            name = p.get("channel_name") or p.get("name")
            secret = p.get("channel_secret") or p.get("secret")
            if isinstance(secret, (bytes, bytearray)):
                secret_hex = secret.hex()
            elif isinstance(secret, str):
                secret_hex = secret
            else:
                secret_hex = None
            if not name:
                empty += 1
                continue
            await self.db.execute(
                """INSERT INTO channels(channel_idx,name,secret_hex,last_synced_at)
                VALUES(?,?,?,?)
                ON CONFLICT(channel_idx) DO UPDATE SET
                    name=excluded.name, secret_hex=excluded.secret_hex,
                    last_synced_at=excluded.last_synced_at""",
                (i, name, secret_hex, int(time.time())),
            )
            count += 1
            self.logger.info("channel %d = %r", i, name)
        self.logger.info(
            "channels sync: %d recorded (attempted=%d, empty=%d, errors=%d, none=%d, max=%d)",
            count, attempted, empty, errors, none_resp, max_ch,
        )

    # firehose / packet log
    async def _firehose(self, event) -> None:
        try:
            await self._record_packet(event)
        except Exception:
            self.logger.exception("firehose handler failed")
        self.event_count += 1

    async def _record_packet(self, event) -> None:
        evt_name = event.type.name if hasattr(event.type, "name") else str(
            event.type
        )
        packet_type = PACKET_TYPE_MAP.get(evt_name, evt_name)
        payload = event.payload
        if not isinstance(payload, (dict, list)):
            payload = {"value": payload}
        attrs = getattr(event, "attributes", {}) or {}

        sender_prefix = None
        sender_pubkey = None
        sender_name = None
        path = None
        path_len = None
        snr = None
        rssi = None
        channel_idx = None
        text = None

        payload_typename = None
        route_typename = None
        ack_code = None
        path_hash_size: Optional[int] = None
        extra_bits: list[str] = []
        if isinstance(payload, dict):
            sender_prefix = payload.get("pubkey_prefix")
            p = payload.get("path")
            if isinstance(p, str):
                path = p
            path_len = payload.get("path_len")
            # RX_LOG_DATA sets path_hash_size directly (1/2/3 bytes per hop)
            # other events use path_hash_mode (0/1/2 — sentinel -1 for none)
            path_hash_size = payload.get("path_hash_size")
            if path_hash_size is None:
                mode = payload.get("path_hash_mode")
                if mode is not None and mode >= 0:
                    path_hash_size = mode + 1
            snr = payload.get("SNR", payload.get("snr"))
            rssi = payload.get("RSSI", payload.get("rssi"))
            channel_idx = payload.get("channel_idx")
            text = payload.get("text") or payload.get("message")
            payload_typename = payload.get("payload_typename")
            route_typename = payload.get("route_typename")
            if evt_name == "ADVERTISEMENT":
                pk = payload.get("public_key") or payload.get("pubkey")
                if pk and isinstance(pk, str):
                    sender_pubkey = pk
                    sender_prefix = pk[:12]
            elif evt_name in ("NEXT_CONTACT", "NEW_CONTACT"):
                pk = payload.get("public_key")
                name = payload.get("adv_name")
                if pk and isinstance(pk, str):
                    sender_pubkey = pk
                    sender_prefix = pk[:12]
                if name:
                    sender_name = name
                opl = payload.get("out_path_len")
                if opl == -1:
                    extra_bits.append("route=flood")
                elif opl is not None:
                    extra_bits.append(f"hops={opl}")
                last_adv = payload.get("last_advert")
                if last_adv:
                    extra_bits.append(f"last_adv={last_adv}")
            elif evt_name == "ACK":
                ack_code = payload.get("code")

        if sender_prefix:
            full, name = await self.resolve_prefix(sender_prefix)
            sender_pubkey = sender_pubkey or full
            sender_name = name

        if evt_name == "CHANNEL_MSG_RECV" and text:
            m = CHANNEL_SENDER_RE.match(text)
            if m:
                sender_name = m.group(1)
                pk = await self.resolve_name(sender_name)
                if pk:
                    sender_pubkey = pk

        try:
            payload_json = json.dumps(payload, default=str)
        except Exception:
            payload_json = json.dumps({"_unrepr": str(payload)})
        try:
            attrs_json = json.dumps(attrs, default=str)
        except Exception:
            attrs_json = "{}"

        # only RX_LOG_DATA carries the raw packet bytes (as a hex string
        # in payload["payload"]). store it so the web inspector can break
        # a packet down without re-digging it out of payload_json.
        raw_hex = None
        if evt_name == "RX_LOG_DATA" and isinstance(payload, dict):
            rf = payload.get("payload")
            if isinstance(rf, str) and rf:
                raw_hex = rf

        now_ts = int(time.time())
        cur = await self.db.execute(
            """INSERT INTO received_packets
            (received_at, event_type, packet_type, sender_pubkey_prefix,
             sender_pubkey, sender_name, path, path_len, snr, rssi,
             channel_idx, text, payload_json, attributes_json, raw_hex)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_ts, evt_name, packet_type,
                sender_prefix, sender_pubkey, sender_name,
                path, path_len, snr, rssi, channel_idx, text,
                payload_json, attrs_json, raw_hex,
            ),
        )
        await self._trim_global("received_packets", self.cfg.max_packets)

        # live packet feed for the web UI (only build the dict if watched).
        if self.web_packet_feed.has_subscribers():
            self.web_packet_feed.publish({
                "id": cur.lastrowid,
                "received_at": now_ts,
                "event_type": evt_name,
                "packet_type": packet_type,
                "sender_pubkey_prefix": sender_prefix,
                "sender_pubkey": sender_pubkey,
                "sender_name": sender_name,
                "path": path,
                "path_len": path_len,
                "snr": snr,
                "rssi": rssi,
                "channel_idx": channel_idx,
                "text": text,
                "has_raw": raw_hex is not None,
            })

        # for TEXT_MSG packets observed via RX_LOG, the first two bytes of
        # pkt_payload are the destination and sender pubkey hashes (1 byte
        # each). check them so we can see whether a DM is addressed to us.
        dst_byte = None
        src_byte = None
        if payload_typename in ("TEXT_MSG", "ACK") and isinstance(payload, dict):
            pkt = payload.get("pkt_payload")
            if isinstance(pkt, (bytes, bytearray)) and len(pkt) >= 2:
                dst_byte = pkt[0]
                src_byte = pkt[1]

        snippet = (text[:60] + "…") if text and len(text) > 60 else (text or "")
        sender_disp = sender_name or sender_prefix or ""
        bits = [packet_type]
        if payload_typename:
            bits.append(f"pl={payload_typename}")
        if route_typename:
            bits.append(f"rt={route_typename}")
        if dst_byte is not None:
            for_us = (
                self.my_pubkey_byte is not None
                and dst_byte == self.my_pubkey_byte
            )
            bits.append(f"dst={dst_byte:02x}{'(us)' if for_us else ''}")
        if src_byte is not None:
            bits.append(f"src={src_byte:02x}")
        if sender_disp:
            bits.append(f"from={sender_disp}")
        if channel_idx is not None:
            bits.append(f"ch={channel_idx}")
        if path:
            bits.append(f"path={format_path(path, path_hash_size)}")
        if snr is not None:
            bits.append(f"snr={snr}")
        if snippet:
            bits.append(f'text="{snippet}"')
        if ack_code:
            bits.append(f"code={ack_code}")
        if extra_bits:
            bits.extend(extra_bits)
        # error responses include error_code, code_string, reason
        if evt_name == "ERROR" and isinstance(payload, dict):
            ec = payload.get("error_code")
            cs = payload.get("code_string")
            rs = payload.get("reason")
            if ec is not None:
                bits.append(f"code={ec}")
            if cs:
                bits.append(f"code_str={cs!r}")
            if rs:
                bits.append(f"reason={rs!r}")
        self.logger.info(" ".join(bits))

        # correlate this frame against messages we've sent (repeater-repeat
        # tracking). wrapped so a matcher bug can never break packet recording.
        try:
            await self._match_repeat(payload)
        except Exception:
            self.logger.exception("repeat matcher failed")

    # ------------------------------------------------------------------
    # Repeater-repeat tracking
    #
    # When the bot transmits a DM/channel message, nearby repeaters
    # rebroadcast ("repeat") it. The radio doesn't hear its own TX, but it
    # hears each repeater's rebroadcast as an RX_LOG_DATA frame whose on-air
    # payload is byte-identical to ours (only the path grows). We can't learn
    # our sent packet's hash from the radio, so we correlate by message text:
    # decrypt the heard frame (channel secret / our DM shared secret) and
    # compare. Each distinct (pkt_hash, path) is one repeater; if none is
    # heard within cfg.repeat_timeout we log + surface that too.
    # ------------------------------------------------------------------
    def _register_repeat_watch(
        self, *, kind: str, text: str,
        dest_pubkey: Optional[str] = None,
        channel_idx: Optional[int] = None,
        disp_name: str = "", route_mode: str = "unknown",
    ) -> Optional[RepeatWatch]:
        # build + register a watch (no timer yet — see _start_repeat_timer).
        # returns None (inert) when tracking is off or we can't detect repeats.
        if not self.cfg.repeat_tracking or self.my_pubkey_byte is None:
            return None
        text = text or ""
        if not text:
            return None
        dest_byte = None
        if kind == "dm":
            # DM repeats are detected by decrypting with our shared secret;
            # without the private key we'd never match, so don't register
            # (a watch that can't match would always log a false NO_REPEAT).
            if not dest_pubkey or self.my_private_key is None:
                return None
            dest_pubkey = dest_pubkey.lower()
            try:
                dest_byte = int(dest_pubkey[0:2], 16)
            except ValueError:
                return None
        w = RepeatWatch(
            kind=kind, text=text, dest_pubkey=dest_pubkey,
            dest_byte=dest_byte, channel_idx=channel_idx,
            disp_name=disp_name, route_mode=route_mode,
            registered_at=time.monotonic(),
        )
        self._repeat_watches.append(w)
        # bound memory: drop the oldest if we somehow accumulate too many
        while len(self._repeat_watches) > 64:
            old = self._repeat_watches.pop(0)
            t = old.timer_task
            if t is not None and not t.done():
                t.cancel()
        return w

    def _start_repeat_timer(self, watch: Optional[RepeatWatch]) -> None:
        # start the no-repeat countdown AFTER the (possibly slow, retrying)
        # send returns, so the timeout isn't consumed while still transmitting.
        if watch is None or watch.done:
            return
        watch.send_completed_at = time.monotonic()
        try:
            watch.timer_task = asyncio.create_task(
                self._repeat_timeout_runner(watch)
            )
        except RuntimeError:
            pass  # no running loop (not expected from an async caller)

    def _discard_watch(self, watch: RepeatWatch) -> None:
        try:
            self._repeat_watches.remove(watch)
        except ValueError:
            pass

    async def _repeat_timeout_runner(self, watch: RepeatWatch) -> None:
        try:
            await asyncio.sleep(max(0.1, self.cfg.repeat_timeout))
        except asyncio.CancelledError:
            return
        if watch.done:
            self._discard_watch(watch)
            return
        # A repeat was heard -> success; let the watch lapse, no retry.
        if watch.repeat_count > 0:
            watch.done = True
            self._discard_watch(watch)
            return
        # No repeat heard. For channel sends, retry the (idempotent) retransmit
        # if attempts remain; re-arm the same watch so a later repeat still
        # counts. Otherwise surface the final NO_REPEAT (or DIRECT_0HOP).
        if watch.kind == "channel" and watch.retries_left > 0:
            watch.retries_left -= 1
            try:
                await self._retry_channel_watch(watch)
            except Exception:
                self.logger.exception(
                    "channel no-repeat retry failed; giving up"
                )
            else:
                self._start_repeat_timer(watch)
                return
        watch.done = True
        try:
            await self._emit_no_repeat(watch)
        except Exception:
            self.logger.exception("repeat-timeout handler failed")
        finally:
            self._discard_watch(watch)

    async def _retry_channel_watch(self, watch: RepeatWatch) -> None:
        # Resend with the ORIGINAL timestamp so the packet is byte-identical:
        # MeshCore keys a channel message by SHA256(timestamp||text), so nodes
        # that already heard it de-dupe the retry and only repeaters that missed
        # it pick it up. Surfaced as a RETRY row on the web Packets screen.
        snippet = (
            watch.text[:60] + "…" if len(watch.text) > 60 else watch.text
        )
        self.logger.info(
            "RETRY ch=%s: no repeat in %.0fs, resending (%d left): %r",
            watch.channel_idx, self.cfg.repeat_timeout,
            watch.retries_left, snippet,
        )
        await self._emit_synthetic_packet(
            packet_type="RETRY",
            text=watch.text,
            channel_idx=watch.channel_idx,
        )
        await self.mc.commands.send_chan_msg(
            watch.channel_idx, watch.text, timestamp=watch.send_timestamp,
        )

    def _matches_active_channel_watch(
        self, channel_idx: Optional[int], text: str
    ) -> bool:
        # true if an active (not-yet-expired) channel watch matches this
        # decrypted channel message — i.e. it's our own message coming back
        # via a repeater. used to suppress phantom re-ingest.
        if not self.cfg.repeat_tracking or channel_idx is None:
            return False
        for w in self._repeat_watches:
            if (
                w.kind == "channel"
                and not w.done
                and w.channel_idx == channel_idx
                and w.text == text
            ):
                return True
        return False

    async def _match_repeat(self, payload) -> None:
        # fast path: nothing to do unless tracking is on and a send is pending
        if not self.cfg.repeat_tracking or not self._repeat_watches:
            return
        if not isinstance(payload, dict):
            return
        ptype = payload.get("payload_type")
        pkt = payload.get("pkt_payload")
        if not isinstance(pkt, (bytes, bytearray)):
            return
        path_hex = payload.get("path") or ""
        path_len = payload.get("path_len")
        # a frame with no path is the origin's own copy, never a repeater
        # rebroadcast — skip so a hypothetical self-echo isn't counted.
        if not path_len:
            return
        pkt_hash = payload.get("pkt_hash")
        path_hash_size = payload.get("path_hash_size")

        watch: Optional[RepeatWatch] = None
        if ptype == PayloadType.GROUP_TEXT.value:
            if len(pkt) < 3:
                return
            ch = self.channels_by_hash.get(pkt[0])
            if ch is None:
                return
            idx, _name, secret = ch
            dec = decrypt_group_text(bytes(pkt), secret)
            if dec is None:
                return
            for w in self._repeat_watches:
                if (
                    w.kind == "channel" and not w.done
                    and w.channel_idx == idx and w.text == dec.message
                ):
                    watch = w
                    break
        elif ptype == PayloadType.TEXT_MESSAGE.value:
            if len(pkt) < 4 or self.my_private_key is None:
                return
            if pkt[1] != self.my_pubkey_byte:
                return  # not sent by us
            for w in self._repeat_watches:
                if w.kind != "dm" or w.done or w.dest_byte != pkt[0]:
                    continue
                try:
                    their_pub = bytes.fromhex(w.dest_pubkey or "")
                    if len(their_pub) != 32:
                        continue
                    shared = derive_shared_secret(
                        self.my_private_key, their_pub
                    )
                    dec = decrypt_direct_message(bytes(pkt), shared)
                except Exception:
                    continue
                if dec is not None and dec.message == w.text:
                    watch = w
                    break
        else:
            return

        if watch is None:
            return

        # dedupe per heard frame: same (pkt_hash, path) = the same repeat we
        # already counted; a different path = a different repeater. record the
        # frame and bump the count BEFORE any await so interleaved matches
        # can't double-count.
        frame_key = (pkt_hash, path_hex)
        if frame_key in watch.seen_frames:
            return
        watch.seen_frames.add(frame_key)
        watch.repeat_count += 1
        last_hop = (
            path_hex[-(path_hash_size * 2):]
            if path_hex and path_hash_size else (path_hex or None)
        )
        if last_hop:
            watch.repeater_keys.add(last_hop)

        path_disp = format_path(path_hex, path_hash_size) if path_hex else ""
        snippet = (
            watch.text[:60] + "…" if len(watch.text) > 60 else watch.text
        )
        snr = payload.get("snr")
        if watch.kind == "channel":
            target = f"ch={watch.channel_idx}"
        else:
            target = f"to={watch.disp_name or (watch.dest_pubkey or '')[:12]}"
        self.logger.info(
            "REPEAT #%d %s by repeater path=%s snr=%s: %r",
            watch.repeat_count, target, path_disp, snr, snippet,
        )
        await self._emit_synthetic_packet(
            packet_type="REPEAT",
            text=watch.text,
            channel_idx=watch.channel_idx,
            sender_name=watch.disp_name if watch.kind == "dm" else None,
            sender_pubkey=watch.dest_pubkey if watch.kind == "dm" else None,
            sender_prefix=last_hop,
            path=path_hex or None,
            path_len=path_len,
            snr=snr,
        )

    async def _emit_no_repeat(self, watch: RepeatWatch) -> None:
        snippet = (
            watch.text[:60] + "…" if len(watch.text) > 60 else watch.text
        )
        if watch.kind == "channel":
            target = f"ch={watch.channel_idx}"
        else:
            target = f"to={watch.disp_name or (watch.dest_pubkey or '')[:12]}"
        # A 0-hop direct DM goes straight to a neighbor with no repeater in the
        # path, so "no repeat heard" is expected, not a propagation failure —
        # label it DIRECT_0HOP. Everything else (flood, multi-hop, unknown) is
        # a genuine "sent where a repeater could have rebroadcast, but none
        # was heard" → NO_REPEAT.
        if watch.kind == "dm" and watch.route_mode == "direct_0hop":
            self.logger.info(
                "DIRECT_0HOP %s: delivered direct to neighbor, "
                "no repeater involved: %r", target, snippet,
            )
            packet_type = "DIRECT_0HOP"
        else:
            self.logger.info(
                "NO_REPEAT %s: no repeater heard within %.0fs: %r",
                target, self.cfg.repeat_timeout, snippet,
            )
            packet_type = "NO_REPEAT"
        await self._emit_synthetic_packet(
            packet_type=packet_type,
            text=watch.text,
            channel_idx=watch.channel_idx,
            sender_name=watch.disp_name if watch.kind == "dm" else None,
            sender_pubkey=watch.dest_pubkey if watch.kind == "dm" else None,
        )

    async def _emit_synthetic_packet(
        self, *, packet_type: str,
        text: Optional[str] = None,
        channel_idx: Optional[int] = None,
        sender_name: Optional[str] = None,
        sender_pubkey: Optional[str] = None,
        sender_prefix: Optional[str] = None,
        path: Optional[str] = None,
        path_len: Optional[int] = None,
        snr: Optional[float] = None,
    ) -> None:
        # insert a synthetic received_packets row (REPEAT / NO_REPEAT) and
        # push it to the web Packets feed, mirroring _record_packet's storage.
        # event_type stays RX_LOG_DATA (the true source); packet_type carries
        # the synthetic kind so the Packets screen filters/labels it.
        now_ts = int(time.time())
        cur = await self.db.execute(
            """INSERT INTO received_packets
            (received_at, event_type, packet_type, sender_pubkey_prefix,
             sender_pubkey, sender_name, path, path_len, snr, rssi,
             channel_idx, text, payload_json, attributes_json, raw_hex)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_ts, "RX_LOG_DATA", packet_type,
                sender_prefix, sender_pubkey, sender_name,
                path, path_len, snr, None, channel_idx, text,
                "{}", "{}", None,
            ),
        )
        await self._trim_global("received_packets", self.cfg.max_packets)
        if self.web_packet_feed.has_subscribers():
            self.web_packet_feed.publish({
                "id": cur.lastrowid,
                "received_at": now_ts,
                "event_type": "RX_LOG_DATA",
                "packet_type": packet_type,
                "sender_pubkey_prefix": sender_prefix,
                "sender_pubkey": sender_pubkey,
                "sender_name": sender_name,
                "path": path,
                "path_len": path_len,
                "snr": snr,
                "rssi": None,
                "channel_idx": channel_idx,
                "text": text,
                "has_raw": False,
            })

    # DM / channel handlers
    async def _on_dm(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        await self._ingest_dm(
            sender_pubkey_prefix=payload.get("pubkey_prefix"),
            sender_pubkey=None,
            sender_name=None,
            text=payload.get("text", "") or "",
            sender_timestamp=payload.get("sender_timestamp"),
            path_len=payload.get("path_len"),
            path_hash_mode=payload.get("path_hash_mode"),
            txt_type=payload.get("txt_type"),
            snr=payload.get("SNR"),
            signature=payload.get("signature"),
        )

    async def _ingest_dm(
        self, *,
        sender_pubkey_prefix: Optional[str],
        sender_pubkey: Optional[str],
        sender_name: Optional[str],
        text: str,
        sender_timestamp: Optional[int] = None,
        path_len: Optional[int] = None,
        path_hash_mode: Optional[int] = None,
        txt_type: Optional[int] = None,
        snr: Optional[float] = None,
        signature: Optional[str] = None,
    ) -> None:
        # fill any missing identity fields from the contacts table.
        if sender_pubkey_prefix and not sender_pubkey:
            full, name = await self.resolve_prefix(sender_pubkey_prefix)
            sender_pubkey = sender_pubkey or full
            sender_name = sender_name or name
        # two ingest entry points exist for DMs:
        # _on_rx_log_data:  _handle_inbound_dm (client-side decrypt)
        # _on_dm via CONTACT_MSG_RECV (radio's get_msg queue)
        #
        # on firmware that supports both, the same DM data arrives via
        # both routes. also, sender retries need deduping by
        # sender_pubkey + sender_timestamp. this filters reciving a DM
        # from multiple routes down to ingesting one copy of each message.
        if sender_pubkey and sender_timestamp is not None:
            if self._seen_message(sender_pubkey, sender_timestamp):
                self.logger.info(
                    "DM duplicate suppressed from=%s ts=%d",
                    sender_name or sender_pubkey[:12], sender_timestamp,
                )
                return
        now_ts = int(time.time())
        cur = await self.db.execute(
            """INSERT INTO direct_messages
            (sender_pubkey_prefix, sender_pubkey, sender_name, text,
             sender_timestamp, path_len, path_hash_mode, txt_type, snr,
             signature, received_at, is_outgoing)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                sender_pubkey_prefix, sender_pubkey, sender_name, text,
                sender_timestamp, path_len, path_hash_mode, txt_type,
                snr, signature, now_ts,
            ),
        )
        await self._trim_global("direct_messages", self.cfg.max_dms)

        if self.web_message_feed.has_subscribers():
            self.web_message_feed.publish({
                "kind": "dm",
                "id": cur.lastrowid,
                "sender_pubkey_prefix": sender_pubkey_prefix,
                "sender_pubkey": sender_pubkey,
                "sender_name": sender_name,
                "text": text,
                "sender_timestamp": sender_timestamp,
                "snr": snr,
                "received_at": now_ts,
                "is_outgoing": 0,
            })

        if self.cfg.commands_enabled:
            ctx = CommandContext(
                sender_name=sender_name,
                sender_pubkey=sender_pubkey,
                sender_pubkey_prefix=sender_pubkey_prefix,
                message_text=text,
                is_dm=True,
                channel_idx=None,
                channel_name=None,
                path=None,
                path_len=path_len,
                path_hash_mode=path_hash_mode,
                snr=snr,
                rssi=None,
                sender_timestamp=sender_timestamp,
                bot=self,
                from_queue=self._is_from_queue(),
            )
            await self._dispatch_command(ctx)

    async def _on_channel_msg(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        text = payload.get("text", "") or ""
        sender_name = None
        m = CHANNEL_SENDER_RE.match(text) if text else None
        if m:
            sender_name = m.group(1)
        await self._ingest_channel_msg(
            channel_idx=payload.get("channel_idx"),
            text=text,
            sender_name=sender_name,
            sender_timestamp=payload.get("sender_timestamp"),
            path=payload.get("path"),
            path_len=payload.get("path_len"),
            path_hash_mode=payload.get("path_hash_mode"),
            txt_type=payload.get("txt_type"),
            snr=payload.get("SNR"),
            rssi=payload.get("RSSI"),
            attempt=payload.get("attempt"),
            recv_time=payload.get("recv_time"),
        )

    async def _ingest_channel_msg(
        self, *,
        channel_idx: Optional[int],
        text: str,
        sender_name: Optional[str] = None,
        sender_timestamp: Optional[int] = None,
        path: Optional[str] = None,
        path_len: Optional[int] = None,
        path_hash_mode: Optional[int] = None,
        txt_type: Optional[int] = None,
        snr: Optional[float] = None,
        rssi: Optional[int] = None,
        attempt: Optional[int] = None,
        recv_time: Optional[int] = None,
    ) -> None:
        # a channel whose key the radio holds is decrypted both by the radio
        # (CHANNEL_MSG_RECV) and by us (RX_LOG_DATA). dedupe the two so we
        # don't store or act upon the message twice.
        if self._seen_channel_message(channel_idx, sender_timestamp, text):
            self.logger.info(
                "channel msg duplicate suppressed ch=%s ts=%s",
                channel_idx, sender_timestamp,
            )
            return
        ch_row = None
        if channel_idx is not None:
            ch_row = await self.db.fetchone(
                "SELECT name FROM channels WHERE channel_idx=?",
                (channel_idx,),
            )
        ch_name = ch_row["name"] if ch_row else None
        sender_pubkey = (
            await self.resolve_name(sender_name) if sender_name else None
        )

        # [channel_logging] controls only whether we store a copy of the
        # message in channel_messages. It doesn't affect command handling.
        if self._should_log_channel(channel_idx, ch_name):
            now_ts = int(time.time())
            cur = await self.db.execute(
                """INSERT INTO channel_messages
                (channel_idx, channel_name, sender_name, sender_pubkey, text,
                 sender_timestamp, path_len, path_hash_mode, path, txt_type,
                 snr, rssi, attempt, recv_time, received_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    channel_idx, ch_name, sender_name, sender_pubkey, text,
                    sender_timestamp, path_len, path_hash_mode, path,
                    txt_type, snr, rssi, attempt, recv_time, now_ts,
                ),
            )
            await self._trim_channel_messages(
                channel_idx, self.cfg.max_channel_messages
            )
            if self.web_message_feed.has_subscribers():
                self.web_message_feed.publish({
                    "kind": "channel",
                    "id": cur.lastrowid,
                    "channel_idx": channel_idx,
                    "channel_name": ch_name,
                    "sender_name": sender_name,
                    "sender_pubkey": sender_pubkey,
                    "text": text,
                    "sender_timestamp": sender_timestamp,
                    "path": path,
                    "snr": snr,
                    "rssi": rssi,
                    "received_at": now_ts,
                })

        if self.cfg.commands_enabled:
            # channel messages typically arrive as "Name: body". strip the
            # "Name: " prefix from the command text so triggers like "!path"
            # match what the user actually typed.
            command_text = text
            if sender_name:
                p = f"{sender_name}: "
                if text.startswith(p):
                    command_text = text[len(p):]
            ctx = CommandContext(
                sender_name=sender_name,
                sender_pubkey=sender_pubkey,
                sender_pubkey_prefix=sender_pubkey[:12] if sender_pubkey else None,
                message_text=command_text,
                is_dm=False,
                channel_idx=channel_idx,
                channel_name=ch_name,
                path=path,
                path_len=path_len,
                path_hash_mode=path_hash_mode,
                snr=snr,
                rssi=rssi,
                sender_timestamp=sender_timestamp,
                bot=self,
                from_queue=self._is_from_queue(),
            )
            await self._dispatch_command(ctx)

    async def _on_advertisement(self, event) -> None:
        # mark contacts table as needing a refresh; periodic task picks it up.
        self._contacts_dirty = True

    async def _on_rx_log_data(self, event) -> None:
        # RX_LOG_DATA contains every observed RF frame as raw bytes. DMs (using
        # the radio's exported private key) and channel messages (using
        # configured channel secrets) are decrypted here. On message-queueing
        # firmware (v1.15) this duplicates the radio's get_msg path — the radio
        # also decrypts and delivers DMs (CONTACT_MSG_RECV) and channel msgs on
        # its programmed channels (CHANNEL_MSG_RECV) — so the ingest funnel
        # dedups the two. RX_LOG is still essential: it is the only source of
        # raw on-air bytes (packet monitor) and routing paths, it sees traffic
        # the radio doesn't decrypt for us, and it covers channels beyond the
        # radio's slot capacity / non-queueing firmware.
        payload = event.payload
        if not isinstance(payload, dict):
            return
        rf_hex = payload.get("payload")
        if not isinstance(rf_hex, str) or not rf_hex:
            return
        try:
            rf_packet = bytes.fromhex(rf_hex)
        except ValueError:
            return
        env = parse_packet_envelope(rf_packet)
        if env is None:
            return

        pkt_hash = int.from_bytes(
            hashlib.sha256(env.payload).digest()[:4], "little"
        )
        if self._seen_packet(pkt_hash):
            return

        snr = payload.get("snr")
        rssi = payload.get("rssi")
        path_hex = env.path.hex() if env.path else None
        path_len = env.hop_count
        path_hash_mode = env.hash_size - 1

        if env.payload_type == PayloadType.TEXT_MESSAGE.value:
            await self._handle_inbound_dm(
                env.payload, snr=snr, path_hex=path_hex,
                path_len=path_len, path_hash_mode=path_hash_mode,
            )
        elif env.payload_type == PayloadType.GROUP_TEXT.value:
            await self._handle_inbound_channel(
                env.payload, snr=snr, rssi=rssi, path_hex=path_hex,
                path_len=path_len, path_hash_mode=path_hash_mode,
            )
        elif env.payload_type == PayloadType.ADVERT.value:
            # the lib parses adv_key / adv_name / adv_lat / adv_lon /
            # adv_timestamp into the RX_LOG_DATA payload dict.
            await self._update_contact_from_advert(payload)

    async def _update_contact_from_advert(self, payload) -> None:
        # refresh an existing contact's name/location/timestamp from a
        # received advert. updates the DB row only if the contact already
        # exists. new contacts are added by the radio sync per the radio's
        # own auto-add policy (avoids phantom rows here). The radio
        # maintains its own contact store and updates it when it processes
        # the advert, so no push back to the radio is needed.
        pk = payload.get("adv_key")
        if not isinstance(pk, str) or len(pk) != 64:
            return
        pk = pk.lower()
        name = payload.get("adv_name")
        lat = payload.get("adv_lat")
        lon = payload.get("adv_lon")
        ts = payload.get("adv_timestamp")
        now = int(time.time())
        cur = await self.db.execute(
            "UPDATE contacts SET "
            "adv_name=COALESCE(?, adv_name), "
            "adv_lat=COALESCE(?, adv_lat), "
            "adv_lon=COALESCE(?, adv_lon), "
            "last_advert=COALESCE(?, last_advert), "
            "last_synced_at=? "
            "WHERE public_key=?",
            (name, lat, lon, ts, now, pk),
        )
        if cur.rowcount:
            self.logger.debug(
                "advert: refreshed contact %s name=%r", pk[:12], name
            )
        else:
            # unknown to us. let the next radio sync add it
            self._contacts_dirty = True

    async def _handle_inbound_dm(
        self, packet_payload: bytes, *,
        snr: Optional[float], path_hex: Optional[str],
        path_len: Optional[int], path_hash_mode: Optional[int],
    ) -> None:
        if (
            self.my_private_key is None
            or self.my_pubkey_byte is None
        ):
            return  # haven't loaded the key yet
        if len(packet_payload) < 4:
            return
        if packet_payload[0] != self.my_pubkey_byte:
            return  # not addressed to us
        src_byte = packet_payload[1]
        src_hex = f"{src_byte:02x}"
        # find candidate sender contacts by matching pubkey first byte
        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts "
            "WHERE substr(public_key,1,2)=?",
            (src_hex,),
        )
        if not rows:
            self.logger.debug(
                "inbound DM: no contact matches src=%s", src_hex
            )
            return
        decrypted: Optional[DecryptedDM] = None
        sender_pubkey: Optional[str] = None
        sender_name: Optional[str] = None
        for row in rows:
            try:
                their_pk = bytes.fromhex(row["public_key"])
            except ValueError:
                continue
            if len(their_pk) != 32:
                continue
            dec = try_decrypt_dm(
                packet_payload,
                self.my_private_key,
                their_pk,
                self.my_pubkey_byte,
            )
            if dec is not None:
                decrypted = dec
                sender_pubkey = row["public_key"]
                sender_name = row["adv_name"]
                break
        if decrypted is None:
            self.logger.debug(
                "inbound DM from src=%s: %d contact candidate(s), "
                "none decrypted",
                src_hex, len(rows),
            )
            return
        # dedupe (sender_pubkey, sender_timestamp) now lives in
        # _ingest_dm so it filters both this RX_LOG decrypt path AND the
        # radio-queued get_msg/CONTACT_MSG_RECV path, regardless of which
        # fires first.
        self.logger.info(
            "DM decrypted from=%s (%s): %r",
            sender_name or "?", sender_pubkey[:12], decrypted.message,
        )
        await self._ingest_dm(
            sender_pubkey_prefix=sender_pubkey[:12],
            sender_pubkey=sender_pubkey,
            sender_name=sender_name,
            text=decrypted.message,
            sender_timestamp=decrypted.timestamp,
            path_len=path_len,
            path_hash_mode=path_hash_mode,
            txt_type=decrypted.txt_type,
            snr=snr,
        )

    async def _handle_inbound_channel(
        self, packet_payload: bytes, *,
        snr: Optional[float], rssi: Optional[int],
        path_hex: Optional[str], path_len: Optional[int],
        path_hash_mode: Optional[int],
    ) -> None:
        if len(packet_payload) < 3:
            return
        chash = packet_payload[0]
        ch = self.channels_by_hash.get(chash)
        if ch is None:
            return
        idx, name, secret = ch
        dec = decrypt_group_text(packet_payload, secret)
        if dec is None:
            self.logger.debug(
                "channel msg on idx=%d (%s): MAC/AES failed", idx, name
            )
            return
        # if this decrypts to a channel message we ourselves just sent, it's a
        # repeater rebroadcasting our own traffic — _match_repeat (firehose)
        # already counts/surfaces it as a REPEAT, so don't re-ingest it here as
        # a phantom inbound message (or re-dispatch a command on it).
        if self._matches_active_channel_watch(idx, dec.message):
            self.logger.debug(
                "channel msg on idx=%d (%s) is our own repeated send; "
                "suppressing re-ingest", idx, name,
            )
            return
        self.logger.info(
            "channel msg decrypted ch=%d (%s) from=%s: %r",
            idx, name, dec.sender or "?", dec.message,
        )
        # reconstruct "Name: text" if a sender was extracted, so the
        # ingest path's existing channel-msg handling sees a consistent
        # "text" field.
        ingest_text = (
            f"{dec.sender}: {dec.message}" if dec.sender else dec.message
        )
        await self._ingest_channel_msg(
            channel_idx=idx,
            text=ingest_text,
            sender_name=dec.sender,
            sender_timestamp=dec.timestamp,
            path=path_hex,
            path_len=path_len,
            path_hash_mode=path_hash_mode,
            txt_type=dec.flags >> 2,
            snr=snr,
            rssi=rssi,
            attempt=dec.flags & 0x03,
        )

    async def _on_messages_waiting(self, event) -> None:
        self.logger.info("MSG_WAIT received (radio has queued messages)")

    def _begin_queue_drain(self) -> None:
        # opens the "these messages are offline backlog" window; closed by
        # the first NO_MORE_MSGS (or the failsafe deadline in _is_from_queue).
        self._radio_queue_drained = False
        self._drain_deadline = time.monotonic() + 60.0

    def _is_from_queue(self) -> bool:
        if self._radio_queue_drained:
            return False
        if time.monotonic() > self._drain_deadline:
            # NO_MORE_MSGS never arrived (unexpected firmware/lib behavior);
            # fail open so live commands are not ignored forever.
            self.logger.warning(
                "startup queue-drain end marker never seen; treating "
                "messages as live from now on"
            )
            self._radio_queue_drained = True
            return False
        return True

    async def _on_no_more_msgs(self, event) -> None:
        if not self._radio_queue_drained:
            self._radio_queue_drained = True
            self.logger.info(
                "startup message queue drained (%d backlog command(s) "
                "ignored)", self._queued_cmds_skipped,
            )

    async def _drain_radio_queue(self) -> None:
        # Actively pump the radio's offline backlog at startup. The library's
        # start_auto_message_fetching() fetches only ONE message up front and
        # then waits for a MESSAGES_WAITING push — but the radio does not
        # re-announce messages queued before we connected, so without this
        # pump the rest of the backlog sits on the radio until the next live
        # message kicks the fetch loop, and then drains as if live (minutes
        # or hours later, past the drain window).
        pumped = 0
        while not self.stop_event.is_set() and pumped < 200:
            try:
                ev = await asyncio.wait_for(
                    self.mc.commands.get_msg(), timeout=10.0
                )
            except Exception:
                self.logger.exception("startup queue drain: get_msg failed")
                break
            name = getattr(getattr(ev, "type", None), "name", "")
            if ev is None or name in ("NO_MORE_MSGS", "ERROR"):
                break
            pumped += 1
            # yield so the dispatched message event gets ingested in order
            await asyncio.sleep(0.05)
        # the dispatched NO_MORE_MSGS closes the window in event order (after
        # every backlog message handler has run). Give it a moment, then
        # force-close so a lost marker can't leave the window open for 60s.
        for _ in range(20):
            if self._radio_queue_drained:
                return
            await asyncio.sleep(0.1)
        await self._on_no_more_msgs(None)

    async def _on_new_contact(self, event) -> None:
        # library tells us about a advertising node we didn't know.
        self._contacts_dirty = True

    async def _setup_private_key(self) -> bool:
        # export the radio's private key (or load from cache) and derive pubkey
        path = self.cfg.privkey_path
        key: Optional[bytes] = None
        # try cached file first
        if path and path.is_file():
            try:
                data = path.read_bytes()
                if len(data) == 64:
                    key = data
                    self.logger.info("loaded private key from %s", path)
                else:
                    self.logger.warning(
                        "cached key at %s is wrong length (%d); re-exporting",
                        path, len(data),
                    )
            except Exception:
                self.logger.exception("failed reading %s", path)
        if key is None:
            self.logger.info("exporting private key from radio...")
            try:
                ev = await self.mc.commands.export_private_key()
            except Exception:
                self.logger.exception("export_private_key raised")
                return False
            if not ev:
                self.logger.error("export_private_key returned no event")
                return False
            t = getattr(ev.type, "name", "")
            if t == "PRIVATE_KEY" and isinstance(ev.payload, dict):
                key = ev.payload.get("private_key")
                if not isinstance(key, (bytes, bytearray)) or len(key) != 64:
                    self.logger.error(
                        "PRIVATE_KEY payload bad: %r", ev.payload
                    )
                    return False
                key = bytes(key)
                # persist
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(key)
                    os.chmod(path, 0o600)
                    self.logger.info("saved private key to %s (mode 0600)", path)
                except Exception:
                    self.logger.exception("failed saving key to %s", path)
            elif t == "DISABLED":
                self.logger.error(
                    "private key export is disabled on this firmware; "
                    "client-side DM decryption won't work"
                )
                return False
            else:
                self.logger.error("export_private_key returned %s: %r",
                                  t, ev.payload)
                return False
        self.my_private_key = key
        try:
            self.my_public_key_bytes = derive_public_key(key)
        except Exception:
            self.logger.exception("derive_public_key failed")
            return False
        derived_hex = self.my_public_key_bytes.hex()
        if self.my_pubkey and self.my_pubkey.lower() != derived_hex.lower():
            self.logger.warning(
                "derived pubkey %s != radio-reported %s; "
                "private key may not match this radio",
                derived_hex, self.my_pubkey,
            )
        self.my_pubkey_byte = self.my_public_key_bytes[0]
        return True

    async def adopt_new_private_key(self, key: bytes) -> str:
        """After a new private key is imported to the radio, make the bot adopt
        the new identity: refresh the in-memory pubkey fields and rewrite the
        cached key file. Without this the OLD cached key is reloaded on the next
        start and DM decryption silently breaks. Returns the new pubkey hex."""
        self.my_private_key = bytes(key)
        self.my_public_key_bytes = derive_public_key(self.my_private_key)
        self.my_pubkey = self.my_public_key_bytes.hex()
        self.my_pubkey_byte = self.my_public_key_bytes[0]
        path = self.cfg.privkey_path
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.my_private_key)
                os.chmod(path, 0o600)
            except Exception:
                self.logger.exception("failed to rewrite cached key %s", path)
        self.logger.warning(
            "adopted new radio identity: pubkey=%s (contacts must re-add the "
            "bot; old DMs no longer decrypt)", self.my_pubkey,
        )
        return self.my_pubkey

    async def _seed_channels_from_conf(self) -> None:
        # one-time seed of the channels table from mcbot.conf's [channels].
        #
        # after first run the DB is the runtime authority, !adm channel
        # add/remove manages it and conf is ignored. ro re-seed
        # from conf, delete all rows from the channels table first.
        if not self.cfg.channels:
            return
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM channels")
        if not (row and row["n"] == 0):
            return
        self.logger.info(
            "first-run channel seed: writing %d conf channels to DB",
            len(self.cfg.channels),
        )
        now = int(time.time())
        for idx, (name, secret) in self.cfg.channels.items():
            try:
                await self.db.execute(
                    "INSERT INTO channels(channel_idx,name,secret_hex,"
                    "last_synced_at) VALUES(?,?,?,?)",
                    (idx, name, secret.hex(), now),
                )
            except Exception:
                self.logger.exception("seed channel %d failed", idx)

    async def _load_channels(self) -> None:
        # rebuild in-memory channel state (cfg.channels + channels_by_hash)
        # from the channels DB table, which is authoritative. run after the
        # conf seed and the radio sync so it reflects both.
        rows = await self.db.fetchall(
            "SELECT channel_idx, name, secret_hex FROM channels "
            "ORDER BY channel_idx"
        )
        # rebuild in-memory state from DB
        self.cfg.channels.clear()
        self.channels_by_hash.clear()
        for r in rows:
            idx = r["channel_idx"]
            name = r["name"] or ""
            secret_hex = r["secret_hex"] or ""
            if not name or not secret_hex:
                continue
            try:
                secret = bytes.fromhex(secret_hex)
            except ValueError:
                continue
            if len(secret) != 16:
                continue
            self.cfg.channels[idx] = (name, secret)
            chash = hashlib.sha256(secret).digest()[0]
            self.channels_by_hash[chash] = (idx, name, secret)
            self.logger.info(
                "channel cfg: idx=%d name=%r hash=%02x", idx, name, chash,
            )
        if not self.cfg.channels:
            self.logger.info("no channels configured")

    async def _program_channels_on_radio(self) -> None:
        # push each configured channel's secret into one of the radio's slots
        # so send_chan_msg(idx, ...) encrypts with the correct key. the radio
        # may not persist these across reboots, so we program them on every connect.
        if not self.cfg.channels:
            return
        ok = 0
        for idx, (name, secret) in self.cfg.channels.items():
            try:
                ev = await self.mc.commands.set_channel(idx, name, secret)
            except Exception:
                self.logger.exception(
                    "set_channel(%d, %r) raised", idx, name
                )
                continue
            t = getattr(ev.type, "name", "") if ev else ""
            if t == "OK":
                ok += 1
            else:
                self.logger.warning(
                    "set_channel(%d, %r) returned %s: %r",
                    idx, name, t, getattr(ev, "payload", None),
                )
        self.logger.info("programmed %d/%d channels into radio slots",
                         ok, len(self.cfg.channels))

    async def add_channel(
        self, name: str, secret: bytes
    ) -> tuple[Optional[int], Optional[str]]:
        # add a channel at runtime. uses the lowest unused radio
        # slot, programs the radio, writes to DB, and updates in-memory
        # state.
        # returns (idx, error). on success error is None and idx is the
        # allocated slot; on failure idx is None.
        if not name:
            return None, "name required"
        if not secret or len(secret) != 16:
            return None, "secret must be 16 bytes"
        existing = await self.db.fetchone(
            "SELECT channel_idx FROM channels WHERE name=?", (name,)
        )
        if existing:
            return None, f"already exists at idx={existing['channel_idx']}"
        used_rows = await self.db.fetchall(
            "SELECT channel_idx FROM channels"
        )
        used = {r["channel_idx"] for r in used_rows}
        max_ch = 40
        new_idx = next(
            (i for i in range(max_ch) if i not in used), None
        )
        if new_idx is None:
            return None, f"all {max_ch} slots in use"
        try:
            ev = await self.mc.commands.set_channel(new_idx, name, secret)
        except Exception as e:
            return None, f"radio set_channel raised: {e}"
        t = getattr(ev.type, "name", "") if ev else ""
        if t != "OK":
            return None, f"radio rejected (response={t})"
        await self.db.execute(
            "INSERT INTO channels(channel_idx,name,secret_hex,last_synced_at) "
            "VALUES(?,?,?,?)",
            (new_idx, name, secret.hex(), int(time.time())),
        )
        self.cfg.channels[new_idx] = (name, secret)
        chash = hashlib.sha256(secret).digest()[0]
        self.channels_by_hash[chash] = (new_idx, name, secret)
        return new_idx, None

    async def remove_channel(
        self, name: str
    ) -> tuple[Optional[int], Optional[str]]:
        # remove a channel by name. clears the radio slot, deletes the
        # DB row, and updates in-memory state.
        # returns (idx, error)
        row = await self.db.fetchone(
            "SELECT channel_idx, secret_hex FROM channels WHERE name=?",
            (name,),
        )
        if not row:
            return None, f"channel {name!r} not found"
        idx = row["channel_idx"]
        old_secret_hex = row["secret_hex"]
        # attempt radio slot clear (empty name + zero key = unset).
        try:
            await self.mc.commands.set_channel(idx, "", b"\x00" * 16)
        except Exception as e:
            self.logger.warning(
                "clear radio slot %d failed: %s", idx, e
            )
        await self.db.execute(
            "DELETE FROM channels WHERE channel_idx=?", (idx,)
        )
        self.cfg.channels.pop(idx, None)
        if old_secret_hex:
            try:
                old_secret = bytes.fromhex(old_secret_hex)
                if len(old_secret) == 16:
                    chash = hashlib.sha256(old_secret).digest()[0]
                    self.channels_by_hash.pop(chash, None)
            except Exception:
                pass
        return idx, None

    def _seen_packet(self, pkt_hash: int) -> bool:
        # return True if pkt_hash was seen recently (filters DM retries)
        if pkt_hash in self._recent_pkt_hash_set:
            return True
        if len(self._recent_pkt_hashes) == self._recent_pkt_hashes.maxlen:
            old = self._recent_pkt_hashes.popleft()
            self._recent_pkt_hash_set.discard(old)
        self._recent_pkt_hashes.append(pkt_hash)
        self._recent_pkt_hash_set.add(pkt_hash)
        return False

    def _seen_message(self, sender_pubkey: str, sender_timestamp: int) -> bool:
        # return True if we've already processed this message.
        # matches on sender_pubkey and sender_timestamp, so meshcore's 3 retries
        # of the same DM (which only differ in the attempt counter) collapse
        # into one command run.
        if not sender_pubkey or sender_timestamp is None:
            return False
        key = (sender_pubkey.lower(), int(sender_timestamp))
        if key in self._recent_msg_key_set:
            return True
        if len(self._recent_msg_keys) == self._recent_msg_keys.maxlen:
            old = self._recent_msg_keys.popleft()
            self._recent_msg_key_set.discard(old)
        self._recent_msg_keys.append(key)
        self._recent_msg_key_set.add(key)
        return False

    def _seen_channel_message(
        self, channel_idx: Optional[int], sender_timestamp: Optional[int],
        text: str,
    ) -> bool:
        # return True if this channel message was already processed by the
        # other decrypt path. matches on channel_idx + sender_timestamp + text:
        # both paths derive these from the same decrypted payload, so they
        # match. when sender_timestamp is absent we can't relliably dedupe (two
        # distinct messages could share text), so we let it through.
        if sender_timestamp is None:
            return False
        key = (channel_idx, int(sender_timestamp), text)
        if key in self._recent_chan_key_set:
            return True
        if len(self._recent_chan_keys) == self._recent_chan_keys.maxlen:
            old = self._recent_chan_keys.popleft()
            self._recent_chan_key_set.discard(old)
        self._recent_chan_keys.append(key)
        self._recent_chan_key_set.add(key)
        return False

    def _cache_identity(self) -> None:
        si = getattr(self.mc, "self_info", None)
        if isinstance(si, dict):
            pk = si.get("public_key")
            if isinstance(pk, str) and len(pk) >= 2:
                self.my_pubkey = pk.lower()
                try:
                    self.my_pubkey_byte = int(pk[0:2], 16)
                except ValueError:
                    self.my_pubkey_byte = None

    async def _log_identity(self) -> None:
        rows = await self.db.fetchall(
            "SELECT key, value FROM device_info "
            "WHERE key LIKE 'self_info.%' OR key LIKE 'device_info.%' "
            "OR key LIKE 'battery.%'"
        )
        info = {}
        for r in rows:
            try:
                info[r["key"]] = json.loads(r["value"])
            except Exception:
                info[r["key"]] = r["value"]
        name = info.get("self_info.name") or info.get("device_info.name")
        pubkey = (
            info.get("self_info.public_key")
            or info.get("device_info.public_key")
        )
        max_contacts = info.get("device_info.max_contacts")
        max_channels = info.get("device_info.max_channels")
        fw_ver = info.get("device_info.ver") or "?"
        fw_build = info.get("device_info.fw_build") or "?"
        model = info.get("device_info.model") or "?"
        self.logger.info(
            "firmware: model=%s ver=%s build=%s",
            model, fw_ver, fw_build,
        )
        freq = info.get("self_info.radio_freq")
        bw = info.get("self_info.radio_bw")
        sf = info.get("self_info.radio_sf")
        cr = info.get("self_info.radio_cr")
        bat = info.get("battery.level")
        bat_disp = (
            f"{bat} mV" if isinstance(bat, (int, float)) and bat > 100
            else f"{bat}%"
        )
        self.logger.info("identity: name=%s pubkey=%s", name, pubkey)
        self.logger.info(
            "device:   max_contacts=%s max_channels=%s battery=%s",
            max_contacts, max_channels, bat_disp,
        )
        if any(x is not None for x in (freq, bw, sf, cr)):
            self.logger.info(
                "radio:    freq=%s MHz bw=%s kHz sf=%s cr=%s",
                freq, bw, sf, cr,
            )

    async def _on_connected(self, event) -> None:
        reconnected = False
        if event and isinstance(event.payload, dict):
            reconnected = bool(event.payload.get("reconnected"))
        self.logger.info(
            "radio %s", "RECONNECTED" if reconnected else "CONNECTED"
        )
        if reconnected:
            # the library re-established the transport in place (short blip)
            # and already re-sent APP_START; refresh whatever the radio may
            # have changed across its restart and force a contact re-sync.
            self._contacts_dirty = True
            try:
                await self.refresh_self_info()
            except Exception:
                self.logger.exception("post-reconnect self_info refresh failed")

    def _trigger_reconnect_restart(self, why: str) -> None:
        # recover the radio link with a full teardown + rebuild via the
        # amain() outer loop (same path as '!adm restart'), so recovery is
        # always a complete, well-tested startup resync.
        if self.stop_event.is_set() or self._reconnect_restart_armed:
            return
        self._reconnect_restart_armed = True
        self.logger.warning(
            "radio link lost (%s) — restarting to reconnect", why
        )
        self.restart_requested = True
        self.stop_event.set()

    async def _on_disconnected(self, event) -> None:
        reason = None
        if event and isinstance(event.payload, dict):
            reason = event.payload.get("reason")
        self.logger.warning("radio DISCONNECTED reason=%s", reason)
        # "manual_disconnect" is our own shutdown/restart teardown. Anything
        # else means the library has given up (it only emits DISCONNECTED
        # once auto-reconnect is exhausted — 3 attempts over ~3s — or off),
        # so without action here the bot would sit dead forever.
        if reason != "manual_disconnect":
            self._trigger_reconnect_restart(f"disconnect: {reason}")

    async def _watchdog_runner(self) -> None:
        # Periodic link-liveness probe. A silently power-cycled radio on TCP
        # leaves a half-open socket that never raises connection_lost, and an
        # idle bot may not send for hours — so the dead link would go
        # unnoticed. The probe forces traffic; two consecutive silent probes
        # trigger the reconnect/restart path. Interval is a runtime setting
        # (watchdog_interval, 0 = disabled), re-read every cycle.
        misses = 0
        while not self.stop_event.is_set():
            interval = self.watchdog_interval or 0
            wait = interval if interval > 0 else 60.0
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                pass
            if interval <= 0 or self.mc is None:
                continue
            try:
                ev = await asyncio.wait_for(
                    self.mc.commands.send_device_query(), timeout=10.0
                )
                # any reply — even ERROR — proves the link is alive
                ok = ev is not None
            except Exception:
                ok = False
            if ok:
                misses = 0
                continue
            misses += 1
            self.logger.warning(
                "watchdog: radio unresponsive to device query (%d/2)", misses
            )
            if misses >= 2:
                self._trigger_reconnect_restart("watchdog: radio unresponsive")
                return

    # command dispatch
    async def _dispatch_command(self, ctx: CommandContext) -> None:
        cs = self.loader.match(ctx.message_text)
        if not cs:
            return

        # read effective config from command_config table. script-level
        # attributes (cs.allow_dm, cs.dm_only, etc.) are only fall-backs
        # used when a DB row is missing or a column is NULL. this means
        # operator edits in the DB take effect on the next invocation
        # without needing a reload or restart.
        cfg_row = await self.db.fetchone(
            "SELECT enabled, cooldown_seconds, "
            "allowed_channels, allow_dm, dm_only, process_queued "
            "FROM command_config WHERE command=?",
            (cs.name,),
        )
        enabled = True
        cooldown = cs.cooldown_default
        allowed_channels = cs.allowed_channels
        allow_dm = cs.allow_dm
        dm_only = cs.dm_only
        process_queued = cs.process_queued
        if cfg_row:
            if cfg_row["enabled"] is not None:
                enabled = bool(cfg_row["enabled"])
            if cfg_row["cooldown_seconds"] is not None:
                cooldown = int(cfg_row["cooldown_seconds"])
            raw_chans = cfg_row["allowed_channels"]
            if raw_chans is not None:
                try:
                    parsed = json.loads(raw_chans)
                except Exception:
                    parsed = [
                        s.strip()
                        for s in str(raw_chans).split(",")
                        if s.strip()
                    ]
                # empty list/string in DB = "no restriction" (override
                # any script default). non-empty replaces the default.
                allowed_channels = parsed or None
            if cfg_row["allow_dm"] is not None:
                allow_dm = bool(cfg_row["allow_dm"])
            if cfg_row["dm_only"] is not None:
                dm_only = bool(cfg_row["dm_only"])
            if cfg_row["process_queued"] is not None:
                process_queued = bool(cfg_row["process_queued"])

        if not enabled:
            return
        if ctx.from_queue and not process_queued:
            # command arrived while the bot was offline and sat in the
            # radio's queue; replying now (often hours late, and with no
            # routing path recorded) is noise unless explicitly opted in.
            self._queued_cmds_skipped += 1
            self.logger.info(
                "command '%s' from offline backlog ignored "
                "(process_queued off) sender=%s",
                cs.name, ctx.sender_name or ctx.sender_pubkey_prefix,
            )
            return
        if ctx.is_dm and not allow_dm:
            return
        if dm_only and not ctx.is_dm:
            self.logger.info(
                "command '%s' is DM-only; ignoring channel invocation "
                "from %s in %s",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey_prefix,
                ctx.channel_name,
            )
            return

        # resolve full pubkey if we only have a prefix. needed for both the
        # block check and the group-based auth lookup
        if not ctx.sender_pubkey and ctx.sender_pubkey_prefix:
            candidates = await self._pubkey_candidates(ctx)
            if len(candidates) == 1:
                ctx.sender_pubkey = candidates[0]

        # blocked user check: silently drop anything from them
        if ctx.sender_pubkey and await self.is_user_blocked(ctx.sender_pubkey):
            self.logger.info(
                "command '%s' refused: %s is blocked",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey[:12],
            )
            return

        if not ctx.is_dm and allowed_channels:
            cn = (ctx.channel_name or "").lower()
            cn_no_hash = cn.lstrip("#")
            allow_low = [str(x).lower() for x in allowed_channels]
            if (
                cn not in allow_low
                and cn_no_hash not in allow_low
                and str(ctx.channel_idx) not in allow_low
            ):
                return

        # authorization is groups-only and fail-closed: a command runs only
        # if it is granted to a group the caller belongs to, or to the
        # 'public' group (which is also how a command is made open to all).
        # an ungranted command is denied to everyone but owners (who hold
        # the '*' grant).
        ok = await self.is_authorized_for_command(ctx.sender_pubkey, cs.name)
        if not ok:
            self.logger.info(
                "command '%s' denied (not authorized) from %s",
                cs.name,
                ctx.sender_name or ctx.sender_pubkey_prefix,
            )
            return

        key = (
            ctx.sender_pubkey
            or ctx.sender_pubkey_prefix
            or ctx.sender_name
        )
        if not key:
            return
        if cooldown > 0:
            row = await self.db.fetchone(
                "SELECT last_used_at FROM command_cooldowns "
                "WHERE pubkey=? AND command=?",
                (key, cs.name),
            )
            now = time.time()
            if row and (now - row["last_used_at"]) < cooldown:
                remaining = cooldown - (now - row["last_used_at"])
                self.logger.info(
                    "command '%s' from %s skipped: in cooldown "
                    "(%.1fs of %ds left, no reply sent)",
                    cs.name,
                    ctx.sender_name or key,
                    remaining,
                    cooldown,
                )
                return
            await self.db.execute(
                "INSERT INTO command_cooldowns(pubkey,command,last_used_at) "
                "VALUES(?,?,?) ON CONFLICT(pubkey,command) DO UPDATE SET "
                "last_used_at=excluded.last_used_at",
                (key, cs.name, now),
            )

        self.logger.info(
            "running command '%s' for %s in %s",
            cs.name,
            ctx.sender_name or key,
            "DM" if ctx.is_dm else f"ch{ctx.channel_idx}({ctx.channel_name})",
        )
        try:
            result = await cs.handle(ctx)
        except Exception:
            self.logger.exception("command '%s' raised", cs.name)
            return
        if result is None:
            return
        replies = [result] if isinstance(result, str) else list(result)
        # auto-pack multi-line replies so a list-returning command doesn't
        # produce one DM per line. channel replies need more headroom
        # because the radio prepends "<sender_name>: " on TX.
        if ctx.is_dm:
            replies = self.paginate(replies, max_chars=120)
        else:
            replies = self.paginate(replies, max_chars=100)
        # Optional fixed delay before transmitting — placed here, AFTER the
        # handler did all its lookups/web queries, so only the radio TX is held
        # back. A knob for testing whether nearby repeaters miss our sends when
        # we reply too quickly.
        if self.command_delay > 0:
            await asyncio.sleep(self.command_delay)
        for r in replies:
            await self.send_reply(ctx, r)

    async def _pubkey_candidates(self, ctx: CommandContext) -> list[str]:
        # all full 64-hex pubkeys plausibly identifying the message sender.
        # DM senders only deliver a 6-byte prefix, expand via the contacts table.
        out: list[str] = []
        if ctx.sender_pubkey:
            out.append(ctx.sender_pubkey.lower())
        elif ctx.sender_pubkey_prefix:
            rows = await self.db.fetchall(
                "SELECT public_key FROM contacts "
                "WHERE substr(public_key,1,12)=?",
                (ctx.sender_pubkey_prefix.lower(),),
            )
            for r in rows:
                out.append(r["public_key"].lower())
        return out

    async def is_user_blocked(self, pubkey: str) -> bool:
        """Return True if the user is in the 'blocked' group."""
        if not pubkey:
            return False
        row = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups "
            "WHERE lower(pubkey)=? AND group_name='blocked' LIMIT 1",
            (pubkey.lower(),),
        )
        return row is not None

    async def is_authorized_for_command(
        self, pubkey: Optional[str], command: str
    ) -> bool:
        # True if `command` is runnable by `pubkey`. This is the single
        # authorization gate (fail-closed: no grant == denied). Two ways to
        # qualify:
        # 1. An "all users" group (all_users=1 — every user is a member, the
        #    '*' membership) lists `command` (or '*'). Anyone, even an
        #    unresolved sender, can run it. 'public' is seeded all_users=1, so
        #    granting a command to 'public' is how you make it open to all.
        #    The block check has already filtered out blocked users earlier
        #    in the dispatch pipeline.
        # 2. `pubkey` is an explicit member of any group whose command list
        #    grants `command` (or '*'). Owners hold '*', so they run anything.

        row = await self.db.fetchone(
            "SELECT 1 FROM bot_group_commands gc "
            "JOIN bot_groups g ON g.name = gc.group_name "
            "WHERE g.all_users=1 AND gc.command IN (?, '*') LIMIT 1",
            (command,),
        )
        if row:
            return True
        if not pubkey:
            return False
        row = await self.db.fetchone(
            "SELECT 1 FROM bot_user_groups ug "
            "JOIN bot_group_commands gc ON gc.group_name = ug.group_name "
            "WHERE lower(ug.pubkey)=? "
            "AND (gc.command=? OR gc.command='*') LIMIT 1",
            (pubkey.lower(), command),
        )
        return row is not None

    async def effective_groups_for_user(
        self, pubkey: Optional[str]
    ) -> list[str]:
        # all groups "pubkey" effectively belongs to: explicit memberships
        # plus every all-users group (the '*' membership). sorted, de-duped.
        # used by !whoami and the user-detail views. 'blocked' is excluded
        # from the all-users side (a block is always explicit)
        rows = await self.db.fetchall(
            "SELECT name FROM bot_groups "
            "WHERE all_users=1 AND name != 'blocked' "
            "UNION "
            "SELECT group_name FROM bot_user_groups WHERE lower(pubkey)=? "
            "ORDER BY name",
            ((pubkey or "").lower(),),
        )
        return [r["name"] for r in rows]

    async def resolve_target_user(
        self, target: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        # resolve a name or pubkey provided into (pubkey, name, error_msg).
        #
        # 64-hex string: returned as pubkey, name pulled from contacts if known.
        # 12-hex string: prefix lookup against contacts.
        # anything else: treated as an adv_name lookup against contacts.
        #
        # on error, returns (None, None, error_msg).
        if not target:
            return None, None, "missing target"
        s = target.strip()
        low = s.lower()
        is_hex = all(c in "0123456789abcdef" for c in low)

        if is_hex and len(low) == 64:
            row = await self.db.fetchone(
                "SELECT adv_name FROM contacts WHERE public_key=?", (low,)
            )
            name = row["adv_name"] if row else None
            return low, name, None

        if is_hex and len(low) == 12:
            rows = await self.db.fetchall(
                "SELECT public_key, adv_name FROM contacts "
                "WHERE substr(public_key,1,12)=?",
                (low,),
            )
            if not rows:
                return None, None, f"no contact with prefix {low}"
            if len(rows) > 1:
                names = ", ".join(r["adv_name"] or "?" for r in rows[:5])
                return None, None, f"ambiguous prefix {low}: {names}"
            return rows[0]["public_key"], rows[0]["adv_name"], None

        rows = await self.db.fetchall(
            "SELECT public_key, adv_name FROM contacts WHERE adv_name=?",
            (s,),
        )
        if not rows:
            return None, None, f"no contact named {s!r}"
        if len(rows) > 1:
            prefixes = ", ".join(r["public_key"][:12] for r in rows[:5])
            return (
                None, None,
                f"ambiguous name {s!r}: {prefixes} (use pubkey or 12-hex prefix)",
            )
        return rows[0]["public_key"], rows[0]["adv_name"], None

    @staticmethod
    def paginate(lines: list, max_chars: int = 120) -> list:
        # greedy-pack lines (joined by '\\n') into messages of up to
        # max_chars characters. single lines exceeding max_chars are kept
        # intact (the radio will reject them rather than us splitting mid-word).
        # meshcore DM payload caps around ~140 chars after framing. 120 leaves
        # room for path overhead and channel sender-name prepends.
        out: list = []
        current = ""
        for line in lines:
            if not isinstance(line, str):
                line = str(line)
            if not current:
                current = line
                continue
            candidate = current + "\n" + line
            if len(candidate) <= max_chars:
                current = candidate
            else:
                out.append(current)
                current = line
        if current:
            out.append(current)
        return out

    async def audit_log(
        self,
        actor_pubkey: Optional[str],
        actor_name: Optional[str],
        action: str,
        target: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO bot_audit_log(ts, actor_pubkey, actor_name, action, target, detail) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor_pubkey, actor_name, action, target, detail),
        )

    async def seed_command_configs(self) -> int:
        # for each loaded command, ensure a 'command_config' row exists.
        # existing rows are left untouched, operator edits in the DB are the
        # source of truth.
        #
        # note: seeding a command does mot grant it to anyone. Authorization
        # is groups-only and fail-closed, so a freshly-seeded command is
        # runnable only by owners (the '*' grant) until an operator grants it
        # to a group (use '!adm group grant public <cmd>' to make it open).
        # returns the count of rows newly inserted.
        seeded = 0
        for cs in self.loader.commands.values():
            row = await self.db.fetchone(
                "SELECT 1 FROM command_config WHERE command=?", (cs.name,)
            )
            if row:
                continue
            try:
                triggers_json = json.dumps(cs.triggers)
            except Exception:
                triggers_json = None
            try:
                allowed_json = (
                    json.dumps(cs.allowed_channels)
                    if cs.allowed_channels is not None
                    else None
                )
            except Exception:
                allowed_json = None
            await self.db.execute(
                "INSERT INTO command_config "
                "(command, enabled, cooldown_seconds, allowed_channels, "
                " triggers, description, allow_dm, dm_only, process_queued) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cs.name,
                    cs.cooldown_default,
                    allowed_json,
                    triggers_json,
                    cs.description,
                    1 if cs.allow_dm else 0,
                    1 if cs.dm_only else 0,
                    1 if cs.process_queued else 0,
                ),
            )
            seeded += 1
            self.logger.info(
                "seeded command_config for '%s' from script defaults", cs.name
            )
        return seeded

    async def _bootstrap_admin_state(self) -> None:
        """Idempotent: ensure default groups exist and owners from config are
        added. Called once on startup after the DB schema is ready."""
        now = int(time.time())
        # default groups: (name, description, is_system, all_users). 'public'
        # is an all-users group (the '*' member) so commands granted to it are
        # open to everyone and it shows up in !whoami. all_users is only set on
        # first insert (it's not in the ON CONFLICT update), so an operator who
        # later runs '!adm group restrict public' isn't overridden at restart.
        defaults = [
            ("owner",   "Full administrative access",                1, 0),
            ("admin",   "Subset of administrative commands",         1, 0),
            ("blocked", "Users denied command dispatch entirely",    1, 0),
            ("public",  "Commands runnable by anyone (not blocked)", 1, 1),
            ("user",    "Regular users with non-admin commands",     0, 0),
        ]
        for name, desc, is_system, all_users in defaults:
            await self.db.execute(
                "INSERT INTO bot_groups(name, description, is_system, created_at, all_users) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, is_system=excluded.is_system",
                (name, desc, is_system, now, all_users),
            )
        # owner group always has the '*' grant
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_group_commands(group_name, command) "
            "VALUES('owner', '*')",
        )
        # seed owners from config
        for pk in self.cfg.owner_pubkeys:
            row = await self.db.fetchone(
                "SELECT adv_name FROM contacts WHERE public_key=?", (pk,)
            )
            name = row["adv_name"] if row else None
            await self.db.execute(
                "INSERT INTO bot_users(pubkey, name, added_by, added_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(pubkey) DO UPDATE SET "
                "name=COALESCE(excluded.name, bot_users.name)",
                (pk, name, "config", now),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO bot_user_groups(pubkey, group_name) "
                "VALUES(?, 'owner')",
                (pk,),
            )
        if self.cfg.owner_pubkeys:
            self.logger.info(
                "admin bootstrap: %d owner(s) from config",
                len(self.cfg.owner_pubkeys),
            )

    # send primitives (shared by command replies and manual sends)
    async def send_dm_to(self, pubkey: str, text: str, disp_name: str = ""):
        # send a DM to a pubkey with the configured retry/ACK behavior.
        # returns the final MSG_SENT Event, or None if never ACKed. logs
        # delivery outcome; raising is left to the caller's try/except."""
        pk = pubkey.lower()
        to_disp = disp_name or pk[:12]
        snippet = (text[:60] + "…") if len(text) > 60 else text
        self.logger.info("sending DM to=%s: %r", to_disp, snippet)
        contact = None
        lib_contacts = getattr(self.mc, "contacts", None)
        if isinstance(lib_contacts, dict):
            contact = lib_contacts.get(pk)
        # classify the route so a 0-hop direct delivery (no repeater possible)
        # is reported as DIRECT_0HOP rather than a misleading NO_REPEAT.
        # out_path_len: -1 = flood, 0 = direct neighbor, >=1 = multi-hop path.
        route_mode = "unknown"
        if isinstance(contact, dict):
            opl = contact.get("out_path_len")
            if opl == -1:
                route_mode = "flood"
            elif opl == 0:
                route_mode = "direct_0hop"
            elif isinstance(opl, int) and opl >= 1:
                route_mode = "direct_multihop"
        # register the repeat-watch BEFORE sending: send_msg_with_retry can
        # block for seconds across retries, during which repeaters may already
        # be rebroadcasting the first attempt. the no-repeat timer is started
        # only after the send returns (see _start_repeat_timer).
        watch = self._register_repeat_watch(
            kind="dm", text=text, dest_pubkey=pk, disp_name=to_disp,
            route_mode=route_mode,
        )
        t0 = time.monotonic()
        ev = await self.mc.commands.send_msg_with_retry(
            contact or pk, text,
            max_attempts=self.cfg.dm_max_attempts,
            max_flood_attempts=self.cfg.dm_max_flood_attempts,
            flood_after=self.cfg.dm_flood_after,
        )
        self._start_repeat_timer(watch)
        dt_ms = int((time.monotonic() - t0) * 1000)
        if ev is None:
            self.logger.warning(
                "DM not ACKed to=%s after %d attempts (%dms)",
                to_disp, self.cfg.dm_max_attempts, dt_ms,
            )
        elif getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "DM send rejected to=%s: code=%s reason=%s",
                to_disp,
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        else:
            self.logger.info("DM ACKed to=%s in %dms", to_disp, dt_ms)
        return ev

    async def remove_contact_remote(self, pubkey: str):
        self.logger.info("removing contact pk=%s from radio", pubkey[:12])
        ev = await self.mc.commands.remove_contact(pubkey)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "remove_contact rejected pk=%s: code=%s reason=%s",
                pubkey[:12],
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        return ev

    # ------------------------------------------------------------------
    # Radio contact-table rollover (eviction)
    # ------------------------------------------------------------------
    async def _radio_max_contacts(self, refresh: bool = False) -> Optional[int]:
        # current radio contact capacity. reads the cached
        # device_info.max_contacts (already decoded by the lib); re-queries the
        # radio if absent or refresh=True.
        if not refresh:
            row = await self.db.fetchone(
                "SELECT value FROM device_info "
                "WHERE key='device_info.max_contacts'"
            )
            if row and row["value"]:
                try:
                    return int(json.loads(row["value"]))
                except Exception:
                    pass
        try:
            ev = await self.mc.commands.send_device_query()
            if ev and isinstance(ev.payload, dict):
                await self._upsert_device_info(ev.payload, "device_info")
                mc = ev.payload.get("max_contacts")
                if isinstance(mc, int):
                    return mc
        except Exception:
            self.logger.exception("device query for max_contacts failed")
        return None

    async def _eviction_protected_pubkeys(self) -> set:
        # contacts that must never be evicted: configured owners, all bot
        # users, and anyone the bot exchanged a DM with in the last 24h.
        protected = {pk.lower() for pk in self.cfg.owner_pubkeys}
        for r in await self.db.fetchall("SELECT pubkey FROM bot_users"):
            if r["pubkey"]:
                protected.add(r["pubkey"].lower())
        # outgoing DM rows store the counterparty in sender_pubkey, so one
        # query covers both directions of a conversation.
        cutoff = int(time.time()) - 86400
        rows = await self.db.fetchall(
            "SELECT DISTINCT sender_pubkey FROM direct_messages "
            "WHERE received_at > ? AND sender_pubkey IS NOT NULL",
            (cutoff,),
        )
        for r in rows:
            if r["sender_pubkey"]:
                protected.add(r["sender_pubkey"].lower())
        return protected

    async def evict_radio_contacts(
        self, target_free: Optional[int] = None,
        count: Optional[int] = None, dry_run: bool = False,
    ) -> dict:
        # Evict the stalest contacts from the RADIO to free space. Operates on
        # a fresh radio dump (the DB archive may hold contacts no longer on the
        # radio, so victims must come only from the live table) and NEVER
        # deletes DB rows. Either target_free (keep >= N free slots) or count
        # (remove up to N) drives the count. Returns a result dict.
        async with self._evict_lock:
            try:
                ev = await self.mc.commands.get_contacts(lastmod=0, timeout=15)
            except TypeError:
                ev = await self.mc.commands.get_contacts()
            except Exception as e:
                raise RuntimeError(f"could not read radio contacts: {e}")
            if (
                not ev or getattr(ev.type, "name", "") == "ERROR"
                or not isinstance(ev.payload, dict)
            ):
                raise RuntimeError("could not read radio contacts")
            # the contact pubkey is the dict KEY; inject it so downstream code
            # (and the pure selector) can read c["public_key"].
            contacts = [
                {**c, "public_key": pk}
                for pk, c in ev.payload.items() if isinstance(c, dict)
            ]
            used = len(contacts)
            maxc = await self._radio_max_contacts()
            now = int(time.time())

            tf = self.evict_headroom if target_free is None else int(target_free)
            if count is not None:
                need = max(0, int(count))
            elif maxc is None:
                raise RuntimeError(
                    "radio max contacts unknown; cannot compute headroom"
                )
            else:
                need = max(0, tf - (maxc - used))
            need = min(need, self.cfg.radio_evict_max_per_run)

            protected = await self._eviction_protected_pubkeys()
            ptypes = self.cfg.radio_evict_protect_types
            syncmap: dict = {}
            for r in await self.db.fetchall(
                "SELECT public_key, last_synced_at FROM contacts"
            ):
                if r["public_key"]:
                    syncmap[r["public_key"].lower()] = r["last_synced_at"] or 0

            victims = select_eviction_victims(
                contacts, protected_pubkeys=protected, protected_types=ptypes,
                db_synced_at=syncmap, need=need, now=now,
            )
            eligible_total = sum(
                1 for c in contacts
                if (c.get("public_key") or "").lower() not in protected
                and c.get("type") not in ptypes
            )
            result = {
                "used": used, "max": maxc,
                "free_before": (maxc - used) if maxc is not None else None,
                "target_free": (None if count is not None else tf),
                "protected": len(protected), "eligible": eligible_total,
                "evicted": [], "failed": 0, "shortfall": 0, "dry_run": dry_run,
            }
            if dry_run:
                result["evicted"] = [
                    {"pubkey": c["public_key"], "name": c.get("adv_name")}
                    for c in victims
                ]
                result["shortfall"] = max(0, need - len(victims))
                return result

            consecutive_err = 0
            for c in victims:
                pk = c["public_key"]
                rev = await self.remove_contact_remote(pk)
                ok = rev is not None and getattr(rev.type, "name", "") != "ERROR"
                if ok:
                    consecutive_err = 0
                    try:  # keep the lib's contact cache in step (send_dm reads it)
                        cache = getattr(self.mc, "contacts", None)
                        if isinstance(cache, dict):
                            cache.pop(pk, None)
                    except Exception:
                        pass
                    result["evicted"].append(
                        {"pubkey": pk, "name": c.get("adv_name")}
                    )
                else:
                    result["failed"] += 1
                    consecutive_err += 1
                    if consecutive_err >= 3:
                        self.logger.warning(
                            "radio eviction aborted after 3 consecutive errors"
                        )
                        break
            if result["evicted"]:
                self._contacts_dirty = True
            result["shortfall"] = max(0, need - len(result["evicted"]))
            self.logger.info(
                "radio eviction: used=%s max=%s evicted=%d failed=%d "
                "shortfall=%d (protected=%d eligible=%d)",
                used, maxc, len(result["evicted"]), result["failed"],
                result["shortfall"], len(protected), eligible_total,
            )
            return result

    async def _on_contacts_full(self, event) -> None:
        if not self.evict_enabled:
            self.logger.debug(
                "CONTACTS_FULL received but radio_evict_enabled is off"
            )
            return
        nowm = time.monotonic()
        if nowm - self._last_auto_evict < self.cfg.radio_evict_min_interval:
            self.logger.debug("CONTACTS_FULL within debounce window; skipping")
            return
        self._last_auto_evict = nowm
        self.logger.info(
            "CONTACTS_FULL: radio contact table full; evicting stale contacts"
        )

        async def _run():
            try:
                await self.evict_radio_contacts(target_free=self.evict_headroom)
            except Exception:
                self.logger.exception("auto eviction (CONTACTS_FULL) failed")

        asyncio.create_task(_run())

    async def _on_contact_deleted(self, event) -> None:
        # firmware removed/overwrote a contact — refresh the DB view next tick
        self._contacts_dirty = True

    async def send_advert(self, flood: bool):
        label = "flood" if flood else "zero-hop"
        self.logger.info("sending %s advert", label)
        ev = await self.mc.commands.send_advert(flood=flood)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "advert send rejected: code=%s reason=%s",
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        if flood:
            # anchor the periodic-advert schedule to the most recent flood
            # advert, whatever its source, so the interval is "at least N hours
            # between flood adverts".
            self._last_flood_advert = time.monotonic()
        return ev

    # --- runtime settings (registry-driven; each seeded from config on
    #     first run, then DB-managed — see settings.SETTINGS) ---
    def reset_advert_schedule(self) -> None:
        # restart the periodic-advert schedule from now, so an interval
        # change doesn't trigger an immediate overdue advert.
        self._last_flood_advert = time.monotonic()

    async def load_runtime_settings(self) -> None:
        # DB is authoritative; seed it from config on first run (key absent
        # or unparseable).
        for s in SETTINGS.values():
            row = await self.db.fetchone(
                "SELECT value FROM bot_meta WHERE key=?", (s.key,)
            )
            try:
                value = s.clamp(row["value"])
            except (TypeError, ValueError):
                value = getattr(self.cfg, s.key)
                await self.db.execute(
                    "INSERT INTO bot_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (s.key, str(value)),
                )
            setattr(self, s.key, value)

    async def set_runtime_setting(self, key: str, value):
        # apply + persist a registry setting and run its side-effect hook.
        # validation is the caller's job (management.setting_set).
        s = SETTINGS[key]
        value = s.clamp(value)
        setattr(self, s.key, value)
        await self.db.execute(
            "INSERT INTO bot_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (s.key, str(value)),
        )
        if s.on_set:
            getattr(self, s.on_set)()
        self.logger.info(
            "%s set to %s%s%s",
            s.label, value, f" {s.unit}" if s.unit else "",
            " (disabled)" if not value else "",
        )
        return value

    async def send_channel_text(self, channel_idx: int, text: str):
        # single-shot channel send (channel messages have no ACK). returns
        # the radio Event (or None); logs a rejection. no exception on error.
        snippet = (text[:60] + "…") if len(text) > 60 else text
        self.logger.info(
            "sending channel msg ch=%d: %r", channel_idx, snippet
        )
        # Stamp the message ourselves so a no-repeat retry can resend with the
        # SAME timestamp (idempotent retransmit — see _retry_channel_watch).
        ts = int(time.time())
        watch = self._register_repeat_watch(
            kind="channel", text=text, channel_idx=channel_idx,
        )
        if watch is not None:
            watch.send_timestamp = ts
            watch.retries_left = max(0, int(self.channel_retry_max))
        ev = await self.mc.commands.send_chan_msg(channel_idx, text, timestamp=ts)
        self._start_repeat_timer(watch)
        if ev is not None and getattr(ev.type, "name", "") == "ERROR":
            p = ev.payload if isinstance(ev.payload, dict) else {}
            self.logger.warning(
                "channel send rejected: code=%s reason=%s",
                p.get("error_code") or p.get("code_string"),
                p.get("reason"),
            )
        return ev

    # reply
    async def send_reply(self, ctx: CommandContext, text: str) -> None:
        try:
            if ctx.is_dm:
                pk = ctx.sender_pubkey
                if not pk and ctx.sender_pubkey_prefix:
                    pk, _ = await self.resolve_prefix(
                        ctx.sender_pubkey_prefix
                    )
                if not pk:
                    self.logger.warning(
                        "cannot DM-reply: unresolved sender"
                    )
                    return
                await self.send_dm_to(pk, text, ctx.sender_name or "")
            else:
                if ctx.channel_idx is None:
                    return
                await self.send_channel_text(ctx.channel_idx, text)
        except Exception:
            self.logger.exception("send_reply failed")

    # retention helpers
    async def _trim_global(self, table: str, keep: int) -> None:
        if keep <= 0:
            return
        await self.db.execute(
            f"DELETE FROM {table} WHERE id NOT IN "
            f"(SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    async def _trim_channel_messages(
        self, channel_idx: Optional[int], keep: int
    ) -> None:
        if keep <= 0 or channel_idx is None:
            return
        await self.db.execute(
            "DELETE FROM channel_messages WHERE channel_idx=? AND id NOT IN "
            "(SELECT id FROM channel_messages WHERE channel_idx=? "
            "ORDER BY id DESC LIMIT ?)",
            (channel_idx, channel_idx, keep),
        )

    # lifecycle
    async def run(self) -> int:
        self._parse_log_channels()
        self.loader.load_all()

        self.logger.info("=" * 60)
        self.logger.info("mcbot starting")
        self.logger.info(
            "config: radio=%s db=%s logs_dir=%s commands_dir=%s",
            self.cfg.target_desc(), self.cfg.db_path,
            self.cfg.logs_dir, self.cfg.commands_dir,
        )
        self.logger.info(
            "retention: chan_msgs=%d dms=%d packets=%d",
            self.cfg.max_channel_messages,
            self.cfg.max_dms,
            self.cfg.max_packets,
        )
        self.logger.info(
            "log_channels=%s commands_enabled=%s",
            self.cfg.log_channels, self.cfg.commands_enabled,
        )
        self.logger.info("=" * 60)

        # surface config typos / stale settings loudly so they're not silently
        # ignored (mcbot.conf is not strictly validated).
        for w in self.cfg.config_warnings:
            self.logger.warning("mcbot.conf: %s", w)

        # MeshCore.__init__ forces the "meshcore" logger level from its `debug`
        # arg (debug -> DEBUG, else INFO), overriding setup_logging. Drive that
        # arg from our effective level, then re-assert the level after connect
        # so log_level=DEBUG in mcbot.conf turns on the library's verbose output
        # without needing --debug (and so the TypeError fallback / WARNING /
        # ERROR levels are honored too).
        eff_level = effective_log_level(self.cfg)
        want_debug = eff_level == "DEBUG"
        try:
            if self.cfg.transport == "serial":
                try:
                    self.mc = await MeshCore.create_serial(
                        self.cfg.serial_port,
                        baudrate=self.cfg.serial_baud,
                        debug=want_debug,
                        auto_reconnect=self.cfg.auto_reconnect,
                    )
                except TypeError:
                    self.mc = await MeshCore.create_serial(
                        self.cfg.serial_port, self.cfg.serial_baud
                    )
            else:
                try:
                    self.mc = await MeshCore.create_tcp(
                        self.cfg.host, self.cfg.port,
                        debug=want_debug,
                        auto_reconnect=self.cfg.auto_reconnect,
                    )
                except TypeError:
                    self.mc = await MeshCore.create_tcp(
                        self.cfg.host, self.cfg.port
                    )
        except Exception:
            self.logger.exception("connect failed (%s)", self.cfg.target_desc())
            self.db.close()
            if self.cfg.auto_reconnect:
                # radio likely rebooting/unplugged: have amain() rebuild and
                # retry with backoff rather than exiting the process.
                self.connect_failed = True
                self.restart_requested = True
            return 1

        # undo MeshCore.__init__'s override so [logging] log_level wins
        logging.getLogger("meshcore").setLevel(eff_level)

        self.logger.info("connected to %s", self.cfg.target_desc())

        # enable the library's RX_LOG channel-log decryption. This decodes
        # channel text from RX_LOG frames and, via the library's correlation,
        # back-fills the routing path onto the radio's queued channel messages
        # (so !path works for channel commands).
        try:
            if hasattr(self.mc, "set_decrypt_channel_logs"):
                self.mc.set_decrypt_channel_logs(True)
        except Exception:
            self.logger.exception("set_decrypt_channel_logs failed")

        if self.cfg.device_pin:
            try:
                await self.mc.commands.set_devicepin(int(self.cfg.device_pin))
                self.logger.info("device PIN set")
            except Exception:
                self.logger.exception("device PIN set failed")

        await self.sync_device_info()
        self._cache_identity()
        await self.sync_contacts()
        # seed conf channels into an empty table before syncing radio-reported
        # channels, otherwise the radio's built-in Public channel makes the
        # table non-empty and suppresses the one-time conf seed.
        await self._seed_channels_from_conf()
        await self.sync_channels()
        await self._log_identity()
        ok = await self._setup_private_key()
        if not ok:
            self.logger.warning(
                "client-side DM decryption disabled — DMs received via "
                "RX_LOG_DATA will not be decoded"
            )
        await self._load_channels()
        await self._program_channels_on_radio()
        await self._bootstrap_admin_state()
        await self.seed_command_configs()
        await self.load_runtime_settings()

        # ensure radio contact-table headroom on startup (device_info +
        # contacts have been synced above; owners are bootstrapped into
        # bot_users, so the protected set is ready).
        if self.evict_enabled:
            try:
                self._last_auto_evict = time.monotonic()
                res = await self.evict_radio_contacts(
                    target_free=self.evict_headroom
                )
                if res.get("evicted"):
                    self.logger.info(
                        "startup eviction freed %d radio contact slot(s)",
                        len(res["evicted"]),
                    )
            except Exception:
                self.logger.exception("startup radio eviction failed")

        # subscriptions
        try:
            self._subs.append(self.mc.subscribe(None, self._firehose))
            self._subs.append(
                self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
            )
            self._subs.append(
                self.mc.subscribe(
                    EventType.CHANNEL_MSG_RECV, self._on_channel_msg
                )
            )
            if hasattr(EventType, "ADVERTISEMENT"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.ADVERTISEMENT, self._on_advertisement
                    )
                )
            # the firehose (subscribed to all events above) records raw_hex for
            # the packet monitor; this subscription adds the client-side
            # decrypt+ingest of DMs/channel msgs from RX_LOG (deduped against
            # the radio's queued get_msg delivery).
            if hasattr(EventType, "RX_LOG_DATA"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.RX_LOG_DATA, self._on_rx_log_data
                    )
                )
            if hasattr(EventType, "NEW_CONTACT"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.NEW_CONTACT, self._on_new_contact
                    )
                )
            if hasattr(EventType, "CONTACTS_FULL"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.CONTACTS_FULL, self._on_contacts_full
                    )
                )
            if hasattr(EventType, "CONTACT_DELETED"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.CONTACT_DELETED, self._on_contact_deleted
                    )
                )
            if hasattr(EventType, "MESSAGES_WAITING"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.MESSAGES_WAITING, self._on_messages_waiting
                    )
                )
            if hasattr(EventType, "NO_MORE_MSGS"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.NO_MORE_MSGS, self._on_no_more_msgs
                    )
                )
            if hasattr(EventType, "CONNECTED"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.CONNECTED, self._on_connected
                    )
                )
            if hasattr(EventType, "DISCONNECTED"):
                self._subs.append(
                    self.mc.subscribe(
                        EventType.DISCONNECTED, self._on_disconnected
                    )
                )
        except Exception:
            self.logger.exception("subscribing to events failed")

        # messages fetched from here until the first NO_MORE_MSGS are the
        # radio's offline backlog (queued while the bot was down) — commands
        # in them are skipped unless the command opts in via process_queued.
        # Pump the whole backlog ourselves BEFORE starting the library's
        # auto-fetcher: its loop only wakes on MESSAGES_WAITING pushes, which
        # the radio does not send for pre-connect messages (they would leak
        # out on the next live message, past the drain window, and be
        # handled as live).
        self._begin_queue_drain()
        try:
            await self._drain_radio_queue()
        except Exception:
            self.logger.exception("startup queue drain failed")
        try:
            await self.mc.start_auto_message_fetching()
        except Exception:
            self.logger.exception("start_auto_message_fetching failed")

        async def periodic_contacts():
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=60.0
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                if self._contacts_dirty:
                    self._contacts_dirty = False
                    try:
                        await self.sync_contacts()
                    except Exception:
                        self.logger.exception("periodic contacts sync failed")

        async def periodic_advert():
            # anchor the schedule to startup so the first periodic advert is a
            # full interval away (no immediate flood on boot). Reads the
            # runtime-mutable interval each tick, so changes take effect within
            # the poll period without a restart.
            self._last_flood_advert = time.monotonic()
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=60.0)
                    break
                except asyncio.TimeoutError:
                    pass
                hrs = self.advert_interval_hours
                if hrs and hrs > 0:
                    if time.monotonic() >= self._last_flood_advert + hrs * 3600:
                        try:
                            self.logger.info(
                                "periodic flood advert (every %dh)", hrs
                            )
                            await self.send_advert(flood=True)
                        except Exception:
                            self.logger.exception("periodic flood advert failed")

        periodic_task = asyncio.create_task(periodic_contacts())
        advert_task = asyncio.create_task(periodic_advert())
        watchdog_task = asyncio.create_task(self._watchdog_runner())

        self._start_web()

        self.logger.info("bot running; press Ctrl-C to stop")
        try:
            await self.stop_event.wait()
        finally:
            await self.shutdown([periodic_task, advert_task, watchdog_task])
        return 0

    def _start_web(self) -> None:
        # start the in-process web admin UI/API as an asyncio task, if
        # enabled and configured. Failures here never abort the bot.
        if not self.cfg.web_enabled:
            return
        if not self.cfg.web_session_secret:
            self.logger.error(
                "web: [web] enabled but session_secret is unset — "
                "web UI/API NOT started"
            )
            return
        try:
            from webapi.app import make_server
            self._web_server = make_server(self)
            self._web_task = asyncio.create_task(self._web_server.serve())
            scheme = "https" if self.cfg.web_tls_cert else "http"
            self.logger.info(
                "web admin UI/API on %s://%s:%d (docs at /api/docs)",
                scheme, self.cfg.web_host, self.cfg.web_port,
            )
        except Exception:
            self.logger.exception("web server failed to start")
            self._web_server = None
            self._web_task = None

    async def shutdown(self, tasks=None) -> None:
        self.logger.info(
            "shutdown initiated; events_seen=%d", self.event_count
        )
        # stop the web server first so it isn't serving against a tearing-down
        # bot. should_exit makes uvicorn's serve() task return promptly.
        if self._web_server is not None:
            self._web_server.should_exit = True
            if self._web_task is not None:
                try:
                    await asyncio.wait_for(self._web_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._web_task.cancel()
                except Exception:
                    self.logger.exception("web server shutdown error")
            self._web_server = None
            self._web_task = None
        for t in (tasks or []):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                self.logger.exception("background task cleanup failed")
        # cancel any outstanding repeat-watch no-repeat timers
        for w in list(self._repeat_watches):
            tt = w.timer_task
            if tt is not None and not tt.done():
                tt.cancel()
        self._repeat_watches.clear()
        if self.mc:
            try:
                await self.mc.stop_auto_message_fetching()
            except Exception:
                pass
            for sub in self._subs:
                try:
                    self.mc.unsubscribe(sub)
                except Exception:
                    pass
            try:
                await self.mc.disconnect()
            except Exception:
                pass
        self.db.close()
        self.logger.info("shutdown complete")


