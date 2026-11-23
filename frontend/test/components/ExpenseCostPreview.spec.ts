import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const mocks = vi.hoisted(() => ({
  post: vi.fn(), get: vi.fn(), getExpense: vi.fn(), listAttachments: vi.fn(),
  updateExpense: vi.fn(), fetchRecurringExpenses: vi.fn(), updateRecurringExpense: vi.fn(),
  items: [] as any[],
}))
vi.mock('../../src/api/http', () => ({ post: mocks.post, get: mocks.get, ApiError: class extends Error {} }))
vi.mock('../../src/stores/expenses', () => ({ useExpensesStore: () => mocks }))
vi.mock('../../src/stores/recurringExpenses', () => ({ useRecurringExpensesStore: () => ({
  ...mocks, total: 1, limit: 50, offset: 0, activeFilter: null, loading: false, error: null,
}) }))
vi.mock('../../src/composables/usePurchaseExpenseDictionaries', async importOriginal => ({
  ...(await importOriginal<typeof import('../../src/composables/usePurchaseExpenseDictionaries')>()),
  toExpenseInput: (value: unknown) => value,
  usePurchaseExpenseDictionaries: () => ({
    categories: { value: [{ id: 'cat', name: 'Office', default_deductible: false }] },
    treatments: { value: [
      { id: 'tax', code: 'NL_DOMESTIC_PURCH', label: 'Domestic', side: 'PURCHASE', effect: 'APPLY_RATE', active: true },
      { id: 'reverse', code: 'EU_B2B_REVERSE_PURCH', label: 'EU reverse charge', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true },
    ] },
    rates: { value: [] }, categoryOptions: { value: [{ label: 'Office', value: 'cat' }, { label: 'Other category', value: 'other-cat' }] },
    purchaseTreatmentOptions: { value: [{ label: 'Domestic', value: 'tax' }, { label: 'Other', value: 'other' }] },
    vatRateOptions: { value: [{ label: '21%', value: 'rate' }] }, load: vi.fn(),
  }),
}))
vi.mock('naive-ui', async () => {
  const { h } = await import('vue')
  const box = { template: '<div><slot /><slot name="icon" /></div>' }
  const field = { props: ['label'], template: '<div :data-label="label"><slot /></div>' }
  const input = { props: ['value'], emits: ['update:value'], template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />' }
  const number = { props: ['value'], emits: ['update:value'], template: '<input type="number" :value="value" @input="$emit(\'update:value\', Number($event.target.value))" />' }
  const select = { props: ['value', 'options'], emits: ['update:value'], template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="o in options" :value="o.value">{{o.label}}</option></select>' }
  const check = { props: ['checked'], emits: ['update:checked'], template: '<input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" />' }
  const button = { props: ['loading', 'attrType'], emits: ['click'], template: '<button :type="attrType || \'button\'" :disabled="loading" @click="$emit(\'click\', $event)"><slot /></button>' }
  const table = { props: ['data', 'columns'], setup: (props: any) => () => h('div', props.data.map((row: any) => props.columns.find((c: any) => c.key === 'actions').render(row))) }
  return {
    NForm: { template: '<form><slot /></form>' }, NFormItem: field,
    NInput: input, NInputNumber: number, NSelect: select, NCheckbox: check, NButton: button,
    NEmpty: box, NAlert: box, NCard: box, NIcon: box, NPagination: box, NPopconfirm: box, NRadioGroup: box,
    NRadioButton: box, NSpace: box, NSpin: box, NSwitch: check, NTag: box, NText: box,
    NUpload: box, NDatePicker: box, NDataTable: table,
    NModal: { props: ['show'], template: '<div v-if="show"><slot /></div>' },
  }
})
import ExpenseEdit from '../../src/views/expenses/ExpenseEdit.vue'
import RecurringExpenseList from '../../src/views/expenses/RecurringExpenseList.vue'

const legacy = () => ({
  id: 'legacy', expense_date: '2026-06-13', category_id: 'cat', supplier_name: 'Shop',
  vat_treatment_id: 'tax', vat_rate_id: 'rate', net_amount: '100.000', vat_amount: '21.000',
  gross_amount: '121.000', deductible: false, paid_by: 'BUSINESS', business_percentage: '100',
  depreciation_years: 1, include_in_pl: true, pl_cost_basis: 'NET', name: 'Legacy template',
  frequency: 'MONTHLY', start_date: '2026-01-01', active: true,
})
const result = (annual: string) => ({ cost_basis_amount: annual, annual_cost: annual, included_in_pl: true })
const conversionPreview = {
  acquisition_cost: '100.00', annual_baseline: '20.00', preview_period_start: '2026-01-01',
  preview_period_end: '2030-12-31', legacy_preview_truncated: false, pending_in_service: false,
  conversion_blocked: false, conversion_block_reason: null, preview_token: 'token', period_deltas: [], yearly_deltas: [], monthly_schedule: [],
}
function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const field = (w: VueWrapper, label: string) => w.find(`[data-label="${label}"]`)
const button = (w: VueWrapper, text: string) => w.findAll('button').find(b => b.text() === text)!
async function setup(recurring = false) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/expenses/:id/edit', component: ExpenseEdit }, { path: '/fixed-assets', component: ExpenseEdit }] })
  await router.push('/expenses/legacy/edit')
  await router.isReady()
  const wrapper = mount(recurring ? RecurringExpenseList : ExpenseEdit, {
    global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { NDatePicker: true } },
  })
  await flushPromises()
  if (recurring) { await button(wrapper, en.recurring.edit).trigger('click'); await flushPromises() }
  return wrapper
}
async function setupWithRouter() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/expenses/:id/edit', component: ExpenseEdit }, { path: '/fixed-assets', component: ExpenseEdit }] })
  await router.push('/expenses/legacy/edit')
  await router.isReady()
  const wrapper = mount(ExpenseEdit, {
    global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { NDatePicker: true } },
  })
  await flushPromises()
  return { router, wrapper }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.items = [legacy()]
  mocks.getExpense.mockResolvedValue(legacy())
  mocks.listAttachments.mockResolvedValue([])
  mocks.updateExpense.mockImplementation(async (_id, body) => ({ ...legacy(), ...body }))
  mocks.updateRecurringExpense.mockResolvedValue(legacy())
  mocks.fetchRecurringExpenses.mockResolvedValue(undefined)
  mocks.post.mockResolvedValue(result('121.00'))
  mocks.get.mockImplementation(async (path: string) => {
    if (path.endsWith('/cost-preview')) return result('100.00')
    if (path.endsWith('expense-categories')) return { items: [{ id: 'cat', name: 'Office' }] }
    if (path.endsWith('vat-treatments')) return { items: [{ id: 'tax', code: 'DOMESTIC', label: 'Domestic', side: 'PURCHASE' }] }
    return { items: [{ id: 'rate', percent: '21', label: 'Standard' }] }
  })
})

