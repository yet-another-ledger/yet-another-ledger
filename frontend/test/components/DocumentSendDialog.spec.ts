import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
const ui = vi.hoisted(() => ({
  passthrough: {
    inheritAttrs: false,
    template: '<div v-bind="$attrs"><slot /><slot name="footer" /></div>',
  },
  button: {
    inheritAttrs: false,
    template: '<button v-bind="$attrs"><slot /></button>',
  },
  input: {
    inheritAttrs: false,
    props: ['value'],
    emits: ['update:value'],
    template: '<input v-bind="$attrs" :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
  },
  select: {
    inheritAttrs: false,
    props: ['value', 'options'],
    emits: ['update:value'],
    template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>',
  },
}))

vi.mock('../../src/api/http', () => ({ ...http }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('naive-ui', () => ({
  useMessage: () => messages,
  NAlert: ui.passthrough,
  NButton: ui.button,
  NForm: ui.passthrough,
  NFormItem: ui.passthrough,
  NInput: ui.input,
  NModal: ui.passthrough,
  NSelect: ui.select,
  NSpace: ui.passthrough,
  NSpin: ui.passthrough,
}))

import DocumentSendDialog from '../../src/components/DocumentSendDialog.vue'

const templates = {
  invoice: { en: { subject: 'invoice en', body: 'invoice body' }, zh: { subject: 'invoice zh', body: 'invoice body zh' } },
  quote: { en: { subject: 'quote en', body: 'quote body' }, zh: { subject: 'quote zh', body: 'quote body zh' } },
  advance: { en: { subject: 'advance en', body: 'advance body' }, zh: { subject: 'advance zh', body: 'advance body zh' } },
  final: { en: { subject: 'final en', body: 'final body' }, zh: { subject: 'final zh', body: 'final body zh' } },
  credit_note: { en: { subject: 'credit en', body: 'credit body' }, zh: { subject: 'credit zh', body: 'credit body zh' } },
  refund: { en: { subject: 'refund en', body: 'refund body' }, zh: { subject: 'refund zh', body: 'refund body zh' } },
}

describe('DocumentSendDialog Refund flow', () => {
  it('uses the mounted form to send an explicit locale override to the Refund endpoint and emits its artifact-bearing result', async () => {
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/settings/email-templates') return Promise.resolve(templates)
      if (url === '/api/v1/settings/document-defaults') return Promise.resolve({ locale: 'en' })
      throw new Error(`unexpected GET ${url}`)
    })
    const sent = {
      id: 'log-1', related_type: 'REFUND', related_id: 'refund-1', to_email: 'snapshot@example.test',
      cc: null, subject: 'refund zh', body_snapshot: 'refund body zh', attachment_filename: 'refund.pdf',
      artifact_id: 'artifact-1', locale: 'zh', status: 'SENT', error_message: null, creator_id: null,
      created_at: '2026-08-31T10:00:00Z', sent_at: '2026-08-31T10:00:01Z',
    }
    http.post.mockResolvedValue(sent)

    const wrapper = mount(DocumentSendDialog, {
      props: {
        show: true,
        docType: 'refund',
        docId: 'refund-1',
        customerEmail: 'snapshot@example.test',
        customerLocale: 'en',
      },
      global: {},
    })
    await flushPromises()

    const inputs = wrapper.findAll('input')
    expect((inputs[0].element as HTMLInputElement).value).toBe('snapshot@example.test')
    expect((wrapper.find('select').element as HTMLSelectElement).value).toBe('en')

    await wrapper.find('select').setValue('zh')
    await wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!.trigger('click')

    expect(http.post).toHaveBeenCalledWith(
      '/api/v1/payments/refund-1/send-refund-confirmation',
      { to: 'snapshot@example.test', cc: null, locale: 'zh' },
    )
    expect(wrapper.emitted('sent')?.[0]).toEqual([sent])
    expect(wrapper.emitted('update:show')?.[0]).toEqual([false])
    expect(messages.success).toHaveBeenCalled()
  })

  it('omits locale when the Refund snapshot locale is left untouched', async () => {
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/settings/email-templates') return Promise.resolve(templates)
      if (url === '/api/v1/settings/document-defaults') return Promise.resolve({ locale: 'en' })
      throw new Error(`unexpected GET ${url}`)
    })
    http.post.mockResolvedValue({ id: 'log-2' })
    const wrapper = mount(DocumentSendDialog, {
      props: { show: true, docType: 'refund', docId: 'refund-2', customerEmail: 'snapshot@example.test', customerLocale: 'zh' },
    })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!.trigger('click')
    expect(http.post).toHaveBeenCalledWith(
      '/api/v1/payments/refund-2/send-refund-confirmation',
      { to: 'snapshot@example.test', cc: null },
    )
  })

  it('keeps a missing customer email empty and enforces the local recipient requirement', async () => {
    http.post.mockClear()
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/settings/email-templates') return Promise.resolve(templates)
      if (url === '/api/v1/settings/document-defaults') return Promise.resolve({ locale: 'en' })
      throw new Error(`unexpected GET ${url}`)
    })
    const wrapper = mount(DocumentSendDialog, {
      props: { show: true, docType: 'invoice', docId: 'invoice-1', customerEmail: null, customerLocale: null },
    })
    await flushPromises()

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('')
    await wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!.trigger('click')
    expect(messages.warning).toHaveBeenCalledWith('sendDialog.toRequired')
    expect(http.post).not.toHaveBeenCalled()
  })
})

