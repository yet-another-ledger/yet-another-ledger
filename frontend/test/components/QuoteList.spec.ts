import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const store = vi.hoisted(() => ({
  items: [{ id: 'source', status: 'ACCEPTED', quote_number: 'Q1', currency: 'EUR', total_incl_vat: '100' }],
  total: 1, limit: 50, offset: 0, loading: false,
  fetchQuotes: vi.fn(), duplicateQuote: vi.fn(),
}))
const errors = vi.hoisted(() => vi.fn())
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => store }))
vi.mock('../../src/api/http', () => ({ get: vi.fn().mockResolvedValue({ items: [] }), downloadBlob: vi.fn() }))
vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const box = { template: '<div><slot /></div>' }
  const button = { template: '<button><slot /></button>' }
  return {
    useMessage: () => ({ success: vi.fn(), error: errors }), useDialog: () => ({}),
    NButton: button, NSpace: box, NInput: box, NAlert: box, NSpin: box, NPagination: box,
    NSelect: box, NTag: box, NIcon: box, NEmpty: box,
    NDropdown: { template: '<div />' },
    NDataTable: defineComponent({
      props: ['columns', 'data'],
      setup: props => () => h('div', props.data.map((row: unknown) => props.columns.find((col: {key: string}) => col.key === 'actions').render(row))),
    }),
  }
})
import QuoteList from '../../src/views/quotes/QuoteList.vue'

describe('Quote list Duplicate action', () => {
  it('copies saved source, prevents repeated clicks and opens new editor', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/quotes', component: QuoteList },
      { path: '/quotes/:id/edit', component: { template: '<div />' } },
    ] })
    await router.push('/quotes'); await router.isReady()
    let finish!: (value: {id: string}) => void
    store.duplicateQuote.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(QuoteList, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { DocumentSendDialog: true, PdfPreviewDialog: true } } })
    await flushPromises()
    const duplicate = wrapper.findAll('button').find(button => button.text() === 'Duplicate')!
    await duplicate.trigger('click'); await duplicate.trigger('click')
    expect(store.duplicateQuote).toHaveBeenCalledTimes(1)
    expect(store.duplicateQuote).toHaveBeenCalledWith('source')
    finish({ id: 'copy' }); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/quotes/copy/edit')
    wrapper.unmount()
  })

  it('shows duplicate failure and stays on the list', async () => {
    store.duplicateQuote.mockRejectedValueOnce(new Error('unavailable'))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes', component: QuoteList }] })
    await router.push('/quotes'); await router.isReady()
    const wrapper = mount(QuoteList, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { DocumentSendDialog: true, PdfPreviewDialog: true } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'Duplicate')!.trigger('click')
    await flushPromises()
    expect(errors).toHaveBeenCalledWith('unavailable')
    expect(router.currentRoute.value.path).toBe('/quotes')
    wrapper.unmount()
  })
})
