import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(), http: vi.fn(), downloadBlob: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('../../src/api/http', () => ({ ...api }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string, values?: Record<string, unknown>) => values ? `${key} ${Object.values(values).join(' ')}` : key }) }))
vi.mock('../../src/components/PdfPreviewDialog.vue', () => ({ default: defineComponent({ name: 'PdfPreviewDialog', props: ['show', 'src', 'fallbackFilename'], template: '<div data-test="pdf-preview" :data-src="src" :data-filename="fallbackFilename" />' }) }))

vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const { NUpload } = await vi.importActual<typeof import('naive-ui')>('naive-ui')
  const passthrough = defineComponent({ inheritAttrs: false, template: '<div v-bind="$attrs"><slot /><slot name="footer" /><slot name="checked" /><slot name="unchecked" /></div>' })
  const button = defineComponent({ inheritAttrs: false, props: ['disabled', 'loading', 'title'], emits: ['click'], template: '<button v-bind="$attrs" :disabled="disabled || loading" :title="title" @click="$emit(\'click\')"><slot /><slot name="icon" /></button>' })
  const input = defineComponent({ inheritAttrs: false, props: ['value', 'disabled', 'placeholder'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" :disabled="disabled" :placeholder="placeholder" @input="$emit(\'update:value\',$event.target.value)" @keyup="$emit(\'keyup\',$event)" />' })
  const switchStub = defineComponent({ inheritAttrs: false, props: ['value', 'disabled'], emits: ['update:value'], template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'update:value\',!value)"><slot name="checked" /><slot name="unchecked" /></button>' })
  const tabPane = defineComponent({ props: ['tab'], template: '<section><span>{{ tab }}</span><slot /></section>' })
  const dataTable = defineComponent({
    props: ['data', 'columns', 'rowKey'],
    setup(props) { return () => h('div', props.data?.map((row: any) => h('div', { class: 'table-row', key: row.id }, [
      h('span', row.title ?? row.name ?? ''), h('span', row.original_filename ?? ''),
      ...(props.columns ?? []).flatMap((column: any) => {
        if (!column.render) return [h('span', String(row[column.key] ?? ''))]
        const rendered = column.render(row)
        if (rendered.children && typeof rendered.children.default === 'function') {
          const children = rendered.children.default()
          return (Array.isArray(children) ? children : [children]).map((node: any) => h('button', { title: node.props?.title, onClick: node.props?.onClick }, node.children?.default?.() ?? ''))
        }
        return [rendered]
      }),
    ]))) },
  })
  return {
    useMessage: () => messages,
    useDialog: () => ({ warning: vi.fn() }),
    NButton: button, NInput: input, NSelect: passthrough, NSwitch: switchStub, NTabs: passthrough, NTabPane: tabPane,
    NDataTable: dataTable, NSpin: passthrough, NModal: passthrough, NForm: passthrough, NFormItem: passthrough, NTag: passthrough,
    NText: passthrough, NInputNumber: passthrough, NCard: passthrough, NAlert: passthrough, NIcon: passthrough,
    NSpace: passthrough, NGrid: passthrough, NGi: passthrough, NUpload,
  }
})

import ContentLibrary from '../../src/views/content/ContentLibrary.vue'

const limits = { max_appendix_file_bytes: 1024, max_quote_appendices: 4, max_quote_appendix_input_bytes: 4096, max_quote_pdf_bytes: 4096, max_quote_pdf_pages: 20 }
const material = (patch: Record<string, unknown> = {}) => ({
  id: 'pdf-1', owner_quote_id: null, title: 'Brochure', original_filename: '产品资料.pdf', sha256: 'abc', size_bytes: 512,
  page_count: 3, is_archived: false, created_at: '2026-11-04T00:00:00Z', updated_at: '2026-11-04T00:00:00Z', source: 'LIBRARY', ...patch,
})

function mountLibrary() {
  return mount(ContentLibrary)
}

