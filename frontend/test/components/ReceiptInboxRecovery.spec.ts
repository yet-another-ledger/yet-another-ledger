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
  return { NAlert: box, NButton: button, NCard: box, NCheckbox: checkbox, NDatePicker: box, NEmpty: box, NForm: form, NFormItem: box, NInput: input, NInputNumber: number, NSelect: select, NSpin: box, NSpace: box, NTag: box, NUpload: upload }
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
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b })
  return { promise, resolve, reject }
}

const old = (rev = 4) => pending({
  file_revision: rev, ai_state: 'READY', ai_error_code: null,
  ai_suggestion: {
    suggested_category_id: 'cat', supplier_name: 'Supplier from old group',
    net_amount: '137.16', vat_amount: '28.80', vat_rate_percent: '21',
    suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Domestic evidence from old group.' },
  },
})
const newGroup = (patch: Record<string, unknown> = {}) => pending({
  file_revision: 5, ai_state: 'FAILED', ai_error_code: 'AI_INVALID_RESPONSE',
  files: [{ id: 'replacement', filename: 'different-transaction.jpg', mime_type: 'image/jpeg', byte_size: 12 }],
  ...patch,
})
const reverse = (reason: string) => ({ id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason })
const btn = (wrapper: Awaited<ReturnType<ReturnType<typeof setup>['mount']>>, label: string) => wrapper.findAll('button').find(button => button.text() === label)!
const validation = () => new ApiError(422, { detail: 'Amount outside allowed range' }, 'Amount outside allowed range')

beforeEach(() => {
  vi.resetAllMocks()
  inbox.fetch.mockResolvedValue(undefined)
  dicts.load.mockResolvedValue(undefined)
  api.post.mockRejectedValue(new Error('preview unavailable'))
})

const reverseReady = (revision: number, reason: string, updatedAt = '2026-01-03T00:00:00Z') => newGroup({
  file_revision: revision, ai_state: 'READY', ai_error_code: null, updated_at: updatedAt,
  ai_suggestion: {
    suggested_category_id: 'cat', supplier_name: 'Current group supplier', net_amount: '42.00', vat_amount: '0',
    suggested_vat_treatment: reverse(reason),
  },
})
const values = (wrapper: Awaited<ReturnType<ReturnType<typeof setup>['mount']>>) =>
  wrapper.findAll('input[type=number]').slice(0, 2).map(x => (x.element as HTMLInputElement).value)
const treatment = (wrapper: Awaited<ReturnType<ReturnType<typeof setup>['mount']>>) =>
  (wrapper.findAll('select')[1].element as HTMLSelectElement).value


it('keeps extracted reverse-charge receipt VAT when a nominal rate is selected before Apply', async () => {
  inbox.detail.mockResolvedValueOnce(old())
  inbox.retry.mockResolvedValueOnce(reverseReady(4, 'The supplier stated reverse charge.'))
  inbox.confirm.mockReturnValueOnce(new Promise(() => {}))
  const wrapper = await setup('/receipt-inbox/inbox-1').mount()
  await wrapper.findAll('select')[1].setValue('treat')
  await btn(wrapper, en.receiptInbox.retry).trigger('click')
  await flushPromises()
  expect(values(wrapper)).toEqual(['42', '0'])
  expect(treatment(wrapper)).toBe('treat')
  await wrapper.findAll('select')[2].setValue('rate')
  await flushPromises()
  expect(api.post).toHaveBeenCalledWith('/api/v1/expenses/calculate', { net_amount: 42, vat_rate_id: 'rate' })
  expect(values(wrapper)).toEqual(['42', '0'])
  await btn(wrapper, en.expenses.ai.applyTreatment).trigger('click')
  expect(treatment(wrapper)).toBe('reverse')
  await wrapper.find('form').trigger('submit')
  expect(inbox.confirm).toHaveBeenLastCalledWith('inbox-1', expect.objectContaining({
    vat_treatment_id: 'reverse', vat_rate_id: 'rate', net_amount: 42, vat_amount: 0,
  }), 4)
  wrapper.unmount()
})

