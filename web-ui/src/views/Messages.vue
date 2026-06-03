<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { api, wsUrl } from '../api.js'

const channels = ref([])
const selectedCh = ref(null) // channel_idx or 'dm'
const dmTarget = ref(null) // {public_key, adv_name} when DMing a contact
const messages = ref([])
const msgBody = ref(null) // scroll container for the message list
const contacts = ref([])
const contactSearch = ref('')
const error = ref('')
const composeText = ref('')
const sending = ref(false)
const sendError = ref('')
let ws = null

const filteredContacts = computed(() => {
  const q = contactSearch.value.toLowerCase()
  if (!q) return contacts.value
  return contacts.value.filter(
    (c) =>
      (c.adv_name || '').toLowerCase().includes(q) ||
      (c.public_key || '').toLowerCase().includes(q),
  )
})

// Where a composed message would go, or null if no valid target.
const sendTarget = computed(() => {
  if (typeof selectedCh.value === 'number') {
    const ch = channels.value.find((c) => c.channel_idx === selectedCh.value)
    return { kind: 'channel', idx: selectedCh.value, label: ch?.name || `#${selectedCh.value}` }
  }
  if (selectedCh.value === 'dm' && dmTarget.value) {
    return {
      kind: 'dm',
      pubkey: dmTarget.value.public_key,
      label: dmTarget.value.adv_name || dmTarget.value.public_key.slice(0, 12),
    }
  }
  return null
})
const composePlaceholder = computed(() =>
  sendTarget.value
    ? `Message ${sendTarget.value.label}…`
    : 'Pick a channel or a contact to send to…',
)

async function loadChannels() {
  channels.value = (await api('/channels')).items
  if (selectedCh.value === null && channels.value.length) {
    selectCh(channels.value[0].channel_idx)
  }
}
async function loadContacts() {
  contacts.value = (await api('/contacts?limit=500')).items
}
// Jump the message list to the newest (bottom) message. Waits a tick so
// the freshly-set messages have rendered before measuring scrollHeight.
async function scrollToBottom() {
  await nextTick()
  const el = msgBody.value
  if (el) el.scrollTop = el.scrollHeight
}

async function selectCh(idx) {
  selectedCh.value = idx
  dmTarget.value = null
  const r = await api(`/channel-messages?channel_idx=${idx}&limit=100`)
  messages.value = r.items.slice().reverse() // oldest first
  await scrollToBottom()
}
async function selectAllDMs() {
  selectedCh.value = 'dm'
  dmTarget.value = null
  const r = await api('/direct-messages?limit=100')
  messages.value = r.items.slice().reverse()
  await scrollToBottom()
}
async function selectContact(c) {
  selectedCh.value = 'dm'
  dmTarget.value = c
  const r = await api(`/direct-messages?pubkey=${encodeURIComponent(c.public_key)}&limit=100`)
  messages.value = r.items.slice().reverse()
  await scrollToBottom()
}

async function send() {
  const text = composeText.value.trim()
  if (!text || !sendTarget.value || sending.value) return
  sending.value = true
  sendError.value = ''
  try {
    if (sendTarget.value.kind === 'channel') {
      await api('/send/channel', {
        method: 'POST',
        json: { channel_idx: sendTarget.value.idx, text },
      })
    } else {
      await api('/send/dm', {
        method: 'POST',
        json: { pubkey: sendTarget.value.pubkey, text },
      })
    }
    composeText.value = '' // the live feed echoes the sent message back
  } catch (e) {
    sendError.value = e.message
  } finally {
    sending.value = false
  }
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : ''
}

// Channel messages arrive on-wire as "Name: body" (the radio prepends it
// so the bot can demux senders client-side). The sender_name is already
// shown in the meta line, so strip the prefix from the body when present.
function stripSenderPrefix(text, senderName) {
  if (!text || !senderName) return text || ''
  const prefix = `${senderName}: `
  return text.startsWith(prefix) ? text.slice(prefix.length) : text
}

// Tokenise the message body so the template can render @[mention] as a
// styled span and bare http(s) URLs as anchors. Trailing punctuation that
// commonly follows a URL in prose (.,;:!?)) is stripped off the link and
// kept as plain text so "see https://example.com." doesn't link the dot.
const SEG_RE = /(https?:\/\/[^\s<>]+)|@\[([^\]]+)\]/g
function messageSegments(m) {
  const text = stripSenderPrefix(m.text, m.sender_name)
  const segs = []
  let last = 0, hit
  SEG_RE.lastIndex = 0
  while ((hit = SEG_RE.exec(text)) !== null) {
    if (hit.index > last) segs.push({ type: 'text', value: text.slice(last, hit.index) })
    if (hit[1]) {
      const trim = hit[1].match(/^(.*?)([.,;:!?)]+)$/)
      if (trim) {
        segs.push({ type: 'link', value: trim[1] })
        segs.push({ type: 'text', value: trim[2] })
      } else {
        segs.push({ type: 'link', value: hit[1] })
      }
    } else {
      segs.push({ type: 'mention', value: hit[2] })
    }
    last = SEG_RE.lastIndex
  }
  if (last < text.length) segs.push({ type: 'text', value: text.slice(last) })
  return segs
}

