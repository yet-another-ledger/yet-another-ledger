import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }))
vi.mock('../../src/api/http', () => api)
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ user: { email: 'owner@example.test' } }) }))
vi.mock('../../src/stores/company', () => ({ useCompanyStore: () => ({ company: { name: 'Test' }, loading: false }) }))
vi.mock('../../src/composables/useBreakpoint', async () => {
  const { ref } = await import('vue')
  const isDesktop = ref(true)
  return { useBreakpoint: () => ({ isDesktop }) }
})
vi.mock('naive-ui', () => {
  const box = { template: '<div><slot /></div>' }
  return {
    NIcon: box, NForm: box, NFormItem: box, NText: box, NAlert: box, NDivider: box,
    NLayout: box, NLayoutSider: box, NLayoutHeader: box, NLayoutContent: box, NDrawerContent: box, NTag: box,
    NButton: { template: '<button><slot /></button>' },
    NDrawer: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
    NMenu: { name: 'NMenu', props: ['options'], template: '<nav><span v-for="item in options" :key="item.key">{{ item.label }}</span></nav>' },
    NSwitch: { props: ['value', 'disabled'], emits: ['update:value'], template: '<input type="checkbox" :checked="value" :disabled="disabled" @change="$emit(\'update:value\', $event.target.checked)" />' },
    NSelect: box,
  }
})

import { i18n } from '../../src/i18n'
import {
  applyLocale, applyThemePreference, loaded, loadUserPreferences, persistUserPreferences,
  resetUserPreferencesLoaded, setShowBatteryAssets, showBatteryAssets,
} from '../../src/composables/userPreferences'
import { useLocale } from '../../src/composables/useLocale'
import { useBreakpoint } from '../../src/composables/useBreakpoint'
import AppLayout from '../../src/components/AppLayout.vue'
import PreferenceSettings from '../../src/components/settings/PreferenceSettings.vue'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('Battery navigation preference', () => {
  beforeEach(async () => {
    resetUserPreferencesLoaded()
    await persistUserPreferences()
    vi.clearAllMocks()
    applyThemePreference('system')
    applyLocale('en')
    api.get.mockResolvedValue({ theme: 'system', locale: 'en' })
    api.put.mockResolvedValue({})
    useBreakpoint().isDesktop.value = true
  })

  it('defaults off for old preferences and persists the full snapshot on enable/disable', async () => {
    await loadUserPreferences()
    expect(showBatteryAssets.value).toBe(false)
    await setShowBatteryAssets(true)
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/me', { theme: 'system', locale: 'en', show_battery_assets: true })
    await useLocale().setLocale('zh')
    applyThemePreference('dark')
    await persistUserPreferences()
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/me', { theme: 'dark', locale: 'zh', show_battery_assets: true })
    await setShowBatteryAssets(false)
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/me', { theme: 'dark', locale: 'zh', show_battery_assets: false })
  })

  it('loads a saved true and resets it on logout, failed loading and old accounts', async () => {
    api.get.mockResolvedValueOnce({ theme: 'dark', locale: 'zh', show_battery_assets: true })
    await loadUserPreferences()
    expect(showBatteryAssets.value).toBe(true)
    resetUserPreferencesLoaded()
    expect(showBatteryAssets.value).toBe(false)
    await loadUserPreferences()
    expect(showBatteryAssets.value).toBe(false)
    api.get.mockRejectedValueOnce(new Error('offline'))
    await loadUserPreferences()
    expect(loaded.value).toBe(false)
    expect(showBatteryAssets.value).toBe(false)
  })

  it('ignores a previous account response after reset', async () => {
    const response = deferred<{ show_battery_assets: boolean }>()
    api.get.mockReturnValueOnce(response.promise)
    const request = loadUserPreferences()
    resetUserPreferencesLoaded()
    await loadUserPreferences()
    response.resolve({ show_battery_assets: true })
    await request
    expect(showBatteryAssets.value).toBe(false)
  })

  it('rolls back a failed save and does not write before preferences load', async () => {
    await setShowBatteryAssets(true)
    expect(showBatteryAssets.value).toBe(false)
    expect(api.put).not.toHaveBeenCalled()
    await loadUserPreferences()
    api.put.mockRejectedValueOnce(new Error('save failed'))
    await expect(setShowBatteryAssets(true)).rejects.toThrow('save failed')
    expect(showBatteryAssets.value).toBe(false)
  })

  it('does not restore an old account value when a save fails after logout', async () => {
    api.get.mockResolvedValueOnce({ show_battery_assets: true })
    await loadUserPreferences()
    const response = deferred<void>()
    api.put.mockReturnValueOnce(response.promise)
    const request = setShowBatteryAssets(false)
    await flushPromises()
    resetUserPreferencesLoaded()
    response.reject(new Error('old account failure'))
    await expect(request).rejects.toThrow('old account failure')
    expect(showBatteryAssets.value).toBe(false)
  })

  it.each([true, false])('updates the shared navigation menu (desktop=%s), retaining the direct route', async (desktop) => {
    useBreakpoint().isDesktop.value = desktop
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/dashboard', name: 'dashboard', component: { template: '<div />' } },
      { path: '/battery-assets', name: 'battery-assets', component: { template: '<div>Battery page retained</div>' } },
    ] })
    await router.push('/dashboard')
    const layout = mount(AppLayout, { global: { plugins: [router, i18n] } })
    if (!desktop) await layout.get('.hamburger-btn').trigger('click')
    const menu = () => layout.findComponent({ name: 'NMenu' }).props('options') as { key: string }[]
    expect(menu().some(item => item.key === 'battery-assets')).toBe(false)
    await loadUserPreferences()
    const preferences = mount(PreferenceSettings, { global: { plugins: [i18n] } })
    await preferences.get('input[type=checkbox]').setValue(true)
    await flushPromises()
    expect(menu().some(item => item.key === 'battery-assets')).toBe(true)
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/me', expect.objectContaining({ show_battery_assets: true }))
    await preferences.get('input[type=checkbox]').setValue(false)
    await flushPromises()
    expect(menu().some(item => item.key === 'battery-assets')).toBe(false)
    await router.push('/battery-assets')
    await flushPromises()
    expect(layout.text()).toContain('Battery page retained')
    preferences.unmount()
    layout.unmount()
  })
})
