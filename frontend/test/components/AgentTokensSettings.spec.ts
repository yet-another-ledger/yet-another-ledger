import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import AgentTokensSettings from '../../src/components/settings/AgentTokensSettings.vue'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), del: vi.fn() }))
vi.mock('../../src/api/http', () => api)

const token = {
  id: 'token-1', name: 'Hermes', scopes: ['receipt:intake'],
  created_at: '2026-09-30T10:00:00Z', last_used_at: null, revoked_at: null,
}

afterEach(() => vi.clearAllMocks())

describe('AgentTokensSettings', () => {
  it('lists metadata and hides the one-time secret after copying in Chinese', async () => {
    api.get.mockResolvedValue({ items: [token] })
    const i18n = createI18n({ legacy: false, locale: 'zh', messages: { en, zh } })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = mount(AgentTokensSettings, { global: { plugins: [i18n] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Agent 访问令牌')
    expect(wrapper.text()).toContain('Hermes')
    expect(wrapper.text()).toContain('receipt:intake')

    // Exercise the one-time display after a successful create response.
    api.post.mockResolvedValue({ ...token, token: 'jai1.secret.once' })
    await wrapper.find('input').setValue('Temporary')
    const checkboxes = wrapper.findAll('.n-checkbox')
    expect(checkboxes.length).toBeGreaterThan(0)
    await checkboxes[0].trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect((wrapper.get('input[type="password"]').element as HTMLInputElement).value)
      .toBe('jai1.secret.once')

    await wrapper.get('.copy-button').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('jai1.secret.once')
    expect(wrapper.findAll('.secret-alert')).toHaveLength(0)
    expect(wrapper.text()).toContain('密钥已复制并隐藏。')
  })

  it('keeps the secret visible and explains manual copying when clipboard is unavailable', async () => {
    api.get.mockResolvedValue({ items: [] })
    api.post.mockResolvedValue({ ...token, token: 'jai1.secret.once' })
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    const i18n = createI18n({ legacy: false, locale: 'en', messages: { en, zh } })
    const wrapper = mount(AgentTokensSettings, { global: { plugins: [i18n] } })
    await flushPromises()

    await wrapper.find('input').setValue('Temporary')
    await wrapper.findAll('.n-checkbox')[0].trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await wrapper.get('.copy-button').trigger('click')
    await flushPromises()

    expect((wrapper.get('input[type="password"]').element as HTMLInputElement).value)
      .toBe('jai1.secret.once')
    expect(wrapper.text()).toContain('Clipboard access failed.')
    expect(wrapper.text()).toContain('copy it manually')
    expect(wrapper.findAll('.secret-alert')).toHaveLength(1)
  })

  it('keeps the secret after a rejected clipboard write and hides it after retry succeeds', async () => {
    api.get.mockResolvedValue({ items: [] })
    api.post.mockResolvedValue({ ...token, token: 'jai1.secret.once' })
    const writeText = vi.fn().mockRejectedValueOnce(new Error('permission denied')).mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const i18n = createI18n({ legacy: false, locale: 'en', messages: { en, zh } })
    const wrapper = mount(AgentTokensSettings, { global: { plugins: [i18n] } })
    await flushPromises()

    await wrapper.find('input').setValue('Temporary')
    await wrapper.findAll('.n-checkbox')[0].trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await wrapper.get('.copy-button').trigger('click')
    await flushPromises()

    expect((wrapper.get('input[type="password"]').element as HTMLInputElement).value)
      .toBe('jai1.secret.once')
    expect(wrapper.text()).toContain('Clipboard access failed.')
    expect(wrapper.findAll('.secret-alert')).toHaveLength(1)

    await wrapper.get('.copy-button').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.secret-alert')).toHaveLength(0)
    expect(wrapper.text()).toContain('Secret copied and hidden.')
  })
})
