<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const tabs = [
  { key: 'stats', label: 'Overview' },
  { key: 'radio', label: 'Radio' },
  { key: 'contacts', label: 'Contacts' },
  { key: 'channels', label: 'Channels' },
  { key: 'users', label: 'Users' },
  { key: 'groups', label: 'Groups' },
  { key: 'command-config', label: 'Commands' },
  { key: 'audit', label: 'Audit log' },
]
const active = ref('stats')
const data = ref({})
const groupNames = ref([])
const commandNames = ref([])
const channelNames = ref([])
const error = ref('')
const notice = ref('')

// add-form state
const newChannel = ref({ name: '', key: '' })
const newGroup = ref({ name: '', commands: '' })
const newUser = ref({ sel: '', group: '' }) // create-bot-user form (Contacts tab)

const endpoints = {
  stats: '/stats',
  radio: '/device-info',
  contacts: '/contacts?limit=500',
  channels: '/channels',
  users: '/users',
  groups: '/groups',
  'command-config': '/command-config',
  audit: '/audit?limit=200',
}

function decorate(key, payload) {
  const rows = payload.items || payload
  if (key === 'command-config' && Array.isArray(rows)) {
    for (const r of rows) {
      let chans = []
      try {
        chans = r.allowed_channels ? JSON.parse(r.allowed_channels) : []
      } catch {
        chans = []
      }
      r._chans = chans // chip list — array of channel names
    }
  }
  return rows
}

async function loadTab(key, force = false) {
  active.value = key
  error.value = ''
  notice.value = ''
  if (data.value[key] && !force) return
  try {
    const r = await api(endpoints[key])
    data.value[key] = decorate(key, r)
    if (key === 'contacts') rememberContactTypes(data.value[key])
    // users/groups tabs (and the Contacts "create bot user" form) need the
    // list of group names for their group pickers.
    if (
      (key === 'users' || key === 'groups' || key === 'contacts') &&
      !groupNames.value.length
    ) {
      const g = await api('/groups')
      groupNames.value = g.items.map((x) => x.name)
    }
    // Groups tab's grant picker needs the universe of command names.
    if (key === 'groups' && !commandNames.value.length) {
      const cc = await api('/command-config')
      commandNames.value = cc.items.map((x) => x.command).sort()
    }
    // Commands tab's allowed-channels picker needs the channel names.
    if (key === 'command-config' && !channelNames.value.length) {
      const ch = await api('/channels')
      channelNames.value = ch.items.map((x) => x.name)
    }
  } catch (e) {
    error.value = e.message
  }
}
const reload = (key) => loadTab(key, true)

async function run(promise, okMsg, reloadKeys = []) {
  error.value = ''
  notice.value = ''
  try {
    await promise
    if (okMsg) notice.value = okMsg
  } catch (e) {
    error.value = e.message
  }
  for (const k of reloadKeys) await reload(k)
}

// ---- command config ----
// Sync the local reactive row from the authoritative PATCH response. The
// inputs are one-way bound (:value / :checked), so when run()/notice
// triggered a re-render the input would snap back to the *stale* local
// value — that was the "cooldown reverts on edit" bug. Updating the row
// from the server's returned values keeps the input showing what was saved.
function applyCommandRow(updated) {
  const rows = data.value['command-config']
  if (!rows) return
  const i = rows.findIndex((r) => r.command === updated.command)
  if (i === -1) return
  let chans = []
  try {
    chans = updated.allowed_channels ? JSON.parse(updated.allowed_channels) : []
  } catch {
    chans = []
  }
  rows[i] = { ...rows[i], ...updated, _chans: chans }
}
async function patchCommand(cmd, fields) {
  error.value = ''
  notice.value = ''
  try {
    const updated = await api(`/command-config/${encodeURIComponent(cmd)}`, {
      method: 'PATCH',
      json: fields,
    })
    applyCommandRow(updated)
    notice.value = `${cmd}: saved`
  } catch (e) {
    error.value = e.message
    await reload('command-config') // resync to server truth on failure
  }
}
function addCommandChannel(row, channel) {
  if (!channel || row._chans.includes(channel)) return
  patchCommand(row.command, { allowed_channels: [...row._chans, channel] })
}
function removeCommandChannel(row, channel) {
  patchCommand(row.command, { allowed_channels: row._chans.filter((c) => c !== channel) })
}

