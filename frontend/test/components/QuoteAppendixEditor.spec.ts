import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'

const api = vi.hoisted(() => ({ get: vi.fn(), postFile: vi.fn(), fetchBlob: vi.fn(), saveBlob: vi.fn() }))
vi.mock('../../src/api/http', () => ({ ...api }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string, values?: Record<string, unknown>) => values ? `${key} ${Object.values(values).join(' ')}` : key }) }))
vi.mock('../../src/components/PdfPreviewDialog.vue', () => ({ default: defineComponent({ props: ['show', 'src', 'fallbackFilename'], template: '<div data-test="preview" :data-src="src" :data-filename="fallbackFilename" />' }) }))
vi.mock('vuedraggable', async () => {
  const { defineComponent } = await import('vue')
  return { default: defineComponent({
    props: ['modelValue', 'disabled'], emits: ['update:model-value'],
    template: '<div><button data-test="reverse" :disabled="disabled" @click="$emit(\'update:model-value\', [...modelValue].reverse())">reverse</button><slot v-for="(element,index) in modelValue" name="item" :element="element" :index="index" /></div>',
  }) }
})
vi.mock('naive-ui', async () => {
  const { defineComponent } = await import('vue')
  const box = defineComponent({ inheritAttrs: false, template: '<div v-bind="$attrs"><slot /></div>' })
  const button = defineComponent({ inheritAttrs: false, props: ['disabled', 'size', 'type'], emits: ['click'], template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>' })
  const input = defineComponent({ inheritAttrs: false, props: ['value', 'disabled', 'placeholder'], emits: ['update:value'], template: '<input v-bind="$attrs" :value="value" :disabled="disabled" :placeholder="placeholder" @input="$emit(\'update:value\',$event.target.value)" />' })
  const listItem = defineComponent({ template: '<div><slot /><slot name="suffix" /></div>' })
  const thing = defineComponent({ props: ['title', 'description'], template: '<div><b>{{ title }}</b><span>{{ description }}</span></div>' })
  return { NAlert: box, NButton: button, NCard: box, NDivider: box, NEmpty: box, NGi: box, NGrid: box, NInput: input, NList: box, NListItem: listItem, NSpace: box, NSpin: box, NTag: box, NThing: thing }
})

import QuoteAppendixEditor from '../../src/components/QuoteAppendixEditor.vue'

const limits = { max_appendix_file_bytes: 1024, max_quote_appendices: 3, max_quote_appendix_input_bytes: 1536, max_quote_pdf_bytes: 2048, max_quote_pdf_pages: 20 }
const file = (id: string, source: 'LIBRARY' | 'QUOTE', patch: Record<string, unknown> = {}) => ({
  id, owner_quote_id: source === 'QUOTE' ? 'quote-a' : null, source, title: `Title ${id}`, original_filename: `${id}.pdf`,
  sha256: `${id}-hash`, size_bytes: 512, page_count: 2, is_archived: false,
  created_at: '2026-11-04T00:00:00Z', updated_at: '2026-11-04T00:00:00Z', ...patch,
})
const saved = (entry: ReturnType<typeof file>, title = entry.title) => ({
  ...entry, id: `association-${entry.id}`, file_id: entry.id, sort_order: 0, title_snapshot: title,
})
const limitsResponse = () => ({ ...limits })

function setup(appendices: unknown[] = [], getHandler?: (url: string) => Promise<unknown>) {
  api.get.mockImplementation(getHandler ?? ((url: string) => {
    if (url.endsWith('/limits')) return Promise.resolve(limitsResponse())
    if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [file('library', 'LIBRARY')], total: 1 })
    if (url.startsWith('/api/v1/quotes/quote-a/appendix-files')) return Promise.resolve({ items: [file('own', 'QUOTE')], total: 1 })
    return Promise.resolve({ items: [], total: 0 })
  }))
  return mount(QuoteAppendixEditor, { props: { quoteId: 'quote-a', appendices: appendices as never[], readonly: false, disabled: false } })
}

