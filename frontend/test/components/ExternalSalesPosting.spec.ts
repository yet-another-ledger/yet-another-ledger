import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'
import { ApiError } from '../../src/api/http'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  push: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  route: { name: 'external-sales-detail', params: { id: 'sale-1' } },
}))

vi.mock('../../src/api/http', () => ({
  get: mocks.get,
  post: mocks.post,
  put: mocks.put,
  del: mocks.del,
  ApiError: class ApiError extends Error {
    status: number
    detail: unknown
    constructor(status: number, detail: unknown, message: string) {
      super(message)
      this.status = status
      this.detail = detail
    }
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}))

vi.mock('naive-ui', () => ({
  NAlert: { template: '<div><slot /></div>' },
  NButton: {
    props: ['disabled', 'type', 'size', 'attrType'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\', $event)"><slot /></button>',
  },
  NCard: { template: '<section><slot /></section>' },
  NCheckbox: {
    props: ['checked'],
    emits: ['update:checked'],
    template: '<label><input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)"><slot /></label>',
  },
  NForm: { props: ['disabled'], template: '<form><slot /></form>' },
  NFormItem: { template: '<div><slot /></div>' },
  NInput: {
    props: ['value', 'type', 'disabled'],
    emits: ['update:value'],
    template: '<textarea v-if="type === \'textarea\'" :disabled="disabled" :value="value ?? \'\'" @input="$emit(\'update:value\', $event.target.value)"></textarea><input v-else :disabled="disabled" :value="value ?? \'\'" @input="$emit(\'update:value\', $event.target.value)">',
  },
  NModal: { template: '<div><slot /></div>' },
  NSelect: {
    props: ['value', 'disabled', 'options'],
    emits: ['update:value'],
    template: '<select :disabled="disabled" :value="value ?? \'\'" @change="$emit(\'update:value\', $event.target.value)"><option value=""></option></select>',
  },
  useMessage: () => ({ success: mocks.success, error: mocks.error }),
}))

import ExternalSales from '../../src/views/externalSales/ExternalSales.vue'



function sale(status: 'PENDING' | 'POSTED' = 'PENDING') {
  return {
    id: 'sale-1',
    source_id: 'source-1',
    source_name: 'Platform',
    original_number: 'EXT-42',
    document_date: '2026-10-05',
    covered_from: null,
    covered_to: null,
    counterparty: null,
    description: null,
    include_in_pl: true,
    status,
    tax_summaries: [{
      id: 'tax-1', treatment_id: 'treatment-1', treatment_code: 'NL_DOMESTIC',
      treatment_label: 'NL domestic', rate_id: 'rate-1', rate_label: 'Custom',
      rate_percent: '10.000', net_amount: '10.00', vat_amount: '1.25',
    }],
    files: [{ id: 'file-1', filename: 'original.pdf', mime_type: 'application/pdf', byte_size: 8, sha256: 'abc', duplicate_warning: false, created_at: '2026-10-05T10:00:00Z' }],
    created_at: '2026-10-05T10:00:00Z', updated_at: '2026-10-05T10:00:00Z', version: 3,
    posted_at: status === 'POSTED' ? '2026-10-05T11:00:00Z' : null,
    posted_by: status === 'POSTED' ? 'user-1' : null,
    posted_source_name: status === 'POSTED' ? 'Platform' : null,
    original_net_total: status === 'POSTED' ? '10.00' : null,
    original_vat_total: status === 'POSTED' ? '1.25' : null,
    mismatch_ack_reason: status === 'POSTED' ? 'Original document reviewed.' : null,
    postings: status === 'POSTED' ? [{ id: 'posting-1', version: 4, actor_id: 'user-1', created_at: '2026-10-05T11:00:00Z', snapshot: {} }] : [],
  }
}

function calculation() {
  return {
    sale_id: 'sale-1', expected_version: 3, original_net_total: '10.00',
    original_vat_total: '1.25', derived_vat_total: '1.00', mismatch: true,
    mismatch_row_indices: [0], warnings: ['Original VAT 1.25 differs from rate-derived VAT 1.00.'],
  }
}

async function render(status: 'PENDING' | 'POSTED' = 'PENDING', current = sale(status), locale = 'en') {
  const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
  mocks.get.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/v1/external-sales-sources')) return { items: [], total: 0, limit: 100, offset: 0 }
    if (url.startsWith('/api/v1/external-sales?')) return { items: [current], total: 1, limit: 50, offset: 0 }
    if (url.startsWith('/api/v1/vat-treatments')) return { items: [] }
    if (url.startsWith('/api/v1/vat-rates')) return { items: [] }
    if (url === '/api/v1/external-sales/sale-1') return current
    throw new Error(`Unexpected GET ${url}`)
  })
  const wrapper = mount(ExternalSales, {
    global: { plugins: [i18n], stubs: { 'router-link': { template: '<a><slot /></a>' } } },
  })
  await flushPromises()
  return wrapper
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find((item) => item.text().includes(label))!
}

