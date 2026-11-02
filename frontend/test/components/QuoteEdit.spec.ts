import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), downloadBlob: vi.fn() }))
const quotes = vi.hoisted(() => ({
  fetchQuote: vi.fn(), fetchProductOptions: vi.fn(), calculatePreview: vi.fn(),
  createQuote: vi.fn(), updateQuote: vi.fn(), transitionStatus: vi.fn(),
  duplicateQuote: vi.fn(), convertQuote: vi.fn(), reactivateQuote: vi.fn(),
}))
const invoices = vi.hoisted(() => ({ fetchProductOptions: vi.fn() }))
const payments = vi.hoisted(() => ({ listQuotePayments: vi.fn(), recordQuotePayment: vi.fn() }))
const dialog = vi.hoisted(() => ({ warning: vi.fn() }))
const ui = vi.hoisted(() => ({
  box: { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /></div>' },
  button: { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /></button>' },
  input: {
    inheritAttrs: false, props: ['value', 'modelValue'], emits: ['update:value', 'update:modelValue'],
    template: '<input v-bind="$attrs" :value="value ?? modelValue" @input="$emit(\'update:value\', $event.target.value); $emit(\'update:modelValue\', $event.target.value)" />',
  },
  number: {
    inheritAttrs: false, props: ['value'], emits: ['update:value'],
    template: '<input v-bind="$attrs" :value="value ?? \'\'" @input="$emit(\'update:value\', Number($event.target.value))" />',
  },
  modal: {
    inheritAttrs: false, props: ['show'], emits: ['update:show', 'positive-click', 'negative-click'],
    template: '<section v-if="show" v-bind="$attrs"><slot /></section>',
  },
}))

vi.mock('../../src/api/http', () => ({ ...http, ApiError: class ApiError extends Error {} }))
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => quotes }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => invoices }))
vi.mock('../../src/stores/payments', () => ({ usePaymentsStore: () => payments }))
vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }), useDialog: () => dialog,
  NAlert: ui.box, NButton: ui.button, NCard: ui.box, NCollapse: ui.box, NCollapseItem: ui.box, NDatePicker: ui.box, NDivider: ui.box, NDropdown: ui.box,
  NForm: ui.box, NFormItem: ui.box, NGrid: ui.box, NGi: ui.box, NIcon: ui.box, NInput: ui.input, NInputNumber: ui.number,
  NList: ui.box, NListItem: ui.box, NModal: ui.modal, NSelect: ui.box, NSpace: ui.box, NSpin: ui.box, NSwitch: ui.box,
  NTag: ui.box, NText: ui.box, NThing: ui.box, NDescriptions: ui.box, NDescriptionsItem: ui.box,
  NEmpty: ui.box,
}))
vi.mock('@vicons/ionicons5', () => ({ AddOutline: ui.box, TrashOutline: ui.box, DocumentTextOutline: ui.box, DownloadOutline: ui.box, MailOutline: ui.box, EyeOutline: ui.box }))

import QuoteEdit from '../../src/views/quotes/QuoteEdit.vue'
import DocumentWorkflowPanel from '../../src/components/DocumentWorkflowPanel.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

const quote = {
  id: 'quote', quote_number: 'Q-1', status: 'ACCEPTED', customer_id: 'customer',
  quote_date: '2026-08-31', valid_until: '2026-09-30', tax_mode: 'LINE', amounts_include_vat: false,
  vat_treatment_id: null, document_vat_rate_id: null, discount_type: 'NONE', discount_value: '0',
  notes: null, warranty_text: null, terms_text: null, bank_text: null, payment_terms_text: null,
  currency: 'EUR', lines: [], converted_invoice_id: null,
}
const chain = {
  settlement_mode: 'UNSET', totals: {}, nodes: [], relations: [], events: [], timeline: [],
  available_actions: [
    { code: 'CONVERT_TO_INVOICE', available: true, target_id: 'quote', target_type: 'QUOTE' },
    { code: 'RECORD_QUOTE_PAYMENT', available: true, target_id: 'quote', target_type: 'QUOTE' },
    { code: 'CREATE_ADVANCE', available: true, target_id: 'quote', target_type: 'QUOTE' },
  ],
}

