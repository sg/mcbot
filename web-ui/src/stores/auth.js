import { defineStore } from 'pinia'

// Holds the bearer token (a session token from /api/login, or a manually
// entered API token) and the identity string the server reports.
export const useAuth = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('mcbot_token') || '',
    identity: localStorage.getItem('mcbot_identity') || '',
  }),
  actions: {
    set(token, identity) {
      this.token = token
      this.identity = identity || ''
      localStorage.setItem('mcbot_token', token)
      localStorage.setItem('mcbot_identity', this.identity)
    },
    clear() {
      this.token = ''
      this.identity = ''
      localStorage.removeItem('mcbot_token')
      localStorage.removeItem('mcbot_identity')
    },
  },
})
