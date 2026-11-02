<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import LineOrdering from '../../components/LineOrdering.vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { localDateStr } from '../../utils/date'
import {
  useMessage, useDialog,
  NButton, NSpace, NInput, NForm, NFormItem, NCard, NSpin, NAlert,
  NDivider, NInputNumber, NSelect, NSwitch, NTag, NDatePicker,
  NGrid, NGi, NText, NModal, NList, NListItem, NThing, NDropdown, NEmpty, NCheckbox,
} from 'naive-ui'
import { AddOutline, TrashOutline, DocumentTextOutline, DownloadOutline, MailOutline, EyeOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import InvoicePaymentPanel from '../../components/InvoicePaymentPanel.vue'
import DocumentWorkflowPanel from '../../components/DocumentWorkflowPanel.vue'
import DocumentSendDialog from '../../components/DocumentSendDialog.vue'
import PdfPreviewDialog from '../../components/PdfPreviewDialog.vue'
import EmailLogPanel from '../../components/EmailLogPanel.vue'
import { useInvoicesStore } from '../../stores/invoices'
import type { InvoicePaymentsResponse } from '../../stores/payments'
import { ApiError, get, downloadBlob } from '../../api/http'
import {
  createDocumentChainPaymentChangeHandler,
  useDocumentChainRefresh,
} from '../../composables/useDocumentChainRefresh'
import type { components } from '../../api/schema'
import { persistedReceiptCustomer, receiptAuditTarget } from '../../utils/receiptEmail'
import { invoiceDocumentKindLabelKey } from '../../utils/documentKind'
import { invoiceDocumentSendType } from '../../utils/documentSend'
import { useRouteEntityReload } from '../../composables/useRouteEntityReload'

type CustomerRead = components['schemas']['CustomerRead']
type VatRateRead = components['schemas']['VatRateRead']
type VatTreatmentRead = components['schemas']['VatTreatmentRead']
type ProductInvoiceOptionRead = components['schemas']['ProductInvoiceOptionRead']
type InvoiceCalculationRead = components['schemas']['InvoiceCalculationRead']
type InvoiceWrite = components['schemas']['InvoiceWrite']
type InvoiceRead = components['schemas']['InvoiceRead']
type DocumentChainRead = components['schemas']['DocumentChainRead']
type DocumentTemplateRead = components['schemas']['DocumentTemplateRead']
type ContentBlockRead = components['schemas']['ContentBlockRead']
type NoteTemplateRead = components['schemas']['NoteTemplateRead']
type EmailLogRead = components['schemas']['EmailLogRead']
type InvoiceRevisionChanges = components['schemas']['InvoiceRevisionChanges']
type InvoiceRevisionRead = components['schemas']['InvoiceRevisionRead']

let lineKeyCounter = 0
function nextLineKey() { return `l${++lineKeyCounter}` }

interface LineRow {
  _key: string
  _lineId?: string
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
  _persistedNumeric?: {
    quantity: { raw: string; input: number }
    unit_price: { raw: string; input: number }
    discount_value: { raw: string; input: number }
  }
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
const store = useInvoicesStore()

const isEdit = ref(false)
const pageLoading = ref(false)
const saving = ref(false)
const revisionMode = ref(false)
const eligibilityChecking = ref(false)
const revisionReason = ref('')
const confirmUndelivered = ref(false)
const revisionHistory = ref<InvoiceRevisionRead[]>([])
const historyLoading = ref(false)
const historyError = ref(false)
let historyGeneration = 0
let eligibilityGeneration = 0
const pageError = ref<string | null>(null)
const existingInvoice = ref<InvoiceRead | null>(null)
const {
  documentChain,
  chainRefreshing,
  initialChainError,
  paymentRefreshError,
  revisionRefreshError,
  loadInitialDocumentChain,
  refreshAfterPayment,
  refreshAfterRevision,
  resetDocumentChain,
} = useDocumentChainRefresh<DocumentChainRead>(async () => {
  const invoiceId = existingInvoice.value?.id
  if (!invoiceId) throw new Error('Invoice document chain is unavailable')
  return get<DocumentChainRead>(`/api/v1/invoices/${invoiceId}/document-chain`)
})

// Invoice header fields.
const customerId = ref<string | null>(null)
const customers = ref<CustomerRead[]>([])
// Derive the selected customer from the form id and the asynchronously loaded
// options.  Keeping a separate ref here left existing invoices without an
// email recipient when their customer was appended after the initial search.
const selectedCustomer = computed(() =>
  customers.value.find(customer => customer.id === customerId.value) ?? null,
)

const referenceNumber = ref<string | null>(null)
const invoiceDate = ref(localDateStr(new Date()))
const dueDate = ref<string | null>(null)
const supplyOrAdvanceDate = ref<string | null>(null)
const taxMode = ref<'LINE' | 'DOCUMENT'>('LINE')
const amountsIncludeVat = ref(false)
const vatTreatmentId = ref<string | null>(null)
const documentVatRateId = ref<string | null>(null)
const discountType = ref<'NONE' | 'PERCENTAGE' | 'FIXED'>('NONE')
const discountValue = ref(0)
const persistedDocumentDiscount = ref<{ raw: string; input: number } | null>(null)
const notes = ref<string | null>(null)
const lines = ref<LineRow[]>([emptyLine()])

// Reference data.
const vatRates = ref<VatRateRead[]>([])
const vatTreatments = ref<VatTreatmentRead[]>([])
const productOptions = ref<ProductInvoiceOptionRead[]>([])

// Content block / template state
const warrantyText = ref<string | null>(null)
const termsText = ref<string | null>(null)
const bankText = ref<string | null>(null)
const paymentTermsText = ref<string | null>(null)
const documentTemplates = ref<DocumentTemplateRead[]>([])
const contentBlocks = ref<ContentBlockRead[]>([])
const noteTemplates = ref<NoteTemplateRead[]>([])
const showTemplateModal = ref(false)
const showNoteTemplateModal = ref(false)

// Preview calculation result.
const preview = ref<InvoiceCalculationRead | null>(null)
const previewLoading = ref(false)
const previewError = ref<string | null>(null)

// ------------------------------------------------------------------ fetch ref data

async function loadReferenceData(isCurrent: () => boolean) {
  const [ratesRes, treatsRes] = await Promise.all([
    get<{ items: VatRateRead[] }>('/api/v1/vat-rates'),
    get<{ items: VatTreatmentRead[] }>('/api/v1/vat-treatments?side=SALES'),
  ])
  if (!isCurrent()) return
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

const filteredTemplates = computed(() =>
  documentTemplates.value.filter(t => t.applies_to === 'INVOICE' || t.applies_to === 'BOTH')
)

function applyTemplate(template: DocumentTemplateRead) {
  if (saving.value) return
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

function insertNoteTemplate(body: string) {
  if (saving.value) return
  notes.value = notes.value ? `${notes.value}\n${body}` : body
  showNoteTemplateModal.value = false
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
  if (saving.value) return
  const block = contentBlocks.value.find(b => b.id === id)
  if (!block) return
  if (kind === 'WARRANTY') warrantyText.value = block.body
  if (kind === 'TERMS') termsText.value = block.body
  if (kind === 'BANK') bankText.value = block.body
  if (kind === 'PAYMENT_TERMS') paymentTermsText.value = block.body
}

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

async function searchCustomers(q: string) {
  const res = await get<{ items: CustomerRead[] }>(`/api/v1/customers?q=${encodeURIComponent(q)}&limit=20`)
  customers.value = res.items
}

async function searchProducts(q: string) {
  productOptions.value = await store.fetchProductOptions(q)
}

// ------------------------------------------------------------------ customer selection

const customerOptions = computed(() => customers.value.map(c => ({
  label: c.name,
  value: c.id,
})))

function handleCustomerSelect(id: string | null) {
  customerId.value = id
  // Auto-derive VAT treatment based on customer
  vatTreatmentId.value = null
}

// ------------------------------------------------------------------ VAT rate options

const vatRateOptions = computed(() => vatRates.value.map(r => ({
  label: `${r.label} (${r.percent}%)`,
  value: r.id,
})))

function withFrozenRateOption(snapshot: {
  vat_rate_id?: string | null
  vat_rate_label?: string | null
  vat_rate_percent?: string | number | null
} | undefined) {
  const options = vatRateOptions.value
  if (!snapshot?.vat_rate_id || snapshot.vat_rate_label == null || snapshot.vat_rate_percent == null) return options
  const frozen = { value: snapshot.vat_rate_id, label: `${snapshot.vat_rate_label} (${snapshot.vat_rate_percent}%)` }
  return options.some(option => option.value === frozen.value)
    ? options.map(option => option.value === frozen.value ? frozen : option)
    : [...options, frozen]
}

function lineVatRateOptions(line: LineRow) {
  const current = existingInvoice.value
  if (!current || current.status === 'DRAFT') return vatRateOptions.value
  const saved = current.lines?.find(row => row.id === line._lineId)
  // Retained selections use their own frozen label/rate, including when two
  // lines share a dictionary ID but have different historical percentages.
  return saved && saved.vat_rate_id === line.vat_rate_id
    ? withFrozenRateOption(saved)
    : vatRateOptions.value
}

const documentVatRateOptions = computed(() => {
  const current = existingInvoice.value
  if (!current || current.status === 'DRAFT' || !current.document_vat_rate_id
    || documentVatRateId.value !== current.document_vat_rate_id) return vatRateOptions.value
  return withFrozenRateOption(current.taxes?.find(tax => tax.vat_rate_id === current.document_vat_rate_id))
})

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
  if (saving.value) return
  lines.value.push(emptyLine())
}

function removeLine(i: number) {
  if (saving.value) return
  if (lines.value.length > 1) {
    lines.value.splice(i, 1)
  }
}

function handleProductSelect(i: number, productId: string | null) {
  if (saving.value) return
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
    invoice_date: invoiceDate.value,
    due_date: dueDate.value ?? undefined,
    supply_or_advance_date: supplyOrAdvanceDate.value ?? undefined,
    tax_mode: taxMode.value,
    amounts_include_vat: amountsIncludeVat.value,
    vat_treatment_id: vatTreatmentId.value ?? undefined,
    document_vat_rate_id: documentVatRateId.value ?? undefined,
    discount: { type: discountType.value, value: String(discountValue.value) },
    lines: linesPayload,
  }
}

function buildRevisionChanges(): InvoiceRevisionChanges | null {
  const original = existingInvoice.value
  if (!original) return null
  const changes: InvoiceRevisionChanges = {}
  const clearable = (value: unknown) => value === '' ? null : value
  // Compare raw numeric inputs without calculating invoice amounts.
  const canonicalRawNumber = (value: unknown): string => {
    const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(value ?? '').trim())
    if (!match) return String(value ?? '').trim()
    const integer = match[2]!.replace(/^0+(?=\d)/, '')
    const fraction = (match[3] ?? '').replace(/0+$/, '')
    return `${match[1] === '-' && (integer !== '0' || fraction) ? '-' : ''}${integer}${fraction ? `.${fraction}` : ''}`
  }
  const sameRawNumber = (left: unknown, right: unknown) => canonicalRawNumber(left) === canonicalRawNumber(right)
  const rawOrEditedNumber = (value: number, persisted?: { raw: string; input: number }) =>
    persisted && value === persisted.input ? persisted.raw : canonicalRawNumber(value)
  const documentDiscountRaw = rawOrEditedNumber(discountValue.value, persistedDocumentDiscount.value ?? undefined)
  const pairs: Array<[keyof InvoiceRevisionChanges, unknown, unknown]> = [
    ['invoice_date', original.invoice_date, invoiceDate.value], ['due_date', original.due_date ?? null, dueDate.value],
    ['supply_or_advance_date', original.supply_or_advance_date ?? null, supplyOrAdvanceDate.value],
    ['reference_number', original.reference_number ?? null, referenceNumber.value], ['notes', original.notes ?? null, notes.value],
    ['warranty_text', original.warranty_text ?? null, warrantyText.value], ['terms_text', original.terms_text ?? null, termsText.value],
    ['bank_text', original.bank_text ?? null, bankText.value], ['payment_terms_text', original.payment_terms_text ?? null, paymentTermsText.value],
    ['tax_mode', original.tax_mode, taxMode.value], ['amounts_include_vat', original.amounts_include_vat, amountsIncludeVat.value],
    ['vat_treatment_id', original.vat_treatment_id, vatTreatmentId.value], ['document_vat_rate_id', original.document_vat_rate_id ?? null, documentVatRateId.value],
    ['discount', { type: original.discount_type, value: String(original.discount_value) }, { type: discountType.value, value: documentDiscountRaw }],
  ]
  for (const [key, before, after] of pairs) {
    const normalized = ['due_date', 'supply_or_advance_date', 'reference_number', 'notes', 'warranty_text', 'terms_text', 'bank_text', 'payment_terms_text', 'vat_treatment_id', 'document_vat_rate_id'].includes(String(key))
      ? clearable(after) : after
    const equal = key === 'discount'
      ? (before as { type: string; value: string }).type === (normalized as { type: string; value: string }).type
        && sameRawNumber((before as { value: string }).value, (normalized as { value: string }).value)
      : JSON.stringify(before) === JSON.stringify(normalized)
    if (!equal) Object.assign(changes, { [key]: normalized })
  }
  const originalLines = original.lines ?? []
  const nextLines = lines.value.filter(l => l.name.trim()).map(l => {
    const row: components['schemas']['InvoiceRevisionLineInput'] = {
      name: l.name.trim(), quantity: rawOrEditedNumber(l.quantity, l._persistedNumeric?.quantity), unit_price: rawOrEditedNumber(l.unit_price, l._persistedNumeric?.unit_price),
      product_id: l.product_id, description: clearable(l.description) as string | null, unit_id: l.unit_id,
      unit_name: l.unit_name, discount: { type: l.discount_type, value: rawOrEditedNumber(l.discount_value, l._persistedNumeric?.discount_value) }, vat_rate_id: l.vat_rate_id,
    }
    if (l._lineId) row.line_id = l._lineId
    return row
  })
  const oldRows = originalLines.map(l => ({
    id: l.id, product_id: l.product_id ?? null, name: l.name, description: l.description ?? null,
    quantity: String(l.quantity), unit_id: l.unit_id ?? null, unit_name: l.unit_name ?? null,
    unit_price: String(l.unit_price), discount_type: l.discount_type, discount_value: String(l.discount_value), vat_rate_id: l.vat_rate_id ?? null,
  }))
  const newRows = lines.value.filter(l => l.name.trim()).map(l => ({
    id: l._lineId ?? null, product_id: l.product_id, name: l.name.trim(), description: l.description,
    quantity: rawOrEditedNumber(l.quantity, l._persistedNumeric?.quantity), unit_id: l.unit_id, unit_name: l.unit_name,
    unit_price: rawOrEditedNumber(l.unit_price, l._persistedNumeric?.unit_price), discount_type: l.discount_type,
    discount_value: rawOrEditedNumber(l.discount_value, l._persistedNumeric?.discount_value), vat_rate_id: l.vat_rate_id,
  }))
  if (JSON.stringify(oldRows) !== JSON.stringify(newRows)) changes.lines = nextLines
  return changes
}

const revisionChanges = computed(() => revisionMode.value ? buildRevisionChanges() : null)
const hasRevisionChanges = computed(() => Object.keys(revisionChanges.value ?? {}).length > 0)
const revisionDirty = computed(() => hasRevisionChanges.value || Boolean(revisionReason.value.trim()) || confirmUndelivered.value)
function reportPeriod(date: string | null | undefined): string {
  if (!date) return '—'
  const [year, month] = date.split('-').map(Number)
  return `Q${Math.ceil(month / 3)} ${year}`
}
const crossesReportPeriod = computed(() => Boolean(revisionMode.value && existingInvoice.value
  && invoiceDate.value !== existingInvoice.value.invoice_date
  && reportPeriod(invoiceDate.value) !== reportPeriod(existingInvoice.value.invoice_date)))
function displayRevisionValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return t('invoices.revision.emptyValue')
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? t('vat.yes') : t('vat.no')
  return String(value)
}
function displayRevisionField(key: string, value: unknown, snapshot?: Record<string, unknown>): string {
  if (key === 'vat_treatment_id' && typeof value === 'string' && snapshot) {
    const treatment = snapshot.vat_treatment_snapshot as Record<string, unknown> | undefined
    const code = snapshot.vat_treatment_code ?? treatment?.code
    const label = snapshot.vat_treatment_label ?? treatment?.label
    if (typeof code === 'string' && typeof label === 'string') return `${code} – ${label}`
  }
  if (key === 'document_vat_rate_id' && typeof value === 'string' && snapshot) {
    const taxes = snapshot.taxes ?? snapshot.document_taxes
    if (Array.isArray(taxes)) {
      const tax = (taxes as Array<Record<string, unknown>>).find(item => item.vat_rate_id === value)
      if (tax && typeof tax.vat_rate_label === 'string' && typeof tax.vat_rate_percent === 'string') {
        return `${tax.vat_rate_label} (${tax.vat_rate_percent}%)`
      }
    }
  }
  return displayRevisionValue(value)
}
const revisionSummary = computed(() => {
  const changes = revisionChanges.value
  const original = existingInvoice.value
  if (!changes || !original) return []
  const labels: Record<string, string> = {
    invoice_date: 'invoiceDate', due_date: 'dueDate', supply_or_advance_date: 'supplyDate', reference_number: 'reference', notes: 'notes',
    warranty_text: 'warranty', terms_text: 'terms', bank_text: 'bank', payment_terms_text: 'paymentTerms',
    tax_mode: 'taxMode', amounts_include_vat: 'amountsIncludeVat', vat_treatment_id: 'vatTreatment',
    document_vat_rate_id: 'documentVatRate', discount: 'discount', lines: 'lines',
  }
  const before: Record<string, unknown> = {
    invoice_date: original.invoice_date, due_date: original.due_date, supply_or_advance_date: original.supply_or_advance_date, reference_number: original.reference_number,
    notes: original.notes, warranty_text: original.warranty_text, terms_text: original.terms_text, bank_text: original.bank_text,
    payment_terms_text: original.payment_terms_text, tax_mode: original.tax_mode, amounts_include_vat: original.amounts_include_vat,
    vat_treatment_id: original.vat_treatment_id, document_vat_rate_id: original.document_vat_rate_id,
    discount: `${original.discount_type} ${original.discount_value}`,
  }
  return Object.entries(changes).map(([key, value]) => ({
    key, label: t(`invoices.revision.field.${labels[key] ?? key}`),
    before: key === 'lines'
      ? (original.lines ?? []).map((line, index) => `${index + 1}. ${line.name} · ${line.description ?? t('invoices.revision.emptyValue')} · ${line.quantity} × ${line.unit_price} · ${line.unit_name ?? t('invoices.revision.emptyValue')} · ${t('invoices.revision.field.discount')}: ${line.discount_type} ${line.discount_value} · ${t('invoices.revision.field.vatTreatment')}: ${line.vat_rate_id ?? t('invoices.revision.emptyValue')} · ${t('invoices.revision.lineTotal')}: ${line.total_incl_vat ?? t('invoices.revision.emptyValue')}`).join('; ')
      : displayRevisionField(key, before[key], original as unknown as Record<string, unknown>),
    after: key === 'lines'
      ? (value as components['schemas']['InvoiceRevisionLineInput'][]).map((line, index) => {
        const currentRows = lines.value.filter(row => row.name.trim())
        const calc = lineCalcMap.value.get(currentRows[index]?._key ?? '')
        return `${index + 1}. ${line.name} · ${line.description ?? t('invoices.revision.emptyValue')} · ${line.quantity} × ${line.unit_price} · ${line.unit_name ?? t('invoices.revision.emptyValue')} · ${t('invoices.revision.field.discount')}: ${line.discount?.type ?? 'NONE'} ${line.discount?.value ?? '0'} · ${t('invoices.revision.field.vatTreatment')}: ${line.vat_rate_id ?? t('invoices.revision.emptyValue')} · ${t('invoices.revision.lineTotal')}: ${calc?.total_incl_vat ?? t('invoices.revision.previewPending')}`
      }).join('; ')
      : displayRevisionField(key, value, preview.value as unknown as Record<string, unknown> | undefined),
  }))
})
function historyRows(revision: InvoiceRevisionRead) {
  const before = revision.before_snapshot.invoice as unknown as Record<string, unknown>
  const after = revision.after_snapshot.invoice as unknown as Record<string, unknown>
  const fields = ['invoice_date', 'due_date', 'supply_or_advance_date', 'reference_number', 'notes', 'warranty_text',
    'terms_text', 'bank_text', 'payment_terms_text', 'tax_mode', 'amounts_include_vat', 'vat_treatment_id',
    'vat_treatment_code', 'vat_treatment_label', 'vat_treatment_effect', 'vat_treatment_requires_icp',
    'document_vat_rate_id', 'discount_type', 'discount_value', 'document_discount_amount', 'line_discount_total',
    'subtotal_excl_vat', 'taxable_amount', 'vat_total', 'total_incl_vat', 'due_amount']
  return fields.filter(key => JSON.stringify(before[key] ?? null) !== JSON.stringify(after[key] ?? null)).map(key => ({
    key, label: t(`invoices.revision.field.${({
      invoice_date: 'invoiceDate', due_date: 'dueDate', supply_or_advance_date: 'supplyDate', reference_number: 'reference',
      warranty_text: 'warranty', terms_text: 'terms', bank_text: 'bank', payment_terms_text: 'paymentTerms',
      tax_mode: 'taxMode', amounts_include_vat: 'amountsIncludeVat', vat_treatment_id: 'vatTreatment',
      vat_treatment_code: 'vatTreatmentCode', vat_treatment_label: 'vatTreatmentLabel', vat_treatment_effect: 'vatTreatmentEffect',
      vat_treatment_requires_icp: 'vatTreatmentIcp', document_vat_rate_id: 'documentVatRate',
      discount_type: 'discountType', discount_value: 'discount', document_discount_amount: 'documentDiscount',
      line_discount_total: 'lineDiscount', subtotal_excl_vat: 'subtotal', taxable_amount: 'taxable',
      vat_total: 'vat', total_incl_vat: 'total', due_amount: 'dueAmount', notes: 'notes',
    } as Record<string, string>)[key] ?? key}`),
    before: displayRevisionField(key, before[key], before), after: displayRevisionField(key, after[key], after),
  }))
}
function historyLines(revision: InvoiceRevisionRead, side: 'before_snapshot' | 'after_snapshot') {
  const snapshot = revision[side].invoice as unknown as Record<string, unknown>
  const rows = (snapshot.lines ?? []) as Array<Record<string, unknown>>
  return rows.map((line, index) => {
    const parts = [
      `${index + 1}. ${String(line.name ?? '')}`,
      line.description ? String(line.description) : null,
      line.unit_name ? String(line.unit_name) : null,
      `${String(line.quantity ?? '')} × ${String(line.unit_price ?? '')}`,
      line.discount_type && line.discount_type !== 'NONE' ? `${t('invoices.revision.field.discount')}: ${line.discount_type} ${String(line.discount_value ?? '')}` : null,
      line.vat_rate_label ? `${t('invoices.revision.field.vatTreatment')}: ${String(line.vat_rate_label)} (${String(line.vat_rate_percent ?? '')}%)` : null,
      Array.isArray(line.line_taxes) && line.line_taxes.length
        ? (line.line_taxes as Array<Record<string, unknown>>).map(tax => `${String(tax.vat_rate_label ?? '')} ${String(tax.effective_vat_percent ?? '')}%: ${String(tax.tax_amount ?? '')}`).join(', ')
        : null,
      `${t('invoices.revision.lineTotal')}: ${String(line.total_incl_vat ?? '')}`,
    ].filter(Boolean)
    return parts.join(' · ')
  })
}

