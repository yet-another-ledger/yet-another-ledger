import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import AiSettingsForm from '../../src/components/settings/AiSettingsForm.vue'
import en from '../../src/locales/en.json'

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('../../src/api/http', () => api)

vi.mock('naive-ui', () => {
  const box = { template: '<div><slot /></div>' }
  const form = { emits: ['submit'], template: '<form @submit.prevent="$emit(\'submit\', $event)"><slot /></form>' }
  const input = {
    props: ['value', 'type', 'disabled', 'placeholder'], emits: ['update:value'],
    template: '<input :type="type || \'text\'" :value="value" :disabled="disabled" :placeholder="placeholder" @input="$emit(\'update:value\', $event.target.value)" />',
  }
  const select = {
    props: ['value', 'options'], emits: ['update:value'],
    template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{option.label}}</option></select>',
  }
  const button = { props: ['attrType'], template: '<button :type="attrType || \'button\'"><slot /></button>' }
  const checkbox = { props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" />' }
  return {
    NAlert: box, NButton: button, NCheckbox: checkbox, NForm: form,
    NFormItem: box, NInput: input, NSelect: select, NSpace: box, NSpin: box, NSwitch: checkbox,
  }
})

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
const envSettings = {
  enabled: true,
  api_protocol: 'chat_completions',
  base_url: 'https://synthetic-one.invalid/v1',
  environment_base_url: 'https://synthetic-one.invalid/v1',
  api_key_set: true,
  model: 'synthetic-one',
  receipt_prompt: '',
}

afterEach(() => vi.clearAllMocks())

