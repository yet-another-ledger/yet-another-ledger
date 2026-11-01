import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }))
vi.mock('../../src/api/http', async (importOriginal) => ({ ...await importOriginal<typeof import('../../src/api/http')>(), ...api }))
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /></div>' }
  const button = { inheritAttrs: false, props: ['disabled', 'type', 'attrType'], template: '<button v-bind="$attrs" :disabled="disabled" :type="attrType || \'button\'"><slot /></button>' }
  const input = { inheritAttrs: false, props: ['value', 'type', 'placeholder'], emits: ['update:value'], template: '<textarea v-if="type === \'textarea\'" v-bind="$attrs" :value="value" @input="$emit(\'update:value\',$event.target.value)" /><input v-else v-bind="$attrs" :value="value" :placeholder="placeholder" @input="$emit(\'update:value\',$event.target.value)" />' }
  const select = { inheritAttrs: false, props: ['value', 'options', 'placeholder'], emits: ['update:value'], template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\',$event.target.value)"><option value=""></option><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>' }
  const datePicker = { inheritAttrs: false, props: ['formattedValue'], emits: ['update:formatted-value'], template: '<input type="date" v-bind="$attrs" :value="formattedValue || \'\'" @input="$emit(\'update:formatted-value\',$event.target.value || null)" />' }
  const upload = { inheritAttrs: false, props: ['max', 'multiple', 'disabled', 'accept', 'defaultUpload'], emits: ['change'], methods: { selectFiles(event: Event) { const input = event.target as HTMLInputElement; const fileList = Array.from(input.files || []).slice(0, Number(this.max)).map((file) => ({ id: file.name, name: file.name, status: 'pending', file })); this.$emit('change', { fileList }) } }, template: '<div><input type="file" :multiple="multiple" :disabled="disabled" :accept="accept" @change="selectFiles"><slot /></div>' }
  return {
    NAlert: box, NButton: button, NCard: box, NCheckbox: { props: ['checked'], template: '<input type="checkbox" />' },
    NForm: { inheritAttrs: false, template: '<form v-bind="$attrs"><slot /></form>' }, NFormItem: box,
    NInput: input, NModal: box, NSelect: select, NDatePicker: datePicker, NUpload: upload, useMessage: () => ({ success: vi.fn(), error: vi.fn() }),
  }
})

import en from '../../src/locales/en.json'
import ExternalSales from '../../src/views/externalSales/ExternalSales.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
const source = { id: 'source-platform', name: 'Platform', kind: 'PLATFORM', default_include_in_pl: true, active: true }
const treatment = { id: 'treatment-domestic', code: 'NL_DOMESTIC', label: 'Domestic sales', side: 'SALES', effect: 'APPLY_RATE', requires_icp: false, active: true }
const rates = [{ id: 'rate-21', label: 'High', percent: '21.000', active: true }, { id: 'rate-9', label: 'Low', percent: '9.000', active: true }]

const saved = { id: 'sale-1', source_id: source.id, source_name: source.name, original_number: 'saved-number', document_kind: 'SALE', related_external_sale_id: null, historical_original_number: null, historical_original_explanation: null, document_date: '2026-01-02', covered_from: null, covered_to: null, counterparty: 'saved counterparty', description: 'saved description', include_in_pl: true, status: 'PENDING', tax_summaries: [{ id: 'tax-1', treatment_id: treatment.id, rate_id: rates[0].id, treatment_code: 'NL_DOMESTIC', treatment_label: 'Domestic sales', rate_label: 'High', rate_percent: '21', net_amount: '100.00', vat_amount: '21.00' }], files: [{ id: 'file-1', filename: 'original.pdf', mime_type: 'application/pdf', byte_size: 9, sha256: '', duplicate_warning: false, created_at: '' }], created_at: '', updated_at: '', version: 1, posted_at: null, posted_by: null, posted_source_name: null, original_net_total: null, original_vat_total: null, mismatch_ack_reason: null, postings: [] }

function setup(path = '/external-sales/new') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/external-sales', component: ExternalSales },
      { path: '/external-sales/new', name: 'external-sales-new', component: ExternalSales },
      { path: '/external-sales/:id', name: 'external-sales-detail', component: ExternalSales },
    ],
  })
  return { router, mount: async () => { await router.push(path); await router.isReady(); const wrapper = mount(ExternalSales, { global: { plugins: [router, i18n] } }); await flushPromises(); return wrapper } }
}

