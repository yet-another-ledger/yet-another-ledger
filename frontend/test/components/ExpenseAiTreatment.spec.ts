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

describe('Expense attachment treatment suggestions', () => {
  it('keeps fixed asset preview keyboard submission separate from expense saving', async () => {
    mocks.post.mockResolvedValue({
      acquisition_cost: '10.00', annual_baseline: '2.00', preview_period_start: '2026-01-01',
      preview_period_end: '2030-12-31', legacy_preview_truncated: false,
      pending_in_service: false, conversion_blocked: false, conversion_block_reason: null,
      period_deltas: [], yearly_deltas: [], monthly_schedule: [],
    })
    const { wrapper } = await setup()
    const [expenseForm] = wrapper.findAll('form')
    const previewForm = wrapper.find('.asset-preview-form')
    const assetNameInput = previewForm.find('input')
    const saveButton = wrapper.findAll('button').find(button => button.text() === en.expenses.save)!
    const previewButton = wrapper.findAll('button').find(button => button.text() === en.fixedAssets.previewButton)!

    expect(assetNameInput.element.closest('form')).toBe(previewForm.element)
    expect(previewButton.element.closest('form')).toBe(previewForm.element)
    expect(saveButton.element.closest('form')).toBe(expenseForm.element)

    const setupState = (wrapper.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
    setupState.supplierName = 'Unsaved supplier change'
    await previewForm.trigger('submit')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/fixed-assets/expenses/one/conversion-preview', expect.any(Object))
    expect(mocks.updateExpense).not.toHaveBeenCalled()

    await expenseForm.trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ supplier_name: 'Unsaved supplier change' }))
    wrapper.unmount()
  })

  it('resets fixed asset draft terms when the detail route changes purchase id', async () => {
    const { router, wrapper } = await setup('/expenses/one/edit')
    const setupState = (wrapper.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
    setupState.assetInServiceDate = '2020-03-15'
    setupState.assetUsefulLife = 10
    setupState.assetResidualValue = 100
    await flushPromises()

    await router.push('/expenses/two/edit')
    await flushPromises()
    expect(setupState.assetInServiceDate).toBe('')
    expect(setupState.assetUsefulLife).toBe(5)
    expect(setupState.assetResidualValue).toBe(0)

    await wrapper.findAll('button').find(button => button.text() === en.fixedAssets.previewButton)!.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/fixed-assets/expenses/two/conversion-preview', expect.objectContaining({
      in_service_date: null, useful_life_years: 5, residual_value: 0,
    }))
    wrapper.unmount()
  })

  it.each(['before refresh', 'after refresh'])('withdraws a missing extraction source when a failed delete refresh completes %s the extraction', async order => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'other-document.pdf' }
    mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB]).mockResolvedValueOnce([attachmentB])
    mocks.deleteAttachment.mockRejectedValueOnce(new TypeError('Delete response lost'))
    const pending = deferred<unknown>()
    mocks.aiExtract.mockReturnValueOnce(pending.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    const completeExtraction = async () => {
      pending.resolve({
        net_amount: '100', vat_amount: '0',
        suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'A-only reverse-charge evidence.' },
      })
      await flushPromises()
    }
    if (order === 'before refresh') await completeExtraction()
    await rows[1].find('.confirm-popconfirm').trigger('click')
    await flushPromises()
    if (order === 'after refresh') await completeExtraction()

    expect(wrapper.findAll('.attach-name').map(x => x.text())).toEqual(['other-document.pdf'])
    expect(wrapper.text()).not.toContain('A-only reverse-charge evidence.')
    expect(wrapper.findAll('button').some(x => x.text() === en.expenses.ai.applyTreatment)).toBe(false)
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe(order === 'before refresh' ? '' : '10')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', net_amount: 100, vat_amount: 0,
    }))
    wrapper.unmount()
  })

  it.each([true, false])('reconciles a missing A source on manual recheck while attempted B is %s', async attemptedBRemains => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'other-document.pdf' }
    mocks.listAttachments.mockResolvedValueOnce([attachment, attachmentB]).mockRejectedValueOnce(new Error('list unavailable'))
    mocks.listAttachments.mockResolvedValueOnce(attemptedBRemains ? [attachmentB] : [])
    mocks.aiExtract.mockResolvedValueOnce({
      net_amount: '100', vat_amount: '0',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'A-only reverse-charge evidence.' },
    })
    mocks.deleteAttachment.mockRejectedValueOnce(new TypeError('Delete response lost'))
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('select')[1].setValue('tax')
    await wrapper.findAll('input[type="number"]')[1].setValue('7')
    await rows[1].find('.confirm-popconfirm').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain(en.expenses.ai.attachmentStatusUncertain)

    await wrapper.findAll('button').find(button => button.text() === en.expenses.ai.recheckAttachments)!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.attach-name').map(x => x.text())).toEqual(attemptedBRemains ? ['other-document.pdf'] : [])
    expect(wrapper.text()).not.toContain('A-only reverse-charge evidence.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('tax')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('7')
    wrapper.unmount()
  })

  it('shows a current suggestion, keeps a saved treatment, and applies only on explicit choice', async () => {
    mocks.aiExtract.mockResolvedValue({
      vat_amount: '0', vat_rate_percent: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
    })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    const treatment = wrapper.findAll('select')[1]
    expect((treatment.element as HTMLSelectElement).value).toBe('tax')
    expect(wrapper.text()).toContain('The invoice states reverse charge.')
    expect(wrapper.text()).toContain('Saved choice')
    expect(wrapper.text()).toContain('Review or select the applicable Dutch nominal VAT rate.')
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    expect((treatment.element as HTMLSelectElement).value).toBe('reverse')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_rate_id: 'rate' }))
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect(wrapper.text()).not.toContain('Apply suggestion')
  })

  it('does not prefill an ordinary rate or run a gross preview while the saved treatment is reverse charge', async () => {
    mocks.getExpense.mockResolvedValue(expense('one', { vat_treatment_id: 'reverse', vat_rate_id: null, vat_amount: '0' }))
    mocks.aiExtract.mockResolvedValue({
      vat_amount: '0', vat_rate_percent: '21',
      suggested_vat_treatment: { id: 'tax', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT is charged.' },
    })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    expect(mocks.post).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Review or select the applicable Dutch nominal VAT rate.')
  })

  it('keeps the existing ordinary attachment rate prefill behavior on a saved Expense', async () => {
    mocks.getExpense.mockResolvedValue(expense('one', { vat_rate_id: 'alt-rate' }))
    mocks.aiExtract.mockResolvedValue({ vat_rate_percent: '21', vat_amount: '2.1' })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
  })

  it('preserves ordinary AI rates through partial and failed same-attachment retries but clears only AI-owned rates for reverse charge', async () => {
    mocks.aiExtract.mockResolvedValueOnce({
      vat_rate_percent: '21', vat_amount: '2.1',
      suggested_vat_treatment: { id: 'tax', code: 'NL_DOMESTIC_PURCH', reason: 'Dutch VAT is charged.' },
    }).mockResolvedValueOnce({
      vat_amount: '2.1', suggested_vat_treatment: { id: 'tax', code: 'NL_DOMESTIC_PURCH', reason: 'Ordinary VAT remains.' },
    }).mockResolvedValueOnce({}).mockResolvedValueOnce({
      vat_amount: '0', vat_rate_percent: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The buyer accounts for VAT.' },
    }).mockResolvedValueOnce({
      vat_amount: '0', vat_rate_percent: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The buyer still accounts for VAT.' },
    })
    const { wrapper } = await setup()
    const retry = async () => {
      await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
      await flushPromises()
    }
    await retry()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await retry()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    await wrapper.findAll('select')[2].setValue('rate')
    await retry()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
  })

  it('keeps missing VAT across a failed same-attachment retry and restores it only after a new value', async () => {
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: null })
      .mockRejectedValueOnce(new Error('provider failed'))
      .mockResolvedValueOnce({ net_amount: '100', vat_amount: '0' })
    const { wrapper } = await setup()
    const extract = () => wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await extract(); await flushPromises()
    await wrapper.findAll('select')[1].setValue('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await extract(); await flushPromises()
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    await extract(); await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_amount: 0 }))
  })

  it('keeps the missing-VAT boundary and explicit manual VAT provenance across an ordinary save', async () => {
    mocks.aiExtract.mockResolvedValue({
      net_amount: '100', vat_amount: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
    })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click'); await flushPromises()
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)
    await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(2)
    expect(mocks.updateExpense).toHaveBeenLastCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_amount: 0 }))
  })

  it('does not restore a pre-save VAT baseline after an ordinary preview has been saved', async () => {
    mocks.post.mockResolvedValue({ vat_amount: '4.2', gross_amount: '24.2' })
    const { wrapper } = await setup()
    await wrapper.findAll('input[type="number"]')[0].setValue('20')
    await new Promise(resolve => setTimeout(resolve, 350)); await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('4.2')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    await wrapper.findAll('select')[1].setValue('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('4.2')
  })

  it('invalidates a pending ordinary VAT preview when the owner edits receipt VAT', async () => {
    mocks.getExpense.mockResolvedValue(expense('one', { vat_rate_id: null }))
    let finishPreview!: (value: unknown) => void
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finishPreview = resolve }))
    const { wrapper } = await setup()
    await wrapper.findAll('select')[2].setValue('rate')
    await wrapper.findAll('input[type="number"]')[1].setValue('0.9')
    finishPreview({ vat_amount: '2.1', gross_amount: '12.1' }); await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0.9')
    expect(wrapper.text()).not.toContain('12.10')
  })

  it('invalidates an issued VAT preview when extraction writes receipt VAT before the suggestion is applied', async () => {
    let finishAi!: (value: unknown) => void
    let finishPreview!: (value: unknown) => void
    mocks.aiExtract.mockReturnValueOnce(new Promise(resolve => { finishAi = resolve }))
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finishPreview = resolve }))
    const { wrapper } = await setup()
    await wrapper.find('.attach-item').find('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('select')[2].setValue('alt-rate')
    expect(mocks.post).toHaveBeenCalledTimes(1)
    finishAi({ vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' } })
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    finishPreview({ vat_amount: '0.9', gross_amount: '10.9' })
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0')
  })

  it('cancels a debounced VAT preview when extraction writes the receipt form', async () => {
    let finishAi!: (value: unknown) => void
    mocks.aiExtract.mockReturnValueOnce(new Promise(resolve => { finishAi = resolve }))
    const { wrapper } = await setup()
    await wrapper.find('.attach-item').find('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('input[type="number"]')[0].setValue('12')
    finishAi({ vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' } })
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 350))
    await flushPromises()
    expect(mocks.post).not.toHaveBeenCalled()
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('12')
  })

  it('preserves an independent ordinary VAT preview when uploading an attachment', async () => {
    mocks.post.mockResolvedValue({ vat_amount: '0.9', gross_amount: '10.9' })
    mocks.uploadAttachment.mockResolvedValue({ ...attachment, id: 'attachment-2', filename: 'second.pdf' })
    const { wrapper } = await setup()
    await wrapper.findAll('select')[2].setValue('alt-rate')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0.9')

    await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0.9')

    await wrapper.findAll('select')[1].setValue('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('2.1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'alt-rate', net_amount: 10, vat_amount: 2.1,
    }))
    wrapper.unmount()
  })

  it('preserves an independent ordinary VAT preview when the first attachment extraction fails', async () => {
    mocks.post.mockResolvedValue({ vat_amount: '0.9', gross_amount: '10.9' })
    mocks.aiExtract.mockRejectedValue(new Error('Provider unavailable'))
    const { wrapper } = await setup()
    await wrapper.findAll('select')[2].setValue('alt-rate')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0.9')

    await wrapper.findAll('select')[1].setValue('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('2.1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'alt-rate', net_amount: 10, vat_amount: 2.1,
    }))
    wrapper.unmount()
  })

  it('disables Apply while saving so the save response cannot undo a later explicit choice', async () => {
    mocks.aiExtract.mockResolvedValue({ suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' }, vat_amount: '0' })
    let finishSave!: (value: unknown) => void
    mocks.updateExpense.mockReturnValueOnce(new Promise(resolve => { finishSave = resolve }))
    const { wrapper } = await setup()
    await wrapper.find('.attach-item').find('button').trigger('click')
    await flushPromises()
    const apply = wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('button').some(x => x.text() === 'Apply suggestion')).toBe(false)
    await apply.trigger('click')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('tax')
    finishSave(expense('one'))
    await flushPromises()
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('tax')
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('reverse')
  })

  it('blocks both attachment extraction entry points while saving is in flight', async () => {
    const save = deferred<unknown>()
    mocks.uploadAttachment.mockResolvedValueOnce({ ...attachment, id: 'new-attachment' })
    mocks.updateExpense.mockReturnValueOnce(save.promise)
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
    await flushPromises()
    await wrapper.find('form').trigger('submit')
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)
    const extractionButtons = wrapper.findAll('button').filter(button => button.text().includes(en.expenses.ai.autoFill))
    expect(extractionButtons.length).toBeGreaterThanOrEqual(2)
    expect(extractionButtons.every(button => (button.element as HTMLButtonElement).disabled)).toBe(true)
    for (const button of extractionButtons) await button.trigger('click')
    expect(mocks.aiExtract).not.toHaveBeenCalled()
    save.resolve(expense('one')); await flushPromises()
    wrapper.unmount()
  })

  it('withdraws AI rates when switching from attachment A to B, including partial and failed B results', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ vat_rate_percent: '21', suggested_vat_treatment: { id: 'tax', code: 'NL_DOMESTIC_PURCH', reason: 'A charges Dutch VAT.' } })
      .mockResolvedValueOnce({ vat_amount: '0.4' })
      .mockRejectedValueOnce(new Error('provider failed'))
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].find('button').trigger('click'); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    await wrapper.findAll('.attach-item')[1].find('button').trigger('click'); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
    await wrapper.findAll('.attach-item')[1].find('button').trigger('click'); await flushPromises()
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('')
  })

  it('withdraws an in-flight suggestion when the owner saves the Expense', async () => {
    let finishAi!: (value: unknown) => void
    mocks.aiExtract.mockReturnValueOnce(new Promise(resolve => { finishAi = resolve }))
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain(en.expenses.ai.extracting)
    await wrapper.find('form').trigger('submit'); await flushPromises()
    finishAi({ supplier_name: 'Stale extracted supplier', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Stale evidence.' } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Stale evidence.')
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Supplier one')!.element as HTMLInputElement).value).toBe('Supplier one')
    expect(wrapper.text()).not.toContain(en.expenses.ai.extracting)
  })

  it('keeps B missing receipt VAT when deletion of previously extracted A succeeds late', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletion = deferred<void>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '25', vat_amount: '3.3' }).mockResolvedValueOnce({
      net_amount: '100', vat_amount: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' },
    })
    mocks.deleteAttachment.mockReturnValueOnce(deletion.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)

    await rows[0].find('.confirm-popconfirm').trigger('click')
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    deletion.resolve()
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text() === 'Apply suggestion')!.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledTimes(1)
    await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenLastCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'rate', net_amount: 100, vat_amount: 0,
    }))
  })

  it('keeps B selected when deletion of A fails while B extraction is pending', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletion = deferred<void>()
    const extractionB = deferred<unknown>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ vat_amount: '3.3' }).mockReturnValueOnce(extractionB.promise).mockResolvedValueOnce({})
    mocks.deleteAttachment.mockReturnValueOnce(deletion.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await rows[0].find('.confirm-popconfirm').trigger('click')
    const deletingAExtract = rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!
    expect((deletingAExtract.element as HTMLButtonElement).disabled).toBe(true)
    await deletingAExtract.trigger('click')
    expect(mocks.aiExtract).toHaveBeenCalledTimes(1)
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()

    deletion.reject(new Error('delete failed'))
    await flushPromises()
    extractionB.resolve({ vat_amount: '0', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' } })
    await flushPromises()
    expect(wrapper.text()).toContain('Buyer accounts for VAT.')

    const topExtractionButton = wrapper.findAll('button').find(button => button.text() === en.expenses.ai.autoFill)!
    await topExtractionButton.trigger('click')
    await flushPromises()
    expect(mocks.aiExtract).toHaveBeenLastCalledWith(attachmentB.id, 'en')
  })

  it('reloads the new route and ignores late expense details from the previous route', async () => {
    let finishOne!: (value: unknown) => void
    mocks.getExpense.mockImplementation((id: string) => id === 'one' ? new Promise(resolve => { finishOne = resolve }) : Promise.resolve(expense('two')))
    const { router, wrapper } = await setup()
    await router.push('/expenses/two/edit'); await flushPromises()
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Supplier two')!.element as HTMLInputElement).value).toBe('Supplier two')
    finishOne(expense('one', { supplier_name: 'Late supplier one' })); await flushPromises()
    expect(wrapper.text()).not.toContain('Late supplier one')
    expect(mocks.getExpense).toHaveBeenCalledWith('two')
  })

  it('does not apply a late save response to a different expense route', async () => {
    let finishSave!: (value: unknown) => void
    mocks.updateExpense.mockReturnValueOnce(new Promise(resolve => { finishSave = resolve }))
    const { router, wrapper } = await setup()
    await wrapper.find('form').trigger('submit'); await flushPromises()
    await router.push('/expenses/two/edit'); await flushPromises()
    finishSave(expense('one', { supplier_name: 'Late saved supplier' })); await flushPromises()
    expect(wrapper.text()).not.toContain('Late saved supplier')
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Supplier two')!.element as HTMLInputElement).value).toBe('Supplier two')
  })

  it('ignores late AI extraction and attachment upload responses after switching routes', async () => {
    let finishAi!: (value: unknown) => void
    let finishUpload!: (value: unknown) => void
    mocks.aiExtract.mockReturnValueOnce(new Promise(resolve => { finishAi = resolve }))
    mocks.uploadAttachment.mockReturnValueOnce(new Promise(resolve => { finishUpload = resolve }))
    const { router, wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    const upload = wrapper.findComponent({ name: 'UploadStub' })
    await upload.find('button').trigger('click')
    await flushPromises()
    await router.push('/expenses/two/edit'); await flushPromises()
    finishUpload(attachment); finishAi({ supplier_name: 'Stale AI supplier', suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Stale evidence.' } })
    await flushPromises()
    expect((wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Supplier two')!.element as HTMLInputElement).value).toBe('Supplier two')
    expect(wrapper.text()).not.toContain('Stale evidence.')
    expect(wrapper.text()).not.toContain('Stale AI supplier')
  })

  it('requires a current receipt VAT after applying reverse charge to a saved ordinary Expense with missing extracted VAT', async () => {
    mocks.aiExtract.mockResolvedValue({
      net_amount: '100', vat_amount: null,
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'Buyer accounts for VAT.' },
    })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(x => x.text() === 'Apply suggestion')!.trigger('click')
    expect((wrapper.findAll('select')[2].element as HTMLSelectElement).value).toBe('rate')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', vat_rate_id: 'rate', net_amount: 100, vat_amount: 0,
    }))
  })

  it('clears stale VAT after a reference-free missing result, then replaces it from a later valid extraction', async () => {
    mocks.aiExtract.mockResolvedValueOnce({ vat_amount: '3.3' }).mockResolvedValueOnce({ net_amount: '100' }).mockResolvedValueOnce({ vat_amount: '0.7' })
    const { wrapper } = await setup()
    await wrapper.findAll('select')[1].setValue('reverse')
    const extract = wrapper.findAll('button').find(x => x.text().includes(en.expenses.ai.autoFill))!
    await extract.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('3.3')
    await extract.trigger('click')
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    await extract.trigger('click'); await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('0.7')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ vat_treatment_id: 'reverse', vat_amount: 0.7 }))
  })

  it('blocks applying and saving the current suggestion while its attachment deletion is pending, then clears its AI amounts on success', async () => {
    const deletion = deferred<void>()
    mocks.aiExtract.mockResolvedValue({
      net_amount: '100', vat_amount: '0',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
    })
    mocks.deleteAttachment.mockReturnValueOnce(deletion.promise)
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()

    const row = wrapper.find('.attach-item')
    await row.find('.confirm-popconfirm').trigger('click')
    const apply = wrapper.findAll('button').find(button => button.text() === 'Apply suggestion')!
    expect((apply.element as HTMLButtonElement).disabled).toBe(true)
    await apply.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()

    deletion.resolve()
    await flushPromises()
    expect(wrapper.text()).not.toContain('The invoice states reverse charge.')
    expect((wrapper.findAll('select')[1].element as HTMLSelectElement).value).toBe('tax')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
  })

  it('restores the unchanged suggestion and its amounts when current attachment deletion fails', async () => {
    const deletion = deferred<void>()
    mocks.aiExtract.mockResolvedValue({
      net_amount: '100', vat_amount: '0',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'The invoice states reverse charge.' },
    })
    mocks.deleteAttachment.mockReturnValueOnce(deletion.promise)
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    deletion.reject(new Error('delete failed'))
    await flushPromises()

    const apply = wrapper.findAll('button').find(button => button.text() === 'Apply suggestion')!
    expect((apply.element as HTMLButtonElement).disabled).toBe(false)
    expect(wrapper.text()).toContain('The invoice states reverse charge.')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('100')
    await apply.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', net_amount: 100, vat_amount: 0,
    }))
  })

  it('serializes attachment deletion and keeps B current when A deletion completes', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletionA = deferred<void>()
    const deletionB = deferred<void>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.deleteAttachment.mockReturnValueOnce(deletionA.promise).mockReturnValueOnce(deletionB.promise)
    mocks.aiExtract.mockResolvedValue({
      net_amount: '100', vat_amount: '0',
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'B states reverse charge.' },
    })
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].find('.confirm-popconfirm').trigger('click')
    const secondDelete = rows[1].findAll('button').find(button => button.text().includes(en.receipt.delete))!
    expect((secondDelete.element as HTMLButtonElement).disabled).toBe(true)
    await rows[1].find('.confirm-popconfirm').trigger('click')
    expect(mocks.deleteAttachment).toHaveBeenCalledTimes(1)

    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    deletionA.resolve()
    await flushPromises()
    expect(wrapper.text()).toContain('B states reverse charge.')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('100')
    await wrapper.findAll('button').find(button => button.text() === 'Apply suggestion')!.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'reverse', net_amount: 100, vat_amount: 0,
    }))

    deletionB.resolve()
    await flushPromises()
  })

  it('protects values sourced from deleting A after B becomes current and clears only A-owned values on success', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletionA = deferred<void>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '21' }).mockResolvedValueOnce({
      suggested_vat_treatment: { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'B states reverse charge.' },
    })
    mocks.deleteAttachment.mockReturnValueOnce(deletionA.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await rows[0].find('.confirm-popconfirm').trigger('click')
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()

    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('100')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()

    deletionA.resolve()
    await flushPromises()
    expect(wrapper.text()).toContain('B states reverse charge.')
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    await wrapper.findAll('input[type="number"]')[0].setValue('0')
    await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'tax', net_amount: 0, vat_amount: 0,
    }))
  })

  it('requires each withdrawn AI amount to be reviewed while allowing an explicit zero', async () => {
    mocks.aiExtract.mockResolvedValue({ net_amount: '100', vat_amount: '0' })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()

    await wrapper.findAll('input[type="number"]')[0].setValue('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.withdrawnVatRequired)

    await wrapper.findAll('input[type="number"]')[1].setValue('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 0, vat_amount: 0 }))
  })

  it('reconciles a lost DELETE response against the authoritative attachment list', async () => {
    let present = true
    mocks.listAttachments.mockImplementation(async () => present ? [attachment] : [])
    mocks.aiExtract.mockResolvedValue({ net_amount: '100', vat_amount: '0', suggested_vat_treatment: {
      id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'A states reverse charge.',
    } })
    mocks.deleteAttachment.mockImplementationOnce(async () => {
      present = false
      throw new TypeError('Failed to fetch')
    })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()

    expect(mocks.listAttachments).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain('A states reverse charge.')
    expect(wrapper.findAll('.attach-item')).toHaveLength(0)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
  })

  it('keeps an uncertain source locked until a later attachment recheck succeeds', async () => {
    mocks.aiExtract.mockResolvedValue({ net_amount: '100', vat_amount: '0', suggested_vat_treatment: {
      id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', reason: 'A states reverse charge.',
    } })
    mocks.deleteAttachment.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    mocks.listAttachments.mockResolvedValueOnce([attachment]).mockRejectedValueOnce(new Error('list unavailable')).mockResolvedValueOnce([])
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('A states reverse charge.')
    expect((wrapper.findAll('button').find(button => button.text() === 'Apply suggestion')!.element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()

    await wrapper.findAll('button').find(button => button.text() === en.expenses.ai.recheckAttachments)!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('A states reverse charge.')
    expect(wrapper.findAll('.attach-item')).toHaveLength(0)
  })

  it.each([
    ['net amount', 0, 'withdrawnNetRequired'],
    ['VAT amount', 1, 'withdrawnVatRequired'],
  ] as const)('keeps %s review required when a temporary value is cleared again', async (_field, index, messageKey) => {
    mocks.aiExtract.mockResolvedValue({ net_amount: '100', vat_amount: '8' })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()

    const amounts = wrapper.findAll('input[type="number"]')
    await amounts[0].setValue('80')
    await amounts[1].setValue('7')
    await amounts[index].setValue('')
    expect((amounts[index].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai[messageKey])

    await amounts[0].setValue('0')
    await amounts[1].setValue('0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 0, vat_amount: 0 }))
  })

  it('keeps withdrawn amount review required after a later attachment AI prefill', async () => {
    mocks.aiExtract.mockResolvedValue({ net_amount: '120', vat_amount: '9' })
    mocks.uploadAttachment.mockResolvedValue({ ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text() === 'Upload test file')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    const netAmountInput = wrapper.findAll('input[type="number"]')[0]
    await netAmountInput.setValue('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.withdrawnNetRequired)
  })

  it.each([true, false])('keeps withdrawn VAT required when B missing-VAT extraction settles before A DELETE (B success=%s)', async (bSucceeds) => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletionA = deferred<void>()
    const extractionB = deferred<unknown>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '4' }).mockReturnValueOnce(extractionB.promise)
    mocks.deleteAttachment.mockReturnValueOnce(deletionA.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('input[type="number"]')[0].setValue('80')
    await rows[0].find('.confirm-popconfirm').trigger('click')
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')

    if (bSucceeds) extractionB.resolve({ net_amount: '125' })
    else extractionB.reject(new Error('B extraction failed'))
    await flushPromises()
    deletionA.resolve()
    await flushPromises()

    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.withdrawnVatRequired)
    await wrapper.findAll('input[type="number"]')[1].setValue('7')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'tax', vat_rate_id: 'rate', net_amount: bSucceeds ? 125 : 80, vat_amount: 7,
    }))
  })

  it('withdraws A VAT when B extraction settles before the successful A DELETE', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletionA = deferred<void>()
    const extractionB = deferred<unknown>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '4' }).mockReturnValueOnce(extractionB.promise)
    mocks.deleteAttachment.mockReturnValueOnce(deletionA.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await rows[0].find('.confirm-popconfirm').trigger('click')
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    extractionB.resolve({ net_amount: '125' })
    await flushPromises()
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()

    deletionA.resolve()
    await flushPromises()
    expect(wrapper.findAll('.attach-item')).toHaveLength(1)
    await wrapper.findAll('input[type="number"]')[1].setValue('7')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 125, vat_amount: 7 }))
  })

  it('keeps VAT review required when the successful A DELETE precedes a failed B extraction', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    const deletionA = deferred<void>()
    const extractionB = deferred<unknown>()
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '4' }).mockReturnValueOnce(extractionB.promise)
    mocks.deleteAttachment.mockReturnValueOnce(deletionA.promise)
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await rows[0].find('.confirm-popconfirm').trigger('click')
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    deletionA.resolve()
    await flushPromises()
    extractionB.reject(new Error('B extraction failed'))
    await flushPromises()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.findAll('.attach-item')).toHaveLength(1)
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    await wrapper.findAll('input[type="number"]')[0].setValue('80')
    await wrapper.findAll('input[type="number"]')[1].setValue('7')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({ net_amount: 80, vat_amount: 7 }))
  })

  it('requires VAT review when switching from extracted A to still-attached B with missing VAT', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-b.pdf' }
    mocks.listAttachments.mockResolvedValue([attachment, attachmentB])
    mocks.aiExtract.mockResolvedValueOnce({ net_amount: '100', vat_amount: '4' }).mockResolvedValueOnce({ net_amount: '125' })
    const { wrapper } = await setup()
    const rows = wrapper.findAll('.attach-item')
    await rows[0].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await rows[1].findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.attach-item')).toHaveLength(2)
    expect((wrapper.findAll('input[type="number"]')[0].element as HTMLInputElement).value).toBe('125')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
  })

  it('requires review after missing extraction withdraws saved VAT on reverse then ordinary treatment changes', async () => {
    mocks.aiExtract.mockResolvedValueOnce({})
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    const treatment = wrapper.findAll('select')[1]
    await treatment.setValue('reverse')
    expect((wrapper.findAll('input[type="number"]')[1].element as HTMLInputElement).value).toBe('')
    await treatment.setValue('tax')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.withdrawnVatRequired)
  })

  it.each([0, 7])('requires review after missing extraction withdraws saved reverse-charge VAT, then saves explicit %s', async vatAmount => {
    mocks.getExpense.mockResolvedValue(expense('one', { vat_treatment_id: 'reverse' }))
    mocks.aiExtract.mockResolvedValue({ net_amount: '100' })
    const { wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()

    const vatInput = wrapper.findAll('input[type="number"]')[1]
    expect((vatInput.element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).not.toContain(en.expenses.ai.withdrawnVatRequired)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.reverseVatRequired)

    const treatment = wrapper.findAll('select')[1]
    await treatment.setValue('tax')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(en.expenses.ai.withdrawnVatRequired)

    await vatInput.setValue(String(vatAmount))
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateExpense).toHaveBeenCalledWith('one', expect.objectContaining({
      vat_treatment_id: 'tax', vat_rate_id: 'rate', net_amount: 100, vat_amount: vatAmount,
    }))
    wrapper.unmount()
  })

  it('keeps attachment checks and uploads serialized and ignores an upload response after changing Expense pages', async () => {
    const attachmentB = { ...attachment, id: 'attachment-2', filename: 'receipt-two.pdf' }
    const recheck = deferred<typeof attachment[]>()
    const upload = deferred<typeof attachment>()
    mocks.listAttachments.mockImplementation(async (id: string) => id === 'two' ? [attachmentB] : [attachment])
    mocks.deleteAttachment.mockRejectedValueOnce(new TypeError('response lost'))
    mocks.listAttachments.mockResolvedValueOnce([attachment]).mockRejectedValueOnce(new Error('list unavailable'))
    mocks.listAttachments.mockImplementationOnce(() => recheck.promise)
    mocks.uploadAttachment.mockReturnValueOnce(upload.promise)
    const { router, wrapper } = await setup()
    await wrapper.findAll('button').find(button => button.text().includes(en.expenses.ai.autoFill))!.trigger('click')
    await flushPromises()
    await wrapper.find('.attach-item').find('.confirm-popconfirm').trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === en.expenses.ai.recheckAttachments)!.trigger('click')

    const uploadButton = wrapper.findAll('button').find(button => button.text() === 'Upload test file')!
    expect((uploadButton.element as HTMLButtonElement).disabled).toBe(true)
    await uploadButton.trigger('click')
    expect(mocks.uploadAttachment).not.toHaveBeenCalled()

    recheck.resolve([attachment])
    await flushPromises()
    await uploadButton.trigger('click')
    await flushPromises()
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1)
    const deleteButton = wrapper.findAll('.attach-item')[0].findAll('button').find(button => button.text().includes(en.receipt.delete))!
    expect((deleteButton.element as HTMLButtonElement).disabled).toBe(true)

    await router.push('/expenses/two/edit')
    await flushPromises()
    upload.resolve({ ...attachment, id: 'stale-upload', filename: 'stale.pdf' })
    await flushPromises()
    expect(wrapper.findAll('.attach-item')).toHaveLength(1)
    expect(wrapper.text()).toContain('receipt-two.pdf')
    expect(wrapper.text()).not.toContain('stale.pdf')
  })
})

