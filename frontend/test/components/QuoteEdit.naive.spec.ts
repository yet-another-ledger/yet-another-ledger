import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { createI18n } from 'vue-i18n'
import { NDialogProvider, NDropdown, NMessageProvider, NSelect } from 'naive-ui'
import QuoteEdit from '../../src/views/quotes/QuoteEdit.vue'
import QuoteAppendixEditor from '../../src/components/QuoteAppendixEditor.vue'
import en from '../../src/locales/en.json'
import zh from '../../src/locales/zh.json'

const api = vi.hoisted(() => ({ get: vi.fn(), postFile: vi.fn(), fetchBlob: vi.fn(), saveBlob: vi.fn() }))
const store = vi.hoisted(() => ({
  fetchQuote: vi.fn(), updateQuote: vi.fn(), createQuote: vi.fn(), calculatePreview: vi.fn(),
  duplicateQuote: vi.fn(), transitionStatus: vi.fn(),
}))
vi.mock('../../src/api/http', () => ({ ...api, ApiError: class ApiError extends Error {} }))
vi.mock('../../src/stores/quotes', () => ({ useQuotesStore: () => store }))
vi.mock('../../src/stores/invoices', () => ({ useInvoicesStore: () => ({ fetchProductOptions: () => Promise.resolve([]) }) }))

const limits = {
  max_appendix_file_bytes: 1024, max_quote_appendices: 3,
  max_quote_appendix_input_bytes: 3072, max_quote_pdf_bytes: 3072, max_quote_pdf_pages: 20,
}
const quote = {
  id: 'quote-a', quote_number: 'Q-A', status: 'DRAFT', customer_id: 'customer', quote_date: '2026-11-04',
  valid_until: '2026-12-04', tax_mode: 'LINE', amounts_include_vat: false, vat_treatment_id: null,
  document_vat_rate_id: null, discount_type: 'NONE', discount_value: '0', notes: null, warranty_text: null,
  terms_text: null, bank_text: null, payment_terms_text: null, currency: 'EUR', appendix_version: 0,
  appendices: [], lines: [{ id: 'line', name: 'Original', quantity: '1', unit_price: '10', discount_type: 'NONE', discount_value: '0', vat_rate_id: null }],
}
const uploaded = { id: 'uploaded', owner_quote_id: 'quote-a', source: 'QUOTE', title: 'uploaded', original_filename: 'uploaded.pdf', sha256: 'hash', size_bytes: 100, page_count: 1, is_archived: false, created_at: '', updated_at: '' }
const i18n = createI18n({ legacy: false, locale: 'en', messages: { en, zh } })
let wrappers: ReturnType<typeof mount>[] = []

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const found = wrapper.findAll('button').find(item => item.text().trim() === label)
  if (!found) throw new Error(`Missing button: ${label}`)
  return found
}

async function mountRoute(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/quotes/new', component: QuoteEdit },
    { path: '/quotes/:id/edit', component: QuoteEdit },
    { path: '/quotes', component: { template: '<div>Quote list</div>' } },
  ] })
  await router.push(path)
  await router.isReady()
  const wrapper = mount(defineComponent({ setup: () => () => h(NMessageProvider, () => h(NDialogProvider, () => h(RouterView))) }), {
    attachTo: document.body,
    global: { plugins: [router, i18n], components: { NMessageProvider, NDialogProvider }, stubs: {
      DocumentSendDialog: true, PdfPreviewDialog: true, EmailLogPanel: true,
      QuotePaymentPanel: true, DocumentWorkflowPanel: true,
    } },
  })
  wrappers.push(wrapper)
  return { wrapper, router }
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  store.fetchQuote.mockResolvedValue(quote)
  api.get.mockImplementation((url: string) => {
    if (url.endsWith('/limits')) return Promise.resolve(limits)
    if (url.includes('/appendix-files') || url.startsWith('/api/v1/pdf-library?')) return Promise.resolve({ items: [], total: 0 })
    if (url.includes('/document-chain')) return Promise.resolve({ settlement_mode: 'UNSET', totals: {}, nodes: [], relations: [], events: [], timeline: [], available_actions: [] })
    if (url.startsWith('/api/v1/customers?q=')) return Promise.resolve({ items: [{ id: 'customer', name: 'Customer' }] })
    if (url.includes('vat-rates') || url.includes('vat-treatments')) return Promise.resolve({ items: [] })
    if (url.includes('document-templates') || url.includes('content-blocks') || url.includes('note-templates')) return Promise.resolve([
      { id: 'default-terms', kind: 'TERMS', body: 'New quote default terms', is_default: true },
    ])
    if (url.includes('default-valid-days')) return Promise.resolve({ default_valid_days: 30 })
    throw new Error(`Unexpected GET ${url}`)
  })
})

afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount()
  wrappers = []
  document.body.innerHTML = ''
})

