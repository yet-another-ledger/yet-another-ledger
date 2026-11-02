import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NForm, NModal, NSelect } from 'naive-ui'
import en from '../../src/locales/en.json'

const api = vi.hoisted(() => ({ get: vi.fn(), downloadBlob: vi.fn() }))
const store = vi.hoisted(() => ({
  fetchInvoice: vi.fn(), fetchProductOptions: vi.fn(), calculatePreview: vi.fn(), createInvoice: vi.fn(), updateInvoice: vi.fn(),
  transitionStatus: vi.fn(), calculateRevision: vi.fn(), saveRevision: vi.fn(), fetchRevisions: vi.fn(),
}))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }))
const dialogs = vi.hoisted(() => ({ warning: vi.fn() }))

vi.mock('../../src/api/http', () => ({ ...api, ApiError: class extends Error { status = 500 } }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => store }))
vi.mock('naive-ui', async () => {
  const actual = await vi.importActual<typeof import('naive-ui')>('naive-ui')
  return { ...actual, useMessage: () => messages, useDialog: () => dialogs }
})
vi.mock('@vicons/ionicons5', () => {
  const icon = { template: '<span />' }
  return { AddOutline: icon, TrashOutline: icon, DocumentTextOutline: icon, DownloadOutline: icon, MailOutline: icon, EyeOutline: icon }
})

import InvoiceEdit from '../../src/views/invoices/InvoiceEdit.vue'
const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function invoice(id: string) {
  return {
    id, invoice_number: `INV-${id}`, document_kind: 'STANDARD', status: 'SENT', paid_status: 'UNPAID',
    customer_id: `customer-${id}`, reference_number: null, invoice_date: '2026-08-31', due_date: null,
    supply_or_advance_date: null, updated_at: '2026-08-31T10:00:00Z',
    revision_eligibility: { available: true, reason_code: null }, tax_mode: 'LINE', amounts_include_vat: false,
    vat_treatment_id: 'treatment-A', document_vat_rate_id: null, discount_type: 'NONE', discount_value: '0',
    notes: `notes-${id}`, warranty_text: null, terms_text: null, bank_text: null, payment_terms_text: null,
    currency: 'EUR', due_amount: '10.00', total_incl_vat: '10.00', lines: [{
      id: `line-${id}`, product_id: null, name: `line-${id}`, description: null, quantity: '1', unit_id: null,
      unit_name: null, unit_price: '10', discount_type: 'NONE', discount_value: '0', vat_rate_id: null,
    }],
  }
}

