<script setup>
import { ref, provide } from 'vue'
import { api } from '../api.js'
import { ManageKey } from './manage/context.js'
import StatsTab from './manage/StatsTab.vue'
import RadioTab from './manage/RadioTab.vue'
import ContactsTab from './manage/ContactsTab.vue'
import ChannelsTab from './manage/ChannelsTab.vue'
import UsersTab from './manage/UsersTab.vue'
import GroupsTab from './manage/GroupsTab.vue'
import CommandsTab from './manage/CommandsTab.vue'
import AuditTab from './manage/AuditTab.vue'
import DatabaseTab from './manage/DatabaseTab.vue'

const tabs = [
  { key: 'stats', label: 'Overview' },
  { key: 'radio', label: 'Radio' },
  { key: 'contacts', label: 'Contacts' },
  { key: 'channels', label: 'Channels' },
  { key: 'users', label: 'Users' },
  { key: 'groups', label: 'Groups' },
  { key: 'command-config', label: 'Commands' },
  { key: 'audit', label: 'Audit log' },
  { key: 'database', label: 'Database' },
]
const TAB_COMPONENTS = {
  stats: StatsTab,
  radio: RadioTab,
  contacts: ContactsTab,
  channels: ChannelsTab,
  users: UsersTab,
  groups: GroupsTab,
  'command-config': CommandsTab,
  audit: AuditTab,
  database: DatabaseTab,
}
const active = ref('stats')
const data = ref({})
const groupNames = ref([])
const commandNames = ref([])
const channelNames = ref([])
const error = ref('')
const notice = ref('')

const endpoints = {
  stats: '/stats',
  radio: '/device-info',
  contacts: '/contacts?limit=500',
  channels: '/channels',
  users: '/users',
  groups: '/groups',
  'command-config': '/command-config',
  audit: '/audit?limit=200',
  database: '/db/saved-queries', // saved-query picker
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

// Tab components fetch their own data (onMounted) through this shared,
// per-key cache; force reloads after a mutation via reload(key).
async function fetchTab(key, force = false) {
  if (data.value[key] && !force) return
  try {
    const r = await api(endpoints[key])
    data.value[key] = decorate(key, r)
  } catch (e) {
    error.value = e.message
  }
}
const reload = (key) => fetchTab(key, true)

function switchTab(key) {
  active.value = key
  error.value = ''
  notice.value = ''
}

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

// shared name caches for the tabs' pickers (loaded once on demand)
async function ensureGroupNames() {
  if (groupNames.value.length) return
  try {
    groupNames.value = (await api('/groups')).items.map((x) => x.name)
  } catch (e) {
    error.value = e.message
  }
}
async function ensureCommandNames() {
  if (commandNames.value.length) return
  try {
    commandNames.value = (await api('/command-config')).items.map((x) => x.command).sort()
  } catch (e) {
    error.value = e.message
  }
}
async function ensureChannelNames() {
  if (channelNames.value.length) return
  try {
    channelNames.value = (await api('/channels')).items.map((x) => x.name)
  } catch (e) {
    error.value = e.message
  }
}

// ---- runtime settings (registry-driven; Radio + Commands tabs render
//      their group's rows via SettingRows.vue) ----
const settings = ref({})
async function loadSettings() {
  try {
    const m = {}
    for (const s of await api('/settings')) m[s.key] = s
    settings.value = m
  } catch (e) {
    error.value = e.message
  }
}
function settingsFor(group) {
  return Object.values(settings.value).filter((s) => s.group === group)
}
async function applySetting(key) {
  const s = settings.value[key]
  const n = Number(s.value)
  await run(
    api(`/settings/${key}`, { method: 'PUT', json: { value: n } }),
    n > 0 ? `${s.label}: ${n}${s.unit ? ' ' + s.unit : ''}` : `${s.label} disabled`,
  )
  await loadSettings()
}

provide(ManageKey, {
  data, error, notice,
  fetchTab, reload, run,
  groupNames, commandNames, channelNames,
  ensureGroupNames, ensureCommandNames, ensureChannelNames,
  settings, settingsFor, applySetting, loadSettings,
})
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
              @click="switchTab(t.key)"
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
        <!-- KeepAlive preserves per-tab UI state (search text, drafts,
             selections) across tab switches, like the old single-file view -->
        <KeepAlive>
          <component :is="TAB_COMPONENTS[active]" />
        </KeepAlive>
      </div>
    </div>
  </div>
</template>
