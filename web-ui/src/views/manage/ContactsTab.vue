<script setup>
import {
  ref, computed, watch, onMounted, onUnmounted,
  onActivated, onDeactivated, nextTick,
} from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'

const { data, error, notice, fetchTab, reload } = useManage()

onMounted(() => fetchTab('contacts'))
// the tab lives under KeepAlive, so the arrow-key listener must follow
// activation (visible tab), not mount — else it keeps firing on other tabs
onActivated(() => window.addEventListener('keydown', onContactKey))
onDeactivated(() => window.removeEventListener('keydown', onContactKey))
onUnmounted(() => window.removeEventListener('keydown', onContactKey))

// ---- contacts view (search + type-filter + click-sort) ----
const CONTACT_TYPE_LABEL = { 1: 'Comp', 2: 'Rptr', 3: 'Room', 4: 'Sens' }
function typeLabel(t) {
  if (t == null) return '?'
  return CONTACT_TYPE_LABEL[t] || String(t)
}
function shortKey(pk) {
  return pk && pk.length > 14 ? `${pk.slice(0, 6)}...${pk.slice(-6)}` : pk || ''
}
function fmtLocalDateTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const contactSearch = ref('')
const contactExcluded = ref(new Set()) // type labels turned off
const contactKnownTypes = ref(new Set()) // accumulates as rows arrive
const contactSort = ref({ col: 'adv_name', dir: 'asc' })
const selectedContacts = ref(new Set()) // public_keys marked for deletion

// ---- contact detail pane (click a row to inspect) ----
// Track by pubkey (not the row object) so the selection survives a contacts
// reload; detailContact resolves to null if the row is gone (e.g. deleted).
const detailPubkey = ref(null)
const detailContact = computed(
  () => (data.value.contacts || []).find((c) => c.public_key === detailPubkey.value) || null,
)
function selectContact(c) {
  detailPubkey.value = c.public_key
}
// Ordered [label, value, mono?] rows rendered in the detail pane.
const contactDetailFields = computed(() => {
  const c = detailContact.value
  if (!c) return []
  let outPath
  if (c.out_path_len === -1) outPath = 'flood (no path)'
  else if (c.out_path_len === 0) outPath = 'direct (0 hops)'
  else if (c.out_path) outPath = `${c.out_path} (${c.out_path_len} hop${c.out_path_len === 1 ? '' : 's'})`
  else outPath = `${c.out_path_len ?? '?'} hop(s)`
  const loc = c.adv_lat != null && c.adv_lon != null ? `${c.adv_lat}, ${c.adv_lon}` : '—'
  return [
    ['name', c.adv_name || '—', false],
    ['type', typeLabel(c.type), false],
    ['public key', c.public_key, true],
    ['flags', c.flags ?? '—', false],
    ['out path', outPath, true],
    ['path hash mode', c.out_path_hash_mode ?? '—', false],
    ['location (lat, lon)', loc, false],
    ['last advert', fmtLocalDateTime(c.last_advert) || '—', false],
    ['last modified', fmtLocalDateTime(c.lastmod) || '—', false],
    ['first seen', fmtLocalDateTime(c.first_seen_at) || '—', false],
    ['last synced', fmtLocalDateTime(c.last_synced_at) || '—', false],
  ]
})

const contactTypes = computed(() => [...contactKnownTypes.value].sort())
const contactAllChecked = computed(() => contactExcluded.value.size === 0)
const contactTypeChecked = (label) => !contactExcluded.value.has(label)
function toggleContactAll(on) {
  contactExcluded.value = on ? new Set() : new Set(contactKnownTypes.value)
}
function toggleContactType(label, on) {
  const next = new Set(contactExcluded.value)
  on ? next.delete(label) : next.add(label)
  contactExcluded.value = next
}
function rememberContactTypes(rows) {
  let next = null
  for (const r of rows) {
    const t = typeLabel(r.type)
    if (!contactKnownTypes.value.has(t)) {
      next = next || new Set(contactKnownTypes.value)
      next.add(t)
    }
  }
  if (next) contactKnownTypes.value = next
}
watch(
  () => data.value.contacts,
  (rows) => rows && rememberContactTypes(rows),
  { immediate: true },
)
function sortContacts(col) {
  if (contactSort.value.col === col) {
    contactSort.value = { col, dir: contactSort.value.dir === 'asc' ? 'desc' : 'asc' }
  } else {
    contactSort.value = { col, dir: 'asc' }
  }
}
function toggleContactSelected(pk) {
  const next = new Set(selectedContacts.value)
  next.has(pk) ? next.delete(pk) : next.add(pk)
  selectedContacts.value = next
}
const selectedContactRows = computed(() =>
  (data.value.contacts || []).filter((r) => selectedContacts.value.has(r.public_key)),
)
const canDeleteContacts = computed(() => selectedContactRows.value.length > 0)

async function deleteSelectedContacts() {
  const rows = selectedContactRows.value
  if (!rows.length) return
  const names = rows.map((r) => r.adv_name || r.public_key.slice(0, 12))
  // Cap preview at 20 names so the OS confirm() stays usable on large sets.
  const preview = names.slice(0, 20).join('\n')
  const more = names.length > 20 ? `\n…and ${names.length - 20} more` : ''
  if (!confirm(`Delete ${rows.length} contact(s)?\n\n${preview}${more}`)) return

  error.value = ''
  notice.value = ''
  let ok = 0, fail = 0
  let lastErr = ''
  for (const r of rows) {
    try {
      await api(`/contacts/${encodeURIComponent(r.public_key)}`, { method: 'DELETE' })
      ok++
    } catch (e) {
      fail++
      lastErr = `${e.message} (${r.adv_name || r.public_key.slice(0, 12)})`
    }
  }
  selectedContacts.value = new Set()
  await reload('contacts')
  notice.value = fail ? `Deleted ${ok}, ${fail} failed` : `Deleted ${ok} contact(s)`
  if (lastErr) error.value = lastErr
}