let previewTimer: ReturnType<typeof setTimeout> | null = null
let previewVersion = 0
// Snapshot the row identities alongside each request; never match results to current indices.
const lineCalcMap = ref(new Map<string, InvoiceCalculationRead['lines'][number]>())
const isPersistedReadOnlyInvoice = computed(() => isEdit.value && !revisionMode.value
  && existingInvoice.value != null && existingInvoice.value.status !== 'DRAFT')
const displayPreview = computed(() => isPersistedReadOnlyInvoice.value && existingInvoice.value
  ? existingInvoice.value as unknown as InvoiceCalculationRead
  : preview.value)
function schedulePreview() {
  const version = ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
  preview.value = null
  previewError.value = null
  lineCalcMap.value = new Map()
  previewLoading.value = false
  if (isPersistedReadOnlyInvoice.value && existingInvoice.value) {
    preview.value = null
    const persistedLineCalculations = new Map<string, InvoiceCalculationRead['lines'][number]>()
    for (const [index, line] of (existingInvoice.value.lines ?? []).entries()) {
      const key = lines.value[index]?._key
      if (key) persistedLineCalculations.set(key, line as unknown as InvoiceCalculationRead['lines'][number])
    }
    lineCalcMap.value = persistedLineCalculations
    return
  }
  previewTimer = setTimeout(async () => {
    const req = buildCalculationRequest()
    const keys = lines.value.filter(l => l.name.trim()).map(l => l._key)
    if (!req) return
    previewLoading.value = true
    try {
      const result = revisionMode.value && existingInvoice.value
        ? await store.calculateRevision(existingInvoice.value.id, {
          expected_updated_at: existingInvoice.value.updated_at,
          changes: buildRevisionChanges() ?? {},
        })
        : await store.calculatePreview(req as Parameters<typeof store.calculatePreview>[0])
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
  [customerId, referenceNumber, invoiceDate, dueDate, supplyOrAdvanceDate, taxMode, amountsIncludeVat, vatTreatmentId, documentVatRateId,
    discountType, discountValue, notes, warrantyText, termsText, bankText, paymentTermsText, lines],
  schedulePreview,
  { deep: true },
)

// ------------------------------------------------------------------ save

function buildWritePayload(): InvoiceWrite | null {
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
    invoice_date: invoiceDate.value,
    due_date: dueDate.value ?? undefined,
    supply_or_advance_date: supplyOrAdvanceDate.value ?? undefined,
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
  if (revisionMode.value) {
    await handleSaveRevision()
    return
  }
  // Formal documents intentionally do not share the legacy invoice write
  // contract.  Their dedicated controls in DocumentWorkflowPanel submit raw
  // intent to the kind-specific endpoints.
  if (existingInvoice.value?.document_kind === 'ADVANCE' || existingInvoice.value?.document_kind === 'CREDIT_NOTE') return
  const payload = buildWritePayload()
  if (!payload) {
    message.error(t('invoices.validationError'))
    return
  }
  saving.value = true
  try {
    if (isEdit.value && existingInvoice.value) {
      await store.updateInvoice(existingInvoice.value.id, payload)
      message.success(t('invoices.updateSuccess'))
    } else {
      await store.createInvoice(payload)
      message.success(t('invoices.createSuccess'))
    }
    router.push('/invoices')
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('invoices.saveFailed'))
  } finally {
    saving.value = false
  }
}

function confirmDiscard(): Promise<boolean> {
  if (!revisionMode.value || !revisionDirty.value) return Promise.resolve(true)
  return new Promise(resolve => dialog.warning({
    title: t('invoices.revision.unsavedTitle'), content: t('invoices.revision.unsavedContent'),
    positiveText: t('invoices.revision.discard'), negativeText: t('invoices.revision.keepEditing'),
    onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false), onClose: () => resolve(false),
  }))
}

async function cancelRevision(): Promise<void> {
  if (!await confirmDiscard()) return
  revisionMode.value = false
  revisionReason.value = ''
  confirmUndelivered.value = false
  if (existingInvoice.value) populateFromInvoice(existingInvoice.value)
  schedulePreview()
}

async function enterRevision(): Promise<void> {
  const current = existingInvoice.value
  if (!current?.revision_eligibility?.available || revisionMode.value || eligibilityChecking.value || saving.value) return
  const context = ++eligibilityGeneration
  const invoiceId = current.id
  eligibilityChecking.value = true
  try {
    const [fresh] = await Promise.all([
      store.fetchInvoice(invoiceId),
      loadReferenceData(() => context === eligibilityGeneration
        && existingInvoice.value?.id === invoiceId && route.params.id === invoiceId),
    ])
    if (context !== eligibilityGeneration || existingInvoice.value?.id !== invoiceId || route.params.id !== invoiceId) return
    populateFromInvoice(fresh)
    if (!fresh.revision_eligibility?.available) {
      message.warning(t('invoices.revision.noLongerEligible'))
      return
    }
    revisionMode.value = true
  } catch (e: unknown) {
    if (context === eligibilityGeneration && existingInvoice.value?.id === invoiceId && route.params.id === invoiceId) {
      message.error(e instanceof Error ? e.message : t('invoices.revision.eligibilityFailed'))
    }
    return
  } finally {
    if (context === eligibilityGeneration) eligibilityChecking.value = false
  }
  revisionReason.value = ''
  confirmUndelivered.value = false
  schedulePreview()
}

async function handleSaveRevision(): Promise<void> {
  const inv = existingInvoice.value
  const changes = buildRevisionChanges()
  if (!inv || !changes || !hasRevisionChanges.value || saving.value || !revisionReason.value.trim() || !confirmUndelivered.value) return
  saving.value = true
  showTemplateModal.value = false
  showNoteTemplateModal.value = false
  const saveContext = ++saveGeneration
  const invoiceId = inv.id
  try {
    const updated = await store.saveRevision(invoiceId, {
      expected_updated_at: inv.updated_at,
      changes,
      reason: revisionReason.value.trim(),
      confirm_not_delivered: true,
    })
    if (saveContext !== saveGeneration || existingInvoice.value?.id !== invoiceId || route.params.id !== invoiceId) return
    populateFromInvoice(updated)
    revisionMode.value = false
    revisionReason.value = ''
    confirmUndelivered.value = false
    message.success(t('invoices.revision.saved'))
    // Saving has committed. Refresh each parent-owned/current projection as a
    // follow-up read; a failure must not turn the successful revision into a
    // save error or overwrite the retained current invoice/history.
    await refreshAfterRevision()
    await loadRevisionHistory()
  } catch (e: unknown) {
    if (saveContext !== saveGeneration || existingInvoice.value?.id !== invoiceId || route.params.id !== invoiceId) return
    message.error(e instanceof ApiError && e.status === 409
      ? t('invoices.revision.conflict')
      : e instanceof Error ? e.message : t('invoices.revision.saveFailed'))
  } finally {
    if (saveContext === saveGeneration && existingInvoice.value?.id === invoiceId && route.params.id === invoiceId) saving.value = false
  }
}

async function loadRevisionHistory(): Promise<void> {
  const id = existingInvoice.value?.id
  if (!id) return
  const generation = ++historyGeneration
  historyLoading.value = true
  historyError.value = false
  try {
    const allItems: InvoiceRevisionRead[] = []
    let offset = 0
    let total = 0
    do {
      const result = await store.fetchRevisions(id, { offset, limit: 100 })
      if (generation !== historyGeneration || existingInvoice.value?.id !== id) return
      allItems.push(...result.items)
      total = result.total
      offset += result.items.length
      if (!result.items.length) break
    } while (offset < total)
    if (generation === historyGeneration && existingInvoice.value?.id === id) revisionHistory.value = allItems
  } catch {
    if (generation === historyGeneration && existingInvoice.value?.id === id) historyError.value = true
  } finally {
    if (generation === historyGeneration) historyLoading.value = false
  }
}

onBeforeRouteLeave(async () => await confirmDiscard())
onBeforeRouteUpdate(async (to, from) => to.params.id === from.params.id ? true : await confirmDiscard())

async function handleStatusTransition(newStatus: 'SENT' | 'CANCELLED' | 'DRAFT') {
  if (!existingInvoice.value) return
  try {
    await store.transitionStatus(existingInvoice.value.id, { status: newStatus })
    message.success(t('invoices.statusUpdated'))
    router.push('/invoices')
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('invoices.statusFailed'))
  }
}

