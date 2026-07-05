<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'

const { data, error, fetchTab, run } = useManage()
const newChannel = ref({ name: '', key: '' })
onMounted(() => fetchTab('channels'))

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
</script>

<template>
  <div>
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
</template>
