<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { api, wsUrl } from '../api.js'
import { fmtTime } from '../time.js'
import HexDump from '../components/HexDump.vue'
import FieldBreakout from '../components/FieldBreakout.vue'
import PacketDetail from '../components/PacketDetail.vue'

const packets = ref([])
const listEl = ref(null) // scroll container for the packet list
// Type-filter model: `excluded` holds the packet types the user has turned
// off. Empty set = "All" (everything shown). `knownTypes` accumulates the
// set of types we've seen so checkboxes don't flicker as old packets roll
// off the list.
const excluded = ref(new Set())
const knownTypes = ref(new Set())
const selected = ref(null) // full packet row
const decoded = ref(null) // decode response (raw packets)
const detail = ref(null) // full row + payload/attributes (non-raw packets)
const activeIndex = ref(-1) // hovered field index
const error = ref('')
let ws = null

const types = computed(() => [...knownTypes.value].sort())
const allChecked = computed(() => excluded.value.size === 0)
const isChecked = (t) => !excluded.value.has(t)
const filtered = computed(() =>
  excluded.value.size === 0
    ? packets.value
    : packets.value.filter((p) => !excluded.value.has(p.packet_type)),
)

function rememberTypes(rows) {
  let next = null
  for (const p of rows) {
    if (p.packet_type && !knownTypes.value.has(p.packet_type)) {
      next = next || new Set(knownTypes.value)
      next.add(p.packet_type)
    }
  }
  if (next) knownTypes.value = next
}

function toggleAll(on) {
  // "All" off => hide everything (excluded = every known type); on => show all.
  excluded.value = on ? new Set() : new Set(knownTypes.value)
}
function toggleType(t, on) {
  // Unchecking a single type while "All" is on naturally drops out of all-mode
  // (since `excluded` becomes non-empty), without any special-case logic.
  const next = new Set(excluded.value)
  on ? next.delete(t) : next.add(t)
  excluded.value = next
}

// active byte range derived from the hovered field
const activeRange = computed(() => {
  if (!decoded.value || activeIndex.value < 0) return null
  const f = decoded.value.fields[activeIndex.value]
  return f ? { offset: f.offset, length: f.length } : null
})

async function load() {
  try {
    const r = await api('/packets?limit=200')
    // API returns newest-first; show oldest at top, newest at the bottom
    packets.value = r.items.slice().reverse()
    rememberTypes(r.items)
    await nextTick()
    scrollToBottom()
  } catch (e) {
    error.value = e.message
  }
}

function atBottom() {
  const el = listEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40
}
function scrollToBottom() {
  const el = listEl.value
  if (el) el.scrollTop = el.scrollHeight
}
function scrollRowIntoView(id) {
  listEl.value
    ?.querySelector(`[data-pid="${id}"]`)
    ?.scrollIntoView({ block: 'nearest' })
}

// keyboard: Up = previous (older, above) row, Down = next (newer, below) row
function selectByOffset(delta) {
  const list = filtered.value
  if (!list.length) return
  let i = selected.value ? list.findIndex((p) => p.id === selected.value.id) : -1
  if (i === -1) i = list.length - 1 // nothing selected -> start at newest (bottom)
  else i = Math.max(0, Math.min(list.length - 1, i + delta))
  const p = list[i]
  select(p)
  nextTick(() => scrollRowIntoView(p.id))
}
function onKey(e) {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
  const t = e.target
  if (
    t &&
    (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
      t.tagName === 'SELECT' || t.isContentEditable)
  )
    return
  if (e.metaKey || e.ctrlKey || e.altKey) return
  e.preventDefault()
  selectByOffset(e.key === 'ArrowUp' ? -1 : 1)
}

async function select(p) {
  selected.value = p
  decoded.value = null
  detail.value = null
  activeIndex.value = -1
  try {
    if (p.has_raw) {
      decoded.value = await api('/packets/decode', {
        method: 'POST',
        json: { packet_id: p.id },
      })
    } else {
      // non-raw packet: pull the full row (payload_json/attributes_json are
      // omitted from the list view) so the inspector can show its details.
      detail.value = await api(`/packets/${p.id}`)
    }
  } catch (e) {
    error.value = e.message
  }
}

// hovering a hex byte -> find the field containing that offset
function onHoverOffset(off) {
  if (off === null || !decoded.value) {
    activeIndex.value = -1
    return
  }
  const idx = decoded.value.fields.findIndex(
    (f) => off >= f.offset && off < f.offset + f.length,
  )
  activeIndex.value = idx
}


onMounted(() => {
  load()
  ws = new WebSocket(wsUrl('/ws/packets'))
  ws.onmessage = (ev) => {
    const p = JSON.parse(ev.data)
    const stick = atBottom() // only auto-tail if the user is already at the bottom
    packets.value.push(p) // newest at the bottom
    if (packets.value.length > 500) packets.value.shift() // drop oldest (top)
    rememberTypes([p])
    if (stick) nextTick(scrollToBottom)
  }
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  if (ws) ws.close()
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div class="panes">
    <div class="pane" style="flex: 1.3">
      <h3>Packets (live)</h3>
      <div class="toolbar">
        <label class="chk">
          <input
            type="checkbox"
            :checked="allChecked"
            @change="toggleAll($event.target.checked)"
          />
          All
        </label>
        <label v-for="t in types" :key="t" class="chk">
          <input
            type="checkbox"
            :checked="isChecked(t)"
            @change="toggleType(t, $event.target.checked)"
          />
          {{ t }}
        </label>
        <span class="muted">{{ filtered.length }} shown</span>
        <span v-if="error" class="err">{{ error }}</span>
      </div>
      <div class="body" ref="listEl">
        <table>
          <thead>
            <tr><th>time</th><th>type</th><th>from</th><th>ch</th><th>text</th><th>raw</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="p in filtered"
              :key="p.id"
              :data-pid="p.id"
              class="clickable"
              :class="{ selected: selected && selected.id === p.id }"
              @click="select(p)"
            >
              <td>{{ fmtTime(p.received_at) }}</td>
              <td>{{ p.packet_type }}</td>
              <td>{{ p.sender_name || p.sender_pubkey_prefix || '' }}</td>
              <td>{{ p.channel_idx ?? '' }}</td>
              <td>{{ p.text || '' }}</td>
              <td>{{ p.has_raw ? '●' : '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="pane" style="flex: 1">
      <h3>Inspector</h3>
      <div class="body">
        <div v-if="!selected" class="empty">select a packet</div>
        <template v-else-if="!selected.has_raw">
          <PacketDetail v-if="detail" :packet="detail" />
          <div v-else class="empty">loading…</div>
        </template>
        <template v-else-if="decoded">
          <HexDump
            :hex="decoded.raw_hex"
            :active-range="activeRange"
            @hover-offset="onHoverOffset"
          />
          <div v-if="decoded.error" class="err" style="padding: 0 12px">
            {{ decoded.error }}
          </div>
          <FieldBreakout
            :fields="decoded.fields"
            :decoded="decoded.decoded"
            :active-index="activeIndex"
            @hover-field="(i) => (activeIndex = i)"
          />
        </template>
        <div v-else class="empty">decoding…</div>
      </div>
    </div>
  </div>
</template>