// ------------------------------------------------------------------ load existing invoice

function populateFromInvoice(inv: InvoiceRead) {
  existingInvoice.value = inv
  customerId.value = inv.customer_id
  referenceNumber.value = inv.reference_number ?? null
  invoiceDate.value = inv.invoice_date
  dueDate.value = inv.due_date ?? null
  supplyOrAdvanceDate.value = inv.supply_or_advance_date ?? null
  taxMode.value = inv.tax_mode
  amountsIncludeVat.value = inv.amounts_include_vat
  vatTreatmentId.value = inv.vat_treatment_id
  documentVatRateId.value = inv.document_vat_rate_id ?? null
  discountType.value = inv.discount_type
  discountValue.value = Number(inv.discount_value)
  persistedDocumentDiscount.value = { raw: String(inv.discount_value), input: Number(inv.discount_value) }
  notes.value = inv.notes ?? null
  warrantyText.value = inv.warranty_text ?? null
  termsText.value = inv.terms_text ?? null
  bankText.value = inv.bank_text ?? null
  paymentTermsText.value = inv.payment_terms_text ?? null
  lines.value = (inv.lines ?? []).map(l => ({
    _key: nextLineKey(),
    _lineId: l.id,
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
    _persistedNumeric: {
      quantity: { raw: String(l.quantity), input: Number(l.quantity) },
      unit_price: { raw: String(l.unit_price), input: Number(l.unit_price) },
      discount_value: { raw: String(l.discount_value), input: Number(l.discount_value) },
    },
  }))
}

