<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../../api.js'
import { useManage } from './context.js'
import { cell } from './cells.js'

const { data, fetchTab, reload, run } = useManage()

const dbQuery = ref('')
const dbResult = ref(null) // { columns, rows, rowcount }
const dbError = ref('')
const dbRunning = ref(false)
const picker = ref(null) // null | 'load' | 'delete'

onMounted(() => fetchTab('database')) // populates the saved-query picker

const dbSummary = computed(() => {
  const r = dbResult.value
  if (!r) return ''
  if (r.columns.length) return `${r.rows.length} row(s)`
  return r.rowcount >= 0 ? `${r.rowcount} row(s) affected` : 'OK'
})

async function runQuery() {
  const sql = dbQuery.value.trim()
  if (!sql) return
  dbRunning.value = true
  dbError.value = ''
  dbResult.value = null
  try {
    dbResult.value = await api('/db/query', { method: 'POST', json: { sql } })
  } catch (e) {
    dbError.value = e.message
  } finally {
    dbRunning.value = false
  }
}

async function saveQuery() {
  const sql = dbQuery.value.trim()
  if (!sql) return
  const name = (window.prompt('Save query as:') || '').trim()
  if (!name) return
  await run(
    api('/db/saved-queries', { method: 'POST', json: { name, query: sql } }),
    `saved "${name}"`,
    ['database'],
  )
}

function openPicker(mode) {
  picker.value = mode
  reload('database') // refresh the saved-query list
}
function pickQuery(q) {
  if (picker.value !== 'load') return
  dbQuery.value = q.query
  picker.value = null
}
async function deleteQuery(q) {
  if (!confirm(`Delete saved query "${q.name}"?`)) return
  await run(
    api(`/db/saved-queries/${q.id}`, { method: 'DELETE' }),
    `deleted "${q.name}"`,
    ['database'],
  )
}
</script>

<template>
  <!-- SQL editor (top) + results, with saved queries -->
  <div class="db-console">
    <div class="db-editor">
      <textarea
        v-model="dbQuery"
        class="db-query mono"
        spellcheck="false"
        placeholder="SQL — one statement, e.g. SELECT * FROM contacts LIMIT 20"
      ></textarea>
      <div class="toolbar">
        <button :disabled="!dbQuery.trim() || dbRunning" @click="runQuery">
          {{ dbRunning ? 'Running…' : 'Submit' }}
        </button>
        <button :disabled="!dbQuery.trim()" @click="saveQuery">Save</button>
        <button @click="openPicker('load')">Load</button>
        <button @click="openPicker('delete')">Delete</button>
        <span class="muted">{{ dbSummary }}</span>
        <span class="muted db-warn">runs against the live database</span>
      </div>
    </div>

    <!-- Load / Delete picker -->
    <div v-if="picker" class="db-picker">
      <div class="toolbar">
        <strong>{{ picker === 'load' ? 'Load a saved query' : 'Delete a saved query' }}</strong>
        <button @click="picker = null">Cancel</button>
      </div>
      <div class="db-picker-list">
        <div v-if="!(data.database && data.database.length)" class="empty">
          no saved queries
        </div>
        <table v-else>
          <tbody>
            <tr
              v-for="q in data.database"
              :key="q.id"
              :class="{ clickable: picker === 'load' }"
              @click="pickQuery(q)"
            >
              <td style="width: 20%">{{ q.name }}</td>
              <td class="mono muted">{{ (q.query || '').slice(0, 80) }}</td>
              <td v-if="picker === 'delete'" style="width: 1%">
                <a href="#" title="delete" @click.prevent.stop="deleteQuery(q)">✕</a>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Results -->
    <div class="db-results">
      <div v-if="dbError" class="err" style="padding: 8px 12px">{{ dbError }}</div>
      <table v-else-if="dbResult && dbResult.columns.length">
        <thead>
          <tr><th v-for="c in dbResult.columns" :key="c">{{ c }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in dbResult.rows" :key="i">
            <td v-for="(v, j) in row" :key="j" :title="cell(v)">{{ cell(v) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else-if="dbResult" class="empty">{{ dbSummary }}</div>
      <div v-else class="empty">no results yet</div>
    </div>
  </div>
</template>

<style scoped>
/* Query editor across the top ~1/5, results fill the rest (each scrolls
   independently); the picker sits between them when open. */
.db-console {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.db-editor {
  flex: 0 0 20%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.db-query {
  flex: 1;
  min-height: 0;
  resize: none;
  width: 100%;
  box-sizing: border-box;
  font-size: 13px;
}
.db-warn {
  margin-left: auto;
  font-style: italic;
}
.db-picker {
  flex: 0 0 auto;
  max-height: 40%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 4px;
  margin: 6px 0;
}
.db-picker-list {
  overflow: auto;
  min-height: 0;
}
.db-results {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
</style>
