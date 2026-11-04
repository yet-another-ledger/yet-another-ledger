import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { createI18n } from 'vue-i18n'
import { NDialogProvider, NMessageProvider, NSelect } from 'naive-ui'
import QuoteEdit from '../../src/views/quotes/QuoteEdit.vue'
import Editor from '../../src/components/QuoteAppendixEditor.vue'
import en from '../../src/locales/en.json'

const api = vi.hoisted(() => ({ get: vi.fn(), postFile: vi.fn(), fetchBlob: vi.fn(), saveBlob: vi.fn() }))
const store = vi.hoisted(() => ({ fetchQuote: vi.fn(), updateQuote: vi.fn(), createQuote: vi.fn(), calculatePreview: vi.fn(), duplicateQuote: vi.fn(), transitionStatus: vi.fn() }))
vi.mock('../../src/api/http', () => ({ ...api, ApiError: class ApiError extends Error { status = 409; detail = { detail: { code: 'QUOTE_APPENDIX_VERSION_CONFLICT' } } } }))
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => store }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => ({ fetchProductOptions: () => Promise.resolve([]) }) }))

const limits = { max_appendix_file_bytes: 1024, max_quote_appendices: 3, max_quote_appendix_input_bytes: 3072, max_quote_pdf_bytes: 3072, max_quote_pdf_pages: 20 }
const file = (id: string) => ({ id, owner_quote_id: null, source: 'LIBRARY', title: id, original_filename: `${id}.pdf`, sha256: id, size_bytes: 100, page_count: 1, is_archived: false, created_at: '', updated_at: '' })
const quote = {
  id: 'quote-a', quote_number: 'Q-A', status: 'DRAFT', customer_id: 'customer', quote_date: '2026-11-04', valid_until: '2026-12-04',
  tax_mode: 'LINE', amounts_include_vat: false, vat_treatment_id: null, document_vat_rate_id: null, discount_type: 'NONE', discount_value: '0',
  notes: null, warranty_text: null, terms_text: null, bank_text: null, payment_terms_text: null, currency: 'EUR', appendices: [], appendix_version: 0,
  lines: [{ id: 'line', name: 'Original', quantity: '1', unit_price: '10', discount_type: 'NONE', discount_value: '0', vat_rate_id: null }],
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
let wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  vi.clearAllMocks()
  store.fetchQuote.mockImplementation((id: string) => Promise.resolve({ ...quote, id, quote_number: id }))
  api.get.mockImplementation((url: string) => {
    if (url.endsWith('/limits')) return Promise.resolve(limits)
    if (url.includes('/appendix-files') || url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [file('first'), file('second')], total: 2 })
    if (url.includes('/document-chain')) return Promise.resolve({ settlement_mode: 'UNSET', totals: {}, nodes: [], relations: [], events: [], timeline: [], available_actions: [] })
    if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [{ id: 'customer', name: 'Customer' }] })
    if (url.includes('vat-rates') || url.includes('vat-treatments')) return Promise.resolve({ items: [] })
    if (url.includes('document-templates') || url.includes('content-blocks') || url.includes('note-templates')) return Promise.resolve([])
    if (url.includes('default-valid-days')) return Promise.resolve({ default_valid_days: 30 })
    throw new Error(`unexpected ${url}`)
  })
})
afterEach(() => { for (const wrapper of wrappers) wrapper.unmount(); wrappers = []; document.body.innerHTML = '' })
const i18n = () => createI18n({ legacy: false, locale: 'en', messages: { en } })
const button = (wrapper: ReturnType<typeof mount>, label: string) => {
  const target = wrapper.findAll('button').find(item => item.text().trim() === label)
  if (!target) throw new Error(`missing button: ${label}`)
  return target
}
async function page(path = '/quotes/quote-a/edit') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/quotes/new', component: QuoteEdit }, { path: '/quotes/:id/edit', component: QuoteEdit },
    { path: '/quotes', component: { template: '<div>List</div>' } },
  ] })
  await router.push(path); await router.isReady()
  const wrapper = mount(defineComponent({ setup: () => () => h(NMessageProvider, () => h(NDialogProvider, () => h(RouterView))) }), {
    attachTo: document.body,
    global: { plugins: [router, i18n()], components: { NMessageProvider, NDialogProvider }, stubs: {
      DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true, QuotePaymentPanel: true, DocumentWorkflowPanel: true,
    } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}

it('actual Naive dialogs and Router invalidate a superseded history confirmation before it can save the next Quote', async () => {
  store.updateQuote.mockImplementation((id: string) => Promise.resolve({ ...quote, id, quote_number: id }))
  const { wrapper, router } = await page('/quotes/quote-b/edit')
  await router.push('/quotes/quote-c/edit'); await flushPromises()
  await router.push('/quotes/quote-a/edit'); await flushPromises()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Unsaved A')

  router.back(); await flushPromises()
  expect([...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue')).toHaveLength(1)
  router.back(); await flushPromises()
  const positives = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue')
  expect(positives).toHaveLength(2)
  positives[1]!.click(); await flushPromises(); await new Promise(resolve => setTimeout(resolve, 50)); await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/quote-b/edit')
  expect(store.updateQuote).toHaveBeenCalledTimes(1)
  expect(store.updateQuote.mock.calls[0]![0]).toBe('quote-a')
  positives[0]!.click(); await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes/quote-b/edit')
  router.forward(); await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/quote-a/edit')
}, 10000)

it('actual Naive editor freezes appendix edits during deferred conflict reload, keeps body, retries after failure', async () => {
  const { wrapper, router } = await page()
  const editor = wrapper.findComponent(Editor)
  await button(editor, 'Add').trigger('click'); await flushPromises()
  expect(editor.find('[data-file-id="first"]').exists()).toBe(true)
  store.updateQuote.mockRejectedValueOnce(new (await import('../../src/api/http')).ApiError(409, { detail: { code: 'QUOTE_APPENDIX_VERSION_CONFLICT' } }, 'conflict'))
  await wrapper.findAll('button').find(item => item.text().trim() === 'Save')!.trigger('click'); await flushPromises()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Body stays edited')

  const stale = deferred<typeof quote>()
  store.fetchQuote.mockReturnValueOnce(stale.promise)
  const requestsBeforeReload = store.fetchQuote.mock.calls.length
  await button(wrapper, 'Reload saved list').trigger('click'); await flushPromises()
  expect(button(editor, 'Add').element.disabled).toBe(true)
  expect(button(wrapper, 'Save').element.disabled).toBe(true)
  await button(wrapper, 'Reload saved list').trigger('click')
  expect(store.fetchQuote).toHaveBeenCalledTimes(requestsBeforeReload + 1)
  await button(editor, 'Add').trigger('click')
  expect(editor.find('[data-file-id="second"]').exists()).toBe(false)

  await router.push({ path: router.currentRoute.value.path, query: { refresh: '1' } }); await flushPromises()
  const serverFile = { ...file('stale-server-file'), file_id: 'stale-server-file', sort_order: 0, title_snapshot: 'Stale server result' }
  stale.resolve({ ...quote, appendix_version: 99, appendices: [serverFile] }); await flushPromises()
  expect(editor.find('[data-file-id="first"]').exists()).toBe(true)
  expect(editor.find('[data-file-id="stale-server-file"]').exists()).toBe(false)
  expect(button(editor, 'Add').element.disabled).toBe(false)

  const failed = deferred<never>()
  store.fetchQuote.mockReturnValueOnce(failed.promise)
  await button(wrapper, 'Reload saved list').trigger('click'); await flushPromises()
  expect(button(editor, 'Add').element.disabled).toBe(true)
  failed.reject(new Error('temporary reload failure')); await flushPromises()
  expect(editor.find('[data-file-id="first"]').exists()).toBe(true)
  expect(wrapper.find('input[placeholder="Service or product name"]').element).toHaveProperty('value', 'Body stays edited')

  const retry = deferred<typeof quote>()
  store.fetchQuote.mockReturnValueOnce(retry.promise)
  await button(wrapper, 'Reload saved list').trigger('click'); await flushPromises()
  expect(button(editor, 'Add').element.disabled).toBe(true)
  retry.resolve({ ...quote, appendix_version: 2, appendices: [] }); await flushPromises()
  expect(editor.find('[data-file-id="first"]').exists()).toBe(false)
  expect(wrapper.find('input[placeholder="Service or product name"]').element).toHaveProperty('value', 'Body stays edited')
  expect(button(editor, 'Add').element.disabled).toBe(false)
})

it('actual Naive leave confirmation closes after save failure and a fresh confirmation can continue', async () => {
  const { wrapper, router } = await page()
  const body = wrapper.find('input[placeholder="Service or product name"]')
  await body.setValue('Unsaved body survives failure')
  store.updateQuote.mockRejectedValueOnce(new Error('temporary save failure'))

  const failedNavigation = router.push('/quotes')
  await flushPromises()
  const firstPositive = [...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Save & Continue')!
  firstPositive.click(); await failedNavigation; await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/quote-a/edit')
  expect(body.element).toHaveProperty('value', 'Unsaved body survives failure')

  store.updateQuote.mockResolvedValueOnce({ ...quote })
  const retryNavigation = router.push('/quotes')
  await flushPromises()
  const retryPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  expect(retryPositive).not.toBe(firstPositive)
  retryPositive.click(); await retryNavigation; await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(2)
  expect(router.currentRoute.value.path).toBe('/quotes')
})

it('actual Naive deferred create disables every confirmation cancellation path until navigation settles', async () => {
  const { wrapper, router } = await page('/quotes/new')
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Persisted after deferred create')
  const create = deferred<typeof quote>()
  store.createQuote.mockReturnValue(create.promise)
  const navigation = router.push('/quotes')
  await flushPromises()
  const positive = [...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Save & Continue')!
  positive.click(); await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(1)
  const cancel = [...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Cancel')!
  expect(cancel.disabled).toBe(true)
  expect(document.body.querySelector('.n-dialog__close')).toBeNull()
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  document.querySelector('.n-dialog__mask')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await flushPromises()
  expect(document.body.contains(positive)).toBe(true)
  expect(router.currentRoute.value.path).toBe('/quotes/new')
  router.back()
  await flushPromises()
  router.forward()
  await flushPromises()

  create.resolve({ ...quote, id: 'created-after-deferred-save' })
  await navigation; await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes/created-after-deferred-save/edit')
  store.updateQuote.mockResolvedValueOnce({ ...quote, id: 'created-after-deferred-save' })
  await button(wrapper, 'Save').trigger('click'); await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(1)
  await router.push('/quotes'); await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes')
})

it('actual Naive Send failure closes its confirmation and a fresh Send action resumes after saving', async () => {
  const { wrapper } = await page()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Unsaved body before Send')
  const send = wrapper.find('document-send-dialog-stub')
  store.updateQuote.mockRejectedValueOnce(new Error('temporary save failure'))
  await button(wrapper, 'Send Email').trigger('click'); await flushPromises()
  const firstPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  firstPositive.click(); await flushPromises()
  expect(send.attributes('show')).toBe('false')

  store.updateQuote.mockResolvedValueOnce({ ...quote })
  await button(wrapper, 'Send Email').trigger('click'); await flushPromises()
  const retryPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  expect(retryPositive).not.toBe(firstPositive)
  retryPositive.click(); await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(2)
  expect(send.attributes('show')).toBe('true')
})

it('actual Naive new Quote failed create can be retried from a fresh prompt and saved Quote identity remains usable', async () => {
  const { wrapper, router } = await page('/quotes/new')
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  const body = wrapper.find('input[placeholder="Service or product name"]')
  await body.setValue('New Quote body survives create failure')
  store.createQuote.mockRejectedValueOnce(new Error('temporary create failure'))

  const failedNavigation = router.push('/quotes')
  await flushPromises()
  const firstPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  firstPositive.click(); await failedNavigation; await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/new')
  expect(body.element).toHaveProperty('value', 'New Quote body survives create failure')

  store.createQuote.mockResolvedValueOnce({ ...quote, id: 'created-on-retry' })
  const retryNavigation = router.push('/quotes')
  await flushPromises()
  const retryPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  expect(retryPositive).not.toBe(firstPositive)
  retryPositive.click(); await retryNavigation; await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(2)
  expect(router.currentRoute.value.path).toBe('/quotes')

  await router.push('/quotes/created-on-retry/edit'); await flushPromises()
  store.updateQuote.mockResolvedValueOnce({ ...quote, id: 'created-on-retry' })
  await button(wrapper, 'Save').trigger('click'); await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes')
})
