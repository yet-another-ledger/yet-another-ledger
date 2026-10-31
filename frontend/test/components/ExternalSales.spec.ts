import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }))
vi.mock('../../src/api/http', () => api)
vi.mock('naive-ui', () => {
  const box = { inheritAttrs: false, template: '<div v-bind="$attrs"><slot /></div>' }
  const button = { inheritAttrs: false, props: ['disabled', 'type', 'attrType'], template: '<button v-bind="$attrs" :disabled="disabled" :type="attrType || \'button\'"><slot /></button>' }
  const input = { inheritAttrs: false, props: ['value', 'type', 'placeholder'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" :placeholder="placeholder" @input="$emit(\'update:value\',$event.target.value)" />' }
  const select = { inheritAttrs: false, props: ['value', 'options', 'placeholder'], emits: ['update:value'], template: '<select v-bind="$attrs" :value="value" @change="$emit(\'update:value\',$event.target.value)"><option value=""></option><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>' }
  return {
    NButton: button, NCard: box, NCheckbox: { props: ['checked'], template: '<input type="checkbox" />' },
    NForm: { inheritAttrs: false, template: '<form v-bind="$attrs"><slot /></form>' }, NFormItem: box,
    NInput: input, NModal: box, NSelect: select, useMessage: () => ({ success: vi.fn(), error: vi.fn() }),
  }
})

import en from '../../src/locales/en.json'
import ExternalSales from '../../src/views/externalSales/ExternalSales.vue'

const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })
const source = { id: 'source-platform', name: 'Platform', kind: 'PLATFORM', default_include_in_pl: true, active: true }
const treatment = { id: 'treatment-domestic', code: 'NL_DOMESTIC', label: 'Domestic sales', side: 'SALES', effect: 'APPLY_RATE', requires_icp: false, active: true }
const rates = [{ id: 'rate-21', label: 'High', percent: '21', active: true }, { id: 'rate-9', label: 'Low', percent: '9', active: true }]

const saved = { id: 'sale-1', source_id: source.id, source_name: source.name, original_number: 'saved-number', document_date: '2026-01-02', covered_from: null, covered_to: null, counterparty: 'saved counterparty', description: 'saved description', include_in_pl: true, status: 'PENDING', tax_summaries: [{ treatment_id: treatment.id, rate_id: rates[0].id, net_amount: '100.00', vat_amount: '21.00' }], files: [{ id: 'file-1', filename: 'original.pdf', mime_type: 'application/pdf', byte_size: 9, sha256: '', duplicate_warning: false, created_at: '' }], created_at: '', updated_at: '' }

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
    await wrapper.findAll('select')[2].setValue(treatment.id)
    await wrapper.findAll('select')[3].setValue(rates[0].id)
    await wrapper.findAll('input[placeholder="Original net amount"]')[0].setValue('100.005')
    await wrapper.findAll('input[placeholder="Original VAT amount"]')[0].setValue('21.005')
    await wrapper.findAll('button').find((button) => button.text() === 'Add tax row')!.trigger('click')
    await wrapper.findAll('select')[4].setValue(treatment.id)
    await wrapper.findAll('select')[5].setValue(rates[1].id)
    await wrapper.findAll('input[placeholder="Original net amount"]')[1].setValue('50')
    await wrapper.findAll('input[placeholder="Original VAT amount"]')[1].setValue('4.5')
    const created = { id: 'sale-1', source_id: source.id, source_name: source.name, original_number: '  EXT 017  ', document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: true, status: 'PENDING', tax_summaries: [], files: [], created_at: '', updated_at: '' }
    api.post.mockResolvedValue(created)
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/external-sales/sale-1') return Promise.resolve(created)
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

  it.each(['upload', 'remove'] as const)('preserves all unsaved fields after an original %s', async (action) => {
    const wrapper = await setup('/external-sales/sale-1').mount()
    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('unsaved-corrected-number')
    await inputs[1].setValue('2026-02-03')
    await inputs[2].setValue('new counterparty')
    await inputs[3].setValue('2026-01-01')
    await inputs[4].setValue('2026-01-31')
    await inputs[5].setValue('unsaved description')
    await wrapper.findAll('select')[1].setValue('false')
    await wrapper.findAll('select')[3].setValue(rates[1].id)
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
      source_id: source.id, original_number: 'unsaved-corrected-number', document_date: '2026-02-03',
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
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:test', revokeObjectURL: vi.fn() })
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

})