it.each(['resolve', 'reject'] as const)('ignores stale new-Quote defaults after real route reuse when the request %s', async outcome => {
  const defaults = deferred<{ default_valid_days: number }>()
  const upload = deferred<typeof uploaded>()
  api.get.mockImplementation((url: string) => url.includes('default-valid-days') ? defaults.promise : Promise.resolve(
    url.includes('/appendix-files') || url.startsWith('/api/v1/pdf-library?') ? { items: [], total: 0 }
      : url.includes('/document-chain') ? { settlement_mode: 'UNSET', totals: {}, nodes: [], relations: [], events: [], timeline: [], available_actions: [] }
        : url.startsWith('/api/v1/customers?q=') ? { items: [{ id: 'customer', name: 'Customer' }] }
          : url.includes('vat-rates') || url.includes('vat-treatments') ? { items: [] }
            : url.includes('document-templates') || url.includes('content-blocks') || url.includes('note-templates') ? [{ id: 'default-terms', kind: 'TERMS', body: 'New quote default terms', is_default: true }]
              : url.endsWith('/limits') ? limits
                : (() => { throw new Error(`Unexpected GET ${url}`) })(),
  ))
  const { wrapper, router } = await mountRoute('/quotes/new')
  await router.push('/quotes/quote-a/edit')
  await flushPromises()

  const body = wrapper.find('input[placeholder="Service or product name"]')
  expect(body.element).toHaveProperty('value', 'Original')
  await body.setValue('Unsaved quote body')
  api.postFile.mockReturnValue(upload.promise)
  const fileInput = wrapper.find('input[type="file"]')
  Object.defineProperty(fileInput.element, 'files', { configurable: true, value: [new File(['%PDF'], 'appendix.pdf', { type: 'application/pdf' })] })
  await fileInput.trigger('change')
  await flushPromises()
  expect(api.postFile).toHaveBeenCalledTimes(1)

  if (outcome === 'resolve') defaults.resolve({ default_valid_days: 40 })
  else defaults.reject(new Error('stale defaults failed'))
  await flushPromises()
  expect(body.element).toHaveProperty('value', 'Unsaved quote body')
  expect(wrapper.find('textarea').element).toHaveProperty('value', '')

  upload.resolve(uploaded)
  await flushPromises()
  expect(wrapper.findComponent(QuoteAppendixEditor).find('[data-file-id="uploaded"]').exists()).toBe(true)

  await button(wrapper, 'Send Email').trigger('click')
  await flushPromises()
  expect(document.body.textContent).toContain('Unsaved Changes')
  const cancel = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Cancel').at(-1)
  expect(cancel).toBeDefined()
  cancel!.click()
  await flushPromises()
  expect(wrapper.find('document-send-dialog-stub').attributes('show')).toBe('false')
  expect(store.updateQuote).not.toHaveBeenCalled()

  const download = wrapper.findAllComponents(NDropdown)[1]
  download.vm.$emit('select', 'default')
  await flushPromises()
  expect(document.body.textContent).toContain('Unsaved Changes')
  expect(api.fetchBlob).not.toHaveBeenCalled()
  const cancelDownload = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Cancel').at(-1)
  cancelDownload!.click()
  await flushPromises()

  const navigation = router.push('/quotes')
  await flushPromises()
  expect(document.body.textContent).toContain('Unsaved Changes')
  const cancelLeave = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Cancel').at(-1)
  cancelLeave!.click()
  await navigation
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/quote-a/edit')
  expect(body.element).toHaveProperty('value', 'Unsaved quote body')
  expect(wrapper.findComponent(QuoteAppendixEditor).find('[data-file-id="uploaded"]').exists()).toBe(true)
  expect(store.updateQuote).not.toHaveBeenCalled()
})