describe('External Sales posting review', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.route.name = 'external-sales-detail'
    mocks.route.params.id = 'sale-1'
  })

  it('shows the server preview and requires acknowledgement plus a reason before confirming', async () => {
    mocks.post.mockImplementation(async (url: string) => {
      if (url.endsWith('/calculate')) return calculation()
      if (url.endsWith('/confirm')) return sale('POSTED')
      throw new Error(`Unexpected POST ${url}`)
    })
    const wrapper = await render()
    await button(wrapper, 'Review posting').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Original net / VAT: 10.00 / 1.25')
    expect(wrapper.text()).toContain('Rate-derived VAT: 1.00')
    expect(wrapper.text()).toContain('Original VAT 1.25 differs')
    const confirm = button(wrapper, 'Confirm and post')
    expect((confirm.element as HTMLButtonElement).disabled).toBe(true)

    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect((confirm.element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.findAll('textarea').at(-1)!.setValue('Original document reviewed.')
    expect((confirm.element as HTMLButtonElement).disabled).toBe(false)
    await confirm.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/confirm', {
      expected_version: 3,
      acknowledge_vat_mismatch: true,
      mismatch_ack_reason: 'Original document reviewed.',
    })
    expect(wrapper.text()).toContain('Posted')
    expect(wrapper.text()).toContain('Original document reviewed.')
  })

  it('invalidates a shown preview when an input changes and discards a late preview response', async () => {
    let resolvePreview!: (value: ReturnType<typeof calculation>) => void
    mocks.post.mockImplementation((url: string) => url.endsWith('/calculate')
      ? new Promise((resolve) => { resolvePreview = resolve })
      : Promise.resolve(sale('POSTED')))
    const wrapper = await render()
    const pending = button(wrapper, 'Review posting').trigger('click')
    await wrapper.findAll('input').at(0)!.setValue('EXT-43')
    resolvePreview(calculation())
    await pending
    await flushPromises()
    expect(wrapper.text()).not.toContain('Rate-derived VAT')
    expect(wrapper.findAll('button').some((item) => item.text().includes('Confirm and post'))).toBe(false)

    mocks.post.mockResolvedValueOnce(calculation())
    await wrapper.findAll('input').at(0)!.setValue('EXT-42')
    await flushPromises()
    await button(wrapper, 'Review posting').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Rate-derived VAT: 1.00')
    await wrapper.findAll('input').at(0)!.setValue('EXT-44')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Rate-derived VAT: 1.00')
    expect(wrapper.findAll('button').some((item) => item.text().includes('Confirm and post'))).toBe(false)
  })

  it('does not preview the persisted server fields while the editor contains unsaved changes', async () => {
    mocks.post.mockResolvedValue(calculation())
    const wrapper = await render()
    await wrapper.findAll('input').at(0)!.setValue('EXT-UNSAVED')

    const review = button(wrapper, 'Review posting')
    expect((review.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('Save your changes before reviewing the posting.')
    await review.trigger('click')
    expect(mocks.post).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Rate-derived VAT')
  })

  it('translates stable posting conflict codes and keeps posted fields and originals read-only', async () => {
    mocks.post.mockImplementation(async (url: string) => {
      if (url.endsWith('/calculate')) return calculation()
      throw new ApiError(
        409,
        { detail: { code: 'EXTERNAL_SALE_VERSION_CONFLICT', external_sale_id: 'sale-1', current_version: 4 } },
        '[object Object]',
      )
    })
    const pending = await render()
    await button(pending, 'Review posting').trigger('click')
    await flushPromises()
    await pending.find('input[type="checkbox"]').setValue(true)
    await pending.findAll('textarea').at(-1)!.setValue('Reason')
    await button(pending, 'Confirm and post').trigger('click')
    await flushPromises()
    expect(mocks.error).toHaveBeenCalledWith('This entry changed. Reload it and review the current details before posting.')

    const posted = await render('POSTED')
    expect(posted.text()).toContain('original.pdf')
    expect(posted.findAll('button').some((item) => item.text().includes('Remove file'))).toBe(false)
    expect(posted.find('input[type="file"]').exists()).toBe(false)
    expect(posted.find('input[type="text"]').exists()).toBe(false)
    expect(posted.text()).toContain('Original document reviewed.')
  })
})


