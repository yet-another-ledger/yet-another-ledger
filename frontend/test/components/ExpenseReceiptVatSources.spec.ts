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
  const upload = defineComponent({ name: 'UploadStub', props: ['customRequest', 'disabled'], setup(props) { return { send: () => props.customRequest?.({ file: { file: new File(['x'], 'new.pdf', { type: 'application/pdf' }) } }) } }, template: '<div><button type="button" :disabled="disabled" @click="send">Upload test file</button><slot /></div>' })
  const popconfirm = { emits: ['positive-click'], template: '<div><slot name="trigger" /><button class="confirm-popconfirm" type="button" @click="$emit(\'positive-click\')">Confirm</button><slot /></div>' }
  return { NForm: { emits: ['submit'], template: '<form @submit="$emit(\'submit\', $event)"><slot /></form>' }, NFormItem: field, NInput: input, NInputNumber: number, NSelect: select, NCheckbox: check, NButton: button, NEmpty: box, NAlert: box, NCard: box, NIcon: box, NPagination: box, NPopconfirm: popconfirm, NRadioGroup: box, NRadioButton: box, NSpace: box, NSpin: box, NSwitch: check, NTag: box, NText: box, NUpload: upload, NDatePicker: box, NDataTable: box, NModal: box }
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
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function setup(path = '/expenses/one/edit') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/expenses/:id/edit', component: ExpenseEdit }] })
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
async function extractRow(wrapper: Awaited<ReturnType<typeof setup>>['wrapper'], index: number) {
  await wrapper.findAll('.attach-item')[index].findAll('button').find(
    button => button.text().includes(en.expenses.ai.autoFill),
  )!.trigger('click')
  await flushPromises()
}

it('withdraws VAT calculated from removed A net without withdrawing the present B receipt VAT', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'remaining.pdf' }
  mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB]).mockResolvedValueOnce([attachmentB])
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100' }).mockResolvedValueOnce({ vat_amount: '7' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
  mocks.deleteAttachment.mockRejectedValueOnce(new TypeError('Delete B response lost'))
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  await extractRow(wrapper, 1)
  const [net, vat] = wrapper.findAll('input[type="number"]')
  expect((net.element as HTMLInputElement).value).toBe('100')
  expect((vat.element as HTMLInputElement).value).toBe('7')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect(mocks.post).toHaveBeenCalledWith('/api/v1/expenses/calculate', { net_amount: 100, vat_rate_id: 'alt-rate' })
  expect((vat.element as HTMLInputElement).value).toBe('9')

  await wrapper.findAll('.attach-item')[1].find('.confirm-popconfirm').trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.attach-name').map(x => x.text())).toEqual(['remaining.pdf'])
  expect((net.element as HTMLInputElement).value).toBe('')
  expect((vat.element as HTMLInputElement).value).toBe('7')
  await net.setValue('10')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
    net_amount: 10,
    vat_amount: 7,
  }))
  wrapper.unmount()
})

it('keeps human receipt VAT when the attachment basis for a later ordinary calculation is removed', async () => {
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
  mocks.deleteAttachment.mockResolvedValueOnce(undefined)
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  const [net, vat] = wrapper.findAll('input[type="number"]')
  await vat.setValue('7')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect(mocks.post).toHaveBeenCalledWith('/api/v1/expenses/calculate', { net_amount: 100, vat_rate_id: 'alt-rate' })
  await wrapper.find('.confirm-popconfirm').trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.attach-name').map(x => x.text())).toEqual([])
  expect((net.element as HTMLInputElement).value).toBe('')
  await net.setValue('10')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('7')
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 10, vat_amount: 7 }))
  wrapper.unmount()
})

it('keeps an ordinary preview when only its receipt VAT baseline attachment is removed', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'net.pdf' }
  mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB])
  mocks.aiExtract.mockResolvedValueOnce({ vat_amount: '7' }).mockResolvedValueOnce({ net_amount: '100' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
  mocks.deleteAttachment.mockResolvedValueOnce(undefined)
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  await extractRow(wrapper, 1)
  const vat = wrapper.findAll('input[type="number"]')[1]
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('9')

  await wrapper.findAll('.attach-item')[0].find('.confirm-popconfirm').trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.attach-name').map(x => x.text())).toEqual(['net.pdf'])
  expect((vat.element as HTMLInputElement).value).toBe('9')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 100, vat_amount: 9 }))
  wrapper.unmount()
})

