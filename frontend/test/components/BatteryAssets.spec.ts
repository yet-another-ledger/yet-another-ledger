import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), postFile: vi.fn() }))
vi.mock('../../src/api/http', async (importOriginal) => ({ ...await importOriginal<typeof import('../../src/api/http')>(), ...api }))
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /></div>' }
  const button = { inheritAttrs: false, props: ['disabled', 'loading', 'type'], template: '<button v-bind="$attrs" :disabled="disabled"><slot /></button>' }
  const input = { inheritAttrs: false, props: ['value', 'type', 'placeholder', 'disabled'], emits: ['update:value'], template: '<textarea v-if="type === \'textarea\'" v-bind="$attrs" :disabled="disabled" :value="value" @input="$emit(\'update:value\',$event.target.value)" /><input v-else v-bind="$attrs" :disabled="disabled" :value="value" :placeholder="placeholder" @input="$emit(\'update:value\',$event.target.value)" />' }
  return {
    NAlert: box, NButton: button, NCard: box,
    NCheckbox: { props: ['checked'], emits: ['update:checked'], template: '<label><input type="checkbox" :checked="checked" @change="$emit(\'update:checked\',$event.target.checked)" /><slot /></label>' },
    NDatePicker: { props: ['formattedValue'], emits: ['update:formattedValue'], template: '<input type="date" :value="formattedValue" @input="$emit(\'update:formattedValue\',$event.target.value)" />' },
    NFormItem: box, NInput: input,
    NInputNumber: { name: 'NInputNumber', inheritAttrs: false, props: ['value', 'disabled'], emits: ['update:value'], template: '<input type="number" v-bind="$attrs" :disabled="disabled" :value="value" @input="$emit(\'update:value\',($event.target.value === \'\' ? null : Number($event.target.value)))" />' },
    NPagination: { name: 'NPagination', props: ['page', 'pageSize', 'itemCount', 'disabled'], emits: ['update:page'], template: '<nav><button :disabled="disabled || page === 1" @click="$emit(\'update:page\',page-1)">Previous page</button><button :disabled="disabled || page*pageSize >= itemCount" @click="$emit(\'update:page\',page+1)">Next page</button></nav>' },
    NList: box, NListItem: box, NSelect: { props: ['value', 'options'], emits: ['update:value'], template: '<select :value="value" @change="$emit(\'update:value\',Number($event.target.value))"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>' },
    NSpace: box, NTag: box, NText: box, NUpload: { name: 'NUpload', template: '<div><slot /></div>' },
  }
})

import { ApiError } from '../../src/api/http'
import en from '../../src/locales/en.json'
import BatteryAssets from '../../src/views/battery/BatteryAssets.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
const asset = {
  id: 'asset-1', acquisition_expense_id: 'expense-1', acquisition_expense_date: '2024-12-20',
  in_service_date: '2025-02-01', applicable_years: [2025, 2026, 2027, 2028, 2029],
  net_basis: '10000.00', purchase_vat: '2100.00', purchase_vat_rate_percent: '21.000', currency: 'EUR',
  version: 1,
  ordinary_vat_full_deduction_confirmed: true, historical_acknowledged_periods: ['2024-Q4'],
}
function render(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/battery-assets/:id?', component: BatteryAssets }] })
  return router.push(path).then(async () => {
    await router.isReady()
    const wrapper = mount(BatteryAssets, { global: { plugins: [router, i18n] } })
    await flushPromises()
    return { wrapper, router }
  })
}
function revisionReasonInput(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('input').find(row => row.element.type === 'text')!
}

