<script setup>
import InfoTip from '../../components/InfoTip.vue'
import { useManage } from './context.js'

defineProps({ group: { type: String, required: true } })
const { settingsFor, applySetting } = useManage()
</script>

<template>
  <div v-for="s in settingsFor(group)" :key="s.key" class="toolbar">
    <label class="chk">
      {{ s.label }}<template v-if="s.unit"> ({{ s.unit }})</template>
      <input
        type="number"
        :min="s.min"
        :max="s.max"
        :step="s.step"
        v-model.number="s.value"
        style="width: 5em"
      />
    </label>
    <button @click="applySetting(s.key)">Apply</button>
    <InfoTip :text="s.description" />
  </div>
</template>