it('keeps a completed preview tied to its original attachment after manual net editing', async () => {
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '7' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
  mocks.deleteAttachment.mockResolvedValueOnce(undefined)
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  const [net, vat] = wrapper.findAll('input[type="number"]')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('9')

  await net.setValue('10')
  await wrapper.find('.confirm-popconfirm').trigger('click')
  await flushPromises()
  expect((net.element as HTMLInputElement).value).toBe('10')
  expect((vat.element as HTMLInputElement).value).toBe('')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).not.toHaveBeenCalled()
  wrapper.unmount()
})

it.each(['pending', 'unverified'] as const)(
  'blocks saving while the completed preview net source is being deleted or uncertain (%s)',
  async stage => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'vat.pdf' }
    mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB]).mockRejectedValueOnce(new TypeError('list offline'))
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100' }).mockResolvedValueOnce({ vat_amount: '7' })
    mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
    const deletion = deferred()
    mocks.deleteAttachment.mockReturnValueOnce(deletion.promise)
    const { wrapper } = await setup()
    try {
      await extractRow(wrapper, 0)
      await extractRow(wrapper, 1)
      const [net, vat] = wrapper.findAll('input[type="number"]')
      await wrapper.findAll('select')[2].setValue('alt-rate')
      await flushPromises()
      expect((vat.element as HTMLInputElement).value).toBe('9')
      await net.setValue('10')
      await wrapper.findAll('.attach-item')[0].find('.confirm-popconfirm').trigger('click')
      if (stage === 'unverified') {
        deletion.reject(new TypeError('delete response lost'))
        await flushPromises()
        expect(mocks.listAttachments).toHaveBeenCalledTimes(2)
      }
      const save = wrapper.findAll('button').find(button => button.text() === en.expenses.save)!
      expect((save.element as HTMLButtonElement).disabled).toBe(true)
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      expect(mocks.updateExpense).not.toHaveBeenCalled()
    } finally {
      if (stage === 'pending') deletion.resolve(undefined)
      await new Promise(resolve => setTimeout(resolve, 350))
      await flushPromises()
      wrapper.unmount()
    }
  },
)

it('preserves an independent ordinary preview through upload and a failed first extraction', async () => {
  mocks.post.mockResolvedValue({ vat_amount: '0.9', gross_amount: '10.9' })
  mocks.uploadAttachment.mockResolvedValue({ ...attachment, id: 'attachment-2', filename: 'second.pdf' })
  mocks.aiExtract.mockRejectedValue(new Error('Provider unavailable'))
  const { wrapper } = await setup()
  const vat = wrapper.findAll('input[type="number"]')[1]
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('0.9')

  await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('0.9')
  await extractRow(wrapper, 1)
  expect((vat.element as HTMLInputElement).value).toBe('0.9')

  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 10, vat_amount: 0.9 }))
  wrapper.unmount()
})

it('preserves an independent ordinary preview when upload withdraws the prior AI receipt baseline', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'new.pdf' }
  mocks.uploadAttachment.mockResolvedValueOnce(attachmentB)
  mocks.aiExtract.mockResolvedValueOnce({ vat_amount: '7' }).mockRejectedValueOnce(new Error('Provider unavailable'))
  mocks.post.mockResolvedValueOnce({ vat_amount: '0.9', gross_amount: '10.9' })
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  const [net, vat] = wrapper.findAll('input[type="number"]')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('0.9')

  await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('0.9')
  await extractRow(wrapper, 1)
  expect((vat.element as HTMLInputElement).value).toBe('0.9')

  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 10, vat_amount: 0.9 }))
  wrapper.unmount()
})