onMounted(async () => {
  try {
    await loadChannels()
    await loadContacts()
  } catch (e) {
    error.value = e.message
  }
  ws = new WebSocket(wsUrl('/ws/messages'))
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data)
    // If the user is already at the bottom, stay pinned to it as new
    // messages arrive; if they've scrolled up to read history, don't yank.
    const el = msgBody.value
    const atBottom = el
      ? el.scrollHeight - el.scrollTop - el.clientHeight < 60
      : true
    let appended = false
    if (selectedCh.value === 'dm' && m.kind === 'dm') {
      // In a specific conversation, only append messages for that peer.
      if (!dmTarget.value || m.sender_pubkey === dmTarget.value.public_key) {
        messages.value.push(m)
        appended = true
      }
    } else if (m.kind === 'channel' && m.channel_idx === selectedCh.value) {
      messages.value.push(m)
      appended = true
    }
    if (messages.value.length > 300) messages.value.shift()
    if (appended && atBottom) scrollToBottom()
  }
})
onUnmounted(() => ws && ws.close())
</script>

<template>
  <div class="panes">
    <!-- Left column: Channels stacked above Contacts, freeing horizontal
         space for the messages pane. -->
    <div class="side-col">
      <div class="pane" style="flex: 1 1 0; min-height: 160px">
        <h3>Channels</h3>
        <div class="body">
          <table>
            <tbody>
              <tr
                v-for="c in channels"
                :key="c.channel_idx"
                class="clickable"
                :class="{ selected: selectedCh === c.channel_idx }"
                @click="selectCh(c.channel_idx)"
              >
                <td>{{ c.name }} <span class="muted">#{{ c.channel_idx }}</span></td>
              </tr>
              <tr
                class="clickable"
                :class="{ selected: selectedCh === 'dm' && !dmTarget }"
                @click="selectAllDMs"
              >
                <td>✉ Direct messages</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="pane" style="flex: 1 1 0; min-height: 200px">
        <h3>Contacts ({{ contacts.length }})</h3>
        <div class="toolbar">
          <input v-model="contactSearch" placeholder="search" style="flex: 1" />
        </div>
        <div class="body">
          <table>
            <tbody>
              <tr
                v-for="c in filteredContacts"
                :key="c.public_key"
                class="clickable"
                :class="{ selected: dmTarget && dmTarget.public_key === c.public_key }"
                @click="selectContact(c)"
                title="Open a DM conversation with this contact"
              >
                <td>
                  {{ c.adv_name || '(unnamed)' }}<br />
                  <span class="muted mono">{{ c.public_key.slice(0, 12) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="pane" style="flex: 1">
      <h3>
        {{ selectedCh === 'dm'
          ? (dmTarget ? `DM · ${dmTarget.adv_name || dmTarget.public_key.slice(0, 12)}` : 'Direct messages')
          : channels.find((c) => c.channel_idx === selectedCh)?.name || 'Messages' }}
      </h3>
      <div class="body" ref="msgBody">
        <div v-if="error" class="err" style="padding: 10px">{{ error }}</div>
        <div v-if="!messages.length" class="empty">no messages</div>
        <div
          v-for="m in messages"
          :key="m.id"
          class="msg"
          :class="{ out: m.is_outgoing }"
        >
          <div class="meta">
            <span class="who">{{ m.is_outgoing ? '→ me' : (m.sender_name || m.sender_pubkey?.slice(0, 12) || '?') }}</span>
            · {{ fmtTime(m.received_at) }}
            <span v-if="m.snr != null">· snr {{ m.snr }}</span>
          </div>
          <div>
            <template v-for="(seg, i) in messageSegments(m)" :key="i">
              <a
                v-if="seg.type === 'link'"
                :href="seg.value"
                target="_blank"
                rel="noopener noreferrer"
              >{{ seg.value }}</a>
              <span v-else-if="seg.type === 'mention'" class="mention">@[{{ seg.value }}]</span>
              <template v-else>{{ seg.value }}</template>
            </template>
          </div>
        </div>
      </div>
      <div class="toolbar">
        <input
          style="flex: 1"
          :placeholder="composePlaceholder"
          v-model="composeText"
          :disabled="!sendTarget || sending"
          @keyup.enter="send"
        />
        <button :disabled="!sendTarget || sending || !composeText.trim()" @click="send">
          {{ sending ? 'Sending…' : 'Send' }}
        </button>
      </div>
      <div v-if="sendError" class="err" style="padding: 0 12px 8px">{{ sendError }}</div>
    </div>
  </div>
</template>
