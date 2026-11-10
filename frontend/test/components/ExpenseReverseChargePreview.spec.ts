import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent } from 'vue'
import en from '../../src/locales/en.json'

const mocks = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), getExpense: vi.fn(), listAttachments: vi.fn(), aiExtract: vi.fn(),
  updateExpense: vi.fn(), createExpense: vi.fn(), uploadAttachment: vi.fn(), deleteAttachment: vi.fn(),
}))
vi.mock('../../src/api/http', () => ({ get: mocks.get, post: mocks.post, ApiError: class extends Error {} }))
vi.mock('../../src/stores/expenses', () => ({ useExpensesStore: () => mocks }))
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div><slot /><slot name="icon" /></div>' }
  const field = { props: ['label'], template: '<div :data-label="label"><slot /></div>' }
  const input = { props: ['value'], emits: ['update:value'], template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />' }
  const number = { props: ['value'], emits: ['update:value'], template: '<input type="number" :value="value" @input="$emit(\'update:value\', $event.target.value === \'\' ? null : Number($event.target.value))" />' }
  const select = { props: ['value', 'options'], emits: ['update:value'], template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><option value=""></option><option v-for="o in options" :key="o.value" :value="o.value">{{o.label}}</option></select>' }
  const button = { props: ['loading', 'disabled', 'attrType', 'text'], template: '<button :type="attrType || \'button\'" :disabled="loading || disabled"><slot /></button>' }
  const check = { props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" />' }
  const upload = defineComponent({ name: 'UploadStub', props: ['customRequest'], setup(props) { return { send: () => props.customRequest?.({ file: { file: new File(['x'], 'new.pdf', { type: 'application/pdf' }) } }) } }, template: '<div><button type="button" @click="send">Upload test file</button><slot /></div>' })
  return { NForm: { emits: ['submit'], template: '<form @submit="$emit(\'submit\', $event)"><slot /></form>' }, NFormItem: field, NInput: input, NInputNumber: number, NSelect: select, NCheckbox: check, NButton: button, NEmpty: box, NAlert: box, NCard: box, NIcon: box, NPagination: box, NPopconfirm: box, NRadioGroup: box, NRadioButton: box, NSpace: box, NSpin: box, NSwitch: check, NTag: box, NText: box, NUpload: upload, NDatePicker: box, NDataTable: box, NModal: box }
})

import ExpenseEdit from '../../src/views/expenses/ExpenseEdit.vue'

const attachment = { id: 'attachment-1', filename: 'receipt.pdf', mime_type: 'application/pdf', byte_size: 10 }
const expense = (id: string, patch: Record<string, unknown> = {}) => ({
  id, expense_date: '2026-01-02', category_id: 'cat', supplier_name: `Supplier ${id}`,
  vat_treatment_id: 'tax', vat_rate_id: 'rate', net_amount: '10', vat_amount: '2.1', gross_amount: '12.1',
  deductible: true, paid_by: 'BUSINESS', business_percentage: '100', depreciation_years: 1,
  include_in_pl: true, pl_cost_basis: 'AUTO', is_draft: false, ...patch,
})
function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}

async function setup(path = '/expenses/one/edit') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/expenses/:id/edit', component: ExpenseEdit },
    { path: '/expenses/new', component: ExpenseEdit },
  ] })
  await router.push(path); await router.isReady()
  const wrapper = mount(ExpenseEdit, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { NDatePicker: true } } })
  await flushPromises()
  return { router, wrapper }
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.get.mockImplementation(async (path: string) => {
    if (path.endsWith('/expense-categories')) return { items: [{ id: 'cat', name: 'Office', default_deductible: true }] }
    if (path.endsWith('/vat-treatments')) return { items: [
      { id: 'tax', code: 'NL_DOMESTIC_PURCH', label: 'Domestic', side: 'PURCHASE', effect: 'APPLY_RATE', active: true },
      { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', label: 'EU reverse', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true },
    ] }
    return { items: [{ id: 'rate', percent: '21', label: 'Standard' }, { id: 'alt-rate', percent: '9', label: 'Reduced' }] }
  })
  mocks.getExpense.mockImplementation(async (id: string) => expense(id))
  mocks.listAttachments.mockResolvedValue([attachment])
  mocks.updateExpense.mockImplementation(async (id: string, body: unknown) => ({ ...expense(id), ...(body as object) }))
  mocks.createExpense.mockResolvedValue(expense('created'))
  mocks.aiExtract.mockResolvedValue({})
  mocks.post.mockResolvedValue({ vat_amount: '2.1', gross_amount: '12.1' })
})