it('restores a human VAT baseline when a new attachment extraction invalidates the preview', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'second.pdf' }
  mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB])
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100' }).mockResolvedValueOnce({})
  mocks.post.mockResolvedValueOnce({ vat_amount: '9', gross_amount: '109' })
  const { wrapper } = await setup()
  await extractRow(wrapper, 0)
  const vat = wrapper.findAll('input[type="number"]')[1]
  await vat.setValue('7')
  await wrapper.findAll('select')[2].setValue('alt-rate')
  await flushPromises()
  expect((vat.element as HTMLInputElement).value).toBe('9')

  await extractRow(wrapper, 1)
  expect((vat.element as HTMLInputElement).value).toBe('7')
  wrapper.unmount()
})

it.each(['net', 'rate'] as const)(
  'restores the receipt VAT when successful extraction changes the completed preview %s input',
  async changedInput => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'changed.pdf' }
    mocks.uploadAttachment.mockResolvedValueOnce(attachmentB)
    mocks.aiExtract.mockResolvedValueOnce(changedInput === 'net'
      ? { net_amount: '200' }
      : { vat_rate_percent: '21' })
    mocks.post.mockResolvedValueOnce({ vat_amount: '0.9', gross_amount: '10.9' })
    const { wrapper } = await setup()
    try {
      const vat = wrapper.findAll('input[type="number"]')[1]
      await wrapper.findAll('select')[2].setValue('alt-rate')
      await flushPromises()
      expect((vat.element as HTMLInputElement).value).toBe('0.9')

      await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
      await flushPromises()
      expect((vat.element as HTMLInputElement).value).toBe('0.9')
      await extractRow(wrapper, 1)

      expect((vat.element as HTMLInputElement).value).toBe('2.1')
      await wrapper.find('form').trigger('submit')
      await flushPromises()
      expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
        vat_amount: 2.1,
        ...(changedInput === 'net' ? { net_amount: 200 } : { vat_rate_id: 'rate' }),
      }))
    } finally { wrapper.unmount() }
  },
)

it('invalidates an equal numeric net preview when extraction changes its attachment ownership', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'same-net.pdf' }
  mocks.uploadAttachment.mockResolvedValueOnce(attachmentB)
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '10' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '0.9', gross_amount: '10.9' })
  const { wrapper } = await setup()
  try {
    const vat = wrapper.findAll('input[type="number"]')[1]
    await wrapper.findAll('select')[2].setValue('alt-rate')
    await flushPromises()
    expect((vat.element as HTMLInputElement).value).toBe('0.9')

    await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
    await flushPromises()
    await extractRow(wrapper, 1)

    expect((vat.element as HTMLInputElement).value).toBe('2.1')
  } finally { wrapper.unmount() }
})

it('requires VAT review when changed preview inputs have no receipt or human VAT baseline', async () => {
  const attachmentB = { ...attachment, id: 'attachment-2', filename: 'new-net.pdf' }
  mocks.uploadAttachment.mockResolvedValueOnce(attachmentB)
  mocks.aiExtract.mockResolvedValueOnce({ net_amount: '200' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '0.9', gross_amount: '10.9' })
  const { wrapper } = await setup()
  try {
    const [net, vat] = wrapper.findAll('input[type="number"]')
    await vat.setValue('')
    await wrapper.findAll('select')[2].setValue('alt-rate')
    await flushPromises()
    expect((vat.element as HTMLInputElement).value).toBe('0.9')

    await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
    await flushPromises()
    await extractRow(wrapper, 1)

    expect((net.element as HTMLInputElement).value).toBe('200')
    expect((vat.element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
  } finally { wrapper.unmount() }
})

it('invalidates an ordinary preview when deleting its AI source clears the preview rate', async () => {
  mocks.aiExtract.mockResolvedValueOnce({ vat_rate_percent: '9' })
  mocks.post.mockResolvedValueOnce({ vat_amount: '0.99', gross_amount: '11.99' })
  mocks.deleteAttachment.mockResolvedValueOnce(undefined)
  const { wrapper } = await setup()
  try {
    await extractRow(wrapper, 0)
    const [net, vat] = wrapper.findAll('input[type="number"]')
    await net.setValue(11)
    await new Promise(resolve => setTimeout(resolve, 350))
    await flushPromises()
    expect((vat.element as HTMLInputElement).value).toBe('0.99')

    await wrapper.find('.confirm-popconfirm').trigger('click')
    await flushPromises()
    expect((vat.element as HTMLInputElement).value).toBe('2.1')
  } finally { wrapper.unmount() }
})
