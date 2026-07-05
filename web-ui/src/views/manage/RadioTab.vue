<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../../api.js'
import InfoTip from '../../components/InfoTip.vue'
import { useManage } from './context.js'
import SettingRows from './SettingRows.vue'

const { data, error, notice, fetchTab, run, loadSettings } = useManage()

onMounted(async () => {
  await fetchTab('radio')
  await loadSettings()
  await loadRadioPresets()
})

// ---- advert send ----
const advertMode = ref('zero') // 'zero' or 'flood'
const advertSending = ref(false)
async function sendAdvert() {
  if (advertSending.value) return
  advertSending.value = true
  await run(
    api('/radio/advert', { method: 'POST', json: { flood: advertMode.value === 'flood' } }),
    `${advertMode.value === 'flood' ? 'flood' : 'zero-hop'} advert sent`,
  )
  advertSending.value = false
}

// ---- radio config forms (Identity + Radio settings) ----
const radioPresets = ref([])
const maxTxPower = ref(null)
const selectedPreset = ref('')
const identityForm = ref({ name: '', private_key: '' })
const identityOrig = ref({ name: '', private_key: '' })
const identityReboot = ref(false)
const radioForm = ref({})
const radioOrig = ref({})
const radioReboot = ref(false)
const identityChanged = computed(
  () => JSON.stringify(identityForm.value) !== JSON.stringify(identityOrig.value),
)
const radioChanged = computed(
  () => JSON.stringify(radioForm.value) !== JSON.stringify(radioOrig.value),
)

function radioVal(key) {
  const rec = deviceInfoMap.value.get(key)
  return rec ? parseDevValue(rec.value) : null
}
// snapshot the radio's reported values into the editable forms (form + a
// pristine copy so Save/Cancel only enable on an actual change)
function populateRadioForms() {
  identityForm.value = { name: radioVal('self_info.name') ?? '', private_key: '' }
  identityOrig.value = { ...identityForm.value }
  radioForm.value = {
    freq: radioVal('self_info.radio_freq'),
    bw: radioVal('self_info.radio_bw'),
    sf: radioVal('self_info.radio_sf'),
    cr: radioVal('self_info.radio_cr'),
    tx_power: radioVal('self_info.tx_power'),
    lat: radioVal('self_info.adv_lat'),
    lon: radioVal('self_info.adv_lon'),
    adv_loc_policy: !!radioVal('self_info.adv_loc_policy'),
  }
  radioOrig.value = { ...radioForm.value }
  maxTxPower.value = radioVal('self_info.max_tx_power')
  selectedPreset.value = ''
  identityReboot.value = false
  radioReboot.value = false
}
async function loadRadioPresets() {
  if (radioPresets.value.length) return
  try {
    radioPresets.value = (await api('/radio/presets')).items
  } catch (e) {
    error.value = e.message
  }
}
function applyPreset() {
  const p = radioPresets.value.find((x) => x.name === selectedPreset.value)
  if (!p) return // "Custom" clears the selection but keeps the fields
  radioForm.value.freq = p.freq
  radioForm.value.bw = p.bw
  radioForm.value.sf = p.sf
  radioForm.value.cr = p.cr
}
function cancelIdentity() {
  identityForm.value = { ...identityOrig.value }
  identityReboot.value = false
}
async function saveIdentity() {
  const f = identityForm.value
  const body = { reboot: identityReboot.value }
  if (f.name !== identityOrig.value.name) body.name = f.name
  if ((f.private_key || '').trim()) {
    if (
      !confirm(
        'Import a new private key?\n\nThis gives the node a NEW identity — ' +
          'existing contacts must re-add the bot and old DMs will no longer decrypt.',
      )
    )
      return
    body.private_key = f.private_key.trim()
  }
  await run(api('/radio/identity', { method: 'POST', json: body }), 'identity updated', ['radio'])
}
function cancelRadio() {
  radioForm.value = { ...radioOrig.value }
  selectedPreset.value = ''
  radioReboot.value = false
}
async function saveRadio() {
  const f = radioForm.value
  const o = radioOrig.value
  const body = { reboot: radioReboot.value }
  // radio params are atomic on the firmware — send all four if any changed
  if (['freq', 'bw', 'sf', 'cr'].some((k) => f[k] !== o[k])) {
    body.freq = Number(f.freq)
    body.bw = Number(f.bw)
    body.sf = Number(f.sf)
    body.cr = Number(f.cr)
  }
  if (f.tx_power !== o.tx_power) body.tx_power = Number(f.tx_power)
  if (f.lat !== o.lat || f.lon !== o.lon) {
    body.lat = Number(f.lat)
    body.lon = Number(f.lon)
  }
  if (f.adv_loc_policy !== o.adv_loc_policy) body.adv_loc_policy = f.adv_loc_policy
  await run(api('/radio/settings', { method: 'POST', json: body }), 'radio settings updated', ['radio'])
}

