<script setup>
const props = defineProps({
  fields: { type: Array, default: () => [] },
  decoded: { type: Object, default: null },
  activeIndex: { type: Number, default: -1 },
})
const emit = defineEmits(['hover-field'])

function fmtValue(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <div>
    <div
      v-for="(f, i) in fields"
      :key="i"
      class="field-row"
      :class="{ active: i === activeIndex }"
      @mouseenter="emit('hover-field', i)"
      @mouseleave="emit('hover-field', -1)"
    >
      <div class="fname">{{ f.name }}</div>
      <div class="fmeta">offset {{ f.offset }} · {{ f.length }} byte(s)</div>
      <div class="fval">{{ f.hex }}</div>
      <div v-if="f.value !== null && f.value !== undefined" class="fval muted">
        {{ fmtValue(f.value) }}
      </div>
      <div v-if="f.description" class="fmeta">{{ f.description }}</div>
    </div>

    <div v-if="decoded" class="decoded">
      <h4 style="margin: 6px 0">Decoded payload</h4>
      <div class="kv">
        <template v-for="(v, k) in decoded" :key="k">
          <div class="k">{{ k }}</div>
          <div class="mono">{{ fmtValue(v) }}</div>
        </template>
      </div>
    </div>
  </div>
</template>
