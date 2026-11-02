<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import LineOrdering from '../../components/LineOrdering.vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { localDateStr } from '../../utils/date'
import {
  useMessage, useDialog,
  NButton, NSpace, NInput, NForm, NFormItem, NCard, NSpin, NAlert,
  NDivider, NInputNumber, NSelect, NSwitch, NTag, NDatePicker,
  NGrid, NGi, NText, NModal, NList, NListItem, NThing, NDropdown, NEmpty,
} from 'naive-ui'
import { AddOutline, TrashOutline, DocumentTextOutline, DownloadOutline, MailOutline, EyeOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import DocumentSendDialog from '../../components/DocumentSendDialog.vue'
import PdfPreviewDialog from '../../components/PdfPreviewDialog.vue'
import EmailLogPanel from '../../components/EmailLogPanel.vue'
import QuotePaymentPanel from '../../components/QuotePaymentPanel.vue'
import DocumentWorkflowPanel from '../../components/DocumentWorkflowPanel.vue'
import { useQuotesStore } from '../../stores/quotes'
import { useInvoicesStore } from '../../stores/invoices'
import { get, downloadBlob } from '../../api/http'
import {
  createDocumentChainPaymentChangeHandler,
  useDocumentChainRefresh,
} from '../../composables/useDocumentChainRefresh'
import type { components } from '../../api/schema'
import { persistedReceiptCustomer } from '../../utils/receiptEmail'

type CustomerRead = components['schemas']['CustomerRead']
type VatRateRead = components['schemas']['VatRateRead']
type VatTreatmentRead = components['schemas']['VatTreatmentRead']
type ProductInvoiceOptionRead = components['schemas']['ProductInvoiceOptionRead']
type QuoteCalculationRead = components['schemas']['QuoteCalculationRead']
type QuoteWrite = components['schemas']['QuoteWrite']
type QuoteRead = components['schemas']['QuoteRead']
type DocumentChainRead = components['schemas']['DocumentChainRead']
type DocumentTemplateRead = components['schemas']['DocumentTemplateRead']
type ContentBlockRead = components['schemas']['ContentBlockRead']
type NoteTemplateRead = components['schemas']['NoteTemplateRead']
type EmailLogRead = components['schemas']['EmailLogRead']

let lineKeyCounter = 0
function nextLineKey() { return `l${++lineKeyCounter}` }

interface LineRow {
  _key: string
  product_id: string | null
  name: string
  description: string | null
  quantity: number
  unit_id: string | null
  unit_name: string | null
  unit_price: number
  discount_type: 'NONE' | 'PERCENTAGE' | 'FIXED'
  discount_value: number
  vat_rate_id: string | null
}

function emptyLine(): LineRow {
  return {
    _key: nextLineKey(),
    product_id: null,
    name: '',
    description: null,
    quantity: 1,
    unit_id: null,
    unit_name: null,
    unit_price: 0,
    discount_type: 'NONE',
    discount_value: 0,
    vat_rate_id: null,
  }
}

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const store = useQuotesStore()
const invoicesStore = useInvoicesStore()

const isEdit = ref(false)
const pageLoading = ref(false)
const saving = ref(false)
const duplicating = ref(false)
const pageError = ref<string | null>(null)
const existingQuote = ref<QuoteRead | null>(null)
const {
  documentChain,
  chainRefreshing,
  initialChainError,
  paymentRefreshError,
  loadInitialDocumentChain,
  refreshAfterPayment,
  resetDocumentChain,
} = useDocumentChainRefresh<DocumentChainRead>(async () => {
  const quoteId = existingQuote.value?.id
  if (!quoteId) throw new Error('Quote document chain is unavailable')
  return get<DocumentChainRead>(`/api/v1/quotes/${quoteId}/document-chain`)
})
const canConvertFromProjection = computed(() => documentChain.value?.available_actions.some(action => action.code === 'CONVERT_TO_INVOICE' && action.available && action.target_id === existingQuote.value?.id && action.target_type === 'QUOTE') ?? false)
const canRecordQuotePaymentFromProjection = computed(() => documentChain.value?.available_actions.some(action => action.code === 'RECORD_QUOTE_PAYMENT' && action.available && action.target_id === existingQuote.value?.id) ?? false)
const advanceActionReason = computed(() => {
  const reason = documentChain.value?.available_actions.find(action => action.code === 'CREATE_ADVANCE' && action.target_id === existingQuote.value?.id)?.reason_code
  return reason ? t(`workflow.reasons.${reason}`, reason) : null
})
// The chain, not the Quote row, owns settlement-mode state.  In particular a
// missing projection is not evidence that a Quote is still UNSET.
const settlementMode = computed(() => documentChain.value?.settlement_mode ?? null)
// An UNSET Quote has exactly one selected continuation at a time.  This is
// transient UI state only: the first successful backend command locks mode.
const selectedModeContinuation = ref<'RECEIPT_ONLY' | 'FORMAL_ADVANCE' | null>(null)
// A successful first Quote payment atomically locks UNSET -> RECEIPT_ONLY on
// the backend.  Keep that fact until the authoritative projection confirms
// it: retaining a stale UNSET chain after a refresh failure must never reopen
// Direct or Formal paths.
const receiptModeLockCommitted = ref(false)
const advanceCardSignal = ref(0)
const showModeCards = computed(() => isEdit.value && existingQuote.value?.status === 'ACCEPTED' && documentChain.value !== null && settlementMode.value === 'UNSET' && selectedModeContinuation.value === null)
// An accepted UNSET quote chooses its direct path from the mode card. It must
// remain unavailable in the header while that Quote is in either transient
// continuation, so only a single mode path can be started at once. Other
// status/mode combinations use the single projected conversion command in
// the header; do not infer that command from Quote status or local mode state.
const showHeaderConvert = computed(() => canConvertFromProjection.value
  && selectedModeContinuation.value === null
  && !(existingQuote.value?.status === 'ACCEPTED' && settlementMode.value === 'UNSET'))
const showFormalContinuation = computed(() => settlementMode.value === 'FORMAL_ADVANCE' || (settlementMode.value === 'UNSET' && selectedModeContinuation.value === 'FORMAL_ADVANCE'))
const showReceiptContinuation = computed(() => settlementMode.value === 'RECEIPT_ONLY' || (settlementMode.value === 'UNSET' && selectedModeContinuation.value === 'RECEIPT_ONLY'))

// Quote header fields
const customerId = ref<string | null>(null)
const customers = ref<CustomerRead[]>([])
const referenceNumber = ref<string | null>(null)
const quoteDate = ref(localDateStr(new Date()))
const validUntil = ref<string | null>(null)
const taxMode = ref<'LINE' | 'DOCUMENT'>('LINE')
const amountsIncludeVat = ref(false)
const vatTreatmentId = ref<string | null>(null)
const documentVatRateId = ref<string | null>(null)
const discountType = ref<'NONE' | 'PERCENTAGE' | 'FIXED'>('NONE')
const discountValue = ref(0)

// Content fields
const notes = ref<string | null>(null)
const warrantyText = ref<string | null>(null)
const termsText = ref<string | null>(null)
const bankText = ref<string | null>(null)
const paymentTermsText = ref<string | null>(null)

const lines = ref<LineRow[]>([emptyLine()])

// Reference data
const vatRates = ref<VatRateRead[]>([])
const vatTreatments = ref<VatTreatmentRead[]>([])
const productOptions = ref<ProductInvoiceOptionRead[]>([])

// Preview
const preview = ref<QuoteCalculationRead | null>(null)
const previewLoading = ref(false)
const previewError = ref<string | null>(null)

// Content libraries
const documentTemplates = ref<DocumentTemplateRead[]>([])
const contentBlocks = ref<ContentBlockRead[]>([])
const noteTemplates = ref<NoteTemplateRead[]>([])
const showTemplateModal = ref(false)
const showNoteTemplateModal = ref(false)

// Dirty tracking + reactivate modal
const cleanSnapshot = ref<string | null>(null)
const showReactivateModal = ref(false)
const reactivateDate = ref<string | null>(null)
const reactivating = ref(false)

// ------------------------------------------------------------------ fetch ref data

async function loadReferenceData() {
  const [ratesRes, treatsRes] = await Promise.all([
    get<{ items: VatRateRead[] }>('/api/v1/vat-rates'),
    get<{ items: VatTreatmentRead[] }>('/api/v1/vat-treatments?side=SALES'),
  ])
  vatRates.value = ratesRes.items
  vatTreatments.value = treatsRes.items
}

async function loadContentLibraries() {
  const [templates, blocks, noteTpls] = await Promise.all([
    get<DocumentTemplateRead[]>('/api/v1/document-templates'),
    get<ContentBlockRead[]>('/api/v1/content-blocks'),
    get<NoteTemplateRead[]>('/api/v1/note-templates'),
  ])
  documentTemplates.value = templates
  contentBlocks.value = blocks
  noteTemplates.value = noteTpls
}

async function searchCustomers(q: string) {
  const res = await get<{ items: CustomerRead[] }>(`/api/v1/customers?q=${encodeURIComponent(q)}&limit=20`)
  customers.value = res.items
}

async function searchProducts(q: string) {
  productOptions.value = await invoicesStore.fetchProductOptions(q)
}

// ------------------------------------------------------------------ customer selection

const customerOptions = computed(() => customers.value.map(c => ({
  label: c.name,
  value: c.id,
})))

function handleCustomerSelect(id: string | null) {
  customerId.value = id
  vatTreatmentId.value = null
}

// ------------------------------------------------------------------ VAT options

const vatRateOptions = computed(() => vatRates.value.map(r => ({
  label: `${r.label} (${r.percent}%)`,
  value: r.id,
})))

const vatTreatmentOptions = computed(() => vatTreatments.value
  .filter(t => t.active)
  .map(t => ({ label: `${t.code} – ${t.label}`, value: t.id })))

// ------------------------------------------------------------------ line management

const discountTypeOptions = computed(() => [
  { label: t('common.discountNone'), value: 'NONE' },
  { label: t('common.discountPercentage'), value: 'PERCENTAGE' },
  { label: t('common.discountFixed'), value: 'FIXED' },
])

function addLine() {
  lines.value.push(emptyLine())
}

function removeLine(i: number) {
  if (lines.value.length > 1) lines.value.splice(i, 1)
}

function handleProductSelect(i: number, productId: string | null) {
  const line = lines.value[i]
  line.product_id = productId
  if (productId) {
    const opt = productOptions.value.find(p => p.id === productId)
    if (opt) {
      if (opt.unit_id) line.unit_id = opt.unit_id
      if (opt.unit_name) line.unit_name = opt.unit_name
      if (opt.default_vat_rate_id) line.vat_rate_id = opt.default_vat_rate_id
      if (!line.name) line.name = opt.name
    }
  }
}

// ------------------------------------------------------------------ document template apply

const filteredTemplates = computed(() =>
  documentTemplates.value.filter(t => t.applies_to === 'QUOTE' || t.applies_to === 'BOTH')
)

function applyTemplate(template: DocumentTemplateRead) {
  lines.value = (template.lines ?? []).map(l => ({
    _key: nextLineKey(),
    product_id: null,
    name: l.name,
    description: l.description ?? null,
    quantity: Number(l.quantity),
    unit_id: l.unit_id ?? null,
    unit_name: l.unit_name ?? null,
    unit_price: l.unit_price != null ? Number(l.unit_price) : 0,
    discount_type: (l.discount_type as 'NONE' | 'PERCENTAGE' | 'FIXED') ?? 'NONE',
    discount_value: Number(l.discount_value),
    vat_rate_id: l.vat_rate_id ?? null,
  }))
  showTemplateModal.value = false
}

// ------------------------------------------------------------------ note template insert

function insertNoteTemplate(body: string) {
  notes.value = notes.value ? `${notes.value}\n${body}` : body
  showNoteTemplateModal.value = false
}

// ------------------------------------------------------------------ content blocks preload

function applyDefaultContentBlocks() {
  for (const kind of ['WARRANTY', 'TERMS', 'BANK', 'PAYMENT_TERMS'] as const) {
    const defaultBlock = contentBlocks.value.find(b => b.kind === kind && b.is_default)
    if (defaultBlock) {
      if (kind === 'WARRANTY' && !warrantyText.value) warrantyText.value = defaultBlock.body
      if (kind === 'TERMS' && !termsText.value) termsText.value = defaultBlock.body
      if (kind === 'BANK' && !bankText.value) bankText.value = defaultBlock.body
      if (kind === 'PAYMENT_TERMS' && !paymentTermsText.value) paymentTermsText.value = defaultBlock.body
    }
  }
}

const contentBlocksByKind = computed(() => {
  const result: Record<string, ContentBlockRead[]> = {}
  for (const b of contentBlocks.value) {
    if (!result[b.kind]) result[b.kind] = []
    result[b.kind].push(b)
  }
  return result
})

function selectContentBlock(kind: 'WARRANTY' | 'TERMS' | 'BANK' | 'PAYMENT_TERMS', id: string) {
  const block = contentBlocks.value.find(b => b.id === id)
  if (!block) return
  if (kind === 'WARRANTY') warrantyText.value = block.body
  if (kind === 'TERMS') termsText.value = block.body
  if (kind === 'BANK') bankText.value = block.body
  if (kind === 'PAYMENT_TERMS') paymentTermsText.value = block.body
}

// ------------------------------------------------------------------ preview

function buildCalculationRequest() {
  if (!customerId.value) return null
  const linesPayload = lines.value
    .filter(l => l.name.trim())
    .map(l => ({
      product_id: l.product_id ?? undefined,
      name: l.name.trim(),
      description: l.description ?? undefined,
      quantity: String(l.quantity),
      unit_id: l.unit_id ?? undefined,
      unit_name: l.unit_name ?? undefined,
      unit_price: String(l.unit_price),
      discount: { type: l.discount_type, value: String(l.discount_value) },
      vat_rate_id: l.vat_rate_id ?? undefined,
    }))
  if (!linesPayload.length) return null
  return {
    customer_id: customerId.value,
    quote_date: quoteDate.value,
    valid_until: validUntil.value ?? undefined,
    tax_mode: taxMode.value,
    amounts_include_vat: amountsIncludeVat.value,
    vat_treatment_id: vatTreatmentId.value ?? undefined,
    document_vat_rate_id: documentVatRateId.value ?? undefined,
    discount: { type: discountType.value, value: String(discountValue.value) },
    lines: linesPayload,
  }
}

let previewTimer: ReturnType<typeof setTimeout> | null = null
let previewVersion = 0
// Snapshot the row identities alongside each request; never match results to current indices.
const lineCalcMap = ref(new Map<string, QuoteCalculationRead['lines'][number]>())
function schedulePreview() {
  const version = ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
  preview.value = null
  previewError.value = null
  lineCalcMap.value = new Map()
  previewLoading.value = false
  previewTimer = setTimeout(async () => {
    const req = buildCalculationRequest()
    const keys = lines.value.filter(l => l.name.trim()).map(l => l._key)
    if (!req) return
    previewLoading.value = true
    try {
      const result = await store.calculatePreview(req as Parameters<typeof store.calculatePreview>[0])
      if (version !== previewVersion) return
      preview.value = result
      lineCalcMap.value = new Map(result.lines.map((line, i) => [keys[i]!, line]))
    } catch (e: unknown) {
      if (version !== previewVersion) return
      previewError.value = e instanceof Error ? e.message : String(e)
      preview.value = null
    } finally {
      if (version === previewVersion) previewLoading.value = false
    }
  }, 600)
}
onBeforeUnmount(() => {
  ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
})

watch(
  [customerId, quoteDate, taxMode, amountsIncludeVat, vatTreatmentId, documentVatRateId,
    discountType, discountValue, lines],
  schedulePreview,
  { deep: true },
)

function currentFormState(): string {
  return JSON.stringify({
    customerId: customerId.value,
    referenceNumber: referenceNumber.value,
    quoteDate: quoteDate.value,
    validUntil: validUntil.value,
    taxMode: taxMode.value,
    amountsIncludeVat: amountsIncludeVat.value,
    vatTreatmentId: vatTreatmentId.value,
    documentVatRateId: documentVatRateId.value,
    discountType: discountType.value,
    discountValue: discountValue.value,
    notes: notes.value,
    warrantyText: warrantyText.value,
    termsText: termsText.value,
    bankText: bankText.value,
    paymentTermsText: paymentTermsText.value,
    lines: lines.value,
  })
}

const formDirty = computed(() => {
  if (!isEdit.value || !existingQuote.value || cleanSnapshot.value === null) return false
  return currentFormState() !== cleanSnapshot.value
})

function isDateDisabled(ts: number): boolean {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return ts < today.getTime()
}

// ------------------------------------------------------------------ save

function buildWritePayload(): QuoteWrite | null {
  if (!customerId.value) return null
  const linesPayload = lines.value
    .filter(l => l.name.trim())
    .map(l => ({
      product_id: l.product_id ?? undefined,
      name: l.name.trim(),
      description: l.description ?? undefined,
      quantity: String(l.quantity),
      unit_id: l.unit_id ?? undefined,
      unit_name: l.unit_name ?? undefined,
      unit_price: String(l.unit_price),
      discount: { type: l.discount_type, value: String(l.discount_value) },
      vat_rate_id: l.vat_rate_id ?? undefined,
    }))
  if (!linesPayload.length) return null
  return {
    customer_id: customerId.value,
    reference_number: referenceNumber.value ?? undefined,
    quote_date: quoteDate.value,
    valid_until: validUntil.value ?? undefined,
    tax_mode: taxMode.value,
    amounts_include_vat: amountsIncludeVat.value,
    vat_treatment_id: vatTreatmentId.value ?? undefined,
    document_vat_rate_id: documentVatRateId.value ?? undefined,
    discount: { type: discountType.value, value: String(discountValue.value) },
    notes: notes.value ?? undefined,
    warranty_text: warrantyText.value ?? undefined,
    terms_text: termsText.value ?? undefined,
    bank_text: bankText.value ?? undefined,
    payment_terms_text: paymentTermsText.value ?? undefined,
    lines: linesPayload,
  }
}

async function handleSave() {
  const payload = buildWritePayload()
  if (!payload) {
    message.error(t('quotes.validationError'))
    return
  }
  saving.value = true
  try {
    if (isEdit.value && existingQuote.value) {
      await store.updateQuote(existingQuote.value.id, payload)
      message.success(t('quotes.updateSuccess'))
    } else {
      await store.createQuote(payload)
      message.success(t('quotes.createSuccess'))
    }
    router.push('/quotes')
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.saveFailed'))
  } finally {
    saving.value = false
  }
}

// ------------------------------------------------------------------ status actions

async function withDirtyCheck(): Promise<boolean> {
  if (!formDirty.value) return true
  return new Promise<boolean>((resolve) => {
    dialog.warning({
      title: t('quotes.unsavedChanges'),
      content: t('quotes.unsavedChangesContent'),
      positiveText: t('quotes.saveAndContinue'),
      negativeText: t('quotes.cancel'),
      onPositiveClick: async () => {
        if (!existingQuote.value) { resolve(false); return }
        const payload = buildWritePayload()
        if (!payload) {
          message.error(t('quotes.validationError'))
          resolve(false)
          return
        }
        saving.value = true
        try {
          await store.updateQuote(existingQuote.value.id, payload)
          cleanSnapshot.value = currentFormState()
          resolve(true)
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : t('quotes.saveFailed'))
          resolve(false)
        } finally {
          saving.value = false
        }
      },
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
      onMaskClick: () => resolve(false),
      onEsc: () => resolve(false),
    })
  })
}