describe('Content Library PDF Materials', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [material()], total: 42 })
      return Promise.resolve([])
    })
    api.http.mockResolvedValue(material())
    api.patch.mockResolvedValue(material())
  })

  it('debounces live title search, clears it immediately, and keeps paging and archive filters', async () => {
    const wrapper = mountLibrary()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/v1/pdf-library/limits')
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('include_archived=false'))
    expect(api.get).toHaveBeenCalledWith('/api/v1/document-templates')
    expect(api.get).toHaveBeenCalledWith('/api/v1/content-blocks')
    expect(api.get).toHaveBeenCalledWith('/api/v1/note-templates')
    expect(wrapper.text()).toContain('产品资料.pdf')
    expect(wrapper.text()).toContain('3')
    const search = wrapper.find('input[placeholder="contentLibrary.pdfMaterials.search"]')
    const beforeSearch = api.get.mock.calls.length
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      await search.setValue('data')
      await vi.advanceTimersByTimeAsync(200)
      await search.setValue('  datasheet  ')
      await vi.advanceTimersByTimeAsync(299)
      expect(api.get).toHaveBeenCalledTimes(beforeSearch)
      await vi.advanceTimersByTimeAsync(1)
      expect(api.get).toHaveBeenCalledTimes(beforeSearch + 1)
    } finally {
      vi.useRealTimers()
    }
    expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining('search=datasheet'))
    await wrapper.findAll('button').find(b => b.text() === 'contentLibrary.pdfMaterials.next')!.trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining('offset=20'))
    await search.setValue('')
    await flushPromises()
    expect(api.get).toHaveBeenLastCalledWith('/api/v1/pdf-library?limit=20&offset=0&include_archived=false')
    await wrapper.get('button[aria-label="contentLibrary.pdfMaterials.showArchived"]').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining('include_archived=true'))
    wrapper.unmount()
  })

  it('uploads one multipart PDF with an optional title, blocks duplicate clicks, and retries after failure', async () => {
    let finish!: (result: unknown) => void
    api.http.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = mountLibrary()
    await flushPromises()
    const inputFile = wrapper.find('.pdf-upload-card input[type=file]')
    const file = new File(['%PDF-x'], '资料.pdf', { type: 'application/pdf' })
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [file] })
    await inputFile.trigger('change')
    await flushPromises()
    const first = new File(['%PDF-first'], 'first.pdf', { type: 'application/pdf' })
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [first] })
    await inputFile.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('first.pdf')
    expect(wrapper.text()).not.toContain('资料.pdf ·')
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [file] })
    await inputFile.trigger('change')
    await flushPromises()
    expect(api.http).not.toHaveBeenCalled()
    await wrapper.find('input[placeholder="contentLibrary.pdfMaterials.titleFromFilename"]').setValue('安装手册')
    const upload = wrapper.findAll('button').find(b => b.text().includes('contentLibrary.pdfMaterials.upload'))!
    await upload.trigger('click')
    await upload.trigger('click')
    expect(api.http).toHaveBeenCalledTimes(1)
    const form = api.http.mock.calls[0][1].body as FormData
    expect((form.get('file') as File).name).toBe(file.name)
    expect(form.get('title')).toBe('安装手册')
    expect(api.http.mock.calls[0][1].headers).toBeUndefined()
    finish(material())
    await flushPromises()
    expect(messages.success).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('资料.pdf ·')

    api.http.mockRejectedValueOnce(new Error('upload rejected'))
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [file] })
    await inputFile.trigger('change')
    await flushPromises()
    await upload.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('upload rejected')
    await wrapper.findAll('button').find(b => b.text().includes('common.retry'))!.trigger('click')
    await flushPromises()
    expect(api.http).toHaveBeenCalledTimes(3)
    const replacement = new File(['%PDF-replacement'], 'replacement.pdf', { type: 'application/pdf' })
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [replacement] })
    await inputFile.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('replacement.pdf')
    expect(api.http).toHaveBeenCalledTimes(3)
    const oversized = new File(['x'.repeat(1025)], 'large.pdf', { type: 'application/pdf' })
    Object.defineProperty(inputFile.element, 'files', { configurable: true, value: [oversized] })
    await inputFile.trigger('change')
    await flushPromises()
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.fileTooLarge')
    expect(wrapper.findAll('button').find(b => b.text().includes('contentLibrary.pdfMaterials.upload'))!.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('renames, archives, restores, and presents original metadata without deleting files', async () => {
    let archived = false
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [material({ is_archived: archived })], total: 1 })
      return Promise.resolve([])
    })
    api.patch.mockImplementation((_url: string, body: Record<string, unknown>) => {
      if (typeof body.is_archived === 'boolean') archived = body.is_archived
      return Promise.resolve(material({ is_archived: archived, title: body.title ?? 'Brochure' }))
    })
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.findAll('button').find(b => b.attributes('title') === 'contentLibrary.pdfMaterials.rename')!.trigger('click')
    await wrapper.findAll('input').find(x => (x.element as HTMLInputElement).value === 'Brochure')!.setValue('New brochure')
    await wrapper.findAll('button').filter(b => b.text() === 'contentLibrary.save').at(-1)!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenCalledWith('/api/v1/pdf-library/pdf-1', { title: 'New brochure' })
    await wrapper.findAll('button').find(b => b.attributes('title') === 'contentLibrary.pdfMaterials.archive')!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenCalledWith('/api/v1/pdf-library/pdf-1', { is_archived: true })
    await wrapper.findAll('button').find(b => b.attributes('title') === 'contentLibrary.pdfMaterials.restore')!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenCalledWith('/api/v1/pdf-library/pdf-1', { is_archived: false })
    await wrapper.findAll('button').find(b => b.attributes('title') === 'contentLibrary.pdfMaterials.preview')!.trigger('click')
    expect(wrapper.find('[data-test="pdf-preview"]').attributes('data-src')).toBe('/api/v1/pdf-files/pdf-1/content?disposition=inline')
    await wrapper.findAll('button').find(b => b.attributes('title') === 'contentLibrary.pdfMaterials.download')!.trigger('click')
    await flushPromises()
    expect(api.downloadBlob).toHaveBeenCalledWith('/api/v1/pdf-files/pdf-1/content?disposition=attachment', '产品资料.pdf')
    wrapper.unmount()
  })

  it('never guesses limits after their request fails and retains the three existing tabs', async () => {
    api.get.mockImplementation((url: string) => url === '/api/v1/pdf-library/limits' ? api.get.mock.calls.filter(([callUrl]) => callUrl === url).length === 1 ? Promise.reject(new Error('offline')) : Promise.resolve(limits) : url.startsWith('/api/v1/pdf-library?') ? Promise.resolve({ items: [], total: 0 }) : Promise.resolve([]))
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.limitsFailed')
    expect(wrapper.findAll('button').find(b => b.text() === 'contentLibrary.pdfMaterials.chooseFile')!.attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find(b => b.text() === 'common.retry')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.limits 1.0 KB 4 4.0 KB 4.0 KB 20')
    expect(wrapper.findAll('button').find(b => b.text() === 'contentLibrary.pdfMaterials.chooseFile')!.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('contentLibrary.template.tabTitle')
    expect(wrapper.text()).toContain('contentLibrary.block.tabTitle')
    expect(wrapper.text()).toContain('contentLibrary.note.tabTitle')
    wrapper.unmount()
  })

  it('ignores an older title search result during the debounce for newer input', async () => {
    let finishOld!: (value: unknown) => void
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) {
        if (url.includes('search=old')) return new Promise(resolve => { finishOld = resolve })
        if (url.includes('search=new')) return Promise.resolve({ items: [material({ id: 'new-file', title: 'New result' })], total: 1 })
        return Promise.resolve({ items: [], total: 0 })
      }
      return Promise.resolve([])
    })
    const wrapper = mountLibrary()
    await flushPromises()
    const search = wrapper.find('input[placeholder="contentLibrary.pdfMaterials.search"]')
    await search.setValue('old')
    await search.trigger('keyup', { key: 'Enter' })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      await search.setValue('new')
      finishOld({ items: [material({ id: 'old-file', title: 'Old result' })], total: 1 })
      await flushPromises()
      expect(wrapper.text()).not.toContain('Old result')
      expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('search=new'))
      await vi.advanceTimersByTimeAsync(300)
      expect(wrapper.text()).toContain('New result')
    } finally {
      vi.useRealTimers()
    }
    wrapper.unmount()
  })

  it('returns to the last populated page after archiving the only row on page 2 of 21', async () => {
    let archived = false
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) {
        const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'))
        const total = archived ? 20 : 21
        return Promise.resolve({
          items: offset === 20 && !archived ? [material({ id: 'last-file', title: 'Last file' })]
            : offset === 0 ? Array.from({ length: 20 }, (_, index) => material({ id: `file-${index}` }))
              : [],
          total,
        })
      }
      return Promise.resolve([])
    })
    api.patch.mockImplementation((_url: string, body: Record<string, unknown>) => {
      archived = body.is_archived === true
      return Promise.resolve(material({ id: 'last-file', is_archived: archived }))
    })

    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'contentLibrary.pdfMaterials.next')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.page 2 2 21')
    await wrapper.findAll('button').find(button => button.attributes('title') === 'contentLibrary.pdfMaterials.archive')!.trigger('click')
    await flushPromises()

    const listCalls = api.get.mock.calls.map(([url]) => String(url)).filter(url => url.startsWith('/api/v1/pdf-library?'))
    expect(listCalls.slice(-2).map(url => new URL(url, 'http://localhost').searchParams.get('offset'))).toEqual(['20', '0'])
    expect(wrapper.findAll('.table-row')).toHaveLength(20)
    expect(wrapper.text()).not.toContain('contentLibrary.pdfMaterials.page 2')
    expect(wrapper.text()).not.toContain('contentLibrary.pdfMaterials.empty')
    wrapper.unmount()
  })

  it('corrects a vanished filtered last page to offset zero without clearing the title filter', async () => {
    let renamedAway = false
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) {
        const params = new URL(url, 'http://localhost').searchParams
        const offset = Number(params.get('offset'))
        const search = params.get('search')
        if (search !== 'needle') return Promise.resolve({ items: [], total: 0 })
        if (renamedAway) return Promise.resolve({ items: [], total: 0 })
        if (offset === 20) return Promise.resolve({ items: [material({ id: 'needle-last', title: 'needle last' })], total: 21 })
        if (offset === 0) return Promise.resolve({ items: Array.from({ length: 20 }, (_, index) => material({ id: `needle-${index}` })), total: 21 })
        return Promise.resolve({ items: [], total: 0 })
      }
      return Promise.resolve([])
    })
    api.patch.mockImplementation(() => {
      renamedAway = true
      return Promise.resolve(material({ id: 'needle-last', title: 'renamed away' }))
    })

    const wrapper = mountLibrary()
    await flushPromises()
    const search = wrapper.find('input[placeholder="contentLibrary.pdfMaterials.search"]')
    await search.setValue('needle')
    await search.trigger('keyup', { key: 'Enter' })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'contentLibrary.pdfMaterials.next')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.attributes('title') === 'contentLibrary.pdfMaterials.rename')!.trigger('click')
    await wrapper.findAll('input').at(-1)!.setValue('renamed away')
    await wrapper.findAll('button').filter(button => button.text() === 'contentLibrary.save').at(-1)!.trigger('click')
    await flushPromises()

    const listCalls = api.get.mock.calls.map(([url]) => String(url)).filter(url => url.startsWith('/api/v1/pdf-library?'))
    const recentParams = listCalls.slice(-2).map(url => new URL(url, 'http://localhost').searchParams)
    expect(recentParams.map(params => params.get('offset'))).toEqual(['20', '0'])
    expect(recentParams.every(params => params.get('search') === 'needle')).toBe(true)
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.noResults')
    expect(wrapper.text()).not.toContain('contentLibrary.pdfMaterials.empty')
    wrapper.unmount()
  })

  it('does not correct from an expired page response and keeps a valid final page', async () => {
    let finishExpired!: (result: unknown) => void
    api.get.mockImplementation((url: string) => {
      if (url === '/api/v1/pdf-library/limits') return Promise.resolve(limits)
      if (url.startsWith('/api/v1/pdf-library?')) {
        const params = new URL(url, 'http://localhost').searchParams
        if (params.get('search') === 'expired') return new Promise(resolve => { finishExpired = resolve })
        if (params.get('search') === 'current') return Promise.resolve({ items: [material({ id: 'current', title: 'Current result' })], total: 1 })
        return Promise.resolve({ items: Array.from({ length: 20 }, (_, index) => material({ id: `page-${index}` })), total: 40 })
      }
      return Promise.resolve([])
    })
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'contentLibrary.pdfMaterials.next')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('contentLibrary.pdfMaterials.page 2 2 40')

    const search = wrapper.find('input[placeholder="contentLibrary.pdfMaterials.search"]')
    await search.setValue('expired')
    await search.trigger('keyup', { key: 'Enter' })
    await search.setValue('current')
    await search.trigger('keyup', { key: 'Enter' })
    await flushPromises()
    const callCountBeforeExpiredResponse = api.get.mock.calls.length
    finishExpired({ items: [], total: 0 })
    await flushPromises()

    expect(api.get).toHaveBeenCalledTimes(callCountBeforeExpiredResponse)
    expect(wrapper.text()).toContain('Current result')
    expect(wrapper.text()).not.toContain('contentLibrary.pdfMaterials.page 1 1 0')
    wrapper.unmount()
  })
})
