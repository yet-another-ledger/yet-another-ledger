import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import Draggable from 'vuedraggable'
import en from '../../src/locales/en.json'

const mocks = vi.hoisted(() => ({
  get: vi.fn(), fetch: vi.fn(), save: vi.fn(), calculate: vi.fn(), products: vi.fn(),
  success: vi.fn(), error: vi.fn(),
}))
const ui = vi.hoisted(() => ({
  box: { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="header-extra" /><slot name="icon" /></div>' },
  button: { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /><slot name="icon" /></button>' },
  input: { props: ['value'], emits: ['update:value'], template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />' },
  select: { props: ['value', 'options'], emits: ['update:value'], template: '<select :value="value"><option v-for="option in options ?? []" :key="option.value" :value="option.value">{{ option.label }}</option></select>' },
}))
vi.mock('../../src/api/http', () => ({ get: mocks.get, downloadBlob: vi.fn(), ApiError: class extends Error {} }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => ({ fetchInvoice: mocks.fetch, updateInvoice: mocks.save, createInvoice: mocks.save, calculatePreview: mocks.calculate, fetchProductOptions: mocks.products }) }))
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => ({ fetchQuote: mocks.fetch, updateQuote: mocks.save, createQuote: mocks.save, calculatePreview: mocks.calculate }) }))
vi.mock('../../src/stores/estimates', () => ({ useEstimatesStore: () => ({ fetchEstimate: mocks.fetch, updateEstimate: mocks.save, createEstimate: mocks.save, calculatePreview: mocks.calculate }) }))
vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: mocks.success, error: mocks.error, warning: vi.fn() }), useDialog: () => ({ warning: (options: any) => options.onPositiveClick() }),
  NAlert: ui.box, NButton: ui.button, NCard: ui.box, NDatePicker: ui.input, NDivider: ui.box, NDropdown: ui.box,
  NForm: ui.box, NFormItem: ui.box, NGrid: ui.box, NGi: ui.box, NIcon: ui.box, NInput: ui.input, NInputNumber: ui.input,
  NList: ui.box, NListItem: ui.box, NModal: ui.box, NSpace: ui.box, NSelect: ui.select, NSpin: ui.box, NSwitch: ui.input,
  NTag: ui.box, NText: ui.box, NThing: { props: ['title'], template: '<span data-template-title>{{ title }}</span>' }, NEmpty: ui.box,
}))
vi.mock('@vicons/ionicons5', () => ({ AddOutline: ui.box, TrashOutline: ui.box, DocumentTextOutline: ui.box, DownloadOutline: ui.box, MailOutline: ui.box, EyeOutline: ui.box }))
import InvoiceEdit from '../../src/views/invoices/InvoiceEdit.vue'
import QuoteEdit from '../../src/views/quotes/QuoteEdit.vue'
import EstimateEdit from '../../src/views/estimates/EstimateEdit.vue'
import LineOrdering from '../../src/components/LineOrdering.vue'

function deferred() {
  let resolve!: (result: any) => void
  let reject!: (error: Error) => void
  const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function row(name: string, price = '10') {
  return { name, description: `description ${name}`, quantity: '1', unit_price: price, unit_cost_excl_vat: price,
    margin_rate: '0.2', group_id: name === 'third' ? 'g2' : 'g1', product_id: null,
    unit_id: null, unit_name: null, discount_type: 'NONE', discount_value: '0', vat_rate_id: null }
}
function record(status = 'DRAFT') {
  return { id: 'a', name: 'Estimate', invoice_number: 'INV-a', quote_number: 'Q-a', document_kind: 'STANDARD', status,
    customer_id: 'customer', invoice_date: '2026-10-25', quote_date: '2026-10-25', due_date: null, valid_until: null,
    tax_mode: 'LINE', amounts_include_vat: false, discount_type: 'FIXED', discount_value: '0.01', currency: 'EUR',
    groups: [{ id: 'g1', public_description: 'Group 1', vat_rate_id: null }, { id: 'g2', public_description: 'Group 2', vat_rate_id: null }],
    lines: [row('same'), row('same', '20'), row('third', '30')] }
}
function result(amount: string, lines = 3) {
  return { lines: Array.from({ length: lines }, (_, i) => ({ line_total: `${i + 1}.00`, margin_amount: '0.20', line_sell_excl_vat: `${i + 1}.20` })),
    groups: [], subtotal_excl_vat: amount, total_excl_vat: amount, total_margin: '0.60', total_incl_vat: amount,
    line_discount_total: '0', document_discount_amount: '0.01', taxable_amount: amount, vat_total: '0' }
}
const editors = [['invoices', InvoiceEdit, 'Save invoice'], ['quotes', QuoteEdit, 'Save quote'], ['estimates', EstimateEdit, 'Save estimate']] as const
const mounted: ReturnType<typeof mount>[] = []
async function setup(kind: typeof editors[number][0], component: typeof editors[number][1], status = 'DRAFT', isNew = false, pendingFetch?: Promise<any>) {
  mocks.fetch.mockReturnValue(pendingFetch ?? Promise.resolve(record(status)))
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: `/${kind}/:id/edit`, component }, { path: `/${kind}/new`, component }, { path: `/${kind}`, component: { template: '<div />' } },
  ] })
  await router.push(isNew ? `/${kind}/new` : `/${kind}/a/edit`); await router.isReady()
  const wrapper = mount(component, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: {
    InvoicePaymentPanel: true, QuotePaymentPanel: true, DocumentWorkflowPanel: true, DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
  } } })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks()
  mocks.products.mockResolvedValue([]); mocks.calculate.mockResolvedValue(result('60.00')); mocks.save.mockRejectedValue(new Error('save rejected'))
  mocks.get.mockImplementation((url: string) => {
    if (url.includes('document-chain')) return Promise.resolve({ nodes: [], relations: [], events: [], totals: {}, available_actions: [] })
    if (url === '/api/v1/customers/customer') return Promise.resolve({ id: 'customer', name: 'Customer' })
    if (url.includes('templates') || url.includes('content-blocks')) return Promise.resolve([])
    return Promise.resolve({ items: [] })
  })
})
afterEach(() => { mounted.splice(0).forEach(w => w.unmount()); vi.useRealTimers() })