// ---- channels ----
async function addChannel() {
  const body = { name: newChannel.value.name.trim() }
  if (newChannel.value.key.trim()) body.key = newChannel.value.key.trim()
  if (!body.name) return
  await run(api('/channels', { method: 'POST', json: body }), `added ${body.name}`, ['channels'])
  if (!error.value) newChannel.value = { name: '', key: '' }
}
function removeChannel(name) {
  if (!confirm(`Remove channel ${name}? This clears its radio slot.`)) return
  run(api(`/channels/${encodeURIComponent(name)}`, { method: 'DELETE' }), `removed ${name}`, ['channels'])
}

// ---- users ----
function addUserGroup(pubkey, group) {
  if (!group) return
  run(api(`/users/${pubkey}/groups`, { method: 'POST', json: { group } }), 'group added', ['users'])
}
function removeUserGroup(pubkey, group) {
  run(api(`/users/${pubkey}/groups/${encodeURIComponent(group)}`, { method: 'DELETE' }), 'group removed', ['users'])
}
function renameUser(pubkey, current) {
  const name = prompt('New alias:', current || '')
  if (name === null) return
  run(api(`/users/${pubkey}`, { method: 'PATCH', json: { name } }), 'renamed', ['users'])
}
function deleteUser(pubkey, name) {
  if (!confirm(`Delete user ${name || pubkey.slice(0, 12)} from the bot?`)) return
  run(api(`/users/${pubkey}`, { method: 'DELETE' }), 'deleted', ['users'])
}
function toggleBlock(u) {
  const blocked = (u.groups || []).includes('blocked')
  const p = blocked
    ? api(`/users/${u.pubkey}/block`, { method: 'DELETE' })
    : api(`/users/${u.pubkey}/block`, { method: 'POST' })
  run(p, blocked ? 'unblocked' : 'blocked', ['users'])
}

// ---- groups ----
async function addGroup() {
  const name = newGroup.value.name.trim()
  if (!name) return
  const commands = newGroup.value.commands.split(',').map((s) => s.trim()).filter(Boolean)
  await run(api('/groups', { method: 'POST', json: { name, commands } }), `group ${name} created`, ['groups'])
  if (!error.value) newGroup.value = { name: '', commands: '' }
}
function deleteGroup(name) {
  if (!confirm(`Delete group ${name}?`)) return
  run(api(`/groups/${encodeURIComponent(name)}`, { method: 'DELETE' }), 'deleted', ['groups'])
}
function grantCommand(name, command) {
  if (!command) return
  run(
    api(`/groups/${encodeURIComponent(name)}/commands`, { method: 'POST', json: { command } }),
    `granted ${command} to ${name}`,
    ['groups'],
  )
}
function revokeCommand(name, command) {
  if (!command) return
  run(
    api(`/groups/${encodeURIComponent(name)}/commands/${encodeURIComponent(command)}`, { method: 'DELETE' }),
    `revoked ${command} from ${name}`,
    ['groups'],
  )
}
function setAllUsers(name, on) {
  // Toggle the group's "*" (all-users) membership.
  run(
    api(`/groups/${encodeURIComponent(name)}/all-users`, { method: on ? 'POST' : 'DELETE' }),
    on ? `${name}: now includes all users` : `${name}: explicit members only`,
    ['groups'],
  )
}

// ---- Radio tab: advert send + device-info grouping ----
const advertMode = ref('zero') // 'zero' or 'flood'
const advertSending = ref(false)
async function sendAdvert() {
  if (advertSending.value) return
  advertSending.value = true
  await run(
    api('/radio/advert', { method: 'POST', json: { flood: advertMode.value === 'flood' } }),
    `${advertMode.value === 'flood' ? 'flood' : 'zero-hop'} advert sent`,
  )
  advertSending.value = false
}

