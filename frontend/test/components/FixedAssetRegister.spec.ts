import { afterEach, describe, expect, it, vi } from 'vitest'
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

describe('fixed asset register year switching', () => {
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

  it('refreshes an open detail from the selected year response', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00'))
    const targetYear = new Date().getFullYear() + 1
    mocks.get.mockResolvedValueOnce(asset(new Date().getFullYear(), '11.11')).mockResolvedValueOnce(asset(targetYear, '22.22'))
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