async function mountWithVat(inv: any, rates: () => any[]) {
  for (const mock of Object.values(store)) mock.mockReset()
  api.get.mockClear()
  messages.error.mockClear()
  store.fetchInvoice.mockResolvedValue(inv)
  store.fetchProductOptions.mockResolvedValue([])
  store.fetchRevisions.mockResolvedValue({ items: [], total: 0 })
  const preview = { lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' }
  store.calculatePreview.mockResolvedValue(preview)
  store.calculateRevision.mockResolvedValue(preview)
  api.get.mockImplementation((url: string) => {
    if (url === '/api/v1/vat-rates') return Promise.resolve({ items: rates() })
    if (url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [] })
    if (url === '/api/v1/content-blocks' || url === '/api/v1/document-templates' || url === '/api/v1/note-templates') return Promise.resolve([])
    if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
    if (url.includes('/document-chain')) return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
    if (url === `/api/v1/customers/${inv.customer_id}`) return Promise.resolve({ id: inv.customer_id, name: 'Customer' })
    throw new Error(`unexpected GET ${url}`)
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
  await router.push(`/invoices/${inv.id}/edit`)
  await router.isReady()
  const wrapper = mount({ template: '<router-view />' }, { global: { plugins: [router, i18n], stubs: {
    InvoicePaymentPanel: true, DocumentWorkflowPanel: true, DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
  } } })
  await flushPromises()
  return wrapper
}

describe('InvoiceEdit VAT selection labels', () => {
  const rate = { id: 'rate-A', label: 'SI2 test rate', percent: '23', active: true }
  function frozenInvoice() {
    const inv = invoice('A')
    return { ...inv, taxes: [], lines: [{ ...inv.lines[0], vat_rate_id: rate.id, vat_rate_label: 'Original rate', vat_rate_percent: '21' }] }
  }

  it.each(['LINE', 'DOCUMENT'])('shows the issued %s tax snapshot before and during revision', async mode => {
    const inv = { ...frozenInvoice(), tax_mode: mode, document_vat_rate_id: mode === 'DOCUMENT' ? rate.id : null,
      taxes: mode === 'DOCUMENT' ? [{ vat_rate_id: rate.id, vat_rate_label: 'Original rate', vat_rate_percent: '21' }] : [] }
    const wrapper = await mountWithVat(inv, () => [rate, { ...rate, id: 'rate-B', label: 'Reduced', percent: '9' }])
    const selectedRate = () => wrapper.findAllComponents(NSelect).find(select => select.props('value') === rate.id)!
    expect(selectedRate().text()).toContain('Original rate (21%)')
    expect(selectedRate().text()).not.toContain('SI2 test rate')
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    expect(selectedRate().text()).toContain('Original rate (21%)')
    const select = selectedRate()
    await select.vm.$emit('update:value', 'rate-B')
    await flushPromises()
    expect(select.text()).toContain('Reduced (9%)')
    await select.vm.$emit('update:value', rate.id)
    await flushPromises()
    expect(select.text()).toContain('Original rate (21%)')
    wrapper.unmount()
  })

  it('keeps different snapshots of one rate distinct while new lines use the current dictionary', async () => {
    const inv = frozenInvoice()
    inv.lines.push({ ...inv.lines[0]!, id: 'line-B', name: 'Another line', vat_rate_label: 'Older rate', vat_rate_percent: '19' })
    const wrapper = await mountWithVat(inv, () => [rate])
    const selectedRates = () => wrapper.findAllComponents(NSelect).filter(select => select.props('value') === rate.id)
    expect(selectedRates().map(select => select.text())).toEqual(['Original rate (21%)', 'Older rate (19%)'])
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Add line'))!.trigger('click')
    const newRate = wrapper.findAllComponents(NSelect).find(select => select.props('value') == null
      && select.props('options')?.some((option: any) => option.value === rate.id))!
    await newRate.vm.$emit('update:value', rate.id)
    await flushPromises()
    expect(selectedRates().map(select => select.text())).toEqual(['Original rate (21%)', 'Older rate (19%)', 'SI2 test rate (23%)'])
    wrapper.unmount()
  })

  it('uses current dictionary choices for a draft', async () => {
    const wrapper = await mountWithVat({ ...frozenInvoice(), status: 'DRAFT' }, () => [rate])
    const select = wrapper.findAllComponents(NSelect).find(select => select.props('value') === rate.id)!
    expect(select.text()).toContain('SI2 test rate (23%)')
    wrapper.unmount()
  })

  it('refreshes choices on entering revision after another tab restores the dictionary', async () => {
    let rates = [rate]
    const wrapper = await mountWithVat(frozenInvoice(), () => rates)
    rates = [{ ...rate, label: 'Restored dictionary', percent: '21' }, { ...rate, id: 'rate-new', label: 'New choice', percent: '9' }]
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    const select = wrapper.findAllComponents(NSelect).find(select => select.props('value') === rate.id)!
    expect(select.props('options')).toContainEqual({ value: 'rate-new', label: 'New choice (9%)' })
    expect(select.text()).toContain('Original rate (21%)')
    expect(api.get.mock.calls.filter(([url]) => url === '/api/v1/vat-rates')).toHaveLength(2)
    wrapper.unmount()
  })
})

describe('InvoiceEdit with actual Naive UI controls', () => {
  it('freezes every editor control during a pending save and unlocks them after failure', async () => {
    const inv = invoice('A')
    const pending = deferred<any>()
    for (const mock of Object.values(store)) mock.mockReset()
    store.fetchInvoice.mockResolvedValue(inv)
    store.fetchProductOptions.mockResolvedValue([])
    store.fetchRevisions.mockResolvedValue({ items: [], total: 0 })
    store.calculatePreview.mockResolvedValue({ lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' })
    store.calculateRevision.mockResolvedValue({ lines: [], subtotal_excl_vat: '10', line_discount_total: '0', document_discount_amount: '0', taxable_amount: '10', vat_total: '0', total_incl_vat: '10' })
    store.saveRevision.mockReturnValue(pending.promise)
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/vat-rates') return Promise.resolve({ items: [] })
      if (url.startsWith('/api/v1/vat-treatments')) return Promise.resolve({ items: [{ id: 'treatment-A', code: 'NL', label: 'Domestic', active: true }] })
      if (url === '/api/v1/content-blocks') return Promise.resolve([{ id: 'block-A', name: 'Warranty block', kind: 'WARRANTY', body: 'Warranty' }])
      if (url === '/api/v1/document-templates') return Promise.resolve([{ id: 'template-A', name: 'Invoice template', applies_to: 'INVOICE', lines: [] }])
      if (url === '/api/v1/note-templates') return Promise.resolve([])
      if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [] })
      if (url.includes('/document-chain')) return Promise.resolve({ quote_id: null, nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
      if (url === '/api/v1/customers/customer-A') return Promise.resolve({ id: 'customer-A', name: 'Customer A' })
      throw new Error(`unexpected GET ${url}`)
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/invoices/:id/edit', component: InvoiceEdit }] })
    await router.push('/invoices/A/edit')
    await router.isReady()
    const wrapper = mount({ template: '<router-view />' }, { global: { plugins: [router, i18n], stubs: {
      InvoicePaymentPanel: true, DocumentWorkflowPanel: true, DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
    } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Revise invoice'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Add line'))!.trigger('click')
    await flushPromises()

    const notesInput = wrapper.findAll('textarea').find(el => el.element.value === 'notes-A')!
    expect(notesInput.exists()).toBe(true)
    expect(wrapper.text()).toContain('Load from library')
    expect(wrapper.findAll('button').some(button => button.text().includes('Apply Template'))).toBe(true)
    await notesInput.setValue('submitted notes')
    // Clearing this actual Naive UI selection used to bypass NForm's disabled state.
    const treatmentField = wrapper.findAll('.n-form-item').find(field => field.text().includes('VAT treatment'))!
    const clearable = treatmentField.find('.n-base-selection')
    const treatmentSelect = wrapper.findAllComponents(NSelect).find(select => select.text().includes('NL – Domestic'))!
    expect(clearable.exists()).toBe(true)
    await clearable.trigger('mouseenter')
    const clearButton = clearable.find('[data-clear]')
    expect(clearButton.exists()).toBe(true)
    await clearButton.trigger('mousedown')
    await clearButton.trigger('click')
    expect(treatmentSelect.text()).toContain('Auto-derive from customer')
    await treatmentSelect.vm.$emit('update:value', 'treatment-A')
    await flushPromises()
    const reason = wrapper.findAll('textarea').at(-1)!
    await reason.setValue('audited correction')
    const confirmation = wrapper.find('.n-checkbox')
    await confirmation.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('Apply Template'))!.trigger('click')
    expect(wrapper.findComponent(NModal).props('show')).toBe(true)
    await wrapper.findAll('button').find(button => button.text().includes('Save revision'))!.trigger('click')
    await flushPromises()
    expect(store.saveRevision).toHaveBeenCalledTimes(1)

    const nativeNotes = wrapper.findAll('textarea').find(el => el.element.value === 'submitted notes')!
    expect(nativeNotes.element.disabled).toBe(true)
    expect(wrapper.findAll('textarea').at(-1)!.element.disabled).toBe(true)
    expect(confirmation.classes()).toContain('n-checkbox--disabled')
    expect(store.saveRevision.mock.calls[0]![1].changes).not.toHaveProperty('vat_treatment_id')
    expect(wrapper.findComponent(NForm).props('disabled')).toBe(true)
    expect(treatmentSelect.vm.mergedDisabled).toBe(true)
    expect(wrapper.findAllComponents(NSelect).find(select => select.text().includes('Per line'))!.vm.mergedDisabled).toBe(true)
    expect(wrapper.findComponent(NModal).props('show')).toBe(false)
    expect(clearable.find('[data-clear]').exists()).toBe(false)
    expect(wrapper.find('.line-drag-handle').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Load from library')
    expect(wrapper.findAll('button').some(button => button.text().includes('Apply Template'))).toBe(false)
    expect(wrapper.findAll('button').find(button => button.text().includes('Cancel revision'))!.element.disabled).toBe(true)

    pending.resolve(Promise.reject(new Error('save failed')) as any)
    await flushPromises()
    expect(wrapper.findAll('textarea').find(el => el.element.value === 'submitted notes')!.element.disabled).toBe(false)
    expect(wrapper.findAll('textarea').at(-1)!.element.disabled).toBe(false)
    expect(wrapper.find('.n-checkbox--disabled').exists()).toBe(false)
    expect(treatmentSelect.vm.mergedDisabled).toBe(false)
    expect(wrapper.find('.line-drag-handle').exists()).toBe(true)
    expect(wrapper.text()).toContain('Load from library')
    expect(wrapper.findAll('button').some(button => button.text().includes('Apply Template'))).toBe(true)
    expect(wrapper.findAll('button').find(button => button.text().includes('Cancel revision'))!.element.disabled).toBe(false)
    wrapper.unmount()
  })
})
