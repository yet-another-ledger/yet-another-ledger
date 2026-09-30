/**
 * Settings panel composable – controls the unified, Affine-style settings
 * modal overlay (single gear entry point).
 *
 * The open/active state is a module-level singleton (same pattern as
 * ``useTheme``) so the header button, the panel component and any deep-link
 * all share one source of truth.  The panel itself is mounted once in
 * ``App.vue`` and overlays whatever page is active.
 */

import { ref } from 'vue'

export type SettingsCategory = 'preference' | 'smtp' | 'theme' | 'dictionary' | 'mileage' | 'ai' | 'document-defaults' | 'email-templates' | 'agent-tokens'

// Module-level shared state — every useSettingsPanel() caller sees the same refs.
const isOpen = ref(false)
const activeCategory = ref<SettingsCategory>('preference')

export function useSettingsPanel() {
  function open(category: SettingsCategory = 'preference') {
    activeCategory.value = category
    isOpen.value = true
  }

  function close() {
    isOpen.value = false
  }

  return { isOpen, activeCategory, open, close }
}
