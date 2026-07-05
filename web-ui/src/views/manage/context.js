import { inject } from 'vue'

// Shared state/actions provided by Manage.vue to its tab components:
// { data, error, notice, fetchTab, reload, run, groupNames, commandNames,
//   channelNames, ensureGroupNames, ensureCommandNames, ensureChannelNames,
//   settings, settingsFor, applySetting, loadSettings }
export const ManageKey = Symbol('manage-context')

export function useManage() {
  return inject(ManageKey)
}
