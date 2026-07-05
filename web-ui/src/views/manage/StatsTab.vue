<script setup>
import { onMounted } from 'vue'
import { useManage } from './context.js'

const { data, fetchTab } = useManage()
onMounted(() => fetchTab('stats'))
</script>

<template>
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
</template>
