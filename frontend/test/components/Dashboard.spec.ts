import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const store = vi.hoisted(() => ({
  dashboardLoading: false, dashboardError: null, fetchDashboard: vi.fn(),
  dashboardReport: {
    year: 2025, quarter: 4,
    kpi: { ytd_revenue: '0', ytd_expense: '0', ytd_profit: '0', current_quarter_vat_payable: '189.00' },
    current_quarter_vat_status: {
      is_complete: false,
      missing_inputs: [] as { asset_id: string; year: number; code: string }[],
    },
    monthly: [], top_expense_categories: [],
  },
}))
vi.mock('../../src/stores/reports', () => ({ useReportsStore: () => store }))
vi.mock('vue-echarts', () => ({ default: { template: '<div />' } }))
vi.mock('naive-ui', () => {
  const slot = { template: '<div><slot /></div>' }
  return {
    NAlert: { props: ['type'], template: '<section :data-alert="type"><slot /></section>' },
    NButton: slot, NCard: slot, NDataTable: slot, NGi: slot, NGrid: slot,
    NInputNumber: slot, NSpace: slot, NSpin: slot, NText: slot,
    NStatistic: { props: ['label', 'value'], template: '<div class="statistic">{{ label }}: {{ value }}<slot name="suffix" /></div>' },
  }
})
import Dashboard from '../../src/views/Dashboard.vue'

async function render(locale: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/dashboard', component: Dashboard },
    { path: '/battery-assets/:id', component: { template: '<div>Annual input</div>' } },
  ] })
  await router.push('/dashboard')
  const wrapper = mount(Dashboard, { global: {
    plugins: [router, createI18n({ legacy: false, locale, messages: { en, zh } })],
  } })
  await flushPromises()
  return { wrapper, router }
}

describe('Dashboard VAT completeness', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.dashboardReport.current_quarter_vat_status = { is_complete: false, missing_inputs: [] }
  })
  for (const locale of ['en', 'zh']) {
    it.each(['MISSING', 'PERCENTAGE_MISSING', 'UNCONFIRMED', 'RECONFIRMATION_REQUIRED'])(
      `labels the known subtotal and links the annual input (${locale}, %s)`, async (code) => {
        store.dashboardReport.current_quarter_vat_status.missing_inputs = [{ asset_id: 'asset-42', year: 2025, code }]
        const { wrapper, router } = await render(locale)
        const messages = locale === 'en' ? en : zh
        expect(wrapper.get('[data-alert="warning"]').text()).toContain(messages.dashboard.vatIncomplete)
        expect(wrapper.text()).toContain(messages.dashboard.currentQuarterVatSubtotal)
        expect(wrapper.text()).toContain('189.00')
        const link = wrapper.get('a[href="/battery-assets/asset-42?year=2025"]')
        expect(link.text()).toContain(messages.reports.vat.missingCodes[code as keyof typeof messages.reports.vat.missingCodes])
        await link.trigger('click')
        await flushPromises()
        expect(router.currentRoute.value.path).toBe('/battery-assets/asset-42')
        expect(router.currentRoute.value.query.year).toBe('2025')
        wrapper.unmount()
      },
    )
    it(`preserves the ordinary complete VAT label (${locale})`, async () => {
      store.dashboardReport.current_quarter_vat_status.is_complete = true
      const { wrapper } = await render(locale)
      const messages = locale === 'en' ? en : zh
      expect(wrapper.text()).toContain(messages.dashboard.currentQuarterVat)
      expect(wrapper.text()).not.toContain(messages.dashboard.currentQuarterVatSubtotal)
      expect(wrapper.text()).toContain('189.00')
      expect(wrapper.find('[data-alert="warning"]').exists()).toBe(false)
      wrapper.unmount()
    })
    it(`retains incomplete status with no annual input links (${locale})`, async () => {
      const { wrapper } = await render(locale)
      expect(wrapper.find('[data-alert="warning"]').exists()).toBe(true)
      expect(wrapper.find('a').exists()).toBe(false)
      wrapper.unmount()
    })
  }
})