function byText(wrapper: ReturnType<typeof mount>, text: string) {
  const target = wrapper.findAll('button').find(item => item.text().includes(text))
  if (!target) throw new Error(`missing ${text}`)
  return target
}

describe('QuoteEdit UNSET mode cards', () => {
  function deferred<T>() {
    let resolve!: (value: T) => void
    let reject!: (cause: unknown) => void
    const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
    return { promise, resolve, reject }
  }

  it('keeps every billing mode card structured with a separate title, description, and action row', async () => {
    quotes.fetchQuote.mockResolvedValue(quote)
    quotes.convertQuote.mockResolvedValue({ id: 'quote-invoice' })
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve(chain)
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/quotes/:id/edit', component: QuoteEdit },
      { path: '/invoices/:id/edit', component: { template: '<div />' } },
    ] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()

    const cards = wrapper.findAll('.billing-mode-card')
    expect(cards).toHaveLength(3)
    expect(cards.map(card => card.find('.billing-mode-card-title').text())).toEqual([
      'Direct invoice', 'Receipt-only deposits', 'Formal advance and final',
    ])
    for (const card of cards) {
      expect(card.find('.billing-mode-card-description').exists()).toBe(true)
      expect(card.find('.billing-mode-card-actions').exists()).toBe(true)
      expect(card.find('.billing-mode-card-actions button').attributes('disabled')).toBeUndefined()
    }

    await cards[0].find('.billing-mode-card-actions button').trigger('click')
    await flushPromises()
    expect(quotes.convertQuote).toHaveBeenCalledWith('quote')
    expect(router.currentRoute.value.fullPath).toBe('/invoices/quote-invoice/edit')
  })

  it('uses the actual Formal continuation controls for signal, calculate/create and failure', async () => {
    quotes.fetchQuote.mockResolvedValue(quote)
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve(chain)
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      QuotePaymentPanel: { template: '<div data-receipt />' },
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await byText(wrapper, 'Create advance').trigger('click')
    await flushPromises()
    const workflow = wrapper.findComponent(DocumentWorkflowPanel)
    expect(workflow.exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    http.post.mockResolvedValueOnce({ taxable_amount: '50', vat_total: '10.50', gross_amount: '60.50', buckets: [] })
      .mockResolvedValueOnce({ id: 'advance-1' })
    const raw = workflow.findAll('input').find(input => input.attributes('value') === '')
    if (!raw) throw new Error('missing Advance raw input')
    await raw.setValue('50')
    const calculate = workflow.findAll('button').find(button => button.text().includes('Calculate'))
    if (!calculate) throw new Error('missing Advance Calculate button')
    await calculate.trigger('click')
    expect(http.post).toHaveBeenCalledWith('/api/v1/quotes/quote/advance-invoices/calculate', { input_mode: 'GROSS_AMOUNT', gross_amount: '50' })
    const create = workflow.findAll('button').find(button => button.text().includes('Create draft'))
    if (!create) throw new Error('missing Advance Create button')
    await create.trigger('click')
    expect(http.post).toHaveBeenCalledWith('/api/v1/quotes/quote/advance-invoices', {
      input_mode: 'GROSS_AMOUNT', gross_amount: '50', invoice_date: expect.any(String), due_date: null, supply_or_advance_date: null, reference_number: null,
    })
    await router.push('/quotes/quote/edit'); await flushPromises()
    await byText(wrapper, 'Create advance').trigger('click'); await flushPromises()
    const failedWorkflow = wrapper.findComponent(DocumentWorkflowPanel)
    const failedRaw = failedWorkflow.findAll('input').find(input => input.attributes('value') === '')
    if (!failedRaw) throw new Error('missing failed Advance raw input')
    await failedRaw.setValue('51')
    http.post.mockRejectedValueOnce(new Error('network'))
    const failedCalculate = failedWorkflow.findAll('button').find(button => button.text().includes('Calculate'))
    if (!failedCalculate) throw new Error('missing failed Advance Calculate button')
    await failedCalculate.trigger('click')
    expect(failedWorkflow.text()).toContain('The operation could not be completed')
    // This is the actual parent continuation control with the real workflow
    // child still mounted; it must return the UNSET quote to all three cards.
    await byText(wrapper, 'Cancel').trigger('click'); await flushPromises()
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)
    expect(wrapper.text()).toContain('Direct invoice')
    expect(wrapper.text()).toContain('Record deposit')
    expect(wrapper.text()).toContain('Create advance')
  })

  it('uses the actual Receipt continuation to record a deposit and remains locked after the mode changes', async () => {
    const refreshedChain = deferred<typeof chain>()
    let chainCalls = 0
    quotes.fetchQuote.mockResolvedValue(quote)
    invoices.fetchProductOptions.mockResolvedValue([])
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100.00', paid_total: '0.00', remaining_amount: '100.00', converted_invoice_id: null })
    payments.recordQuotePayment.mockResolvedValue({ items: [{ id: 'payment-1', amount: '12.5' }], total_incl_vat: '100.00', paid_total: '12.50', remaining_amount: '87.50', converted_invoice_id: null })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') {
        chainCalls += 1
        return chainCalls === 1 ? Promise.resolve(chain) : refreshedChain.promise
      }
      if (url === '/api/v1/payment-methods') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await byText(wrapper, 'Record deposit').trigger('click'); await flushPromises()
    const deposit = wrapper.findComponent({ name: 'QuotePaymentPanel' })
    expect(deposit.exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    await byText(deposit as unknown as ReturnType<typeof mount>, 'Record deposit').trigger('click')
    const amount = deposit.findAll('input').find(input => input.attributes('value') === '')
    if (!amount) throw new Error('missing deposit amount input')
    await amount.setValue('12.5')
    await byText(deposit as unknown as ReturnType<typeof mount>, 'Record').trigger('click')
    expect(payments.recordQuotePayment).toHaveBeenCalledWith('quote', expect.objectContaining({ amount: 12.5, payment_method_id: null, reference: null, note: null }))
    // The payment mutation itself has committed the backend mode lock.  While
    // its independent chain read is pending, the stale UNSET projection must
    // not reopen an alternate path.
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Cancel')).toHaveLength(0)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('Create advance')
    refreshedChain.resolve({ ...chain, settlement_mode: 'RECEIPT_ONLY' })
    await flushPromises()
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Cancel')).toHaveLength(0)
  })

  it('keeps the real Receipt payment mutation successful when its parent chain refresh rejects, then retries the current chain once', async () => {
    let chainCalls = 0
    const receiptChain = {
      settlement_mode: 'RECEIPT_ONLY',
      totals: { due_amount: '90.00', refund_due_amount: '0.00', credit_total: '0.00', incoming_payment_total: '10.00' },
      nodes: [{ id: 'invoice-r', node_type: 'INVOICE', document_kind: 'STANDARD', number: 'INV-R', occurred_on: '2026-08-31', charge_amount: '100.00', credit_amount: '0.00', due_amount: '90.00', refund_due_amount: '0.00', incoming_payment_amount: '10.00', refund_amount: '0.00' }],
      relations: [{ from_node_id: 'quote', to_node_id: 'invoice-r', relation_type: 'QUOTE_TO_INVOICE' }],
      events: [{ id: 'event-r', event_type: 'QUOTE_PAYMENT_CREATED', occurred_at: '2026-08-31T10:00:00Z', event_order: 1 }],
      timeline: [
        { kind: 'NODE', order: 1, node: { id: 'invoice-r', node_type: 'INVOICE', document_kind: 'STANDARD', number: 'INV-R', occurred_on: '2026-08-31', charge_amount: '100.00', credit_amount: '0.00', due_amount: '90.00', refund_due_amount: '0.00', incoming_payment_amount: '10.00', refund_amount: '0.00' } },
        { kind: 'RELATION', order: 2, relation: { from_node_id: 'quote', to_node_id: 'invoice-r', relation_type: 'QUOTE_TO_INVOICE' } },
        { kind: 'EVENT', order: 3, event: { id: 'event-r', event_type: 'QUOTE_PAYMENT_CREATED', occurred_at: '2026-08-31T10:00:00Z', event_order: 1 } },
      ],
      available_actions: [
        { code: 'RECORD_QUOTE_PAYMENT', available: true, target_id: 'quote', target_type: 'QUOTE' },
        { code: 'CREATE_ADVANCE', available: false, reason_code: 'FORMAL_CHAIN_REQUIRED', target_id: 'quote', target_type: 'QUOTE' },
      ],
    }
    quotes.fetchQuote.mockResolvedValue(quote)
    invoices.fetchProductOptions.mockResolvedValue([])
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100.00', paid_total: '10.00', remaining_amount: '90.00', converted_invoice_id: null })
    payments.recordQuotePayment.mockClear()
    payments.recordQuotePayment.mockResolvedValue({ items: [{ id: 'payment-r', amount: '12.50' }], total_incl_vat: '100.00', paid_total: '22.50', remaining_amount: '77.50', converted_invoice_id: null })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') {
        chainCalls += 1
        if (chainCalls === 2) return Promise.reject(new Error('chain refresh unavailable'))
        if (chainCalls === 1) return Promise.resolve(chain)
        return Promise.resolve({ ...receiptChain, totals: { ...receiptChain.totals, due_amount: '77.50' } })
      }
      if (url === '/api/v1/payment-methods') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(chainCalls).toBe(1)
    await byText(wrapper, 'Record deposit').trigger('click')
    const paymentPanel = wrapper.findComponent({ name: 'QuotePaymentPanel' })
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record deposit').trigger('click')
    const amount = paymentPanel.findAll('input').find(input => input.attributes('value') === '')
    if (!amount) throw new Error('missing receipt amount input')
    await amount.setValue('12.5')
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record').trigger('click')
    await flushPromises()
    expect(payments.recordQuotePayment).toHaveBeenCalledTimes(1)
    expect(chainCalls).toBe(2)
    expect(wrapper.text()).toContain('payment was saved, but the document chain below may be out of date')
    expect(wrapper.text()).not.toContain('Could not record payment')
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Cancel')).toHaveLength(0)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('Create advance')
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(chainCalls).toBe(3)
    expect(wrapper.text()).toContain('77.50')
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(true)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Cancel')).toHaveLength(0)
  })

  it('allows cancelling an UNSET Receipt selection when its first payment fails', async () => {
    quotes.fetchQuote.mockResolvedValue(quote)
    invoices.fetchProductOptions.mockResolvedValue([])
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100.00', paid_total: '0.00', remaining_amount: '100.00', converted_invoice_id: null })
    payments.recordQuotePayment.mockRejectedValue(new Error('payment unavailable'))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve(chain)
      if (url === '/api/v1/payment-methods') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await byText(wrapper, 'Record deposit').trigger('click'); await flushPromises()
    const paymentPanel = wrapper.findComponent({ name: 'QuotePaymentPanel' })
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record deposit').trigger('click')
    const amount = paymentPanel.findAll('input').find(input => input.attributes('value') === '')
    if (!amount) throw new Error('missing receipt amount input')
    await amount.setValue('12.5')
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record').trigger('click')
    await flushPromises()
    // The page continuation Back remains available because no backend lock
    // committed by the failed mutation.
    await byText(wrapper, 'Cancel').trigger('click'); await flushPromises()
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(true)
    expect(wrapper.text()).toContain('Direct invoice')
    expect(wrapper.text()).toContain('Create advance')
  })

  it('clears a committed Receipt mode marker before a reused route loads another UNSET quote', async () => {
    const paymentRefresh = deferred<typeof chain>()
    const quoteB = { ...quote, id: 'quote-b', quote_number: 'Q-B', customer_id: 'customer-b' }
    let quoteAChainCalls = 0
    quotes.fetchQuote.mockImplementation((id: string) => Promise.resolve(id === 'quote' ? quote : quoteB))
    invoices.fetchProductOptions.mockResolvedValue([])
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100.00', paid_total: '0.00', remaining_amount: '100.00', converted_invoice_id: null })
    payments.recordQuotePayment.mockResolvedValue({ items: [{ id: 'payment-a', amount: '12.50' }], total_incl_vat: '100.00', paid_total: '12.50', remaining_amount: '87.50', converted_invoice_id: null })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') {
        quoteAChainCalls += 1
        return quoteAChainCalls === 1 ? Promise.resolve(chain) : paymentRefresh.promise
      }
      if (url === '/api/v1/quotes/quote-b/document-chain') return Promise.resolve(chain)
      if (url === '/api/v1/payment-methods') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer A' })
      if (url === '/api/v1/customers/customer-b') return Promise.resolve({ id: 'customer-b', name: 'Customer B' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await byText(wrapper, 'Record deposit').trigger('click'); await flushPromises()
    const paymentPanel = wrapper.findComponent({ name: 'QuotePaymentPanel' })
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record deposit').trigger('click')
    const amount = paymentPanel.findAll('input').find(input => input.attributes('value') === '')
    if (!amount) throw new Error('missing receipt amount input')
    await amount.setValue('12.5')
    await byText(paymentPanel as unknown as ReturnType<typeof mount>, 'Record').trigger('click')
    await router.push('/quotes/quote-b/edit'); await flushPromises()
    expect(wrapper.text()).toContain('Q-B')
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(false)
    paymentRefresh.resolve({ ...chain, settlement_mode: 'RECEIPT_ONLY' })
    await flushPromises()
    expect(wrapper.text()).toContain('Q-B')
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(true)
  })

  it.each(['success', 'reject'] as const)('reused A → B route keeps only B workflow, payment and error state when B %s', async (bOutcome) => {
    const bQuote = { ...quote, id: 'quote-b', quote_number: 'Q-B', customer_id: 'customer-b' }
    const bFetch = deferred<typeof quote>()
    const aRefresh = deferred<typeof chain>()
    quotes.fetchQuote.mockImplementation((id: string) => id === 'quote' ? Promise.resolve(quote) : bFetch.promise)
    invoices.fetchProductOptions.mockResolvedValue([])
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100', paid_total: '0', remaining_amount: '100', converted_invoice_id: null })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return aRefresh.promise
      if (url === '/api/v1/quotes/quote-b/document-chain') return Promise.resolve({ ...chain, settlement_mode: 'RECEIPT_ONLY' })
      if (url === '/api/v1/payment-methods') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer A' })
      if (url === '/api/v1/customers/customer-b') return Promise.resolve({ id: 'customer-b', name: 'Customer B' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    // A's form is populated before its authoritative chain settles.
    await flushPromises()
    expect(wrapper.text()).toContain('Q-1')
    await router.push('/quotes/quote-b/edit'); await flushPromises()
    expect(wrapper.text()).not.toContain('Q-1')
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)
    // Late A success/rejection is ignored while B's quote fetch owns the page.
    aRefresh.resolve({ ...chain, settlement_mode: 'FORMAL_ADVANCE' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Q-1')
    if (bOutcome === 'success') bFetch.resolve(bQuote)
    else bFetch.reject(new Error('B fetch failed'))
    await flushPromises()
    if (bOutcome === 'success') {
      expect(wrapper.text()).toContain('Q-B')
      expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(true)
      expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(true)
      expect(wrapper.text()).not.toContain('Q-1')
    } else {
      expect(wrapper.text()).toContain('B fetch failed')
      expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)
    }
  })

  it('mounts exactly the selected direct, receipt, or formal continuation', async () => {
    quotes.fetchQuote.mockResolvedValue(quote)
    quotes.convertQuote.mockResolvedValue({ id: 'converted' })
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve(chain)
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      QuotePaymentPanel: { template: '<div data-receipt />' },
      DocumentWorkflowPanel: { props: ['openAdvanceSignal'], template: '<div data-formal :data-signal="openAdvanceSignal" />' },
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.text()).toContain('Direct invoice')
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(1)
    expect(wrapper.find('[data-receipt]').exists()).toBe(false)
    expect(wrapper.find('[data-formal]').exists()).toBe(false)
    await byText(wrapper, 'Record deposit').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-receipt]').exists()).toBe(true)
    expect(wrapper.find('[data-formal]').exists()).toBe(false)
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    await byText(wrapper, 'Cancel').trigger('click'); await flushPromises()
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(1)
    await byText(wrapper, 'Create advance').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-receipt]').exists()).toBe(false)
    expect(wrapper.find('[data-formal]').attributes('data-signal')).toBe('1')
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    await byText(wrapper, 'Cancel').trigger('click'); await flushPromises()
    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(1)
    await byText(wrapper, 'Convert to Invoice').trigger('click'); await flushPromises()
    expect(quotes.convertQuote).toHaveBeenCalledWith('quote')
  })

  it.each(['SENT', 'EXPIRED'] as const)('uses the sole projected Direct action for %s UNSET quotes', async (status) => {
    quotes.fetchQuote.mockResolvedValue({ ...quote, status })
    quotes.convertQuote.mockClear()
    quotes.convertQuote.mockResolvedValue({ id: `${status.toLowerCase()}-invoice` })
    invoices.fetchProductOptions.mockResolvedValue([])
    const directOnlyChain = {
      ...chain,
      available_actions: [
        { code: 'CONVERT_TO_INVOICE', available: true, target_id: 'quote', target_type: 'QUOTE' },
        { code: 'RECORD_QUOTE_PAYMENT', available: false, reason_code: 'QUOTE_NOT_ACCEPTED', target_id: 'quote', target_type: 'QUOTE' },
        { code: 'CREATE_ADVANCE', available: false, reason_code: 'QUOTE_NOT_ACCEPTED', target_id: 'quote', target_type: 'QUOTE' },
      ],
    }
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve(directOnlyChain)
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/quotes/:id/edit', component: QuoteEdit },
      { path: '/invoices/:id/edit', component: { template: '<div />' } },
    ] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()

    const directActions = wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')
    expect(directActions).toHaveLength(1)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)

    await directActions[0].trigger('click'); await flushPromises()
    expect(quotes.convertQuote).toHaveBeenCalledTimes(1)
    expect(quotes.convertQuote).toHaveBeenCalledWith('quote')
    expect(router.currentRoute.value.fullPath).toBe(`/invoices/${status.toLowerCase()}-invoice/edit`)
  })

  it.each([
    ['SENT', 'unavailable', { code: 'CONVERT_TO_INVOICE', available: false, reason_code: 'CONVERSION_UNAVAILABLE', target_id: 'quote', target_type: 'QUOTE' }],
    ['EXPIRED', 'another Quote target', { code: 'CONVERT_TO_INVOICE', available: true, target_id: 'another-quote', target_type: 'QUOTE' }],
    ['SENT', 'non-Quote target type', { code: 'CONVERT_TO_INVOICE', available: true, target_id: 'quote', target_type: 'INVOICE' }],
    ['EXPIRED', 'non-Quote target type', { code: 'CONVERT_TO_INVOICE', available: true, target_id: 'quote', target_type: 'INVOICE' }],
  ] as const)('does not invent a Direct action when the %s projection has %s', async (status, _caseName, convertAction) => {
    quotes.fetchQuote.mockResolvedValue({ ...quote, status })
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return Promise.resolve({
        ...chain,
        available_actions: [
          convertAction,
          { code: 'RECORD_QUOTE_PAYMENT', available: true, target_id: 'another-quote', target_type: 'QUOTE' },
          { code: 'CREATE_ADVANCE', available: true, target_id: 'another-quote', target_type: 'QUOTE' },
        ],
      })
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()

    expect(wrapper.findAll('button').filter(button => button.text() === 'Convert to Invoice')).toHaveLength(0)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)
  })

  it.each([
    ['DIRECT_INVOICE', false, true, false],
    ['RECEIPT_ONLY', true, true, false],
    ['FORMAL_ADVANCE', false, true, false],
    ['DIRECT_INVOICE', false, true, true],
  ])('renders only the legal locked continuation for %s', async (mode, receipt, formal, converted) => {
    quotes.fetchQuote.mockResolvedValue({ ...quote, converted_invoice_id: converted ? 'invoice' : null })
    invoices.fetchProductOptions.mockResolvedValue([])
    let chainCalls = 0
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') {
        chainCalls += 1
        return Promise.resolve({
        ...chain,
        settlement_mode: mode,
        available_actions: chain.available_actions.map(action => action.code === 'CONVERT_TO_INVOICE'
          ? { ...action, available: mode === 'DIRECT_INVOICE' && !converted }
          : action),
        })
      }
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    payments.listQuotePayments.mockResolvedValue({ items: [], total_incl_vat: '100.00', paid_total: '0.00', remaining_amount: '100.00', converted_invoice_id: null })
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    // Quote page owns the initial projection read.  The real workflow child
    // may load its own artifacts but must not issue a duplicate chain GET.
    expect(chainCalls).toBe(1)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(receipt)
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(formal)
    expect(wrapper.text().includes('Convert to Invoice')).toBe(mode === 'DIRECT_INVOICE' && !converted)
    expect(wrapper.text().includes('Create advance')).toBe(mode === 'FORMAL_ADVANCE')
    if (mode === 'FORMAL_ADVANCE') {
      expect(wrapper.find('.workflow-modal').exists()).toBe(false)
      expect(wrapper.findComponent({ name: 'QuotePaymentPanel' }).exists()).toBe(false)
    }
  })

  it('drops a late A rejection, shows only B chain failure, and retries B with the real timeline panel', async () => {
    const aChain = deferred<typeof chain>()
    let bAttempts = 0
    quotes.fetchQuote.mockImplementation((id: string) => Promise.resolve(id === 'quote'
      ? quote
      : { ...quote, id: 'quote-b', quote_number: 'Q-B', customer_id: 'customer-b' }))
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/quotes/quote/document-chain') return aChain.promise
      if (url === '/api/v1/quotes/quote-b/document-chain') {
        bAttempts += 1
        if (bAttempts === 1) return Promise.reject(new Error('B chain unavailable'))
        return Promise.resolve({
          ...chain,
          settlement_mode: 'DIRECT_INVOICE',
          totals: { due_amount: '5.00', refund_due_amount: '0.00', credit_total: '0.00', incoming_payment_total: '0.00' },
          nodes: [{ id: 'invoice-B', node_type: 'INVOICE', document_kind: 'STANDARD', number: 'INV-B', occurred_on: '2026-08-31', charge_amount: '10.00', credit_amount: '0.00', due_amount: '5.00', refund_due_amount: '0.00', incoming_payment_amount: '5.00', refund_amount: '0.00' }],
          relations: [{ from_node_id: 'quote-b', to_node_id: 'invoice-B', relation_type: 'QUOTE_TO_INVOICE' }],
          events: [{ event_type: 'MODE_LOCKED', occurred_at: '2026-08-31T10:00:00Z' }],
          timeline: [
            { kind: 'NODE', order: 1, node: { id: 'invoice-B', node_type: 'INVOICE', document_kind: 'STANDARD', number: 'INV-B', occurred_on: '2026-08-31', charge_amount: '10.00', credit_amount: '0.00', due_amount: '5.00', refund_due_amount: '0.00', incoming_payment_amount: '5.00', refund_amount: '0.00' } },
            { kind: 'RELATION', order: 2, relation: { from_node_id: 'quote-b', to_node_id: 'invoice-B', relation_type: 'QUOTE_TO_INVOICE' } },
            { kind: 'EVENT', order: 3, event: { event_type: 'MODE_LOCKED', occurred_at: '2026-08-31T10:00:00Z' } },
          ],
        })
      }
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer A' })
      if (url === '/api/v1/customers/customer-b') return Promise.resolve({ id: 'customer-b', name: 'Customer B' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes/:id/edit', component: QuoteEdit }] })
    await router.push('/quotes/quote/edit'); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      QuotePaymentPanel: { template: '<div data-receipt />' },
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await router.push('/quotes/quote-b/edit'); await flushPromises()
    aChain.reject(new Error('late A chain failure'))
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load the document chain')
    expect(wrapper.text()).not.toContain('late A chain failure')
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(false)
    expect(wrapper.find('.billing-mode-cards').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Direct invoice')
    expect(wrapper.text()).not.toContain('Record deposit')
    expect(wrapper.text()).not.toContain('Create advance')
    expect(bAttempts).toBe(1)
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(bAttempts).toBe(2)
    expect(wrapper.findComponent(DocumentWorkflowPanel).exists()).toBe(true)
    expect(wrapper.text()).toContain('INV-B')
    expect(wrapper.text()).toContain('Billing mode locked')
    expect(wrapper.text()).toContain('Quote converted to invoice')
  })
})

describe('Quote duplication', () => {
  async function setup(status = 'DRAFT', id = 'quote') {
    vi.clearAllMocks()
    quotes.fetchQuote.mockResolvedValue({ ...quote, status, lines: [{
      id: 'line', name: 'Original', quantity: '1', unit_price: '10',
      discount_type: 'NONE', discount_value: '0', vat_rate_id: 'rate',
    }] })
    invoices.fetchProductOptions.mockResolvedValue([])
    http.get.mockImplementation((url: string) => {
      if (url.includes('/document-chain')) return Promise.resolve(chain)
      if (url.startsWith('/api/v1/customers?q=') || url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/settings/quote-default-valid-days') return Promise.resolve({ default_valid_days: 30 })
      if (url.startsWith('/api/v1/document-templates') || url.startsWith('/api/v1/content-blocks') || url.startsWith('/api/v1/note-templates')) return Promise.resolve([])
      if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
      throw new Error(`unexpected ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/quotes/:id/edit', component: QuoteEdit },
    ] })
    await router.push(`/quotes/${id}/edit`); await router.isReady()
    const wrapper = mount(QuoteEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
      DocumentWorkflowPanel: true, QuotePaymentPanel: true,
    } } })
    await flushPromises()
    return { wrapper, router }
  }

  it('duplicates accepted saved quotes, blocks repeat clicks, and navigates', async () => {
    const { wrapper, router } = await setup('ACCEPTED')
    let finish!: (value: { id: string }) => void
    quotes.duplicateQuote.mockReturnValue(new Promise(resolve => { finish = resolve }))
    await byText(wrapper, 'Duplicate').trigger('click')
    await byText(wrapper, 'Duplicate').trigger('click')
    expect(quotes.duplicateQuote).toHaveBeenCalledTimes(1)
    expect(quotes.duplicateQuote).toHaveBeenCalledWith('quote')
    expect(byText(wrapper, 'Duplicate').attributes('disabled')).toBeDefined()
    finish({ id: 'copy' }); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/quotes/copy/edit')
    wrapper.unmount()
  })

  it('guards unsaved edits with cancel or save-and-continue', async () => {
    const { wrapper } = await setup()
    await wrapper.find('input[value="Original"]').setValue('Changed')
    await byText(wrapper, 'Duplicate').trigger('click')
    expect(dialog.warning).toHaveBeenCalledTimes(1)
    dialog.warning.mock.calls[0][0].onNegativeClick(); await flushPromises()
    expect(quotes.duplicateQuote).not.toHaveBeenCalled()
    await byText(wrapper, 'Duplicate').trigger('click')
    quotes.updateQuote.mockResolvedValue({ ...quote, status: 'DRAFT' })
    quotes.duplicateQuote.mockResolvedValue({ id: 'copy' })
    await dialog.warning.mock.calls[1][0].onPositiveClick(); await flushPromises()
    expect(quotes.updateQuote.mock.calls[0][1].lines[0].name).toBe('Changed')
    expect(quotes.duplicateQuote).toHaveBeenCalledWith('quote')
    wrapper.unmount()
  })

  it('blocks copying after save failure and permits retry after duplicate failure', async () => {
    const { wrapper, router } = await setup()
    await wrapper.find('input[value="Original"]').setValue('Changed')
    await byText(wrapper, 'Duplicate').trigger('click')
    quotes.updateQuote.mockRejectedValueOnce(new Error('save failed'))
    await dialog.warning.mock.calls[0][0].onPositiveClick(); await flushPromises()
    expect(quotes.duplicateQuote).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/quotes/quote/edit')
    await byText(wrapper, 'Duplicate').trigger('click')
    quotes.updateQuote.mockResolvedValue({ ...quote, status: 'DRAFT' })
    quotes.duplicateQuote.mockRejectedValueOnce(new Error('copy failed'))
    await dialog.warning.mock.calls[1][0].onPositiveClick(); await flushPromises()
    expect(byText(wrapper, 'Duplicate').attributes('disabled')).toBeUndefined()
    expect(router.currentRoute.value.path).toBe('/quotes/quote/edit')
    quotes.duplicateQuote.mockResolvedValue({ id: 'copy' })
    await byText(wrapper, 'Duplicate').trigger('click'); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/quotes/copy/edit')
    wrapper.unmount()
  })

  it('hides Duplicate on a new unsaved quote', async () => {
    const { wrapper } = await setup('DRAFT', 'new')
    expect(wrapper.findAll('button').some(button => button.text().includes('Duplicate'))).toBe(false)
    wrapper.unmount()
  })
})
