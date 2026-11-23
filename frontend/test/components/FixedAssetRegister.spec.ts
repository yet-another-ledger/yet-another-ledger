import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const mocks = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../src/api/http', () => ({ get: mocks.get, ApiError: class extends Error {} }))
vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const box = defineComponent({ template: '<div><slot /></div>' })
  const input = defineComponent({
    props: ['value'], emits: ['update:value'],
    setup(props, { emit }) { return () => h('input', { value: props.value, onInput: (event: Event) => emit('update:value', Number((event.target as HTMLInputElement).value)) }) },
  })
  const table = defineComponent({
    props: ['data', 'columns'],
    setup(props) {
      return () => h('div', (props.data as any[]).map(row => h('div', [
        h('span', `${row.name}:${row.yearly_deltas[0]?.fixed_asset_depreciation ?? '0'}`),
        h('span', (props.columns as any[]).find(column => column.key === 'book')?.render(row)),
        (props.columns as any[]).find(column => column.key === 'details')?.render(row),
      ])))
    },
  })
  return {
    NAlert: box, NButton: defineComponent({ emits: ['click'], setup(_, { slots, emit }) { return () => h('button', { onClick: () => emit('click') }, slots.default?.()) } }),
    NCard: box, NDataTable: table, NText: box, NSpace: box, NInputNumber: input,
    NInput: input, NList: box, NListItem: box,
  }
})

import FixedAssetRegister from '../../src/views/FixedAssetRegister.vue'

function asset(year: number, amount: string) {
  return [{ id: 'asset-1', acquisition_expense_id: 'expense-1', name: `Asset ${year}`, acquisition_date: '2026-01-01', in_service_date: '2026-01-01', acquisition_cost: '1000.00', residual_value: '0.00', useful_life_years: 5, business_percentage: '100', pending_in_service: false, yearly_deltas: [{ year, fixed_asset_depreciation: amount, planned_fixed_asset_depreciation: amount, cumulative_depreciation: amount, opening_book_value: '1000.00', closing_book_value: '900.00', planned_closing_book_value: '900.00' }] }]
}

function futureYearAsset() {
  return [{
    ...asset(2027, '0.00')[0],
    yearly_deltas: [{
      year: 2027,
      fixed_asset_depreciation: '0.00',
      planned_fixed_asset_depreciation: '1800.00',
      cumulative_depreciation: '77.42',
      opening_book_value: '9922.58',
      closing_book_value: '9922.58',
      planned_opening_book_value: '8272.58',
      planned_closing_book_value: '6472.58',
    }],
  }]
}

function setup(path = '/fixed-assets') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/fixed-assets', component: FixedAssetRegister }] })
  router.push(path)
  return router.isReady().then(() => mount(FixedAssetRegister, {
    global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })] },
  }))
}

afterEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
})

beforeEach(() => {
  mocks.get.mockImplementation((url: string) => {
    if (url.endsWith('/revisions')) return Promise.resolve([])
    if (url.includes('/annual-use/')) return Promise.resolve(null)
    return Promise.resolve([])
  })
})

