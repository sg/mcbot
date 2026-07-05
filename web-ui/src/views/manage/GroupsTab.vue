<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'

const { data, error, fetchTab, run, commandNames, ensureCommandNames } = useManage()
const newGroup = ref({ name: '', commands: '' })

onMounted(async () => {
  await fetchTab('groups')
  // the grant picker needs the universe of command names
  await ensureCommandNames()
})

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
</script>

<template>
  <div>
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
</template>
