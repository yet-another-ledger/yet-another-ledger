import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const http = vi.hoisted(() => ({ get: vi.fn(), downloadBlob: vi.fn() }))
const ApiErrorMock = vi.hoisted(() => class extends Error {
  status: number
  constructor(status: number, _detail: unknown, message: string) { super(message); this.status = status }
})
const store = vi.hoisted(() => ({
  fetchInvoice: vi.fn(), fetchProductOptions: vi.fn(), calculatePreview: vi.fn(),
  createInvoice: vi.fn(), updateInvoice: vi.fn(), transitionStatus: vi.fn(),
  calculateRevision: vi.fn(), saveRevision: vi.fn(), fetchRevisions: vi.fn(),
}))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
const dialogs = vi.hoisted(() => ({ warning: vi.fn() }))
const ui = vi.hoisted(() => ({
  box: { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="icon" /><slot name="footer" /></div>' },
  button: { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /><slot name="icon" /></button>' },
  input: { inheritAttrs: false, props: ['value'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" @input="$emit(\'update:value\', $event.target.value)" />' },
  select: { inheritAttrs: false, props: ['value', 'options'], emits: ['update:value', 'search'], template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="option in options ?? []" :key="option.value" :value="option.value">{{ option.label }}</option></select>' },
}))

vi.mock('../../src/api/http', () => ({ ...http, ApiError: ApiErrorMock }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => store }))
vi.mock('naive-ui', () => ({
  useMessage: () => messages,
  NAlert: ui.box, NButton: ui.button, NCard: ui.box, NCollapse: ui.box, NCollapseItem: ui.box, NDatePicker: ui.input, NDivider: ui.box, NDropdown: ui.box,
  NForm: ui.box, NFormItem: ui.box, NGrid: ui.box, NGi: ui.box, NIcon: ui.box, NInput: ui.input, NInputNumber: ui.input,
  NList: ui.box, NListItem: ui.box, NModal: ui.box, NSpace: ui.box, NSelect: ui.select, NSpin: ui.box, NSwitch: ui.input,
  NTag: ui.box, NText: ui.box, NThing: ui.box, NEmpty: ui.box,
  NCheckbox: { inheritAttrs: false, props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" v-bind="$attrs" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" />' },
  useDialog: () => dialogs,
}))
vi.mock('@vicons/ionicons5', () => ({ AddOutline: ui.box, TrashOutline: ui.box, DocumentTextOutline: ui.box, DownloadOutline: ui.box, MailOutline: ui.box, EyeOutline: ui.box }))

import InvoiceEdit from '../../src/views/invoices/InvoiceEdit.vue'
import { ApiError } from '../../src/api/http'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

function invoice(id: string, kind: 'STANDARD' | 'CREDIT_NOTE') {
  return {
    id,
    invoice_number: `INV-${id}`,
    document_kind: kind,
    status: 'DRAFT', paid_status: 'UNPAID', customer_id: `customer-${id}`,
    reference_number: `reference-${id}`, invoice_date: '2026-08-31', due_date: '2026-09-30',
    supply_or_advance_date: null, updated_at: '2026-08-31T10:00:00Z', revision_eligibility: { available: false, reason_code: 'wrong_state' },
    tax_mode: 'LINE', amounts_include_vat: false, vat_treatment_id: `treatment-${id}`,
    document_vat_rate_id: null, discount_type: 'NONE', discount_value: '0', notes: `notes-${id}`,
    warranty_text: null, terms_text: null, bank_text: null, payment_terms_text: null,
    currency: 'EUR', due_amount: '10.00', total_incl_vat: '10.00', lines: [{
      id: `line-${id}`, product_id: null, name: `line-${id}`, description: null, quantity: '1', unit_id: null, unit_name: null,
      unit_price: '10', discount_type: 'NONE', discount_value: '0', vat_rate_id: null,
    }],
  }
}

describe('InvoiceEdit reused route', () => {
  function deferred<T>() {
    let resolve!: (value: T) => void
    let reject!: (cause: unknown) => void
    const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
    return { promise, resolve, reject }
  }

  it.each(['success', 'error'] as const)('isolates an old A eligibility response through an unkeyed A→B→A route reuse (%s)', async outcome => {
    const oldA = deferred<any>()
    const currentA = deferred<any>()
    const a = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const b = { ...invoice('B', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    let aFetches = 0
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => {
      if (id === 'B') return Promise.resolve(b)
      aFetches += 1
      if (aFetches === 1 || aFetches === 3) return Promise.resolve(a)
      return aFetches === 2 ? oldA.promise : currentA.promise
    })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.includes('/document-chain')) return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url.startsWith('/api/v1/customers/')) return Promise.resolve({ id: url.split('/').at(-1), name: 'Customer' })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit')
    await router.isReady()
    const wrapper = mount(defineComponent({ template: '<router-view />' }), { global: { plugins: [router, i18n], stubs: {
      InvoicePaymentPanel: true, DocumentWorkflowPanel: true, DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
    } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    expect(wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.attributes('disabled')).toBeDefined()

    await router.push('/invoices/B/edit'); await flushPromises()
    expect(wrapper.text()).toContain('INV-B')
    await router.push('/invoices/A/edit'); await flushPromises()
    expect(wrapper.text()).toContain('INV-A')
    const revise = () => wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!
    await revise().trigger('click')
    await flushPromises()
    expect(revise().attributes('disabled')).toBeDefined()

    if (outcome === 'success') oldA.resolve(a)
    else oldA.reject(new Error('stale A eligibility failure'))
    await flushPromises()
    expect(revise().attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('stale A eligibility failure')
    expect(wrapper.text()).not.toContain('Reason for revision')

    currentA.resolve(a)
    await flushPromises()
    expect((wrapper.findComponent(InvoiceEdit).vm.$.setupState as any).revisionMode).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text().includes('Revise invoice'))).toBe(false)
    wrapper.unmount()
  })

  it.each([
    ['invoice fetch', 'success', 'success'], ['invoice fetch', 'success', 'reject'],
    ['invoice fetch', 'reject', 'success'], ['invoice fetch', 'reject', 'reject'],
    ['document chain', 'success', 'success'], ['document chain', 'success', 'reject'],
    ['document chain', 'reject', 'success'], ['document chain', 'reject', 'reject'],
    ['customer', 'success', 'success'], ['customer', 'success', 'reject'],
    ['customer', 'reject', 'success'], ['customer', 'reject', 'reject'],
  ] as const)('keeps a reused B page authoritative when A %s completes late (%s) and B currently %s', async (lateTarget, lateOutcome, bOutcome) => {
    const late = deferred<any>()
    const bFetch = deferred<any>()
    const a = { ...invoice('A', 'STANDARD'), status: 'SENT' }
    const b = { ...invoice('B', 'STANDARD'), status: 'SENT' }
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => {
      if (id === 'B') return bFetch.promise
      return lateTarget === 'invoice fetch' ? late.promise : Promise.resolve(a)
    })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/invoices/A/document-chain') return lateTarget === 'document chain' ? late.promise : Promise.resolve({ quote_id: 'quote-A', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return lateTarget === 'customer' ? late.promise : Promise.resolve({ id: 'customer-A', name: 'Customer A' })
      if (url === '/api/v1/invoices/B/document-chain') return Promise.resolve({ quote_id: 'quote-B', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-B') return Promise.resolve({ id: 'customer-B', name: 'Customer B' })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentWorkflowPanel: { props: ['invoice', 'documentChain'], template: '<div data-workflow :data-invoice="invoice.id" :data-chain="documentChain?.quote_id ?? \'pending\'" />' },
      InvoicePaymentPanel: { props: ['invoiceId'], template: '<div data-payment :data-invoice="invoiceId" />' },
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    if (lateTarget !== 'invoice fetch') expect(wrapper.text()).toContain('INV-A')

    await router.push('/invoices/B/edit'); await flushPromises()
    // B is pending: route reset removes A's form, customer, chain, child
    // actions/payment contract, error, and page-owned loading inheritance.
    expect(wrapper.text()).not.toContain('INV-A')
    expect(wrapper.text()).not.toContain('Customer A')
    expect(wrapper.find('[data-workflow]').exists()).toBe(false)
    expect(wrapper.find('[data-payment]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('A late failure')

    if (lateOutcome === 'success') late.resolve(lateTarget === 'customer'
      ? { id: 'customer-A', name: 'Late Customer A' }
      : { ...a, quote_id: 'quote-A', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
    else late.reject(new Error('A late failure'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('INV-A')
    expect(wrapper.text()).not.toContain('Late Customer A')
    expect(wrapper.text()).not.toContain('A late failure')
    expect(wrapper.find('[data-workflow]').exists()).toBe(false)

    if (bOutcome === 'success') bFetch.resolve(b)
    else bFetch.reject(new Error('B current failure'))
    await flushPromises()
    if (bOutcome === 'success') {
      expect(wrapper.text()).toContain('INV-B')
      expect(wrapper.text()).toContain('Customer B')
      expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-chain': 'quote-B' })
      expect(wrapper.find('[data-payment]').attributes()).toMatchObject({ 'data-invoice': 'B' })
      expect(wrapper.text()).not.toContain('A late failure')
    } else {
      expect(wrapper.text()).toContain('B current failure')
      expect(wrapper.find('[data-workflow]').exists()).toBe(false)
      expect(wrapper.find('[data-payment]').exists()).toBe(false)
    }
  })

  it('keeps B customer options, error, chain and loading state current when A customer completes late', async () => {
    let resolveACustomer!: (value: { id: string; name: string }) => void
    let resolveBChain!: (value: { quote_id: string | null; nodes: never[]; relations: never[]; events: never[]; totals: Record<string, never>; available_actions: never[] }) => void
    let rejectBCustomer!: (cause: unknown) => void
    const aCustomer = new Promise<{ id: string; name: string }>(resolve => { resolveACustomer = resolve })
    const bCustomer = new Promise<{ id: string; name: string }>((_resolve, reject) => { rejectBCustomer = reject })
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve(invoice(id, 'STANDARD')))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/invoices/A/document-chain') return Promise.resolve({ quote_id: 'quote-A', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return aCustomer
      if (url === '/api/v1/invoices/B/document-chain') return new Promise(resolve => { resolveBChain = resolve })
      if (url === '/api/v1/customers/customer-B') return bCustomer
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentWorkflowPanel: { props: ['invoice', 'documentChain'], template: '<div data-workflow :data-invoice="invoice.id" :data-chain="documentChain?.quote_id ?? \'none\'" />' },
      InvoicePaymentPanel: { template: '<div />' }, DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'A', 'data-chain': 'quote-A' })

    await router.push('/invoices/B/edit'); await flushPromises()
    expect(wrapper.find('[data-workflow]').exists()).toBe(false)
    resolveACustomer({ id: 'customer-A', name: 'Late A' })
    await flushPromises()
    expect(wrapper.html()).not.toContain('Late A')
    resolveBChain({ quote_id: 'quote-B', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-chain': 'quote-B' })
    rejectBCustomer(new Error('B customer failed'))
    await flushPromises()
    expect(wrapper.text()).toContain('B customer failed')
    expect(wrapper.text()).not.toContain('Late A')
  })

  it('resets A state and keeps B authoritative when A returns late, including child workflow/payment contracts', async () => {
    let resolveAChain!: (value: { quote_id: string; nodes: never[]; relations: never[]; events: never[]; totals: Record<string, never>; available_actions: never[] }) => void
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve(invoice(id, id === 'A' ? 'STANDARD' : 'CREDIT_NOTE')))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/customers/customer-A') return Promise.resolve({ id: 'customer-A', name: 'Customer A', email: 'a@example.test' })
      if (url === '/api/v1/customers/customer-B') return Promise.resolve({ id: 'customer-B', name: 'Customer B', email: 'b@example.test' })
      if (url === '/api/v1/invoices/A/document-chain') return new Promise(resolve => { resolveAChain = resolve })
      if (url === '/api/v1/invoices/B/document-chain') return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit')
    await router.isReady()
    const wrapper = mount(InvoiceEdit, {
      global: {
        plugins: [router, i18n],
        stubs: {
          DocumentWorkflowPanel: { props: ['invoice', 'documentChain'], template: '<div data-workflow :data-invoice="invoice.id" :data-customer="invoice.customer_id" :data-chain="documentChain?.quote_id ?? \'none\'" />' },
          InvoicePaymentPanel: { props: ['invoiceId'], template: '<div data-payment :data-invoice="invoiceId" />' },
          DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
        },
      },
    })
    await flushPromises()
    expect(store.fetchInvoice).toHaveBeenCalledWith('A')
    expect(wrapper.find('[data-workflow]').exists()).toBe(false)
    expect(wrapper.find('[data-payment]').attributes()).toMatchObject({ 'data-invoice': 'A' })
    expect(wrapper.text()).toContain('INV-A')

    await router.push('/invoices/B/edit')
    await flushPromises()
    resolveAChain({ quote_id: 'quote-A', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
    await flushPromises()

    expect(store.fetchInvoice).toHaveBeenCalledWith('B')
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-customer': 'customer-B', 'data-chain': 'none' })
    expect(wrapper.find('[data-payment]').exists()).toBe(false)
    expect(wrapper.text()).toContain('INV-B')
    expect(wrapper.html()).not.toContain('reference-A')
    expect(wrapper.html()).not.toContain('line-A')
  })

  it('shows a current chain-read error without fake workflow actions and retries the invoice endpoint', async () => {
    let chainAttempts = 0
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockResolvedValue(invoice('B', 'STANDARD'))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/customers/customer-B') return Promise.resolve({ id: 'customer-B', name: 'Customer B' })
      if (url === '/api/v1/invoices/B/document-chain') {
        chainAttempts += 1
        return chainAttempts === 1
          ? Promise.reject(new Error('current chain unavailable'))
          : Promise.resolve({ quote_id: 'quote-B', nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      }
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/B/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentWorkflowPanel: { props: ['invoice', 'documentChain'], template: '<div data-workflow :data-invoice="invoice.id" :data-chain="documentChain.quote_id" />' },
      InvoicePaymentPanel: { template: '<div />' }, DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load the document chain')
    expect(wrapper.find('[data-workflow]').exists()).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(chainAttempts).toBe(2)
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-chain': 'quote-B' })
  })

  it('retains the last successful invoice chain and warns, then retries, after a payment refresh failure', async () => {
    let chainAttempts = 0
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockResolvedValue(invoice('B', 'STANDARD'))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/customers/customer-B') return Promise.resolve({ id: 'customer-B', name: 'Customer B' })
      if (url === '/api/v1/invoices/B/document-chain') {
        chainAttempts += 1
        if (chainAttempts === 2) return Promise.reject(new Error('refresh unavailable'))
        return Promise.resolve({ quote_id: `quote-B-${chainAttempts}`, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      }
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/B/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentWorkflowPanel: { props: ['documentChain'], template: '<div data-workflow :data-chain="documentChain.quote_id" />' },
      InvoicePaymentPanel: { emits: ['paymentsChanged'], template: '<button data-mutate @click="$emit(\'paymentsChanged\', { paid_status: \'PARTIALLY_PAID\', status: \'SENT\', due_amount: \'5.00\' })">mutate</button>' },
      DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes('data-chain')).toBe('quote-B-1')
    await wrapper.find('[data-mutate]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('payment was saved, but the document chain below may be out of date')
    expect(wrapper.find('[data-workflow]').attributes('data-chain')).toBe('quote-B-1')
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click'); await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes('data-chain')).toBe('quote-B-3')
  })

  it('passes an existing invoice customer email to the mounted send dialog', async () => {
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockResolvedValue({ ...invoice('A', 'STANDARD'), status: 'SENT', party_snapshot_locale: 'zh' })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/invoices/A/document-chain') return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return Promise.resolve({ id: 'customer-A', name: 'Customer A', email: 'a@example.test' })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      InvoicePaymentPanel: { template: '<div />' }, DocumentWorkflowPanel: { template: '<div />' },
      DocumentSendDialog: { props: ['customerEmail', 'customerLocale'], template: '<div data-send :data-email="customerEmail ?? \'null\'" :data-locale="customerLocale" />' },
      PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()

    expect(wrapper.find('[data-send]').attributes()).toMatchObject({
      'data-email': 'a@example.test', 'data-locale': 'zh',
    })
  })

  it('updates the send-dialog customer email when the existing customer arrives after the initial search', async () => {
    let resolveCustomer!: (customer: { id: string; name: string; email: string }) => void
    const lateCustomer = new Promise<{ id: string; name: string; email: string }>(resolve => { resolveCustomer = resolve })
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockResolvedValue({ ...invoice('A', 'STANDARD'), status: 'SENT' })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/invoices/A/document-chain') return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return lateCustomer
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      InvoicePaymentPanel: { template: '<div />' }, DocumentWorkflowPanel: { template: '<div />' },
      DocumentSendDialog: { props: ['customerEmail'], template: '<div data-send :data-email="customerEmail ?? \'null\'" />' },
      PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.find('[data-send]').attributes('data-email')).toBe('null')

    resolveCustomer({ id: 'customer-A', name: 'Customer A', email: 'late@example.test' })
    await flushPromises()
    expect(wrapper.find('[data-send]').attributes('data-email')).toBe('late@example.test')
  })

  it('keeps the send-dialog recipient scoped to the current invoice on a reused A-to-B route', async () => {
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve({ ...invoice(id, 'STANDARD'), status: 'SENT' }))
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/invoices/A/document-chain' || url === '/api/v1/invoices/B/document-chain') return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return Promise.resolve({ id: 'customer-A', name: 'Customer A', email: 'a@example.test' })
      if (url === '/api/v1/customers/customer-B') return Promise.resolve({ id: 'customer-B', name: 'Customer B', email: null })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      InvoicePaymentPanel: { template: '<div />' }, DocumentWorkflowPanel: { template: '<div />' },
      DocumentSendDialog: { props: ['customerEmail'], template: '<div data-send :data-email="customerEmail ?? \'null\'" />' },
      PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    expect(wrapper.find('[data-send]').attributes('data-email')).toBe('a@example.test')

    await router.push('/invoices/B/edit'); await flushPromises()
    expect(wrapper.find('[data-send]').attributes('data-email')).toBe('null')
  })
})

describe('InvoiceEdit revision workflow', () => {
  function deferred<T>() {
    let resolve!: (value: T) => void
    let reject!: (error: unknown) => void
    const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
    return { promise, resolve, reject }
  }

  function mountDetail(inv: any, blocks: any[] = []) {
    for (const mock of Object.values(store)) mock.mockClear()
    dialogs.warning.mockClear()
    messages.success.mockClear()
    messages.error.mockClear()
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockResolvedValue(inv)
    store.fetchRevisions.mockResolvedValue({ items: [], total: 0 })
    store.calculatePreview.mockResolvedValue({ lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' })
    store.calculateRevision.mockResolvedValue({ lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' })
    store.saveRevision.mockResolvedValue(inv)
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/content-blocks') return Promise.resolve(blocks)
      if (url === '/api/v1/document-templates' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.includes('/document-chain')) return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url.startsWith('/api/v1/customers/')) return Promise.resolve({ id: inv.customer_id, name: 'Customer' })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    return { router, ready: async () => {
      await router.push(`/invoices/${inv.id}/edit`); await router.isReady()
      const wrapper = mount(defineComponent({ template: '<router-view v-slot="{ Component, route }"><component :is="Component" :key="route.fullPath" /></router-view>' }), { global: { plugins: [router, i18n], stubs: {
        InvoicePaymentPanel: { template: '<div />' }, DocumentWorkflowPanel: { props: ['documentChain'], template: '<div data-workflow :data-due="documentChain?.totals?.due_amount ?? \'none\'" />' },
        DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
      } } })
      await flushPromises()
      return wrapper
    } }
  }

  it('ignores a late post-revision chain response from invoice A after the page moves to B', async () => {
    const lateRevisionChain = deferred<any>()
    const invA = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const invB = { ...invoice('B', 'STANDARD'), status: 'SENT' }
    const revisedA = { ...invA, notes: 'Saved A', updated_at: '2026-08-31T11:00:00Z' }
    let aChainReads = 0
    for (const mock of Object.values(store)) mock.mockClear()
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve(id === 'A' ? invA : invB))
    store.fetchRevisions.mockResolvedValue({ items: [], total: 0 })
    store.saveRevision.mockResolvedValue(revisedA)
    store.calculateRevision.mockResolvedValue({ lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' })
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/document-templates' || url === '/api/v1/content-blocks' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/customers/customer-A' || url === '/api/v1/customers/customer-B') return Promise.resolve({ id: url.endsWith('A') ? 'customer-A' : 'customer-B', name: 'Customer' })
      if (url === '/api/v1/invoices/A/document-chain') {
        aChainReads += 1
        return aChainReads === 1
          ? Promise.resolve({ quote_id: 'quote-A-v1', nodes: [], relations: [], events: [], totals: { due_amount: '121.00' }, available_actions: [] })
          : lateRevisionChain.promise
      }
      if (url === '/api/v1/invoices/B/document-chain') return Promise.resolve({ quote_id: 'quote-B', nodes: [], relations: [], events: [], totals: { due_amount: '55.00' }, available_actions: [] })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit'); await router.isReady()
    const wrapper = mount(InvoiceEdit, { global: { plugins: [router, i18n], stubs: {
      DocumentWorkflowPanel: { props: ['invoice', 'documentChain'], template: '<div data-workflow :data-invoice="invoice.id" :data-chain="documentChain?.quote_id ?? \'pending\'" />' },
      InvoicePaymentPanel: { template: '<div />' }, DocumentSendDialog: { template: '<div />' }, PdfPreviewDialog: { template: '<div />' }, EmailLogPanel: { template: '<div />' },
    } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'notes-A')!.setValue('Saved A')
    await wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox').at(-1)!.setValue('Correction A')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(aChainReads).toBe(2)

    await router.push('/invoices/B/edit'); await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-chain': 'quote-B' })
    lateRevisionChain.resolve({ quote_id: 'quote-A-revision', nodes: [], relations: [], events: [], totals: { due_amount: '242.00' }, available_actions: [] })
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes()).toMatchObject({ 'data-invoice': 'B', 'data-chain': 'quote-B' })
    expect(wrapper.text()).toContain('INV-B')
    expect(wrapper.text()).not.toContain('INV-A')
  })

  it('gates revision by backend eligibility, locks customer, and sends explicit clears and retained line IDs with reason and confirmation', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    store.saveRevision.mockResolvedValue({ ...inv, notes: null, lines: [{ ...inv.lines[0], name: 'Revised line' }] })
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    expect(wrapper.text()).toContain('Revise invoice')
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    expect(store.fetchInvoice).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('select')[0]!.attributes('disabled')).toBeDefined()

    const lineName = wrapper.findAll('input').find(input => input.element.value === 'line-A')!
    await lineName.setValue('Revised line')
    const notes = wrapper.findAll('input').find(input => input.element.value === 'notes-A')!
    await notes.setValue('')
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('Corrected description')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()

    expect(store.saveRevision).toHaveBeenCalledTimes(1)
    const [, payload] = store.saveRevision.mock.calls[0]!
    expect(payload).toMatchObject({ reason: 'Corrected description', confirm_not_delivered: true, expected_updated_at: inv.updated_at })
    expect(payload.changes).toMatchObject({ notes: null, lines: [{ line_id: 'line-A', name: 'Revised line' }] })
  })

  it('keeps revision history visible after completion and renders unsafe history values as text', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'COMPLETED', revision_eligibility: { available: false, reason_code: 'has_payment' } }
    const unsafe = '<img src=x onerror=alert(1)>'
    const { ready } = mountDetail(inv)
    store.fetchRevisions.mockResolvedValue({ items: [{
      id: 'rev-1', invoice_id: 'A', revision_order: 1, actor_user_id: unsafe,
      created_at: '2026-10-01T10:00:00Z', reason: unsafe, undelivered_confirmed: true,
      before_snapshot: { invoice: { ...inv, lines: inv.lines }, credit_basis: [] },
      after_snapshot: { invoice: { ...inv, notes: unsafe, lines: inv.lines }, credit_basis: [] },
    }], total: 1 })
    const wrapper = await ready()
    expect(wrapper.find('[title="Revision history"]').exists()).toBe(true)
    expect(wrapper.text()).toContain(unsafe)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.findAll('button').some(button => button.text().includes('Revise invoice'))).toBe(false)
    expect(wrapper.text()).toContain('Reason for revision')
  })

  it('renders historical and confirmation VAT labels from each frozen side, never the current dictionaries', async () => {
    const base = invoice('A', 'STANDARD')
    const old = {
      ...base, status: 'SENT', tax_mode: 'DOCUMENT', document_vat_rate_id: 'rate-old',
      vat_treatment_id: 'treatment-old', vat_treatment_snapshot: { id: 'treatment-old', code: 'OLD-CODE', label: 'Frozen treatment old' },
      document_taxes: [{ vat_rate_id: 'rate-old', vat_rate_label: 'Frozen old', vat_rate_percent: '21', effective_vat_percent: '21', taxable_amount: '100', tax_amount: '21' }],
      revision_eligibility: { available: true, reason_code: null },
    }
    const before = {
      ...base, tax_mode: 'DOCUMENT', document_vat_rate_id: 'rate-old',
      vat_treatment_id: 'treatment-old', vat_treatment_code: 'OLD-CODE', vat_treatment_label: 'Frozen treatment old',
      taxes: [{ vat_rate_id: 'rate-old', vat_rate_label: 'Frozen old', vat_rate_percent: '21', effective_vat_percent: '21', taxable_amount: '100', tax_amount: '21' }],
    }
    const after = {
      ...before, document_vat_rate_id: 'rate-new', vat_treatment_id: 'treatment-new',
      vat_treatment_code: 'NEW-CODE', vat_treatment_label: 'Frozen treatment new',
      taxes: [{ vat_rate_id: 'rate-new', vat_rate_label: 'Frozen new', vat_rate_percent: '9', effective_vat_percent: '9', taxable_amount: '100', tax_amount: '9' }],
    }
    const { ready } = mountDetail(old)
    http.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates') return Promise.resolve({ items: [
        { id: 'rate-old', label: 'Current old', percent: '6' }, { id: 'rate-new', label: 'Current new', percent: '0' },
      ] })
      if (url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [
        { id: 'treatment-old', code: 'CURRENT-OLD', label: 'Current treatment old' },
        { id: 'treatment-new', code: 'CURRENT-NEW', label: 'Current treatment new' },
      ] })
      if (url === '/api/v1/content-blocks' || url === '/api/v1/document-templates' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.includes('/document-chain')) return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url.startsWith('/api/v1/customers/')) return Promise.resolve({ id: old.customer_id, name: 'Customer' })
      throw new Error(`unexpected GET ${url}`)
    })
    store.fetchRevisions.mockResolvedValue({ items: [{
      id: 'rev-tax', invoice_id: 'A', revision_order: 1, actor_user_id: 'user-1', created_at: '2026-10-01T10:00:00Z',
      reason: 'tax correction', undelivered_confirmed: true,
      before_snapshot: { invoice: before, credit_basis: [] }, after_snapshot: { invoice: after, credit_basis: [] },
    }], total: 1 })
    const wrapper = await ready()
    const historicalRows = wrapper.findAll('.revision-change').map(row => row.text())
    expect(historicalRows).toContain('VAT treatment: OLD-CODE – Frozen treatment old → NEW-CODE – Frozen treatment new')
    expect(historicalRows).toContain('Document VAT rate: Frozen old (21%) → Frozen new (9%)')

    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    store.calculateRevision.mockResolvedValue({
      lines: [], subtotal_excl_vat: '100', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '100',
      vat_total: '9', total_incl_vat: '109', document_taxes: after.taxes,
      vat_treatment_snapshot: { id: 'treatment-new', code: 'NEW-CODE', label: 'Frozen treatment new' },
    })
    state.documentVatRateId = 'rate-new'
    state.vatTreatmentId = 'treatment-new'
    await new Promise(resolve => setTimeout(resolve, 700))
    await flushPromises()
    const summaryRateRow = wrapper.findAll('.revision-change').map(row => row.text()).find(row => row.startsWith('Document VAT rate:'))
    expect(summaryRateRow).toBe('Document VAT rate: Frozen old (21%) → Frozen new (9%)')
    const summaryTreatmentRow = wrapper.findAll('.revision-change').map(row => row.text()).find(row => row.startsWith('VAT treatment:'))
    expect(summaryTreatmentRow).toBe('VAT treatment: OLD-CODE – Frozen treatment old → NEW-CODE – Frozen treatment new')
  })

  it('retries a failed history load and displays the recovered result', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'COMPLETED', revision_eligibility: { available: false, reason_code: 'has_payment' } }
    const { ready } = mountDetail(inv)
    store.fetchRevisions.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ items: [], total: 0 })
    const wrapper = await ready()
    expect(wrapper.text()).toContain('Could not load revision history.')
    const retry = wrapper.findAll('button').find(button => button.text().includes('Retry'))!
    await retry.trigger('click')
    await flushPromises()
    expect(store.fetchRevisions).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[description="No revisions have been recorded."]').exists()).toBe(true)
  })

  it('saves consecutive revisions with the latest version and reloads newest-first history after each save', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const updated1 = { ...inv, notes: 'First change', updated_at: '2026-08-31T11:00:00Z' }
    const updated2 = { ...updated1, reference_number: 'reference-2', updated_at: '2026-08-31T12:00:00Z' }
    const revision = (order: number, reason: string) => ({
      id: `rev-${order}`, invoice_id: 'A', revision_order: order, actor_user_id: 'user-1',
      created_at: `2026-10-0${order}T10:00:00Z`, reason, undelivered_confirmed: true,
      before_snapshot: { invoice: { ...inv, notes: null }, credit_basis: [] },
      after_snapshot: { invoice: { ...inv, notes: reason }, credit_basis: [] },
    })
    const { ready } = mountDetail(inv)
    let chainReads = 0
    http.get.mockImplementation((url: string) => {
      if (url.includes('/document-chain')) {
        chainReads += 1
        return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: { due_amount: chainReads === 1 ? '121.00' : '242.00' }, available_actions: [] })
      }
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/content-blocks' || url === '/api/v1/document-templates' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers/')) return Promise.resolve({ id: 'customer-A', name: 'Customer' })
      throw new Error(`unexpected GET ${url}`)
    })
    store.fetchInvoice.mockResolvedValueOnce(inv).mockResolvedValueOnce(inv).mockResolvedValueOnce(updated1)
    store.saveRevision.mockResolvedValueOnce(updated1).mockResolvedValueOnce(updated2)
    store.fetchRevisions.mockResolvedValueOnce({ items: [revision(1, 'First correction')], total: 1 })
      .mockResolvedValueOnce({ items: [revision(1, 'First correction')], total: 1 })
      .mockResolvedValueOnce({ items: [revision(2, 'Second correction'), revision(1, 'First correction')], total: 2 })
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes('data-due')).toBe('121.00')
    await wrapper.findAll('input').find(input => input.element.value === 'notes-A')!.setValue('First change')
    let fields = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await fields.at(-1)!.setValue('First correction')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-workflow]').attributes('data-due')).toBe('242.00')
    expect(store.fetchRevisions).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('First correction')

    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'reference-A')!.setValue('reference-2')
    fields = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await fields.at(-1)!.setValue('Second correction')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(store.saveRevision).toHaveBeenCalledTimes(2)
    expect(store.saveRevision.mock.calls[1]?.[1].expected_updated_at).toBe(updated1.updated_at)
    expect(store.fetchRevisions).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain('Second correction')
  })

  it('retains a saved revision when its chain refresh fails and retries the current projection', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const updated = { ...inv, notes: 'Saved change', due_amount: '242.00', updated_at: '2026-08-31T11:00:00Z' }
    const { ready } = mountDetail(inv)
    let chainReads = 0
    http.get.mockImplementation((url: string) => {
      if (url.includes('/document-chain')) {
        chainReads += 1
        if (chainReads === 2) return Promise.reject(new Error('projection unavailable'))
        return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: { due_amount: chainReads === 1 ? '121.00' : '242.00' }, available_actions: [] })
      }
      if (url === '/api/v1/vat-rates' || url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
      if (url === '/api/v1/content-blocks' || url === '/api/v1/document-templates' || url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/customers/')) return Promise.resolve({ id: 'customer-A', name: 'Customer' })
      throw new Error(`unexpected GET ${url}`)
    })
    store.saveRevision.mockResolvedValue(updated)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'notes-A')!.setValue('Saved change')
    await wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox').at(-1)!.setValue('Correction')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Your revision was saved, but the document chain below could not be refreshed')
    expect(wrapper.text()).toContain('Due: EUR 242.00')
    expect(wrapper.find('[data-workflow]').attributes('data-due')).toBe('121.00')
    expect(messages.success).toHaveBeenCalledWith('Invoice revised. Its status and number are unchanged.')
    expect(messages.error).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(chainReads).toBe(3)
    expect(wrapper.find('[data-workflow]').attributes('data-due')).toBe('242.00')
    expect(wrapper.text()).not.toContain('Your revision was saved, but the document chain below could not be refreshed')
  })

  it('blocks a second save and preserves the entire form when the backend reports a stale conflict', async () => {
    let rejectSave!: (error: unknown) => void
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    store.saveRevision.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject }))
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const lineName = wrapper.findAll('input').find(input => input.element.value === 'line-A')!
    await lineName.setValue('Kept local line')
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('Network correction')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    const save = wrapper.findAll('button').find(button => button.text().includes('Save revision'))!
    await save.trigger('click')
    await flushPromises()
    expect(save.attributes('disabled')).toBeDefined()
    expect(store.saveRevision).toHaveBeenCalledTimes(1)
    rejectSave(new ApiError(409, { code: 'stale' }, 'stale'))
    await flushPromises()
    expect(lineName.element.value).toBe('Kept local line')
    expect(messages.error).toHaveBeenLastCalledWith(expect.stringContaining('Your inputs are still here'))
    expect(wrapper.findAll('input').some(input => input.element.value === 'Network correction')).toBe(true)
  })

  it('requires both reason and delivery confirmation, disables a no-op, and retains inputs on a generic save failure', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const save = wrapper.findAll('button').find(button => button.text().includes('Save revision'))!
    expect(save.attributes('disabled')).toBeDefined()
    const notes = wrapper.findAll('input').find(input => input.element.value === 'notes-A')!
    await notes.setValue('Local change')
    expect(save.attributes('disabled')).toBeDefined()
    const reason = () => wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox').at(-1)!
    await reason().setValue('   ')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(save.attributes('disabled')).toBeDefined()
    await reason().setValue('Correcting a typo')
    await wrapper.find('input[type="checkbox"]').setValue(false)
    expect(save.attributes('disabled')).toBeDefined()
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(save.attributes('disabled')).toBeUndefined()
    store.saveRevision.mockRejectedValue(new Error('temporary save failure'))
    await save.trigger('click')
    await flushPromises()
    expect(notes.element.value).toBe('Local change')
    expect(reason().element.value).toBe('Correcting a typo')
    expect(messages.error).toHaveBeenLastCalledWith('temporary save failure')
  })

  it('rechecks eligibility at entry and refuses revision when the fresh detail has become ineligible', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    store.fetchInvoice.mockResolvedValueOnce(inv).mockResolvedValueOnce({
      ...inv, revision_eligibility: { available: false, reason_code: 'has_payment' },
    })
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    expect(store.fetchInvoice).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain('Review and confirm revision')
    expect(messages.warning).toHaveBeenCalledWith(expect.stringContaining('no longer eligible'))
  })

  it('copies selected Terms & Conditions text into the revision and keeps it independent of later library edits', async () => {
    const block = { id: 'terms-1', kind: 'TERMS', name: 'Current terms', body: 'Copied term text', is_default: false }
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', terms_text: null, revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv, [block])
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const selector = wrapper.findAll('select').find(select => Array.from(select.element.options).some(option => option.text === 'Current terms'))!
    await selector.setValue('terms-1')
    expect(wrapper.findAll('input').some(input => input.element.value === 'Copied term text')).toBe(true)
    block.body = 'Changed library text'
    expect(wrapper.findAll('input').some(input => input.element.value === 'Copied term text')).toBe(true)
    expect(wrapper.findAll('input').some(input => input.element.value === 'Changed library text')).toBe(false)
    store.saveRevision.mockResolvedValue({ ...inv, terms_text: 'Copied term text' })
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('Update customer terms')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(store.saveRevision.mock.calls.at(-1)?.[1].changes.terms_text).toBe('Copied term text')
  })

  it('shows the old and new quarterly report periods when the invoice date crosses a quarter', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    ;(wrapper.findComponent(InvoiceEdit).vm.$.setupState as any).invoiceDate = '2026-10-01'
    await flushPromises()
    expect(wrapper.text()).toContain('Q3 2026')
    expect(wrapper.text()).toContain('Q4 2026')
    expect(wrapper.text()).toContain('does not know whether either reporting period has been filed')
  })

  it('cancels a clean revision without prompting or sending a save', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Cancel revision'))!.trigger('click')
    await flushPromises()
    expect(dialogs.warning).not.toHaveBeenCalled()
    expect(store.saveRevision).not.toHaveBeenCalled()
    expect(wrapper.findAll('button').some(button => button.text().includes('Revise invoice'))).toBe(true)
  })

  it('asks before leaving a dirty revision and honors both keep-editing and discard choices', async () => {
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve({
      ...invoice(id, 'STANDARD'), status: 'SENT', revision_eligibility: { available: id === 'A', reason_code: null },
    }))
    const { router, ready } = mountDetail({
      ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null },
    })
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'line-A')!.setValue('Unsaved line')
    dialogs.warning.mockImplementationOnce((options: any) => options.onNegativeClick())
    await router.push('/invoices/B/edit')
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('A')
    expect(wrapper.text()).toContain('Unsaved line')
    dialogs.warning.mockImplementationOnce((options: any) => options.onPositiveClick())
    await router.push('/invoices/B/edit')
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(router.currentRoute.value.params.id).toBe('B')
    expect(store.fetchInvoice).toHaveBeenLastCalledWith('B')
  })

  it('ignores a late revision preview after navigating from invoice A to B', async () => {
    const { router, ready } = mountDetail({
      ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null },
    })
    let resolvePreview!: (value: any) => void
    store.calculateRevision.mockReturnValue(new Promise(resolve => { resolvePreview = resolve }))
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve({
      ...invoice(id, 'STANDARD'), status: 'SENT', revision_eligibility: { available: id === 'A', reason_code: null },
    }))
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await new Promise(resolve => setTimeout(resolve, 620))
    await flushPromises()
    expect(store.calculateRevision).toHaveBeenCalled()
    await router.push('/invoices/B/edit')
    await flushPromises()
    resolvePreview({ lines: [], subtotal_excl_vat: '9999', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '9999', vat_total: '0', total_incl_vat: '9999' })
    await flushPromises()
    expect(wrapper.text()).toContain('INV-B')
    expect(wrapper.text()).not.toContain('9999')
  })

  it('ignores superseded success and error responses on the same invoice without clearing the newest loading state', async () => {
    const requests: Array<{ resolve: (result: any) => void; reject: (error: unknown) => void }> = []
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    store.calculateRevision.mockImplementation(() => new Promise((resolve, reject) => requests.push({ resolve, reject })))
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const waitForCalculation = async () => {
      await new Promise(resolve => setTimeout(resolve, 620))
      await flushPromises()
    }
    await waitForCalculation()
    const notes = wrapper.findAll('input').find(input => input.element.value === 'notes-A')!
    await notes.setValue('preview generation two')
    await waitForCalculation()
    expect(requests.length).toBeGreaterThanOrEqual(2)
    const result = (total: string) => ({ lines: [], subtotal_excl_vat: total, line_discount_total: '0', document_discount_amount: '0', taxable_amount: total, vat_total: '0', total_incl_vat: total })
    requests[0]!.resolve(result('111'))
    await flushPromises()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    expect(state.previewLoading).toBe(true)
    expect(wrapper.text()).not.toContain('111.00')
    requests.at(-1)!.resolve(result('222'))
    await flushPromises()
    expect(wrapper.text()).toContain('222.00')

    state.dueDate = '2026-11-15'
    await waitForCalculation()
    state.termsText = 'next text change'
    await waitForCalculation()
    expect(requests.length).toBeGreaterThanOrEqual(4)
    const current = requests.at(-1)!
    requests.at(-2)!.reject(new Error('superseded preview failed'))
    await flushPromises()
    expect(state.previewLoading).toBe(true)
    expect(state.previewError).toBeNull()
    current.resolve(result('444'))
    await flushPromises()
    expect(wrapper.text()).toContain('444.00')
    expect(wrapper.text()).not.toContain('superseded preview failed')
  })

  it('keeps database scale-three numeric values as a clean no-op', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', discount_value: '0.000', revision_eligibility: { available: true, reason_code: null }, lines: [{ ...invoice('A', 'STANDARD').lines[0], quantity: '1.000', unit_price: '10.000', discount_value: '0.000' }] }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('No changes to save.')
    expect(wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.attributes('disabled')).toBeDefined()
    expect(dialogs.warning).not.toHaveBeenCalled()
  })

  it('preserves original scale-three numeric strings when other revision fields or line order change', async () => {
    const raw = '9007199254740.991'
    const base = invoice('A', 'STANDARD')
    const inv = {
      ...base, status: 'SENT', discount_value: raw, revision_eligibility: { available: true, reason_code: null },
      lines: [
        { ...base.lines[0], quantity: raw, unit_price: raw, discount_value: raw },
        { ...base.lines[0], id: 'line-A2', name: 'second', quantity: '1.000', unit_price: '10.000', discount_value: '0.000' },
      ],
    }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    expect(state.revisionChanges).toEqual({})

    state.notes = 'notes only'
    await flushPromises()
    expect(state.revisionChanges).toEqual({ notes: 'notes only' })
    expect(state.revisionChanges).not.toHaveProperty('lines')
    expect(state.revisionChanges).not.toHaveProperty('discount')

    state.lines[0].description = 'description only'
    await flushPromises()
    const afterDescription = state.revisionChanges.lines
    expect(afterDescription[0]).toMatchObject({ quantity: raw, unit_price: raw, discount: { value: raw } })
    expect(afterDescription[1]).toMatchObject({ quantity: '1.000', unit_price: '10.000', discount: { value: '0.000' } })

    state.lines.reverse()
    await flushPromises()
    const afterReorder = state.revisionChanges.lines
    expect(afterReorder.map((line: any) => line.name)).toEqual(['second', 'line-A'])
    expect(afterReorder[1]).toMatchObject({ quantity: raw, unit_price: raw, discount: { value: raw } })
    expect(state.revisionChanges.discount).toBeUndefined()
  })

  it('shows supply date old and new values in the translated revision summary, including clearing', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', supply_or_advance_date: '2026-08-01', revision_eligibility: { available: true, reason_code: null } }
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    state.supplyOrAdvanceDate = '2026-08-02'
    await flushPromises()
    expect(wrapper.text()).toContain('Supply date: 2026-08-01 → 2026-08-02')
    state.supplyOrAdvanceDate = null
    await flushPromises()
    expect(wrapper.text()).toContain('Supply date: 2026-08-01 → (empty)')
  })

  it('loads every history page and retries from the first page after a failed page', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'COMPLETED', revision_eligibility: { available: false, reason_code: 'has_payment' } }
    const makeRevision = (order: number) => ({
      id: `rev-${order}`, invoice_id: 'A', revision_order: order, actor_user_id: 'user-1',
      created_at: '2026-10-01T10:00:00Z', reason: `Reason ${order}`, undelivered_confirmed: true,
      before_snapshot: { invoice: { ...inv, lines: [] }, credit_basis: [] },
      after_snapshot: { invoice: { ...inv, lines: [] }, credit_basis: [] },
    })
    const { ready } = mountDetail(inv)
    let failSecond = true
    store.fetchRevisions.mockImplementation((_id: string, page: { offset: number; limit: number }) => {
      if (page.offset === 100 && failSecond) { failSecond = false; return Promise.reject(new Error('offline')) }
      const first = page.offset
      return Promise.resolve({ items: Array.from({ length: Math.min(page.limit, 101 - first) }, (_, index) => makeRevision(101 - first - index)), total: 101 })
    })
    const wrapper = await ready()
    expect(wrapper.text()).toContain('Could not load revision history.')
    await wrapper.findAll('button').find(button => button.text().includes('Retry'))!.trigger('click')
    await flushPromises()
    expect(store.fetchRevisions.mock.calls.map(call => call[1]?.offset)).toEqual([0, 100, 0, 100])
    expect(wrapper.text()).toContain('Reason 1')
    expect(wrapper.text()).toContain('Reason 101')
    const history = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    expect(history.revisionHistory[0].revision_order).toBe(101)
    expect(history.revisionHistory.at(-1).revision_order).toBe(1)
  })

  it('ignores a late history page after its invoice context resets to another route', async () => {
    const invA = { ...invoice('A', 'STANDARD'), status: 'COMPLETED' }
    const invB = { ...invoice('B', 'STANDARD'), status: 'COMPLETED' }
    let resolveA!: (result: any) => void
    const pendingA = new Promise<any>(resolve => { resolveA = resolve })
    const revision = (id: string, reason: string) => ({
      id, invoice_id: id === 'rev-A' ? 'A' : 'B', revision_order: 1, actor_user_id: null,
      created_at: '2026-10-01T10:00:00Z', reason, undelivered_confirmed: true,
      before_snapshot: { invoice: { ...invA, lines: [] }, credit_basis: [] },
      after_snapshot: { invoice: { ...invA, lines: [] }, credit_basis: [] },
    })
    const { router, ready } = mountDetail(invA)
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve(id === 'A' ? invA : invB))
    store.fetchRevisions.mockImplementationOnce(() => pendingA).mockResolvedValueOnce({ items: [revision('rev-B', 'B history')], total: 1 })
    const wrapper = await ready()
    await router.push('/invoices/B/edit')
    await flushPromises()
    expect(wrapper.text()).toContain('B history')
    resolveA({ items: [revision('rev-A', 'A stale history')], total: 1 })
    await flushPromises()
    expect(wrapper.text()).toContain('B history')
    expect(wrapper.text()).not.toContain('A stale history')
  })

  it('does not let an A save response overwrite invoice B after route navigation', async () => {
    const invA = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const invB = { ...invoice('B', 'STANDARD'), status: 'SENT', notes: 'B current', revision_eligibility: { available: false, reason_code: 'wrong_state' } }
    let resolvePending!: (value: any) => void
    const pending = new Promise<any>(resolve => { resolvePending = resolve })
    const { router, ready } = mountDetail(invA)
    store.fetchInvoice.mockImplementation((id: string) => Promise.resolve(id === 'A' ? invA : invB))
    const wrapper = await ready()
    store.saveRevision.mockReturnValue(pending)
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const notes = wrapper.findAll('input').find(input => input.element.value === 'notes-A')!
    await notes.setValue('A pending save')
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('A reason')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    dialogs.warning.mockImplementationOnce((options: any) => options.onPositiveClick())
    await router.push('/invoices/B/edit')
    await flushPromises()
    resolvePending({ ...invA, notes: 'A saved' })
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('B')
    expect(wrapper.text()).toContain('INV-B')
    expect(wrapper.text()).not.toContain('A pending save')
    expect(wrapper.text()).not.toContain('A saved')
    expect(messages.success).not.toHaveBeenCalledWith(expect.stringContaining('Invoice revised'))
  })

  it('freezes the complete revision form while a real save request is pending', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    let resolveSave!: (value: any) => void
    const pending = new Promise<any>(resolve => { resolveSave = resolve })
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    store.saveRevision.mockReturnValue(pending)
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const notes = wrapper.findAll('input').find(input => input.element.value === 'notes-A')!
    await notes.setValue('submitted notes')
    const inputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await inputs.at(-1)!.setValue('Fix typo')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()

    expect(store.saveRevision).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="invoice-edit-form"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('input[type="checkbox"]').attributes('disabled')).toBeDefined()
    expect((wrapper.findComponent(InvoiceEdit).vm.$.setupState as any).revisionReason).toBe('Fix typo')
    expect(wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.attributes('loading')).toBe('true')

    resolveSave({ ...inv, notes: 'submitted notes' })
    await flushPromises()
    expect(messages.success).toHaveBeenCalledWith('Invoice revised. Its status and number are unchanged.')
    expect(wrapper.findAll('button').find(button => button.text().includes('Save revision'))).toBeUndefined()
  })

  it('keeps the form frozen through a pending save error and preserves the entered revision', async () => {
    const inv = { ...invoice('A', 'STANDARD'), status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    let rejectSave!: (cause: unknown) => void
    const { ready } = mountDetail(inv)
    const wrapper = await ready()
    store.saveRevision.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject }))
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'notes-A')!.setValue('keep this after error')
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('Fix typo')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="invoice-edit-form"]').attributes('disabled')).toBeDefined()
    expect(store.saveRevision).toHaveBeenCalledTimes(1)
    rejectSave(new Error('request failed'))
    await flushPromises()
    expect(wrapper.find('[data-testid="invoice-edit-form"]').attributes('disabled')).not.toBe('true')
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    expect(state.notes).toBe('keep this after error')
    expect(state.revisionReason).toBe('Fix typo')
    expect(messages.error).toHaveBeenCalledWith('request failed')
  })

  it('matches revision totals to filtered row identities and keeps issued amounts frozen after load, cancel, and save', async () => {
    const source = invoice('A', 'STANDARD')
    const issued = {
      ...source, status: 'SENT', revision_eligibility: { available: true, reason_code: null },
      subtotal_excl_vat: '100.00', line_discount_total: '0.00', document_discount_amount: '0.00',
      taxable_amount: '100.00', vat_total: '21.00', total_incl_vat: '121.00',
      due_amount: '121.00', vat_treatment_snapshot: { code: 'STANDARD', label: 'Standard', effect: 'TAXED', requires_icp: false },
      lines: [{ ...source.lines[0], name: 'A', total_incl_vat: '10.00' }],
    }
    let resolveStalePreview!: (result: any) => void
    store.calculateRevision.mockReturnValueOnce(new Promise(resolve => { resolveStalePreview = resolve }))
    const { ready } = mountDetail(issued)
    const wrapper = await ready()
    expect(wrapper.text()).toContain('121.00')
    expect(store.calculatePreview).not.toHaveBeenCalled()

    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    const first = state.lines[0]
    state.lines = [
      { ...first, name: '', _key: 'blank-leading-row' },
      { ...first, name: 'A', _key: 'row-A', unit_price: 10 },
      { ...first, _lineId: undefined, name: 'B', _key: 'row-B', unit_price: 20 },
    ]
    state.notes = 'temporary edit'
    await new Promise(resolve => setTimeout(resolve, 620))
    await flushPromises()
    const calculatedLines = [
      { name: 'A', total_incl_vat: '10.00' }, { name: 'B', total_incl_vat: '20.00' },
    ]
    store.calculateRevision.mockResolvedValue({
      lines: calculatedLines, subtotal_excl_vat: '30.00', line_discount_total: '0.00', document_discount_amount: '0.00',
      taxable_amount: '30.00', vat_total: '0.00', total_incl_vat: '30.00',
    })
    state.notes = 'cause another preview'
    await new Promise(resolve => setTimeout(resolve, 620))
    await flushPromises()
    expect(state.lineCalcMap.get('row-A')?.total_incl_vat).toBe('10.00')
    expect(state.lineCalcMap.get('row-B')?.total_incl_vat).toBe('20.00')
    expect(wrapper.text()).toContain('1. A · (empty) · 1 × 10 · (empty) · Discount: NONE 0 · VAT treatment: (empty) · total: 10.00')
    expect(wrapper.text()).toContain('2. B · (empty) · 1 × 20 · (empty) · Discount: NONE 0 · VAT treatment: (empty) · total: 20.00')

    dialogs.warning.mockImplementation((options: any) => options.onPositiveClick())
    await wrapper.findAll('button').find(button => button.text().includes('Cancel revision'))!.trigger('click')
    await flushPromises()
    resolveStalePreview({ lines: [], subtotal_excl_vat: '999', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '999', vat_total: '0', total_incl_vat: '999' })
    await flushPromises()
    expect(wrapper.text()).toContain('121.00')
    expect(wrapper.text()).not.toContain('999.00')
    expect(store.calculatePreview).not.toHaveBeenCalled()

    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input').find(input => input.element.value === 'notes-A')!.setValue('text only revision')
    const textInputs = wrapper.findAll('input').filter(input => input.attributes('type') !== 'checkbox')
    await textInputs.at(-1)!.setValue('Correct stale wording')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(store.calculatePreview).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('121.00')
    expect(wrapper.text()).not.toContain('10.00\nTotal')
  })

  it('includes supply date in an ordinary draft write and shows edited line discount and VAT in the confirmation', async () => {
    const draft = { ...invoice('A', 'STANDARD'), revision_eligibility: { available: false, reason_code: 'wrong_state' } }
    const { ready } = mountDetail(draft)
    const wrapper = await ready()
    const state = wrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    state.supplyOrAdvanceDate = '2026-08-02'
    await flushPromises()
    store.updateInvoice.mockResolvedValue(draft)
    await wrapper.findAll('button').find(button => button.text() === 'Save')!.trigger('click')
    await flushPromises()
    expect(store.updateInvoice.mock.calls[0]?.[1].supply_or_advance_date).toBe('2026-08-02')

    const revised = { ...draft, status: 'SENT', revision_eligibility: { available: true, reason_code: null } }
    const setup = mountDetail(revised)
    const revisionWrapper = await setup.ready()
    await revisionWrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const revisionState = revisionWrapper.findComponent(InvoiceEdit).vm.$.setupState as any
    revisionState.lines[0].discount_type = 'PERCENTAGE'
    revisionState.lines[0].discount_value = 10
    revisionState.lines[0].vat_rate_id = 'changed-rate'
    await flushPromises()
    expect(revisionWrapper.text()).toContain('NONE 0 · VAT treatment: (empty)')
    expect(revisionWrapper.text()).toContain('PERCENTAGE 10')
    expect(revisionWrapper.text()).toContain('changed-rate')
  })
})