// ---- radio contact-table rollover ----
const contactStatus = ref(null)
const statusLoading = ref(false)
const evictBusy = ref(false)
const policyForm = ref({ enabled: true, headroom: 8 })

async function loadContactStatus() {
  if (statusLoading.value) return
  statusLoading.value = true
  error.value = ''
  try {
    const s = await api('/radio/contacts-status')
    contactStatus.value = s
    policyForm.value = { enabled: s.policy.enabled, headroom: s.policy.headroom }
  } catch (e) {
    error.value = e.message
  }
  statusLoading.value = false
}

async function applyPolicy() {
  await run(
    api('/radio/contacts-policy', {
      method: 'POST',
      json: {
        enabled: policyForm.value.enabled,
        headroom: Number(policyForm.value.headroom),
      },
    }),
    'Eviction policy updated (runtime only — set radio_evict_* in mcbot.conf to persist)',
  )
  await loadContactStatus()
}

async function evictNow() {
  if (evictBusy.value) return
  evictBusy.value = true
  error.value = ''
  notice.value = ''
  try {
    // dry-run preview first, then confirm the actual victims
    const preview = await api('/radio/evict-contacts', {
      method: 'POST',
      json: { dry_run: true },
    })
    const victims = preview.evicted || []
    if (!victims.length) {
      notice.value = `Nothing to evict (used ${preview.used}/${preview.max ?? '?'}).`
      evictBusy.value = false
      return
    }
    const names = victims.map((v) => v.name || v.pubkey.slice(0, 12))
    const shown = names.slice(0, 20).join('\n')
    const more = names.length > 20 ? `\n…and ${names.length - 20} more` : ''
    if (!confirm(`Evict ${victims.length} stale contact(s) from the radio?\n\n${shown}${more}`)) {
      evictBusy.value = false
      return
    }
    const res = await api('/radio/evict-contacts', { method: 'POST', json: {} })
    notice.value =
      `Evicted ${res.evicted.length} contact(s)` +
      (res.failed ? `, ${res.failed} failed` : '') +
      (res.shortfall ? `, ${res.shortfall} short (protected)` : '')
  } catch (e) {
    error.value = e.message
  }
  evictBusy.value = false
  await loadContactStatus()
}

// ---- device-info display ----
// Display label + (optional) unit for each known device_info key. Anything
// not listed falls into an "Other" group with the raw key.
const DEVINFO_GROUPS = [
  {
    title: 'Identity',
    keys: [
      ['self_info.name', 'Name'],
      ['self_info.public_key', 'Public key', { mono: true }],
      ['device_info.model', 'Model'],
      ['device_info.ver', 'Firmware version'],
      ['device_info.fw ver', 'Firmware code'],
      ['device_info.fw_build', 'Firmware build'],
    ],
  },
  {
    title: 'Radio',
    keys: [
      ['self_info.radio_freq', 'Frequency', { unit: 'MHz' }],
      ['self_info.radio_bw', 'Bandwidth', { unit: 'kHz' }],
      ['self_info.radio_sf', 'Spreading factor'],
      ['self_info.radio_cr', 'Coding rate'],
      ['self_info.tx_power', 'TX power', { unit: 'dBm' }],
      ['self_info.max_tx_power', 'Max TX power', { unit: 'dBm' }],
      ['device_info.path_hash_mode', 'Out path-hash mode', { fmt: (v) => `${Number(v) + 1} byte(s)/hop` }],
    ],
  },
  {
    title: 'Advertising',
    keys: [
      ['self_info.adv_type', 'Advertised type'],
      ['self_info.adv_lat', 'Latitude'],
      ['self_info.adv_lon', 'Longitude'],
      ['self_info.adv_loc_policy', 'Location-share policy'],
    ],
  },
  {
    title: 'Capacity',
    keys: [
      ['device_info.max_channels', 'Max channels'],
      ['device_info.max_contacts', 'Max contacts'],
    ],
  },
  {
    title: 'Battery / memory',
    keys: [
      ['battery.level', 'Battery', { unit: 'mV' }],
      ['battery.total_kb', 'Total memory', { unit: 'kB' }],
      ['battery.used_kb', 'Used memory', { unit: 'kB' }],
    ],
  },
  {
    title: 'Other',
    keys: [
      ['device_info.ble_pin', 'BLE PIN'],
      ['device_info.repeat', 'Repeater mode'],
      ['self_info.manual_add_contacts', 'Manual contact add'],
      ['self_info.multi_acks', 'Multi-acks'],
      ['self_info.telemetry_mode_base', 'Telemetry: base'],
      ['self_info.telemetry_mode_env', 'Telemetry: env'],
      ['self_info.telemetry_mode_loc', 'Telemetry: location'],
    ],
  },
]
const DEVINFO_KNOWN = new Set(DEVINFO_GROUPS.flatMap((g) => g.keys.map((k) => k[0])))