it('previews while an unadopted reverse-charge suggestion is pending, then restores receipt VAT on Apply', async () => {
  mocks.aiExtract.mockResolvedValue({
    vat_amount: '0', net_amount: '100', vat_rate_percent: null,
    suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
  })
  mocks.post.mockResolvedValue({ vat_amount: '9', gross_amount: '109' })
  const { wrapper } = await setup()
  await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
  await flushPromises()
  const vatInput = wrapper.findAll('input[type=number]')[1]
  expect((vatInput.element as HTMLInputElement).value).toBe('0')
  expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('tax')
  expect(wrapper.text()).toContain(en.expenses.ai.nominalRateReview)
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect(mocks.post).toHaveBeenCalledTimes(1)
  expect((vatInput.element as HTMLInputElement).value).toBe('9')
  await wrapper.findAll('button').find(x => x.text() === en.expenses.ai.applyTreatment)!.trigger('click')
  expect((vatInput.element as HTMLInputElement).value).toBe('0')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
    vat_treatment_id: 'reverse', vat_rate_id: 'alt-rate', net_amount: 100, vat_amount: 0,
  }))
  wrapper.unmount()
})

it('restores the saved receipt VAT after a preview when reverse charge is selected manually', async () => {
  mocks.post.mockResolvedValue({ vat_amount: '9', gross_amount: '109' })
  const { wrapper } = await setup()
  const vatInput = wrapper.findAll('input[type=number]')[1]
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vatInput.element as HTMLInputElement).value).toBe('9')
  await wrapper.findAll('select')[1].setValue('reverse')
  expect((vatInput.element as HTMLInputElement).value).toBe('2.1')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_amount: 2.1 }))
  wrapper.unmount()
})

it('restores a manually cleared receipt VAT after an ordinary preview before reverse charge', async () => {
  mocks.post.mockResolvedValueOnce({ vat_amount: '0.9', gross_amount: '10.9' })
  const { wrapper } = await setup()
  const vatInput = wrapper.findAll('input[type=number]')[1]
  await vatInput.setValue('')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vatInput.element as HTMLInputElement).value).toBe('0.9')

  await wrapper.findAll('select')[1].setValue('reverse')
  expect((vatInput.element as HTMLInputElement).value).toBe('')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('requires explicit VAT review when reverse charge withdraws an ordinary preview with an empty baseline', async () => {
  const { wrapper } = await setup('/expenses/new')
  await wrapper.findAll('select')[0].setValue('cat')
  await wrapper.findAll('input[type=number]')[0].setValue('100')
  await wrapper.findAll('select')[2].setValue('rate')
  await flushPromises()
  expect((wrapper.findAll('input[type=number]')[1].element as HTMLInputElement).value).toBe('2.1')

  const treatment = wrapper.findAll('select')[1]
  await treatment.setValue('reverse')
  const vatInput = wrapper.findAll('input[type=number]')[1]
  expect((vatInput.element as HTMLInputElement).value).toBe('')
  expect(wrapper.text()).not.toContain(en.expenses.ai.withdrawnVatRequired)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.createExpense).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain(en.expenses.ai.reverseVatRequired)

  await treatment.setValue('tax')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.createExpense).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain(en.expenses.ai.withdrawnVatRequired)
  wrapper.unmount()
})

it('keeps an ordinary treatment editable when a reverse suggestion has no extracted VAT', async () => {
  mocks.aiExtract.mockResolvedValue({
    net_amount: '100', vat_rate_percent: null,
    suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
  })
  const { wrapper } = await setup()
  await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
  await flushPromises()
  const vatInput = wrapper.findAll('input[type=number]')[1]
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect(mocks.post).toHaveBeenCalledTimes(1)
  expect((vatInput.element as HTMLInputElement).value).toBe('2.1')
  wrapper.unmount()
})

it('requires explicit VAT entry for missing reverse-charge receipt VAT while accepting manual zero', async () => {
  mocks.aiExtract
    .mockResolvedValueOnce({ net_amount: '100', vat_amount: '3.3' })
    .mockResolvedValueOnce({
      net_amount: '100',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Reverse charge is stated.' },
    })
  const { wrapper } = await setup()
  const extract = wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!
  await extract.trigger('click'); await flushPromises()
  await extract.trigger('click'); await flushPromises()
  await wrapper.findAll('button').find(x => x.text() === en.expenses.ai.applyTreatment)!.trigger('click')
  expect((wrapper.findAll('input[type=number]')[1].element as HTMLInputElement).value).toBe('')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain(en.expenses.ai.reverseVatRequired)
  await wrapper.findAll('input[type=number]')[1].setValue('0')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_amount: 0 }))
  wrapper.unmount()
})

it('does not carry AI receipt VAT from a previous attachment into the new attachment', async () => {
  mocks.aiExtract.mockResolvedValue({ net_amount: '100', vat_amount: '3.3' })
  mocks.uploadAttachment.mockResolvedValue({ ...attachment, id: 'attachment-2', filename: 'new.pdf' })
  const { wrapper } = await setup()
  await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
  await flushPromises()
  const vatInput = wrapper.findAll('input[type=number]')[1]
  expect((vatInput.element as HTMLInputElement).value).toBe('3.3')
  await wrapper.findAll('button').find(x => x.text() === 'Upload test file')!.trigger('click')
  await flushPromises()
  expect((vatInput.element as HTMLInputElement).value).toBe('')
  await wrapper.findAll('select')[1].setValue('reverse')
  expect((vatInput.element as HTMLInputElement).value).toBe('')
  wrapper.unmount()
})