// Display label + (optional) unit for each known device_info key. Anything
// not listed falls into an "Other" group with the raw key.
const DEVINFO_GROUPS = [
  {
    title: 'Identity',
    keys: [
      ['self_info.name', 'Name'],
      ['self_info.public_key', 'Public key', { mono: true }],
      ['device_info.model', 'Model'],
      ['device_info.ver', 'Firmware version'],
      ['device_info.fw ver', 'Firmware code'],
      ['device_info.fw_build', 'Firmware build'],
    ],
  },
  {
    title: 'Radio',
    keys: [
      ['self_info.radio_freq', 'Frequency', { unit: 'MHz' }],
      ['self_info.radio_bw', 'Bandwidth', { unit: 'kHz' }],
      ['self_info.radio_sf', 'Spreading factor'],
      ['self_info.radio_cr', 'Coding rate'],
      ['self_info.tx_power', 'TX power', { unit: 'dBm' }],
      ['self_info.max_tx_power', 'Max TX power', { unit: 'dBm' }],
      ['device_info.path_hash_mode', 'Out path-hash mode', { fmt: (v) => `${Number(v) + 1} byte(s)/hop` }],
    ],
  },
  {
    title: 'Advertising',
    keys: [
      ['self_info.adv_type', 'Advertised type'],
      ['self_info.adv_lat', 'Latitude'],
      ['self_info.adv_lon', 'Longitude'],
      ['self_info.adv_loc_policy', 'Location-share policy'],
    ],
  },
  {
    title: 'Capacity',
    keys: [
      ['device_info.max_channels', 'Max channels'],
      ['device_info.max_contacts', 'Max contacts'],
    ],
  },
  {
    title: 'Battery / memory',
    keys: [
      ['battery.level', 'Battery', { unit: 'mV' }],
      ['battery.total_kb', 'Total memory', { unit: 'kB' }],
      ['battery.used_kb', 'Used memory', { unit: 'kB' }],
    ],
  },
  {
    title: 'Other',
    keys: [
      ['device_info.ble_pin', 'BLE PIN'],
      ['device_info.repeat', 'Repeater mode'],
      ['self_info.manual_add_contacts', 'Manual contact add'],
      ['self_info.multi_acks', 'Multi-acks'],
      ['self_info.telemetry_mode_base', 'Telemetry: base'],
      ['self_info.telemetry_mode_env', 'Telemetry: env'],
      ['self_info.telemetry_mode_loc', 'Telemetry: location'],
    ],
  },
]
const DEVINFO_KNOWN = new Set(DEVINFO_GROUPS.flatMap((g) => g.keys.map((k) => k[0])))

function parseDevValue(raw) {
  if (raw == null) return null
  try { return JSON.parse(raw) } catch { return raw }
}
function devValueText(rec, opts) {
  const v = parseDevValue(rec.value)
  if (v == null || v === '') return ''
  if (opts?.fmt) return opts.fmt(v)
  let s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  if (opts?.unit) s = `${s} ${opts.unit}`
  return s
}
const deviceInfoMap = computed(() => {
  const m = new Map()
  for (const rec of data.value.radio || []) m.set(rec.key, rec)
  return m
})
// Records whose keys aren't in any group go into "Unrecognized" so we
// don't silently drop fields the firmware adds later.
const unknownDeviceKeys = computed(() =>
  [...(data.value.radio || [])]
    .filter((r) => !DEVINFO_KNOWN.has(r.key))
    .sort((a, b) => a.key.localeCompare(b.key)),
)

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

