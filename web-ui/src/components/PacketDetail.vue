<script setup>
import { computed } from 'vue'
import { fmtDateTime } from '../time.js'

const props = defineProps({
  packet: { type: Object, required: true },
})

// One concise line per packet_type describing what the event is and what the
// bot does with it — so a non-raw row explains itself without reading the code.
const TYPE_INFO = {
  ADVERT:
    "A node broadcast an advertisement (presence beacon). The bot learns its public key, name, and route and tracks it as a contact.",
  DM: "A direct message addressed to the bot, decrypted from RF. Dispatched to a command handler if it is a command, else logged.",
  GRPCHAT:
    "A channel (group) message the bot holds the key for. Logged, and dispatched if it is a command.",
  DM_SENT: "The radio accepted an outbound DM from the bot for transmission.",
  ACK: "Acknowledgement that a message the bot sent reached its destination.",
  CMD_ACK: "The radio firmware accepted a command the bot issued (OK frame).",
  CMD_ERR: "The radio firmware rejected a command the bot issued (ERROR frame).",
  CONTACT: "One contact entry streamed from the radio during a contact-table sync.",
  NEW_CONTACT: "The radio added a new contact after hearing a previously-unknown node.",
  CONTACTS_SYNC: "A contact-table sync from the radio completed.",
  CONTACT_DEL: "A contact was removed from the radio's contact table.",
  CONTACTS_FULL:
    "The radio's contact table is full and is dropping new nodes. Triggers automatic eviction when enabled.",
  REPEAT: "A repeater was heard rebroadcasting a message the bot sent (delivery is propagating).",
  NO_REPEAT:
    "No repeater was heard rebroadcasting the bot's message within the timeout window.",
  RETRY:
    "No repeater was heard, so the bot resent the channel message — an identical retransmit (same timestamp) that only repeaters which missed it pick up.",
  DIRECT_0HOP:
    "The bot's DM went straight to a 0-hop neighbor, so no repeater is involved — nothing to rebroadcast.",
  PATH_UPDATE: "Routing-path information for a contact was updated.",
  PATH_RESPONSE: "A response carrying the route to a contact.",
  TRACE: "A path-trace (traceroute) response.",
  TELEMETRY: "A telemetry response from a node.",
  BATTERY: "The radio reported its battery status.",
  STATUS: "A status response from the radio.",
  DEVICE_INFO: "The radio reported its device information.",
  SELF_INFO: "The radio reported its own identity/configuration.",
  CHANNEL_INFO: "The radio reported channel configuration.",
  TIME: "The radio reported its current clock time.",
  MSG_WAIT: "The radio signaled that messages are queued; the bot will fetch them.",
  NO_MORE_MSGS: "The radio's queued-message fetch is drained — nothing more waiting.",
  LOGIN_OK: "A login to a repeater/room server succeeded.",
  LOGIN_FAIL: "A login to a repeater/room server failed.",
  CONNECTION: "The bot's link to the radio changed state (connected/disconnected).",
  RX_LOG: "A raw RF frame captured from the radio's receive log.",
  LOG: "A log line emitted by the radio.",
}

const info = computed(() => TYPE_INFO[props.packet.packet_type] || '')

function has(v) {
  return v !== null && v !== undefined && v !== ''
}

// Curated envelope: the indexed columns the bot pulled out of the packet,
// in a readable order, omitting whatever this packet type didn't set.
const envelope = computed(() => {
  const p = props.packet
  const rows = []
  const add = (k, v, opts = {}) => {
    if (has(v)) rows.push({ k, v, ...opts })
  }
  add('received', p.received_at, { time: true })
  add('event_type', p.event_type)
  add('packet_type', p.packet_type)
  add('channel', p.channel_idx)
  add('from', p.sender_name)
  add('pubkey', p.sender_pubkey || p.sender_pubkey_prefix, { mono: true })
  add('path', has(p.path_len) ? `${p.path} (${p.path_len} hops)` : p.path, {
    mono: true,
  })
  add('SNR', p.snr)
  add('RSSI', p.rssi)
  add('text', p.text)
  return rows
})

function parseJson(s) {
  if (!s) return null
  if (typeof s === 'object') return s
  try {
    return JSON.parse(s)
  } catch {
    return null
  }
}

// payload_json carries the raw library event payload; drop the {"value": ...}
// wrapper the bot adds for non-dict payloads so the actual value shows flat.
const payload = computed(() => {
  const obj = parseJson(props.packet.payload_json)
  if (!obj || typeof obj !== 'object') return null
  const keys = Object.keys(obj)
  if (keys.length === 0) return null
  return obj
})

const attributes = computed(() => {
  const obj = parseJson(props.packet.attributes_json)
  if (!obj || typeof obj !== 'object' || Object.keys(obj).length === 0)
    return null
  return obj
})

function isTimeKey(k) {
  return k === 'timestamp' || k.endsWith('_timestamp') || k.endsWith('_at') ||
    k === 'last_advert'
}
function fmtVal(k, v) {
  if (v === null || v === undefined) return ''
  if (isTimeKey(k) && typeof v === 'number' && v > 0) return fmtDateTime(v)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <div class="pdetail">
    <p v-if="info" class="pdesc">{{ info }}</p>

    <h4>Packet</h4>
    <div class="kv">
      <template v-for="r in envelope" :key="r.k">
        <div class="k">{{ r.k }}</div>
        <div :class="{ mono: r.mono }">{{ r.time ? fmtDateTime(r.v) : r.v }}</div>
      </template>
    </div>

    <template v-if="payload">
      <h4>Payload</h4>
      <div class="kv">
        <template v-for="(v, k) in payload" :key="k">
          <div class="k">{{ k }}</div>
          <div class="mono">{{ fmtVal(k, v) }}</div>
        </template>
      </div>
    </template>

    <template v-if="attributes">
      <h4>Attributes</h4>
      <div class="kv">
        <template v-for="(v, k) in attributes" :key="k">
          <div class="k">{{ k }}</div>
          <div class="mono">{{ fmtVal(k, v) }}</div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.pdetail {
  padding: 12px;
}
.pdesc {
  margin: 0 0 12px;
  color: var(--muted);
  line-height: 1.5;
}
.pdetail h4 {
  margin: 14px 0 6px;
}
.pdetail h4:first-of-type {
  margin-top: 0;
}
.pdetail .kv {
  word-break: break-all;
}
</style>