describe.each(editors)('%s line ordering', (kind, component) => {
  it('moves first/last duplicate-content rows while retaining row identity, edits, memberships and payload order after failed save', async () => {
    const wrapper = await setup(kind, component)
    const ordering = wrapper.findComponent(LineOrdering)
    const draggable = ordering.findComponent(Draggable)
    expect(draggable.vm.$attrs.handle).toBe('.line-drag-handle')
    expect(draggable.props('itemKey')).toBe('_key')
    const rows = ordering.props('modelValue') as any[]
    const keys = rows.map(l => l._key)
    expect(new Set(keys).size).toBe(3)
    const originalInput = wrapper.findAll('[data-line-key]')[0]!.find('input').element
    await wrapper.findAll('[data-line-key]')[0]!.find('input').setValue('edited')
    draggable.vm.$emit('update:modelValue', [rows[2], rows[1], rows[0]])
    await flushPromises()
    expect(ordering.props('modelValue').map(l => l._key)).toEqual([keys[2], keys[1], keys[0]])
    expect(wrapper.findAll('[data-line-key]')[2]!.find('input').element).toBe(originalInput)
    expect((originalInput as HTMLInputElement).value).toBe('edited')
    // A request already saving must not accept another drag mutation.
    const pending = deferred(); mocks.save.mockReturnValueOnce(pending.promise)
    const save = wrapper.findAll('button').find(b => b.text() === en[kind].save)!
    expect(save).toBeDefined(); await save.trigger('click'); await flushPromises()
    expect(ordering.props('disabled')).toBe(true)
    draggable.vm.$emit('update:modelValue', rows)
    await flushPromises()
    expect(ordering.props('modelValue').map(l => l._key)).toEqual([keys[2], keys[1], keys[0]])
    pending.reject(new Error('save rejected')); await flushPromises()
    expect(ordering.props('disabled')).toBe(false)
    expect(mocks.error).toHaveBeenCalled()
    const payload = mocks.save.mock.calls[0]![1]
    expect(payload.lines.map((l: any) => l.name)).toEqual(['third', 'same', 'edited'])
    expect(JSON.stringify(payload)).not.toContain('_key')
    if (kind === 'estimates') {
      expect(payload.groups.map((g: any) => g.public_description)).toEqual(['Group 1', 'Group 2'])
      expect(payload.lines.map((l: any) => l.group_ref)).toEqual([payload.groups[1].ref, payload.groups[0].ref, payload.groups[0].ref])
    }
  })

  it.each(['success', 'error'])('ignores stale preview %s during rapid moves, including the debounce interval', async outcome => {
    const wrapper = await setup(kind, component)
    const old = deferred(); mocks.calculate.mockReturnValueOnce(old.promise)
    await vi.advanceTimersByTimeAsync(600)
    const ordering = wrapper.findComponent(LineOrdering)
    const rows = ordering.props('modelValue')
    ordering.findComponent(Draggable).vm.$emit('update:modelValue', [rows[2], rows[0], rows[1]])
    await flushPromises()
    if (outcome === 'success') old.resolve(result('999.99'))
    else old.reject(new Error('stale preview failure'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('999.99')
    expect(wrapper.text()).not.toContain('stale preview failure')
    const current = deferred(); mocks.calculate.mockReturnValueOnce(current.promise)
    await vi.advanceTimersByTimeAsync(600)
    expect(mocks.calculate.mock.calls.at(-1)![0].lines.map((l: any) => l.name)).toEqual(['third', 'same', 'same'])
    current.resolve(result('123.45')); await flushPromises()
    expect(wrapper.text()).toContain('123.45')
    if (kind === 'estimates') {
      const rendered = wrapper.findAll('[data-line-key]')
      expect(rendered[0]!.text()).toContain('1.20')
      expect(rendered[1]!.text()).toContain('2.20')
      expect(rendered[2]!.text()).toContain('3.20')
    }
  })

  it('keeps the newest completed preview when an older in-flight request finishes last', async () => {
    const wrapper = await setup(kind, component)
    const old = deferred(); const newest = deferred()
    mocks.calculate.mockReturnValueOnce(old.promise).mockReturnValueOnce(newest.promise)
    await vi.advanceTimersByTimeAsync(600)
    const ordering = wrapper.findComponent(LineOrdering)
    const rows = ordering.props('modelValue')
    ordering.findComponent(Draggable).vm.$emit('update:modelValue', [...rows].reverse()); await flushPromises()
    await vi.advanceTimersByTimeAsync(600)
    newest.resolve(result('234.56')); await flushPromises()
    expect(wrapper.text()).toContain('234.56')
    old.resolve(result('999.99')); await flushPromises()
    expect(wrapper.text()).toContain('234.56')
    expect(wrapper.text()).not.toContain('999.99')
  })

  it('disables dragging while the existing record is loading', async () => {
    const pending = deferred()
    const wrapper = await setup(kind, component, 'DRAFT', false, pending.promise)
    const ordering = wrapper.findComponent(LineOrdering)
    // Invoice and Quote may hide the form during loading; Estimate keeps it mounted.
    if (ordering.exists()) {
      expect(ordering.props('disabled')).toBe(true)
      expect(wrapper.findAll('.line-drag-handle')).toHaveLength(0)
    }
    pending.resolve(record()); await flushPromises()
    expect(wrapper.findComponent(LineOrdering).props('disabled')).toBe(false)
  })

  it('gives new and added rows unique keys, removes a row then reorders without losing input', async () => {
    const wrapper = await setup(kind, component, 'DRAFT', true)
    const ordering = wrapper.findComponent(LineOrdering)
    const add = wrapper.findAll('button').find(b => b.text() === en[kind].addLine)!
    await add.trigger('click'); await add.trigger('click')
    let rows = ordering.props('modelValue') as any[]
    expect(new Set(rows.map(l => l._key)).size).toBe(3)
    const removedKey = rows[1]._key
    // Each line has a single remove button, alongside its shared handle.
    await wrapper.findAll('[data-line-key]')[1]!.findAll('button').at(-1)!.trigger('click')
    rows = ordering.props('modelValue') as any[]
    ordering.findComponent(Draggable).vm.$emit('update:modelValue', [...rows].reverse()); await flushPromises()
    expect(ordering.props('modelValue')).toHaveLength(2)
    expect(ordering.props('modelValue').some(l => l._key === removedKey)).toBe(false)
  })
})
it.each([['invoices', InvoiceEdit, 'SENT'], ['quotes', QuoteEdit, 'ACCEPTED']] as const)('disables handles for existing %s read-only boundaries', async (kind, component, status) => {
  const wrapper = await setup(kind, component, status)
  expect(wrapper.findComponent(LineOrdering).props('disabled')).toBe(true)
  expect(wrapper.findAll('.line-drag-handle')).toHaveLength(0)
})

it.each([['invoices', InvoiceEdit], ['quotes', QuoteEdit]] as const)('assigns stable distinct keys to %s template lines and excludes keys from save', async (kind, component) => {
  const template = { id: 'template', name: 'Two equal rows', applies_to: 'BOTH', lines: [row('template same'), row('template same')] }
  const ordinaryGet = mocks.get.getMockImplementation()!
  mocks.get.mockImplementation((url: string) => url === '/api/v1/document-templates' ? Promise.resolve([template]) : ordinaryGet(url))
  const wrapper = await setup(kind, component)
  const apply = wrapper.findAll('button').find(b => b.text() === en[kind].applyTemplate)!
  await apply.trigger('click')
  await wrapper.find('[data-template-title]').trigger('click')
  await flushPromises()
  const ordering = wrapper.findComponent(LineOrdering)
  const rows = ordering.props('modelValue')
  expect(rows).toHaveLength(2)
  expect(new Set(rows.map(l => l._key)).size).toBe(2)
  ordering.findComponent(Draggable).vm.$emit('update:modelValue', [...rows].reverse()); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === en[kind].save)!.trigger('click'); await flushPromises()
  expect(mocks.save.mock.calls[0]![1].lines.map((l: any) => l.name)).toEqual(['template same', 'template same'])
  expect(JSON.stringify(mocks.save.mock.calls[0]![1])).not.toContain('_key')
})