// ---- create a bot user from a contact (Contacts tab toolbar) ----
// Groups that can be an initial membership: 'public' is an all-users flag
// (not a membership) and 'blocked' is set via block(), so both are excluded.
const assignableGroups = computed(() =>
  groupNames.value.filter((g) => g !== 'public' && g !== 'blocked'),
)
// One searchable <datalist> option per contact: "<name> — <full pubkey>".
// The full pubkey makes each label unique (even for duplicate names) and
// lets us resolve the chosen text straight back to a pubkey.
function contactOptionLabel(c) {
  return `${c.adv_name || '(unnamed)'} — ${c.public_key}`
}
function selectedNewUserPubkey() {
  const sel = newUser.value.sel.trim()
  if (!sel) return ''
  const c = (data.value.contacts || []).find((x) => contactOptionLabel(x) === sel)
  if (c) return c.public_key
  // also accept a raw 64-hex pubkey pasted directly
  return /^[0-9a-fA-F]{64}$/.test(sel) ? sel.toLowerCase() : ''
}
async function createUser() {
  const pubkey = selectedNewUserPubkey()
  if (!pubkey) {
    alert('Pick a contact from the list (or paste a 64-hex pubkey).')
    return
  }
  if (!newUser.value.group) {
    alert('Choose an initial group.')
    return
  }
  error.value = ''
  notice.value = ''
  try {
    await api('/users', {
      method: 'POST',
      json: { pubkey, group: newUser.value.group },
    })
    notice.value = `bot user created and added to ${newUser.value.group}`
    newUser.value = { sel: '', group: '' }
    delete data.value.users // force the Users tab to refetch with the new row
  } catch (e) {
    // e.g. 409 when a bot user already exists for this contact
    alert(e.message)
  }
}

const contactSearch = ref('')
const contactExcluded = ref(new Set()) // type labels turned off
const contactKnownTypes = ref(new Set()) // accumulates as rows arrive
const contactSort = ref({ col: 'adv_name', dir: 'asc' })
const selectedContacts = ref(new Set()) // public_keys marked for deletion

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

