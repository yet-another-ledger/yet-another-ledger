import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const routerPush = vi.hoisted(() => vi.fn())

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
    event_rows: [
      { source_type: 'EXTERNAL_SALE', external_sale_id: 'external-42', external_source_name: 'Frozen platform', external_original_number: 'EXT-42', event_date: '2026-03-31', event_kind: 'DOCUMENT_TAX', taxable_amount: '100', vat_amount: '21' },
      { source_type: 'EXTERNAL_SALE', external_sale_id: 'external-42', external_source_name: 'Frozen platform', external_original_number: 'EXT-42', event_date: '2026-03-31', event_kind: 'PAYMENT_TAX', taxable_amount: '50', vat_amount: '10.5' },
      { source_type: 'EXTERNAL_SALE', external_sale_id: 'external-43', external_source_name: 'Frozen platform', external_original_number: 'EXT-42', event_date: '2026-03-31', event_kind: 'DOCUMENT_TAX', taxable_amount: '50', vat_amount: '10.5' },
      { source_type: 'INVOICE', external_sale_id: 'invoice-like', external_source_name: 'Ignored', external_original_number: 'IGNORED', event_date: '2026-03-31', event_kind: 'DOCUMENT_TAX', taxable_amount: '1', vat_amount: '0.21' },
      { source_type: 'EXTERNAL_SALE', external_sale_id: null, external_source_name: 'Missing id', external_original_number: 'NO-ID', event_date: '2026-03-31', event_kind: 'DOCUMENT_TAX', taxable_amount: '1', vat_amount: '0.21' },
      { source_type: 'EXTERNAL_SALE', external_sale_id: '   ', external_source_name: 'Blank id', external_original_number: 'BLANK', event_date: '2026-03-31', event_kind: 'DOCUMENT_TAX', taxable_amount: '1', vat_amount: '0.21' },
    ],
  },
}))
vi.mock('../../src/stores/reports', () => ({ useReportsStore: () => store }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))
vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  const slot = { template: '<div><slot /></div>' }
  const statistic = {
    props: ['label', 'value'],
    template: '<div class="statistic">{{ label }}: {{ value }}</div>',
  }
  return {
    NAlert: slot, NButton: actual.NButton, NCard: slot, NCollapse: actual.NCollapse, NCollapseItem: actual.NCollapseItem,
    NDataTable: slot, NDatePicker: slot, NGrid: slot, NGridItem: slot, NList: actual.NList, NListItem: actual.NListItem, NRadioButton: slot, NRadioGroup: slot, NSelect: slot,
    NSpace: slot, NSpin: slot, NStatistic: statistic, NText: slot,
  }
})
import ProfitLoss from '../../src/views/reports/ProfitLoss.vue'
import VatReturn from '../../src/views/reports/VatReturn.vue'

describe('External sales report traceability', () => {
  it.each(['en', 'zh'])('starts both report source lists collapsed and provides one VAT action per sale id (%s)', async (locale) => {
    const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
    const pl = mount(ProfitLoss, { global: { plugins: [i18n] } })
    const vat = mount(VatReturn, { global: { plugins: [i18n] } })

    expect(pl.get('.report-source-panel__toggle').attributes('aria-expanded')).toBe('false')
    expect(vat.get('.report-source-panel__toggle').attributes('aria-expanded')).toBe('false')
    expect(pl.find('.report-source-panel__action').exists()).toBe(false)
    expect(vat.find('.report-source-panel__action').exists()).toBe(false)
    await vat.get('.report-source-panel__toggle').trigger('click')
    expect(vat.get('.report-source-panel__action').isVisible()).toBe(true)
    expect(pl.find('.report-source-panel__action').exists()).toBe(false)
    const vatButtons = vat.findAll('.report-source-panel__action')
    expect(vatButtons).toHaveLength(2)
    expect(vat.text()).toContain('Frozen platform · EXT-42 · 2026-03-31')
    expect(vat.findAll('a[href^="/invoices/"]')).toHaveLength(0)
    expect(store.vatReport.event_rows).toHaveLength(6)
    expect(store.vatReport.totals.output_vat_total.vat).toBe('21')

    pl.unmount()
    vat.unmount()
  })

  it('shows an empty state for a VAT period without external sale IDs', async () => {
    const original = store.vatReport
    store.vatReport = { ...original, event_rows: [original.event_rows[3], original.event_rows[4], original.event_rows[5]] }
    const i18n = createI18n({ legacy: false, locale: 'en', messages: { en, zh } })
    const wrapper = mount(VatReturn, { global: { plugins: [i18n] } })
    expect(wrapper.findAll('.report-source-panel__action')).toHaveLength(0)
    await wrapper.get('.report-source-panel__toggle').trigger('click')
    expect(wrapper.text()).toContain('No external sales in this period.')
    wrapper.unmount()
    store.vatReport = original
  })

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

  it.each(['en', 'zh'])('offers an explicit action to open each P/L and VAT source (%s)', async (locale) => {
    routerPush.mockClear()
    for (const component of [ProfitLoss, VatReturn]) {
      const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
      const wrapper = mount(component, {
        global: {
          plugins: [i18n],
          stubs: { 'router-link': { props: ['to'], template: '<a :href="to"><slot /></a>' } },
        },
      })
      await wrapper.get('.report-source-panel__toggle').trigger('click')
      const action = wrapper.get('.report-source-panel__action')
      expect(wrapper.get('.report-source-panel__label').text()).toContain('Frozen platform')
      expect(wrapper.get('.report-source-panel__label').text()).toContain('EXT-42')
      expect(wrapper.get('.report-source-panel__label').text()).toContain('2026-03-31')
      expect(wrapper.find('a[href^="/invoices/"]').exists()).toBe(false)
      expect(action.element.tagName).toBe('BUTTON')
      expect(action.attributes('aria-label')).toContain('Frozen platform')
      expect(action.text()).toBe(locale === 'en' ? 'Open record' : '打开记录')
      await action.trigger('click')
      expect(routerPush).toHaveBeenLastCalledWith('/external-sales/external-42')
      wrapper.unmount()
    }
  })
})
