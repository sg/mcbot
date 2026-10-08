<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../../api.js'
import InfoTip from '../../components/InfoTip.vue'
import { useManage } from './context.js'

const { data, error, notice, fetchTab } = useManage()
onMounted(() => fetchTab('stats'))

// ---- restart ----
// After POST /bot/restart the server is gone for a few seconds (full
// teardown + reinit). Probe /api/health every 10s and reload the page the
// first time it answers. A blind location.reload() against a dead server
// would strand the user on a browser error page where this code no longer
// runs, so the probe has to succeed before the reload.
const RESTART_POLL_MS = 10000
const restarting = ref(false)
const restartStatus = ref('')
let pollTimer = null

async function probeAndReload(attempt) {
  restartStatus.value = `waiting for the bot to come back… (check ${attempt})`
  try {
    const res = await fetch('/api/health', { cache: 'no-store' })
    if (res.ok) {
      restartStatus.value = 'bot is back — reloading'
      location.reload()
      return
    }
  } catch {
    /* still down — keep polling */
  }
  pollTimer = setTimeout(() => probeAndReload(attempt + 1), RESTART_POLL_MS)
}

async function restartBot() {
  if (restarting.value) return
  if (
    !confirm(
      'Restart the bot?\n\nSame as "!adm restart": full teardown + reinit — re-reads ' +
        'mcbot.conf, re-opens the DB, reconnects the radio. The web UI is unavailable ' +
        'for a few seconds; this page reloads itself once the bot is back.',
    )
  )
    return
  restarting.value = true
  error.value = ''
  notice.value = ''
  try {
    await api('/bot/restart', { method: 'POST' })
  } catch (e) {
    error.value = e.message
    restarting.value = false
    return
  }
  restartStatus.value = 'restart requested'
  pollTimer = setTimeout(() => probeAndReload(1), RESTART_POLL_MS)
}

onUnmounted(() => clearTimeout(pollTimer))
</script>

<template>
  <div>
    <div v-if="data.stats" class="decoded">
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
    <div v-else class="empty">loading…</div>

    <h4 class="sec">Bot</h4>
    <div class="toolbar">
      <button :disabled="restarting" @click="restartBot">
        {{ restarting ? 'Restarting…' : 'Restart' }}
      </button>
      <InfoTip text="Same as '!adm restart': full teardown + reinit (re-reads mcbot.conf, re-opens the DB, reconnects the radio). This page reloads itself when the bot is back." />
      <span v-if="restartStatus" class="muted">{{ restartStatus }}</span>
    </div>
  </div>
</template>

<style scoped>
.sec {
  margin: 16px 0 6px;
  padding: 10px 12px 0;
  border-top: 1px solid var(--border);
}
</style>
