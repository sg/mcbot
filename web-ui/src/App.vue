<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from './stores/auth.js'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const showNav = computed(() => route.path !== '/login')

function logout() {
  auth.clear()
  router.push('/login')
}
</script>

<template>
  <nav v-if="showNav" class="nav">
    <span class="brand">mcbot</span>
    <router-link to="/messages">Messages</router-link>
    <router-link to="/packets">Packets</router-link>
    <router-link to="/manage">Manage</router-link>
    <span class="spacer" />
    <span class="who">{{ auth.identity || 'authenticated' }}</span>
    <button @click="logout">Log out</button>
  </nav>
  <router-view />
</template>