describe('AiSettingsForm', () => {
  it('sends only the changed protocol when saving an environment-backed setup', async () => {
    api.get.mockResolvedValue(envSettings)
    api.put.mockResolvedValue({ ...envSettings, api_protocol: 'responses' })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    await wrapper.find('select').setValue('responses')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/v1/settings/ai', {
      api_protocol: 'responses',
    })
  })

  it('syncs rotated effective settings and tests against them without stale overrides', async () => {
    api.get.mockResolvedValue(envSettings)
    api.put.mockResolvedValue({
      enabled: true,
      api_protocol: 'chat_completions',
      base_url: 'https://synthetic-two.invalid/v1',
      api_key_set: true,
      model: 'synthetic-two',
      receipt_prompt: '',
    })
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/v1/settings/ai', {})
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {})
    expect((wrapper.findAll('input').find(input => input.element.getAttribute('type') === 'text')!.element as HTMLInputElement).value)
      .toBe('https://synthetic-two.invalid/v1')
  })

  it('sends only dirty fields for a temporary probe while preserving explicit overrides', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'responses', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const baseUrlInput = fields.find(input => (input.element as HTMLInputElement).value === envSettings.base_url)!
    await baseUrlInput.setValue('https://temporary.invalid/v1')
    await wrapper.find('select').setValue('responses')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      api_protocol: 'responses',
      base_url: 'https://temporary.invalid/v1',
    })
  })

  it('binds a newly typed key to the displayed provider on both Test and Save', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'responses', detail: 'ok',
    })
    api.put.mockResolvedValue({ ...envSettings, base_url: 'https://provider-b.invalid/v1' })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const baseUrl = fields.find(input => (input.element as HTMLInputElement).value === envSettings.base_url)!
    const key = fields.find(input => (input.element as HTMLInputElement).type === 'password')!
    await baseUrl.setValue('https://provider-b.invalid/v1')
    await key.setValue('synthetic-key-b')

    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      base_url: 'https://provider-b.invalid/v1',
      api_key: 'synthetic-key-b',
      expected_base_url: 'https://provider-b.invalid/v1',
    })

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/settings/ai', {
      base_url: 'https://provider-b.invalid/v1',
      api_key: 'synthetic-key-b',
      expected_base_url: 'https://provider-b.invalid/v1',
    })
  })

  it('omits the expected provider when no new key is supplied', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {})
  })

  it('binds a key-only Test and Save to the root shown by the loaded form', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    api.put.mockResolvedValue(envSettings)
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const keyInput = wrapper.findAll('input').find(input => (input.element as HTMLInputElement).type === 'password')!
    await keyInput.setValue('synthetic-key-a')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      api_key: 'synthetic-key-a',
      expected_base_url: envSettings.base_url,
    })

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/settings/ai', {
      api_key: 'synthetic-key-a',
      expected_base_url: envSettings.base_url,
    })
  })

  it('uses the concrete environment root when Base URL is cleared for a new key', async () => {
    api.get.mockResolvedValue({
      ...envSettings,
      base_url: 'https://saved-provider.invalid/v1',
      environment_base_url: 'https://environment-provider.invalid/v1',
    })
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    api.put.mockResolvedValue(envSettings)
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const baseUrl = fields.find(input => (input.element as HTMLInputElement).value === 'https://saved-provider.invalid/v1')!
    expect((baseUrl.element as HTMLInputElement).placeholder).toBe('https://environment-provider.invalid/v1')
    const key = fields.find(input => (input.element as HTMLInputElement).type === 'password')!
    await baseUrl.setValue('')
    await key.setValue('synthetic-environment-key')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      base_url: '',
      api_key: 'synthetic-environment-key',
      expected_base_url: 'https://environment-provider.invalid/v1',
    })

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/settings/ai', {
      base_url: '',
      api_key: 'synthetic-environment-key',
      expected_base_url: 'https://environment-provider.invalid/v1',
    })
  })

  it('uses the latest successful Save response environment root for blank-root key operations', async () => {
    const initial = {
      ...envSettings,
      base_url: 'https://saved-provider-b.invalid/v1',
      environment_base_url: 'https://environment-a.invalid/v1',
    }
    const rotated = {
      ...initial,
      environment_base_url: 'https://environment-c.invalid/v1',
    }
    api.get.mockResolvedValue(initial)
    api.put.mockResolvedValue(rotated)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    // A normal form submission accepts the server's rotated environment root.
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const inputs = wrapper.findAll('input')
    const baseUrl = inputs.find(input => (input.element as HTMLInputElement).value === initial.base_url)!
    const key = inputs.find(input => (input.element as HTMLInputElement).type === 'password')!
    expect((baseUrl.element as HTMLInputElement).placeholder).toBe(rotated.environment_base_url)

    await baseUrl.setValue('')
    await key.setValue('synthetic-key-for-c')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/settings/ai/test', {
      base_url: '',
      api_key: 'synthetic-key-for-c',
      expected_base_url: rotated.environment_base_url,
    })

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/ai', {
      base_url: '',
      api_key: 'synthetic-key-for-c',
      expected_base_url: rotated.environment_base_url,
    })
  })

  it('keeps a newer key edit made while Save is pending', async () => {
    api.get.mockResolvedValue(envSettings)
    let finishSave!: (value: typeof envSettings) => void
    api.put.mockReturnValue(new Promise(resolve => { finishSave = resolve }))
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const keyInput = wrapper.findAll('input').find(input => (input.element as HTMLInputElement).type === 'password')!
    await keyInput.setValue('first-synthetic-key')
    const submit = wrapper.find('form').trigger('submit')
    await flushPromises()
    await keyInput.setValue('newer-synthetic-key')
    finishSave(envSettings)
    await submit
    await flushPromises()

    expect((keyInput.element as HTMLInputElement).value).toBe('newer-synthetic-key')
  })

  it('invalidates a key draft when a pending Save changes its provider root', async () => {
    const rotated = {
      ...envSettings,
      base_url: 'https://provider-d.invalid/v1',
      environment_base_url: 'https://provider-d.invalid/v1',
    }
    api.get.mockResolvedValue(envSettings)
    let finishSave!: (value: typeof rotated) => void
    api.put.mockReturnValueOnce(new Promise(resolve => { finishSave = resolve }))
    api.put.mockResolvedValue(rotated)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const root = fields.find(input => (input.element as HTMLInputElement).type === 'text')!
    const key = fields.find(input => (input.element as HTMLInputElement).type === 'password')!
    const firstSave = wrapper.find('form').trigger('submit')
    await flushPromises()
    await key.setValue('synthetic-key-for-a')
    const model = fields.find(input => (input.element as HTMLInputElement).value === envSettings.model)!
    // Preserve unrelated edits made while the Save is pending.
    await model.setValue('unsaved-model-edit')
    finishSave(rotated)
    await firstSave
    await flushPromises()

    expect((root.element as HTMLInputElement).value).toBe(rotated.base_url)
    expect((key.element as HTMLInputElement).value).toBe('')
    expect((model.element as HTMLInputElement).value).toBe('unsaved-model-edit')
    expect(wrapper.text()).toContain('The provider address changed. Re-enter the API key for the current provider.')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      model: 'unsaved-model-edit',
    })

    // A whole-field replacement while the new root is displayed binds to it,
    // even if a later server refresh reports that the environment switched back.
    await key.setValue('synthetic-key-for-b')
    const replacementTest = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await replacementTest!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/settings/ai/test', {
      api_key: 'synthetic-key-for-b',
      expected_base_url: rotated.base_url,
      model: 'unsaved-model-edit',
    })

    // A canonical response that moves the fallback root back invalidates the B draft.
    api.put.mockResolvedValueOnce({ ...envSettings, base_url: '', environment_base_url: envSettings.environment_base_url })
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect((key.element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('The provider address changed. Re-enter the API key for the current provider.')
    await key.setValue('synthetic-key-for-a-replacement')
    const saveAgain = wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenLastCalledWith('/api/v1/settings/ai', {
      api_key: 'synthetic-key-for-a-replacement',
      expected_base_url: envSettings.environment_base_url,
    })
    await saveAgain
    await flushPromises()
  })

  it('rebinds a nonempty key draft when the user intentionally edits its provider root', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const root = fields.find(input => (input.element as HTMLInputElement).type === 'text')!
    const key = fields.find(input => (input.element as HTMLInputElement).type === 'password')!
    await key.setValue('synthetic-key-for-b')
    await root.setValue('https://provider-b.invalid/v1')
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      base_url: 'https://provider-b.invalid/v1',
      api_key: 'synthetic-key-for-b',
      expected_base_url: 'https://provider-b.invalid/v1',
    })
  })

  it('keeps a key draft when the user explicitly confirms the same provider root', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: true, multimodal: true, api_protocol: 'chat_completions', detail: 'ok',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const fields = wrapper.findAll('input')
    const root = fields.find(input => (input.element as HTMLInputElement).type === 'text')!
    const key = fields.find(input => (input.element as HTMLInputElement).type === 'password')!
    await key.setValue('synthetic-key-for-a')
    await root.setValue(envSettings.base_url)
    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    await testButton!.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {
      api_key: 'synthetic-key-for-a',
      expected_base_url: envSettings.base_url,
    })
  })

  it('does not call a rejected probe successful or claim vision is unavailable', async () => {
    api.get.mockResolvedValue(envSettings)
    api.post.mockResolvedValue({
      ok: false,
      multimodal: false,
      api_protocol: 'responses',
      detail: 'Probe request was rejected (HTTP 400). Selected protocol: responses.',
    })
    const wrapper = mount(AiSettingsForm, { global: { plugins: [i18n] } })
    await flushPromises()

    const testButton = wrapper.findAll('button').find(button => button.text() === 'Test Connection')
    expect(testButton).toBeDefined()
    await testButton!.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/v1/settings/ai/test', {})
    expect(wrapper.text()).toContain('Connected: No')
    expect(wrapper.text()).toContain('Selected protocol: responses')
    expect(wrapper.text()).not.toContain('Supports images')
    expect(wrapper.text()).not.toContain('model does NOT support image input')
  })
})
