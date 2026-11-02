import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'

const store = vi.hoisted(() => ({
  vatLoading: false,
  vatError: null,
  fetchVatReturn: vi.fn(),
  vatReport: {
    year: 2026,
    quarter: 1,
    from: '2026-01-01',
    to: '2026-03-31',
    is_last_period_of_year: false,
    boxes: {
      box_1a: { base: '100.00', vat: '21.00' }, box_1b: { base: '0', vat: '0' },
      box_1c: { base: '0', vat: '0' }, box_1d: { vat: '0' }, box_1e: { base: '0' },
      box_2a: { base: '0', vat: '0' }, box_3a: { base: '0' }, box_3b: { base: '0' },
      box_3c: { base: '0' }, box_4a: { base: '0', vat: '0' }, box_4b: { base: '0', vat: '0' },
      box_5b: { vat: '0' },
    },
    totals: { output_vat_total: { vat: '21.00' }, net_payable_or_refundable: { vat: '21.00' } },
    warnings: ['A receipt-only quote deposit still needs review.'],
    infos: ['A settled receipt-only quote deposit has no duplicate VAT.'],
    disclaimer: 'Bookkeeping assistance only.',
  },
}))

const ui = vi.hoisted(() => ({
  alert: {
    props: ['type'],
    template: '<section :data-alert="type"><slot /></section>',
  },
}))

vi.mock('../../src/stores/reports', () => ({ useReportsStore: () => store }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('naive-ui', () => ({
  NAlert: ui.alert,
  NButton: { template: '<button><slot /></button>' },
  NCard: { template: '<section><slot /></section>' },
  NCollapse: {
    props: ['defaultExpandedNames'],
    template: '<div><slot /></div>',
  },
  NCollapseItem: {
    props: ['title', 'name'],
    template: '<section><h3>{{ title }}</h3><slot /></section>',
  },
  NDataTable: { template: '<table />' },
  NGrid: { template: '<div><slot /></div>' },
  NGridItem: { template: '<div><slot /></div>' },
  NSelect: { template: '<select />' },
  NSpin: { template: '<div />' },
  NStatistic: { template: '<div><slot /></div>' },
  NText: { template: '<span><slot /></span>' },
}))

import VatReturn from '../../src/views/reports/VatReturn.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

describe('VatReturn notices', () => {
  it('renders receipt-only warning and informational notices together', () => {
    const wrapper = mount(VatReturn, { global: { plugins: [i18n] } })

    expect(wrapper.findAll('[data-alert="warning"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-alert="info"]')).toHaveLength(2)
    expect(wrapper.text()).toContain('Warnings')
    expect(wrapper.text()).toContain('Information')
    expect(wrapper.text()).toContain('still needs review')
    expect(wrapper.text()).toContain('no duplicate VAT')
  })
})