async function handleStatusTransition(newStatus: 'SENT' | 'ACCEPTED' | 'REJECTED' | 'DRAFT') {
  if (!existingQuote.value) return
  if (!(await withDirtyCheck())) return
  try {
    await store.transitionStatus(existingQuote.value.id, { status: newStatus })
    message.success(t('quotes.statusUpdated'))
    router.push('/quotes')
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.statusFailed'))
  }
}

async function handleDuplicate() {
  if (!existingQuote.value || duplicating.value || saving.value || pageLoading.value) return
  duplicating.value = true
  try {
    if (!(await withDirtyCheck())) return
    const duplicate = await store.duplicateQuote(existingQuote.value.id)
    message.success(t('quotes.duplicateSuccess'))
    await router.push(`/quotes/${duplicate.id}/edit`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.duplicateFailed'))
  } finally {
    duplicating.value = false
  }
}

async function handleConvert() {
  if (!existingQuote.value) return
  if (!(await withDirtyCheck())) return
  try {
    const invoice = await store.convertQuote(existingQuote.value.id)
    message.success(t('quotes.convertSuccess'))
    router.push(`/invoices/${invoice.id}/edit`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.convertFailed'))
  }
}

function beginFormalAdvance(): void {
  selectedModeContinuation.value = 'FORMAL_ADVANCE'
  advanceCardSignal.value += 1
}

function beginReceiptOnly(): void {
  selectedModeContinuation.value = 'RECEIPT_ONLY'
}

function abandonModeContinuation(): void {
  if (settlementMode.value === 'UNSET' && !receiptModeLockCommitted.value) selectedModeContinuation.value = null
}

function handleReactivate() {
  if (!existingQuote.value) return
  reactivateDate.value = null
  showReactivateModal.value = true
}

async function confirmReactivate() {
  if (!existingQuote.value) return
  if (!(await withDirtyCheck())) return
  reactivating.value = true
  try {
    const payload = reactivateDate.value ? { valid_until: reactivateDate.value } : {}
    await store.reactivateQuote(existingQuote.value.id, payload)
    message.success(t('quotes.reactivateSuccess'))
    showReactivateModal.value = false
    router.push('/quotes')
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.reactivateFailed'))
  } finally {
    reactivating.value = false
  }
}

// ------------------------------------------------------------------ load existing quote

function populateFromQuote(q: QuoteRead) {
  existingQuote.value = q
  customerId.value = q.customer_id
  referenceNumber.value = q.reference_number ?? null
  quoteDate.value = q.quote_date
  validUntil.value = q.valid_until ?? null
  taxMode.value = q.tax_mode
  amountsIncludeVat.value = q.amounts_include_vat
  vatTreatmentId.value = q.vat_treatment_id
  documentVatRateId.value = q.document_vat_rate_id ?? null
  discountType.value = q.discount_type
  discountValue.value = Number(q.discount_value)
  notes.value = q.notes ?? null
  warrantyText.value = q.warranty_text ?? null
  termsText.value = q.terms_text ?? null
  bankText.value = q.bank_text ?? null
  paymentTermsText.value = q.payment_terms_text ?? null
  lines.value = (q.lines ?? []).map(l => ({
    _key: nextLineKey(),
    product_id: l.product_id ?? null,
    name: l.name,
    description: l.description ?? null,
    quantity: Number(l.quantity),
    unit_id: l.unit_id ?? null,
    unit_name: l.unit_name ?? null,
    unit_price: Number(l.unit_price),
    discount_type: l.discount_type as 'NONE' | 'PERCENTAGE' | 'FIXED',
    discount_value: Number(l.discount_value),
    vat_rate_id: l.vat_rate_id ?? null,
  }))
  cleanSnapshot.value = currentFormState()
}

let routeGeneration = 0
function resetRouteState(): void {
  ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
  previewLoading.value = false
  lineCalcMap.value = new Map()
  isEdit.value = false
  existingQuote.value = null
  resetDocumentChain()
  selectedModeContinuation.value = null
  receiptModeLockCommitted.value = false
  advanceCardSignal.value = 0
  pageError.value = null
  preview.value = null
  previewError.value = null
  cleanSnapshot.value = null
  lines.value = [emptyLine()]
}
async function loadRoute(): Promise<void> {
  const generation = ++routeGeneration
  resetRouteState()
  pageLoading.value = true
  try {
    await Promise.all([
      loadReferenceData(),
      loadContentLibraries(),
      searchCustomers(''),
      invoicesStore.fetchProductOptions().then(opts => { productOptions.value = opts }),
    ])

    const id = route.params.id as string | undefined
    if (id && id !== 'new') {
      isEdit.value = true
      const q = await store.fetchQuote(id)
      if (generation !== routeGeneration || route.params.id !== id) return
      populateFromQuote(q)
      await loadInitialDocumentChain()
      if (generation !== routeGeneration || route.params.id !== id) return
      if (!customers.value.find(c => c.id === q.customer_id)) {
        const custRes = await get<CustomerRead>(`/api/v1/customers/${q.customer_id}`)
        if (generation !== routeGeneration || route.params.id !== id) return
        customers.value = [custRes, ...customers.value]
      }
    } else {
      // New quote – pre-fill valid_until from company default, then apply default content blocks
      try {
        const vd = await get<{ default_valid_days: number }>('/api/v1/settings/quote-default-valid-days')
        if (vd?.default_valid_days) {
          const d = new Date()
          d.setDate(d.getDate() + vd.default_valid_days)
          validUntil.value = localDateStr(d)
        }
      } catch {
        // leave validUntil null; backend will fill in its own default on save
      }
      applyDefaultContentBlocks()
    }
  } catch (e: unknown) {
    if (generation === routeGeneration) pageError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (generation === routeGeneration) pageLoading.value = false
  }
}
onMounted(() => { void loadRoute() })
watch(() => route.params.id, () => { void loadRoute() })

// ACCEPTED is the only read-only state; expired/rejected can still be edited
const isReadOnly = computed(() => existingQuote.value?.status === 'ACCEPTED' && isEdit.value)

const fmtMoney = (v: string | number) => Number(v).toFixed(2)

// ---- Selected customer for dialog pre-fill ----
const selectedCustomer = computed(() => customers.value.find(c => c.id === customerId.value) ?? null)
// Existing payment receipts retain the quote's persisted customer even when
// this editable form has a different, unsaved customer selection.
const receiptCustomer = computed(() =>
  persistedReceiptCustomer(existingQuote.value?.customer_id, customers.value),
)

const handlePaymentsChanged = createDocumentChainPaymentChangeHandler(async () => {
  // QuotePaymentPanel emits only after its mutation succeeds.  When this is
  // the first Receipt-only command from UNSET, its backend transaction has
  // already committed the mode lock even though the independent chain reload
  // may still be pending or fail.
  if (settlementMode.value === 'UNSET' && selectedModeContinuation.value === 'RECEIPT_ONLY') {
    receiptModeLockCommitted.value = true
  }
  const refreshed = await refreshAfterPayment()
  if (refreshed && settlementMode.value === 'RECEIPT_ONLY') {
    receiptModeLockCommitted.value = false
  }
  return refreshed
})

async function retryInitialDocumentChain(): Promise<void> {
  if (chainRefreshing.value) return
  await loadInitialDocumentChain()
}

async function retryPaymentDocumentChain(): Promise<void> {
  if (chainRefreshing.value) return
  await refreshAfterPayment()
}

// ---- PDF download ----
const downloadingPdf = ref(false)

const pdfLocaleOptions = computed(() => [
  { label: t('pdf.localeDefault'), key: 'default' },
  { label: t('pdf.localeEn'), key: 'en' },
  { label: t('pdf.localeZh'), key: 'zh' },
])

async function handleDownloadPdf(locale?: 'en' | 'zh') {
  if (!existingQuote.value) return
  downloadingPdf.value = true
  try {
    const id = existingQuote.value.id
    const url = locale
      ? `/api/v1/quotes/${id}/pdf?locale=${locale}`
      : `/api/v1/quotes/${id}/pdf`
    await downloadBlob(url, `${existingQuote.value.quote_number}.pdf`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('pdf.downloadFailed'))
  } finally {
    downloadingPdf.value = false
  }
}

function handlePdfLocaleSelect(key: string) {
  if (key === 'default') {
    handleDownloadPdf()
  } else {
    handleDownloadPdf(key as 'en' | 'zh')
  }
}

// ---- PDF preview (in-app modal) ----
const previewShow = ref(false)
const previewSrc = ref<string | null>(null)
const previewFallback = ref('quote.pdf')

function openPreview(locale?: 'en' | 'zh') {
  if (!existingQuote.value) return
  const id = existingQuote.value.id
  previewSrc.value = locale
    ? `/api/v1/quotes/${id}/pdf?locale=${locale}`
    : `/api/v1/quotes/${id}/pdf`
  previewFallback.value = `${existingQuote.value.quote_number}.pdf`
  previewShow.value = true
}

function handlePreviewLocaleSelect(key: string) {
  if (key === 'default') {
    openPreview()
  } else {
    openPreview(key as 'en' | 'zh')
  }
}

// ---- Send dialog ----
const sendDialogShow = ref(false)

function openSendDialog() {
  sendDialogShow.value = true
}

// ---- Email log panel ref ----
const emailLogPanelRef = ref<InstanceType<typeof EmailLogPanel> | null>(null)

function handleSent(_log: EmailLogRead) {
  emailLogPanelRef.value?.refresh()
}

function handleReceiptSent(log: EmailLogRead) {
  if (log.related_type === 'QUOTE') emailLogPanelRef.value?.refresh()
}
</script>

<template>
  <div class="quote-edit-page">
    <n-spin :show="pageLoading">
          <div class="quote-edit-container">

            <!-- Page title + status actions -->
            <div class="page-header">
              <h2>
                {{ isEdit
                  ? (existingQuote ? existingQuote.quote_number : t('quotes.edit'))
                  : t('quotes.new') }}
              </h2>
              <n-space v-if="existingQuote" align="center">
                <n-tag :type="existingQuote.status === 'DRAFT' ? 'default'
                  : existingQuote.status === 'SENT' ? 'info'
                  : existingQuote.status === 'ACCEPTED' ? 'success'
                  : existingQuote.status === 'REJECTED' ? 'error'
                  : 'warning'">
                  {{ t(`quotes.status${existingQuote.status}`) }}
                </n-tag>

                <n-button size="small" :loading="duplicating" :disabled="duplicating || saving || pageLoading" @click="handleDuplicate">
                  {{ t('quotes.duplicate') }}
                </n-button>

                <!-- PDF preview dropdown (default / en / zh) -->
                <n-dropdown
                  :options="pdfLocaleOptions"
                  trigger="click"
                  @select="handlePreviewLocaleSelect"
                >
                  <n-button size="small">
                    <template #icon><n-icon><EyeOutline /></n-icon></template>
                    {{ t('pdf.preview') }}
                  </n-button>
                </n-dropdown>

                <!-- PDF download dropdown (default / en / zh) -->
                <n-dropdown
                  :options="pdfLocaleOptions"
                  trigger="click"
                  @select="handlePdfLocaleSelect"
                >
                  <!-- v-if/v-else to avoid loading-prop+v-if prod bug -->
                  <n-button v-if="downloadingPdf" size="small" loading disabled>
                    {{ t('pdf.download') }}
                  </n-button>
                  <n-button v-else size="small">
                    <template #icon><n-icon><DownloadOutline /></n-icon></template>
                    {{ t('pdf.download') }}
                  </n-button>
                </n-dropdown>

                <!-- Send email button -->
                <n-button size="small" type="info" @click="openSendDialog">
                  <template #icon><n-icon><MailOutline /></n-icon></template>
                  {{ t('sendDialog.title') }}
                </n-button>

                <!-- Show link to converted invoice -->
                <n-text v-if="existingQuote.converted_invoice_id" depth="3" style="font-size: 13px">
                  {{ t('quotes.convertedInvoiceLink') }}:
                  <a
                    style="cursor: pointer; color: var(--n-color-target)"
                    @click="router.push(`/invoices/${existingQuote!.converted_invoice_id}/edit`)"
                  >{{ t('quotes.viewInvoice') }}</a>
                </n-text>

                <!-- DRAFT actions -->
                <template v-if="existingQuote.status === 'DRAFT'">
                  <n-button size="small" type="info" @click="handleStatusTransition('SENT')">
                    {{ t('quotes.markSent') }}
                  </n-button>
                </template>

                <!-- SENT actions -->
                <template v-else-if="existingQuote.status === 'SENT'">
                  <n-button size="small" type="success" @click="handleStatusTransition('ACCEPTED')">
                    {{ t('quotes.markAccepted') }}
                  </n-button>
                  <n-button size="small" type="error" @click="handleStatusTransition('REJECTED')">
                    {{ t('quotes.markRejected') }}
                  </n-button>
                  <n-button v-if="showHeaderConvert" size="small" type="primary" @click="handleConvert">
                    {{ t('quotes.convertToInvoice') }}
                  </n-button>
                </template>

                <!-- REJECTED actions -->
                <template v-else-if="existingQuote.status === 'REJECTED'">
                  <n-button size="small" type="info" @click="handleStatusTransition('SENT')">
                    {{ t('quotes.reSend') }}
                  </n-button>
                </template>

                <!-- EXPIRED actions -->
                <template v-else-if="existingQuote.status === 'EXPIRED'">
                  <n-button v-if="showHeaderConvert" size="small" type="primary" @click="handleConvert">
                    {{ t('quotes.convertToInvoice') }}
                  </n-button>
                  <n-button size="small" type="warning" @click="handleReactivate">
                    {{ t('quotes.reactivate') }}
                  </n-button>
                </template>

                <!-- ACCEPTED actions -->
                <template v-else-if="existingQuote.status === 'ACCEPTED'">
                  <n-button
                    v-if="showHeaderConvert"
                    size="small"
                    type="primary"
                    @click="handleConvert"
                  >
                    {{ t('quotes.convertToInvoice') }}
                  </n-button>
                </template>
              </n-space>
            </div>

            <n-card v-if="showModeCards" size="small" :title="t('workflow.mode')" class="billing-mode-cards">
              <n-grid :cols="1" s:cols="3" x-gap="12" y-gap="12" responsive="screen">
                <n-gi class="billing-mode-card-grid-item">
                  <n-card size="small" class="billing-mode-card">
                    <div class="billing-mode-card-body">
                      <div>
                        <n-text strong class="billing-mode-card-title">{{ t('workflow.modes.DIRECT_INVOICE') }}</n-text>
                        <n-text depth="3" class="billing-mode-card-description">{{ t('quotes.convertToInvoice') }}</n-text>
                      </div>
                      <div class="billing-mode-card-actions">
                        <n-button type="primary" :disabled="!canConvertFromProjection" @click="handleConvert">{{ t('quotes.convertToInvoice') }}</n-button>
                      </div>
                    </div>
                  </n-card>
                </n-gi>
                <n-gi class="billing-mode-card-grid-item">
                  <n-card size="small" class="billing-mode-card">
                    <div class="billing-mode-card-body">
                      <div>
                        <n-text strong class="billing-mode-card-title">{{ t('workflow.modes.RECEIPT_ONLY') }}</n-text>
                        <n-text depth="3" class="billing-mode-card-description">{{ t('workflow.receiptOnly') }}</n-text>
                      </div>
                      <div class="billing-mode-card-actions">
                        <n-button type="primary" :disabled="!canRecordQuotePaymentFromProjection" @click="beginReceiptOnly">{{ t('payments.addQuotePayment') }}</n-button>
                      </div>
                    </div>
                  </n-card>
                </n-gi>
                <n-gi class="billing-mode-card-grid-item">
                  <n-card size="small" class="billing-mode-card">
                    <div class="billing-mode-card-body">
                      <div>
                        <n-text strong class="billing-mode-card-title">{{ t('workflow.modes.FORMAL_ADVANCE') }}</n-text>
                        <n-text depth="3" class="billing-mode-card-description">{{ advanceActionReason ?? t('workflow.createAdvance') }}</n-text>
                      </div>
                      <div class="billing-mode-card-actions">
                        <n-button type="primary" :disabled="!documentChain?.available_actions.some(action => action.code === 'CREATE_ADVANCE' && action.available && action.target_id === existingQuote?.id)" @click="beginFormalAdvance">{{ t('workflow.createAdvance') }}</n-button>
                      </div>
                    </div>
                  </n-card>
                </n-gi>
              </n-grid>
            </n-card>

            <n-spin v-if="existingQuote && !documentChain && chainRefreshing" size="small" style="margin-bottom: 16px" />
            <n-alert v-if="initialChainError && !documentChain" type="error" style="margin-bottom: 16px">
              {{ t('chain.initialLoadFailed') }}
              <n-button text type="primary" :loading="chainRefreshing" :disabled="chainRefreshing" @click="retryInitialDocumentChain">
                {{ t('chain.retry') }}
              </n-button>
            </n-alert>
            <n-alert v-if="paymentRefreshError" type="warning" style="margin-bottom: 16px">
              {{ t('chain.paymentRefreshFailed') }}
              <n-button text type="primary" :loading="chainRefreshing" :disabled="chainRefreshing" @click="retryPaymentDocumentChain">
                {{ t('chain.retry') }}
              </n-button>
            </n-alert>

            <DocumentWorkflowPanel
              v-if="existingQuote && documentChain && (settlementMode !== 'UNSET' || showFormalContinuation)"
              :quote-id="existingQuote.id"
              :document-chain="documentChain"
              :refresh-chain="loadInitialDocumentChain"
              :chain-loading="chainRefreshing"
              :chain-error="initialChainError ? t('chain.initialLoadFailed') : null"
              :open-advance-signal="advanceCardSignal"
              @changed="loadInitialDocumentChain"
              @continuation-cancelled="abandonModeContinuation"
            />
            <n-empty
              v-else-if="existingQuote && initialChainError && !chainRefreshing"
              :description="t('chain.initialLoadFailed')"
              size="small"
              style="margin-bottom: 16px"
            />

            <n-button v-if="selectedModeContinuation && settlementMode === 'UNSET' && !receiptModeLockCommitted" quaternary size="small" @click="abandonModeContinuation">{{ t('common.cancel') }}</n-button>

            <n-alert v-if="pageError" type="error" style="margin-bottom: 16px">
              {{ pageError }}
            </n-alert>

            <n-form label-placement="top" :disabled="isReadOnly">

              <!-- Quote header -->
              <n-card :title="t('quotes.headerSection')" style="margin-bottom: 16px">
                <n-grid :cols="2" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('quotes.customer')" required>
                      <n-select
                        v-model:value="customerId"
                        filterable
                        clearable
                        remote
                        :options="customerOptions"
                        :placeholder="t('quotes.customerSearch')"
                        @search="searchCustomers"
                        @update:value="handleCustomerSelect"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('quotes.referenceNumber')">
                      <n-input v-model:value="referenceNumber" :placeholder="t('quotes.referenceNumberPlaceholder')" clearable />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('quotes.quoteDate')" required>
                      <n-date-picker
                        v-model:formatted-value="quoteDate"
                        value-format="yyyy-MM-dd"
                        type="date"
                        clearable
                        style="width: 100%"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('quotes.validUntil')">
                      <n-date-picker
                        v-model:formatted-value="validUntil"
                        value-format="yyyy-MM-dd"
                        type="date"
                        clearable
                        style="width: 100%"
                      />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- VAT settings -->
              <n-card :title="t('quotes.vatSection')" style="margin-bottom: 16px">
                <n-grid :cols="2" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('quotes.taxMode')">
                      <n-select
                        v-model:value="taxMode"
                        :options="[
                          { label: t('quotes.taxModeLine'), value: 'LINE' },
                          { label: t('quotes.taxModeDocument'), value: 'DOCUMENT' },
                        ]"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('quotes.amountsIncludeVat')">
                      <n-switch v-model:value="amountsIncludeVat" />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('quotes.vatTreatment')">
                      <n-select
                        v-model:value="vatTreatmentId"
                        :options="vatTreatmentOptions"
                        :placeholder="t('quotes.vatTreatmentAuto')"
                        clearable
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi v-if="taxMode === 'DOCUMENT'">
                    <n-form-item :label="t('quotes.documentVatRate')" required>
                      <n-select
                        v-model:value="documentVatRateId"
                        :options="vatRateOptions"
                        :placeholder="t('quotes.selectVatRate')"
                        clearable
                      />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- Discount -->
              <n-card :title="t('quotes.discountSection')" style="margin-bottom: 16px">
                <n-grid :cols="3" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('quotes.discountType')">
                      <n-select v-model:value="discountType" :options="discountTypeOptions" />
                    </n-form-item>
                  </n-gi>
                  <n-gi v-if="discountType !== 'NONE'">
                    <n-form-item :label="discountType === 'PERCENTAGE' ? t('quotes.discountPercent') : t('quotes.discountFixed')">
                      <n-input-number v-model:value="discountValue" :min="0" :precision="3" />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- Content blocks + Notes -->
              <n-card :title="t('quotes.contentSection')" style="margin-bottom: 16px">
                <!-- Notes -->
                <n-form-item :label="t('quotes.notes')">
                  <div style="width: 100%">
                    <n-input
                      v-model:value="notes"
                      type="textarea"
                      :autosize="{ minRows: 2 }"
                      clearable
                    />
                    <n-button
                      v-if="!isReadOnly && noteTemplates.length > 0"
                      size="small"
                      secondary
                      style="margin-top: 4px"
                      @click="showNoteTemplateModal = true"
                    >
                      {{ t('quotes.insertNoteTemplate') }}
                    </n-button>
                  </div>
                </n-form-item>

                <!-- Warranty -->
                <n-form-item :label="t('quotes.warrantyText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['WARRANTY']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['WARRANTY'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('quotes.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('WARRANTY', v)"
                      />
                    </div>
                    <n-input v-model:value="warrantyText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>

                <!-- Terms -->
                <n-form-item :label="t('quotes.termsText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['TERMS']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['TERMS'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('quotes.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('TERMS', v)"
                      />
                    </div>
                    <n-input v-model:value="termsText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>

                <!-- Bank info -->
                <n-form-item :label="t('quotes.bankText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['BANK']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['BANK'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('quotes.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('BANK', v)"
                      />
                    </div>
                    <n-input v-model:value="bankText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>

                <!-- Payment terms -->
                <n-form-item :label="t('quotes.paymentTermsText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['PAYMENT_TERMS']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['PAYMENT_TERMS'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('quotes.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('PAYMENT_TERMS', v)"
                      />
                    </div>
                    <n-input v-model:value="paymentTermsText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>
              </n-card>

              <!-- Line items -->
              <n-card :title="t('quotes.linesSection')" style="margin-bottom: 16px">
                <template #header-extra>
                  <n-button
                    v-if="!isReadOnly && filteredTemplates.length > 0"
                    size="small"
                    secondary
                    @click="showTemplateModal = true"
                  >
                    <template #icon><n-icon><DocumentTextOutline /></n-icon></template>
                    {{ t('quotes.applyTemplate') }}
                  </n-button>
                </template>

                <LineOrdering v-model="lines" :disabled="isReadOnly || saving || pageLoading">
                  <template #default="{ line, index: i }">
                <div class="line-row">
                  <n-divider v-if="i > 0" />
                  <n-grid :cols="12" :x-gap="8" :y-gap="4">
                    <!-- Product selector -->
                    <n-gi :span="4">
                      <n-form-item :label="t('quotes.product')" size="small">
                        <n-select
                          :value="line.product_id"
                          filterable
                          clearable
                          remote
                          :options="productOptions.map(p => ({ label: p.name, value: p.id }))"
                          :placeholder="t('quotes.productSearch')"
                          @search="searchProducts"
                          @update:value="(v: string | null) => handleProductSelect(i, v)"
                        />
                      </n-form-item>
                    </n-gi>
                    <!-- Name -->
                    <n-gi :span="5">
                      <n-form-item :label="t('quotes.lineName')" size="small" required>
                        <n-input v-model:value="line.name" :placeholder="t('quotes.lineNamePlaceholder')" />
                      </n-form-item>
                    </n-gi>
                    <!-- Remove button -->
                    <n-gi :span="1" style="display: flex; align-items: flex-end; padding-bottom: 2px">
                      <n-button
                        v-if="lines.length > 1 && !isReadOnly"
                        size="small"
                        quaternary
                        circle
                        type="error"
                        @click="removeLine(i)"
                      >
                        <template #icon><n-icon><TrashOutline /></n-icon></template>
                      </n-button>
                    </n-gi>
                    <!-- Description -->
                    <n-gi :span="12">
                      <n-form-item :label="t('quotes.lineDescription')" size="small">
                        <n-input v-model:value="line.description" type="textarea" :autosize="{ minRows: 1, maxRows: 6 }" :placeholder="t('quotes.lineDescriptionPlaceholder')" clearable />
                      </n-form-item>
                    </n-gi>
                    <!-- Qty -->
                    <n-gi :span="2">
                      <n-form-item :label="t('quotes.quantity')" size="small">
                        <n-input-number v-model:value="line.quantity" :min="0.001" :precision="3" />
                      </n-form-item>
                    </n-gi>
                    <!-- Unit name -->
                    <n-gi :span="2">
                      <n-form-item :label="t('quotes.unit')" size="small">
                        <n-input v-model:value="line.unit_name" :placeholder="t('quotes.unitPlaceholder')" clearable />
                      </n-form-item>
                    </n-gi>
                    <!-- Unit price -->
                    <n-gi :span="3">
                      <n-form-item :label="amountsIncludeVat ? t('quotes.unitPriceIncl') : t('quotes.unitPriceExcl')" size="small">
                        <n-input-number v-model:value="line.unit_price" :min="0" :precision="3" />
                      </n-form-item>
                    </n-gi>
                    <!-- Discount type -->
                    <n-gi :span="2">
                      <n-form-item :label="t('quotes.lineDiscountType')" size="small">
                        <n-select v-model:value="line.discount_type" :options="discountTypeOptions" size="small" />
                      </n-form-item>
                    </n-gi>
                    <!-- Discount value -->
                    <n-gi v-if="line.discount_type !== 'NONE'" :span="2">
                      <n-form-item :label="t('quotes.lineDiscountValue')" size="small">
                        <n-input-number v-model:value="line.discount_value" :min="0" :precision="3" size="small" />
                      </n-form-item>
                    </n-gi>
                    <!-- VAT rate (LINE mode) -->
                    <n-gi v-if="taxMode === 'LINE'" :span="3">
                      <n-form-item :label="t('quotes.vatRate')" size="small">
                        <n-select
                          v-model:value="line.vat_rate_id"
                          :options="vatRateOptions"
                          :placeholder="t('quotes.selectVatRate')"
                          clearable
                          size="small"
                        />
                      </n-form-item>
                    </n-gi>
                  </n-grid>
                </div>
                  </template>
                </LineOrdering>

                <n-button
                  v-if="!isReadOnly"
                  dashed
                  block
                  style="margin-top: 12px"
                  @click="addLine"
                >
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('quotes.addLine') }}
                </n-button>
              </n-card>

              <!-- Preview totals -->
              <n-card :title="t('quotes.totalsSection')" style="margin-bottom: 16px">
                <div v-if="previewLoading" style="text-align:center; padding: 12px">
                  <n-spin />
                </div>
                <n-alert v-else-if="previewError" type="error">{{ previewError }}</n-alert>
                <div v-else-if="preview" class="totals-table">
                  <div class="total-row">
                    <span>{{ t('quotes.subtotalExclVat') }}</span>
                    <n-text>{{ fmtMoney(preview.subtotal_excl_vat) }}</n-text>
                  </div>
                  <div v-if="Number(preview.line_discount_total) !== 0" class="total-row">
                    <span>{{ t('quotes.lineDiscountTotal') }}</span>
                    <n-text>-{{ fmtMoney(preview.line_discount_total) }}</n-text>
                  </div>
                  <div v-if="Number(preview.document_discount_amount) !== 0" class="total-row">
                    <span>{{ t('quotes.documentDiscountAmount') }}</span>
                    <n-text>-{{ fmtMoney(preview.document_discount_amount) }}</n-text>
                  </div>
                  <div class="total-row">
                    <span>{{ t('quotes.taxableAmount') }}</span>
                    <n-text>{{ fmtMoney(preview.taxable_amount) }}</n-text>
                  </div>
                  <div class="total-row">
                    <span>{{ t('quotes.vatTotal') }}</span>
                    <n-text>{{ fmtMoney(preview.vat_total) }}</n-text>
                  </div>
                  <n-divider style="margin: 8px 0" />
                  <div class="total-row total-row--bold">
                    <span>{{ t('quotes.totalInclVat') }}</span>
                    <n-text strong>{{ fmtMoney(preview.total_incl_vat) }}</n-text>
                  </div>
                  <div v-if="preview.vat_treatment_snapshot" class="total-row" style="font-size: 12px; color: var(--n-text-color-3)">
                    <span>{{ t('quotes.vatTreatmentApplied') }}</span>
                    <span>{{ preview.vat_treatment_snapshot.code }}</span>
                  </div>
                </div>
                <n-text v-else depth="3">{{ t('quotes.totalsPlaceholder') }}</n-text>
              </n-card>

            </n-form>

            <QuotePaymentPanel
              v-if="isEdit && existingQuote && showReceiptContinuation"
              :quote-id="existingQuote.id"
              :quote-status="existingQuote.status"
              :converted-invoice-id="existingQuote.converted_invoice_id"
              :can-record-payment="canRecordQuotePaymentFromProjection"
              :customer-email="receiptCustomer?.email ?? null"
              :customer-locale="receiptCustomer?.locale ?? null"
              @payments-changed="handlePaymentsChanged"
              @receipt-sent="handleReceiptSent"
            />

            <!-- Email log (only for existing quotes) -->
            <EmailLogPanel
              v-if="isEdit && existingQuote"
              ref="emailLogPanelRef"
              doc-type="quote"
              :doc-id="existingQuote.id"
            />

            <!-- Action buttons -->
            <n-space justify="end" style="margin-bottom: 24px">
              <n-button @click="router.push('/quotes')">{{ t('quotes.backToList') }}</n-button>
              <n-button
                v-if="!isReadOnly"
                type="primary"
                :loading="saving"
                @click="handleSave"
              >
                {{ isEdit ? t('quotes.save') : t('quotes.create') }}
              </n-button>
            </n-space>

          </div>
    </n-spin>

    <!-- Document template picker modal -->
    <n-modal
      v-model:show="showTemplateModal"
      preset="card"
      :title="t('quotes.selectTemplateTitle')"
      style="max-width: 560px"
    >
      <n-list hoverable clickable>
        <n-list-item
          v-for="tmpl in filteredTemplates"
          :key="tmpl.id"
          @click="applyTemplate(tmpl)"
        >
          <n-thing :title="tmpl.name" :description="`${(tmpl.lines ?? []).length} ${t('quotes.templateLineCount')}`" />
        </n-list-item>
      </n-list>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showTemplateModal = false">{{ t('quotes.cancel') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Note template picker modal -->
    <n-modal
      v-model:show="showNoteTemplateModal"
      preset="card"
      :title="t('quotes.selectNoteTemplateTitle')"
      style="max-width: 480px"
    >
      <n-list hoverable clickable>
        <n-list-item
          v-for="nt in noteTemplates"
          :key="nt.id"
          @click="insertNoteTemplate(nt.body)"
        >
          <n-thing :title="nt.name" :description="nt.body.slice(0, 80) + (nt.body.length > 80 ? '…' : '')" />
        </n-list-item>
      </n-list>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showNoteTemplateModal = false">{{ t('quotes.cancel') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Reactivate modal -->
    <n-modal
      v-model:show="showReactivateModal"
      preset="card"
      :title="t('quotes.reactivateModalTitle')"
      style="max-width: 400px"
    >
      <n-form label-placement="top">
        <n-form-item :label="t('quotes.reactivateNewDate')" required>
          <n-date-picker
            v-model:formatted-value="reactivateDate"
            value-format="yyyy-MM-dd"
            type="date"
            :is-date-disabled="isDateDisabled"
            clearable
            style="width: 100%"
          />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showReactivateModal = false">{{ t('quotes.cancel') }}</n-button>
          <n-button
            type="primary"
            :loading="reactivating"
            :disabled="!reactivateDate"
            @click="confirmReactivate"
          >
            {{ t('quotes.confirm') }}
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Send email dialog (only for existing quotes) -->
    <DocumentSendDialog
      v-if="isEdit && existingQuote"
      v-model:show="sendDialogShow"
      doc-type="quote"
      :doc-id="existingQuote.id"
      :customer-email="selectedCustomer?.email ?? null"
      :customer-locale="selectedCustomer?.locale ?? null"
      @sent="handleSent"
    />

    <!-- PDF preview dialog -->
    <PdfPreviewDialog
      v-model:show="previewShow"
      :src="previewSrc"
      :fallback-filename="previewFallback"
    />
  </div>
</template>

<style scoped>
.quote-edit-container {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  gap: 12px;
}

.page-header h2 {
  margin: 0;
}

.totals-table {
  max-width: 380px;
  margin-left: auto;
}

.total-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
}

.total-row--bold {
  font-weight: 600;
  font-size: 16px;
}

.line-row {
  margin-bottom: 8px;
}

.billing-mode-card-grid-item {
  display: flex;
}

.billing-mode-card {
  width: 100%;
  height: 100%;
}

.billing-mode-card :deep(.n-card__content) {
  display: flex;
  height: 100%;
}

.billing-mode-card-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 132px;
}

.billing-mode-card-title,
.billing-mode-card-description {
  display: block;
}

.billing-mode-card-description {
  margin-top: 8px;
  line-height: 1.5;
}

.billing-mode-card-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: auto;
  padding-top: 20px;
}
</style>
