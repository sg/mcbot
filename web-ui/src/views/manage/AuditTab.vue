<script setup>
import { onMounted } from 'vue'
import { useManage } from './context.js'
import { cols, cellAt } from './cells.js'

const { data, fetchTab } = useManage()
onMounted(() => fetchTab('audit'))
</script>

<template>
  <table v-if="Array.isArray(data.audit)">
    <thead>
      <tr><th v-for="c in cols(data.audit)" :key="c">{{ c }}</th></tr>
    </thead>
    <tbody>
      <tr v-for="(row, i) in data.audit" :key="i">
        <td v-for="c in cols(data.audit)" :key="c" :title="cellAt(row, c)">
          {{ cellAt(row, c) }}
        </td>
      </tr>
    </tbody>
  </table>
  <div v-else class="empty">loading…</div>
</template>
