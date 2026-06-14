<script setup>
// A small circled-"i" info icon. Shows `text` on hover/focus; clicking pins it
// open (so it works on touch and stays put while reading).
import { ref } from 'vue'

defineProps({ text: { type: String, default: '' } })
const pinned = ref(false)
</script>

<template>
  <span
    class="infotip"
    :class="{ pinned }"
    tabindex="0"
    role="button"
    :aria-label="text"
    @click="pinned = !pinned"
    @keydown.enter.prevent="pinned = !pinned"
    @keydown.space.prevent="pinned = !pinned"
    @blur="pinned = false"
  >
    <span class="dot" aria-hidden="true">i</span>
    <span class="bubble">{{ text }}</span>
  </span>
</template>

<style scoped>
.infotip {
  position: relative;
  display: inline-flex;
  vertical-align: middle;
  cursor: pointer;
  outline: none;
}
.dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid var(--muted);
  color: var(--muted);
  font: italic 700 11px/1 Georgia, "Times New Roman", serif;
  user-select: none;
}
.infotip:hover .dot,
.infotip:focus .dot,
.infotip.pinned .dot {
  border-color: var(--accent);
  color: var(--accent);
}
.bubble {
  display: none;
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 30;
  width: max-content;
  max-width: 300px;
  padding: 6px 9px;
  background: var(--panel2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
  font-size: 12px;
  line-height: 1.35;
  white-space: normal;
  text-align: left;
}
.infotip:hover .bubble,
.infotip:focus .bubble,
.infotip.pinned .bubble {
  display: block;
}
</style>