let loadGeneration = 0
let saveGeneration = 0

function resetInvoiceContext(): void {
  ++eligibilityGeneration
  eligibilityChecking.value = false
  ++saveGeneration
  saving.value = false
  ++previewVersion
  ++historyGeneration
  if (previewTimer) clearTimeout(previewTimer)
  previewLoading.value = false
  lineCalcMap.value = new Map()
  resetDocumentChain()
  pageLoading.value = false
  existingInvoice.value = null
  persistedDocumentDiscount.value = null
  revisionMode.value = false
  revisionHistory.value = []
  historyLoading.value = false
  historyError.value = false
  preview.value = null
  previewError.value = null
  pageError.value = null
  customerId.value = null
  lines.value = [emptyLine()]
}

async function loadPage(id = route.params.id as string | undefined): Promise<void> {
  const generation = ++loadGeneration
  pageLoading.value = true
  try {
    await Promise.all([
      loadReferenceData(() => generation === loadGeneration),
      loadContentLibraries(),
      searchCustomers(''),
      store.fetchProductOptions().then(opts => { productOptions.value = opts }),
    ])

    if (id && id !== 'new') {
      isEdit.value = true
      const inv = await store.fetchInvoice(id)
      if (generation !== loadGeneration) return
      populateFromInvoice(inv)
      if (inv.document_kind === 'STANDARD') void loadRevisionHistory()
      await loadInitialDocumentChain()
      if (generation !== loadGeneration) return
      // Ensure the selected customer is in the list even if not returned by the initial search.
      if (!customers.value.find(c => c.id === inv.customer_id)) {
        const custRes = await get<CustomerRead>(`/api/v1/customers/${inv.customer_id}`)
        if (generation !== loadGeneration) return
        customers.value = [custRes, ...customers.value]
      }
    } else {
      isEdit.value = false
      applyDefaultContentBlocks()
    }
  } catch (e: unknown) {
    if (generation === loadGeneration) pageError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (generation === loadGeneration) pageLoading.value = false
  }
}