describe('BatteryAssets', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?')
      ? Promise.resolve({ items: [], total: 0, limit: 100, offset: 0 }) : Promise.resolve(null))
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('previews, requires historical acknowledgement, then registers and navigates to the first year', async () => {
    api.post.mockImplementation((url: string) => url.endsWith('/preview')
      ? Promise.resolve({ eligible: true, conflict_code: null, net_basis: '10000', purchase_vat: '2100', purchase_vat_rate_percent: '21', applicable_years: [2025, 2026, 2027, 2028, 2029], affected_historical_periods: ['2024-Q4'] })
      : Promise.resolve(asset))
    const { wrapper, router } = await render('/battery-assets?expense_id=expense-1')
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await button('Preview registration').trigger('click')
    await flushPromises()
    expect(button('Register asset').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('2024-Q4')
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await button('Preview registration').trigger('click')
    await flushPromises()
    const checks = wrapper.findAll('input[type="checkbox"]')
    ;(checks[1].element as HTMLInputElement).checked = true
    await checks[1].trigger('change')
    await flushPromises()
    expect((button('Register asset').element as HTMLButtonElement).disabled).toBe(false)
    await button('Register asset').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/battery-assets', expect.objectContaining({
      acquisition_expense_id: 'expense-1', owner_confirms_ordinary_vat_full_deduction: true,
      acknowledge_affected_periods: true,
    }))
    expect(router.currentRoute.value.fullPath).toBe('/battery-assets/asset-1?year=2025')
    wrapper.unmount()
  })

  it('saves an explicit zero, calculates and confirms server values, then locks confirmed inputs', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [asset], total: 1, limit: 100, offset: 0 })
      if (url === '/api/v1/battery-assets/asset-1') return Promise.resolve(asset)
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(null)
      return Promise.resolve(null)
    })
    api.put.mockResolvedValue({ ...asset, version: 1, private_use_percentage: '0', basis_note: 'No private use', confirmed: false, calculation_base: null, calculation_vat: null, evidence: [] })
    api.post.mockImplementation((url: string) => url.endsWith('/calculate')
      ? Promise.resolve({ calculation_base: '0.00', calculation_vat: '0.00', annual_use_id: 'annual-1', year: 2025, version: 1 })
      : Promise.resolve({ ...asset, version: 2, private_use_percentage: '0', basis_note: 'No private use', confirmed: true, calculation_base: '0.00', calculation_vat: '0.00', evidence: [] }))
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const addEvidence = () => wrapper.findAll('button').find(row => row.text() === 'Add evidence')!
    expect(addEvidence().attributes('disabled')).toBeDefined()
    await wrapper.find('input[type="number"]').setValue('0')
    await wrapper.find('textarea').setValue('No private use')
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await button('Save annual input').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/battery-assets/asset-1/annual-use/2025', {
      expected_version: null, private_use_percentage: 0, basis_note: 'No private use',
    })
    expect(addEvidence().attributes('disabled')).toBeUndefined()
    await button('Preview Q4 calculation').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Q4 box 1d: base 0.00, VAT 0.00')
    await button('Confirm annual calculation').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/asset-1/annual-use/2025/confirm', { expected_version: 1, expected_asset_version: 1 })
    expect((wrapper.find('input[type="number"]').element as HTMLInputElement).disabled).toBe(false)
    expect(button('Preview annual correction').exists()).toBe(true)
    expect(addEvidence().attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('keeps evidence locked through correction preview and unlocks it after saving the zero revision', async () => {
    let current = { ...annualRecord(), confirmed: true,
      calculation_base: '900.00', calculation_vat: '189.00',
      evidence: [{ id: 'old-evidence', filename: 'original-45.pdf', media_type: 'application/pdf' }] }
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [asset], total: 1 })
      if (url === '/api/v1/battery-assets/asset-1') return Promise.resolve(asset)
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(current)
      return Promise.resolve({ items: [] })
    })
    api.post.mockImplementation((url: string) => {
      if (url.endsWith('/revisions/preview')) return Promise.resolve({ affected_periods: ['2025-Q4'],
        current_calculation_base: '900.00', current_calculation_vat: '189.00',
        proposed_calculation_base: '0.00', proposed_calculation_vat: '0.00' })
      current = { ...current, private_use_percentage: '0', basis_note: 'Zero private use',
        version: 2, confirmed: false, calculation_base: null, calculation_vat: null }
      return Promise.resolve(current)
    })
    api.postFile.mockResolvedValue({ id: 'new-evidence' })
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    const uploadNode = wrapper.findComponent({ name: 'NUpload' })
    const upload = uploadNode.vm.$attrs['custom-request'] as (options: { file: { file: File } }) => Promise<void>
    const options = { file: { file: new File(['%PDF-test'], 'zero.pdf', { type: 'application/pdf' }) } }
    expect(button('Add evidence').attributes('disabled')).toBeDefined()
    expect(uploadNode.vm.$attrs.disabled).toBe(true)
    expect(wrapper.text()).toContain('Previewing a correction does not save it')
    await wrapper.find('input[type="number"]').setValue('0')
    await wrapper.find('textarea').setValue('Zero private use')
    await revisionReasonInput(wrapper).setValue('Correct EMS observation')
    await button('Preview annual correction').trigger('click')
    await flushPromises()
    await upload(options)
    expect(api.postFile).not.toHaveBeenCalled()
    expect(button('Add evidence').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('original-45.pdf')
    await wrapper.findAll('input[type="checkbox"]').at(-1)!.setValue(true)
    await button('Confirm annual correction').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/asset-1/annual-use/2025/revisions', {
      expected_version: 1, expected_asset_version: 1, private_use_percentage: 0,
      basis_note: 'Zero private use', reason: 'Correct EMS observation', acknowledge_affected_periods: ['2025-Q4'],
    })
    expect(button('Add evidence').attributes('disabled')).toBeUndefined()
    expect(uploadNode.vm.$attrs.disabled).toBe(false)
    expect(wrapper.text()).not.toContain('Previewing a correction does not save it')
    await upload(options)
    await flushPromises()
    expect(api.postFile).toHaveBeenCalledExactlyOnceWith(
      '/api/v1/battery-assets/asset-1/annual-use/2025/evidence', options.file.file,
    )
    expect(wrapper.text()).toContain('original-45.pdf')
    expect(wrapper.find('input[type="number"]').element.value).toBe('0')
    wrapper.unmount()
  })

  it.each([
    { status: 409, detail: { code: 'BATTERY_ANNUAL_CONFIRMED', detail: 'BATTERY_ANNUAL_CONFIRMED' }, message: 'select Confirm annual correction first' },
    { status: 422, detail: { code: 'BATTERY_EVIDENCE_INVALID', detail: 'Invalid PDF content.' }, message: 'Invalid PDF content.' },
    { status: 500, detail: { unknown: true }, message: 'Request failed (500).' },
  ])('renders readable evidence errors for status $status', async ({ status, detail, message }) => {
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [asset], total: 1 })
      if (url === '/api/v1/battery-assets/asset-1') return Promise.resolve(asset)
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(annualRecord())
      return Promise.resolve({ items: [] })
    })
    api.postFile.mockRejectedValueOnce(new ApiError(status, { detail }, '[object Object]'))
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const upload = wrapper.findComponent({ name: 'NUpload' }).vm.$attrs['custom-request'] as (options: { file: { file: File } }) => Promise<void>
    await upload({ file: { file: new File(['bad-pdf'], 'ems.pdf', { type: 'application/pdf' }) } })
    await flushPromises()
    expect(wrapper.text()).toContain(message)
    expect(wrapper.text()).not.toContain('[object Object]')
    wrapper.unmount()
  })

  it('loads the annual selected by a navigated report link and renders request failures', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [asset], total: 1, limit: 100, offset: 0 })
      if (url === '/api/v1/battery-assets/asset-1') return Promise.resolve(asset)
      if (url.endsWith('/annual-use/2026')) return Promise.reject(new Error('Annual input unavailable'))
      return Promise.resolve(null)
    })
    const { wrapper, router } = await render('/battery-assets')
    await router.push('/battery-assets/asset-1?year=2026')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/battery-assets/asset-1/annual-use/2026')
    expect(wrapper.text()).toContain('Annual input unavailable')
    wrapper.unmount()
  })

  it('invalidates asset revision preview and acknowledgement when fields change', async () => {
    api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?')
      ? Promise.resolve({ items: [asset], total: 1, limit: 100, offset: 0 })
      : url === '/api/v1/battery-assets/asset-1' ? Promise.resolve({ ...asset, version: 3 })
        : url.includes('/revisions') ? Promise.resolve({ items: [] }) : Promise.resolve(null))
    api.post.mockResolvedValue({
      asset_id: asset.id, expected_version: 3, before: { ...asset, version: 3 }, after: { ...asset, version: 4 },
      expense_before: { expense_date: '2024-12-20', net_amount: '10000', vat_amount: '2100' },
      expense_after: { expense_date: '2025-01-02', net_amount: '10000', vat_amount: '2100' },
      affected_periods: ['2024-Q4', '2025-Q1', '2025-Q4'], annual_previews: [], warnings: [],
    })
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    expect(api.get).toHaveBeenCalledWith('/api/v1/battery-assets/asset-1/revisions?limit=100')
    expect(api.get).toHaveBeenCalledWith(
      '/api/v1/battery-assets/asset-1/annual-use/2025/revisions?limit=100',
    )
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await wrapper.findAll('input[type="date"]')[0]!.setValue('2025-01-02')
    await wrapper.findAll('textarea').at(-1)!.setValue('Correct acquisition date')
    await button('Preview correction').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('2024-Q4, 2025-Q1, 2025-Q4')
    const checkbox = wrapper.findAll('input[type="checkbox"]').at(-1)!
    await checkbox.setValue(true)
    expect((button('Confirm correction').element as HTMLButtonElement).disabled).toBe(false)
    await wrapper.findAll('input[type="date"]')[0]!.setValue('2025-01-03')
    expect(wrapper.text()).not.toContain('2024-Q4, 2025-Q1, 2025-Q4')
    expect(wrapper.findAll('button').some(row => row.text() === 'Confirm correction')).toBe(false)
    wrapper.unmount()
  })

  it('echoes the server rate snapshot and discards a stale preview on save conflict', async () => {
    const token = 'a'.repeat(64)
    api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?')
      ? Promise.resolve({ items: [asset], total: 1 })
      : url === '/api/v1/battery-assets/asset-1' ? Promise.resolve(asset)
        : url.includes('/revisions') ? Promise.resolve({ items: [], total: 0 }) : Promise.resolve(null))
    api.post.mockImplementation((url: string) => url.endsWith('/preview') ? Promise.resolve({
      asset_id: asset.id, expected_version: 1, rate_snapshot: token,
      before: asset, after: { ...asset, version: 2 }, annual_previews: [], affected_periods: ['2025-Q4'],
      expense_before: { expense_date: '2024-12-20', net_amount: '10000', vat_amount: '2100' },
      expense_after: { expense_date: '2024-12-20', net_amount: '11000', vat_amount: '2100' },
    }) : Promise.reject(new ApiError(409, { detail: { code: 'BATTERY_PREVIEW_STALE' } }, '[object Object]')))
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    await wrapper.findAll('textarea').at(-1)!.setValue('Supplier rate correction')
    await button('Preview correction').trigger('click'); await flushPromises()
    await wrapper.findAll('input[type="checkbox"]').at(-1)!.setValue(true)
    await button('Confirm correction').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/asset-1/revisions', expect.objectContaining({ expected_rate_snapshot: token }))
    expect(wrapper.text()).toContain('Preview the correction again before saving.')
    expect(wrapper.findAll('button').some(row => row.text() === 'Confirm correction')).toBe(false)
    wrapper.unmount()
  })

  it('discards a late asset revision preview after switching assets', async () => {
    const pending = deferred<unknown>()
    api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?')
      ? Promise.resolve({ items: [{ ...asset, id: 'asset-1' }, { ...asset, id: 'asset-2' }], total: 2, limit: 100, offset: 0 })
      : url === '/api/v1/battery-assets/asset-1' || url === '/api/v1/battery-assets/asset-2'
        ? Promise.resolve({ ...asset, id: url.split('/').at(-1), version: 1 })
        : url.includes('/revisions') ? Promise.resolve({ items: [] }) : Promise.resolve(null))
    api.post.mockReturnValueOnce(pending.promise)
    const { wrapper, router } = await render('/battery-assets/asset-1?year=2025')
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await wrapper.findAll('textarea').at(-1)!.setValue('Correction on asset one')
    await button('Preview correction').trigger('click')
    await router.push('/battery-assets/asset-2?year=2025')
    await flushPromises()
    pending.resolve({ affected_periods: ['2025-Q4'], annual_previews: [] })
    await flushPromises()
    expect(wrapper.text()).not.toContain('2025-Q4')
    expect(wrapper.findAll('button').some(row => row.text() === 'Confirm correction')).toBe(false)
    wrapper.unmount()
  })

  it('loads retained out-of-window annual history and evidence as read-only', async () => {
    const revisedAsset = { ...asset, in_service_date: '2026-02-01', applicable_years: [2026, 2027, 2028, 2029, 2030] }
    const oldAnnual = { ...annualRecord(2025), confirmed: true,
      calculation_base: '900.00', calculation_vat: '189.00',
      evidence: [{ id: 'evidence-1', filename: 'old-bill.pdf', media_type: 'application/pdf' }] }
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [revisedAsset], total: 1, limit: 100, offset: 0 })
      if (url === '/api/v1/battery-assets/asset-1') return Promise.resolve(revisedAsset)
      if (url.endsWith('/revisions?limit=100')) return Promise.resolve({ items: [{ id: 'asset-rev-1', version: 2,
        reason: 'Corrected service date', actor_id: null, created_at: '2026-02-04T12:00:00Z',
        affected_periods: ['2025-Q4'], before_snapshot: { annual_before: [{ year: 2025 }] }, after_snapshot: {} }] })
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(oldAnnual)
      if (url.endsWith('/annual-use/2025/revisions?limit=100')) return Promise.resolve({ items: [] })
      return Promise.resolve(null)
    })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    await flushPromises()
    expect(wrapper.find('select').element).toHaveProperty('value', '2025')
    expect(wrapper.text()).toContain('outside the asset’s current five-year window')
    expect(wrapper.text()).toContain('900.00')
    const addEvidence = wrapper.findAll('button').find(row => row.text() === 'Add evidence')!
    expect((addEvidence.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('old-bill.pdf')
    const openEvidence = wrapper.findAll('button').find(row => row.text() === 'Open original')!
    await openEvidence.trigger('click')
    expect(open).toHaveBeenCalledWith(
      '/api/v1/battery-assets/asset-1/annual-use/2025/evidence/evidence-1/content', '_blank', 'noopener',
    )
    wrapper.unmount()
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function annualRecord(year = 2025, assetId = 'asset-1', pct: string | null = '45', note = `EMS ${year}`, version = 1) {
  return { id: `${assetId}-${year}`, asset_id: assetId, year, version,
    private_use_percentage: pct, basis_note: note, confirmed: false, evidence: [],
    calculation_base: null, calculation_vat: null, confirmed_at: null, asset_version_used: null }
}
function calculationRecord(year = 2025) {
  return { asset_id: 'asset-1', year, expected_version: 1, private_use_percentage: '45',
    calculation_base: '900.00', calculation_vat: '189.00', report_quarter: 4 }
}
function configureAnnual() {
  api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?')
    ? Promise.resolve({ items: [asset, { ...asset, id: 'asset-2' }], total: 2, limit: 100, offset: 0 })
    : !url.includes('/annual-use/') ? Promise.resolve({ ...asset, id: url.split('/').at(-1) })
    : Promise.resolve(annualRecord(Number(url.split('/').at(-1)), url.split('/')[4])))
  api.post.mockImplementation((url: string) => url.endsWith('/calculate')
    ? Promise.resolve(calculationRecord())
    : Promise.resolve({ ...annualRecord(), confirmed: true }))
}

describe('Battery annual target and saved snapshot safety', () => {
  beforeEach(() => { vi.clearAllMocks(); configureAnnual() })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('discards reversed year responses and prevents writes until the current year loads', async () => {
    const oldYear = deferred<ReturnType<typeof annualRecord>>()
    const currentYear = deferred<ReturnType<typeof annualRecord>>()
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url.endsWith('/2026') ? oldYear.promise
      : url.endsWith('/2027') ? currentYear.promise : original(url))
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    await wrapper.find('select').setValue('2026')
    expect(wrapper.find('textarea').element.value).toBe('')
    expect(button('Save annual input').attributes('disabled')).toBeDefined()
    await button('Save annual input').trigger('click')
    expect(api.put).not.toHaveBeenCalled()
    await wrapper.find('select').setValue('2027')
    currentYear.resolve(annualRecord(2027, 'asset-1', '70'))
    await flushPromises()
    oldYear.resolve(annualRecord(2026, 'asset-1', '20'))
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('EMS 2027')
    expect(wrapper.find('input[type="number"]').element.value).toBe('70')
    expect(api.put).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('ignores old asset loads and errors after route/query changes', async () => {
    const old = deferred<ReturnType<typeof annualRecord>>()
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url.endsWith('asset-1/annual-use/2025') ? old.promise : original(url))
    const { wrapper, router } = await render('/battery-assets/asset-1?year=2025')
    await router.push('/battery-assets/asset-2?year=2026')
    await flushPromises()
    old.reject(new Error('Stale asset error'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('Stale asset error')
    expect(wrapper.find('textarea').element.value).toBe('EMS 2026')
    await router.push('/battery-assets/asset-2?year=2027')
    await flushPromises()
    expect(wrapper.find('select').element.value).toBe('2027')
    expect(wrapper.find('textarea').element.value).toBe('EMS 2027')
    wrapper.unmount()
  })

  it.each(['percentage', 'basis', 'clear'])('invalidates preview on unsaved %s and requires an audited revision before recalculation', async (field) => {
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    await button('Preview Q4 calculation').trigger('click')
    await flushPromises()
    expect(button('Confirm annual calculation').attributes('disabled')).toBeUndefined()
    if (field === 'basis') await wrapper.find('textarea').setValue('Updated basis')
    else await wrapper.find('input[type="number"]').setValue(field === 'clear' ? '' : '0')
    expect(wrapper.text()).not.toContain('Q4 box 1d: base 900.00')
    expect(button('Confirm annual calculation').attributes('disabled')).toBeDefined()
    expect(button('Preview Q4 calculation').attributes('disabled')).toBeDefined()
    await button('Confirm annual calculation').trigger('click')
    expect(api.post.mock.calls.filter(([url]) => url.endsWith('/confirm'))).toHaveLength(0)
    const pct = field === 'clear' ? null : field === 'percentage' ? '0' : '45'
    const note = field === 'basis' ? 'Updated basis' : 'EMS 2025'
    await revisionReasonInput(wrapper).setValue('Correct annual observation')
    api.post.mockResolvedValueOnce({ affected_periods: ['2025-Q4'], current_calculation_base: '900.00', current_calculation_vat: '189.00', proposed_calculation_base: '0.00', proposed_calculation_vat: '0.00' })
    await button('Preview annual correction').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('2025-Q4')
    const ack = wrapper.findAll('input[type="checkbox"]').at(-1)!
    await ack.setValue(true)
    api.post.mockResolvedValueOnce(annualRecord(2025, 'asset-1', pct, note, 2))
    await button('Confirm annual correction').trigger('click')
    await flushPromises()
    expect(button('Confirm annual calculation').attributes('disabled')).toBeDefined()
    if (field === 'clear') {
      expect(button('Preview Q4 calculation').attributes('disabled')).toBeDefined()
    } else {
      await button('Preview Q4 calculation').trigger('click')
      await flushPromises()
      expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/asset-1/annual-use/2025/calculate', { expected_version: 2, expected_asset_version: 1 })
      await button('Confirm annual calculation').trigger('click')
      await flushPromises()
      expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/asset-1/annual-use/2025/confirm', { expected_version: 2, expected_asset_version: 1 })
    }
    wrapper.unmount()
  })

  it.each(['calculate', 'confirm', 'upload'].flatMap(operation => [
    { operation, fails: false }, { operation, fails: true },
  ]))('does not apply late $operation (fails=$fails) to another target or clear its busy state', async ({ operation, fails }) => {
    const old = deferred<unknown>()
    const { wrapper, router } = await render('/battery-assets/asset-1?year=2025')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    if (operation === 'confirm') {
      await button('Preview Q4 calculation').trigger('click')
      await flushPromises()
    }
    if (operation === 'upload') api.postFile.mockReturnValueOnce(old.promise)
    else api.post.mockReturnValueOnce(old.promise)
    if (operation === 'upload') {
      // NUpload's actual custom-request callback is forwarded through the stub.
      const uploadNode = wrapper.findAllComponents({ name: 'NUpload' })[0]
      const request = uploadNode?.vm.$attrs['custom-request'] as ((options: any) => Promise<void>)
      void request({ file: { file: new File(['%PDF-test'], 'ems.pdf', { type: 'application/pdf' }) } })
    } else {
      await button(operation === 'calculate' ? 'Preview Q4 calculation' : 'Confirm annual calculation').trigger('click')
    }
    await router.push('/battery-assets/asset-2?year=2026')
    await flushPromises()
    if (fails) old.reject(new Error('Stale command failed'))
    else old.resolve(operation === 'calculate' ? calculationRecord() : { ...annualRecord(), confirmed: true })
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('EMS 2026')
    expect(wrapper.text()).not.toContain('Stale command failed')
    expect(wrapper.find('input[type="number"]').attributes('disabled')).toBeUndefined()
    expect(button('Confirm annual calculation').attributes('disabled')).toBeDefined()
    expect(button('Preview Q4 calculation').attributes('disabled')).toBeUndefined()
    expect(api.get.mock.calls.filter(([url]) => url.endsWith('asset-1/annual-use/2025'))).toHaveLength(1)
    wrapper.unmount()
  })

  it('discards registration preview and completion after leaving that acquisition target', async () => {
    const preview = deferred<unknown>()
    api.post.mockReturnValueOnce(preview.promise)
    const { wrapper, router } = await render('/battery-assets?expense_id=expense-1')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    await button('Preview registration').trigger('click')
    await router.push('/battery-assets?expense_id=expense-2')
    preview.resolve({ eligible: true, affected_historical_periods: [] })
    await flushPromises()
    expect(button('Register asset').attributes('disabled')).toBeDefined()
    api.post.mockResolvedValueOnce({ eligible: true, affected_historical_periods: [] })
    await button('Preview registration').trigger('click')
    await flushPromises()
    const register = deferred<unknown>()
    api.post.mockReturnValueOnce(register.promise)
    await button('Register asset').trigger('click')
    await router.push('/battery-assets/asset-2?year=2026')
    await flushPromises()
    register.resolve(asset)
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/battery-assets/asset-2?year=2026')
    wrapper.unmount()
  })

  it('discards a stale asset-list response and leaves failed current loads unwritable', async () => {
    const list = deferred<unknown>()
    api.get.mockReturnValueOnce(list.promise)
    const { wrapper, router } = await render('/battery-assets/asset-1?year=2025')
    await router.push('/battery-assets/asset-2?year=2026')
    await flushPromises()
    list.resolve({ items: [asset] })
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('EMS 2026')
    api.get.mockRejectedValueOnce(new Error('Current load failed'))
    await wrapper.find('select').setValue('2027')
    await flushPromises()
    expect(wrapper.text()).toContain('Current load failed')
    const save = wrapper.findAll('button').find(row => row.text() === 'Save annual input')!
    expect(save.attributes('disabled')).toBeDefined()
    await save.trigger('click')
    expect(api.put).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('discards an annual revision preview after newer edits and a calculation after any edit', async () => {
    const calc = deferred<ReturnType<typeof calculationRecord>>()
    const { wrapper } = await render('/battery-assets/asset-1?year=2025')
    const button = (text: string) => wrapper.findAll('button').find(row => row.text() === text)!
    await revisionReasonInput(wrapper).setValue('Corrected usage')
    const revisionPreview = deferred<unknown>()
    api.post.mockReturnValueOnce(revisionPreview.promise)
    await button('Preview annual correction').trigger('click')
    const numberInput = wrapper.findAllComponents({ name: 'NInputNumber' })[0]!
    numberInput.vm.$emit('update:value', 0)
    revisionPreview.resolve({ affected_periods: ['2025-Q4'] })
    await flushPromises()
    expect(wrapper.find('input[type="number"]').element.value).toBe('0')
    expect(wrapper.findAll('button').some(row => row.text() === 'Confirm annual correction')).toBe(false)
    api.post.mockReturnValueOnce(calc.promise)
    expect(button('Preview Q4 calculation').attributes('disabled')).toBeDefined()
    await revisionReasonInput(wrapper).setValue('')
    numberInput.vm.$emit('update:value', 10)
    numberInput.vm.$emit('update:value', 0)
    expect(button('Confirm annual calculation').attributes('disabled')).toBeDefined()
    calc.resolve(calculationRecord())
    await flushPromises()
    expect(button('Confirm annual calculation').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})

// UUIDs and complete detail fields mirror the paginated API contract.
const pagedAssets = Array.from({ length: 201 }, (_, index) => ({
  ...asset, id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
  acquisition_expense_id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
  version: 1, rule_version: 1, owner_confirmed_at: '2025-01-01T00:00:00Z', registered_by: null,
  historical_acknowledged_at: '2025-01-01T00:00:00Z', created_at: '2025-01-01T00:00:00Z',
}))
function configurePages() {
  api.get.mockImplementation((url: string) => {
    if (url.includes('/revisions?')) return Promise.resolve({ items: [], total: 0, limit: 100, offset: 0 })
    if (url.startsWith('/api/v1/battery-assets?limit=')) {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'))
      return Promise.resolve({ items: pagedAssets.slice(offset, offset + 100), total: pagedAssets.length, limit: 100, offset })
    }
    const id = url.split('/')[4]
    if (!url.includes('/annual-use/')) return Promise.resolve(pagedAssets.find(row => row.id === id))
    return Promise.resolve(annualRecord(Number(url.split('/').at(-1)), id))
  })
}

describe('Battery collection pages and independent route detail', () => {
  beforeEach(() => { vi.clearAllMocks(); configurePages() })

  it('loads an off-page report target independently of a delayed first page and saves its missing annual input', async () => {
    const list = deferred<unknown>()
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url.startsWith('/api/v1/battery-assets?limit=') ? list.promise
      : url.includes('/annual-use/') ? Promise.resolve(null) : original(url))
    const target = pagedAssets[100]!
    const { wrapper } = await render(`/battery-assets/${target.id}?year=2026`)
    expect(api.get).toHaveBeenCalledWith(`/api/v1/battery-assets/${target.id}`)
    expect(api.get).toHaveBeenCalledWith(`/api/v1/battery-assets/${target.id}/annual-use/2026`)
    await wrapper.find('input[type="number"]').setValue('0')
    await wrapper.find('textarea').setValue('Off-page EMS basis')
    api.put.mockResolvedValue(annualRecord(2026, target.id, '0', 'Off-page EMS basis'))
    await wrapper.findAll('button').find(row => row.text() === 'Save annual input')!.trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(`/api/v1/battery-assets/${target.id}/annual-use/2026`, {
      expected_version: null, private_use_percentage: 0, basis_note: 'Off-page EMS basis',
    })
    list.resolve({ items: pagedAssets.slice(0, 100), total: 201, limit: 100, offset: 0 })
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('Off-page EMS basis')
    wrapper.unmount()
  })

  it('pages through all assets and opens the last item without losing dirty inputs while paging', async () => {
    const { wrapper, router } = await render(`/battery-assets/${pagedAssets[0]!.id}?year=2025`)
    await wrapper.find('textarea').setValue('Unsaved current basis')
    const next = () => wrapper.findAll('button').find(row => row.text() === 'Next page')!
    await next().trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/battery-assets?limit=100&offset=100')
    expect(wrapper.find('textarea').element.value).toBe('Unsaved current basis')
    expect(wrapper.findAll('button').find(row => row.text() === 'Preview Q4 calculation')!.attributes('disabled')).toBeDefined()
    await next().trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/battery-assets?limit=100&offset=200')
    expect(next().attributes('disabled')).toBeDefined()
    // The list item invokes the real route navigation handler.
    await wrapper.find('.battery-layout').findAll('[clickable] > div')[0]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe(pagedAssets[200]!.id)
    expect(wrapper.find('textarea').element.value).toBe('EMS 2025')
    wrapper.unmount()
  })

  it('keeps a failed list page separate from editable detail and retries the same offset', async () => {
    const original = api.get.getMockImplementation()!
    let fail = true
    api.get.mockImplementation((url: string) => url.endsWith('offset=100') && fail
      ? Promise.reject(new Error('Page unavailable')) : original(url))
    const { wrapper } = await render(`/battery-assets/${pagedAssets[100]!.id}?year=2026`)
    await wrapper.find('textarea').setValue('Unsaved basis')
    await revisionReasonInput(wrapper).setValue('Corrected basis note')
    await wrapper.findAll('button').find(row => row.text() === 'Next page')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Page unavailable')
    expect(wrapper.find('textarea').element.value).toBe('Unsaved basis')
    expect(wrapper.findAll('button').find(row => row.text() === 'Preview annual correction')!.attributes('disabled')).toBeUndefined()
    fail = false
    await wrapper.findAll('button').find(row => row.text() === 'Retry')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Page unavailable')
    expect(api.get.mock.calls.filter(([url]) => url.endsWith('offset=100'))).toHaveLength(2)
    expect(wrapper.find('textarea').element.value).toBe('Unsaved basis')
    wrapper.unmount()
  })

  it.each([false, true])('ignores superseded page completion (fails=%s) across route and page changes', async (fails) => {
    const oldPage = deferred<unknown>()
    const currentPage = deferred<unknown>()
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url.endsWith('offset=100') ? oldPage.promise
      : url.endsWith('offset=200') ? currentPage.promise : original(url))
    const { wrapper, router } = await render(`/battery-assets/${pagedAssets[0]!.id}?year=2025`)
    const pagination = wrapper.findComponent({ name: 'NPagination' })
    pagination.vm.$emit('update:page', 2)
    await flushPromises()
    await router.push(`/battery-assets/${pagedAssets[100]!.id}?year=2026`)
    await flushPromises()
    // A queued page event must get a distinct request generation.
    pagination.vm.$emit('update:page', 3)
    await flushPromises()
    if (fails) oldPage.reject(new Error('Stale page failed'))
    else oldPage.resolve({ items: pagedAssets.slice(100, 200), total: 201, limit: 100, offset: 100 })
    await flushPromises()
    expect(pagination.props('disabled')).toBe(true)
    expect(wrapper.text()).not.toContain('Stale page failed')
    expect(wrapper.find('textarea').element.value).toBe('EMS 2026')
    currentPage.resolve({ items: pagedAssets.slice(200), total: 201, limit: 100, offset: 200 })
    await flushPromises()
    expect(pagination.props('disabled')).toBe(false)
    expect(wrapper.findAll('[clickable] > div')).toHaveLength(1)
    wrapper.unmount()
  })

  it.each([false, true])('ignores an off-page detail completion (fails=%s) after a new route wins', async (fails) => {
    const old = deferred<unknown>()
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === `/api/v1/battery-assets/${pagedAssets[100]!.id}` ? old.promise : original(url))
    const { wrapper, router } = await render(`/battery-assets/${pagedAssets[100]!.id}?year=2025`)
    expect(wrapper.find('textarea').exists()).toBe(false)
    await router.push(`/battery-assets/${pagedAssets[200]!.id}?year=2027`)
    await flushPromises()
    if (fails) old.reject(new Error('Stale detail failed'))
    else old.resolve(pagedAssets[100])
    await flushPromises()
    expect(wrapper.text()).not.toContain('Stale detail failed')
    expect(wrapper.find('textarea').element.value).toBe('EMS 2027')
    expect(api.get).not.toHaveBeenCalledWith(`/api/v1/battery-assets/${pagedAssets[100]!.id}/annual-use/2025`)
    wrapper.unmount()
  })

  it('loads the latest query year after delayed detail even when the first list request fails', async () => {
    const detail = deferred<unknown>()
    const original = api.get.getMockImplementation()!
    const target = pagedAssets[100]!
    api.get.mockImplementation((url: string) => url.includes('?limit=')
      ? Promise.reject(new Error('First page unavailable'))
      : url === `/api/v1/battery-assets/${target.id}` ? detail.promise : original(url))
    const { wrapper, router } = await render(`/battery-assets/${target.id}?year=2025`)
    expect(wrapper.text()).toContain('First page unavailable')
    await router.push(`/battery-assets/${target.id}?year=2028`)
    detail.resolve(target)
    await flushPromises()
    expect(wrapper.find('select').element.value).toBe('2028')
    expect(wrapper.find('textarea').element.value).toBe('EMS 2028')
    expect(api.get).not.toHaveBeenCalledWith(`/api/v1/battery-assets/${target.id}/annual-use/2025`)
    wrapper.unmount()
  })

  it('shows a current detail failure and never substitutes a listed asset or writes an annual record', async () => {
    const original = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === `/api/v1/battery-assets/${pagedAssets[0]!.id}`
      ? Promise.reject(new Error('Detail unavailable')) : original(url))
    const { wrapper } = await render(`/battery-assets/${pagedAssets[0]!.id}?year=2025`)
    expect(wrapper.text()).toContain('Detail unavailable')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(api.get.mock.calls.some(([url]) => url.includes('/annual-use/'))).toBe(false)
    expect(api.put).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('Battery registration local calendar date', () => {
  beforeEach(() => { vi.clearAllMocks(); configurePages() })
  afterEach(() => vi.useRealTimers())

  it.each([
    { local: [2027, 0, 1, 0, 30], expected: '2027-01-01' },
    { local: [2027, 6, 15, 0, 30], expected: '2027-07-15' },
  ])('defaults to $expected at local 00:30 and preserves a selected historical date and acknowledgement', async ({ local, expected }) => {
    // Run with TZ=Europe/Amsterdam to cover UTC's preceding day, including New Year.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(local[0]!, local[1]!, local[2]!, local[3]!, local[4]!))
    expect(new Date().getFullYear()).toBe(2027)
    if (process.env.TZ === 'Europe/Amsterdam') expect(new Date().toISOString().slice(0, 10)).not.toBe(expected)
    const { wrapper } = await render('/battery-assets?expense_id=expense-1')
    const date = wrapper.find('input[type="date"]')
    expect(date.element.value).toBe(expected)
    api.post.mockResolvedValue({ eligible: true, affected_historical_periods: ['2024-Q4'] })
    const preview = () => wrapper.findAll('button').find(row => row.text() === 'Preview registration')!
    await preview().trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/preview', expect.objectContaining({ in_service_date: expected }))
    await date.setValue('2024-03-31')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(1)
    await preview().trigger('click')
    await flushPromises()
    await wrapper.findAll('input[type="checkbox"]')[1]!.setValue(true)
    expect(date.element.value).toBe('2024-03-31')
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/battery-assets/preview', expect.objectContaining({ in_service_date: '2024-03-31' }))
    expect(wrapper.findAll('input[type="checkbox"]')[1]!.element.checked).toBe(true)
    wrapper.unmount()
  })
})

describe('Battery readable and complete revision history', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
  const fullAsset = { ...asset, id: '00000000-0000-4000-8000-000000000001',
    acquisition_expense_id: '00000000-0000-4000-8000-000000000002', rule_version: 1,
    owner_confirmed_at: '2025-02-01T10:00:00Z', registered_by: '00000000-0000-4000-8000-000000000003',
    historical_acknowledged_at: '2025-02-01T10:00:00Z', created_at: '2025-02-01T10:00:00Z' }
  const prior = { id: '00000000-0000-4000-8000-000000000004', asset_id: fullAsset.id,
    year: 2025, version: 1, private_use_percentage: '45.000', basis_note: 'EMS ORIGINAL PRIOR VALUE',
    confirmed: true, confirmed_at: '2025-12-31T20:00:00Z', calculation_base: '900.00',
    calculation_vat: '189.00', asset_version_used: 1, evidence: [] }
  const current = { ...prior, version: 2, private_use_percentage: '30.000',
    basis_note: 'EMS REVISED CURRENT VALUE', confirmed: false, confirmed_at: null,
    calculation_base: null, calculation_vat: null, asset_version_used: null }
  const rev = { id: '00000000-0000-4000-8000-000000000005', version: 2,
    reason: 'Independent annual audit revision', actor_id: fullAsset.registered_by,
    created_at: '2026-01-01T10:00:00Z', before_snapshot: prior, after_snapshot: current }
  it('retains readable prior calculation and basis in annual audit UI', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [fullAsset], total: 1, limit: 100, offset: 0 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve(fullAsset)
      if (url.startsWith(`/api/v1/battery-assets/${fullAsset.id}/annual-use/2025/revisions`)) return Promise.resolve({ items: [rev], total: 1, limit: 100, offset: 0 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}/annual-use/2025`) return Promise.resolve(current)
      return Promise.resolve({ items: [], total: 0, limit: 100, offset: 0 })
    })
    const { wrapper } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      expect(wrapper.text()).toContain(rev.reason)
      expect(wrapper.text()).toContain('EMS ORIGINAL PRIOR VALUE')
      expect(wrapper.text()).toContain('900.00')
      expect(wrapper.text()).toContain('189.00')
      expect(wrapper.text()).toContain(fullAsset.registered_by)
    } finally { wrapper.unmount() }
  })
  it('lets an owner inspect retained audit rows after the first100', async () => {
    const rows = Array.from({ length: 101 }, (_, n) => ({ ...rev,
      id: `00000000-0000-4000-8000-${String(n + 10).padStart(12, '0')}`,
      version: 102 - n, reason: n === 100 ? 'OLDEST ORIGINAL AUDIT REASON' : `Revision ${102 - n}` }))
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [fullAsset], total: 1, limit: 100, offset: 0 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve({ ...fullAsset, version: 102 })
      if (url.startsWith(`/api/v1/battery-assets/${fullAsset.id}/annual-use/2025/revisions`)) {
        const query = new URL(url, 'http://localhost').searchParams
        const offset = Number(query.get('offset') ?? 0); const limit = Number(query.get('limit') ?? 50)
        return Promise.resolve({ items: rows.slice(offset, offset + limit), total: 101, limit, offset })
      }
      if (url === `/api/v1/battery-assets/${fullAsset.id}/annual-use/2025`) return Promise.resolve(current)
      return Promise.resolve({ items: [], total: 0, limit: 100, offset: 0 })
    })
    const { wrapper } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      for (let n = 0; n < 3 && !wrapper.text().includes('OLDEST ORIGINAL AUDIT REASON'); n += 1) {
        const next = wrapper.findAll('button').find(button => /next|more|older/i.test(button.text()) && !(button.element as HTMLButtonElement).disabled)
        if (!next) break
        await next.trigger('click'); await flushPromises()
      }
      expect(wrapper.text()).toContain('OLDEST ORIGINAL AUDIT REASON')
    } finally { wrapper.unmount() }
  })
  it('loads all asset history before resolving a retained historical year and renders its snapshots', async () => {
    const changed = { ...fullAsset, in_service_date: '2026-02-01', applicable_years: [2026, 2027, 2028, 2029, 2030] }
    const old = { ...rev, affected_periods: ['2025-Q4'], reason: 'OLDEST ASSET CORRECTION',
      before_snapshot: { ...fullAsset, annual_before: [prior], expense_before: { net_amount: '10000.00', vat_amount: '2100.00', vat_rate_label: 'Original frozen rate' } },
      after_snapshot: { ...changed, annual_after: [{ ...prior, confirmed: false, calculation_base: null, calculation_vat: null }] } }
    const rows = Array.from({ length: 101 }, (_, index) => ({ ...old, id: `revision-${index}`,
      reason: index === 100 ? old.reason : `Recent revision ${index}`,
      before_snapshot: index === 100 ? old.before_snapshot : { ...fullAsset, annual_before: [] } }))
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [changed], total: 1 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve(changed)
      if (url.startsWith(`/api/v1/battery-assets/${fullAsset.id}/revisions?`)) {
        const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? 0)
        return Promise.resolve({ items: rows.slice(offset, offset + 100), total: 101, offset, limit: 100 })
      }
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(prior)
      return Promise.resolve({ items: [], total: 0 })
    })
    const { wrapper } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      expect(api.get).toHaveBeenCalledWith(`/api/v1/battery-assets/${fullAsset.id}/revisions?limit=100&offset=100`)
      expect(wrapper.find('select').element.value).toBe('2025')
      const history = wrapper.find('.asset-history').text()
      for (const value of ['OLDEST ASSET CORRECTION', 'EMS ORIGINAL PRIOR VALUE', '900.00', '189.00', 'Original frozen rate', fullAsset.registered_by, 'Version 2', 'After revision']) expect(history).toContain(value)
    } finally { wrapper.unmount() }
  })

  it('reports a failed subsequent annual-history page and retries the full retained history', async () => {
    let fails = true
    const rows = Array.from({ length: 101 }, (_, index) => ({ ...rev, id: `revision-${index}`, reason: index === 100 ? 'Retried oldest entry' : `Revision ${index}` }))
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [fullAsset], total: 1 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve(fullAsset)
      if (url.includes('/annual-use/2025/revisions?')) {
        const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? 0)
        if (offset && fails) return Promise.reject(new Error('Second history page unavailable'))
        return Promise.resolve({ items: rows.slice(offset, offset + 100), total: 101, offset, limit: 100 })
      }
      if (url.endsWith('/annual-use/2025')) return Promise.resolve(current)
      return Promise.resolve({ items: [], total: 0 })
    })
    const { wrapper } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      expect(wrapper.text()).toContain('Second history page unavailable')
      expect(wrapper.text()).not.toContain('Retried oldest entry')
      fails = false
      await wrapper.findAll('button').find(button => button.text() === 'Retry history')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Retried oldest entry')
      expect(wrapper.text()).not.toContain('Second history page unavailable')
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('ignores an old second history page after leaving and returning to the same year (failure=%s)', async (failure) => {
    const late = deferred<unknown>()
    let first = true
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [fullAsset], total: 1 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve(fullAsset)
      if (url.includes('/annual-use/2025/revisions?')) {
        if (url.includes('offset=100')) return late.promise
        if (first) { first = false; return Promise.resolve({ items: Array.from({ length: 100 }, (_, i) => ({ ...rev, id: `old-${i}` })), total: 101 }) }
        return Promise.resolve({ items: [{ ...rev, reason: 'Fresh returned-year history' }], total: 1 })
      }
      if (url.endsWith('/annual-use/2025') || url.endsWith('/annual-use/2026')) return Promise.resolve(current)
      return Promise.resolve({ items: [], total: 0 })
    })
    const { wrapper, router } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      await router.push(`/battery-assets/${fullAsset.id}?year=2026`); await flushPromises()
      await router.push(`/battery-assets/${fullAsset.id}?year=2025`); await flushPromises()
      if (failure) late.reject(new Error('Obsolete history failure'))
      else late.resolve({ items: [{ ...rev, reason: 'Obsolete history result' }], total: 101 })
      await flushPromises()
      expect(wrapper.text()).toContain('Fresh returned-year history')
      expect(wrapper.text()).not.toContain('Obsolete history')
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('discards a delayed asset-history page after leaving and returning to the same asset (failure=%s)', async (failure) => {
    const late = deferred<unknown>()
    let first = true
    const second = { ...fullAsset, id: '00000000-0000-4000-8000-000000000099' }
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/battery-assets?')) return Promise.resolve({ items: [fullAsset, second], total: 2 })
      if (url === `/api/v1/battery-assets/${fullAsset.id}`) return Promise.resolve(fullAsset)
      if (url === `/api/v1/battery-assets/${second.id}`) return Promise.resolve(second)
      if (url.startsWith(`/api/v1/battery-assets/${fullAsset.id}/revisions?`)) {
        if (url.includes('offset=100')) return late.promise
        if (first) { first = false; return Promise.resolve({ items: Array.from({ length: 100 }, (_, i) => ({ ...rev, id: `old-${i}`, affected_periods: [] })), total: 101 }) }
        return Promise.resolve({ items: [{ ...rev, reason: 'Fresh returned-asset history', affected_periods: [] }], total: 1 })
      }
      if (/\/annual-use\/2025$/.test(url)) return Promise.resolve(current)
      return Promise.resolve({ items: [], total: 0 })
    })
    const { wrapper, router } = await render(`/battery-assets/${fullAsset.id}?year=2025`)
    try {
      await router.push(`/battery-assets/${second.id}?year=2025`); await flushPromises()
      await router.push(`/battery-assets/${fullAsset.id}?year=2025`); await flushPromises()
      if (failure) late.reject(new Error('Obsolete asset-history failure'))
      else late.resolve({ items: [{ ...rev, reason: 'Obsolete asset-history result', affected_periods: [] }], total: 101 })
      await flushPromises()
      expect(wrapper.text()).toContain('Fresh returned-asset history')
      expect(wrapper.text()).not.toContain('Obsolete asset-history')
      expect(wrapper.find('textarea').element.value).toBe(current.basis_note)
    } finally { wrapper.unmount() }
  })

})
