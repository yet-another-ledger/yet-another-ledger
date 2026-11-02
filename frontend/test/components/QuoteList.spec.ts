import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'
import en from '../../src/locales/en.json'

const store = vi.hoisted(() => ({
  items: [{ id: 'source', status: 'ACCEPTED', quote_number: 'Q1', currency: 'EUR', total_incl_vat: '100' }],
  total: 1, limit: 50, offset: 0, loading: false,
  fetchQuotes: vi.fn(), duplicateQuote: vi.fn(), deleteQuote: vi.fn(),
}))
const errors = vi.hoisted(() => vi.fn())
const confirmDelete = vi.hoisted(() => vi.fn())
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => store }))
vi.mock('../../src/api/http', () => ({ get: vi.fn().mockResolvedValue({ items: [] }), downloadBlob: vi.fn() }))
vi.mock('naive-ui', async () => {
  const { defineComponent, h } = await import('vue')
  const box = { template: '<div><slot /></div>' }
  const button = { template: '<button><slot name="icon" /><slot /></button>' }
  return {
    useMessage: () => ({ success: vi.fn(), error: errors }), useDialog: () => ({ warning: confirmDelete }),
    NButton: button, NSpace: box, NInput: box, NAlert: box, NSpin: box, NPagination: box,
    NSelect: box, NTag: box, NIcon: box, NEmpty: box,
    NDropdown: box,
    NDataTable: defineComponent({
      name: 'NDataTable',
      props: ['columns', 'data'],
      setup: props => () => h('div', props.data.map((row: {id: string}) => h('div', { 'data-row': row.id }, props.columns.find((col: {key: string}) => col.key === 'actions').render(row)))),
    }),
  }
})
import QuoteList from '../../src/views/quotes/QuoteList.vue'

describe('Quote list Duplicate action', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    store.items = [{ id: 'source', status: 'ACCEPTED', quote_number: 'Q1', currency: 'EUR', total_incl_vat: '100' }]
    store.total = 1
  })

  it('copies saved source, prevents repeated clicks and opens new editor', async () => {
    store.items.push({ id: 'other', status: 'DRAFT', quote_number: 'Q2', currency: 'EUR', total_incl_vat: '200' })
    store.total = 2
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/quotes', component: QuoteList },
      { path: '/quotes/:id/edit', component: { template: '<div />' } },
    ] })
    await router.push('/quotes'); await router.isReady()
    let finish!: (value: {id: string}) => void
    store.duplicateQuote.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(QuoteList, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { DocumentSendDialog: true, PdfPreviewDialog: true } } })
    await flushPromises()
    const duplicate = wrapper.get('[data-row="source"] button[aria-label="Duplicate"]')
    expect(duplicate.text()).toBe('')
    expect(duplicate.find('svg').exists()).toBe(true)
    await duplicate.trigger('click'); await duplicate.trigger('click')
    const otherDuplicate = wrapper.get('[data-row="other"] button[aria-label="Duplicate"]')
    expect(duplicate.attributes('loading')).toBe('true')
    expect(duplicate.attributes('disabled')).toBeDefined()
    expect(otherDuplicate.attributes('disabled')).toBeDefined()
    await otherDuplicate.trigger('click')
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
    await wrapper.get('button[aria-label="Duplicate"]').trigger('click')
    await flushPromises()
    expect(errors).toHaveBeenCalledWith('unavailable')
    expect(router.currentRoute.value.path).toBe('/quotes')
    expect(wrapper.get('button[aria-label="Duplicate"]').attributes('disabled')).toBeUndefined()
    store.duplicateQuote.mockRejectedValueOnce(null)
    await wrapper.get('button[aria-label="Duplicate"]').trigger('click')
    await flushPromises()
    expect(store.duplicateQuote).toHaveBeenCalledTimes(2)
    expect(errors).toHaveBeenLastCalledWith(en.quotes.duplicateFailed)
    expect(wrapper.get('button[aria-label="Duplicate"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps six left-aligned icon positions for every status and disables only protected deletion', async () => {
    store.items = ['DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'EXPIRED'].map(status => ({
      id: status, status, quote_number: status, currency: 'EUR', total_incl_vat: '100',
    }))
    store.total = store.items.length
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/quotes', component: QuoteList }] })
    await router.push('/quotes'); await router.isReady()
    const wrapper = mount(QuoteList, { global: { plugins: [router, createI18n({ legacy: false, locale: 'en', messages: { en } })], stubs: { DocumentSendDialog: true, PdfPreviewDialog: true } } })
    await flushPromises()
    const table = wrapper.findComponent({ name: 'NDataTable' })
    const actionColumn = table.props('columns').find((column: {key: string}) => column.key === 'actions')
    expect(actionColumn.align).toBe('left')
    for (const row of wrapper.findAll('[data-row]')) {
      const buttons = row.findAll('button')
      expect(buttons).toHaveLength(6)
      expect(buttons.map(button => button.attributes('title'))).toEqual([
        en.quotes.edit, en.quotes.duplicate, en.pdf.preview, en.pdf.download, en.sendDialog.title, en.quotes.delete,
      ])
      expect(buttons.every(button => button.find('svg').exists())).toBe(true)
      const actions = row.get('[justify="start"]')
      expect(actions.attributes('wrap')).toBe('false')
      expect(buttons[5].attributes('disabled') !== undefined).toBe(row.attributes('data-row') === 'ACCEPTED')
    }
    await wrapper.get('[data-row="ACCEPTED"] button[aria-label="Delete"]').trigger('click')
    expect(confirmDelete).not.toHaveBeenCalled()
    expect(store.deleteQuote).not.toHaveBeenCalled()
    await wrapper.get('[data-row="DRAFT"] button[aria-label="Delete"]').trigger('click')
    expect(confirmDelete).toHaveBeenCalledTimes(1)
    expect(store.deleteQuote).not.toHaveBeenCalled()
    await confirmDelete.mock.calls[0][0].onPositiveClick()
    expect(store.deleteQuote).toHaveBeenCalledWith('DRAFT')
    wrapper.unmount()
  })
})