function cols(rows) {
  return rows && rows.length ? Object.keys(rows[0]).filter((c) => !c.startsWith('_')) : []
}
function cell(v) {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

onMounted(() => loadTab('stats'))
</script>

<template>
  <div class="panes">
    <div class="pane" style="flex: 0 0 170px">
      <h3>Manage</h3>
      <div class="body">
        <table>
          <tbody>
            <tr
              v-for="t in tabs"
              :key="t.key"
              class="clickable"
              :class="{ selected: active === t.key }"
              @click="loadTab(t.key)"
            >
              <td>{{ t.label }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="pane" style="flex: 1">
      <h3>{{ tabs.find((t) => t.key === active)?.label }}</h3>

      <div v-if="error || notice" class="toolbar">
        <span v-if="error" class="err">{{ error }}</span>
        <span v-if="notice" class="muted">{{ notice }}</span>
      </div>

      <div class="body">
        <!-- Overview -->
        <div v-if="active === 'stats' && data.stats" class="decoded">
          <div class="kv">
            <div class="k">identity</div>
            <div class="mono">{{ data.stats.identity?.pubkey || '—' }}</div>
            <div class="k">events seen</div>
            <div>{{ data.stats.event_count }}</div>
            <div class="k">commands loaded</div>
            <div>{{ data.stats.commands_loaded }}</div>
          </div>
          <h4>Counts</h4>
          <div class="kv">
            <template v-for="(v, k) in data.stats.counts" :key="k">
              <div class="k">{{ k }}</div>
              <div>{{ v }}</div>
            </template>
          </div>
        </div>

        <!-- Radio: advert button + device-info -->
        <div v-else-if="active === 'radio'">
          <div class="toolbar">
            <label class="chk">
              <input type="radio" value="zero" v-model="advertMode" />
              Zero hop
            </label>
            <label class="chk">
              <input type="radio" value="flood" v-model="advertMode" />
              Flood
            </label>
            <button :disabled="advertSending" @click="sendAdvert">
              {{ advertSending ? 'Sending…' : 'Advert' }}
            </button>
            <span class="muted">
              Zero-hop reaches direct neighbors only; flood propagates through the mesh.
            </span>
          </div>
          <div class="decoded">
            <template v-for="g in DEVINFO_GROUPS" :key="g.title">
              <h4 style="margin: 12px 0 4px">{{ g.title }}</h4>
              <div class="kv">
                <template v-for="entry in g.keys" :key="entry[0]">
                  <div class="k">{{ entry[1] }}</div>
                  <div :class="{ mono: entry[2]?.mono }">
                    {{ devValueText(deviceInfoMap.get(entry[0]) || {}, entry[2]) || '—' }}
                  </div>
                </template>
              </div>
            </template>
            <template v-if="unknownDeviceKeys.length">
              <h4 style="margin: 12px 0 4px">Unrecognized</h4>
              <div class="kv">
                <template v-for="r in unknownDeviceKeys" :key="r.key">
                  <div class="k mono">{{ r.key }}</div>
                  <div>{{ devValueText(r) || '—' }}</div>
                </template>
              </div>
            </template>
          </div>
        </div>

        <!-- Channels (editable) -->
        <div v-else-if="active === 'channels'">
          <div class="toolbar">
            <input v-model="newChannel.name" placeholder="name (e.g. #bot or MyChan)" style="flex: 1" />
            <input v-model="newChannel.key" placeholder="hex key (non-# only)" style="flex: 1" />
            <button @click="addChannel">Add channel</button>
          </div>
          <table>
            <thead><tr><th>idx</th><th>name</th><th>secret</th><th></th></tr></thead>
            <tbody>
              <tr v-for="c in data.channels" :key="c.channel_idx">
                <td>{{ c.channel_idx }}</td>
                <td>{{ c.name }}</td>
                <td class="mono muted">{{ (c.secret_hex || '').slice(0, 12) }}…</td>
                <td><button @click="removeChannel(c.name)">remove</button></td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Users (editable) -->
        <div v-else-if="active === 'users'">
          <table>
            <thead>
              <tr>
                <th>name</th>
                <th>pubkey</th>
                <th style="width: 130px">add group</th>
                <th>groups</th>
                <th>actions</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in data.users" :key="u.pubkey">
                <td>{{ u.name || '?' }}</td>
                <td class="mono muted">{{ u.pubkey.slice(0, 12) }}</td>
                <td>
                  <select @change="addUserGroup(u.pubkey, $event.target.value); $event.target.value = ''">
                    <option value="">+ group…</option>
                    <option v-for="g in groupNames" :key="g" :value="g">{{ g }}</option>
                  </select>
                </td>
                <td class="chips">
                  <span v-for="g in u.groups" :key="g" class="tag">
                    {{ g }}
                    <a href="#" @click.prevent="removeUserGroup(u.pubkey, g)" title="remove">×</a>
                  </span>
                </td>
                <td style="white-space: normal">
                  <button @click="renameUser(u.pubkey, u.name)">rename</button>
                  <button @click="toggleBlock(u)">
                    {{ (u.groups || []).includes('blocked') ? 'unblock' : 'block' }}
                  </button>
                  <button @click="deleteUser(u.pubkey, u.name)">delete</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Groups (editable) -->
        <div v-else-if="active === 'groups'">
          <div class="toolbar">
            <input v-model="newGroup.name" placeholder="new group name" />
            <input v-model="newGroup.commands" placeholder="commands (comma list, optional)" style="flex: 1" />
            <button @click="addGroup">Create group</button>
          </div>
          <table>
            <thead>
              <tr>
                <th>name</th>
                <th>users</th>
                <th title="Every user is a member (the * membership)">all users</th>
                <th style="width: 150px">grant command</th>
                <th>commands</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in data.groups" :key="g.name">
                <td>{{ g.name }}<span v-if="g.is_system" class="muted"> [sys]</span></td>
                <td>{{ g.nusers }}<span v-if="g.all_users" class="muted"> +*</span></td>
                <td>
                  <input
                    type="checkbox"
                    :checked="g.all_users"
                    :disabled="g.name === 'blocked'"
                    title="Include all users (*) in this group"
                    @change="setAllUsers(g.name, $event.target.checked)"
                  />
                </td>
                <td>
                  <select @change="grantCommand(g.name, $event.target.value); $event.target.value = ''">
                    <option value="">+ command…</option>
                    <option value="*">* (all commands)</option>
                    <option v-for="c in commandNames" :key="c" :value="c">{{ c }}</option>
                  </select>
                </td>
                <td class="chips">
                  <span v-for="c in g.commands" :key="c" class="tag">
                    {{ c }}
                    <a href="#" @click.prevent="revokeCommand(g.name, c)" title="revoke">×</a>
                  </span>
                </td>
                <td><button :disabled="g.is_system" @click="deleteGroup(g.name)">delete</button></td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Command config (editable) -->
        <div v-else-if="active === 'command-config'">
          <table>
            <thead>
              <tr>
                <th>command</th><th>enabled</th><th>allow_dm</th>
                <th>dm_only</th><th>cooldown</th>
                <th style="width: 140px">allow channel</th>
                <th>allowed channels</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in data['command-config']" :key="r.command">
                <td>{{ r.command }}</td>
                <td><input type="checkbox" :checked="!!r.enabled" @change="patchCommand(r.command, { enabled: $event.target.checked })" /></td>
                <td><input type="checkbox" :checked="!!r.allow_dm" @change="patchCommand(r.command, { allow_dm: $event.target.checked })" /></td>
                <td><input type="checkbox" :checked="!!r.dm_only" @change="patchCommand(r.command, { dm_only: $event.target.checked })" /></td>
                <td><input type="number" :value="r.cooldown_seconds" style="width: 64px" @change="patchCommand(r.command, { cooldown_seconds: Number($event.target.value) })" /></td>
                <td>
                  <select @change="addCommandChannel(r, $event.target.value); $event.target.value = ''">
                    <option value="">+ channel…</option>
                    <option v-for="c in channelNames" :key="c" :value="c">{{ c }}</option>
                  </select>
                </td>
                <td class="chips">
                  <span v-if="!r._chans.length" class="muted">(any)</span>
                  <span v-for="c in r._chans" :key="c" class="tag">
                    {{ c }}
                    <a href="#" @click.prevent="removeCommandChannel(r, c)" title="remove">×</a>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Contacts (search + type-filter + click-sort) -->
        <div v-else-if="active === 'contacts'">
          <!-- Create a bot user from a contact + an initial group. -->
          <div class="toolbar">
            <input
              v-model="newUser.sel"
              list="newUserContacts"
              placeholder="add bot user: search a contact…"
              style="flex: 0 0 320px"
            />
            <datalist id="newUserContacts">
              <option
                v-for="c in data.contacts || []"
                :key="c.public_key"
                :value="contactOptionLabel(c)"
              />
            </datalist>
            <select v-model="newUser.group">
              <option value="">initial group…</option>
              <option v-for="g in assignableGroups" :key="g" :value="g">{{ g }}</option>
            </select>
            <button
              :disabled="!selectedNewUserPubkey() || !newUser.group"
              @click="createUser"
            >
              Add bot user
            </button>
          </div>

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
              <tr v-for="c in visibleContacts" :key="c.public_key">
                <td>
                  <input
                    type="checkbox"
                    :checked="selectedContacts.has(c.public_key)"
                    @change="toggleContactSelected(c.public_key)"
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

        <!-- Read-only generic table (audit) -->
        <table v-else-if="Array.isArray(data[active])">
          <thead>
            <tr><th v-for="c in cols(data[active])" :key="c">{{ c }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in data[active]" :key="i">
              <td v-for="c in cols(data[active])" :key="c" :title="cell(row[c])">
                {{ cell(row[c]) }}
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">loading…</div>
      </div>
    </div>
  </div>
</template>
