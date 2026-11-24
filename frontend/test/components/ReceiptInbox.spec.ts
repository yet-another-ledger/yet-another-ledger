import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'
import { ApiError } from '../../src/api/http'

const inbox = vi.hoisted(() => ({
  fetch: vi.fn(), detail: vi.fn(), upload: vi.fn(), append: vi.fn(), removeFile: vi.fn(), discard: vi.fn(), retry: vi.fn(), confirm: vi.fn(),
  items: [] as any[], total: 0, loading: false, error: null as string | null,
}))
const api = vi.hoisted(() => ({ post: vi.fn() }))
const dicts = vi.hoisted(() => ({
  categories: { value: [{ id: 'cat', name: 'Office', default_deductible: true }, { id: 'private', name: 'Private', default_deductible: false }] }, rates: { value: [{ id: 'rate', percent: '21' }] },
  treatmentItems: [
    { id: 'treat', code: 'NL_DOMESTIC_PURCH', label: 'Domestic', side: 'PURCHASE', effect: 'APPLY_RATE', active: true },
    { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', label: 'EU reverse charge', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true },
  ], treatments: null as any,
  categoryOptions: { value: [{ label: 'Office', value: 'cat' }, { label: 'Private', value: 'private' }] }, purchaseTreatmentOptions: { value: [{ label: 'DOMESTIC', value: 'treat' }, { label: 'EU reverse charge', value: 'reverse' }] },
  vatRateOptions: { value: [{ label: '21%', value: 'rate' }] }, load: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('../../src/stores/receiptInbox', () => ({ useReceiptInboxStore: () => inbox }))
vi.mock('../../src/api/http', () => ({ ApiError: class ApiError extends Error { constructor(public status:number, public detail:unknown, message:string) { super(message) } }, post: api.post }))
vi.mock('../../src/composables/usePurchaseExpenseDictionaries', async importOriginal => ({
  ...(await importOriginal<typeof import('../../src/composables/usePurchaseExpenseDictionaries')>()),
  toExpenseInput: (fields: unknown) => fields,
  usePurchaseExpenseDictionaries: () => {
    dicts.treatments = ref(dicts.treatmentItems)
    return { ...dicts, treatments: dicts.treatments }
  },
}))
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="icon" /></div>' }
  const button = { inheritAttrs: false, props: ['disabled', 'type', 'attrType', 'tag', 'href', 'download'], template: '<component :is="tag || \'button\'" v-bind="$attrs" :data-variant="type" :href="href" :download="download" :disabled="disabled" :type="attrType"><slot /></component>' }
  const input = { inheritAttrs: false, props: ['value'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" @input="$emit(\'update:value\',$event.target.value)" />' }
  const select = { inheritAttrs: false, props: ['value', 'options', 'clearable'], emits: ['update:value'], template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\',$event.target.value)"><option value=""></option><option v-for="x in options" :key="x.value" :value="x.value">{{x.label}}</option></select>' }
  const form = { inheritAttrs: false, template: '<form v-bind="$attrs"><slot /></form>' }
  const number = { inheritAttrs: false, props: ['value'], emits: ['update:value'], template: '<input type="number" v-bind="$attrs" :value="value" @input="$emit(\'update:value\',Number($event.target.value))" />' }
  const checkbox = { props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" :checked="checked" @change="$emit(\'update:checked\',$event.target.checked)" />' }
  const upload = defineComponent({ name: 'UploadStub', emits: ['change'], setup(_, { emit }) {
    const fileList = ref<{ file: File }[]>([])
    function choose(file: File) { fileList.value.push({ file }); emit('change', { fileList: fileList.value }) }
    return { choose, fileList }
  }, template: '<div><slot /></div>' })
  return { NAlert: box, NButton: button, NCard: box, NCheckbox: checkbox, NEmpty: box, NForm: form, NFormItem: box, NInput: input, NInputNumber: number, NSelect: select, NSpin: box, NSpace: box, NTag: box, NUpload: upload }
})

import ReceiptInbox from '../../src/views/expenses/ReceiptInbox.vue'
const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
const pending = (patch: Record<string, unknown> = {}) => ({ id: 'inbox-1', status: 'PENDING', files: [{ id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 }], file_revision: 0, ai_state: 'UNAVAILABLE', ai_suggestion: null, ai_error_code: 'AI_DISABLED', expense_id: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', confirmed_at: null, ...patch })

function setup(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/receipt-inbox', component: ReceiptInbox }, { path: '/receipt-inbox/:id', component: ReceiptInbox }, { path: '/expenses/:id/edit', component: { template: '<div />' } }] })
  return { router, mount: async () => { await router.push(path); await router.isReady(); const wrapper = mount(ReceiptInbox, { global: { plugins: [router, i18n] } }); await flushPromises(); return wrapper } }
}

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('ReceiptInbox review workflow', () => {
  beforeEach(() => {
    vi.resetAllMocks(); inbox.items = []; inbox.total = 0; inbox.fetch.mockResolvedValue(undefined); dicts.load.mockResolvedValue(undefined)
    dicts.treatmentItems = [
      { id: 'treat', code: 'NL_DOMESTIC_PURCH', label: 'Domestic', side: 'PURCHASE', effect: 'APPLY_RATE', active: true },
      { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', label: 'EU reverse charge', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true },
    ]
    dicts.rates.value = [{ id: 'rate', percent: '21' }]
    dicts.vatRateOptions.value = [{ label: '21%', value: 'rate' }]
    api.post.mockResolvedValue({ vat_amount: '2.10', gross_amount: '12.10' })
  })

  it('loads each reused route, clears the prior draft, and ignores a late detail response', async () => {
    inbox.items = [pending(), pending({ id: 'inbox-2', files: [{ id: 'file-2', filename: 'second.jpg', mime_type: 'image/jpeg', byte_size: 42 }], ai_state: 'READY', ai_suggestion: { supplier_name: 'Current AI' } })]
    let finishFirst!: (value: unknown) => void
    inbox.detail.mockImplementation((id: string) => id === 'inbox-1' ? new Promise(resolve => { finishFirst = resolve }) : Promise.resolve(pending({ id: 'inbox-2', files: [{ id: 'file-2', filename: 'second.jpg', mime_type: 'image/jpeg', byte_size: 42 }], ai_state: 'READY', ai_suggestion: { supplier_name: 'Current AI', net_amount: '20' } })))
    const app = setup('/receipt-inbox'), wrapper = await app.mount()
    await app.router.push('/receipt-inbox/inbox-1')
    await flushPromises()
    await app.router.push('/receipt-inbox/inbox-2')
    await flushPromises()
    expect(wrapper.text()).toContain('second.jpg')
    await wrapper.findAll('input').find(x => x.attributes('type') !== 'date' && x.attributes('type') !== 'number')!.setValue('Old draft')
    finishFirst(pending({ ai_state: 'READY', ai_suggestion: { supplier_name: 'Stale AI', net_amount: '99' } }))
    await flushPromises()
    expect(wrapper.text()).toContain('second.jpg')
    expect(wrapper.text()).not.toContain('receipt.jpg')
    expect((wrapper.findAll('input').find(x => x.attributes('type') !== 'date' && x.attributes('type') !== 'number')!.element as HTMLInputElement).value).toBe('Old draft')
    expect((wrapper.find('input[type="number"]').element as HTMLInputElement).value).toBe('20')
    await app.router.push('/receipt-inbox')
    await app.router.push('/receipt-inbox/inbox-2')
    await flushPromises()
    expect(wrapper.findAll('input').some(x => (x.element as HTMLInputElement).value === 'Old draft')).toBe(false)
    expect(inbox.detail).toHaveBeenCalledWith('inbox-2')
  })

  it('remounts the stateful upload after each append so previous files are never sent twice', async () => {
    inbox.detail.mockResolvedValue(pending())
    inbox.append.mockImplementation(async (_id: string, files: File[]) => pending({ files: [{ id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 }, ...files.map((file, i) => ({ id: `${file.name}-${i}`, filename: file.name, mime_type: file.type, byte_size: file.size }))], ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Updated suggestion', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Updated file evidence.' } } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const first = new File(['a'], 'first.jpg', { type: 'image/jpeg' })
    const second = new File(['b'], 'second.jpg', { type: 'image/jpeg' })
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(first)
    await flushPromises()
    await wrapper.findAll('button').find(x => x.text() === 'Add files')!.trigger('click')
    await flushPromises()
    expect(inbox.append.mock.calls[0][1]).toEqual([first])
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Updated suggestion')!.element as HTMLInputElement).value).toBe('Updated suggestion')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(second)
    await flushPromises()
    await wrapper.findAll('button').find(x => x.text() === 'Add files')!.trigger('click')
    await flushPromises()
    expect(inbox.append.mock.calls[0][1]).toEqual([first])
    expect(inbox.append.mock.calls[1][1]).toEqual([second])
  })

  it('retracts an AI-owned treatment after a successful file removal returns no treatment', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT is charged.' } } }))
    inbox.removeFile.mockResolvedValue(pending({ files: [{ id: 'file-2', filename: 'other.jpg', mime_type: 'image/jpeg', byte_size: 40 }], ai_state: 'FAILED', ai_suggestion: null }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('')
    expect(wrapper.text()).not.toContain('Dutch VAT is charged.')
  })

  it('replaces and withdraws product evidence after same-cost file revisions while preserving product edits', async () => {
    const files = [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'extra.jpg', mime_type: 'image/jpeg', byte_size: 42 },
    ]
    const suggestion = { suggested_product_name: 'Product A', suggested_product_brand: 'Brand A', suggested_product_model: 'Model A' }
    inbox.detail.mockResolvedValue(pending({ files, file_revision: 1, ai_state: 'READY', ai_suggestion: suggestion }))
    const firstReply = deferred<unknown>()
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('input[type="checkbox"]')[2]!.setValue(true)
    await flushPromises()
    expect(wrapper.findAll('input').map(input => (input.element as HTMLInputElement).value)).toContain('Product A')
    await wrapper.findAll('select')[0]!.setValue('cat')
    await wrapper.findAll('select')[1]!.setValue('treat')
    await wrapper.findAll('input[type="number"]')[0]!.setValue('10')
    await wrapper.findAll('input[type="number"]')[1]!.setValue('2.1')
    await wrapper.findAll('input[type="number"]').at(-1)!.setValue('3')
    await flushPromises()
    const productInput = wrapper.findAll('input').find(input => (input.element as HTMLInputElement).value === 'Product A')!
    const brandInput = wrapper.findAll('input').find(input => (input.element as HTMLInputElement).value === 'Brand A')!
    expect((productInput.element as HTMLInputElement).value).toBe('Product A')
    await brandInput.setValue('Owner brand')
    api.post.mockReturnValue(firstReply.promise)
    await wrapper.findAll('button').find(button => button.text() === en.fixedAssets.estimateButton)!.trigger('click')
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/fixed-assets/residual-estimate', expect.objectContaining({ cost_inputs: expect.objectContaining({ net_amount: 10, vat_amount: 2.1 }) }))

    inbox.removeFile.mockResolvedValueOnce(pending({ files: [files[1]], file_revision: 2, updated_at: '2026-01-01T00:01:00Z', ai_state: 'READY', ai_suggestion: { suggested_product_name: 'Product B', suggested_product_brand: 'Brand B', suggested_product_model: 'Model B' } }))
    await wrapper.findAll('button').find(button => button.text() === 'Delete')!.trigger('click')
    await flushPromises()
    firstReply.resolve({ status: 'suggested', suggested_residual_value: '9.00', reason: 'Previous file estimate', assumptions: [], acquisition_cost: '10.00', currency_code: 'EUR' })
    await flushPromises()
    expect((productInput.element as HTMLInputElement).value).toBe('Product B')
    expect((brandInput.element as HTMLInputElement).value).toBe('Owner brand')
    expect((wrapper.findAll('input[type="number"]').at(-1)!.element as HTMLInputElement).value).toBe('3')
    expect(wrapper.text()).not.toContain('Previous file estimate')

    inbox.removeFile.mockResolvedValueOnce(pending({ files: [files[1]], file_revision: 3, updated_at: '2026-01-01T00:02:00Z', ai_state: 'FAILED', ai_suggestion: null }))
    await wrapper.findAll('button').find(button => button.text() === 'Delete')!.trigger('click')
    await flushPromises()
    expect((productInput.element as HTMLInputElement).value).toBe('')
    expect((brandInput.element as HTMLInputElement).value).toBe('Owner brand')
    expect((wrapper.findAll('input[type="number"]').at(-1)!.element as HTMLInputElement).value).toBe('3')
    wrapper.unmount()
  })

  it('blocks confirmation while an append is pending, including direct form submission', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old files.' } } }))
    let finish!: (value: unknown) => void
    inbox.append.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[1].setValue('reverse')
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(new File(['x'], 'new.pdf', { type: 'application/pdf' }))
    await nextTick()
    await wrapper.find('.append').findAll('button').at(1)!.trigger('click')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect(wrapper.text()).not.toContain('Old files.')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    expect(inbox.retry).not.toHaveBeenCalled()
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    expect((wrapper.findAll('button').find(x => x.text() === en.receiptInbox.confirm)!.element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).not.toHaveBeenCalled()
    finish(pending({ ai_suggestion: null, ai_state: 'FAILED' }))
    await flushPromises()
  })

  it('blocks confirmation while a removal is pending, including direct form submission', async () => {
    inbox.detail.mockResolvedValue(pending({ files: [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'extra.jpg', mime_type: 'image/jpeg', byte_size: 42 },
    ], ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old files.' } } }))
    let finish!: (value: unknown) => void
    inbox.removeFile.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[1].setValue('reverse')
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    expect((wrapper.findAll('button').find(x => x.text() === en.receiptInbox.confirm)!.element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).not.toHaveBeenCalled()
    finish(pending({ files: [{ id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 }], ai_suggestion: null, ai_state: 'FAILED' }))
    await flushPromises()
  })

  it.each(['append', 'removeFile'] as const)('allows manual review after a coherent detail resolves an ambiguous %s', async method => {
    const oldFiles = [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-extra', filename: 'extra.jpg', mime_type: 'image/jpeg', byte_size: 42 },
    ]
    const oldGroup = pending({ files: oldFiles, ai_state: 'READY', ai_suggestion: { supplier_name: 'Old group' } })
    const changedGroup = pending({ files: method === 'append'
      ? [...oldFiles, { id: 'file-new', filename: 'new.pdf', mime_type: 'application/pdf', byte_size: 42 }]
      : [oldFiles[1]], ai_state: 'FAILED', ai_suggestion: null })
    inbox.detail.mockResolvedValueOnce(oldGroup).mockResolvedValueOnce(oldGroup)
    inbox[method].mockRejectedValueOnce(new TypeError('Failed to fetch'))
    inbox.retry.mockResolvedValueOnce(oldGroup).mockResolvedValueOnce(changedGroup)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    if (method === 'append') {
      ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(new File(['x'.repeat(42)], 'new.pdf', { type: 'application/pdf' }))
      await nextTick()
      await wrapper.find('.append').findAll('button').at(1)!.trigger('click')
    } else {
      await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    }
    await flushPromises()
    expect(inbox.detail).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    expect((wrapper.findAll('button').find(x => x.text() === en.receiptInbox.confirm)!.element as HTMLButtonElement).disabled).toBe(false)
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).toHaveBeenCalledTimes(1)
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.any(Object), 0)
  })

  it('clears a committed append selection after recovery so the next append contains only new files', async () => {
    const oldGroup = pending()
    const first = new File(['one'], 'first.pdf', { type: 'application/pdf' })
    const second = new File(['two'], 'second.pdf', { type: 'application/pdf' })
    const committedGroup = pending({ file_revision: 1, files: [...oldGroup.files, { id: 'first', filename: first.name, mime_type: first.type, byte_size: first.size }] })
    inbox.detail.mockResolvedValueOnce(oldGroup).mockResolvedValueOnce(oldGroup)
    inbox.append.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValueOnce(pending({ files: [...oldGroup.files, { id: 'first', filename: first.name, mime_type: first.type, byte_size: first.size }, { id: 'second', filename: second.name, mime_type: second.type, byte_size: second.size }] }))
    inbox.retry.mockResolvedValueOnce(committedGroup)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(first)
    await nextTick(); await wrapper.find('.append').findAll('button').at(1)!.trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(second)
    await nextTick(); await wrapper.find('.append').findAll('button').at(1)!.trigger('click'); await flushPromises()
    expect(inbox.append.mock.calls[1][1]).toEqual([second])
  })

  it('restores the AI treatment and retained AI rate after a real LAST_FILE rejection', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT on the receipt.' }, vat_rate_percent: '21' } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Same-group treatment.' } } }))
    inbox.removeFile.mockRejectedValueOnce(new ApiError(409, { detail: 'LAST_FILE' }, 'LAST_FILE'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    expect(wrapper.text()).toContain('Same-group treatment.')
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
  })

  it('preserves a manual treatment selected while a definitive file rejection is pending', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT on the receipt.' } } }))
    let reject!: (error: unknown) => void
    inbox.removeFile.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    await wrapper.findAll('select')[1].setValue('reverse')
    reject(new ApiError(409, { detail: 'LAST_FILE' }, 'LAST_FILE'))
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect(wrapper.text()).toContain('Dutch VAT on the receipt.')
  })

  it('keeps a same-group retry alive after a definitive LAST_FILE rejection', async () => {
    const retry = deferred<unknown>()
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old receipt evidence.' } } }))
    inbox.retry.mockReturnValueOnce(retry.promise)
    inbox.removeFile.mockRejectedValueOnce(new ApiError(409, { detail: 'LAST_FILE' }, 'LAST_FILE'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(button => button.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === 'Delete')!.trigger('click')
    await flushPromises()
    retry.resolve(pending({ updated_at: '2026-01-02T00:00:00Z', ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Current retry survives LAST_FILE.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Current retry survives LAST_FILE.')
    expect(wrapper.text()).not.toContain('Old receipt evidence.')
    wrapper.unmount()
  })

  it('keeps manual review available after an explicit file-action rejection', async () => {
    inbox.detail.mockResolvedValue(pending())
    inbox.removeFile.mockRejectedValueOnce(new ApiError(409, { detail: 'LAST_FILE' }, 'LAST_FILE'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    await flushPromises()
    expect(inbox.detail).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).toHaveBeenCalledTimes(1)
  })

  it('treats append validation 422 as a known rejection and keeps the chosen files for correction', async () => {
    inbox.detail.mockResolvedValue(pending())
    inbox.append.mockRejectedValueOnce(new ApiError(422, 'File exceeds configured size limit.', 'invalid file'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const chosen = new File(['oversized'], 'too-large.pdf', { type: 'application/pdf' })
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(chosen)
    await nextTick()
    await wrapper.find('.append').findAll('button').at(1)!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    expect((wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { fileList: { file: File }[] }).fileList.map(x => x.file)).toEqual([chosen])
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    inbox.confirm.mockResolvedValueOnce({ inbox: pending({ status: 'CONFIRMED', expense_id: 'saved' }), expense: { id: 'saved' } })
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.objectContaining({ category_id: 'cat' }), 0)
    expect(inbox.detail).toHaveBeenCalledTimes(1)
  })

  it('sends the reviewed file revision and reloads the current group after a revision conflict', async () => {
    inbox.detail.mockResolvedValueOnce(pending({ file_revision: 4 })).mockResolvedValueOnce(pending({ file_revision: 5, files: [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'new.jpg', mime_type: 'image/jpeg', byte_size: 42 },
    ], ai_suggestion: null }))
    inbox.confirm.mockRejectedValueOnce(new ApiError(409, { detail: 'FILE_GROUP_CHANGED' }, 'FILE_GROUP_CHANGED'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.any(Object), 4)
    expect(inbox.detail).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('a[download]')).toHaveLength(2)
    expect(wrapper.findAll('a[download]')[1].attributes('href')).toContain('file-2')
    expect(wrapper.text()).toContain(en.receiptInbox.fileGroupChanged)
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
  })

  it('applies the overlapping retry result when confirmation fails before the retry finishes', async () => {
    const previous = pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Previous extraction.' } } })
    inbox.detail.mockResolvedValueOnce(previous).mockResolvedValueOnce(previous)
    let finishRetry!: (value: unknown) => void
    inbox.retry.mockReturnValueOnce(new Promise(resolve => { finishRetry = resolve }))
    inbox.confirm.mockRejectedValueOnce(new ApiError(422, 'invalid expense', 'invalid expense'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain(en.receiptInbox.ai.RUNNING)
    finishRetry(pending({ updated_at: '2026-01-02T00:00:00Z', ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Retry completed after the failed confirmation.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Retry completed after the failed confirmation.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
  })

  it('ignores an older retry response after a file append and a newer retry have started', async () => {
    inbox.detail.mockResolvedValueOnce(pending()).mockResolvedValueOnce(pending({ file_revision: 1, ai_state: 'RUNNING', ai_suggestion: null, files: [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'new.jpg', mime_type: 'image/jpeg', byte_size: 3 },
    ] }))
    let finishOld!: (value: unknown) => void
    let finishCurrent!: (value: unknown) => void
    let rejectConfirm!: (error: unknown) => void
    inbox.retry.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve })).mockReturnValueOnce(new Promise(resolve => { finishCurrent = resolve }))
    inbox.append.mockResolvedValueOnce(pending({ file_revision: 1, files: [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'new.jpg', mime_type: 'image/jpeg', byte_size: 3 },
    ], ai_suggestion: null }))
    inbox.confirm.mockReturnValueOnce(new Promise((_, reject) => { rejectConfirm = reject }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    const chosen = new File(['new'], 'new.jpg', { type: 'image/jpeg' })
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(chosen)
    await nextTick(); await wrapper.find('.append').findAll('button').at(1)!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit')
    finishOld(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Stale one-file retry.' } } }))
    await flushPromises()
    rejectConfirm(new ApiError(422, 'invalid expense', 'invalid expense'))
    await flushPromises()
    finishCurrent(pending({ file_revision: 1, updated_at: '2026-01-03T00:00:00Z', ai_state: 'READY', files: [
      { id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 },
      { id: 'file-2', filename: 'new.jpg', mime_type: 'image/jpeg', byte_size: 3 },
    ], ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Current two-file retry.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Current two-file retry.')
    expect(wrapper.text()).not.toContain('Stale one-file retry.')
    expect(wrapper.findAll('a[download]')).toHaveLength(2)
  })

  it('keeps the unchanged group suggestion reviewable after a rejected removal and replaces it after retry', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old evidence.' } } }))
    inbox.removeFile.mockRejectedValueOnce(new ApiError(409, { detail: 'LAST_FILE' }, 'LAST_FILE'))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Latest retry evidence.' } } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text() === 'Delete')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Old evidence.')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Latest retry evidence.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
  })

  it('preserves an ordinary AI rate through partial and failed retries, clears it for reverse charge, and keeps a manual rate', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH' }, vat_rate_percent: '21' } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH' } } }))
      .mockResolvedValueOnce(pending({ ai_state: 'FAILED', ai_suggestion: null }))
      .mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH' } } }))
      .mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH' } } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const retry = () => wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    await wrapper.findAll('select')[2].setValue('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
  })

  it('keeps the last successful missing-VAT fact through a failed retry and clears it after VAT is recognized', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: {
      supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42', vat_amount: '3.3',
      suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Ordinary receipt VAT.' },
    } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, updated_at: '2026-01-02T00:00:00Z', ai_suggestion: {
      supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Latest extraction has no receipt VAT.' },
    } })).mockResolvedValueOnce(pending({ ai_state: 'FAILED', ai_error_code: 'AI_PROVIDER_ERROR', updated_at: '2026-01-03T00:00:00Z', ai_suggestion: null }))
      .mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, updated_at: '2026-01-04T00:00:00Z', ai_suggestion: {
        supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42', vat_amount: '0',
      } }))
    inbox.confirm.mockResolvedValue({ inbox: pending(), expense: { id: 'saved' } })
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const retry = () => wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    await retry(); await flushPromises()
    await retry(); await flushPromises()
    const selects = wrapper.findAll('select')
    await selects[1].setValue('reverse')
    await selects[2].setValue('rate')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm).not.toHaveBeenCalled()
    await retry(); await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'rate', net_amount: 42, vat_amount: 0,
    }), 0)
  })

  it('replaces an ordinary AI rate only when a retry percentage matches the loaded dictionary', async () => {
    dicts.rates.value = [{ id: 'rate', percent: '21' }, { id: 'rate-9', percent: '9' }]
    dicts.vatRateOptions.value = [{ label: '21%', value: 'rate' }, { label: '9%', value: 'rate-9' }]
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_suggestion: { vat_rate_percent: '21' } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { vat_rate_percent: '17' } }))
      .mockResolvedValueOnce(pending({ ai_state: 'READY', ai_suggestion: { vat_rate_percent: '9' } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const retry = () => wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry(); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate-9')
  })

  it('does not let a retry start while confirmation is pending or invalidate its success', async () => {
    inbox.detail.mockResolvedValue(pending())
    let finish!: (value: unknown) => void
    inbox.confirm.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    expect(inbox.retry).not.toHaveBeenCalled()
    finish({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    await flushPromises()
    expect(wrapper.vm.$route.path).toBe('/expenses/expense-1/edit')
  })

  it('uses the category deductible default for manual and AI classification', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_category_id: 'private' } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const category = wrapper.findAll('select')[0]
    await category.setValue('private')
    expect((wrapper.find('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(false)
    await category.setValue('cat')
    // Initial AI classification fills the editable form automatically.
    await wrapper.findAll('select')[1].setValue('treat')
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10')
    await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    inbox.confirm.mockResolvedValue({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(inbox.confirm.mock.calls[0][1]).toMatchObject({ category_id: 'cat', deductible: true })
  })

  it('auto-fills, replaces and retracts an AI-owned treatment while preserving manual selection and clearing', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT is charged.' } } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The buyer accounts for VAT.' }, vat_rate_percent: '21', vat_amount: '0' } }))
      .mockResolvedValueOnce(pending({ ai_suggestion: null, ai_state: 'FAILED' }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
    expect(wrapper.text()).toContain('Review or select the applicable Dutch nominal VAT rate.')
    await wrapper.findAll('select')[1].setValue('treat')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
    await wrapper.findAll('select')[1].setValue('')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('')
  })

  it('waits for dictionaries and does not auto-fill an unavailable treatment', async () => {
    dicts.treatmentItems = []
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { vat_rate_percent: '21', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' } } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    expect(wrapper.text()).toContain('The suggested VAT treatment is not available in the current purchase dictionary.')
    dicts.treatments.value = [{ id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', label: 'EU reverse charge', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true }]
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
  })

  it('does not apply an ordinary suggested rate when the owner selected reverse charge', async () => {
    inbox.detail.mockResolvedValue(pending())
    inbox.retry.mockResolvedValue(pending({ ai_suggestion: {
      suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT is charged.' }, vat_rate_percent: '21', vat_amount: '2.1',
    } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[1].setValue('reverse')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
  })

  it('offers an explicit apply action for a different suggestion and keeps it through confirmation', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Reverse charge is stated.' } } }))
    inbox.confirm.mockResolvedValue({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    expect(wrapper.text()).toContain('Apply suggestion')
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    expect(inbox.confirm).not.toHaveBeenCalled()
    expect((selects[1].element as HTMLSelectElement).value).toBe('reverse')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm.mock.calls[0][1].vat_treatment_id).toBe('reverse')
  })

  it('prevents a late retry from restoring a suggestion after confirmation starts', async () => {
    inbox.detail.mockResolvedValue(pending())
    let finishRetry!: (value: unknown) => void
    let finishConfirm!: (value: unknown) => void
    inbox.retry.mockReturnValueOnce(new Promise(resolve => { finishRetry = resolve }))
    inbox.confirm.mockReturnValueOnce(new Promise(resolve => { finishConfirm = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm).toHaveBeenCalledTimes(1)
    finishRetry(pending({ ai_state: 'READY', ai_suggestion: { supplier_name: 'Stale retry result', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Stale retry evidence.' } } }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('Stale retry evidence.')
    finishConfirm({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    await flushPromises()
  })

  it('keeps a coherent READY detail after the overlapping retry transport reports failure', async () => {
    const old = pending({ ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Old reverse-charge evidence.' } } })
    inbox.detail.mockResolvedValue(old)
    let rejectRetry!: (error: unknown) => void
    inbox.retry.mockReturnValueOnce(new Promise((_, reject) => { rejectRetry = reject }))
    let rejectConfirm!: (error: unknown) => void
    inbox.confirm.mockReturnValueOnce(new Promise((_, reject) => { rejectConfirm = reject }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat')
    await wrapper.findAll('select')[1].setValue('treat')
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10')
    await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).toHaveBeenCalledTimes(1)
    rejectConfirm(new ApiError(422, { detail: 'invalid confirmation' }, 'Invalid confirmation'))
    await flushPromises()
    rejectRetry(new TypeError('Failed to fetch')); await flushPromises()
    expect(wrapper.text()).toContain('Invalid confirmation')
    expect(wrapper.text()).toContain('Old reverse-charge evidence.')
    expect(wrapper.findAll('button').some(x => x.text() === 'Apply suggestion')).toBe(true)
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('treat')
    expect((wrapper.findAll('button').find(x => x.text() === en.receiptInbox.confirm)!.element as HTMLButtonElement).disabled).toBe(false)
  })

  it('withdraws all unedited AI fields when a fresh file revision is accepted and asks for review', async () => {
    const old = pending({ file_revision: 4, ai_state: 'READY', ai_error_code: null, ai_suggestion: {
      expense_date: '2026-01-02', supplier_name: 'Old supplier', net_amount: '137.16', vat_amount: '28.80', vat_rate_percent: '21',
      suggested_category_id: 'cat', summary: 'Old summary', suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old treatment.' },
    } })
    const fresh = pending({ file_revision: 5, ai_state: 'FAILED', ai_error_code: 'AI_INVALID_RESPONSE', files: [{ id: 'fresh', filename: 'fresh.pdf', mime_type: 'application/pdf', byte_size: 20 }], ai_suggestion: null })
    inbox.detail.mockResolvedValueOnce(old).mockResolvedValueOnce(fresh)
    inbox.confirm.mockRejectedValueOnce(new ApiError(409, { detail: 'FILE_GROUP_CHANGED' }, 'FILE_GROUP_CHANGED')).mockReturnValueOnce(new Promise(() => {}))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain(en.receiptInbox.fileGroupChanged)
    expect(wrapper.text()).toContain('fresh.pdf')
    expect(wrapper.findAll('input[type="number"]').slice(0, 2).map(x => (x.element as HTMLInputElement).value)).toEqual(['', ''])
    expect(wrapper.findAll('input').map(x => (x.element as HTMLInputElement).value)).not.toContain('Old supplier')
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('12'); await wrapper.findAll('input[type="number"]')[1].setValue('2.52')
    await wrapper.findAll('input').find(x => (x.element as HTMLInputElement).type === 'text')?.setValue('Fresh supplier')
    await wrapper.find('form').trigger('submit')
    expect(inbox.confirm).toHaveBeenLastCalledWith('inbox-1', expect.objectContaining({ supplier_name: 'Fresh supplier', net_amount: 12, vat_amount: 2.52 }), 5)
    wrapper.unmount()
  })

  it('preserves an explicitly cleared supplier and manually entered zero amounts across a revision change', async () => {
    const old = pending({ file_revision: 2, ai_state: 'READY', ai_suggestion: { supplier_name: 'Suggested supplier', net_amount: '12', vat_amount: '2.52' } })
    const fresh = pending({ file_revision: 3, ai_state: 'FAILED', ai_suggestion: null })
    inbox.detail.mockResolvedValueOnce(old).mockResolvedValueOnce(fresh)
    inbox.confirm.mockRejectedValueOnce(new ApiError(409, { detail: 'FILE_GROUP_CHANGED' }, 'FILE_GROUP_CHANGED'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    const supplierField = wrapper.findAll('input').find(input => (input.element as HTMLInputElement).value === 'Suggested supplier')!
    await supplierField.setValue('')
    await wrapper.findAll('input[type="number"]')[0].setValue(0); await wrapper.findAll('input[type="number"]')[1].setValue(0)
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect((supplierField.element as HTMLInputElement).value).toBe('')
    expect(wrapper.findAll('input[type="number"]').slice(0, 2).map(input => (input.element as HTMLInputElement).value)).toEqual(['0', '0'])
    wrapper.unmount()
  })

  it('prefers the fresh consistent detail over an earlier cached retry result', async () => {
    const retry = deferred<unknown>(), confirm = deferred<unknown>()
    const latest = pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Committed reverse-charge evidence.' } } })
    inbox.detail.mockResolvedValueOnce(pending()).mockResolvedValueOnce(latest)
    inbox.retry.mockReturnValueOnce(retry.promise); inbox.confirm.mockReturnValueOnce(confirm.promise)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.find('form').trigger('submit')
    retry.resolve(pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Older retry response.' } } }))
    await flushPromises(); confirm.reject(new ApiError(422, 'invalid expense', 'invalid expense')); await flushPromises()
    expect(wrapper.text()).toContain('Committed reverse-charge evidence.')
    expect(wrapper.text()).not.toContain('Older retry response.')
    wrapper.unmount()
  })

  it('preserves a current same-group retry when append is explicitly rejected', async () => {
    const retry = deferred<unknown>()
    inbox.detail.mockResolvedValue(pending({ file_revision: 4, ai_state: 'READY', ai_suggestion: { supplier_name: 'Old supplier', suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old evidence.' } } }))
    inbox.retry.mockReturnValueOnce(retry.promise)
    inbox.append.mockRejectedValueOnce(new ApiError(422, { detail: 'too large' }, 'too large'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    const upload = wrapper.findComponent({ name: 'UploadStub' })
    ;(upload.vm as unknown as { choose: (file: File) => void }).choose(new File(['x'], 'bad.pdf'))
    await nextTick(); await wrapper.find('.append').findAll('button').at(1)!.trigger('click'); await flushPromises()
    retry.resolve(pending({ file_revision: 4, ai_state: 'READY', ai_suggestion: { supplier_name: 'Current supplier', vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Current same-group evidence.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Current same-group evidence.')
    expect(wrapper.text()).not.toContain('Old evidence.')
    expect(wrapper.findAll('input').map(x => (x.element as HTMLInputElement).value)).toContain('Current supplier')
    wrapper.unmount()
  })

  it('stages a retry while the second confirmation is pending and reviews it only after failure', async () => {
    const retry = deferred<unknown>(), secondConfirm = deferred<unknown>()
    const old = pending({ ai_state: 'READY', ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Original evidence.' } } })
    const committed = pending({ updated_at: '2026-01-02T00:00:00Z', ai_state: 'READY', ai_error_code: null, ai_suggestion: { vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Persisted retry evidence.' } } })
    inbox.detail.mockResolvedValueOnce(old).mockResolvedValueOnce(old).mockResolvedValueOnce(committed)
    inbox.retry.mockReturnValueOnce(retry.promise)
    inbox.confirm.mockRejectedValueOnce(new ApiError(422, 'invalid', 'invalid')).mockReturnValueOnce(secondConfirm.promise)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    await wrapper.find('form').trigger('submit')
    retry.resolve(committed); await flushPromises()
    expect(wrapper.text()).toContain('Original evidence.')
    expect(wrapper.text()).not.toContain('Persisted retry evidence.')
    secondConfirm.reject(new ApiError(422, 'invalid', 'invalid')); await flushPromises()
    expect(wrapper.text()).toContain('Persisted retry evidence.')
    wrapper.unmount()
  })

  it('ignores a failed recovery request from a previous inbox route', async () => {
    const recovery = deferred<unknown>(), retry = deferred<unknown>()
    const other = pending({ id: 'inbox-2', file_revision: 2, ai_state: 'READY', ai_error_code: null, ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Second item evidence.' } } })
    inbox.detail.mockResolvedValueOnce(pending()).mockReturnValueOnce(recovery.promise).mockResolvedValueOnce(other)
    inbox.retry.mockReturnValueOnce(retry.promise); inbox.confirm.mockRejectedValueOnce(new ApiError(422, 'invalid', 'invalid'))
    const app = setup('/receipt-inbox/inbox-1'), wrapper = await app.mount()
    await wrapper.findAll('select')[0].setValue('cat'); await wrapper.findAll('select')[1].setValue('treat'); await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10'); await wrapper.findAll('input[type="number"]')[1].setValue('2.1')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    await app.router.push('/receipt-inbox/inbox-2'); await flushPromises()
    recovery.reject(new TypeError('network')); await flushPromises()
    expect(wrapper.text()).toContain('Second item evidence.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    retry.resolve(pending()); wrapper.unmount()
  })

  it('keeps manual VAT after a late preview and clears gross when VAT is edited', async () => {
    inbox.detail.mockResolvedValue(pending())
    let finish!: (value: unknown) => void
    api.post.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10')
    await wrapper.findAll('input[type="number"]')[1].setValue('1.5')
    finish({ vat_amount: '2.10', gross_amount: '12.10' })
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('1.5')
    expect(wrapper.text()).toContain('Calculated by backend after saving.')
    expect(wrapper.text()).not.toContain('12.10')
    api.post.mockResolvedValueOnce({ vat_amount: '2.10', gross_amount: '12.10' })
    await wrapper.findAll('input[type="number"]')[0].setValue('10.01')
    await flushPromises()
    expect(wrapper.text()).not.toContain('12.10')
    await wrapper.findAll('input[type="number"]')[1].setValue('1.6')
    expect(wrapper.text()).not.toContain('12.10')
  })

  it('ignores an older VAT preview when the net amount changes again', async () => {
    inbox.detail.mockResolvedValue(pending())
    let finishOld!: (value: unknown) => void
    api.post.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve }))
    api.post.mockResolvedValueOnce({ vat_amount: '4.20', gross_amount: '24.20' })
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[0].setValue('10')
    await wrapper.findAll('input[type="number"]')[0].setValue('20')
    await flushPromises()
    finishOld({ vat_amount: '2.10', gross_amount: '12.10' })
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('4.2')
    expect(wrapper.text()).toContain('24.20')
    expect(wrapper.text()).not.toContain('12.10')
  })

  it('allows ordinary preview with an unadopted reverse suggestion and restores extracted receipt VAT on selection', async () => {
    inbox.detail.mockResolvedValue(pending({
      ai_state: 'READY', ai_error_code: null,
      ai_suggestion: {
        suggested_category_id: 'cat', net_amount: '42', vat_amount: '0',
        suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Reverse charge on receipt.' },
      },
    }))
    api.post.mockResolvedValueOnce({ vat_amount: '8.82', gross_amount: '50.82' })
    inbox.confirm.mockResolvedValue({ inbox: pending(), expense: { id: 'saved' } })
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    const vatInput = wrapper.findAll('input[type="number"]')[1]
    await selects[1].setValue('treat')
    await selects[2].setValue('rate')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/expenses/calculate', { net_amount: 42, vat_rate_id: 'rate' })
    expect((vatInput.element as HTMLInputElement).value).toBe('8.82')
    await selects[1].setValue('reverse')
    expect((vatInput.element as HTMLInputElement).value).toBe('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'rate', vat_amount: 0,
    }), 0)
    wrapper.unmount()
  })

  it('lets the owner reach records beyond the first hundred', async () => {
    inbox.total = 101
    inbox.items = Array.from({ length: 100 }, (_, i) => pending({ id: `first-${i}` }))
    inbox.fetch.mockImplementation(async (_status: string, offset: number) => { if (offset === 100) inbox.items = [pending({ id: 'oldest' })] })
    const wrapper = await setup('/receipt-inbox').mount()
    await wrapper.findAll('button').find(x => x.text() === 'Next page')!.trigger('click')
    await flushPromises()
    expect(inbox.fetch).toHaveBeenCalledWith('PENDING', 100)
    expect(wrapper.find('a[href="/receipt-inbox/oldest"]').exists()).toBe(true)
  })

  it('keeps AI-off receipts reviewable, applies suggestions as editable values and confirms only raw owner-edited input', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: { expense_date: '2026-02-03', supplier_name: 'AI Shop', net_amount: '10.00', vat_amount: '2.10', vat_rate_percent: '21', suggested_category_id: 'cat', summary: 'model summary' } }))
    inbox.confirm.mockResolvedValue({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    const app = setup('/receipt-inbox/inbox-1'), wrapper = await app.mount()
    expect(wrapper.text()).not.toContain('Some receipt details are missing or uncertain')
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'AI Shop')!.element as HTMLInputElement).value).toBe('AI Shop')
    const supplier = wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'AI Shop')
    expect(supplier).toBeTruthy()
    await supplier!.setValue('Owner correction')
    await wrapper.findAll('select')[1].setValue('treat')
    const inputs = wrapper.findAll('input')
    await inputs.find(x => (x.element as HTMLInputElement).type === 'date')!.setValue('2026-02-04')
    // The confirm action is explicit; merely applying AI never invokes it.
    expect(inbox.confirm).not.toHaveBeenCalled()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const body = inbox.confirm.mock.calls[0][1]
    expect(body).toMatchObject({ expense_date: '2026-02-04', supplier_name: 'Owner correction', category_id: 'cat', vat_rate_id: 'rate', net_amount: 10, vat_amount: 2.1 })
    expect(body).not.toHaveProperty('gross_amount')
  })

  it('shows processed history without linking to an expense that has been deleted', async () => {
    inbox.detail.mockResolvedValue(pending({ status: 'CONFIRMED', confirmed_at: '2026-01-02T00:00:00Z', expense_id: null }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect(wrapper.text()).toContain('Processed')
    expect(wrapper.text()).toContain('The linked expense is unavailable (it may have been deleted).')
    expect(wrapper.find('a[href*="/expenses/"]').exists()).toBe(false)
  })

  it('allows manual completion when AI is unavailable and keeps the confirm action in a loading state', async () => {
    inbox.detail.mockResolvedValue(pending())
    let finish!: (value: unknown) => void
    inbox.confirm.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    const date = wrapper.find('input[type="date"]')
    await date.setValue('2026-03-01')
    const supplier = wrapper.findAll('input').find(x => x.attributes('type') !== 'date' && x.attributes('type') !== 'number')!
    await supplier.setValue('Manual supplier')
    const numbers = wrapper.findAll('input[type="number"]')
    await numbers[0].setValue('15'); await numbers[1].setValue('3.15')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Confirming…')
    expect(inbox.confirm.mock.calls[0][1]).toMatchObject({ expense_date: '2026-03-01', supplier_name: 'Manual supplier', net_amount: 15, vat_amount: 3.15 })
    finish({ inbox: pending({ status: 'CONFIRMED' }), expense: { id: 'expense-1' } })
    await flushPromises()
  })

  it('blocks confirmation when required dictionary selections or amounts are missing', async () => {
    inbox.detail.mockResolvedValue(pending())
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Choose a category, VAT treatment, VAT rate, and enter both amounts.')
    expect(inbox.confirm).not.toHaveBeenCalled()
  })

  it('shows provider failures and keeps the retry action available', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_state: 'FAILED', ai_error_code: 'AI_PROVIDER_ERROR' }))
    inbox.retry.mockResolvedValue(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Retry Shop', net_amount: '22', vat_amount: '4.62' } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect(wrapper.text()).toContain('AI extraction failed')
    expect(wrapper.text()).toContain('AI provider request failed')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect(inbox.retry).toHaveBeenCalledWith('inbox-1', 'en')
    expect(wrapper.text()).toContain('Suggestions ready for review')
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Retry Shop')!.element as HTMLInputElement).value).toBe('Retry Shop')
  })

  it('automatically fills newly available retry fields without replacing manual edits or clearing them on failed retry', async () => {
    inbox.detail.mockResolvedValue(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Initial AI', net_amount: '10' } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Changed AI', net_amount: '12', vat_amount: '2.52' } }))
      .mockResolvedValueOnce(pending({ ai_state: 'FAILED', ai_error_code: 'AI_TIMEOUT', ai_suggestion: null }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const supplier = wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Initial AI')!
    await supplier.setValue('Owner value')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((supplier.element as HTMLInputElement).value).toBe('Owner value')
    expect((wrapper.find('input[type="number"]').element as HTMLInputElement).value).toBe('12')
    await wrapper.findAll('button').find(x => x.text().includes('Retry extraction'))!.trigger('click')
    await flushPromises()
    expect((supplier.element as HTMLInputElement).value).toBe('Owner value')
    expect((wrapper.find('input[type="number"]').element as HTMLInputElement).value).toBe('12')
  })

  it('uses Receipt Inbox labels and a primary Download button for each file', async () => {
    inbox.detail.mockResolvedValue(pending())
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    expect(wrapper.find('h1').text()).toBe('Receipt Inbox')
    const download = wrapper.findAll('a').find(x => x.text() === 'Download')!
    expect(download.attributes('data-variant')).toBe('primary')
    expect(download.attributes('download')).toBe('receipt.jpg')
    expect(download.attributes('href')).toBe('/api/v1/attachments/file-1/content')
    expect(download.find('button').exists()).toBe(false)
    expect(en.nav.receiptInbox).toBe('Receipt Inbox')
  })

  it('surfaces confirmation conflicts with an existing expense recovery route', async () => {
    inbox.detail.mockResolvedValue(pending())
    inbox.confirm.mockRejectedValue(new (await import('../../src/api/http')).ApiError(409, { detail: 'ALREADY_CONFIRMED', expense_id: 'expense-existing' }, 'Conflict'))
    const app = setup('/receipt-inbox/inbox-1'), wrapper = await app.mount()
    // Complete the required fields with native controls and submit.
    const selects = wrapper.findAll('select')
    await selects[0].setValue('cat'); await selects[1].setValue('treat'); await selects[2].setValue('rate')
    const numberInputs = wrapper.findAll('input[type="number"]')
    await numberInputs[0].setValue('10'); await numberInputs[1].setValue('2.1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('This receipt was already confirmed.')
    expect(wrapper.find('a[href="/expenses/expense-existing/edit?tab=purchase"]').exists()).toBe(true)
  })

  it('accepts a verified READY snapshot and applies its automatic prefill during 409 recovery', async () => {
    const previous = pending({ file_revision: 4, ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Old supplier', net_amount: '137.16', vat_amount: '28.80', suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old group.' } } })
    const current = pending({ file_revision: 5, updated_at: '2026-01-03T00:00:00Z', ai_state: 'READY', ai_error_code: null, files: [{ id: 'new', filename: 'new.pdf', mime_type: 'application/pdf', byte_size: 12 }], ai_suggestion: { supplier_name: 'Current supplier', net_amount: '42', vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Current group.' } } })
    inbox.detail.mockResolvedValueOnce(previous).mockResolvedValueOnce(current)
    inbox.confirm.mockRejectedValueOnce(new ApiError(409, { detail: 'FILE_GROUP_CHANGED' }, 'FILE_GROUP_CHANGED'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[0].setValue('cat')
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).not.toContain(en.receiptInbox.fileGroupUnverified)
    expect(wrapper.text()).toContain('Current group.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('42')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
    wrapper.unmount()
  })

  it('releases same-group retry staging when confirmation recovery GET fails', async () => {
    const retry = deferred<ReturnType<typeof pending>>()
    const previous = pending({ file_revision: 4, ai_state: 'READY', ai_error_code: null, ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old evidence.' } } })
    inbox.detail.mockResolvedValueOnce(previous).mockRejectedValueOnce(new TypeError('GET failed'))
    inbox.confirm.mockRejectedValueOnce(new ApiError(422, { detail: 'invalid' }, 'invalid'))
    inbox.retry.mockReturnValueOnce(retry.promise)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('select')[1].setValue('reverse')
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    retry.resolve(pending({ file_revision: 4, updated_at: '2026-01-02T00:00:00Z', ai_state: 'READY', ai_error_code: null, ai_suggestion: { suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Current retry evidence.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Current retry evidence.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect(wrapper.findAll('button').some(x => x.text() === en.expenses.ai.applyTreatment)).toBe(false)
    wrapper.unmount()
  })

  it('does not let a retry started before an ambiguous append unlock its old snapshot', async () => {
    const retry = deferred<ReturnType<typeof pending>>()
    const old = pending({ file_revision: 4, ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Old supplier', net_amount: '20', vat_amount: '4.2', suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Old group.' } } })
    inbox.detail.mockResolvedValueOnce(old).mockRejectedValueOnce(new TypeError('Recovery GET failed'))
    inbox.retry.mockReturnValueOnce(retry.promise)
    inbox.append.mockRejectedValueOnce(new TypeError('Append response lost'))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(new File(['x'], 'new.pdf'))
    await nextTick()
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.append)!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain(en.receiptInbox.fileGroupUnverified)
    retry.resolve({ ...old, updated_at: '2026-01-02T00:00:00Z' })
    await flushPromises()
    expect(wrapper.text()).toContain(en.receiptInbox.fileGroupUnverified)
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('20')
    expect(wrapper.findAll('button').find(x => x.text() === en.receiptInbox.confirm)?.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('compares extraction timestamps at microsecond precision across UTC ISO forms', async () => {
    const retry = deferred<ReturnType<typeof pending>>()
    const running = pending({ file_revision: 4, ai_state: 'RUNNING', ai_error_code: null, updated_at: '2026-01-03T00:00:00.123100+00:00' })
    inbox.detail.mockResolvedValueOnce(pending({ file_revision: 4 })).mockResolvedValueOnce(running)
    inbox.confirm.mockRejectedValueOnce(new ApiError(422, { detail: 'invalid' }, 'invalid'))
    inbox.retry.mockReturnValueOnce(retry.promise)
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    retry.resolve(pending({ file_revision: 4, updated_at: '2026-01-03T00:00:00.123900Z', ai_state: 'READY', ai_error_code: null, ai_suggestion: { suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Later by 800 microseconds.' } } }))
    await flushPromises()
    expect(wrapper.text()).toContain('Later by 800 microseconds.')
    expect(wrapper.text()).not.toContain(en.receiptInbox.ai.RUNNING)
    wrapper.unmount()
  })

  it('keeps a manually edited receipt VAT when ordinary preview returns a different VAT', async () => {
    inbox.detail.mockResolvedValueOnce(pending({ file_revision: 4, ai_state: 'READY', ai_error_code: null, ai_suggestion: { net_amount: '137.16', vat_amount: '28.80', supplier_name: 'Supplier' } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    await wrapper.findAll('input[type="number"]')[1].setValue('15')
    api.post.mockResolvedValueOnce({ vat_amount: '28.80', gross_amount: '165.96' })
    await wrapper.findAll('select')[2].setValue('rate')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('15')
    expect(wrapper.find('strong').text()).toBe(en.expenses.grossHint)
    wrapper.unmount()
  })

  it('keeps ordinary partial values but blocks reverse confirmation after a current snapshot omits VAT without a treatment reference', async () => {
    inbox.detail.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, ai_suggestion: {
      expense_date: '2026-01-02', supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42',
      vat_amount: '3.3', vat_rate_percent: '21',
      suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT was charged.' },
    } }))
    inbox.retry.mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, updated_at: '2026-01-02T00:00:00Z', ai_suggestion: {
      expense_date: '2026-01-02', supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42',
    } })).mockResolvedValueOnce(pending({ ai_state: 'READY', ai_error_code: null, updated_at: '2026-01-03T00:00:00Z', ai_suggestion: {
      expense_date: '2026-01-02', supplier_name: 'Supplier', suggested_category_id: 'cat', net_amount: '42', vat_amount: '0.75',
    } }))
    inbox.confirm.mockResolvedValue({ inbox: pending(), expense: { id: 'saved' } })
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const selects = wrapper.findAll('select')
    await selects[1].setValue('treat')
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    await flushPromises()
    const vatInput = wrapper.findAll('input[type="number"]')[1]
    expect((vatInput.element as HTMLInputElement).value).toBe('3.3')
    await selects[1].setValue('reverse')
    expect((vatInput.element as HTMLInputElement).value).toBe('')
    await selects[2].setValue('rate')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(x => x.text() === en.receiptInbox.retry)!.trigger('click')
    await flushPromises()
    expect((vatInput.element as HTMLInputElement).value).toBe('0.75')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'rate', net_amount: 42, vat_amount: 0.75,
    }), 0)
    wrapper.unmount()
  })
})