describe('Posted external record traceability', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.route.name = 'external-sales-detail'
    mocks.route.params.id = 'sale-1'
  })

  it.each(['en', 'zh'])('shows frozen multi-rate headers and audit from a report detail link (%s)', async (locale) => {
    const current = sale('POSTED')
    Object.assign(current, {
      covered_from: '2026-09-01', covered_to: '2026-09-30',
      counterparty: 'Original customer', description: '<script>unsafe()</script>\nMonthly work',
      source_name: 'Renamed source', posted_source_name: 'Frozen platform', version: 4,
      original_net_total: '60.00', original_vat_total: '5.75',
    })
    current.tax_summaries.push({
      id: 'tax-2', treatment_id: 'treatment-2', treatment_code: 'NL_DOMESTIC',
      treatment_label: 'Frozen domestic label', rate_id: 'deleted-rate', rate_label: 'Frozen low',
      rate_percent: '9.000', net_amount: '50.00', vat_amount: '4.50',
    })
    const wrapper = await render('POSTED', current, locale)
    const detail = wrapper.get('[data-testid="posted-detail"]')
    for (const value of ['Frozen platform', 'EXT-42', '2026-10-05', 'Original customer', '2026-09-01', '2026-09-30', 'Monthly work']) {
      expect(detail.text()).toContain(value)
    }
    expect(detail.text()).not.toContain('Renamed source')
    expect(detail.text()).toContain('<script>unsafe()</script>')
    expect(detail.find('script').exists()).toBe(false)
    expect(detail.get('[data-testid="posted-include-pl"]').text()).toBe(locale === 'en' ? 'Yes' : '是')
    const rows = detail.findAll('[data-testid="posted-tax-summaries"] tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('NL domestic (NL_DOMESTIC)')
    expect(rows[0]!.text()).toContain('Custom (10.000%)')
    expect(rows[0]!.text()).toContain('10.00')
    expect(rows[0]!.text()).toContain('1.25')
    expect(rows[1]!.text()).toContain('Frozen domestic label (NL_DOMESTIC)')
    expect(rows[1]!.text()).toContain('Frozen low (9.000%)')
    expect(rows[1]!.text()).toContain('50.00')
    expect(rows[1]!.text()).toContain('4.50')
    expect(detail.get('[data-testid="posting-history"] tbody tr').findAll('td').map(cell => cell.text())).toEqual(['4', 'user-1', '2026-10-05T11:00:00Z'])
    expect(detail.text()).toContain(locale === 'en' ? 'Posting history' : '入账历史')
    expect(detail.findAll('input, select, textarea, button')).toHaveLength(0)
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/external-sales/sale-1')
    expect(mocks.post).not.toHaveBeenCalled()
    expect(mocks.put).not.toHaveBeenCalled()
    expect(mocks.del).not.toHaveBeenCalled()
  })

  it.each(['en', 'zh'])('retains the reviewed energy P/L exclusion independently of source defaults (%s)', async (locale) => {
    const current = sale('POSTED')
    Object.assign(current, { include_in_pl: false, posted_source_name: 'Energy original' })
    const wrapper = await render('POSTED', current, locale)
    expect(wrapper.get('[data-testid="posted-include-pl"]').text()).toBe(locale === 'en' ? 'No' : '否')
    expect(wrapper.get('[data-testid="posted-detail"]').text()).toContain('Energy original')
    expect(wrapper.get('[data-testid="posted-tax-summaries"]').text()).toContain('1.25')
    expect(wrapper.findAll('button').some(item => ['Confirm and post', 'Save pending entry', '确认并入账'].includes(item.text()))).toBe(false)
  })
})
