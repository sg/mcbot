<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'

const { data, error, notice, fetchTab, reload, run, groupNames, ensureGroupNames } = useManage()
const newUser = ref({ sel: '', group: '' }) // create-bot-user form

onMounted(async () => {
  await fetchTab('users')
  await ensureGroupNames()
  // the "create bot user from a contact" picker needs the contact list
  await fetchTab('contacts')
})

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
    await reload('users') // refresh the Users table so the new row shows now
  } catch (e) {
    // e.g. 409 when a bot user already exists for this contact
    alert(e.message)
  }
}
</script>

<template>
  <div>
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
</template>
