<script setup>
import { onMounted } from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'
import SettingRows from './SettingRows.vue'

const {
  data, error, notice, fetchTab, reload,
  channelNames, ensureChannelNames, loadSettings,
} = useManage()

onMounted(async () => {
  await fetchTab('command-config')
  await loadSettings()
  // the allowed-channels picker needs the channel names
  await ensureChannelNames()
})

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
</script>

<template>
  <div>
    <SettingRows group="commands" />
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
</template>
