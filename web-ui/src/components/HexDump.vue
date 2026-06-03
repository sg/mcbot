<script setup>
import { computed } from 'vue'

const props = defineProps({
  hex: { type: String, default: '' },
  // active byte range [offset, length] to highlight, or null
  activeRange: { type: Object, default: null },
})
const emit = defineEmits(['hover-offset'])

// Split the hex string into byte tokens.
const bytes = computed(() => {
  const h = (props.hex || '').replace(/\s+/g, '')
  const out = []
  for (let i = 0; i + 2 <= h.length; i += 2) out.push(h.slice(i, i + 2))
  return out
})

function isActive(i) {
  const r = props.activeRange
  return r && i >= r.offset && i < r.offset + r.length
}
</script>

<template>
  <div class="hexdump">
    <span
      v-for="(b, i) in bytes"
      :key="i"
      class="hexbyte"
      :class="{ active: isActive(i) }"
      @mouseenter="emit('hover-offset', i)"
      @mouseleave="emit('hover-offset', null)"
      >{{ b }}<span v-if="(i + 1) % 16 === 0"><br /></span><span v-else> </span></span
    >
    <div v-if="!bytes.length" class="empty">no raw bytes</div>
  </div>
</template>