describe('Expense cost comparison', () => {
  it.each(['success', 'error'] as const)('keeps a newer purchase page intact when an older conversion completes with %s', async outcome => {
    const pending = deferred<unknown>()
    mocks.post.mockResolvedValueOnce(conversionPreview).mockReturnValueOnce(pending.promise)
    const { router, wrapper } = await setupWithRouter()
    await button(wrapper, en.fixedAssets.previewButton).trigger('click')
    await flushPromises()
    await button(wrapper, en.fixedAssets.confirmConversion).trigger('click')
    expect(mocks.post).toHaveBeenLastCalledWith('/api/v1/fixed-assets/expenses/legacy/convert', expect.objectContaining({ preview_token: 'token' }))

    await router.push('/expenses/other/edit')
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
    state.supplierName = 'B unsaved work'
    if (outcome === 'success') pending.resolve({})
    else pending.reject(new Error('First purchase conversion failed'))
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/expenses/other/edit')
    expect(state.supplierName).toBe('B unsaved work')
    expect(state.assetConversionLoading).toBe(false)
    expect(wrapper.text()).not.toContain('First purchase conversion failed')
    expect(wrapper.text()).not.toContain(en.fixedAssets.confirmConversion)
    wrapper.unmount()
  })

  it('navigates after a successful conversion that still belongs to the current purchase', async () => {
    mocks.post.mockResolvedValueOnce(conversionPreview).mockResolvedValueOnce({
      id: 'asset-1', acquisition_date: '2027-01-01',
    })
    const { router, wrapper } = await setupWithRouter()
    await button(wrapper, en.fixedAssets.previewButton).trigger('click')
    await flushPromises()
    await button(wrapper, en.fixedAssets.confirmConversion).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/fixed-assets?asset=asset-1&year=2027')
    wrapper.unmount()
  })

  it('shows an error when a failed conversion still belongs to the current purchase', async () => {
    mocks.post.mockResolvedValueOnce(conversionPreview).mockRejectedValueOnce(new Error('Conversion failed'))
    const { router, wrapper } = await setupWithRouter()
    await button(wrapper, en.fixedAssets.previewButton).trigger('click')
    await flushPromises()
    await button(wrapper, en.fixedAssets.confirmConversion).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/expenses/legacy/edit')
    expect(wrapper.text()).toContain('Conversion failed')
    wrapper.unmount()
  })

  it('does not navigate when a pending conversion completes after the editor unmounts', async () => {
    const pending = deferred<unknown>()
    mocks.post.mockResolvedValueOnce(conversionPreview).mockReturnValueOnce(pending.promise)
    const { router, wrapper } = await setupWithRouter()
    await button(wrapper, en.fixedAssets.previewButton).trigger('click')
    await flushPromises()
    await button(wrapper, en.fixedAssets.confirmConversion).trigger('click')
    wrapper.unmount()
    const routeAfterUnmount = router.currentRoute.value.fullPath
    pending.resolve({ id: 'asset-1' })
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(routeAfterUnmount)
    expect(router.currentRoute.value.fullPath).not.toBe('/fixed-assets')
  })

  it('shows an existing asset link and hides the conversion form for a linked purchase', async () => {
    mocks.get.mockImplementation(async (path: string) => {
      if (path.endsWith('/cost-preview')) return {
        ...result('2000.00'), linked_fixed_asset_id: 'asset-1', effective_annual_cost: '77.42',
      }
      if (path.endsWith('expense-categories')) return { items: [{ id: 'cat', name: 'Office' }] }
      if (path.endsWith('vat-treatments')) return { items: [{ id: 'tax', code: 'DOMESTIC', label: 'Domestic', side: 'PURCHASE' }] }
      return { items: [{ id: 'rate', percent: '21', label: 'Standard' }] }
    })
    const { router, wrapper: w } = await setupWithRouter()
    expect(mocks.get.mock.calls.map(([path]) => path)).toContain('/api/v1/expenses/legacy/cost-preview')
    expect(w.text()).toContain(en.fixedAssets.linkedSourceNotice)
    expect(w.text()).toContain(en.fixedAssets.openRegisteredAsset)
    expect(w.text()).not.toContain(en.fixedAssets.previewTitle)
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Saved purchase P&L inputs: 2000.00')
    expect(w.text()).toContain('Current fixed asset P&L cost: 77.42')
    expect(w.text()).not.toContain('Current: 2000.00')
    const state = (w.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
    state.expenseDate = '2028-01-01'
    await button(w, en.fixedAssets.openRegisteredAsset).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/fixed-assets?asset=asset-1&year=2026')
    w.unmount()
  })

  it('uses a fresh saved-cost GET as the ordinary purchase baseline after saving', async () => {
    let reads = 0
    mocks.get.mockImplementation(async (path: string) => {
      if (path.endsWith('/cost-preview')) {
        reads += 1
        return result(reads <= 2 ? '100.00' : '121.00')
      }
      if (path.endsWith('expense-categories')) return { items: [{ id: 'cat', name: 'Office' }] }
      if (path.endsWith('vat-treatments')) return { items: [{ id: 'tax', code: 'DOMESTIC', label: 'Domestic', side: 'PURCHASE' }] }
      return { items: [{ id: 'rate', percent: '21', label: 'Standard' }] }
    })
    const w = await setup()
    await field(w, en.expenses.plCostBasis).find('select').setValue('AUTO')
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/expenses/legacy/cost-preview')
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/expenses/cost-preview', expect.objectContaining({ pl_cost_basis: 'AUTO', net_amount: 100, category_id: 'cat', deductible: false }))
    expect(w.text()).toContain('Current: 100.00')
    expect(w.text()).not.toContain('Current fixed asset P&L cost')
    expect(w.text()).toContain('Proposed: 121.00')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(w.text()).not.toContain('Proposed:')
    expect(mocks.updateExpense).toHaveBeenCalledWith('legacy', expect.objectContaining({ pl_cost_basis: 'AUTO' }))
    mocks.post.mockResolvedValueOnce(result('130.00'))
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Current: 121.00')
    expect(w.text()).toContain('Proposed: 130.00')
    expect(w.text()).not.toContain('Current: 100.00')
    w.unmount()
  })

  it.each([
    [en.expenses.category, 'select', 'other-cat'],
    [en.expenses.plCostBasis, 'select', 'GROSS'],
    [en.expenses.includeInPl, 'input', false],
    [en.expenses.netAmount, 'input', '101'],
    [en.expenses.vatAmount, 'input', '22'],
    [en.expenses.vatTreatment, 'select', 'other'],
    [en.expenses.deductible, 'input', true],
    [en.expenses.businessPercentage, 'input', '50'],
    [en.expenses.depreciationYears, 'input', '2'],
  ])('invalidates displayed and pending comparisons when %s changes', async (label, selector, value) => {
    const w = await setup()
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Proposed: 121.00')
    let finish!: (r: unknown) => void
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await button(w, en.expenses.previewCost).trigger('click')
    await field(w, label as string).find(selector as string).setValue(value)
    expect(w.text()).not.toContain('Proposed:')
    finish(result('999.99'))
    await flushPromises()
    expect(w.text()).not.toContain('Proposed:')
    expect(w.text()).not.toContain('999.99')
    w.unmount()
  })

  it('discards a pending comparison when saving the same form', async () => {
    const w = await setup()
    let finish!: (r: unknown) => void
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await button(w, en.expenses.previewCost).trigger('click')
    await w.find('form').trigger('submit')
    await flushPromises()
    finish(result('999.99'))
    await flushPromises()
    expect(w.text()).not.toContain('Proposed:')
    w.unmount()
  })

  it('shows a failed comparison without retaining a previous current or proposed value', async () => {
    const w = await setup()
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    mocks.get.mockRejectedValueOnce(new Error('Current value unavailable'))
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(w.text()).toContain(en.expenses.costPreviewError)
    expect(w.text()).not.toContain('Current:')
    expect(w.text()).not.toContain('Proposed:')
    w.unmount()
  })

  it('does not let an older response replace a newer comparison or its loading state', async () => {
    const w = await setup()
    let finish!: (r: unknown) => void
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await button(w, en.expenses.previewCost).trigger('click')
    await field(w, en.expenses.plCostBasis).find('select').setValue('AUTO')
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    finish(result('999.99'))
    await flushPromises()
    expect(w.text()).toContain('Proposed: 121.00')
    expect(w.text()).not.toContain('999.99')
    w.unmount()
  })
})

describe('Recurring template cost comparison', () => {
  it.each(['AUTO', 'GROSS'])('previews legacy NET → %s for future generated Expenses', async mode => {
    const w = await setup(true)
    expect((field(w, en.expenses.plCostBasis).find('select').element as HTMLSelectElement).value).toBe('NET')
    await field(w, en.expenses.plCostBasis).find('select').setValue(mode)
    await button(w, en.expenses.previewCost).trigger('click')
    await flushPromises()
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/recurring-expenses/legacy/cost-preview')
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/expenses/cost-preview', expect.objectContaining({ pl_cost_basis: mode }))
    expect(w.text()).toContain('Current: 100.00')
    expect(w.text()).toContain('Proposed: 121.00')
    expect(w.text()).toContain(en.recurring.costPreviewHint)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateRecurringExpense).toHaveBeenCalledWith('legacy', expect.objectContaining({ pl_cost_basis: mode }))
    expect(w.text()).not.toContain('Proposed:')
    await button(w, en.recurring.edit).trigger('click')
    expect(w.text()).not.toContain('Proposed:')
    w.unmount()
  })

  it('clears a pending result on modal close and cannot restore it on reopen', async () => {
    const w = await setup(true)
    let finish!: (r: unknown) => void
    mocks.post.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await button(w, en.expenses.previewCost).trigger('click')
    await button(w, en.recurring.cancel).trigger('click')
    await button(w, en.recurring.edit).trigger('click')
    finish(result('999.99'))
    await flushPromises()
    expect(w.text()).not.toContain('Proposed:')
    w.unmount()
  })
})