describe('External Sales manual draft editor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/external-sales-sources')) return Promise.resolve({ items: [source], total: 1, limit: 100, offset: 0 })
      if (url.split('?')[0] === '/api/v1/external-sales') return Promise.resolve({ items: [], total: 0, limit: 50, offset: 0 })
      if (url === '/api/v1/external-sales/sale-1') return Promise.resolve(structuredClone(saved))
      if (url.startsWith('/api/v1/external-sales/sale-1/revisions?')) return Promise.resolve({ items: [], total: 0, offset: 0, limit: 50 })
      if (url.includes('/vat-treatments')) return Promise.resolve({ items: [treatment] })
      if (url.includes('/vat-rates')) return Promise.resolve({ items: rates })
      return Promise.resolve({})
    })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('submits the exact original number, dictionary identities, and raw multi-rate amounts', async () => {
    const wrapper = await setup().mount()
    const selects = wrapper.findAll('select')
    await selects[0].setValue(source.id)
    await wrapper.findAll('input')[0].setValue('  EXT 017  ')
    await wrapper.findAll('select')[3].setValue(treatment.id)
    await wrapper.findAll('select')[4].setValue(rates[0].id)
    await wrapper.findAll('input[placeholder="Original net amount"]')[0].setValue('100.005')
    await wrapper.findAll('input[placeholder="Original VAT amount"]')[0].setValue('21.005')
    await wrapper.findAll('button').find((button) => button.text() === 'Add tax row')!.trigger('click')
    await wrapper.findAll('select')[5].setValue(treatment.id)
    await wrapper.findAll('select')[6].setValue(rates[1].id)
    await wrapper.findAll('input[placeholder="Original net amount"]')[1].setValue('50')
    await wrapper.findAll('input[placeholder="Original VAT amount"]')[1].setValue('4.5')
    const created = { id: 'sale-1', source_id: source.id, source_name: source.name, original_number: '  EXT 017  ', document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: true, status: 'PENDING', tax_summaries: [], files: [], created_at: '', updated_at: '' }
    api.post.mockResolvedValue(created)
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/external-sales/sale-1') return Promise.resolve(created)
      if (url.startsWith('/api/v1/external-sales/sale-1/revisions?')) return Promise.resolve({ items: [], total: 0, offset: 0, limit: 50 })
      if (url.startsWith('/api/v1/external-sales-sources')) return Promise.resolve({ items: [source], total: 1, limit: 100, offset: 0 })
      if (url.split('?')[0] === '/api/v1/external-sales') return Promise.resolve({ items: [created], total: 1, limit: 50, offset: 0 })
      if (url.includes('/vat-treatments')) return Promise.resolve({ items: [treatment] })
      if (url.includes('/vat-rates')) return Promise.resolve({ items: rates })
      return Promise.resolve({})
    })
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const [url, body] = api.post.mock.calls[0]
    expect(url).toBe('/api/v1/external-sales')
    expect(body.original_number).toBe('  EXT 017  ')
    expect(body.include_in_pl).toBe(true)
    expect(body.tax_summaries[0]).toEqual({ treatment_id: treatment.id, rate_id: rates[0].id, net_amount: '100.005', vat_amount: '21.005' })
    expect(body.tax_summaries[0]).not.toHaveProperty('rate_percent')
    expect(body.tax_summaries[1]).toEqual({ treatment_id: treatment.id, rate_id: rates[1].id, net_amount: '50', vat_amount: '4.5' })
  })

  it('discloses the provider, retries extraction, and protects edited fields while filling blanks', async () => {
    const pending = {
      ...structuredClone(saved),
      original_number: null,
      document_date: null,
      counterparty: null,
      description: null,
      tax_summaries: [],
    }
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/external-sales-sources')) return Promise.resolve({ items: [source], total: 1, limit: 100, offset: 0 })
      if (url === '/api/v1/external-sales/sale-1') return Promise.resolve(structuredClone(pending))
      if (url.startsWith('/api/v1/external-sales/sale-1/revisions?')) return Promise.resolve({ items: [], total: 0, offset: 0, limit: 50 })
      if (url.includes('/vat-treatments')) return Promise.resolve({ items: [treatment] })
      if (url.includes('/vat-rates')) return Promise.resolve({ items: rates })
      if (url.split('?')[0] === '/api/v1/external-sales') return Promise.resolve({ items: [pending], total: 1, limit: 50, offset: 0 })
      return Promise.resolve({})
    })
    api.post.mockResolvedValue({
      ...pending,
      ai_state: 'READY',
      ai_suggestion: {
        document_date: '2026-03-04', original_number: 'AI-44', counterparty: 'AI counterparty',
        description: 'AI summary', observed_net_total: null, observed_vat_total: null,
        explicit_electricity_sale_component: null, confidence: 'medium',
        review_notes: ['Check source values.'], tax_summaries: [],
      },
    })
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect(wrapper.text()).toContain('uploaded originals are sent to the configured AI provider')
    await wrapper.get('[data-testid="sale-number"]').setValue('manual-number')
    await wrapper.find('input[placeholder="Original net amount"]').setValue('12.34')
    await wrapper.findAll('button').find((button) => button.text() === 'Retry extraction')!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/external-sales/sale-1/extract', {})
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('manual-number')
    expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe('2026-03-04')
    expect((wrapper.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('12.34')
    expect(wrapper.text()).toContain('Check source values.')
    wrapper.unmount()
  })


  const suggestion = {
    document_date: '2026-03-04', original_number: 'AI-44', counterparty: 'AI counterparty',
    description: 'AI summary', observed_net_total: '100', observed_vat_total: '21',
    explicit_electricity_sale_component: null, confidence: 'medium', review_notes: ['Review originals.'],
    tax_summaries: [{ rate_percent: '21', net_amount: '100', vat_amount: '21' }],
  }
  function aiPending(ai_state = 'READY') {
    return {
      ...structuredClone(saved), source_id: null, include_in_pl: null,
      original_number: null, document_date: null, counterparty: null, description: null,
      tax_summaries: [], ai_state, ai_error_code: null,
      ai_suggestion: ai_state === 'READY' ? structuredClone(suggestion) : null,
    }
  }
  function serveDetail(sale: object) {
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/sale-1'
      ? Promise.resolve(structuredClone(sale)) : originalGet(url))
  }

  it.each(['observed_net_total', 'observed_vat_total'])('applies a valid partial date with nullable invalid %s from the API', async (field) => {
    const partial = { ...suggestion, original_number: null, counterparty: null, description: null,
      observed_net_total: null, observed_vat_total: null, tax_summaries: [], [field]: null }
    serveDetail({ ...aiPending(), ai_suggestion: partial })
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe('2026-03-04')
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('')
    expect((wrapper.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('Suggestions are ready for review')
    wrapper.unmount()
  })

  it.each(['upload', 'remove'] as const)('waits for a delayed %s response before retrying the current file group', async (action) => {
    const secondFile = { ...saved.files[0], id: 'file-2', filename: 'second.pdf' }
    const pending = { ...aiPending('NOT_RUN'), files: [...saved.files, secondFile] }
    serveDetail(pending)
    let finishFile!: (value: unknown) => void
    const fetchMock = vi.fn().mockImplementation(() => new Promise(resolve => { finishFile = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    api.del.mockImplementation(() => new Promise(resolve => { finishFile = resolve }))
    const changed = { ...pending, version: 2,
      files: action === 'upload' ? [...pending.files, { ...secondFile, id: 'file-3' }] : [secondFile],
      ai_state: 'READY', ai_suggestion: { document_date: '2026-10-30', review_notes: ['FILE-AUTO'] } }
    let finishRetry!: (value: unknown) => void
    api.post.mockImplementation(() => new Promise(resolve => { finishRetry = resolve }))
    const wrapper = await setup('/external-sales/sale-1').mount()
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
    await input.trigger('change')
    await button(action === 'upload' ? 'Upload originals' : 'Remove file').trigger('click')
    expect(button('Retry extraction').attributes('disabled')).toBeDefined()
    expect(button('Upload originals').attributes('disabled')).toBeDefined()
    expect(button('Remove file').attributes('disabled')).toBeDefined()
    await button('Retry extraction').trigger('click')
    await button('Upload originals').trigger('click')
    await button('Remove file').trigger('click')
    expect(api.post).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(action === 'upload' ? 1 : 0)
    expect(api.del).toHaveBeenCalledTimes(action === 'remove' ? 1 : 0)
    // The owner remains free to type while the file request and automatic run finish.
    await wrapper.find('textarea').setValue('MANUAL')
    await wrapper.get('[data-testid="include-in-pl"]').setValue('false')
    await wrapper.find('input[placeholder="Original VAT amount"]').setValue('0')
    finishFile(action === 'upload' ? { ok: true, json: async () => changed } : changed)
    await flushPromises()
    expect(button('Retry extraction').attributes('disabled')).toBeUndefined()
    expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe('2026-10-30')
    await button('Retry extraction').trigger('click')
    expect(api.post).toHaveBeenCalledWith('/api/v1/external-sales/sale-1/extract', {})
    finishRetry({ ...changed, ai_suggestion: { ...suggestion, original_number: 'LATEST-RETRY', review_notes: ['LATEST-RETRY'] } })
    await flushPromises()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('LATEST-RETRY')
    expect(wrapper.text()).toContain('LATEST-RETRY')
    expect(wrapper.text()).not.toContain('FILE-AUTO')
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('MANUAL')
    expect((wrapper.find('input[placeholder="Original VAT amount"]').element as HTMLInputElement).value).toBe('0')
    expect((wrapper.get('[data-testid="include-in-pl"]').element as HTMLSelectElement).value).toBe('false')
    wrapper.unmount()
  })

  it.each(['upload', 'remove'] as const)('releases retry after a failed %s without losing manual fields', async (action) => {
    serveDetail(aiPending('NOT_RUN'))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network unavailable')))
    api.del.mockRejectedValue(new Error('Network unavailable'))
    api.post.mockResolvedValue(aiPending())
    const wrapper = await setup('/external-sales/sale-1').mount()
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await wrapper.find('textarea').setValue('Manual after failure')
    if (action === 'upload') {
      const input = wrapper.get('input[type="file"]')
      Object.defineProperty(input.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
      await input.trigger('change')
    }
    await button(action === 'upload' ? 'Upload originals' : 'Remove file').trigger('click')
    await flushPromises()
    expect(button('Retry extraction').attributes('disabled')).toBeUndefined()
    expect(button('Remove file').attributes('disabled')).toBeUndefined()
    await button('Retry extraction').trigger('click')
    await flushPromises()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('AI-44')
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('Manual after failure')
    wrapper.unmount()
  })

  it.each(['upload', 'remove'] as const)('keeps a new route file request busy when an old %s response finishes', async (action) => {
    serveDetail(aiPending('NOT_RUN'))
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/next'
      ? Promise.resolve({ ...aiPending('NOT_RUN'), id: 'next' }) : originalGet(url))
    const completions: Array<(value: unknown) => void> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(resolve => completions.push(resolve))))
    api.del.mockImplementation(() => new Promise(resolve => completions.push(resolve)))
    const { router, mount } = setup('/external-sales/sale-1')
    const wrapper = await mount()
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    const startFile = async () => {
      if (action === 'upload') {
        const input = wrapper.get('input[type="file"]')
        Object.defineProperty(input.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
        await input.trigger('change')
      }
      await button(action === 'upload' ? 'Upload originals' : 'Remove file').trigger('click')
    }
    await startFile()
    await router.push('/external-sales/next')
    await flushPromises()
    expect(button('Retry extraction').attributes('disabled')).toBeUndefined()
    await startFile()
    const complete = (index: number, id: string) => {
      const result = { ...aiPending(), id, version: 2 }
      completions[index]!(action === 'upload' ? { ok: true, json: async () => result } : result)
    }
    complete(0, 'sale-1')
    await flushPromises()
    expect(button('Retry extraction').attributes('disabled')).toBeDefined()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('')
    complete(1, 'next')
    await flushPromises()
    expect(button('Retry extraction').attributes('disabled')).toBeUndefined()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('AI-44')
    wrapper.unmount()
  })

  it('applies durable READY suggestions on first detail load and a fresh page mount', async () => {
    serveDetail(aiPending())
    for (let visit = 0; visit < 2; visit++) {
      const wrapper = await setup('/external-sales/sale-1').mount()
      expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('AI-44')
      expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe('2026-03-04')
      expect((wrapper.get('[data-testid="tax-rate-0"]').element as HTMLSelectElement).value).toBe('rate-21')
      expect((wrapper.find('.tax-row select').element as HTMLSelectElement).value).toBe('')
      expect((wrapper.get('[data-testid="include-in-pl"]').element as HTMLSelectElement).value).toBe('')
      expect(api.post).not.toHaveBeenCalled()
      wrapper.unmount()
    }
  })

  it('preserves existing header values, manual treatment/P&L and explicit zero amounts on READY load', async () => {
    const sale = { ...structuredClone(saved), include_in_pl: false, ai_state: 'READY', ai_suggestion: suggestion }
    sale.tax_summaries[0]!.net_amount = '0.000'
    sale.tax_summaries[0]!.vat_amount = '0.000'
    serveDetail(sale)
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe(saved.original_number)
    expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe(saved.document_date)
    expect((wrapper.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('0.000')
    expect((wrapper.find('input[placeholder="Original VAT amount"]').element as HTMLInputElement).value).toBe('0.000')
    expect((wrapper.find('.tax-row select').element as HTMLSelectElement).value).toBe(treatment.id)
    expect((wrapper.get('[data-testid="include-in-pl"]').element as HTMLSelectElement).value).toBe('false')
    wrapper.unmount()
  })

  it.each(['', null])('keeps a saved manual empty description %s after refresh and a fresh mount, and allows a new retry', async (empty) => {
    let persisted = { ...structuredClone(saved), description: saved.description as string | null, include_in_pl: false, ai_state: 'READY', ai_error_code: null, ai_suggestion: structuredClone(suggestion) as typeof suggestion | null }
    persisted.tax_summaries[0]!.net_amount = '0.000'
    persisted.tax_summaries[0]!.vat_amount = '0.000'
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/sale-1'
      ? Promise.resolve(structuredClone(persisted)) : originalGet(url))
    api.put.mockImplementation(async (_url, body) => {
      expect(body.description).toBe('')
      expect(body.include_in_pl).toBe(false)
      expect(body.tax_summaries[0].net_amount).toBe('0.000')
      // Match the API's durable invalidation on a successful manual PUT.
      persisted = { ...persisted, version: persisted.version + 1, description: empty, ai_state: 'NOT_RUN', ai_suggestion: null }
      return structuredClone(persisted)
    })
    api.post.mockImplementation(async (url: string) => {
      if (url.endsWith('/calculate')) return { sale_id: saved.id, expected_version: persisted.version, original_net_total: '0', original_vat_total: '0', derived_vat_total: '0', mismatch: false, mismatch_row_indices: [], warnings: [] }
      if (url.endsWith('/extract')) {
        persisted = { ...persisted, ai_state: 'READY', ai_suggestion: { ...structuredClone(suggestion), description: 'New extraction summary' } }
        return structuredClone(persisted)
      }
      throw new Error(`Unexpected request ${url}`)
    })
    const wrapper = await setup('/external-sales/sale-1').mount()
    await wrapper.find('textarea').setValue('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledOnce()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')
    const review = () => wrapper.findAll('button').find(button => button.text() === 'Review posting')!
    expect(review().attributes('disabled')).toBeUndefined()
    await review().trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/calculate', { expected_version: 2 })
    wrapper.unmount()

    const reopened = await setup('/external-sales/sale-1').mount()
    expect((reopened.find('textarea').element as HTMLTextAreaElement).value).toBe('')
    expect(reopened.findAll('button').find(button => button.text() === 'Review posting')!.attributes('disabled')).toBeUndefined()
    await reopened.findAll('button').find(button => button.text() === 'Retry extraction')!.trigger('click')
    await flushPromises()
    expect((reopened.find('textarea').element as HTMLTextAreaElement).value).toBe('New extraction summary')
    expect((reopened.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('0.000')
    expect((reopened.get('[data-testid="include-in-pl"]').element as HTMLSelectElement).value).toBe('false')
    reopened.unmount()
  })

  it.each([
    ['21', '21.000'], ['9', '9.000'], ['7.125', '7.125'], ['7.100', '7.1'],
    ['0', '0.000'], ['1E+1', '10.000'], ['1E-3', '0.001'],
  ])('matches observed rate %s to dictionary decimal %s without changing raw amounts', async (observed, percent) => {
    const pending = aiPending('NOT_RUN')
    serveDetail(pending)
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url.includes('/vat-rates') ? Promise.resolve({ items: [
      { id: 'inactive-match', label: 'Inactive', percent, active: false },
      { id: 'different-rate', label: 'Different', percent: '3.333', active: true },
      { id: 'custom-rate', label: 'Custom user rate', percent, active: true },
    ] }) : originalGet(url))
    api.post.mockResolvedValue({ ...pending, ai_state: 'READY', ai_suggestion: {
      ...suggestion, tax_summaries: [{ rate_percent: observed, net_amount: '100.005', vat_amount: '0.000' }],
    } })
    const wrapper = await setup('/external-sales/sale-1').mount()
    await wrapper.findAll('button').find(button => button.text() === 'Retry extraction')!.trigger('click')
    await flushPromises()
    expect((wrapper.get('[data-testid="tax-rate-0"]').element as HTMLSelectElement).value).toBe('custom-rate')
    expect((wrapper.find('.tax-row select').element as HTMLSelectElement).value).toBe('')
    expect((wrapper.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('100.005')
    expect((wrapper.find('input[placeholder="Original VAT amount"]').element as HTMLInputElement).value).toBe('0.000')
    wrapper.unmount()
  })

  it('applies multiple rates and leaves unmatched rates for manual selection', async () => {
    const sale = aiPending()
    sale.ai_suggestion!.tax_summaries = [
      { rate_percent: '21', net_amount: '100', vat_amount: '21' },
      { rate_percent: '9', net_amount: '50', vat_amount: '4.5' },
      { rate_percent: '5.555', net_amount: '10', vat_amount: '0' },
    ]
    serveDetail(sale)
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect(wrapper.findAll('[data-testid^="tax-rate-"]').map(row => (row.element as HTMLSelectElement).value)).toEqual(['rate-21', 'rate-9', ''])
    expect(wrapper.findAll('.tax-row select:first-child').map(row => (row.element as HTMLSelectElement).value)).toEqual(['', '', ''])
    wrapper.unmount()
  })

  it.each(['ready', 'failure'])('recovers durable RUNNING with one local retry and re-enables retry after %s', async (outcome) => {
    const pending = aiPending('RUNNING')
    serveDetail(pending)
    let complete!: (value: unknown) => void
    let fail!: (error: Error) => void
    api.post.mockImplementation(() => new Promise((resolve, reject) => { complete = resolve; fail = reject }))
    const wrapper = await setup('/external-sales/sale-1').mount()
    const retry = () => wrapper.findAll('button').find(button => button.text() === 'Retry extraction')!
    expect(wrapper.text()).toContain('Extracting observed values')
    expect(retry().attributes('disabled')).toBeUndefined()
    await retry().trigger('click')
    await retry().trigger('click')
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(retry().attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="sale-number"]').setValue('MANUAL')
    await wrapper.find('input[type="date"]').setValue('2026-04-05')
    await wrapper.find('input[type="date"]').setValue('')
    await wrapper.find('input[placeholder="Original net amount"]').setValue('0')
    await wrapper.find('input[placeholder="Original VAT amount"]').setValue('0')
    await wrapper.get('[data-testid="include-in-pl"]').setValue('false')
    if (outcome === 'ready') complete(aiPending())
    else fail(new Error('Recoverable network failure'))
    await flushPromises()
    expect(retry().attributes('disabled')).toBeUndefined()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('MANUAL')
    expect((wrapper.find('input[type="date"]').element as HTMLInputElement).value).toBe('')
    expect((wrapper.find('input[placeholder="Original net amount"]').element as HTMLInputElement).value).toBe('0')
    expect((wrapper.find('input[placeholder="Original VAT amount"]').element as HTMLInputElement).value).toBe('0')
    expect((wrapper.get('[data-testid="include-in-pl"]').element as HTMLSelectElement).value).toBe('false')
    wrapper.unmount()
  })

  it('keeps posted read-only state when an older extraction HTTP response arrives after confirmation', async () => {
    let complete!: (value: unknown) => void
    api.post.mockImplementation((url: string) => {
      if (url.endsWith('/extract')) return new Promise(resolve => { complete = resolve })
      if (url.endsWith('/calculate')) return Promise.resolve({ sale_id: saved.id, expected_version: 1, original_net_total: '100', original_vat_total: '21', derived_vat_total: '21', mismatch: false, mismatch_row_indices: [], warnings: [] })
      if (url.endsWith('/confirm')) return Promise.resolve({ ...saved, status: 'POSTED', version: 2, posted_source_name: source.name, posted_at: '2026-10-31T00:00:00Z', original_net_total: '100', original_vat_total: '21' })
      throw new Error(`Unexpected request ${url}`)
    })
    const wrapper = await setup('/external-sales/sale-1').mount()
    const button = (label: string) => wrapper.findAll('button').find(row => row.text() === label)!
    await button('Retry extraction').trigger('click')
    await button('Review posting').trigger('click')
    await flushPromises()
    await button('Confirm and post').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="posted-detail"]').exists()).toBe(true)
    complete({ ...saved, ai_state: 'READY', ai_suggestion: suggestion })
    await flushPromises()
    expect(wrapper.find('[data-testid="posted-detail"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sale-number"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="posted-detail"]').text()).toContain(saved.original_number)
    wrapper.unmount()
  })

  it('ignores old extraction after a saved newer version on the same route', async () => {
    let complete!: (value: unknown) => void
    api.post.mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const wrapper = await setup('/external-sales/sale-1').mount()
    await wrapper.findAll('button').find(button => button.text() === 'Retry extraction')!.trigger('click')
    const next = { ...structuredClone(saved), version: 2, original_number: 'NEW-VERSION' }
    api.put.mockResolvedValue(next)
    serveDetail(next)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    complete({ ...saved, ai_state: 'READY', ai_suggestion: suggestion })
    await flushPromises()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('NEW-VERSION')
    api.post.mockResolvedValue({})
    await wrapper.findAll('button').find(button => button.text() === 'Review posting')!.trigger('click')
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/external-sales/sale-1/calculate', { expected_version: 2 })
    wrapper.unmount()
  })

  it('keeps a new route request busy when an old extraction returns last', async () => {
    serveDetail(aiPending('NOT_RUN'))
    const next = { ...aiPending('NOT_RUN'), id: 'next' }
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/next' ? Promise.resolve(next) : originalGet(url))
    const completions: Array<(value: unknown) => void> = []
    api.post.mockImplementation(() => new Promise(resolve => completions.push(resolve)))
    const { router, mount } = setup('/external-sales/sale-1')
    const wrapper = await mount()
    const retry = () => wrapper.findAll('button').find(button => button.text() === 'Retry extraction')!
    await retry().trigger('click')
    await router.push('/external-sales/next')
    await flushPromises()
    await retry().trigger('click')
    completions[0]!({ ...aiPending(), original_number: 'OLD' })
    await flushPromises()
    expect(retry().attributes('disabled')).toBeDefined()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('')
    completions[1]!({ ...aiPending(), id: 'next', ai_suggestion: { ...suggestion, original_number: 'NEXT-AI' } })
    await flushPromises()
    expect(retry().attributes('disabled')).toBeUndefined()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('NEXT-AI')
    wrapper.unmount()
  })

  it('rejects an old extraction after a file change and a newer request generation', async () => {
    const secondFile = { ...saved.files[0], id: 'file-2', filename: 'second.pdf' }
    serveDetail({ ...aiPending('NOT_RUN'), files: [...saved.files, secondFile] })
    const completions: Array<(value: unknown) => void> = []
    api.post.mockImplementation(() => new Promise(resolve => completions.push(resolve)))
    const wrapper = await setup('/external-sales/sale-1').mount()
    const retry = () => wrapper.findAll('button').find(button => button.text() === 'Retry extraction')!
    await retry().trigger('click')
    const changed = { ...aiPending('FAILED'), version: 2, files: [secondFile] }
    api.del.mockResolvedValue(changed)
    await wrapper.findAll('button').find(button => button.text() === 'Remove file')!.trigger('click')
    await flushPromises()
    await retry().trigger('click')
    completions[1]!({ ...changed, ai_state: 'READY', ai_suggestion: { ...suggestion, original_number: 'NEW-RUN' } })
    await flushPromises()
    completions[0]!({ ...aiPending(), ai_suggestion: { ...suggestion, original_number: 'OLD-RUN' } })
    await flushPromises()
    expect((wrapper.get('[data-testid="sale-number"]').element as HTMLInputElement).value).toBe('NEW-RUN')
    expect(wrapper.text()).toContain('Suggestions are ready for review')
    wrapper.unmount()
  })

  it.each(['upload', 'remove'] as const)('preserves all unsaved fields after an original %s', async (action) => {
    const wrapper = await setup('/external-sales/sale-1').mount()
    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('unsaved-corrected-number')
    await inputs[1].setValue('2026-02-03')
    await inputs[2].setValue('new counterparty')
    await inputs[3].setValue('2026-01-01')
    await inputs[4].setValue('2026-01-31')
    await wrapper.find('textarea').setValue('unsaved description')
    await wrapper.find('[data-testid="include-in-pl"]').setValue('false')
    await wrapper.findAll('select')[4].setValue(rates[1].id)
    await wrapper.find('input[placeholder="Original net amount"]').setValue('123.456')
    await wrapper.find('input[placeholder="Original VAT amount"]').setValue('11.111')
    const serverResult = { ...structuredClone(saved), files: action === 'remove' ? [] : [...saved.files, { ...saved.files[0], id: 'file-2', filename: 'added.pdf' }] }
    if (action === 'remove') {
      api.del.mockResolvedValue(serverResult)
      await wrapper.findAll('button').find((button) => button.text() === 'Remove file')!.trigger('click')
    } else {
      const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => serverResult })
      vi.stubGlobal('fetch', fetchMock)
      const fileInput = wrapper.find('input[type="file"]')
      Object.defineProperty(fileInput.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
      await fileInput.trigger('change')
      await wrapper.findAll('button').find((button) => button.text() === 'Upload originals')!.trigger('click')
      await flushPromises()
      expect(fetchMock).toHaveBeenCalledTimes(1)
    }
    await flushPromises()
    expect(wrapper.findAll('.file-row')).toHaveLength(serverResult.files.length)
    expect(wrapper.findAll('input')[0].element.value).toBe('unsaved-corrected-number')
    api.put.mockResolvedValue(serverResult)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/external-sales/sale-1', {
      source_id: source.id, original_number: 'unsaved-corrected-number', document_kind: 'SALE', related_external_sale_id: null, historical_original_number: null, historical_original_explanation: null, document_date: '2026-02-03',
      covered_from: '2026-01-01', covered_to: '2026-01-31', counterparty: 'new counterparty',
      description: 'unsaved description', include_in_pl: false,
      tax_summaries: [{ treatment_id: treatment.id, rate_id: rates[1].id, net_amount: '123.456', vat_amount: '11.111' }],
    })
    wrapper.unmount()
  })

  it('pages forward and backward through all entries including the oldest draft', async () => {
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => {
      if (url.split('?')[0] !== '/api/v1/external-sales') return originalGet(url)
      const params = new URL(url, 'http://localhost').searchParams
      const offset = Number(params.get('offset') || 0)
      const limit = Number(params.get('limit') || 50)
      return Promise.resolve({ items: Array.from({ length: Math.min(limit, 51 - offset) }, (_, i) => ({ ...saved, id: `sale-${offset + i}`, original_number: `record-${offset + i}` })), total: 51, limit, offset })
    })
    const wrapper = await setup('/external-sales').mount()
    const button = (text: string) => wrapper.findAll('button').find((row) => row.text() === text)!
    expect(wrapper.findAll('.sale-row')).toHaveLength(50)
    expect(button('Previous page').attributes('disabled')).toBeDefined()
    await button('Next page').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/external-sales?limit=50&offset=50')
    expect(wrapper.findAll('.sale-row')).toHaveLength(1)
    expect(wrapper.find('.sale-row').text()).toContain('record-50')
    expect(button('Next page').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('51–51 of 51')
    await button('Previous page').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.sale-row')).toHaveLength(50)
    expect(wrapper.find('.sale-row').text()).toContain('record-0')
    wrapper.unmount()
  })

  it('shows an empty range and disables pagination for an empty list', async () => {
    const wrapper = await setup('/external-sales').mount()
    expect(wrapper.text()).toContain('0–0 of 0')
    for (const text of ['Previous page', 'Next page']) {
      expect(wrapper.findAll('button').find((button) => button.text() === text)!.attributes('disabled')).toBeDefined()
    }
    wrapper.unmount()
  })

  it('enables the file picker immediately after removing one of five originals', async () => {
    const full = { ...structuredClone(saved), files: Array.from({ length: 5 }, (_, i) => ({ ...saved.files[0], id: `file-${i}` })) }
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/sale-1' ? Promise.resolve(full) : originalGet(url))
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect(wrapper.find('input[type="file"]').attributes('disabled')).toBeDefined()
    api.del.mockResolvedValue({ ...full, files: full.files.slice(1) })
    await wrapper.findAll('button').find((button) => button.text() === 'Remove file')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('input[type="file"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.findAll('.file-row')).toHaveLength(4)
    wrapper.unmount()
  })

  it('loads every source page for management and selection, also after a source is saved', async () => {
    const allSources = Array.from({ length: 251 }, (_, i) => ({ ...source, id: `source-${i}`, name: `Source ${i}` }))
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => {
      if (!url.startsWith('/api/v1/external-sales-sources')) return originalGet(url)
      const params = new URL(url, 'http://localhost').searchParams
      const limit = Number(params.get('limit') || 50)
      const offset = Number(params.get('offset') || 0)
      return Promise.resolve({ items: allSources.slice(offset, offset + limit), total: allSources.length, limit, offset })
    })
    const { router, mount } = setup('/external-sales')
    const wrapper = await mount()
    expect(wrapper.findAll('.source-row')).toHaveLength(251)
    const last = wrapper.findAll('.source-row')[250]
    await last.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('input').element.value).toBe('Source 250')
    api.put.mockResolvedValue(allSources[250])
    await wrapper.findAll('button').filter((row) => row.text() === 'Save source').at(-1)!.trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/external-sales-sources/source-250', expect.objectContaining({ name: 'Source 250' }))
    expect(wrapper.findAll('.source-row')).toHaveLength(251)
    expect(api.get.mock.calls.filter(([url]) => url === '/api/v1/external-sales-sources?limit=100&offset=200')).toHaveLength(2)
    await router.push('/external-sales/new')
    await flushPromises()
    expect(wrapper.findAll('select')[0].findAll('option')).toHaveLength(252)
    await wrapper.findAll('select')[0].setValue('source-250')
    expect(wrapper.findAll('select')[0].element.value).toBe('source-250')
    wrapper.unmount()
  })

  it.each(['/external-sales/new', '/external-sales/sale-1'])('normalizes cleared optional dates when saving %s', async (path) => {
    const wrapper = await setup(path).mount()
    for (const input of wrapper.findAll('input[type="date"]')) {
      await input.setValue('2026-03-04')
      await input.setValue('')
    }
    api.post.mockResolvedValue(structuredClone(saved))
    api.put.mockResolvedValue(structuredClone(saved))
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const request = path.endsWith('/new') ? api.post : api.put
    expect(request).toHaveBeenCalledWith(path.endsWith('/new') ? '/api/v1/external-sales' : '/api/v1/external-sales/sale-1', expect.objectContaining({ document_date: null, covered_from: null, covered_to: null }))
    wrapper.unmount()
  })

  it('keeps selected File objects for retry after upload failure and clears them after success', async () => {
    const wrapper = await setup('/external-sales/sale-1').mount()
    const picker = wrapper.get('[data-testid="file-upload"] input[type="file"]')
    const selected = new File(['%PDF-evidence'], 'evidence.pdf', { type: 'application/pdf' })
    Object.defineProperty(picker.element, 'files', { value: [selected], configurable: true })
    await picker.trigger('change')
    const upload = () => wrapper.findAll('button').find((button) => button.text() === 'Upload originals')!
    expect(upload().attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="file-upload"] input[type="file"]').attributes('accept')).toBe('image/png,image/jpeg,image/webp,application/pdf')

    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('Network unavailable'))
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...structuredClone(saved), files: [...saved.files, { ...saved.files[0], id: 'file-2', filename: 'evidence.pdf' }] }) })
    vi.stubGlobal('fetch', fetchMock)
    await upload().trigger('click')
    await flushPromises()
    expect(upload().attributes('disabled')).toBeUndefined()
    await upload().trigger('click')
    await flushPromises()
    const form = fetchMock.mock.calls[1]![1]!.body as FormData
    expect(form.getAll('files')).toEqual([selected])
    expect((form.get('files') as File).name).toBe('evidence.pdf')
    expect(upload().attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="file-upload"] input[type="file"]').element).not.toBe(picker.element)
    wrapper.unmount()
  })

  it('ignores an older detail response and keeps every operation on the current route', async () => {
    let resolveSlow!: (value: typeof saved) => void
    const slow = new Promise<typeof saved>((resolve) => { resolveSlow = resolve })
    const fast = { ...structuredClone(saved), id: 'fast', original_number: 'fast-number' }
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/external-sales/slow') return slow
      if (url === '/api/v1/external-sales/fast') return Promise.resolve(structuredClone(fast))
      return originalGet(url)
    })
    const { router, mount } = setup('/external-sales/sale-1')
    const wrapper = await mount()
    await router.push('/external-sales/slow')
    await flushPromises()
    await router.push('/external-sales/fast')
    await flushPromises()
    expect(wrapper.find('input').element.value).toBe('fast-number')
    resolveSlow({ ...structuredClone(saved), id: 'slow', original_number: 'slow-number' })
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('fast')
    expect(wrapper.find('input').element.value).toBe('fast-number')
    api.put.mockResolvedValue(fast)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/external-sales/fast', expect.anything())
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => fast, blob: async () => new Blob(['original']) })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('confirm', () => true)
    vi.stubGlobal('URL', class extends URL { static createObjectURL = () => 'blob:test'; static revokeObjectURL = vi.fn() })
    const button = (text: string) => wrapper.findAll('button').find((row) => row.text() === text)!
    const picker = wrapper.find('input[type="file"]')
    Object.defineProperty(picker.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
    await picker.trigger('change')
    await button('Upload originals').trigger('click')
    await flushPromises()
    await button('Download').trigger('click')
    await flushPromises()
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/v1/external-sales/fast/files', '/api/v1/external-sales/fast/files/file-1'])
    api.del.mockResolvedValue(fast)
    await button('Remove file').trigger('click')
    await flushPromises()
    await button('Discard pending entry').trigger('click')
    await flushPromises()
    expect(api.del.mock.calls.map(([url]) => url)).toEqual(['/api/v1/external-sales/fast/files/file-1', '/api/v1/external-sales/fast'])
    wrapper.unmount()
  })

  it.each(['pending', 'failed'] as const)('blocks all old-record operations while the next detail is %s', async (state) => {
    let rejectNext!: (reason: Error) => void
    const next = new Promise<typeof saved>((_, reject) => { rejectNext = reject })
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/next' ? next : originalGet(url))
    const { router, mount } = setup('/external-sales/sale-1')
    const wrapper = await mount()
    const button = (text: string) => wrapper.findAll('button').find((row) => row.text() === text)!
    const oldButtons = ['Upload originals', 'Download', 'Remove file', 'Discard pending entry'].map(button)
    const picker = wrapper.find('input[type="file"]')
    Object.defineProperty(picker.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
    await picker.trigger('change')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('confirm', () => true)
    await router.push('/external-sales/next')
    await flushPromises()
    if (state === 'failed') { rejectNext(new Error('unavailable')); await flushPromises() }
    await wrapper.find('form').trigger('submit')
    for (const oldButton of oldButtons) await oldButton.trigger('click')
    await flushPromises()
    expect(api.put).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(api.del).not.toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('.file-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('does not apply an old detail to a new entry', async () => {
    let resolveOld!: (value: typeof saved) => void
    const old = new Promise<typeof saved>((resolve) => { resolveOld = resolve })
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/old' ? old : originalGet(url))
    const { router, mount } = setup('/external-sales/old')
    const wrapper = await mount()
    await router.push('/external-sales/new')
    await flushPromises()
    await wrapper.find('input').setValue('new-entry')
    resolveOld({ ...structuredClone(saved), id: 'old' })
    await flushPromises()
    expect(wrapper.find('input').element.value).toBe('new-entry')
    api.post.mockResolvedValue(saved)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/v1/external-sales', expect.objectContaining({ original_number: 'new-entry' }))
    expect(api.put).not.toHaveBeenCalled()
    wrapper.unmount()
  })


  it.each(['save', 'upload', 'remove', 'discard'] as const)('ignores a completed %s after navigation to another entry', async (action) => {
    let resolveMutation!: (value: typeof saved) => void
    const mutation = new Promise<typeof saved>((resolve) => { resolveMutation = resolve })
    const next = { ...structuredClone(saved), id: 'next', original_number: 'next-number', files: [{ ...saved.files[0], id: 'next-file', filename: 'next.pdf' }] }
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => url === '/api/v1/external-sales/next' ? Promise.resolve(structuredClone(next)) : originalGet(url))
    const { router, mount } = setup('/external-sales/sale-1')
    const wrapper = await mount()
    const button = (text: string) => wrapper.findAll('button').find((row) => row.text() === text)!
    if (action === 'save') {
      api.put.mockReturnValue(mutation)
      await wrapper.find('form').trigger('submit')
    } else if (action === 'upload') {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => mutation }))
      const picker = wrapper.find('input[type="file"]')
      Object.defineProperty(picker.element, 'files', { value: [new File(['%PDF-safe'], 'added.pdf', { type: 'application/pdf' })], configurable: true })
      await picker.trigger('change')
      await button('Upload originals').trigger('click')
    } else {
      api.del.mockReturnValue(mutation)
      vi.stubGlobal('confirm', () => true)
      await button(action === 'remove' ? 'Remove file' : 'Discard pending entry').trigger('click')
    }
    await flushPromises()
    await router.push('/external-sales/next')
    await flushPromises()
    await wrapper.find('input').setValue('unsaved-next-number')
    const picker = wrapper.find('input[type="file"]')
    Object.defineProperty(picker.element, 'files', { value: [new File(['%PDF-next'], 'next.pdf', { type: 'application/pdf' })], configurable: true })
    await picker.trigger('change')
    api.get.mockClear()
    resolveMutation({ ...structuredClone(saved), files: [] })
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('next')
    expect(wrapper.find('input').element.value).toBe('unsaved-next-number')
    expect(wrapper.find('.file-row').text()).toContain('next.pdf')
    expect(button('Upload originals').attributes('disabled')).toBeUndefined()
    expect(api.get).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps the newest detail loading when an older request fails', async () => {
    let rejectOld!: (reason: Error) => void
    let resolveNext!: (value: typeof saved) => void
    const old = new Promise<typeof saved>((_, reject) => { rejectOld = reject })
    const next = new Promise<typeof saved>((resolve) => { resolveNext = resolve })
    const originalGet = api.get.getMockImplementation()!
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/external-sales/old') return old
      if (url === '/api/v1/external-sales/next') return next
      return originalGet(url)
    })
    const { router, mount } = setup('/external-sales/old')
    const wrapper = await mount()
    await router.push('/external-sales/next')
    await flushPromises()
    rejectOld(new Error('old request failure'))
    await flushPromises()
    expect(wrapper.find('.error').exists()).toBe(false)
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    resolveNext({ ...structuredClone(saved), id: 'next', original_number: 'next-number' })
    await flushPromises()
    expect(wrapper.find('input').element.value).toBe('next-number')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })


  it('pages original candidates independently beyond 50 and keeps an existing off-page link readable', async () => {
    const originalGet = api.get.getMockImplementation()!
    const originals = Array.from({ length: 51 }, (_, i) => ({ ...saved, id: `original-${i}`, original_number: `ORIGINAL-${i}`, status: 'POSTED', document_kind: 'SALE' }))
    const credit = { ...saved, document_kind: 'CREDIT', related_external_sale_id: 'original-50' }
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/v1/external-sales/sale-1') return credit
      if (url === '/api/v1/external-sales/original-50') return originals[50]
      if (url.startsWith('/api/v1/external-sales?')) {
        const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'))
        return { items: originals.slice(offset, offset + 50), total: 51, offset, limit: 50 }
      }
      return originalGet(url)
    })
    const wrapper = await setup('/external-sales/sale-1').mount()
    expect(wrapper.get('[data-testid="related-original"]').text()).toContain('ORIGINAL-50')
    expect(wrapper.get('[data-testid="original-pagination"]').text()).toContain('1–50 of 51')
    await wrapper.get('[data-testid="original-pagination"]').findAll('button')[1].trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/external-sales?limit=50&offset=50')
    const select = wrapper.get('[data-testid="related-original"]')
    expect(select.findAll('option').map(row => row.attributes('value'))).toContain('original-50')
    await select.setValue('original-50')
    api.put.mockResolvedValue(credit)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/v1/external-sales/sale-1', expect.objectContaining({ related_external_sale_id: 'original-50' }))
    wrapper.unmount()
  })

  it.each(['SALE', 'SETTLEMENT_ADJUSTMENT'])('normalizes historical/known credit fields on mode changes to %s', async (kind) => {
    const originalGet = api.get.getMockImplementation()!
    const original = { ...saved, id: 'original-1', original_number: 'KNOWN-1', status: 'POSTED' }
    api.get.mockImplementation(async (url: string) => {
      if (url.startsWith('/api/v1/external-sales?')) return { items: [original], total: 1, offset: 0, limit: 50 }
      if (url === '/api/v1/external-sales/original-1') return original
      return originalGet(url)
    })
    const wrapper = await setup().mount()
    await wrapper.get('[data-testid="document-kind"]').setValue('CREDIT')
    await flushPromises()
    await wrapper.get('[data-testid="historical-number"]').setValue('OLD-17')
    await wrapper.get('[data-testid="historical-explanation"]').setValue('Before system')
    await wrapper.get('[data-testid="related-original"]').setValue('original-1')
    await flushPromises()
    expect(wrapper.find('[data-testid="historical-number"]').exists()).toBe(false)
    api.post.mockRejectedValue(new Error('Keep editor for payload assertion'))
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/external-sales', expect.objectContaining({ related_external_sale_id: 'original-1', historical_original_number: null, historical_original_explanation: null }))
    // Clearing known mode reveals empty historical fields, not stale hidden values.
    await wrapper.get('[data-testid="related-original"]').setValue('')
    expect((wrapper.get('[data-testid="historical-number"]').element as HTMLInputElement).value).toBe('')
    await wrapper.get('[data-testid="historical-number"]').setValue('OLD-18')
    await wrapper.get('[data-testid="historical-explanation"]').setValue('Another original')
    await wrapper.get('[data-testid="document-kind"]').setValue(kind)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/v1/external-sales', expect.objectContaining({ document_kind: kind, related_external_sale_id: null, historical_original_number: null, historical_original_explanation: null }))
    await wrapper.get('[data-testid="document-kind"]').setValue('CREDIT')
    expect((wrapper.get('[data-testid="historical-number"]').element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

})