describe('Fixed asset conversion preview freshness', () => {
  const previewResult = {
    expense_id: 'one', expense_date: '2026-01-02', acquisition_cost: '1000.00',
    residual_value: '0.00', depreciable_amount: '1000.00', annual_baseline: '200.00',
    legacy_annual_cost: '10.00', pending_in_service: false, prior_years_affected: [],
    conversion_blocked: false, conversion_block_reason: null, reporting_as_of: '2026-11-23',
    period_deltas: [], monthly_schedule: [], yearly_deltas: [],
  }

  it('clears an old preview when asset inputs change and after the purchase is saved', async () => {
    mocks.post.mockImplementation(async (path: string) => path.includes('fixed-assets') ? previewResult : {})
    mocks.updateExpense.mockImplementation(async (id: string, body: unknown) => expense(id, body as Record<string, unknown>))
    const { wrapper } = await setup()
    const previewButton = wrapper.findAll('button').find(button => button.text() === en.fixedAssets.previewButton)!
    await previewButton.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('1000.00')

    const residualInput = wrapper.find(`[data-label="${en.fixedAssets.residualValue}"] input`)
    await residualInput.setValue('50')
    await flushPromises()
    expect(wrapper.text()).not.toContain('1000.00')

    await previewButton.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('1000.00')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).not.toContain('1000.00')
    wrapper.unmount()
  })

  it('ignores an in-flight response after a preview input changes', async () => {
    const pending = deferred<typeof previewResult>()
    mocks.post.mockImplementation((path: string) => path.includes('fixed-assets') ? pending.promise : Promise.resolve({}))
    const { wrapper } = await setup()
    const previewButton = wrapper.findAll('button').find(button => button.text() === en.fixedAssets.previewButton)!
    await previewButton.trigger('click')
    await wrapper.find(`[data-label="${en.fixedAssets.residualValue}"] input`).setValue('25')
    pending.resolve(previewResult)
    await flushPromises()
    expect(wrapper.text()).not.toContain('1000.00')
    wrapper.unmount()
  })

  it('submits 100% when switching a saved mixed-use purchase to full business use', async () => {
    mocks.getExpense.mockResolvedValueOnce(expense('mixed', { business_percentage: '60' }))
    const { wrapper } = await setup('/expenses/mixed/edit')
    const useTypeField = wrapper.find(`[data-label="${en.fixedAssets.useType}"] select`)
    expect((wrapper.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState.assetUseType)
      .toBe('MIXED_USE')
    await useTypeField.setValue('FULL_BUSINESS')
    await flushPromises()
    expect(wrapper.findAll(`[data-label="${en.expenses.businessPercentage}"]`)).toHaveLength(1)

    await wrapper.findAll('button').find(button => button.text() === en.fixedAssets.previewButton)!.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/fixed-assets/expenses/mixed/conversion-preview',
      expect.objectContaining({ use_type: 'FULL_BUSINESS', business_percentage: 100 }))
    wrapper.unmount()
  })
})
