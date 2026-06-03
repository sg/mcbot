import { useAuth } from './stores/auth.js'

// fetch wrapper: attaches the bearer token, JSON-encodes `json`, and on 401
// clears auth and bounces to the login route. `path` is relative to /api.
export async function api(path, opts = {}) {
  const auth = useAuth()
  const headers = { ...(opts.headers || {}) }
  if (auth.token) headers['Authorization'] = `Bearer ${auth.token}`
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    opts = { ...opts, body: JSON.stringify(opts.json) }
    delete opts.json
  }
  const res = await fetch(`/api${path}`, { ...opts, headers })
  if (res.status === 401) {
    auth.clear()
    if (!location.hash.startsWith('#/login')) location.hash = '#/login'
    throw new Error('unauthorized')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail || detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return res.json()
}

// Build a WebSocket URL for a feed, passing the token as a query param
// (browsers can't set Authorization headers on a WebSocket).
export function wsUrl(path) {
  const auth = useAuth()
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api${path}?token=${encodeURIComponent(auth.token)}`
}