it('accepts confirmed detail after ambiguous confirmation with an in-flight retry', async () => {
  const retry = deferred()
  const confirmed = pending({
    file_revision: 4, status: 'CONFIRMED', expense_id: 'saved-expense',
    updated_at: '2026-01-05T00:00:00Z', confirmed_at: '2026-01-05T00:00:00Z', files: [],
  })
  inbox.detail.mockResolvedValueOnce(old()).mockResolvedValueOnce(confirmed)
  inbox.confirm.mockRejectedValueOnce(new TypeError('Confirmation response lost after commit'))
  // The extraction conditional update misses the confirmed row. The API then returns current detail.
  inbox.retry.mockReturnValueOnce(retry.promise)
  const wrapper = await setup('/receipt-inbox/inbox-1').mount()
  await wrapper.findAll('select')[1].setValue('reverse')
  await wrapper.findAll('select')[2].setValue('rate')
  await btn(wrapper, en.receiptInbox.retry).trigger('click')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(inbox.detail).toHaveBeenCalledTimes(2)
  retry.resolve(confirmed)
  await flushPromises()
  expect(wrapper.find('form').exists()).toBe(false)
  expect(wrapper.find('a[href="/expenses/saved-expense/edit?tab=purchase"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('Domestic evidence from old group.')
  wrapper.unmount()
})

it('verifies ambiguous confirmation without retry and leaves a real 422 unconfirmed', async () => {
  const confirmed = pending({
    file_revision: 4, status: 'CONFIRMED', expense_id: 'saved-expense',
    updated_at: '2026-01-05T00:00:00Z', confirmed_at: '2026-01-05T00:00:00Z', files: [],
  })
  inbox.detail.mockResolvedValueOnce(old()).mockResolvedValueOnce(confirmed)
  inbox.confirm.mockRejectedValueOnce(new TypeError('Confirmation response lost after commit'))
  const wrapper = await setup('/receipt-inbox/inbox-1').mount()
  await wrapper.findAll('select')[1].setValue('reverse')
  await wrapper.findAll('select')[2].setValue('rate')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(inbox.detail).toHaveBeenCalledTimes(2)
  expect(wrapper.find('form').exists()).toBe(false)
  expect(wrapper.find('a[href="/expenses/saved-expense/edit?tab=purchase"]').exists()).toBe(true)
  wrapper.unmount()

  vi.resetAllMocks()
  inbox.fetch.mockResolvedValue(undefined)
  dicts.load.mockResolvedValue(undefined)
  api.post.mockRejectedValue(new Error('preview unavailable'))
  inbox.detail.mockResolvedValueOnce(old())
  inbox.confirm.mockRejectedValueOnce(validation())
  const invalid = await setup('/receipt-inbox/inbox-1').mount()
  await invalid.findAll('select')[1].setValue('reverse')
  await invalid.findAll('select')[2].setValue('rate')
  await invalid.find('form').trigger('submit')
  await flushPromises()
  expect(inbox.detail).toHaveBeenCalledTimes(1)
  expect(invalid.find('form').exists()).toBe(true)
  expect(invalid.find('a[href="/expenses/saved-expense/edit?tab=purchase"]').exists()).toBe(false)
  invalid.unmount()
})

it('keeps the earlier freshness threshold after a later confirmation recovery GET fails', async () => {
  const retry = deferred()
  const older = {
    ...old(), updated_at: '2026-01-02T00:00:00Z',
    ai_suggestion: { ...old().ai_suggestion, suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Superseded retry.' } },
  }
  const newer = {
    ...old(), updated_at: '2026-01-03T00:00:00Z',
    ai_suggestion: { ...old().ai_suggestion, suggested_vat_treatment: reverse('Verified newer retry.') },
  }
  inbox.detail.mockResolvedValueOnce(old()).mockResolvedValueOnce(newer).mockRejectedValueOnce(new TypeError('Recovery GET unavailable'))
  inbox.confirm.mockRejectedValueOnce(validation()).mockRejectedValueOnce(validation())
  inbox.retry.mockReturnValueOnce(retry.promise)
  const wrapper = await setup('/receipt-inbox/inbox-1').mount()
  await wrapper.findAll('select')[1].setValue('reverse')
  await wrapper.findAll('select')[2].setValue('rate')
  await btn(wrapper, en.receiptInbox.retry).trigger('click')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(wrapper.text()).toContain('Verified newer retry.')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(inbox.detail).toHaveBeenCalledTimes(3)
  retry.resolve(older)
  await flushPromises()
  expect(wrapper.text()).toContain('Verified newer retry.')
  expect(wrapper.text()).not.toContain('Superseded retry.')
  expect(treatment(wrapper)).toBe('reverse')
  wrapper.unmount()
})

it('clears stale AI receipt VAT for a current reverse suggestion with missing VAT, then accepts manual zero', async () => {
  const original = pending({
    file_revision: 4, ai_state: 'READY', ai_error_code: null,
    ai_suggestion: {
      suggested_category_id: 'cat', net_amount: '42', vat_amount: '3.3', vat_rate_percent: '21',
      suggested_vat_treatment: { id: 'treat', code: 'NL_DOMESTIC_PURCH', reason: 'Domestic VAT.' },
    },
  })
  const current = pending({
    file_revision: 4, ai_state: 'READY', ai_error_code: null, updated_at: '2026-01-02T00:00:00Z',
    ai_suggestion: {
      suggested_category_id: 'cat', net_amount: '42', vat_rate_percent: null,
      suggested_vat_treatment: reverse('Reverse charge; receipt VAT amount is absent.'),
    },
  })
  inbox.detail.mockResolvedValueOnce(original)
  inbox.retry.mockResolvedValueOnce(current)
  inbox.confirm.mockReturnValueOnce(new Promise(() => {}))
  const wrapper = await setup('/receipt-inbox/inbox-1').mount()
  expect(values(wrapper)[1]).toBe('3.3')
  await btn(wrapper, en.receiptInbox.retry).trigger('click')
  await flushPromises()
  expect(treatment(wrapper)).toBe('reverse')
  expect(values(wrapper)[1]).toBe('')
  await wrapper.findAll('select')[2].setValue('rate')
  await wrapper.findAll('input[type="number"]')[1].setValue('0')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(inbox.confirm).toHaveBeenCalledWith('inbox-1', expect.objectContaining({
    vat_treatment_id: 'reverse', vat_rate_id: 'rate', vat_amount: 0,
  }), 4)
  wrapper.unmount()
})