const routeEntityId = computed(() => typeof route.params.id === 'string' ? route.params.id : undefined)
const routeEntityReload = useRouteEntityReload(routeEntityId, resetInvoiceContext, async (id) => {
  await loadPage(id)
})
onMounted(() => { void routeEntityReload.reload() })

const isReadOnly = computed(() =>
  (revisionMode.value && saving.value)
  || (!revisionMode.value && existingInvoice.value?.status !== 'DRAFT' && isEdit.value)
  || (isEdit.value && existingInvoice.value?.document_kind !== 'STANDARD' && existingInvoice.value?.document_kind !== 'FINAL'),
)
const revisionLocked = computed(() => revisionMode.value)
// D7: a Final keeps its accepted Quote party/currency/treatment boundary.
// Currency has no generic editor in this screen; lock the remaining mutable
// selectors up front instead of relying on a rejected PUT.
const isFinalDraft = computed(() => isEdit.value
  && existingInvoice.value?.document_kind === 'FINAL'
  && existingInvoice.value.status === 'DRAFT')

// Show payment panel only when editing an existing invoice that is SENT or COMPLETED
const showPaymentPanel = computed(() =>
  isEdit.value &&
  existingInvoice.value != null &&
  existingInvoice.value.document_kind !== 'CREDIT_NOTE' &&
  (existingInvoice.value.status === 'SENT' || existingInvoice.value.status === 'COMPLETED' || existingInvoice.value.status === 'DRAFT' || existingInvoice.value.status === 'CANCELLED'),
)

// Payment receipts are always addressed from the persisted invoice source,
// never from a customer choice that is still unsaved in this edit form.
const receiptCustomer = computed(() =>
  persistedReceiptCustomer(existingInvoice.value?.customer_id, customers.value),
)

const handlePaymentsChanged = createDocumentChainPaymentChangeHandler<InvoicePaymentsResponse>(
  refreshAfterPayment,
  (aggregate) => {
  // Update the displayed invoice status and paid_status from the backend aggregate
  // (the aggregate is authoritative – frontend does not locally compute these)
  if (existingInvoice.value) {
    existingInvoice.value = {
      ...existingInvoice.value,
      paid_status: aggregate.paid_status,
      status: aggregate.status,
      due_amount: aggregate.due_amount,
    }
  }
  },
)

async function retryInitialDocumentChain(): Promise<void> {
  if (chainRefreshing.value) return
  await loadInitialDocumentChain()
}

async function retryPaymentDocumentChain(): Promise<void> {
  if (chainRefreshing.value) return
  await refreshAfterPayment()
}

async function retryRevisionDocumentChain(): Promise<void> {
  if (chainRefreshing.value) return
  await refreshAfterRevision()
}

const fmtMoney = (v: string | number) => Number(v).toFixed(2)

// ---- PDF download ----
const downloadingPdf = ref(false)

const pdfLocaleOptions = computed(() => [
  { label: t('pdf.localeDefault'), key: 'default' },
  { label: t('pdf.localeEn'), key: 'en' },
  { label: t('pdf.localeZh'), key: 'zh' },
])

