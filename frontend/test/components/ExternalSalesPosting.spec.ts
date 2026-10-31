import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
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
    props: ['value', 'disabled', 'options', 'clearable'],
    emits: ['update:value'],
    template: '<select :disabled="disabled" :value="value ?? \'\'" @change="$emit(\'update:value\', $event.target.value)"><option value=""></option></select>',
  },
  useMessage: () => ({ success: mocks.success, error: mocks.error }),
}))

import ExternalSales from '../../src/views/externalSales/ExternalSales.vue'



function sale(status: 'PENDING' | 'POSTED' | 'VOID' = 'PENDING') {
  return {
    id: 'sale-1',
    source_id: 'source-1',
    source_name: 'Platform',
    original_number: 'EXT-42',
    document_kind: 'SALE',
    related_external_sale_id: null,
    historical_original_number: null,
    historical_original_explanation: null,
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
    created_at: '2026-10-05T10:00:00Z', updated_at: '2026-10-05T10:00:00Z', version: status === 'POSTED' ? 4 : 3,
    posted_at: status === 'POSTED' ? '2026-10-05T11:00:00Z' : null,
    posted_by: status === 'POSTED' ? 'user-1' : null,
    posted_source_name: status === 'POSTED' ? 'Platform' : null,
    original_net_total: status === 'POSTED' ? '10.00' : null,
    original_vat_total: status === 'POSTED' ? '1.25' : null,
    mismatch_ack_reason: status === 'POSTED' ? 'Original document reviewed.' : null,
    postings: status === 'POSTED' ? [{ id: 'posting-1', version: 4, actor_id: 'user-1', created_at: '2026-10-05T11:00:00Z', snapshot: {} }] : [],
    revisions: [],
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
    if (url.startsWith('/api/v1/external-sales/sale-1/revisions?')) {
      const params = new URL(url, 'http://localhost').searchParams
      const offset = Number(params.get('offset') || 0)
      const limit = Number(params.get('limit') || 50)
      return { items: current.revisions.slice(offset, offset + limit), total: current.revisions.length, offset, limit }
    }
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

  afterEach(() => { vi.unstubAllGlobals() })

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
    expect(posted.find('input[type="file"]').exists()).toBe(true)
    expect(posted.find('[data-testid="posted-detail"] input[type="date"]').exists()).toBe(false)
    expect(posted.text()).toContain('Revise posted entry')
    expect(posted.text()).toContain('Append supporting evidence')
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
    expect(detail.find('input[type="date"]').exists()).toBe(false)
    expect(detail.find('textarea').exists()).toBe(false)
    expect(detail.findAll('button').some(item => item.text().includes('Remove file'))).toBe(false)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.text()).toContain(locale === 'en' ? 'Append supporting evidence' : '追加支持材料')
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

  it.each(['en', 'zh'])('previews and saves a reasoned version-checked revision (%s)', async (locale) => {
    const current = sale('POSTED')
    const revised = {
      ...current,
      version: 5,
      document_date: '2027-01-10',
      revisions: [{ id: 'revision-1', version: 5, action: 'REVISION', reason: 'Date correction', actor_id: 'user-1', created_at: '2027-01-11T12:00:00Z', before_snapshot: { document_date: '2026-10-05' }, after_snapshot: { document_date: '2027-01-10' } }],
    }
    mocks.post.mockImplementation(async (url: string) => {
      if (url.endsWith('/revisions/preview')) return { sale_id: 'sale-1', expected_version: 4, old_period: '2026-10', new_period: '2027-01', old_net_total: '10.00', new_net_total: '10.00', old_vat_total: '1.25', new_vat_total: '1.25', warnings: ['Filing status is unknown.'] }
      if (url.endsWith('/revisions')) return revised
      throw new Error(`Unexpected POST ${url}`)
    })
    const wrapper = await render('POSTED', current, locale)
    await button(wrapper, locale === 'en' ? 'Revise posted entry' : '修订已过账记录').trigger('click')
    await wrapper.get('[data-testid="revision-reason"]').setValue('Date correction')
    await button(wrapper, locale === 'en' ? 'Preview revision' : '预览修订').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('2026-10')
    expect(wrapper.text()).toContain('2027-01')
    const save = button(wrapper, locale === 'en' ? 'Save audited revision' : '保存审计修订')
    expect((save.element as HTMLButtonElement).disabled).toBe(false)
    mocks.get.mockImplementation(async (url: string) => {
      if (url.startsWith('/api/v1/external-sales/sale-1/revisions?')) {
        return { items: revised.revisions, total: revised.revisions.length, offset: 0, limit: 50 }
      }
      throw new Error(`Unexpected GET ${url}`)
    })
    await save.trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/revisions', expect.objectContaining({
      expected_version: 4,
      reason: 'Date correction',
      document_date: '2026-10-05',
      include_in_pl: true,
    }))
    expect(wrapper.get('[data-testid="revision-history"]').text()).toContain('Date correction')
  })

  it('keeps a voided entry read-only while retaining originals and evidence access', async () => {
    const current = sale('VOID')
    const wrapper = await render('POSTED', current)
    expect(wrapper.find('[data-testid="posted-detail"]').text()).toContain('Voided')
    expect(wrapper.find('input[type="date"]').exists()).toBe(false)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.findAll('button').some(item => item.text().includes('Revise posted entry'))).toBe(false)
    expect(wrapper.findAll('button').some(item => item.text().includes('Void mistaken entry'))).toBe(false)
    expect(wrapper.findAll('button').some(item => item.text().includes('Remove file'))).toBe(false)
    wrapper.unmount()
  })

  it('appends evidence to a posted entry without replacing its retained original', async () => {
    const current = sale('POSTED')
    const appended = {
      ...current,
      files: [...current.files, { ...current.files[0], id: 'file-2', filename: 'support.pdf' }],
    }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => appended })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = await render('POSTED', current)
    const input = wrapper.find('input[type="file"]')
    Object.defineProperty(input.element, 'files', {
      value: [new File(['%PDF-support'], 'support.pdf', { type: 'application/pdf' })],
      configurable: true,
    })
    await input.trigger('change')
    await button(wrapper, 'Append supporting evidence').trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/external-sales/sale-1/files')
    expect(wrapper.text()).toContain('original.pdf')
    expect(wrapper.text()).toContain('support.pdf')
    expect(wrapper.findAll('button').some(item => item.text().includes('Remove file'))).toBe(false)
    wrapper.unmount()
  })

  it('pages through the full audit history', async () => {
    const current = sale('POSTED')
    current.revisions = Array.from({ length: 51 }, (_, index) => ({
      id: `revision-${index + 1}`, version: index + 5, action: 'REVISION',
      reason: `Correction ${index + 1}`, actor_id: 'user-1',
      created_at: '2027-01-11T12:00:00Z', before_snapshot: {}, after_snapshot: {},
    }))
    const wrapper = await render('POSTED', current)
    expect(wrapper.findAll('[data-testid="revision-history"] > tbody > tr')).toHaveLength(50)
    expect(wrapper.find('[data-testid="revision-pagination"]').text()).toContain('1–50 of 51')
    await button(wrapper, 'Next page').trigger('click')
    await flushPromises()
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/external-sales/sale-1/revisions?offset=50&limit=50')
    expect(wrapper.findAll('[data-testid="revision-history"] > tbody > tr')).toHaveLength(1)
    expect(wrapper.find('[data-testid="revision-pagination"]').text()).toContain('51–51 of 51')
    wrapper.unmount()
  })

  it('retains explicit P/L on a clearing event and submits stable retained tax identity', async () => {
    mocks.post.mockResolvedValue({ sale_id: 'sale-1', expected_version: 4, old_period: '2026-10', new_period: '2026-10', old_net_total: '10.00', new_net_total: '10.00', old_vat_total: '1.25', new_vat_total: '1.25', warnings: [] })
    const wrapper = await render('POSTED')
    await button(wrapper, 'Revise posted entry').trigger('click')
    const pl = wrapper.getComponent('[data-testid="include-in-pl"]')
    expect(pl.props('clearable')).toBe(false)
    expect(wrapper.getComponent('[data-testid="tax-rate-0"]').props('options')).toContainEqual({ label: 'Custom (10.000%)', value: 'rate-1' })
    pl.vm.$emit('update:value', null)
    await wrapper.get('[data-testid="revision-reason"]').setValue('Text review')
    await button(wrapper, 'Preview revision').trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/revisions/preview', expect.objectContaining({ include_in_pl: true, tax_summaries: [expect.objectContaining({ tax_summary_id: 'tax-1', net_amount: '10.00', vat_amount: '1.25' })] }))
    pl.vm.$emit('update:value', 'false')
    await button(wrapper, 'Preview revision').trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/revisions/preview', expect.objectContaining({ include_in_pl: false }))
    wrapper.unmount()
  })

  it.each(['en', 'zh'])('shows escaped frozen before/after amounts, taxes, P/L and evidence in %s history', async (locale) => {
    const current = sale('POSTED')
    const before = { document_date: '2026-10-05', covered_from: '2026-09-01', covered_to: '2026-09-30', description: '<img src=x onerror=alert(1)>', include_in_pl: true, original_net_total: '100.00', original_vat_total: '21.00', tax_summaries: [{ treatment_code: 'NL_DOMESTIC', treatment_label: 'Frozen domestic', rate_label: 'Frozen high', rate_percent: '21.000', net_amount: '100.00', vat_amount: '21.00' }], originals: [{ id: 'original-frozen', filename: '<script>evidence</script>.pdf', sha256: 'frozen-hash-before', mime_type: 'application/pdf', byte_size: 8 }] }
    const after = { ...before, description: 'Corrected narrative', include_in_pl: false, original_net_total: '200.00', original_vat_total: '42.00', tax_summaries: [{ ...before.tax_summaries[0], net_amount: '200.00', vat_amount: '42.00' }], originals: [...before.originals, { ...before.originals[0], id: 'support-frozen', filename: 'extra.pdf', sha256: 'frozen-hash-after' }] }
    current.revisions = [{ id: 'revision-1', version: 5, action: 'REVISION', reason: 'Change reviewed', actor_id: 'owner-1', created_at: '2026-10-11T12:00:00Z', before_snapshot: before, after_snapshot: after }]
    const wrapper = await render('POSTED', current, locale)
    const history = wrapper.get('[data-testid="revision-history"]')
    expect(history.find('details').exists()).toBe(true)
    await history.get('summary').trigger('click')
    const snapshots = history.findAll('.business-snapshot')
    expect(snapshots).toHaveLength(2)
    expect(snapshots[0].text()).toContain('100.00')
    expect(snapshots[0].text()).toContain('21.00')
    expect(snapshots[0].text()).toContain(locale === 'en' ? 'Yes' : '是')
    expect(snapshots[1].text()).toContain('200.00')
    expect(snapshots[1].text()).toContain('42.00')
    expect(snapshots[1].text()).toContain(locale === 'en' ? 'No' : '否')
    expect(history.text()).toContain('Frozen high (21.000%)')
    expect(history.text()).toContain('2026-09-01')
    expect(history.text()).toContain('frozen-hash-before')
    expect(history.text()).toContain('frozen-hash-after')
    expect(history.text()).toContain('<script>evidence</script>.pdf')
    expect(history.findAll('script, img')).toHaveLength(0)
    expect(history.findAll('input, textarea')).toHaveLength(0)
    wrapper.unmount()
  })

})
