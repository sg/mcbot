<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, wsUrl } from '../api.js'
import HexDump from '../components/HexDump.vue'
import FieldBreakout from '../components/FieldBreakout.vue'

const packets = ref([])
// Type-filter model: `excluded` holds the packet types the user has turned
// off. Empty set = "All" (everything shown). `knownTypes` accumulates the
// set of types we've seen so checkboxes don't flicker as old packets roll
// off the list.
const excluded = ref(new Set())
const knownTypes = ref(new Set())
const selected = ref(null) // full packet row
const decoded = ref(null) // decode response
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
    packets.value = r.items
    rememberTypes(r.items)
  } catch (e) {
    error.value = e.message
  }
}

async function select(p) {
  selected.value = p
  decoded.value = null
  activeIndex.value = -1
  if (!p.has_raw) return
  try {
    decoded.value = await api('/packets/decode', {
      method: 'POST',
      json: { packet_id: p.id },
    })
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

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : ''
}

onMounted(() => {
  load()
  ws = new WebSocket(wsUrl('/ws/packets'))
  ws.onmessage = (ev) => {
    const p = JSON.parse(ev.data)
    packets.value.unshift(p)
    if (packets.value.length > 500) packets.value.pop()
    rememberTypes([p])
  }
})
onUnmounted(() => ws && ws.close())
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
      <div class="body">
        <table>
          <thead>
            <tr><th>time</th><th>type</th><th>from</th><th>ch</th><th>text</th><th>raw</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="p in filtered"
              :key="p.id"
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
          <div class="empty">
            no raw bytes for this packet<br />
            <span class="muted">(only RX_LOG_DATA packets carry on-air bytes)</span>
          </div>
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
