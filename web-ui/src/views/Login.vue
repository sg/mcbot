<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { useAuth } from '../stores/auth.js'

const auth = useAuth()
const router = useRouter()
const username = ref('')
const password = ref('')
const token = ref('')
const error = ref('')
const busy = ref(false)

async function login() {
  error.value = ''
  busy.value = true
  try {
    const r = await api('/login', {
      method: 'POST',
      json: { username: username.value, password: password.value },
    })
    auth.set(r.token, r.user ? `user:${r.user}` : '')
    router.push('/')
  } catch (e) {
    error.value = e.message || 'login failed'
  } finally {
    busy.value = false
  }
}

async function useToken() {
  error.value = ''
  if (!token.value.trim()) return
  auth.set(token.value.trim(), '')
  // Validate by hitting /me.
  try {
    const me = await api('/me')
    auth.set(token.value.trim(), me.identity)
    router.push('/')
  } catch (e) {
    auth.clear()
    error.value = 'invalid token'
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <h2>mcbot admin</h2>
      <div v-if="error" class="err">{{ error }}</div>
      <label>Username</label>
      <input v-model="username" @keyup.enter="login" autofocus />
      <label>Password</label>
      <input v-model="password" type="password" @keyup.enter="login" />
      <button :disabled="busy" @click="login">Log in</button>
      <label style="margin-top: 10px">…or paste an API token</label>
      <input v-model="token" placeholder="bearer token" @keyup.enter="useToken" />
      <button @click="useToken">Use token</button>
    </div>
  </div>
</template>