it('closes after SMTP success and warns separately when saving the Sent copy fails', async () => {
  vi.clearAllMocks()
  http.get.mockImplementation((url: string) => Promise.resolve(url.includes('email-templates') ? templates : { locale: 'en' }))
  http.post.mockResolvedValue({ id: 'log-copy-failed', status: 'SENT', sent_copy_status: 'FAILED', sent_copy_error_code: 'IMAP_TIMEOUT' })
  const wrapper = mount(DocumentSendDialog, { props: {
    show: true, docType: 'quote', docId: 'quote-1', customerEmail: 'to@example.test', customerLocale: 'en',
  } })
  await flushPromises()
  await wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!.trigger('click')
  await flushPromises()
  expect(http.post).toHaveBeenCalledTimes(1)
  expect(messages.warning).toHaveBeenCalledWith('sentCopy.FAILED')
  expect(messages.success).not.toHaveBeenCalled()
  expect(wrapper.emitted('sent')?.[0]?.[0]).toMatchObject({ status: 'SENT', sent_copy_status: 'FAILED' })
  expect(wrapper.emitted('update:show')?.at(-1)).toEqual([false])
  wrapper.unmount()
})

it.each(['FAILED', 'SENT'])('keeps locking and replacement context protection for a late %s result', async (status) => {
  vi.clearAllMocks()
  http.get.mockImplementation((url: string) => Promise.resolve(url.includes('email-templates') ? templates : { locale: 'en' }))
  let complete!: (value: unknown) => void
  http.post.mockReturnValue(new Promise(resolve => { complete = resolve }))
  const wrapper = mount(DocumentSendDialog, { props: {
    show: true, docType: 'quote', docId: 'quote-old', customerEmail: 'to@example.test', customerLocale: 'en',
  } })
  await flushPromises()
  const send = wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!
  await send.trigger('click')
  await send.trigger('click')
  expect(http.post).toHaveBeenCalledTimes(1)
  await wrapper.setProps({ docId: 'quote-new', customerEmail: 'new@example.test' })
  complete({ status, error_message: status === 'FAILED' ? 'SMTP_SEND_FAILED' : null, sent_copy_status: 'NOT_ATTEMPTED' })
  await flushPromises()
  expect(wrapper.emitted('update:show')).toBeUndefined()
  expect((wrapper.find('input').element as HTMLInputElement).value).toBe('new@example.test')
  expect(wrapper.text()).not.toContain('sendDialog.sendFailed')
  expect(wrapper.emitted('update:sending')?.at(-1)).toEqual([false])
  wrapper.unmount()
})

it('shows SMTP failure from HTTP 200 without emitting sent or closing', async () => {
  vi.clearAllMocks()
  http.get.mockImplementation((url: string) => Promise.resolve(url.includes('email-templates') ? templates : { locale: 'en' }))
  http.post.mockResolvedValue({ status: 'FAILED', error_message: 'SMTP_SEND_FAILED', sent_copy_status: 'NOT_ATTEMPTED' })
  const wrapper = mount(DocumentSendDialog, { props: {
    show: true, docType: 'quote', docId: 'quote-1', customerEmail: 'to@example.test', customerLocale: 'en',
  } })
  await flushPromises()
  await wrapper.findAll('button').find(item => item.text() === 'sendDialog.send')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('sendDialog.sendFailed')
  expect(messages.success).not.toHaveBeenCalled()
  expect(messages.warning).not.toHaveBeenCalled()
  expect(wrapper.emitted('sent')).toBeUndefined()
  expect(wrapper.emitted('update:show')).toBeUndefined()
  expect(wrapper.emitted('update:sending')?.at(-1)).toEqual([false])
  wrapper.unmount()
})
