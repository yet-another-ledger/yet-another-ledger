import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('../../src/api/http', () => http)
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
import SmtpSettingsForm from '../../src/components/settings/SmtpSettingsForm.vue'
const box = { template: '<div><slot /><slot name="feedback" /></div>' }
const control = { props: ['value'], emits: ['update:value'], template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />' }
const toggle = { props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" />' }
function form() {
  return mount(SmtpSettingsForm, { global: { stubs: {
    'n-spin': box, 'n-form': { template: '<form><slot /></form>' }, 'n-form-item': box,
    'n-input': control, 'n-input-number': control, 'n-switch': toggle, 'n-checkbox': toggle,
    'n-radio-group': control, 'n-radio': box, 'n-space': box, 'n-alert': box,
    'n-button': { template: '<button><slot /></button>' },
  } } })
}
beforeEach(() => {
  vi.clearAllMocks()
  http.get.mockResolvedValue({ host: 'smtp.test', from_email: 'from@example.com', password_set: true,
    sent_copy: { enabled: true, host: 'imap.test', port: 1993, security: 'SSL_TLS', folder: 'My Sent' } })
  http.put.mockResolvedValue({ password_set: true })
})
it('loads saved IMAP fields, preserves entered port on security change and sends no second credentials', async () => {
  const wrapper = form(); await flushPromises()
  const vm = wrapper.vm as unknown as { sentCopy: {security: string; port: number}; handleSave: () => Promise<void> }
  vm.sentCopy.security = 'STARTTLS'
  expect(vm.sentCopy.port).toBe(1993)
  await vm.handleSave()
  expect(http.put).toHaveBeenCalledWith('/api/v1/settings/smtp', expect.objectContaining({
    password: null, sent_copy: { enabled: true, host: 'imap.test', port: 1993, security: 'STARTTLS', folder: 'My Sent' },
  }))
  expect(Object.keys(http.put.mock.calls[0][1].sent_copy).sort()).toEqual(['enabled', 'folder', 'host', 'port', 'security'])
  wrapper.unmount()
})
it('reports SMTP success with a copy warning without retrying', async () => {
  http.post.mockResolvedValue({ status: 'sent', sent_copy_status: 'FAILED', sent_copy_error_code: 'IMAP_AUTH_FAILED' })
  const wrapper = form(); await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === 'settings.smtp.testEmail')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('settings.smtp.testSuccess sentCopy.FAILED')
  expect(http.post).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})
it('shows load and test failures', async () => {
  http.get.mockRejectedValue(new Error('unavailable'))
  const wrapper = form(); await flushPromises()
  expect(wrapper.text()).toContain('settings.smtp.loadFailed')
  http.post.mockRejectedValue(new Error('SMTP_SEND_FAILED'))
  await wrapper.findAll('button').find(button => button.text() === 'settings.smtp.testEmail')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('SMTP_SEND_FAILED')
  wrapper.unmount()
})