describe('Quote appendix editor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.postFile.mockResolvedValue(file('own', 'QUOTE'))
    api.fetchBlob.mockResolvedValue({ blob: new Blob(['%PDF']), filename: 'own.pdf' })
  })

  it('mixes library and Quote files locally, deduplicates, sorts, removes, and retains an archived saved snapshot', async () => {
    const archived = saved(file('archived', 'LIBRARY', { is_archived: true }), 'Original saved title')
    const wrapper = setup([archived])
    await flushPromises()
    expect(wrapper.text()).toContain('Original saved title')
    expect(wrapper.text()).toContain('quotes.appendices.archived')
    expect(wrapper.text()).toContain('quotes.appendices.limits 1 3 1 KB 2 KB 1 KB 2 20 2 KB')

    const addButtons = wrapper.findAll('button').filter(button => button.text() === 'quotes.appendices.add')
    await addButtons[0]!.trigger('click')
    await addButtons[1]!.trigger('click')
    expect(wrapper.emitted('change')!.at(-1)![0]).toEqual(expect.arrayContaining([
      expect.objectContaining({ file_id: 'archived', title_snapshot: 'Original saved title' }),
      expect.objectContaining({ file_id: 'library', source: 'LIBRARY' }),
      expect.objectContaining({ file_id: 'own', source: 'QUOTE' }),
    ]))
    expect(new Set((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[]).map(item => item.file_id)).size).toBe(3)
    await wrapper.find('[data-test="reverse"]').trigger('click')
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[]).map(item => item.file_id)).toEqual(['own', 'library', 'archived'])
    const remove = wrapper.findAll('button').find(button => button.text() === 'quotes.appendices.remove')!
    await remove.trigger('click')
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[]).map(item => item.file_id)).toHaveLength(2)
    wrapper.unmount()
  })

  it('adds an upload to the local pending selection only, then allows reselection after discard', async () => {
    const wrapper = setup()
    await flushPromises()
    const input = wrapper.find('input[type="file"]')
    const pdf = new File(['%PDF'], 'own.pdf', { type: 'application/pdf' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [pdf] })
    await input.trigger('change')
    await flushPromises()
    expect(api.postFile).toHaveBeenCalledTimes(1)
    expect(api.postFile).toHaveBeenCalledWith('/api/v1/quotes/quote-a/appendix-files', pdf)
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[]).map(item => item.file_id)).toEqual(['own'])
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/quotes/quote-a/appendix-files'))

    await wrapper.findAll('button').filter(button => button.text() === 'quotes.appendices.remove').at(-1)!.trigger('click')
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[])).toEqual([])
    await wrapper.findAll('button').filter(button => button.text() === 'quotes.appendices.add').at(-1)!.trigger('click')
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[])[0]?.file_id).toBe('own')
    const previewButtons = wrapper.findAll('button').filter(button => button.text() === 'pdf.preview')
    await previewButtons.at(-1)!.trigger('click')
    expect(wrapper.find('[data-test="preview"]').attributes('data-src')).toContain('/pdf-files/own/content?disposition=inline')
    const downloadButtons = wrapper.findAll('button').filter(button => button.text() === 'pdf.download')
    await downloadButtons.at(-1)!.trigger('click')
    await flushPromises()
    expect(api.fetchBlob).toHaveBeenCalledWith('/api/v1/pdf-files/own/content?disposition=attachment', 'own.pdf')
    expect(api.saveBlob).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps accepted Quotes read-only while still allowing original preview and download', async () => {
    const wrapper = mount(QuoteAppendixEditor, { props: {
      quoteId: 'quote-a', appendices: [saved(file('own', 'QUOTE'))] as never[], readonly: true, disabled: false,
    } })
    await flushPromises()
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="reverse"]').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').filter(button => button.text() === 'quotes.appendices.remove')).toHaveLength(0)
    expect(wrapper.findAll('button').filter(button => button.text() === 'pdf.preview').length).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('retries a failed limits read and never enables upload while the configured limits are unknown', async () => {
    const wrapper = setup([], (url: string) => {
      if (url.endsWith('/limits')) return api.get.mock.calls.filter(([callUrl]) => callUrl === url).length === 1
        ? Promise.reject(new Error('limits offline')) : Promise.resolve(limitsResponse())
      if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({ items: [], total: 0 })
    })
    await flushPromises()
    expect(wrapper.text()).toContain('limits offline')
    expect(wrapper.find('input[type="file"]').attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find(button => button.text() === 'quotes.appendices.retry')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('input[type="file"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('quotes.appendices.limits')
    wrapper.unmount()
  })

  it('ignores a late upload response after switching Quotes', async () => {
    let finishUpload!: (value: ReturnType<typeof file>) => void
    api.postFile.mockReturnValue(new Promise(resolve => { finishUpload = resolve }))
    const wrapper = setup()
    await flushPromises()
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/limits')) return Promise.resolve(limitsResponse())
      if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [], total: 0 })
      if (url.startsWith('/api/v1/quotes/quote-a/appendix-files')) return Promise.resolve({ items: [], total: 0 })
      if (url.startsWith('/api/v1/quotes/quote-b/appendix-files')) return Promise.resolve({ items: [file('file-b', 'QUOTE')], total: 1 })
      return Promise.resolve({ items: [], total: 0 })
    })
    const input = wrapper.find('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [new File(['%PDF'], 'late.pdf', { type: 'application/pdf' })] })
    await input.trigger('change')
    await wrapper.setProps({ quoteId: 'quote-b' })
    await flushPromises()
    finishUpload(file('file-a-late', 'QUOTE'))
    await flushPromises()
    expect(wrapper.text()).toContain('file-b.pdf')
    expect(wrapper.text()).not.toContain('file-a-late.pdf')
    expect((wrapper.emitted('change')!.at(-1)![0] as { file_id: string }[])).not.toEqual(expect.arrayContaining([expect.objectContaining({ file_id: 'file-a-late' })]))
    wrapper.unmount()
  })

  it('clears Quote-specific picker entries immediately on route reuse and leaves them unavailable after load failure', async () => {
    let failQuoteB = false
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/limits')) return Promise.resolve(limitsResponse())
      if (url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [], total: 0 })
      if (url.startsWith('/api/v1/quotes/quote-a/appendix-files')) return Promise.resolve({ items: [file('own-a', 'QUOTE')], total: 1 })
      if (url.startsWith('/api/v1/quotes/quote-b/appendix-files')) return failQuoteB
        ? Promise.reject(new Error('quote B files unavailable'))
        : Promise.resolve({ items: [file('own-b', 'QUOTE', { owner_quote_id: 'quote-b' })], total: 1 })
      return Promise.resolve({ items: [], total: 0 })
    })
    const wrapper = setup()
    await flushPromises()
    expect(wrapper.text()).toContain('own.pdf')
    failQuoteB = true
    await wrapper.setProps({ quoteId: 'quote-b' })
    expect(wrapper.text()).not.toContain('own.pdf')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/quotes/quote-b/appendix-files'))
    expect(wrapper.text()).not.toContain('own.pdf')
    expect(wrapper.findAll('button').filter(button => button.text() === 'quotes.appendices.add')).toHaveLength(1)
    wrapper.unmount()
  })

  it('renders the correct bilingual remove translation key', async () => {
    const wrapper = setup([saved(file('own', 'QUOTE'))])
    await flushPromises()
    expect(wrapper.findAll('button').some(button => button.text() === 'quotes.appendices.remove')).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === 'common.remove')).toBe(false)
    wrapper.unmount()
  })
})
