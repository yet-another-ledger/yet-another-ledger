import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'

const store = vi.hoisted(() => ({ listInvoicePayments: vi.fn(), recordPayment: vi.fn(), updatePayment: vi.fn(), deletePayment: vi.fn() }))
const api = vi.hoisted(() => ({ get: vi.fn(), downloadBlob: vi.fn() }))
const ui = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
vi.mock('../../src/stores/payments', () => ({ usePaymentsStore: () => store }))
vi.mock('../../src/api/http', () => ({ ...api, ApiError: class ApiError extends Error { status = 500 } }))
vi.mock('naive-ui', () => {
  const passthrough = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="header-extra" /></div>' }
  const button = { inheritAttrs: false, props: ['loading', 'disabled', 'text', 'type'], template: '<button v-bind="$attrs" :disabled="disabled || loading" @click="$emit(\'click\', $event)"><slot /></button>', emits: ['click'] }
  return {
    useMessage: () => ui,
    useDialog: () => ({ warning: vi.fn() }),
    NCard: { ...passthrough, props: ['title'], template: '<section><header>{{ title }}<slot name="header-extra" /></header><slot /></section>' },
    NButton: button,
    NAlert: passthrough, NSpace: passthrough, NTag: passthrough, NText: passthrough,
    NForm: passthrough, NFormItem: passthrough, NInputNumber: passthrough, NSelect: passthrough,
    NInput: passthrough, NDatePicker: passthrough, NSpin: passthrough, NDivider: passthrough,
    NIcon: passthrough, NEmpty: passthrough, NDropdown: passthrough,
  }
})
vi.mock('../../src/components/PdfPreviewDialog.vue', () => ({ default: { template: '<div />' } }))
vi.mock('../../src/components/DocumentSendDialog.vue', () => ({ default: { template: '<div />' } }))
vi.mock('../../src/components/PaymentTaxBreakdown.vue', () => ({ default: { template: '<div />' } }))

import InvoicePaymentPanel from '../../src/components/InvoicePaymentPanel.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
function aggregate(due: string, id: string) {
  return { invoice_id: id, paid_status: 'UNPAID', status: 'SENT', paid_total: '0.00', due_amount: due, items: [] }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function mountPanel(invoiceId = 'A', invoiceVersion = 'v1') {
  return mount(InvoicePaymentPanel, {
    props: { invoiceId, invoiceVersion, invoiceStatus: 'SENT' },
    global: { plugins: [i18n] },
  })
}

describe('InvoicePaymentPanel revision projection refresh', () => {
  it('reloads the authoritative backend aggregate for a same-id invoice version change', async () => {
    store.listInvoicePayments.mockReset().mockResolvedValueOnce(aggregate('121.00', 'A')).mockResolvedValueOnce(aggregate('242.00', 'A'))
    api.get.mockReset().mockResolvedValue({ items: [] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('Due: 121.00')

    await wrapper.setProps({ invoiceVersion: 'v2' })
    await flushPromises()

    expect(store.listInvoicePayments).toHaveBeenCalledTimes(2)
    expect(store.listInvoicePayments).toHaveBeenLastCalledWith('A')
    expect(wrapper.text()).toContain('Due: 242.00')
  })

  it('retains the last aggregate on refresh failure and recovers through its retry control', async () => {
    store.listInvoicePayments.mockReset().mockResolvedValueOnce(aggregate('121.00', 'A'))
      .mockRejectedValueOnce(new Error('temporary read failure'))
      .mockResolvedValueOnce(aggregate('242.00', 'A'))
    api.get.mockReset().mockResolvedValue({ items: [] })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.setProps({ invoiceVersion: 'v2' })
    await flushPromises()

    expect(wrapper.text()).toContain('Could not refresh the current payment balance')
    expect(wrapper.text()).toContain('Due: 121.00')
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(store.listInvoicePayments).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).not.toContain('Could not refresh the current payment balance')
    expect(wrapper.text()).toContain('Due: 242.00')
  })

  it('ignores a late response for invoice A after the panel has moved to invoice B', async () => {
    const requestA = deferred<ReturnType<typeof aggregate>>()
    const requestB = deferred<ReturnType<typeof aggregate>>()
    store.listInvoicePayments.mockReset().mockReturnValueOnce(requestA.promise).mockReturnValueOnce(requestB.promise)
    api.get.mockReset().mockResolvedValue({ items: [] })
    const wrapper = mountPanel('A', 'v1')
    await flushPromises()
    await wrapper.setProps({ invoiceId: 'B', invoiceVersion: 'v2' })
    await flushPromises()

    requestB.resolve(aggregate('55.00', 'B'))
    await flushPromises()
    requestA.resolve(aggregate('121.00', 'A'))
    await flushPromises()

    expect(store.listInvoicePayments.mock.calls.map(([id]) => id)).toEqual(['A', 'B'])
    expect(wrapper.text()).toContain('Due: 55.00')
    expect(wrapper.text()).not.toContain('Due: 121.00')
    expect((wrapper.vm as any).panelLoading).toBe(false)
  })
})