it('guards repeat positive clicks on the real new-Quote leave dialog and navigates after one create', async () => {
  const { wrapper, router } = await mountRoute('/quotes/new')
  await flushPromises()
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Create once')
  await flushPromises()
  const create = deferred<typeof quote>()
  store.createQuote.mockReturnValue(create.promise)
  const navigation = router.push('/quotes')
  await flushPromises()

  const positive = [...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Save & Continue')
  expect(positive).toBeDefined()
  positive!.click()
  // A second event before Vue updates the disabled DOM button exercises the callback guard.
  positive!.click()
  await flushPromises()

  expect(store.createQuote).toHaveBeenCalledTimes(1)
  expect(positive!.disabled).toBe(true)
  expect(wrapper.find('input[placeholder="Service or product name"]').element).toHaveProperty('disabled', true)
  create.resolve({ ...quote, id: 'created-once' })
  await navigation
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes')
  expect(store.createQuote).toHaveBeenCalledTimes(1)
})

it('invalidates superseded real Router confirmations so only a fresh prompt can create', async () => {
  const { wrapper, router } = await mountRoute('/quotes/quote-a/edit')
  await router.push('/quotes/new')
  await flushPromises()
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  const body = wrapper.find('input[placeholder="Service or product name"]')
  await body.setValue('Create from a fresh confirmation')

  router.back()
  await flushPromises()
  const firstPositive = [...document.body.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Save & Continue')!
  expect(firstPositive).toBeDefined()
  router.back()
  await flushPromises()
  await new Promise(resolve => setTimeout(resolve, 400))
  const livePositives = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue')
  // Naive keeps the destroyed dialog node during its leave transition. Its
  // callback is stale immediately, even while that node is still in the DOM.
  expect(livePositives).toHaveLength(2)

  firstPositive.click()
  await flushPromises()
  expect(store.createQuote).not.toHaveBeenCalled()
  const cancel = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Cancel').at(-1)!
  cancel.click()
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/new')
  expect(body.element).toHaveProperty('value', 'Create from a fresh confirmation')
  expect(store.createQuote).not.toHaveBeenCalled()

  store.createQuote.mockResolvedValueOnce({ ...quote, id: 'created-from-fresh-confirmation' })
  const freshNavigation = router.push('/quotes')
  await flushPromises()
  const freshPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  freshPositive.click()
  await freshNavigation
  await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes')
}, 10000)

it('keeps the newest real Router confirmation navigation usable through update and leave', async () => {
  store.fetchQuote.mockImplementation((id: string) => Promise.resolve({ ...quote, id, quote_number: id }))
  store.updateQuote.mockImplementation((id: string) => Promise.resolve({ ...quote, id, quote_number: id }))
  const { wrapper, router } = await mountRoute('/quotes/quote-b/edit')
  await router.push('/quotes/quote-c/edit')
  await flushPromises()
  await router.push('/quotes/new')
  await flushPromises()
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  await wrapper.find('input[placeholder="Service or product name"]').setValue('New Quote before two Back attempts')

  router.back()
  await flushPromises()
  const firstPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)!
  router.back()
  await flushPromises()
  const positives = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue')
  expect(positives).toHaveLength(2)
  store.createQuote.mockResolvedValueOnce({ ...quote, id: 'created-from-current-confirmation' })
  positives.at(-1)!.click()
  await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes/quote-b/edit')
  expect(store.fetchQuote).toHaveBeenCalledWith('quote-b')

  // The current prompt's save is allowed to complete its own navigation. The
  // older prompt was invalidated and cannot issue a second create afterward.
  const stalePositive = firstPositive === positives.at(-1) ? positives[0]! : firstPositive
  stalePositive.click()
  await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(1)
  expect(router.currentRoute.value.path).toBe('/quotes/quote-b/edit')

  await router.push('/quotes/created-from-current-confirmation/edit')
  await flushPromises()
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(store.fetchQuote).toHaveBeenCalledWith('created-from-current-confirmation')
  await wrapper.find('input[placeholder="Service or product name"]').setValue('Update persisted new Quote')
  store.updateQuote.mockResolvedValueOnce({ ...quote, id: 'created-from-current-confirmation' })
  await button(wrapper, 'Save').trigger('click')
  await flushPromises()
  expect(store.updateQuote).toHaveBeenCalledTimes(1)
  expect(store.updateQuote.mock.calls[0]![0]).toBe('created-from-current-confirmation')
  await router.push('/quotes')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes')
}, 10000)

it('keeps a failed new-Quote leave save retryable with the body intact', async () => {
  const originalGet = api.get.getMockImplementation()!
  const libraryFile = { id: 'library-file', owner_quote_id: null, source: 'LIBRARY', title: 'Library file', original_filename: 'library.pdf', sha256: 'hash', size_bytes: 100, page_count: 1, is_archived: false, created_at: '', updated_at: '' }
  api.get.mockImplementation((url: string) => url.startsWith('/api/v1/pdf-library?')
    ? Promise.resolve({ items: [libraryFile], total: 1 })
    : originalGet(url))
  const { wrapper, router } = await mountRoute('/quotes/new')
  await flushPromises()
  wrapper.findAllComponents(NSelect)[0]!.vm.$emit('update:value', 'customer')
  await flushPromises()
  const appendixEditor = wrapper.findComponent(QuoteAppendixEditor)
  const addFile = appendixEditor.findAll('button').find(item => item.text().trim() === 'Add')
  expect(addFile).toBeDefined()
  await addFile!.trigger('click')
  expect(appendixEditor.find('[data-file-id="library-file"]').exists()).toBe(true)
  const body = wrapper.find('input[placeholder="Service or product name"]')
  await body.setValue('Keep this body')
  const navigation = router.push('/quotes')
  await flushPromises()

  store.createQuote.mockRejectedValueOnce(new Error('save failed'))
  const positive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)
  expect(positive).toBeDefined()
  positive!.click()
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/quotes/new')
  expect(body.element).toHaveProperty('value', 'Keep this body')
  expect(appendixEditor.find('[data-file-id="library-file"]').exists()).toBe(true)

  store.createQuote.mockResolvedValue({ ...quote, id: 'created-after-retry' })
  const retryNavigation = router.push('/quotes')
  await flushPromises()
  const retryPositive = [...document.body.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Save & Continue').at(-1)
  expect(retryPositive).toBeDefined()
  retryPositive!.click()
  await retryNavigation
  await flushPromises()
  expect(store.createQuote).toHaveBeenCalledTimes(2)
  expect(store.createQuote.mock.calls[1][0].appendix_file_ids).toEqual(['library-file'])
  expect(router.currentRoute.value.path).toBe('/quotes')
})