const visibleContacts = computed(() => {
  const rows = data.value.contacts || []
  const q = contactSearch.value.toLowerCase().trim()
  const excluded = contactExcluded.value
  const filtered = rows.filter((r) => {
    if (excluded.size && excluded.has(typeLabel(r.type))) return false
    if (!q) return true
    return (
      (r.public_key || '').toLowerCase().includes(q) ||
      (r.adv_name || '').toLowerCase().includes(q)
    )
  })
  const { col, dir } = contactSort.value
  const sign = dir === 'asc' ? 1 : -1
  filtered.sort((a, b) => {
    const av = a[col], bv = b[col]
    if (av == null && bv == null) return 0
    if (av == null) return 1 // nulls last regardless of direction
    if (bv == null) return -1
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
    return String(av).localeCompare(String(bv)) * sign
  })
  return filtered
})

// keyboard nav: Up/Down move the selected row through the visible list and
// populate the detail pane. Mirrors the Packets screen. The listener only
// lives while this tab is mounted.
const contactListEl = ref(null)
function scrollContactIntoView(pk) {
  contactListEl.value
    ?.querySelector(`[data-pk="${pk}"]`)
    ?.scrollIntoView({ block: 'nearest' })
}
function selectContactByOffset(delta) {
  const list = visibleContacts.value
  if (!list.length) return
  const cur = detailContact.value
    ? list.findIndex((c) => c.public_key === detailContact.value.public_key)
    : -1
  // nothing selected yet -> Down starts at the top, Up at the bottom
  const i = cur === -1
    ? (delta > 0 ? 0 : list.length - 1)
    : Math.max(0, Math.min(list.length - 1, cur + delta))
  const c = list[i]
  selectContact(c)
  nextTick(() => scrollContactIntoView(c.public_key))
}
function onContactKey(e) {
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
  selectContactByOffset(e.key === 'ArrowUp' ? -1 : 1)
}
</script>

<template>
  <!-- search + type-filter + click-sort, detail pane on the right -->
  <div class="contacts-split">
    <div class="pane" style="flex: 1.6; min-width: 0">
      <div class="toolbar">
        <input
          v-model="contactSearch"
          placeholder="search name or pubkey…"
          style="flex: 0 0 260px"
        />
        <label class="chk">
          <input
            type="checkbox"
            :checked="contactAllChecked"
            @change="toggleContactAll($event.target.checked)"
          />
          All
        </label>
        <label v-for="t in contactTypes" :key="t" class="chk">
          <input
            type="checkbox"
            :checked="contactTypeChecked(t)"
            @change="toggleContactType(t, $event.target.checked)"
          />
          {{ t }}
        </label>
        <button :disabled="!canDeleteContacts" @click="deleteSelectedContacts">
          Delete{{ canDeleteContacts ? ` (${selectedContactRows.length})` : '' }}
        </button>
        <span class="muted">{{ visibleContacts.length }} shown</span>
      </div>
      <div class="body" ref="contactListEl">
        <table>
          <thead>
            <tr>
              <th style="width: 24px"></th>
              <th class="sortable" @click="sortContacts('public_key')">
                pubkey<span v-if="contactSort.col === 'public_key'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
              <th class="sortable" @click="sortContacts('adv_name')">
                name<span v-if="contactSort.col === 'adv_name'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
              <th class="sortable" @click="sortContacts('type')">
                type<span v-if="contactSort.col === 'type'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
              <th class="sortable" @click="sortContacts('adv_lat')">
                lat<span v-if="contactSort.col === 'adv_lat'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
              <th class="sortable" @click="sortContacts('adv_lon')">
                lon<span v-if="contactSort.col === 'adv_lon'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
              <th class="sortable" @click="sortContacts('last_synced_at')">
                last synced<span v-if="contactSort.col === 'last_synced_at'">
                  {{ contactSort.dir === 'asc' ? '▲' : '▼' }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="c in visibleContacts"
              :key="c.public_key"
              :data-pk="c.public_key"
              class="clickable"
              :class="{ selected: detailContact && detailContact.public_key === c.public_key }"
              @click="selectContact(c)"
            >
              <td>
                <input
                  type="checkbox"
                  :checked="selectedContacts.has(c.public_key)"
                  @change="toggleContactSelected(c.public_key)"
                  @click.stop
                />
              </td>
              <td class="mono" :title="c.public_key">{{ shortKey(c.public_key) }}</td>
              <td>{{ c.adv_name || '' }}</td>
              <td>{{ typeLabel(c.type) }}</td>
              <td>{{ c.adv_lat ?? '' }}</td>
              <td>{{ c.adv_lon ?? '' }}</td>
              <td>{{ fmtLocalDateTime(c.last_synced_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="pane contact-detail">
      <h3>Contact</h3>
      <div class="body">
        <div v-if="!detailContact" class="empty">select a contact</div>
        <div v-else class="decoded">
          <div class="kv">
            <template v-for="f in contactDetailFields" :key="f[0]">
              <div class="k">{{ f[0] }}</div>
              <div :class="{ mono: f[2] }" style="word-break: break-all">{{ f[1] }}</div>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* List on the left, detail pane on the right (each scrolls independently),
   mirroring the Packets screen's inspector layout. */
.contacts-split {
  display: flex;
  gap: 10px;
  height: 100%;
  min-height: 0;
  padding: 10px;
  box-sizing: border-box;
}
.contact-detail {
  flex: 1;
  min-width: 240px;
  max-width: 440px;
}
</style>