describe('fixed asset register year switching', () => {
  it('shows the selected year effective percentage and its localized confirmation state', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00'))
    const mixedAsset = (selectedYear: number, amount: string) => {
      const result: any[] = asset(selectedYear, amount)
      result[0].use_type = 'MIXED_USE'
      return result
    }
    mocks.get.mockImplementation((url: string) => {
      if (url === '/api/v1/fixed-assets?year=2026') return Promise.resolve(mixedAsset(2026, '120.00'))
      if (url === '/api/v1/fixed-assets?year=2027') return Promise.resolve(mixedAsset(2027, '130.00'))
      if (url.endsWith('/revisions')) return Promise.resolve([])
      if (url.includes('/annual-use/2026')) return Promise.resolve({
        asset_id: 'asset-1', year: 2026, estimated_business_percentage: '50',
        actual_business_percentage: '80', effective_business_percentage: '80',
        provisional: false, version: 2, confirmed_at: '2026-11-01T00:00:00Z',
        full_depreciation: '150.00', private_use_reduction: '30.00', net_pnl_effect: '120.00',
      })
      if (url.includes('/annual-use/2027')) return Promise.resolve({
        asset_id: 'asset-1', year: 2027, estimated_business_percentage: '75',
        actual_business_percentage: null, effective_business_percentage: '75',
        provisional: true, version: 1, confirmed_at: null,
        full_depreciation: '160.00', private_use_reduction: '40.00', net_pnl_effect: '120.00',
      })
      return Promise.resolve([])
    })
    const wrapper = await setup('/fixed-assets?asset=asset-1&year=2026')
    await flushPromises()
    expect(wrapper.text()).toContain('Use status: 80% · confirmed')
    expect(wrapper.text()).not.toContain('Use status: 50%')
    const yearInput = wrapper.find('input')
    await yearInput.setValue('2027')
    await flushPromises()
    expect(wrapper.text()).toContain('Use status: 75% · provisional')
    expect(wrapper.text()).not.toContain('Use status: 80% · confirmed')
    wrapper.unmount()
  })

  it('shows distinct realized and planned opening-to-closing values in a future year table and detail', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00'))
    mocks.get.mockResolvedValue(futureYearAsset())
    const wrapper = await setup('/fixed-assets?asset=asset-1&year=2027')
    await flushPromises()

    expect(wrapper.text()).toContain('realized opening → closing book value: 9922.58 → 9922.58')
    expect(wrapper.text()).toContain('planned opening → closing book value: 8272.58 → 6472.58')
    expect(wrapper.text()).toContain('Full-year plan: 1800.00')

    await wrapper.find('button').trigger('click')
    expect(wrapper.text().match(/planned opening → closing book value: 8272\.58 → 6472\.58/g)?.length).toBe(2)
    expect(wrapper.text()).toContain('realized opening → closing book value: 9922.58 → 9922.58')
    wrapper.unmount()
  })

  it('opens a future year deep link and selects the target asset', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00'))
    mocks.get.mockResolvedValue(asset(2027, '22.22'))
    const wrapper = await setup('/fixed-assets?asset=asset-1&year=2027')
    await flushPromises()
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/fixed-assets?year=2027')
    expect(wrapper.text()).toContain('Asset 2027')
    expect(wrapper.text()).toContain('22.22')
    wrapper.unmount()
  })

  it('renders persisted shared monthly impacts and tolerates older snapshots without them', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-15T12:00:00'))
    const row = { ...asset(2026, '150.00')[0], use_type: 'MIXED_USE' }
    mocks.get.mockImplementation((url: string) => {
      if (url === '/api/v1/fixed-assets?year=2026') return Promise.resolve([row])
      if (url === '/api/v1/fixed-assets/asset-1/revisions') return Promise.resolve([
        { version: 3, reason: 'Shared purchase correction', actor_id: null,
          created_at: '2026-11-01T10:00:00Z', affected_periods: [], before_snapshot: {},
          after_snapshot: { monthly_period_deltas: [
            { period: '2026-01', before_depreciation: '16.67', after_depreciation: '0.00', pnl_delta: '-16.67' },
            { period: '2026-02', before_depreciation: '16.66', after_depreciation: '25.00', pnl_delta: '8.34' },
          ] } },
        { version: 2, reason: 'Legacy snapshot', actor_id: null,
          created_at: '2026-10-01T10:00:00Z', affected_periods: [], before_snapshot: {}, after_snapshot: {} },
      ])
      if (url.includes('/annual-use/2026/revisions')) return Promise.resolve([])
      if (url.includes('/annual-use/2026')) return Promise.resolve(null)
      return Promise.resolve([])
    })
    const wrapper = await setup('/fixed-assets?asset=asset-1&year=2026')
    await flushPromises()
    try {
      const text = wrapper.text()
      expect(text).toContain('2026-01 · 16.67 → 0.00 · Δ -16.67')
      expect(text).toContain('2026-02 · 16.66 → 25.00 · Δ 8.34')
      expect(text).toContain('Legacy snapshot')
    } finally { wrapper.unmount() }
  })

  it('renders annual percentage and quarterly transitions with and without monthly audit data', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-15T12:00:00'))
    const row = { ...asset(2026, '150.00')[0], use_type: 'MIXED_USE' }
    mocks.get.mockImplementation((url: string) => {
      if (url === '/api/v1/fixed-assets?year=2026') return Promise.resolve([row])
      if (url.endsWith('/annual-use/2026/revisions')) return Promise.resolve([
        { version: 1, reason: 'Annual confirmation', actor_id: 'owner-1',
          created_at: '2026-10-01T10:00:00Z', affected_periods: ['2026-Q1'],
          before_snapshot: { effective_business_percentage: '41.234' },
          after_snapshot: { effective_business_percentage: '63.456', period_deltas: [
            { period: '2026-Q1', before_business_depreciation: '20.62', after_business_depreciation: '31.73', pnl_delta: '11.11' },
          ] } },
        { version: 2, reason: 'Annual correction', actor_id: 'owner-1',
          created_at: '2026-11-01T10:00:00Z', affected_periods: ['2026-Q1'],
          before_snapshot: { actual_business_percentage: '63.456' },
          after_snapshot: { actual_business_percentage: '74.321', period_deltas: [
            { period: '2026-Q1', before_business_depreciation: '31.73', after_business_depreciation: '37.16', pnl_delta: '5.43' },
          ], monthly_period_deltas: [
            { period: '2026-01', before_business_depreciation: '10.58', after_business_depreciation: '12.39', pnl_delta: '1.81' },
          ] } },
      ])
      if (url.includes('/annual-use/2026')) return Promise.resolve(null)
      return Promise.resolve([])
    })
    const wrapper = await setup('/fixed-assets?asset=asset-1&year=2026')
    await flushPromises()
    try {
      const text = wrapper.text()
      expect(text).toContain('41.234% → 63.456%')
      expect(text).toContain('2026-Q1 · 20.62 → 31.73 · Δ 11.11')
      expect(text).toContain('63.456% → 74.321%')
      expect(text).toContain('2026-Q1 · 31.73 → 37.16 · Δ 5.43')
      expect(text).toContain('2026-01 · 10.58 → 12.39 · Δ 1.81')
      expect(text).toContain('Annual confirmation')
      expect(text).toContain('Annual correction')
    } finally { wrapper.unmount() }
  })

  it('refreshes an open detail from the selected year response', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00'))
    const targetYear = new Date().getFullYear() + 1
    mocks.get.mockImplementation((url: string) => {
      if (url === `/api/v1/fixed-assets?year=${new Date().getFullYear()}`) {
        return Promise.resolve(asset(new Date().getFullYear(), '11.11'))
      }
      if (url === `/api/v1/fixed-assets?year=${targetYear}`) {
        return Promise.resolve(asset(targetYear, '22.22'))
      }
      if (url.endsWith('/revisions')) return Promise.resolve([])
      if (url.includes('/annual-use/')) return Promise.resolve(null)
      return Promise.resolve([])
    })
    const wrapper = await setup()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await wrapper.find('input').setValue(String(targetYear))
    await flushPromises()
    expect(wrapper.text()).toContain(`Asset ${targetYear}`)
    expect(wrapper.text()).toContain('22.22')
    expect(wrapper.text().match(/22\.22/g)?.length).toBeGreaterThan(1)
    expect(wrapper.text()).not.toContain('11.11')
    wrapper.unmount()
  })

  it('ignores an older request that finishes after the current year request', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2027-02-03T12:00:00'))
    const initialYear = new Date().getFullYear()
    const targetYear = initialYear + 1
    let finishOld!: (value: unknown) => void
    let finishNew!: (value: unknown) => void
    mocks.get.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { finishNew = resolve }))
    const wrapper = await setup()
    await wrapper.find('input').setValue(String(targetYear))
    finishNew(asset(targetYear, '22.22'))
    await flushPromises()
    finishOld(asset(initialYear, '11.11'))
    await flushPromises()
    expect(wrapper.text()).toContain(`Asset ${targetYear}:22.22`)
    expect(wrapper.text()).not.toContain(`Asset ${initialYear}:11.11`)
    wrapper.unmount()
  })

  it.each(['', '10000'])('rejects invalid year %s without issuing another request', async year => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2027-02-03T12:00:00'))
    mocks.get.mockResolvedValue(asset(new Date().getFullYear(), '11.11'))
    const wrapper = await setup()
    await flushPromises()
    await wrapper.find('input').setValue(year)
    await flushPromises()
    expect(wrapper.text()).toContain('Enter a calendar year between 1 and 9999.')
    expect(mocks.get).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