async function handleDownloadPdf(locale?: 'en' | 'zh') {
  if (!existingInvoice.value) return
  downloadingPdf.value = true
  try {
    const id = existingInvoice.value.id
    const url = locale
      ? `/api/v1/invoices/${id}/pdf?locale=${locale}`
      : `/api/v1/invoices/${id}/pdf`
    await downloadBlob(url, `${existingInvoice.value.invoice_number ?? 'concept'}.pdf`)
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
const previewFallback = ref('invoice.pdf')

function openPreview(locale?: 'en' | 'zh') {
  if (!existingInvoice.value) return
  const id = existingInvoice.value.id
  previewSrc.value = locale
    ? `/api/v1/invoices/${id}/pdf?preview=true&locale=${locale}`
    : `/api/v1/invoices/${id}/pdf?preview=true`
  previewFallback.value = `${existingInvoice.value.invoice_number ?? 'concept'}.pdf`
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
const receiptQuoteAuditId = ref<string | null>(null)

function handleSent(_log: EmailLogRead) {
  // Refresh email log panel after successful send
  emailLogPanelRef.value?.refresh()
}

function handleReceiptSent(log: EmailLogRead) {
  if (!existingInvoice.value) return
  const target = receiptAuditTarget(existingInvoice.value.id, log)
  if (target === 'refresh-invoice') {
    emailLogPanelRef.value?.refresh()
    receiptQuoteAuditId.value = null
  } else if (target !== null) {
    receiptQuoteAuditId.value = target.quoteId
  }
}
</script>

<template>
  <div class="invoice-edit-page">
    <n-spin :show="pageLoading">
          <div class="invoice-edit-container">

            <!-- Page title + status actions -->
            <div class="page-header">
              <h2>
                {{ isEdit
                  ? (existingInvoice ? (existingInvoice.invoice_number ?? t('invoices.concept')) : t('invoices.edit'))
                  : t('invoices.new') }}
              </h2>
              <n-space v-if="existingInvoice" align="center">
                <n-tag size="small">
                  {{ t(invoiceDocumentKindLabelKey(existingInvoice.document_kind)) }}
                </n-tag>
                <n-tag :type="existingInvoice.status === 'DRAFT' ? 'default' : existingInvoice.status === 'SENT' ? 'info' : existingInvoice.status === 'COMPLETED' ? 'success' : 'warning'">
                  {{ t(`invoices.status${existingInvoice.status}`) }}
                </n-tag>
                <n-tag :type="existingInvoice.paid_status === 'PAID' ? 'success' : existingInvoice.paid_status === 'PARTIALLY_PAID' ? 'warning' : 'default'">
                  {{ t(`invoices.paidStatus${existingInvoice.paid_status}`) }}
                </n-tag>
                <n-text depth="3" style="font-size: 13px">
                  {{ t('invoices.due') }}: {{ existingInvoice.currency }} {{ fmtMoney(existingInvoice.due_amount) }}
                </n-text>

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
                  <!-- v-if/v-else pattern to avoid loading-prop+v-if prod bug -->
                  <n-button v-if="downloadingPdf" size="small" loading disabled>
                    {{ t('pdf.download') }}
                  </n-button>
                  <n-button v-else size="small">
                    <template #icon><n-icon><DownloadOutline /></n-icon></template>
                    {{ t('pdf.download') }}
                  </n-button>
                </n-dropdown>

                <!-- Send email button -->
                <n-button size="small" type="info" :disabled="revisionMode" @click="openSendDialog">
                  <template #icon><n-icon><MailOutline /></n-icon></template>
                  {{ t('sendDialog.title') }}
                </n-button>
                <n-button
                  v-if="existingInvoice.document_kind === 'STANDARD' && existingInvoice.status === 'SENT' && existingInvoice.revision_eligibility?.available && !revisionMode"
                  size="small" type="warning" :loading="eligibilityChecking" :disabled="eligibilityChecking" @click="enterRevision"
                >{{ t('invoices.revision.start') }}</n-button>

                <template v-if="existingInvoice.status === 'DRAFT'">
                  <n-button size="small" type="info" @click="handleStatusTransition('SENT')">
                    {{ t('invoices.markSent') }}
                  </n-button>
                  <n-button size="small" type="warning" @click="handleStatusTransition('CANCELLED')">
                    {{ t('invoices.cancel') }}
                  </n-button>
                </template>
                <template v-else-if="existingInvoice.status === 'CANCELLED'">
                  <n-button size="small" type="primary" @click="handleStatusTransition('DRAFT')">
                    {{ t('invoices.reactivate') }}
                  </n-button>
                </template>
              </n-space>
            </div>

            <n-alert v-if="initialChainError && !documentChain" type="error" style="margin-bottom: 16px">
              {{ t('chain.initialLoadFailed') }}
              <n-button text type="primary" :loading="chainRefreshing" :disabled="chainRefreshing" @click="retryInitialDocumentChain">
                {{ t('chain.retry') }}
              </n-button>
            </n-alert>

            <n-alert
              v-if="existingInvoice?.document_kind === 'STANDARD' && existingInvoice.status === 'SENT' && !revisionMode && !existingInvoice.revision_eligibility?.available"
              type="info" style="margin-bottom: 16px"
            >
              {{ t('invoices.revision.deliveredBoundary') }}
              <div>{{ t('invoices.revision.correctionPaths') }}</div>
            </n-alert>

            <n-alert v-if="revisionMode" type="warning" style="margin-bottom: 16px">
              {{ t('invoices.revision.modeNotice') }}
              <div>{{ t('invoices.revision.deliveryCheck') }}</div>
            </n-alert>
            <n-alert v-if="paymentRefreshError" type="warning" style="margin-bottom: 16px">
              {{ t('chain.paymentRefreshFailed') }}
              <n-button text type="primary" :loading="chainRefreshing" :disabled="chainRefreshing" @click="retryPaymentDocumentChain">
                {{ t('chain.retry') }}
              </n-button>
            </n-alert>
            <n-alert v-if="revisionRefreshError" type="warning" style="margin-bottom: 16px">
              {{ t('chain.revisionRefreshFailed') }}
              <n-button text type="primary" :loading="chainRefreshing" :disabled="chainRefreshing" @click="retryRevisionDocumentChain">
                {{ t('chain.retry') }}
              </n-button>
            </n-alert>

            <DocumentWorkflowPanel
              v-if="existingInvoice && documentChain"
              :invoice="existingInvoice"
              :invoice-version="existingInvoice.updated_at"
              :document-chain="documentChain"
              :refresh-chain="loadInitialDocumentChain"
              :chain-loading="chainRefreshing"
              :chain-error="initialChainError ? t('chain.initialLoadFailed') : null"
              @changed="loadInitialDocumentChain"
            />
            <n-empty
              v-else-if="existingInvoice && initialChainError && !chainRefreshing"
              :description="t('chain.initialLoadFailed')"
              size="small"
              style="margin-bottom: 16px"
            />

            <n-alert v-if="pageError" type="error" style="margin-bottom: 16px">
              {{ pageError }}
            </n-alert>

            <n-form v-if="!isEdit || existingInvoice?.document_kind === 'STANDARD' || existingInvoice?.document_kind === 'FINAL'" data-testid="invoice-edit-form" label-placement="top" :disabled="isReadOnly">

              <!-- Invoice header -->
              <n-card :title="t('invoices.headerSection')" style="margin-bottom: 16px">
                <n-grid :cols="2" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('invoices.customer')" required>
                      <n-select
                        v-model:value="customerId"
                        :disabled="isFinalDraft || revisionLocked"
                        filterable
                        :clearable="!isReadOnly && !revisionLocked && !isFinalDraft"
                        remote
                        :options="customerOptions"
                        :placeholder="t('invoices.customerSearch')"
                        @search="searchCustomers"
                        @update:value="handleCustomerSelect"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.referenceNumber')">
                      <n-input v-model:value="referenceNumber" :placeholder="t('invoices.referenceNumberPlaceholder')" clearable />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.invoiceDate')" required>
                      <n-date-picker
                        v-model:formatted-value="invoiceDate"
                        value-format="yyyy-MM-dd"
                        type="date"
                        :clearable="!isReadOnly"
                        style="width: 100%"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.dueDate')">
                      <n-date-picker
                        v-model:formatted-value="dueDate"
                        value-format="yyyy-MM-dd"
                        type="date"
                        clearable
                        style="width: 100%"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.revision.field.supplyDate')">
                      <n-date-picker
                        v-model:formatted-value="supplyOrAdvanceDate"
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
              <n-card :title="t('invoices.vatSection')" style="margin-bottom: 16px">
                <n-grid :cols="2" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('invoices.taxMode')">
                      <n-select
                        v-model:value="taxMode"
                        :options="[
                          { label: t('invoices.taxModeLine'), value: 'LINE' },
                          { label: t('invoices.taxModeDocument'), value: 'DOCUMENT' },
                        ]"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.amountsIncludeVat')">
                      <n-switch v-model:value="amountsIncludeVat" />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('invoices.vatTreatment')">
                      <n-select
                        v-model:value="vatTreatmentId"
                        :disabled="isFinalDraft || isReadOnly"
                        :options="vatTreatmentOptions"
                        :placeholder="t('invoices.vatTreatmentAuto')"
                        :clearable="!isReadOnly"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi v-if="taxMode === 'DOCUMENT'">
                    <n-form-item :label="t('invoices.documentVatRate')" required>
                      <n-select
                        v-model:value="documentVatRateId"
                        :options="documentVatRateOptions"
                        :placeholder="t('invoices.selectVatRate')"
                        :clearable="!isReadOnly"
                      />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- Discount & notes -->
              <n-card :title="t('invoices.discountSection')" style="margin-bottom: 16px">
                <n-grid :cols="3" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('invoices.discountType')">
                      <n-select v-model:value="discountType" :options="discountTypeOptions" />
                    </n-form-item>
                  </n-gi>
                  <n-gi v-if="discountType !== 'NONE'">
                    <n-form-item :label="discountType === 'PERCENTAGE' ? t('invoices.discountPercent') : t('invoices.discountFixed')">
                      <n-input-number v-model:value="discountValue" :min="0" :precision="3" />
                    </n-form-item>
                  </n-gi>
                  <n-gi :span="3">
                    <n-form-item :label="t('invoices.notes')">
                      <div style="width: 100%">
                        <n-input v-model:value="notes" type="textarea" :autosize="{ minRows: 2 }" clearable />
                        <n-button
                          v-if="!isReadOnly && noteTemplates.length > 0"
                          size="small"
                          secondary
                          style="margin-top: 4px"
                          @click="showNoteTemplateModal = true"
                        >
                          {{ t('invoices.insertNoteTemplate') }}
                        </n-button>
                      </div>
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- Content blocks -->
              <n-card :title="t('invoices.contentSection')" style="margin-bottom: 16px">
                <!-- Warranty -->
                <n-form-item :label="t('invoices.warrantyText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['WARRANTY']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['WARRANTY'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('invoices.selectContentBlock')"
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
                <n-form-item :label="t('invoices.termsText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['TERMS']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['TERMS'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('invoices.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('TERMS', v)"
                      />
                    </div>
                    <n-input v-model:value="termsText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>
                <!-- Bank -->
                <n-form-item :label="t('invoices.bankText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['BANK']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['BANK'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('invoices.selectContentBlock')"
                        size="small"
                        style="width: 250px"
                        clearable
                        @update:value="(v: string) => selectContentBlock('BANK', v)"
                      />
                    </div>
                    <n-input v-model:value="bankText" type="textarea" :autosize="{ minRows: 2 }" clearable />
                  </div>
                </n-form-item>
                <!-- Payment Terms -->
                <n-form-item :label="t('invoices.paymentTermsText')">
                  <div style="width: 100%">
                    <div v-if="!isReadOnly && contentBlocksByKind['PAYMENT_TERMS']?.length" style="margin-bottom: 4px">
                      <n-select
                        :options="(contentBlocksByKind['PAYMENT_TERMS'] ?? []).map(b => ({ label: b.name, value: b.id }))"
                        :placeholder="t('invoices.selectContentBlock')"
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
              <n-card :title="t('invoices.linesSection')" style="margin-bottom: 16px">
                <template #header-extra>
                  <n-button
                    v-if="!isReadOnly && filteredTemplates.length > 0"
                    size="small"
                    secondary
                    @click="showTemplateModal = true"
                  >
                    <template #icon><n-icon><DocumentTextOutline /></n-icon></template>
                    {{ t('invoices.applyTemplate') }}
                  </n-button>
                </template>
                <LineOrdering v-model="lines" :disabled="isReadOnly || saving || pageLoading">
                  <template #default="{ line, index: i }">
                <div class="line-row">
                  <n-divider v-if="i > 0" />

                  <n-grid :cols="12" :x-gap="8" :y-gap="4">
                    <!-- Product selector -->
                    <n-gi :span="4">
                      <n-form-item :label="t('invoices.product')" size="small">
                        <n-select
                          :value="line.product_id"
                          filterable
                          :clearable="!isReadOnly"
                          remote
                          :options="productOptions.map(p => ({ label: p.name, value: p.id }))"
                          :placeholder="t('invoices.productSearch')"
                          @search="searchProducts"
                          @update:value="(v: string | null) => handleProductSelect(i, v)"
                        />
                      </n-form-item>
                    </n-gi>

                    <!-- Name -->
                    <n-gi :span="5">
                      <n-form-item :label="t('invoices.lineName')" size="small" required>
                        <n-input v-model:value="line.name" :placeholder="t('invoices.lineNamePlaceholder')" />
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
                      <n-form-item :label="t('invoices.lineDescription')" size="small">
                        <n-input v-model:value="line.description" type="textarea" :autosize="{ minRows: 1, maxRows: 6 }" :placeholder="t('invoices.lineDescriptionPlaceholder')" clearable />
                      </n-form-item>
                    </n-gi>

                    <!-- Qty -->
                    <n-gi :span="2">
                      <n-form-item :label="t('invoices.quantity')" size="small">
                        <n-input-number v-model:value="line.quantity" :min="0.001" :precision="3" />
                      </n-form-item>
                    </n-gi>

                    <!-- Unit name -->
                    <n-gi :span="2">
                      <n-form-item :label="t('invoices.unit')" size="small">
                        <n-input v-model:value="line.unit_name" :placeholder="t('invoices.unitPlaceholder')" clearable />
                      </n-form-item>
                    </n-gi>

                    <!-- Unit price -->
                    <n-gi :span="3">
                      <n-form-item :label="amountsIncludeVat ? t('invoices.unitPriceIncl') : t('invoices.unitPriceExcl')" size="small">
                        <n-input-number v-model:value="line.unit_price" :min="0" :precision="3" />
                      </n-form-item>
                    </n-gi>

                    <!-- Discount type -->
                    <n-gi :span="2">
                      <n-form-item :label="t('invoices.lineDiscountType')" size="small">
                        <n-select v-model:value="line.discount_type" :options="discountTypeOptions" size="small" />
                      </n-form-item>
                    </n-gi>

                    <!-- Discount value -->
                    <n-gi v-if="line.discount_type !== 'NONE'" :span="2">
                      <n-form-item :label="t('invoices.lineDiscountValue')" size="small">
                        <n-input-number v-model:value="line.discount_value" :min="0" :precision="3" size="small" />
                      </n-form-item>
                    </n-gi>

                    <!-- VAT rate (LINE mode only) -->
                    <n-gi v-if="taxMode === 'LINE'" :span="3">
                      <n-form-item :label="t('invoices.vatRate')" size="small">
                        <n-select
                          v-model:value="line.vat_rate_id"
                          :options="lineVatRateOptions(line)"
                          :placeholder="t('invoices.selectVatRate')"
                          :clearable="!isReadOnly"
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
                  {{ t('invoices.addLine') }}
                </n-button>
              </n-card>

              <!-- Preview totals -->
              <n-card :title="t('invoices.totalsSection')" style="margin-bottom: 24px">
                <div v-if="previewLoading" style="text-align:center; padding: 12px">
                  <n-spin />
                </div>
                <n-alert v-else-if="previewError" type="error">{{ previewError }}</n-alert>
                <div v-else-if="displayPreview" class="totals-table">
                  <div class="total-row">
                    <span>{{ t('invoices.subtotalExclVat') }}</span>
                    <n-text>{{ fmtMoney(displayPreview.subtotal_excl_vat) }} </n-text>
                  </div>
                  <div v-if="Number(displayPreview.line_discount_total) !== 0" class="total-row">
                    <span>{{ t('invoices.lineDiscountTotal') }}</span>
                    <n-text>-{{ fmtMoney(displayPreview.line_discount_total) }}</n-text>
                  </div>
                  <div v-if="Number(displayPreview.document_discount_amount) !== 0" class="total-row">
                    <span>{{ t('invoices.documentDiscountAmount') }}</span>
                    <n-text>-{{ fmtMoney(displayPreview.document_discount_amount) }}</n-text>
                  </div>
                  <div class="total-row">
                    <span>{{ t('invoices.taxableAmount') }}</span>
                    <n-text>{{ fmtMoney(displayPreview.taxable_amount) }}</n-text>
                  </div>
                  <div class="total-row">
                    <span>{{ t('invoices.vatTotal') }}</span>
                    <n-text>{{ fmtMoney(displayPreview.vat_total) }}</n-text>
                  </div>
                  <n-divider style="margin: 8px 0" />
                  <div class="total-row total-row--bold">
                    <span>{{ t('invoices.totalInclVat') }}</span>
                    <n-text strong>{{ fmtMoney(displayPreview.total_incl_vat) }}</n-text>
                  </div>
                  <div v-if="displayPreview.vat_treatment_snapshot" class="total-row" style="font-size: 12px; color: var(--n-text-color-3)">
                    <span>{{ t('invoices.vatTreatmentApplied') }}</span>
                    <span>{{ displayPreview.vat_treatment_snapshot.code }}</span>
                  </div>
                </div>
                <n-text v-else depth="3">{{ t('invoices.totalsPlaceholder') }}</n-text>
              </n-card>

            </n-form>

            <n-card v-if="revisionMode && existingInvoice" :title="t('invoices.revision.confirmTitle')" style="margin-bottom: 24px">
              <div v-if="revisionSummary.length">
                <div v-for="change in revisionSummary" :key="change.key" class="revision-change">
                  <strong>{{ change.label }}:</strong> {{ change.before }} → {{ change.after }}
                </div>
              </div>
              <n-text v-else depth="3">{{ t('invoices.revision.noChanges') }}</n-text>
              <n-alert v-if="crossesReportPeriod" type="warning" style="margin: 12px 0">
                {{ t('invoices.revision.periodChange', {
                  oldPeriod: reportPeriod(existingInvoice.invoice_date), newPeriod: reportPeriod(invoiceDate),
                }) }}
              </n-alert>
              <n-form-item :label="t('invoices.revision.reasonLabel')" required>
                <n-input v-model:value="revisionReason" type="textarea" :autosize="{ minRows: 2 }" :disabled="saving" />
              </n-form-item>
              <n-checkbox v-model:checked="confirmUndelivered" :disabled="saving">
                {{ t('invoices.revision.confirmUndelivered') }}
              </n-checkbox>
            </n-card>

            <n-card v-if="isEdit && existingInvoice?.document_kind === 'STANDARD'" :title="t('invoices.revision.historyTitle')" style="margin-bottom: 24px">
              <n-spin :show="historyLoading">
                <n-alert v-if="historyError" type="error">
                  {{ t('invoices.revision.historyFailed') }}
                  <n-button text type="primary" :disabled="historyLoading" @click="loadRevisionHistory">{{ t('invoices.revision.retry') }}</n-button>
                </n-alert>
                <n-empty v-else-if="!historyLoading && !revisionHistory.length" :description="t('invoices.revision.noHistory')" size="small" />
                <n-card v-for="revision in revisionHistory" :key="revision.id" size="small" style="margin-top: 10px">
                  <div><strong>{{ t('invoices.revision.revisionNumber', { number: revision.revision_order }) }}</strong> · {{ new Date(revision.created_at).toLocaleString() }} · {{ revision.actor_user_id ?? t('invoices.revision.unknownActor') }}</div>
                  <div><strong>{{ t('invoices.revision.reasonLabel') }}:</strong> {{ revision.reason }}</div>
                  <div v-for="row in historyRows(revision)" :key="row.key" class="revision-change">
                    <strong>{{ row.label }}:</strong> {{ row.before }} → {{ row.after }}
                  </div>
                  <div v-if="JSON.stringify(revision.before_snapshot.invoice.lines) !== JSON.stringify(revision.after_snapshot.invoice.lines)" class="revision-lines">
                    <strong>{{ t('invoices.revision.linesBefore') }}</strong>
                    <div v-for="line in historyLines(revision, 'before_snapshot')" :key="`b-${line}`">{{ line }}</div>
                    <strong>{{ t('invoices.revision.linesAfter') }}</strong>
                    <div v-for="line in historyLines(revision, 'after_snapshot')" :key="`a-${line}`">{{ line }}</div>
                  </div>
                </n-card>
              </n-spin>
            </n-card>

            <!-- Payment panel (only for existing SENT/COMPLETED/DRAFT/CANCELLED invoices – panel guards internally) -->
            <InvoicePaymentPanel
              v-if="showPaymentPanel && existingInvoice"
              :invoice-id="existingInvoice.id"
              :invoice-status="existingInvoice.status"
              :invoice-version="existingInvoice.updated_at"
              :customer-email="receiptCustomer?.email ?? null"
              :customer-locale="receiptCustomer?.locale ?? null"
              @payments-changed="handlePaymentsChanged"
              @receipt-sent="handleReceiptSent"
            />

            <!-- Email log (only for existing invoices) -->
            <EmailLogPanel
              v-if="isEdit && existingInvoice"
              ref="emailLogPanelRef"
              doc-type="invoice"
              :doc-id="existingInvoice.id"
            />
            <n-alert v-if="receiptQuoteAuditId" type="info" style="margin-bottom: 16px">
              {{ t('payments.receiptAuditOnQuote') }}
              <n-button text type="primary" @click="router.push(`/quotes/${receiptQuoteAuditId}/edit`)">
                {{ t('payments.openSourceQuoteLog') }}
              </n-button>
            </n-alert>

            <!-- Action buttons -->
            <n-space justify="end" style="margin-bottom: 24px">
              <n-button v-if="revisionMode" :disabled="saving" @click="cancelRevision">{{ t('invoices.revision.cancel') }}</n-button>
              <n-button v-else @click="router.push('/invoices')">{{ t('invoices.backToList') }}</n-button>
              <n-button
                v-if="!isReadOnly || revisionMode"
                type="primary"
                :loading="saving" :disabled="revisionMode && (!hasRevisionChanges || !revisionReason.trim() || !confirmUndelivered || saving)"
                @click="handleSave"
              >
                {{ revisionMode ? t('invoices.revision.save') : isEdit ? t('invoices.save') : t('invoices.create') }}
              </n-button>
            </n-space>

          </div>
    </n-spin>

    <!-- Document template picker modal -->
    <n-modal
      v-model:show="showTemplateModal"
      preset="card"
      :title="t('invoices.selectTemplateTitle')"
      style="max-width: 560px"
    >
      <n-list hoverable clickable>
        <n-list-item
          v-for="tmpl in filteredTemplates"
          :key="tmpl.id"
          @click="applyTemplate(tmpl)"
        >
          <n-thing :title="tmpl.name" :description="`${(tmpl.lines ?? []).length} ${t('invoices.templateLineCount')}`" />
        </n-list-item>
      </n-list>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showTemplateModal = false">{{ t('invoices.cancel') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Note template picker modal -->
    <n-modal
      v-model:show="showNoteTemplateModal"
      preset="card"
      :title="t('invoices.selectNoteTemplateTitle')"
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
          <n-button @click="showNoteTemplateModal = false">{{ t('invoices.cancel') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Send email dialog (only when editing an existing invoice) -->
    <DocumentSendDialog
      v-if="isEdit && existingInvoice"
      v-model:show="sendDialogShow"
      :doc-type="invoiceDocumentSendType(existingInvoice.document_kind)"
      :doc-id="existingInvoice.id"
      :customer-email="selectedCustomer?.email ?? null"
      :customer-locale="existingInvoice.party_snapshot_locale"
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

.invoice-edit-container {
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
</style>