function parseDevValue(raw) {
  if (raw == null) return null
  try { return JSON.parse(raw) } catch { return raw }
}
function devValueText(rec, opts) {
  const v = parseDevValue(rec.value)
  if (v == null || v === '') return ''
  if (opts?.fmt) return opts.fmt(v)
  let s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  if (opts?.unit) s = `${s} ${opts.unit}`
  return s
}
const deviceInfoMap = computed(() => {
  const m = new Map()
  for (const rec of data.value.radio || []) m.set(rec.key, rec)
  return m
})
// Records whose keys aren't in any group go into "Unrecognized" so we
// don't silently drop fields the firmware adds later.
const unknownDeviceKeys = computed(() =>
  [...(data.value.radio || [])]
    .filter((r) => !DEVINFO_KNOWN.has(r.key))
    .sort((a, b) => a.key.localeCompare(b.key)),
)

// (re)snapshot whenever the device-info rows land or refresh (e.g. after
// Save reloads the radio tab data). Registered after deviceInfoMap exists
// because `immediate` runs it during setup.
watch(() => data.value.radio, populateRadioForms, { immediate: true })
</script>

<template>
  <div>
    <!-- Identity -->
    <h4 class="sec">Identity</h4>
    <div class="kv">
      <div class="k">Name</div>
      <div><input v-model="identityForm.name" maxlength="32" style="width: 16em" /></div>
      <div class="k">Public key</div>
      <div class="mono muted">{{ radioVal('self_info.public_key') || '—' }}</div>
      <div class="k">New private key</div>
      <div>
        <input
          v-model="identityForm.private_key"
          placeholder="128-hex to rotate identity (optional)"
          style="width: 24em"
        />
        <InfoTip text="Paste a 64-byte (128 hex) private key to give the node a NEW identity. Existing contacts must re-add the bot and old DMs stop decrypting. Leave blank to keep the current key." />
      </div>
    </div>
    <div class="toolbar">
      <button :disabled="!identityChanged" @click="saveIdentity">Save</button>
      <button :disabled="!identityChanged" @click="cancelIdentity">Cancel</button>
      <label class="chk"><input type="checkbox" v-model="identityReboot" /> Reboot after</label>
    </div>

    <!-- Radio settings -->
    <h4 class="sec">Radio settings</h4>
    <div class="toolbar">
      <label class="chk">
        Preset
        <select v-model="selectedPreset" @change="applyPreset">
          <option value="">Custom…</option>
          <option v-for="p in radioPresets" :key="p.name" :value="p.name">{{ p.name }}</option>
        </select>
      </label>
      <InfoTip text="Fills the fields below with a region's recommended values — review before saving. Presets are starting points; verify for your area." />
    </div>
    <div class="kv">
      <div class="k">Frequency (MHz)</div>
      <div><input type="number" step="0.001" v-model.number="radioForm.freq" style="width: 9em" /></div>
      <div class="k">Bandwidth (kHz)</div>
      <div><input type="number" step="0.1" v-model.number="radioForm.bw" style="width: 9em" /></div>
      <div class="k">Spreading factor</div>
      <div><input type="number" min="5" max="12" v-model.number="radioForm.sf" style="width: 6em" /></div>
      <div class="k">Coding rate</div>
      <div><input type="number" min="5" max="8" v-model.number="radioForm.cr" style="width: 6em" /></div>
      <div class="k">TX power (dBm)</div>
      <div>
        <input type="number" min="0" v-model.number="radioForm.tx_power" style="width: 6em" />
        <span v-if="maxTxPower != null" class="muted"> max {{ maxTxPower }}</span>
      </div>
      <div class="k">Latitude</div>
      <div><input type="number" step="0.00001" v-model.number="radioForm.lat" style="width: 11em" /></div>
      <div class="k">Longitude</div>
      <div><input type="number" step="0.00001" v-model.number="radioForm.lon" style="width: 11em" /></div>
      <div class="k">Include location in adverts</div>
      <div><input type="checkbox" v-model="radioForm.adv_loc_policy" /></div>
    </div>
    <div class="toolbar">
      <button :disabled="!radioChanged" @click="saveRadio">Save</button>
      <button :disabled="!radioChanged" @click="cancelRadio">Cancel</button>
      <label class="chk"><input type="checkbox" v-model="radioReboot" /> Reboot after</label>
      <InfoTip text="Radio-parameter changes usually require a reboot to take effect." />
    </div>

    <!-- Advert -->
    <h4 class="sec">Advert</h4>
    <div class="toolbar">
      <label class="chk">
        <input type="radio" value="zero" v-model="advertMode" />
        Zero hop
      </label>
      <label class="chk">
        <input type="radio" value="flood" v-model="advertMode" />
        Flood
      </label>
      <button :disabled="advertSending" @click="sendAdvert">
        {{ advertSending ? 'Sending…' : 'Advert' }}
      </button>
      <InfoTip text="Zero-hop reaches direct neighbors only; flood propagates through the mesh." />
    </div>

    <SettingRows group="radio" />

    <!-- Contact-table rollover -->
    <div style="border-top: 1px solid var(--border, #333)">
      <div class="toolbar">
        <strong>Contact table</strong>
        <button :disabled="statusLoading" @click="loadContactStatus">
          {{ statusLoading ? 'Checking…' : 'Check contact table' }}
        </button>
        <InfoTip text="Reads the radio's contact list (takes a few seconds)." />
      </div>
      <div v-if="contactStatus" class="decoded">
        <div class="kv">
          <div class="k">Used / max</div>
          <div>
            {{ contactStatus.used }} / {{ contactStatus.max ?? '?' }}
            <span class="muted">({{ contactStatus.free ?? '?' }} free)</span>
          </div>
          <div class="k">Protected / evictable</div>
          <div>{{ contactStatus.protected }} protected, {{ contactStatus.eligible }} evictable</div>
          <div class="k">Firmware autoadd byte</div>
          <div class="mono">{{ contactStatus.autoadd_raw ?? 'n/a' }}</div>
        </div>
        <div class="toolbar">
          <label class="chk">
            <input type="checkbox" v-model="policyForm.enabled" />
            Auto-evict (startup + when full)
          </label>
          <label class="chk">
            Headroom
            <input type="number" min="1" max="100" v-model.number="policyForm.headroom" style="width: 4em" />
          </label>
          <button @click="applyPolicy">Apply</button>
          <button :disabled="evictBusy" @click="evictNow">
            {{ evictBusy ? 'Evicting…' : 'Evict to headroom now' }}
          </button>
          <InfoTip text="Evicts the stalest contacts from the radio (bot users, owners, and recent DM peers are protected; the bot's database keeps the full archive). Policy changes here are runtime only — set radio_evict_* in mcbot.conf to persist across restarts." />
        </div>
      </div>
    </div>

    <h4 class="sec">Device info</h4>
    <div class="muted" style="padding: 0 12px 6px">what the radio currently reports (read-only)</div>
    <div class="decoded">
      <template v-for="g in DEVINFO_GROUPS" :key="g.title">
        <h4 style="margin: 12px 0 4px">{{ g.title }}</h4>
        <div class="kv">
          <template v-for="entry in g.keys" :key="entry[0]">
            <div class="k">{{ entry[1] }}</div>
            <div :class="{ mono: entry[2]?.mono }">
              {{ devValueText(deviceInfoMap.get(entry[0]) || {}, entry[2]) || '—' }}
            </div>
          </template>
        </div>
      </template>
      <template v-if="unknownDeviceKeys.length">
        <h4 style="margin: 12px 0 4px">Unrecognized</h4>
        <div class="kv">
          <template v-for="r in unknownDeviceKeys" :key="r.key">
            <div class="k mono">{{ r.key }}</div>
            <div>{{ devValueText(r) || '—' }}</div>
          </template>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* Section headers. */
.sec {
  margin: 16px 0 6px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.sec:first-child {
  border-top: 0;
  padding-top: 0;
}
</style>
