import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const inbox = vi.hoisted(() => ({
  fetch: vi.fn(), detail: vi.fn(), upload: vi.fn(), append: vi.fn(), removeFile: vi.fn(), discard: vi.fn(), retry: vi.fn(), confirm: vi.fn(),
  items: [] as any[], total: 0, loading: false, error: null as string | null,
}))
const api = vi.hoisted(() => ({ post: vi.fn() }))
const dicts = vi.hoisted(() => ({
  categories: { value: [{ id: 'cat', name: 'Office', default_deductible: true }, { id: 'private', name: 'Private', default_deductible: false }] }, rates: { value: [{ id: 'rate', percent: '21' }] },
  categoryOptions: { value: [{ label: 'Office', value: 'cat' }, { label: 'Private', value: 'private' }] }, purchaseTreatmentOptions: { value: [{ label: 'DOMESTIC', value: 'treat' }] },
  vatRateOptions: { value: [{ label: '21%', value: 'rate' }] }, load: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('../../src/stores/receiptInbox', () => ({ useReceiptInboxStore: () => inbox }))
vi.mock('../../src/api/http', () => ({ ApiError: class ApiError extends Error { constructor(public status:number, public detail:unknown, message:string) { super(message) } }, post: api.post }))
vi.mock('../../src/composables/usePurchaseExpenseDictionaries', () => ({ toExpenseInput: (fields: unknown) => fields, usePurchaseExpenseDictionaries: () => dicts }))
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="icon" /></div>' }
  const button = { inheritAttrs: false, props: ['disabled', 'type', 'attrType', 'tag', 'href', 'download'], template: '<component :is="tag || \'button\'" v-bind="$attrs" :data-variant="type" :href="href" :download="download" :disabled="disabled" :type="attrType"><slot /></component>' }
  const input = { inheritAttrs: false, props: ['value'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" @input="$emit(\'update:value\',$event.target.value)" />' }
  const select = { inheritAttrs: false, props: ['value', 'options'], emits: ['update:value'], template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\',$event.target.value)"><option v-for="x in options" :key="x.value" :value="x.value">{{x.label}}</option></select>' }
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
const pending = (patch: Record<string, unknown> = {}) => ({ id: 'inbox-1', status: 'PENDING', files: [{ id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 }], ai_state: 'UNAVAILABLE', ai_suggestion: null, ai_error_code: 'AI_DISABLED', expense_id: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', confirmed_at: null, ...patch })

function setup(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/receipt-inbox', component: ReceiptInbox }, { path: '/receipt-inbox/:id', component: ReceiptInbox }, { path: '/expenses/:id/edit', component: { template: '<div />' } }] })
  return { router, mount: async () => { await router.push(path); await router.isReady(); const wrapper = mount(ReceiptInbox, { global: { plugins: [router, i18n] } }); await flushPromises(); return wrapper } }
}

describe('ReceiptInbox review workflow', () => {
  beforeEach(() => { vi.clearAllMocks(); inbox.items = []; inbox.total = 0; inbox.fetch.mockResolvedValue(undefined); dicts.load.mockResolvedValue(undefined); api.post.mockResolvedValue({ vat_amount: '2.10', gross_amount: '12.10' }) })

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
    inbox.append.mockImplementation(async (_id: string, files: File[]) => pending({ files: [{ id: 'file-1', filename: 'receipt.jpg', mime_type: 'image/jpeg', byte_size: 42 }, ...files.map((file, i) => ({ id: `${file.name}-${i}`, filename: file.name, mime_type: file.type, byte_size: file.size }))], ai_state: 'READY', ai_error_code: null, ai_suggestion: { supplier_name: 'Updated suggestion' } }))
    const wrapper = await setup('/receipt-inbox/inbox-1').mount()
    const first = new File(['a'], 'first.jpg', { type: 'image/jpeg' })
    const second = new File(['b'], 'second.jpg', { type: 'image/jpeg' })
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(first)
    await flushPromises()
    await wrapper.findAll('button').find(x => x.text() === 'Add files')!.trigger('click')
    await flushPromises()
    expect(inbox.append.mock.calls[0][1]).toEqual([first])
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Updated suggestion')!.element as HTMLInputElement).value).toBe('Updated suggestion')
    ;(wrapper.findComponent({ name: 'UploadStub' }).vm as unknown as { choose: (file: File) => void }).choose(second)
    await flushPromises()
    await wrapper.findAll('button').find(x => x.text() === 'Add files')!.trigger('click')
    await flushPromises()
    expect(inbox.append.mock.calls[0][1]).toEqual([first])
    expect(inbox.append.mock.calls[1][1]).toEqual([second])
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
    expect(wrapper.text()).toContain('12.10')
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
})
