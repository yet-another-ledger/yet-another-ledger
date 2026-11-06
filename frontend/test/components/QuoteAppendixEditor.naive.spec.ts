import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { defineComponent, h } from 'vue'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const api = vi.hoisted(() => ({ get: vi.fn(), postFile: vi.fn(), fetchBlob: vi.fn(), saveBlob: vi.fn() }))
vi.mock('../../src/api/http', () => ({ ...api }))
vi.mock('naive-ui', async () => {
  const actual = await vi.importActual<typeof import('naive-ui')>('naive-ui')
  return actual
})
vi.mock('vuedraggable', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({
    props: ['modelValue'],
    setup(props, { slots }) {
      return () => h('div', props.modelValue.map((element: unknown, index: number) => slots.item?.({ element, index })))
    },
  }) }
})
vi.mock('../../src/components/PdfPreviewDialog.vue', () => ({ default: defineComponent({ template: '<div />' }) }))

import QuoteAppendixEditor from '../../src/components/QuoteAppendixEditor.vue'

const saved = {
  id: 'association-1', quote_id: 'quote-1', file_id: 'file-1', sort_order: 0, title_snapshot: 'Saved file',
  owner_quote_id: 'quote-1', source: 'QUOTE', title: 'Saved file', original_filename: 'saved.pdf',
  sha256: 'hash', size_bytes: 128, page_count: 1, is_archived: false,
}

describe('QuoteAppendixEditor with real Naive UI controls', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/limits')) return Promise.resolve({ max_appendix_file_bytes: 1024, max_quote_appendices: 10, max_quote_appendix_input_bytes: 2048, max_quote_pdf_bytes: 2048, max_quote_pdf_pages: 20 })
      return Promise.resolve({ items: [], total: 0 })
    })
  })

  it.each([
    ['en', 'Remove'],
    ['zh', '移除'],
  ])('renders the localized remove action in %s', async (locale, expected) => {
    const i18n = createI18n({ legacy: false, locale, messages: { en, zh } })
    const wrapper = mount(QuoteAppendixEditor, { props: {
      quoteId: 'quote-1', appendices: [saved] as never[], readonly: false, disabled: false,
    }, global: { plugins: [i18n] } })
    await flushPromises()

    expect(wrapper.find('button').text()).toBe('⠿')
    expect(wrapper.findAll('button').some(button => button.text() === expected)).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === 'common.remove')).toBe(false)
    wrapper.unmount()
  })
})
