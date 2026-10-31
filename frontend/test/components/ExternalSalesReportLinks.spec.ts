import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const store = vi.hoisted(() => ({
  plLoading: false, plError: null, fetchProfitLoss: vi.fn(),
  plReport: {
    revenue_net: '100.00', expense_actual: '0', mileage_expense_actual: '0',
    other_expense_actual: '0', profit_before_mileage: '100.00',
    profit_after_mileage: '100.00', profit: '100.00', series: [],
    external_sales: [{ external_sale_id: 'external-42', source_name: 'Frozen platform', original_number: 'EXT-42', event_date: '2026-03-31' }],
  },
  vatLoading: false, vatError: null, fetchVatReturn: vi.fn(),
  vatReport: {
    year: 2026, quarter: 1, from: '2026-01-01', to: '2026-03-31',
    is_last_period_of_year: false, warnings: [], infos: [], disclaimer: 'Review original.',
    boxes: {
      box_1a: { base: '100', vat: '21' }, box_1b: { base: '0', vat: '0' },
      box_1c: { base: '0', vat: '0' }, box_1d: { vat: '0' }, box_1e: { base: '0' },
      box_2a: { base: '0', vat: '0' }, box_3a: { base: '0' }, box_3b: { base: '0' },
      box_3c: { base: '0' }, box_4a: { base: '0', vat: '0' }, box_4b: { base: '0', vat: '0' }, box_5b: { vat: '0' },
    },
    totals: { output_vat_total: { vat: '21' }, net_payable_or_refundable: { vat: '21' } },
    event_rows: [{ source_type: 'EXTERNAL_SALE', external_sale_id: 'external-42', external_source_name: 'Frozen platform', external_original_number: 'EXT-42', event_date: '2026-03-31' }],
  },
}))
vi.mock('../../src/stores/reports', () => ({ useReportsStore: () => store }))
vi.mock('naive-ui', () => {
  const slot = { template: '<div><slot /></div>' }
  const statistic = {
    props: ['label', 'value'],
    template: '<div class="statistic">{{ label }}: {{ value }}</div>',
  }
  return {
    NAlert: slot, NButton: slot, NCard: slot, NDataTable: slot, NDatePicker: slot,
    NGrid: slot, NGridItem: slot, NRadioButton: slot, NRadioGroup: slot, NSelect: slot,
    NSpace: slot, NSpin: slot, NStatistic: statistic, NText: slot,
  }
})
import ProfitLoss from '../../src/views/reports/ProfitLoss.vue'
import VatReturn from '../../src/views/reports/VatReturn.vue'

describe('External sales report traceability', () => {
  it.each(['en', 'zh'])('shows backend mileage and profit values with explicit labels (%s)', async (locale) => {
    const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
    const wrapper = mount(ProfitLoss, {
      global: { plugins: [i18n] },
    })

    const statistics = wrapper.findAll('.statistic').map((item) => item.text())
    expect(statistics).toContain(`${locale === 'en' ? 'Mileage Expenses' : '里程费用'}: 0.00`)
    expect(statistics).toContain(`${locale === 'en' ? 'Other Expenses' : '其他费用'}: 0.00`)
    expect(statistics).toContain(
      `${locale === 'en' ? 'Profit Before Mileage Expenses' : '扣除里程费用前利润'}: 100.00`,
    )
    expect(statistics).toContain(
      `${locale === 'en' ? 'Profit After Mileage Expenses' : '扣除里程费用后利润'}: 100.00`,
    )
    wrapper.unmount()
  })

  it.each(['en', 'zh'])('links P/L and VAT originals to the same saved detail route (%s)', (locale) => {
    for (const component of [ProfitLoss, VatReturn]) {
      const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
      const wrapper = mount(component, {
        global: {
          plugins: [i18n],
          stubs: { 'router-link': { props: ['to'], template: '<a :href="to"><slot /></a>' } },
        },
      })
      const link = wrapper.get('a[href="/external-sales/external-42"]')
      expect(link.text()).toContain('Frozen platform')
      expect(link.text()).toContain('EXT-42')
      expect(link.text()).toContain('2026-03-31')
      expect(wrapper.find('a[href^="/invoices/"]').exists()).toBe(false)
      wrapper.unmount()
    }
  })
})
