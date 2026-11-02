<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NDatePicker, NDescriptions, NDescriptionsItem,
  NCollapse, NCollapseItem, NDivider, NEmpty, NForm, NFormItem, NInput, NList,
  NListItem, NModal, NSelect, NSpace, NSpin, NTag, NText,
} from 'naive-ui'
import { del, get, post, postFile, put, downloadBlob, ApiError } from '../api/http'
import type { components } from '../api/schema'
import { formatDateTime, localDateStr } from '../utils/date'
import { advanceIntent, availableAction, creditIntent, type CreditIntentRow, uniqueTimelineNodes } from '../utils/lifecycleWorkflow'
import { invoiceDocumentKindLabelKey } from '../utils/documentKind'
import PdfPreviewDialog from './PdfPreviewDialog.vue'
import DocumentSendDialog from './DocumentSendDialog.vue'
import { useSettingsPanel } from '../composables/useSettingsPanel'

type Chain = components['schemas']['DocumentChainRead']
type InvoiceRead = components['schemas']['InvoiceRead']
type AdvanceCalculationRead = components['schemas']['AdvanceCalculationRead']
type CreditCalculationRead = components['schemas']['CreditCalculationRead']
type RefundCollectionRead = components['schemas']['RefundCollectionRead']
type DocumentArtifactListResponse = components['schemas']['DocumentArtifactListResponse']
type PaymentInput = components['schemas']['PaymentInput']
type PaymentRead = components['schemas']['PaymentRead']
type PaymentMutationResponse = components['schemas']['PaymentMutationResponse']
type DocumentArtifactValidationRead = components['schemas']['DocumentArtifactValidationRead']

const props = defineProps<{
  quoteId?: string | null
  invoice?: InvoiceRead | null
  /** Invoice updated_at binds child previews/resources to the saved revision. */
  invoiceVersion?: string
  /** Parent Quote mode-card request; a monotonic signal avoids stale booleans. */
  openAdvanceSignal?: number
  /** Page-owned authoritative chain.  Keeps payment and workflow refreshes aligned. */
  documentChain?: Chain | null
  refreshChain?: () => Promise<boolean>
  /** The parent owns projection reads when it supplies documentChain. */
  chainLoading?: boolean
  chainError?: string | null
}>()
const emit = defineEmits<{ changed: []; continuationCancelled: [] }>()
const { t, locale } = useI18n()
const router = useRouter()
const { open: openSettings } = useSettingsPanel()

const chain = ref<Chain | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
// Parent-owned chain/actions and child-owned retained output have independent
// lifetimes.  In particular, a successful chain refresh cannot make a failed
// artifact/refund read look successful.
const resourceError = ref<string | null>(null)
const issuedOutputLoading = ref(false)
const expandedNames = ref<string[]>([])
const busy = ref(false)
const showAdvance = ref(false)
const showFinal = ref(false)
const showCredit = ref(false)
const showCancellation = ref(false)
const advanceMode = ref<'GROSS_AMOUNT' | 'PERCENTAGE'>('GROSS_AMOUNT')
const advanceRaw = ref('')
const advancePreview = ref<AdvanceCalculationRead | null>(null)
const creditFull = ref(true)
const creditIntentConfirmation = ref(false)
const creditRows = ref<CreditIntentRow[]>([])
const creditPreview = ref<CreditCalculationRead | null>(null)
const finalDate = ref(localDateStr(new Date()))
const advanceDate = ref(localDateStr(new Date()))
const advanceDueDate = ref<string | null>(null)
const advanceSupplyDate = ref<string | null>(null)
const advanceReference = ref<string | null>(null)
const creditDate = ref(localDateStr(new Date()))
const creditDueDate = ref<string | null>(null)
const creditSupplyDate = ref<string | null>(null)
const creditReference = ref<string | null>(null)
const cancellationPreview = ref<components['schemas']['ProjectCancellationPreview'] | null>(null)
const refund = ref<RefundCollectionRead | null>(null)
const refundAmount = ref<string>('')
const refundDate = ref(localDateStr(new Date()))
const refundSendId = ref<string | null>(null)
const refundSendShow = ref(false)
const refundPreviewShow = ref(false)
const refundPreviewSrc = ref<string | null>(null)
const artifacts = ref<DocumentArtifactListResponse | null>(null)
const uploadFile = ref<File | null>(null)
const artifactFileInput = ref<HTMLInputElement | null>(null)
const uploadError = ref<string | null>(null)
type UploadNoticeKind = 'AI_REVIEW' | 'ARTIFACT_CONFLICT'
const uploadNotice = ref<{ kind: UploadNoticeKind; message: string } | null>(null)
const validation = ref<DocumentArtifactValidationRead | null>(null)
const validatingUpload = ref(false)
const uploadingArtifact = ref(false)
const showArtifactUploadConfirm = ref(false)
const refundArtifacts = ref<Record<string, DocumentArtifactListResponse>>({})
const refundMethodId = ref<string | null>(null)
const refundReference = ref<string | null>(null)
const refundNote = ref<string | null>(null)
const editingRefund = ref<PaymentRead | null>(null)
const pendingRefundAction = ref<'create' | 'update' | 'delete' | null>(null)
const showRefundConfirm = ref(false)
const pendingFollowup = ref<'replacement' | 'compensating-invoice' | null>(null)
const showFollowupConfirm = ref(false)
const showCancellationConfirm = ref(false)
const creditSourcePreview = ref<CreditCalculationRead | null>(null)
const advancePreviewSignature = ref<string | null>(null)
const creditPreviewSignature = ref<string | null>(null)
const cancellationPreviewSignature = ref<string | null>(null)
type AdvanceRequest = ReturnType<typeof advanceIntent> & { invoice_date: string; due_date: string | null; supply_or_advance_date: string | null; reference_number: string | null }
type CreditRequest = ReturnType<typeof creditIntent> & { invoice_date: string; due_date: string | null; supply_or_advance_date: string | null; reference_number: string | null }
const advancePreviewRequest = ref<AdvanceRequest | null>(null)
const creditPreviewRequest = ref<CreditRequest | null>(null)
const cancellationPreviewRequest = ref<{ invoice_date: string } | null>(null)
let advanceGeneration = 0
let creditPreviewGeneration = 0
let creditSourceGeneration = 0
let cancellationGeneration = 0
let loadGeneration = 0
let issuedOutputGeneration = 0
let contextGeneration = 0
let artifactUploadGeneration = 0
let busyOwner: 'advance-preview' | 'credit-preview' | 'credit-source' | 'cancellation-preview' | null = null
const paymentMethods = ref<Array<{ id: string; name: string }>>([])

const quoteId = computed(() => props.quoteId ?? props.invoice?.quote_id ?? null)
const invoiceId = computed(() => props.invoice?.id ?? null)
const creditSourceId = computed(() => props.invoice?.source_invoice_id ?? invoiceId.value)
const isCredit = computed(() => props.invoice?.document_kind === 'CREDIT_NOTE')
// Retained output is created only when a document is issued.  Draft editors
// still need their parent-owned chain, totals, and actions, but must not ask
// issued-only endpoints for resources that do not exist yet.
const hasIssuedOutput = computed(() => ['SENT', 'COMPLETED'].includes(props.invoice?.status ?? ''))
const canUploadArtifact = computed(() => hasIssuedOutput.value && artifacts.value?.items.length === 0)
// An invoice and a quote can share a UUID in imported data, but they are
// separate route owners and must not inherit one another's UI state.
const documentOwnerKey = computed(() => {
  if (props.invoice?.id) return `invoice:${props.invoice.id}`
  return props.quoteId ? `quote:${props.quoteId}` : null
})
const sourceCanCredit = computed(() => availableAction(actionCodes.value, 'CREATE_CREDIT_NOTE', invoiceId.value, 'INVOICE'))
const visibleChain = computed(() => props.documentChain ?? chain.value)
const actionCodes = computed(() => visibleChain.value?.available_actions ?? [])
const modeLabel = computed(() => visibleChain.value?.settlement_mode ?? 'UNSET')
const formalQuoteMode = computed(() => modeLabel.value === 'FORMAL_ADVANCE' || modeLabel.value === 'UNSET')
// A Quote's projection is shared by every locked mode, but only the formal
// branch may surface formal commands.  Target filtering alone is not enough:
// a stale or overly broad action list must never turn a Direct/Receipt page
// into a Formal workflow.
const canAdvance = computed(() => formalQuoteMode.value && availableAction(actionCodes.value, 'CREATE_ADVANCE', quoteId.value, 'QUOTE'))
const canFinal = computed(() => formalQuoteMode.value && availableAction(actionCodes.value, 'CREATE_FINAL', quoteId.value, 'QUOTE'))
const canCancelProject = computed(() => formalQuoteMode.value && availableAction(actionCodes.value, 'CREATE_PROJECT_CANCELLATION', quoteId.value, 'QUOTE'))
const canReplacement = computed(() => availableAction(actionCodes.value, 'CREATE_REPLACEMENT', invoiceId.value, 'INVOICE'))
const canCompensation = computed(() => availableAction(actionCodes.value, 'CREATE_COMPENSATING_INVOICE', invoiceId.value, 'INVOICE'))
function actionReason(code: string): string | null {
  const reason = actionCodes.value.find(action => action.code === code)?.reason_code
  return reason ? t(`workflow.reasons.${reason}`) : null
}
const nodeRows = computed(() => uniqueTimelineNodes(visibleChain.value?.nodes))
const timelineRows = computed(() => visibleChain.value?.timeline ?? [])
const nodeLabels = computed(() => new Map(nodeRows.value.map(node => [node.id, node.number ?? node.node_type])))
const sourceLabel = computed(() => nodeLabels.value.get(props.invoice?.source_invoice_id ?? '') ?? props.invoice?.source_invoice_id ?? '—')
const followupContext = computed(() => {
  const code = pendingFollowup.value === 'replacement' ? 'CREATE_REPLACEMENT' : 'CREATE_COMPENSATING_INVOICE'
  return actionCodes.value.find(action => action.code === code && action.target_id === invoiceId.value)?.followup_context ?? null
})
function relationText(relation: components['schemas']['DocumentChainRelationRead']): string {
  return `${nodeLabels.value.get(relation.from_node_id) ?? relation.from_node_id} → ${nodeLabels.value.get(relation.to_node_id) ?? relation.to_node_id} · ${t(`workflow.relations.${relation.relation_type}`, relation.relation_type)}`
}
function eventText(event: components['schemas']['DocumentChainEventRead']): string {
  return t(`workflow.events.${event.event_type}`, event.event_type)
}
function modeText(mode: string): string { return t(`workflow.modes.${mode}`, mode) }
function artifactReasonText(reason: string): string { return t(`workflow.artifactReasons.${reason}`, reason) }

function fmt(value: string | number | null | undefined): string { return value == null ? '—' : String(value) }
const paymentMethodOptions = computed(() => paymentMethods.value.map(item => ({ label: item.name, value: item.id })))
function advanceRequest(): AdvanceRequest { return { ...advanceIntent(advanceMode.value, advanceRaw.value), invoice_date: advanceDate.value, due_date: advanceDueDate.value, supply_or_advance_date: advanceSupplyDate.value, reference_number: advanceReference.value } }
function creditRequest(): CreditRequest { return { ...creditIntent(creditFull.value, creditRows.value), invoice_date: creditDate.value, due_date: creditDueDate.value, supply_or_advance_date: creditSupplyDate.value, reference_number: creditReference.value } }
function invalidateCreditPreview(): void {
  ++creditPreviewGeneration
  if (busyOwner === 'credit-preview') {
    busy.value = false
    busyOwner = null
  }
  creditPreview.value = null
  creditPreviewRequest.value = null
  creditPreviewSignature.value = null
}
function invalidateCreditSource(): void {
  ++creditSourceGeneration
  if (busyOwner === 'credit-source') {
    busy.value = false
    busyOwner = null
  }
  creditSourcePreview.value = null
}
function invalidateCreditMode(): void {
  invalidateCreditPreview()
  invalidateCreditSource()
}
function chooseCreditMode(fullRemaining: boolean): void {
  // Vue's default watcher flush runs after this click handler.  Queue the
  // selected-basis request behind that invalidation so it owns a fresh,
  // current generation instead of being cancelled by its own mode change.
  if (creditFull.value === fullRemaining) {
    // A migrated ambiguous draft intentionally displays a mode but cannot
    // prove that it was the author's intent. Re-clicking that displayed mode
    // is an explicit confirmation, not a no-op.
    if (creditIntentConfirmation.value) {
      creditIntentConfirmation.value = false
      invalidateCreditPreview()
    }
    if (!fullRemaining && !creditSourcePreview.value && !busy.value) void loadCreditSource()
    return
  }
  creditFull.value = fullRemaining
  creditIntentConfirmation.value = false
  if (!fullRemaining) void nextTick().then(loadCreditSource)
}
function advanceSignature(): string { return JSON.stringify(advanceRequest()) }
function creditSignature(): string { return JSON.stringify(creditRequest()) }
function cancellationSignature(): string { return JSON.stringify({ invoice_date: finalDate.value }) }
function errorText(cause: unknown): string {
  if (cause instanceof ApiError) {
    const detail = cause.detail as { detail?: { code?: string }; code?: string }
    const code = detail?.detail?.code ?? detail?.code
    return code ? t(`workflow.errors.${code}`, t('workflow.errors.UNKNOWN')) : t('workflow.errors.UNKNOWN')
  }
  return t('workflow.errors.UNKNOWN')
}
function apiCode(cause: unknown): string | null {
  if (!(cause instanceof ApiError)) return null
  const detail = cause.detail as { detail?: { code?: string }; code?: string }
  return detail?.detail?.code ?? detail?.code ?? null
}
function invalidateArtifactUpload(): void {
  ++artifactUploadGeneration
  validatingUpload.value = false
  uploadingArtifact.value = false
  showArtifactUploadConfirm.value = false
}
function resetUploadSelection(): void {
  invalidateArtifactUpload()
  uploadFile.value = null
  validation.value = null
  uploadError.value = null
  uploadNotice.value = null
  showArtifactUploadConfirm.value = false
}
function onArtifactFileChange(event: Event): void {
  invalidateArtifactUpload()
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  validation.value = null
  uploadError.value = null
  uploadNotice.value = null
  showArtifactUploadConfirm.value = false
  if (!file) { uploadFile.value = null; return }
  if (file.type !== 'application/pdf') { uploadFile.value = null; uploadError.value = t('workflow.upload.invalidType'); return }
  if (file.size > 10 * 1024 * 1024) { uploadFile.value = null; uploadError.value = t('workflow.upload.tooLarge'); return }
  uploadFile.value = file
}
function rejectInvalidArtifactFile(cause: unknown): void {
  // Server-side PDF parsing can reject a browser-accepted MIME/size pair.
  // Drop that File so it cannot be confirmed without an explicit re-selection.
  invalidateArtifactUpload()
  uploadFile.value = null
  validation.value = null
  uploadNotice.value = null
  uploadError.value = errorText(cause)
  if (artifactFileInput.value) artifactFileInput.value.value = ''
}
async function checkArtifactWithAi(): Promise<void> {
  if (!invoiceId.value || !uploadFile.value || validatingUpload.value || uploadingArtifact.value) return
  const generation = ++artifactUploadGeneration
  const owner = documentOwnerKey.value
  const id = invoiceId.value
  const file = uploadFile.value
  validatingUpload.value = true; uploadError.value = null; uploadNotice.value = null
  try {
    const result = await postFile<DocumentArtifactValidationRead>(`/api/v1/invoices/${id}/artifacts/validate-upload?language=${locale.value === 'zh' ? 'zh' : 'en'}`, file)
    if (generation !== artifactUploadGeneration || owner !== documentOwnerKey.value || id !== invoiceId.value || file !== uploadFile.value || !canUploadArtifact.value) return
    validation.value = result
  } catch (cause) {
    if (generation !== artifactUploadGeneration || owner !== documentOwnerKey.value || id !== invoiceId.value || file !== uploadFile.value || !canUploadArtifact.value) return
    validation.value = null
    const code = apiCode(cause)
    if (code === 'ARTIFACT_ALREADY_EXISTS') {
      uploadNotice.value = { kind: 'ARTIFACT_CONFLICT', message: t('workflow.upload.conflict') }
      await loadIssuedOutput(++issuedOutputGeneration)
    } else if (code === 'INVALID_ARTIFACT_FILE') {
      rejectInvalidArtifactFile(cause)
    } else {
      uploadNotice.value = { kind: 'AI_REVIEW', message: code === 'AI_NOT_CONFIGURED' ? t('workflow.upload.aiNotConfigured') : t('workflow.upload.aiFailed') }
    }
  } finally {
    if (generation === artifactUploadGeneration && owner === documentOwnerKey.value && id === invoiceId.value && file === uploadFile.value) validatingUpload.value = false
  }
}
function requestArtifactUpload(): void {
  if (!uploadFile.value || uploadingArtifact.value || validatingUpload.value) return
  showArtifactUploadConfirm.value = true
}
async function confirmArtifactUpload(): Promise<void> {
  if (!invoiceId.value || !uploadFile.value || uploadingArtifact.value) return
  const generation = ++artifactUploadGeneration
  const owner = documentOwnerKey.value
  const id = invoiceId.value
  const file = uploadFile.value
  uploadingArtifact.value = true; uploadError.value = null; uploadNotice.value = null
  try {
    await postFile(`/api/v1/invoices/${id}/artifacts`, file)
    if (generation !== artifactUploadGeneration || owner !== documentOwnerKey.value || id !== invoiceId.value || file !== uploadFile.value || !canUploadArtifact.value) return
    showArtifactUploadConfirm.value = false
    resetUploadSelection()
    await loadIssuedOutput(++issuedOutputGeneration)
  } catch (cause) {
    if (generation !== artifactUploadGeneration || owner !== documentOwnerKey.value || id !== invoiceId.value || file !== uploadFile.value || !canUploadArtifact.value) return
    showArtifactUploadConfirm.value = false
    if (apiCode(cause) === 'ARTIFACT_ALREADY_EXISTS') {
      uploadNotice.value = { kind: 'ARTIFACT_CONFLICT', message: t('workflow.upload.conflict') }
      await loadIssuedOutput(++issuedOutputGeneration)
    } else uploadError.value = errorText(cause)
  } finally {
    if (generation === artifactUploadGeneration && owner === documentOwnerKey.value && id === invoiceId.value && file === uploadFile.value) uploadingArtifact.value = false
  }
}
function validationStatusType(status: string): 'success' | 'warning' | 'info' {
  return status === 'MATCH' ? 'success' : status === 'MISMATCH' || status === 'WARNING' ? 'warning' : 'info'
}
function validationFieldText(field: string): string { return t(`workflow.upload.fields.${field}`, field) }
async function loadPaymentMethods(): Promise<void> {
  if (paymentMethods.value.length) return
  try {
    paymentMethods.value = (await get<{ items: Array<{ id: string; name: string }> }>(
      '/api/v1/payment-methods',
    )).items
  } catch (cause) { error.value = errorText(cause) }
}
function clearIssuedOutput(): void {
  refund.value = null
  artifacts.value = null
  refundArtifacts.value = {}
  resourceError.value = null
  issuedOutputLoading.value = false
}
function invalidateIssuedOutput(): number {
  const generation = ++issuedOutputGeneration
  clearIssuedOutput()
  return generation
}
function resetWorkflowState(invoice: InvoiceRead | null | undefined): void {
  // This owns every locally editable or transient workflow value.  Route
  // owners can both be Quotes (where the invoice watcher never fires), so a
  // documentOwnerKey transition—not just an invoice id transition—must reset
  // these values before the new context is displayed.
  ++advanceGeneration; ++creditPreviewGeneration; ++creditSourceGeneration; ++cancellationGeneration
  invalidateIssuedOutput()
  advanceMode.value = 'GROSS_AMOUNT'; advanceRaw.value = ''
  advancePreview.value = null; advancePreviewRequest.value = null; advancePreviewSignature.value = null
  advanceDate.value = localDateStr(new Date()); advanceDueDate.value = null; advanceSupplyDate.value = null; advanceReference.value = null
  creditFull.value = true; creditIntentConfirmation.value = false; creditRows.value = []
  creditPreview.value = null; creditPreviewRequest.value = null; creditPreviewSignature.value = null; creditSourcePreview.value = null
  creditDate.value = localDateStr(new Date()); creditDueDate.value = null; creditSupplyDate.value = null; creditReference.value = null
  finalDate.value = localDateStr(new Date())
  cancellationPreview.value = null; cancellationPreviewRequest.value = null; cancellationPreviewSignature.value = null
  refundAmount.value = ''; refundDate.value = localDateStr(new Date()); refundMethodId.value = null; refundReference.value = null; refundNote.value = null
  editingRefund.value = null; pendingRefundAction.value = null
  refundSendId.value = null; refundSendShow.value = false; refundPreviewShow.value = false; refundPreviewSrc.value = null
  pendingFollowup.value = null; showFollowupConfirm.value = false; showRefundConfirm.value = false; showCancellationConfirm.value = false
  showAdvance.value = false; showFinal.value = false; showCredit.value = false; showCancellation.value = false
  resetUploadSelection()
  busy.value = false; busyOwner = null; error.value = null
  if (invoice?.document_kind === 'ADVANCE') {
    advanceMode.value = invoice.advance_input_mode ?? 'GROSS_AMOUNT'
    advanceRaw.value = String(advanceMode.value === 'PERCENTAGE' ? invoice.advance_percentage ?? '' : invoice.advance_gross_amount ?? invoice.total_incl_vat)
    advanceDate.value = invoice.invoice_date; advanceDueDate.value = invoice.due_date ?? null
    advanceSupplyDate.value = invoice.supply_or_advance_date ?? null; advanceReference.value = invoice.reference_number ?? null
  }
  if (invoice?.document_kind === 'CREDIT_NOTE') {
    creditDate.value = invoice.invoice_date; creditDueDate.value = invoice.due_date ?? null
    creditSupplyDate.value = invoice.supply_or_advance_date ?? null; creditReference.value = invoice.reference_number ?? null
    const intent = invoice.credit_draft_intent
    creditFull.value = intent?.full_remaining ?? false
    creditIntentConfirmation.value = intent?.requires_confirmation ?? false
    creditRows.value = (intent?.lines ?? []).map(line => ({
      source_basis_line_id: line.source_basis_line_id,
      input_mode: line.input_mode,
      raw: String(line.input_mode === 'QUANTITY' ? line.quantity : line.gross_amount),
    }))
  }
}
async function loadIssuedOutput(generation: number): Promise<void> {
  issuedOutputLoading.value = true
  if (!invoiceId.value || !hasIssuedOutput.value) {
    clearIssuedOutput()
    if (generation === issuedOutputGeneration) issuedOutputLoading.value = false
    return
  }
  try {
    if (isCredit.value) {
      const [collection, history] = await Promise.all([
        get<RefundCollectionRead>(`/api/v1/credit-notes/${invoiceId.value}/refunds`),
        get<DocumentArtifactListResponse>(`/api/v1/invoices/${invoiceId.value}/artifacts`),
      ])
      if (generation !== issuedOutputGeneration) return
      refund.value = collection
      artifacts.value = history
      const allArtifacts = await Promise.all((collection.items ?? []).map(async item => [item.id, await get<DocumentArtifactListResponse>(`/api/v1/payments/${item.id}/artifacts`)] as const))
      if (generation !== issuedOutputGeneration) return
      refundArtifacts.value = Object.fromEntries(allArtifacts)
    } else {
      const history = await get<DocumentArtifactListResponse>(`/api/v1/invoices/${invoiceId.value}/artifacts`)
      if (generation !== issuedOutputGeneration) return
      artifacts.value = history
    }
    if (generation === issuedOutputGeneration) resourceError.value = null
  } catch (cause) {
    if (generation === issuedOutputGeneration) resourceError.value = errorText(cause)
  } finally {
    if (generation === issuedOutputGeneration) issuedOutputLoading.value = false
  }
}
async function load(): Promise<void> {
  const id = quoteId.value ?? invoiceId.value
  if (!id) return
  const generation = ++loadGeneration
  invalidateIssuedOutput()
  ++contextGeneration
  ++advanceGeneration; ++creditPreviewGeneration; ++creditSourceGeneration; ++cancellationGeneration
  // A page-provided projection has a single owner.  Keep it visible while
  // this child independently reads invoice-specific refunds/artifacts.
  if (!props.documentChain) chain.value = null
  advancePreview.value = null; advancePreviewRequest.value = null
  creditPreview.value = null; creditPreviewRequest.value = null; creditSourcePreview.value = null
  // A new route/owner context must never inherit a disabled control or an
  // error from a superseded preview.
  loading.value = true; busy.value = false; busyOwner = null; error.value = null
  try {
    if (!props.documentChain) {
      const endpoint = quoteId.value ? `/api/v1/quotes/${id}/document-chain` : `/api/v1/invoices/${id}/document-chain`
      const nextChain = await get<Chain>(endpoint)
      if (generation !== loadGeneration) return
      chain.value = nextChain
    }
  } catch (cause) { if (generation === loadGeneration) error.value = errorText(cause) }
  finally {
    if (generation === loadGeneration) loading.value = false
  }
  if (generation === loadGeneration && hasIssuedOutput.value) await loadIssuedOutput(++issuedOutputGeneration)
}
async function refresh(): Promise<void> {
  if (loading.value || props.chainLoading) return
  if (props.refreshChain) {
    const context = contextGeneration
    const owner = documentOwnerKey.value
    try {
      const refreshed = await props.refreshChain()
      if (context !== contextGeneration || owner !== documentOwnerKey.value) return
      // The child must not swallow a failed explicit parent-owned refresh.
      // Parent pages may retain the last good chain, so surface that outcome
      // locally as well as through their stale/error banner.
      error.value = refreshed ? null : (props.chainError ?? t('chain.initialLoadFailed'))
    } catch (cause) {
      if (context !== contextGeneration || owner !== documentOwnerKey.value) return
      error.value = errorText(cause)
    }
    // An explicit refresh retries both owners.  The output error remains
    // visible unless this read itself succeeds.
    if (context !== contextGeneration || owner !== documentOwnerKey.value) return
    await loadIssuedOutput(++issuedOutputGeneration)
    return
  }
  await load()
}
// Route-owner changes and lifecycle updates arrive in the same Vue flush.
// Handle them as one transition: an owner change must finish its reset and
// draft-intent hydration before it starts the one output read for that owner.
// Keeping the status case in this watcher also prevents a second watcher from
// invalidating that new read when two issued invoices are swapped in place.
watch(
  () => [documentOwnerKey.value, props.invoice?.status, props.invoiceVersion ?? props.invoice?.updated_at] as const,
  ([owner, status, version], previous) => {
    if (!previous || owner !== previous[0]) {
      expandedNames.value = []
      resetWorkflowState(props.invoice)
      if (owner) void load()
      return
    }
    if (status === previous[1] && version !== previous[2]) {
      // A same-id SENT invoice can be revised in place. Existing credit
      // source/calculation previews and original-PDF advice/confirmation are
      // version-bound and must be discarded. Keep the selected File so the
      // user can explicitly recheck or reconfirm it for the new version.
      invalidateCreditMode()
      invalidateArtifactUpload()
      validation.value = null
      uploadError.value = null
      uploadNotice.value = null
      error.value = null
      if (hasIssuedOutput.value) void loadIssuedOutput(++issuedOutputGeneration)
      return
    }
    if (status === previous[1]) return
    invalidateArtifactUpload()
    validation.value = null
    uploadError.value = null
    uploadNotice.value = null
    if (hasIssuedOutput.value) void loadIssuedOutput(++issuedOutputGeneration)
    else invalidateIssuedOutput()
  },
  { immediate: true },
)
watch(() => props.openAdvanceSignal, signal => {
  if (signal && canAdvance.value) showAdvance.value = true
}, { immediate: true })
watch([advanceMode, advanceRaw, advanceDate, advanceDueDate, advanceSupplyDate, advanceReference], () => { ++advanceGeneration; if (busyOwner === 'advance-preview') { busy.value = false; busyOwner = null }; advancePreview.value = null; advancePreviewRequest.value = null; advancePreviewSignature.value = null })
// Calculation output is tied to the complete raw intent, but the immutable
// source-basis dictionary is tied only to the source/route and selected-mode
// owner.  Keeping them separate lets users add/remove/edit selected rows
// without losing the options they just loaded.
watch([creditRows, creditDate, creditDueDate, creditSupplyDate, creditReference], invalidateCreditPreview, { deep: true })
watch(creditFull, invalidateCreditMode)
watch(finalDate, () => { ++cancellationGeneration; if (busyOwner === 'cancellation-preview') { busy.value = false; busyOwner = null }; cancellationPreview.value = null; cancellationPreviewRequest.value = null; cancellationPreviewSignature.value = null })
watch(showAdvance, open => { if (!open && props.openAdvanceSignal && modeLabel.value === 'UNSET') emit('continuationCancelled') })
function goToNode(node: components['schemas']['DocumentChainNodeRead']): void {
  if (node.node_type.toLowerCase().includes('invoice') || node.document_kind) router.push(`/invoices/${node.id}/edit`)
  else if (node.node_type.toLowerCase().includes('quote')) router.push(`/quotes/${node.id}/edit`)
}
async function calculateAdvance(): Promise<void> {
  if (!quoteId.value || !advanceRaw.value.trim() || busy.value) return
  const request = advanceRequest(); const signature = JSON.stringify(request); const generation = ++advanceGeneration; const context = contextGeneration
  busy.value = true; busyOwner = 'advance-preview'; error.value = null; advancePreview.value = null; advancePreviewSignature.value = null
  try {
    const preview = await post<AdvanceCalculationRead>(`/api/v1/quotes/${quoteId.value}/advance-invoices/calculate`, advanceIntent(request.input_mode, request.input_mode === 'GROSS_AMOUNT' ? request.gross_amount! : request.percentage!))
    if (context !== contextGeneration || generation !== advanceGeneration || advanceSignature() !== signature) return
    advancePreview.value = preview; advancePreviewRequest.value = request; advancePreviewSignature.value = signature
  }
  catch (cause) { if (context === contextGeneration && generation === advanceGeneration) error.value = errorText(cause) } finally { if (context === contextGeneration && generation === advanceGeneration && busyOwner === 'advance-preview') { busy.value = false; busyOwner = null } }
}
async function createAdvance(): Promise<void> {
  if (!quoteId.value || !advancePreview.value || !advancePreviewRequest.value || advancePreviewSignature.value !== advanceSignature() || busy.value) return
  busy.value = true; error.value = null
  try {
    const invoice = await post<InvoiceRead>(`/api/v1/quotes/${quoteId.value}/advance-invoices`, advancePreviewRequest.value)
    showAdvance.value = false; emit('changed'); router.push(`/invoices/${invoice.id}/edit`)
  } catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
async function createFinal(): Promise<boolean> {
  if (!quoteId.value || busy.value) return false
  busy.value = true; error.value = null
  try {
    const invoice = await post<InvoiceRead>(`/api/v1/quotes/${quoteId.value}/final-invoice`, { invoice_date: finalDate.value })
    showFinal.value = false; emit('changed'); router.push(`/invoices/${invoice.id}/edit`)
    return true
  } catch (cause) { error.value = errorText(cause); return false } finally { busy.value = false }
}
async function calculateCredit(): Promise<void> {
  if (!creditSourceId.value || busy.value || creditIntentConfirmation.value) return
  const request = creditRequest(); const signature = JSON.stringify(request); const generation = ++creditPreviewGeneration; const context = contextGeneration; const sourceId = creditSourceId.value
  busy.value = true; busyOwner = 'credit-preview'; error.value = null; creditPreview.value = null; creditPreviewSignature.value = null
  try {
    const preview = await post<CreditCalculationRead>(`/api/v1/invoices/${sourceId}/credit-notes/calculate`, creditIntent(request.full_remaining, (request.lines ?? []).map(line => ({ source_basis_line_id: line.source_basis_line_id, input_mode: line.input_mode, raw: String(line.quantity ?? line.gross_amount ?? '') }))))
    if (context !== contextGeneration || generation !== creditPreviewGeneration || sourceId !== creditSourceId.value || creditSignature() !== signature) return
    creditPreview.value = preview; creditPreviewRequest.value = request; creditPreviewSignature.value = signature
  }
  catch (cause) { if (context === contextGeneration && generation === creditPreviewGeneration && sourceId === creditSourceId.value) error.value = errorText(cause) } finally { if (context === contextGeneration && generation === creditPreviewGeneration && sourceId === creditSourceId.value && busyOwner === 'credit-preview') { busy.value = false; busyOwner = null } }
}
async function loadCreditSource(): Promise<void> {
  if (!creditSourceId.value || busy.value) return
  const context = contextGeneration; const generation = ++creditSourceGeneration; const sourceId = creditSourceId.value
  busy.value = true; busyOwner = 'credit-source'; error.value = null
  try {
    const preview = await post<CreditCalculationRead>(`/api/v1/invoices/${sourceId}/credit-notes/calculate`, { full_remaining: true })
    if (context === contextGeneration && generation === creditSourceGeneration && sourceId === creditSourceId.value) creditSourcePreview.value = preview
  }
  catch (cause) { if (context === contextGeneration && generation === creditSourceGeneration && sourceId === creditSourceId.value) error.value = errorText(cause) }
  finally { if (context === contextGeneration && generation === creditSourceGeneration && sourceId === creditSourceId.value && busyOwner === 'credit-source') { busy.value = false; busyOwner = null } }
}
async function createCredit(): Promise<void> {
  if (!invoiceId.value || !creditPreview.value || !creditPreviewRequest.value || creditPreviewSignature.value !== creditSignature() || busy.value || creditIntentConfirmation.value) return
  busy.value = true; error.value = null
  try {
    const invoice = await post<InvoiceRead>(`/api/v1/invoices/${invoiceId.value}/credit-notes`, creditPreviewRequest.value)
    showCredit.value = false; emit('changed'); router.push(`/invoices/${invoice.id}/edit`)
  } catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
async function updateAdvanceDraft(): Promise<void> {
  if (!invoiceId.value || !advanceRaw.value.trim() || !advancePreview.value || !advancePreviewRequest.value || advancePreviewSignature.value !== advanceSignature() || busy.value) return
  busy.value = true; error.value = null
  try {
    const updated = await put<InvoiceRead>(`/api/v1/advance-invoices/${invoiceId.value}`, advancePreviewRequest.value)
    emit('changed'); await load(); router.replace(`/invoices/${updated.id}/edit`)
  } catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
async function updateCreditDraft(): Promise<void> {
  if (!invoiceId.value || !creditPreview.value || !creditPreviewRequest.value || creditPreviewSignature.value !== creditSignature() || busy.value || creditIntentConfirmation.value) return
  busy.value = true; error.value = null
  try {
    const updated = await put<InvoiceRead>(`/api/v1/credit-notes/${invoiceId.value}`, creditPreviewRequest.value)
    emit('changed'); await load(); router.replace(`/invoices/${updated.id}/edit`)
  } catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
function requestFollowup(path: 'replacement' | 'compensating-invoice'): void {
  pendingFollowup.value = path
  showFollowupConfirm.value = true
}
async function createFollowup(): Promise<boolean> {
  const path = pendingFollowup.value
  if (!invoiceId.value || !path || busy.value) return false
  busy.value = true; error.value = null
  try {
    const invoice = await post<InvoiceRead>(`/api/v1/credit-notes/${invoiceId.value}/${path}`, {})
    showFollowupConfirm.value = false; pendingFollowup.value = null; emit('changed'); router.push(`/invoices/${invoice.id}/edit`)
    return true
  } catch (cause) { error.value = errorText(cause); return false } finally { busy.value = false }
}
async function previewCancellation(): Promise<void> {
  if (!quoteId.value || busy.value) return
  const request = { invoice_date: finalDate.value }; const signature = JSON.stringify(request); const generation = ++cancellationGeneration; const context = contextGeneration
  busy.value = true; busyOwner = 'cancellation-preview'; error.value = null; cancellationPreview.value = null; cancellationPreviewSignature.value = null
  try {
    const preview = await post<components['schemas']['ProjectCancellationPreview']>(`/api/v1/quotes/${quoteId.value}/cancellation/preview`, request)
    if (context !== contextGeneration || generation !== cancellationGeneration || cancellationSignature() !== signature) return
    cancellationPreview.value = preview; cancellationPreviewRequest.value = request; cancellationPreviewSignature.value = signature
  }
  catch (cause) { if (context === contextGeneration && generation === cancellationGeneration) error.value = errorText(cause) } finally { if (context === contextGeneration && generation === cancellationGeneration && busyOwner === 'cancellation-preview') { busy.value = false; busyOwner = null } }
}
function requestCancellation(): void { showCancellationConfirm.value = true }
async function createCancellation(): Promise<boolean> {
  if (!quoteId.value || !cancellationPreview.value || !cancellationPreviewRequest.value || cancellationPreviewSignature.value !== cancellationSignature() || busy.value) return false
  busy.value = true; error.value = null
  try {
    await post(`/api/v1/quotes/${quoteId.value}/cancellation/create-credit-drafts`, { ...cancellationPreviewRequest.value, preview_token: cancellationPreview.value.preview_token })
    showCancellationConfirm.value = false; showCancellation.value = false; emit('changed'); await load()
    return true
  } catch (cause) { error.value = errorText(cause); return false } finally { busy.value = false }
}
async function recordRefund(): Promise<void> {
  if (!invoiceId.value || !refundAmount.value.trim() || busy.value) return
  pendingRefundAction.value = editingRefund.value ? 'update' : 'create'
  showRefundConfirm.value = true
}
function refundPayload(): PaymentInput {
  return { payment_date: refundDate.value, amount: refundAmount.value, payment_method_id: refundMethodId.value, reference: refundReference.value, note: refundNote.value }
}
async function confirmRefund(): Promise<boolean> {
  if (!invoiceId.value || !pendingRefundAction.value || busy.value) return false
  busy.value = true; error.value = null
  try {
    if (pendingRefundAction.value === 'delete' && editingRefund.value) await del<PaymentMutationResponse>(`/api/v1/payments/${editingRefund.value.id}`)
    else if (pendingRefundAction.value === 'update' && editingRefund.value) await put<PaymentMutationResponse>(`/api/v1/payments/${editingRefund.value.id}`, refundPayload())
    else refund.value = await post<RefundCollectionRead>(`/api/v1/credit-notes/${invoiceId.value}/refunds`, refundPayload())
    refundAmount.value = ''; refundMethodId.value = null; refundReference.value = null; refundNote.value = null
    editingRefund.value = null; pendingRefundAction.value = null; showRefundConfirm.value = false
    emit('changed'); await load()
    return true
  } catch (cause) { error.value = errorText(cause); return false } finally { busy.value = false }
}
function editRefund(item: PaymentRead): void {
  editingRefund.value = item; refundAmount.value = String(item.amount); refundDate.value = item.payment_date
  refundMethodId.value = item.payment_method_id ?? null; refundReference.value = item.reference ?? null; refundNote.value = item.note ?? null
}
function deleteRefund(item: PaymentRead): void { editingRefund.value = item; pendingRefundAction.value = 'delete'; showRefundConfirm.value = true }
function previewRefundConfirmation(paymentId: string): void {
  refundPreviewSrc.value = `/api/v1/payments/${paymentId}/refund-confirmation/preview`
  refundPreviewShow.value = true
}
async function downloadRefundConfirmation(paymentId: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try { await downloadBlob(`/api/v1/payments/${paymentId}/refund-confirmation`, `refund-confirmation-${paymentId}.pdf`); await load() }
  catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
function openRefundSend(paymentId: string): void { refundSendId.value = paymentId; refundSendShow.value = true }
async function refundSent(): Promise<void> { await load() }
async function downloadArtifact(id: string, filename: string): Promise<void> {
  if (!invoiceId.value || busy.value) return
  busy.value = true
  try { await downloadBlob(`/api/v1/invoices/${invoiceId.value}/artifacts/${id}`, filename) }
  catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
async function downloadRefundArtifact(paymentId: string, artifactId: string, filename: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try { await downloadBlob(`/api/v1/payments/${paymentId}/artifacts/${artifactId}`, filename) }
  catch (cause) { error.value = errorText(cause) } finally { busy.value = false }
}
</script>

<template>
  <n-card class="workflow" :title="t('workflow.title')" size="small">
    <n-spin :show="loading || issuedOutputLoading">
        <n-alert v-if="error" type="error" closable @close="error = null">{{ error }}</n-alert>
        <n-alert v-if="resourceError" type="error" closable @close="resourceError = null">{{ resourceError }}</n-alert>
      <n-space vertical size="small">
        <n-space justify="space-between" wrap>
          <n-text strong>{{ t('workflow.mode') }}: {{ modeText(modeLabel) }}</n-text>
          <n-button text :loading="loading || chainLoading" :disabled="loading || chainLoading" @click="refresh">{{ t('workflow.refresh') }}</n-button>
        </n-space>
        <n-descriptions v-if="visibleChain" :column="2" bordered size="small">
          <n-descriptions-item :label="t('workflow.due')">{{ fmt(visibleChain.totals.due_amount) }}</n-descriptions-item>
          <n-descriptions-item :label="t('workflow.refundDue')">{{ fmt(visibleChain.totals.refund_due_amount) }}</n-descriptions-item>
          <n-descriptions-item :label="t('workflow.credit')">{{ fmt(visibleChain.totals.credit_total) }}</n-descriptions-item>
          <n-descriptions-item :label="t('workflow.settlement')">{{ fmt(visibleChain.totals.incoming_payment_total) }}</n-descriptions-item>
        </n-descriptions>
        <n-space v-if="quoteId" wrap>
          <n-button v-if="canAdvance" type="primary" :disabled="busy" @click="showAdvance = true">{{ t('workflow.createAdvance') }}</n-button>
          <n-button v-if="canFinal" type="primary" :disabled="busy" @click="showFinal = true">{{ t('workflow.createFinal') }}</n-button>
          <n-button v-if="canCancelProject" :disabled="busy" @click="showCancellation = true">{{ t('workflow.cancelProject') }}</n-button>
          <n-text v-if="modeLabel === 'RECEIPT_ONLY'" depth="3">{{ t('workflow.receiptOnly') }}</n-text>
          <n-text v-if="formalQuoteMode" v-for="action in actionCodes.filter(item => !item.available && ['CREATE_ADVANCE', 'CREATE_FINAL', 'CREATE_PROJECT_CANCELLATION'].includes(item.code))" :key="action.code" depth="3">{{ actionReason(action.code) }}</n-text>
        </n-space>
        <n-space v-if="invoiceId" wrap>
          <n-button v-if="sourceCanCredit" type="warning" :disabled="busy" @click="showCredit = true">{{ t('workflow.createCredit') }}</n-button>
          <template v-if="isCredit">
            <n-button v-if="canReplacement" :disabled="busy" @click="requestFollowup('replacement')">{{ t('workflow.replacement') }}</n-button>
            <n-button v-if="canCompensation" :disabled="busy" @click="requestFollowup('compensating-invoice')">{{ t('workflow.compensation') }}</n-button>
          </template>
        </n-space>
        <template v-if="invoice?.status === 'DRAFT' && invoice.document_kind === 'ADVANCE'">
          <n-divider>{{ t('workflow.advanceTitle') }}</n-divider>
          <n-form class="workflow-form" inline>
            <n-form-item :label="t('workflow.inputMode')"><n-select v-model:value="advanceMode" :options="[{ label: t('workflow.gross'), value: 'GROSS_AMOUNT' }, { label: t('workflow.percentage'), value: 'PERCENTAGE' }]" /></n-form-item>
            <n-form-item :label="t('workflow.rawInput')"><n-input v-model:value="advanceRaw" /></n-form-item>
            <n-form-item :label="t('invoices.invoiceDate')"><n-date-picker v-model:formatted-value="advanceDate" value-format="yyyy-MM-dd" type="date" /></n-form-item>
            <n-form-item :label="t('invoices.supplyOrAdvanceDate')"><n-date-picker v-model:formatted-value="advanceSupplyDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item>
            <n-form-item :label="t('invoices.dueDate')"><n-date-picker v-model:formatted-value="advanceDueDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item>
            <n-form-item :label="t('invoices.referenceNumber')"><n-input v-model:value="advanceReference" /></n-form-item>
            <n-button :loading="busy" @click="calculateAdvance">{{ t('workflow.calculate') }}</n-button>
            <n-button :disabled="!advancePreview" :loading="busy" @click="updateAdvanceDraft">{{ t('common.save') }}</n-button>
          </n-form>
          <n-alert v-if="advancePreview" type="info">{{ t('workflow.backendPreview') }}: {{ advancePreview.taxable_amount }} · {{ advancePreview.vat_total }} · {{ advancePreview.gross_amount }}<n-list v-if="advancePreview.buckets?.length" bordered style="margin-top:8px"><n-list-item v-for="bucket in advancePreview.buckets" :key="bucket.vat_rate_id">{{ bucket.vat_rate_label }} · {{ bucket.taxable_amount }} + {{ bucket.vat_amount }} = {{ bucket.gross_amount }}</n-list-item></n-list></n-alert>
          <n-text depth="3">{{ t('workflow.advanceLockedHint') }}</n-text>
        </template>
        <template v-if="invoice?.status === 'DRAFT' && invoice.document_kind === 'CREDIT_NOTE'">
          <n-divider>{{ t('workflow.creditTitle') }}</n-divider>
          <n-space><n-button :type="creditFull ? 'primary' : 'default'" @click="chooseCreditMode(true)">{{ t('workflow.fullRemaining') }}</n-button><n-button :type="!creditFull ? 'primary' : 'default'" @click="chooseCreditMode(false)">{{ t('workflow.selectLines') }}</n-button></n-space>
          <n-alert v-if="creditIntentConfirmation" type="warning" style="margin-top:8px">{{ t('workflow.creditIntentConfirmation') }}</n-alert>
          <n-button v-if="!creditFull" :loading="busy" style="margin-top:8px" @click="loadCreditSource">{{ t('workflow.loadSourceBasis') }}</n-button>
          <template v-if="!creditFull"><n-space v-for="(_, index) in creditRows" :key="index" wrap style="margin-top:8px"><n-select v-model:value="creditRows[index].source_basis_line_id" :options="(creditSourcePreview?.lines ?? []).map(line => ({ label: `${line.name} · ${line.quantity} · ${line.gross_amount}`, value: line.source_basis_line_id }))" style="min-width:220px" /><n-select v-model:value="creditRows[index].input_mode" :options="[{ label:t('workflow.quantity'), value:'QUANTITY' }, { label:t('workflow.gross'), value:'GROSS_AMOUNT' }]" /><n-input v-model:value="creditRows[index].raw" :placeholder="t('workflow.rawInput')" /><n-button :aria-label="t('common.delete')" @click="creditRows.splice(index, 1)">×</n-button></n-space></template>
          <n-form class="workflow-form" inline><n-form-item :label="t('invoices.invoiceDate')"><n-date-picker v-model:formatted-value="creditDate" value-format="yyyy-MM-dd" type="date" /></n-form-item><n-form-item :label="t('invoices.supplyOrAdvanceDate')"><n-date-picker v-model:formatted-value="creditSupplyDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.dueDate')"><n-date-picker v-model:formatted-value="creditDueDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.referenceNumber')"><n-input v-model:value="creditReference" /></n-form-item></n-form>
          <n-space style="margin-top:8px"><n-button v-if="!creditFull" @click="creditRows.push({ source_basis_line_id: '', input_mode: 'QUANTITY', raw: '' })">{{ t('workflow.addSelection') }}</n-button><n-button :disabled="busy || creditIntentConfirmation" @click="calculateCredit">{{ t('workflow.calculate') }}</n-button><n-button v-if="creditPreview" type="primary" :disabled="creditIntentConfirmation" :loading="busy" @click="updateCreditDraft">{{ t('common.save') }}</n-button></n-space>
          <n-alert v-if="creditPreview" type="info" style="margin-top:8px">{{ creditPreview.gross_amount }} · {{ t('workflow.remaining') }} {{ creditPreview.remaining_gross_amount }}</n-alert>
          <n-text depth="3">{{ t('workflow.creditLockedHint') }}</n-text>
        </template>
        <n-collapse v-model:expanded-names="expandedNames">
          <n-collapse-item name="document-chain" :title="t('workflow.timeline')">
            <n-empty v-if="!timelineRows.length" :description="t('workflow.noTimeline')" size="small" />
            <n-list v-else bordered class="workflow-scroll">
              <n-list-item v-for="item in timelineRows" :key="`${item.kind}:${item.order}`">
                <template v-if="item.kind === 'NODE' && item.node">
                  <template v-for="node in [item.node]" :key="node.id">
                <n-button text :disabled="!node.document_kind && !node.node_type.toLowerCase().includes('quote')" @click="goToNode(node)">
                  <n-tag v-if="node.document_kind" size="small">{{ t(invoiceDocumentKindLabelKey(node.document_kind)) }}</n-tag>
                  {{ node.number ?? node.node_type }} · {{ node.occurred_on }}
                </n-button>
                <n-text depth="3"> · {{ t('workflow.charge') }} {{ fmt(node.charge_amount) }} · {{ t('workflow.credit') }} {{ fmt(node.credit_amount) }} · {{ t('workflow.due') }} {{ fmt(node.due_amount) }} · {{ t('workflow.refundDue') }} {{ fmt(node.refund_due_amount) }}<template v-if="node.incoming_payment_amount && node.incoming_payment_amount !== '0'"> · {{ t('workflow.settlement') }} {{ fmt(node.incoming_payment_amount) }}</template><template v-if="node.refund_amount && node.refund_amount !== '0'"> · {{ t('workflow.refunds') }} {{ fmt(node.refund_amount) }}</template></n-text>
                  </template>
                </template>
                <template v-else-if="item.kind === 'EVENT' && item.event">{{ eventText(item.event) }} · {{ item.event.occurred_at }}</template>
                <template v-else-if="item.kind === 'RELATION' && item.relation">{{ relationText(item.relation) }}</template>
                <template v-else-if="item.kind === 'APPLICATION' && item.application">{{ t('workflow.finalApplications') }} · {{ item.application.taxable_amount }} + {{ item.application.vat_amount }} = {{ item.application.gross_amount }}</template>
              </n-list-item>
            </n-list>
          </n-collapse-item>
        </n-collapse>
        <template v-if="invoice?.document_kind === 'FINAL'">
          <n-divider>{{ t('workflow.finalApplications') }}</n-divider>
          <n-descriptions :column="1" bordered size="small"><n-descriptions-item :label="t('workflow.quoteTotal')">{{ invoice.original_quote_totals?.taxable_amount ?? '—' }} · {{ invoice.original_quote_totals?.vat_total ?? '—' }} · {{ invoice.original_quote_totals?.gross_amount ?? '—' }}</n-descriptions-item><n-descriptions-item :label="t('workflow.finalTotal')">{{ invoice.final_totals?.taxable_amount ?? '—' }} · {{ invoice.final_totals?.vat_total ?? '—' }} · {{ invoice.final_totals?.gross_amount ?? invoice.total_incl_vat }}</n-descriptions-item><n-descriptions-item :label="t('workflow.residualPayable')">{{ invoice.payable_before_payments }}</n-descriptions-item><n-descriptions-item :label="t('workflow.due')">{{ invoice.due_amount }}</n-descriptions-item><n-descriptions-item :label="t('workflow.variance')">{{ invoice.final_variance?.taxable_amount ?? '—' }} · {{ invoice.final_variance?.vat_amount ?? '—' }} · {{ invoice.final_variance?.gross_amount ?? '—' }}</n-descriptions-item></n-descriptions>
          <n-list bordered><n-list-item v-for="application in invoice.final_advance_applications ?? []" :key="application.advance_invoice_id">{{ application.advance_invoice_number }} · {{ application.taxable_amount }} + {{ application.vat_amount }} = {{ application.gross_amount }}<n-list v-if="application.taxes?.length" bordered style="margin-top:8px"><n-list-item v-for="tax in application.taxes" :key="tax.source_vat_rate_id">{{ tax.source_vat_rate_label }} {{ tax.source_vat_rate_percent }}% · {{ tax.taxable_amount }} + {{ tax.vat_amount }} = {{ tax.gross_amount }}</n-list-item></n-list></n-list-item></n-list>
        </template>
          <template v-if="isCredit && refund">
          <n-divider>{{ t('workflow.refunds') }}</n-divider>
          <n-descriptions :column="2" size="small"><n-descriptions-item :label="t('workflow.entitlement')">{{ refund.remaining_entitlement }}</n-descriptions-item><n-descriptions-item :label="t('workflow.refundDue')">{{ refund.chain_refund_due_amount }}</n-descriptions-item></n-descriptions>
          <n-form class="workflow-form" inline @submit.prevent="recordRefund"><n-form-item :label="t('workflow.amount')"><n-input v-model:value="refundAmount" /></n-form-item><n-form-item :label="t('workflow.date')"><n-date-picker v-model:formatted-value="refundDate" value-format="yyyy-MM-dd" type="date" /></n-form-item><n-form-item :label="t('payments.paymentMethod')"><n-select v-model:value="refundMethodId" :options="paymentMethodOptions" clearable @focus="loadPaymentMethods" /></n-form-item><n-form-item :label="t('payments.reference')"><n-input v-model:value="refundReference" /></n-form-item><n-form-item :label="t('payments.note')"><n-input v-model:value="refundNote" /></n-form-item><n-button type="primary" attr-type="submit" :loading="busy">{{ editingRefund ? t('common.save') : t('workflow.recordRefund') }}</n-button></n-form>
          <n-empty v-if="!refund.items?.length" :description="t('workflow.noRefunds')" size="small" />
          <n-list v-if="refund.items?.length" bordered class="workflow-scroll"><n-list-item v-for="item in refund.items" :key="item.id">{{ item.payment_date }} · {{ item.amount }} · {{ item.reference ?? '—' }} <n-space inline><n-button text @click="editRefund(item)">{{ t('common.edit') }}</n-button><n-button text type="error" @click="deleteRefund(item)">{{ t('common.delete') }}</n-button><n-button text @click="previewRefundConfirmation(item.id)">{{ t('pdf.preview') }}</n-button><n-button text :disabled="busy" @click="downloadRefundConfirmation(item.id)">{{ t('pdf.download') }}</n-button><n-button text type="primary" :disabled="busy" @click="openRefundSend(item.id)">{{ t('sendDialog.title') }}</n-button></n-space><n-list v-if="refundArtifacts[item.id]?.items.length" bordered style="margin-top:8px"><n-list-item v-for="artifact in refundArtifacts[item.id].items" :key="artifact.id">{{ artifact.filename }} · {{ artifactReasonText(artifact.creation_reason) }} · {{ formatDateTime(artifact.created_at, true) }} <n-button text :disabled="busy" @click="downloadRefundArtifact(item.id, artifact.id, artifact.filename)">{{ t('workflow.historicalDownload') }}</n-button></n-list-item></n-list><n-empty v-else :description="t('workflow.noArtifacts')" size="small" /></n-list-item></n-list>
        </template>
        <template v-if="artifacts">
          <n-divider>{{ t('workflow.artifacts') }}</n-divider>
          <n-text depth="3">{{ t('workflow.artifactHint') }}</n-text>
          <n-list v-if="artifacts.items.length" bordered class="workflow-scroll"><n-list-item v-for="artifact in artifacts.items" :key="artifact.id">{{ artifact.filename }} · {{ artifact.locale }} · {{ artifactReasonText(artifact.creation_reason) }} · {{ formatDateTime(artifact.created_at, true) }} <n-button text :disabled="busy" @click="downloadArtifact(artifact.id, artifact.filename)">{{ t('workflow.historicalDownload') }}</n-button></n-list-item></n-list>
          <n-alert v-if="uploadNotice" type="info" style="margin-top:8px">{{ uploadNotice.message }} <n-button v-if="uploadNotice.kind === 'AI_REVIEW'" text type="primary" @click="openSettings('ai')">{{ t('workflow.upload.openAiSettings') }}</n-button></n-alert>
          <template v-if="canUploadArtifact && uploadNotice?.kind !== 'ARTIFACT_CONFLICT'">
            <n-alert type="warning" style="margin-top:8px">{{ t('workflow.upload.permanentWarning') }}</n-alert>
            <n-form class="workflow-form" style="margin-top:8px">
              <n-form-item :label="t('workflow.upload.fileLabel')"><input ref="artifactFileInput" :aria-label="t('workflow.upload.fileLabel')" type="file" accept="application/pdf,.pdf" :disabled="validatingUpload || uploadingArtifact" @change="onArtifactFileChange" /></n-form-item>
              <n-text depth="3">{{ t('workflow.upload.sizeHint') }}</n-text>
              <n-alert v-if="uploadError" type="error" style="margin-top:8px">{{ uploadError }}</n-alert>
              <template v-if="uploadFile"><n-alert type="info" style="margin-top:8px">{{ t('workflow.upload.aiPrivacy') }}</n-alert><n-space style="margin-top:8px"><n-button :loading="validatingUpload" :disabled="uploadingArtifact" @click="checkArtifactWithAi">{{ t('workflow.upload.checkAi') }}</n-button><n-button type="primary" :loading="uploadingArtifact" :disabled="validatingUpload || uploadingArtifact" @click="requestArtifactUpload">{{ t('workflow.upload.submit') }}</n-button></n-space></template>
            </n-form>
            <n-alert v-if="validation" :type="validationStatusType(validation.status)" style="margin-top:8px"><strong>{{ t(`workflow.upload.status.${validation.status}`) }}</strong> · {{ validation.summary }}<div data-testid="validation-page-coverage" style="margin-top:8px">{{ t(validation.checked_pages.length < validation.total_pages ? 'workflow.upload.partialPageCheck' : 'workflow.upload.completePageCheck', { pages: validation.checked_pages.join(', '), total: validation.total_pages }) }}</div><n-list bordered style="margin-top:8px"><n-list-item v-for="check in validation.checks" :key="check.field"><n-tag :type="validationStatusType(check.status)">{{ t(`workflow.upload.status.${check.status}`) }}</n-tag> {{ validationFieldText(check.field) }}: {{ check.expected_value ?? '—' }} → {{ check.observed_value ?? '—' }}<span v-if="check.note"> · {{ check.note }}</span></n-list-item></n-list></n-alert>
          </template>
          <n-empty v-if="!artifacts.items.length && !(canUploadArtifact && uploadNotice?.kind !== 'ARTIFACT_CONFLICT')" :description="t('workflow.noArtifacts')" size="small" />
        </template>
      </n-space>
    </n-spin>
  </n-card>

  <n-modal v-model:show="showAdvance" preset="card" :title="t('workflow.advanceTitle')" class="workflow-modal" style="max-width: 520px">
    <n-form><n-form-item :label="t('workflow.inputMode')"><n-select v-model:value="advanceMode" :options="[{ label: t('workflow.gross'), value: 'GROSS_AMOUNT' }, { label: t('workflow.percentage'), value: 'PERCENTAGE' }]" /></n-form-item><n-form-item :label="t('workflow.rawInput')"><n-input v-model:value="advanceRaw" /></n-form-item><n-form-item :label="t('invoices.invoiceDate')"><n-date-picker v-model:formatted-value="advanceDate" value-format="yyyy-MM-dd" type="date" /></n-form-item><n-form-item :label="t('invoices.supplyOrAdvanceDate')"><n-date-picker v-model:formatted-value="advanceSupplyDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.dueDate')"><n-date-picker v-model:formatted-value="advanceDueDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.referenceNumber')"><n-input v-model:value="advanceReference" /></n-form-item><n-button :disabled="busy || !advanceRaw" @click="calculateAdvance">{{ t('workflow.calculate') }}</n-button><n-alert v-if="advancePreview" type="info" style="margin-top: 12px">{{ t('workflow.backendPreview') }}: {{ advancePreview.taxable_amount }} · {{ advancePreview.vat_total }} · {{ advancePreview.gross_amount }}<n-list v-if="advancePreview.buckets?.length" bordered style="margin-top:8px"><n-list-item v-for="bucket in advancePreview.buckets" :key="bucket.vat_rate_id">{{ bucket.vat_rate_label }} · {{ bucket.taxable_amount }} + {{ bucket.vat_amount }} = {{ bucket.gross_amount }}</n-list-item></n-list></n-alert><n-button v-if="advancePreview" type="primary" :disabled="busy" style="margin-top: 12px" @click="createAdvance">{{ t('workflow.createDraft') }}</n-button></n-form>
  </n-modal>
  <n-modal v-model:show="showFinal" preset="dialog" class="workflow-modal" :title="t('workflow.finalTitle')" :positive-text="t('workflow.createDraft')" :negative-text="t('common.cancel')" :loading="busy" @positive-click="createFinal"><n-alert v-if="error" type="error">{{ error }}</n-alert><n-form-item :label="t('workflow.date')"><n-date-picker v-model:formatted-value="finalDate" value-format="yyyy-MM-dd" type="date" /></n-form-item><n-text>{{ t('workflow.finalHint') }}</n-text></n-modal>
  <n-modal v-model:show="showCredit" preset="card" :title="t('workflow.creditTitle')" class="workflow-modal" style="max-width: 620px"><n-form><n-form-item><n-button :type="creditFull ? 'primary' : 'default'" @click="chooseCreditMode(true)">{{ t('workflow.fullRemaining') }}</n-button><n-button :type="!creditFull ? 'primary' : 'default'" style="margin-left: 8px" @click="chooseCreditMode(false)">{{ t('workflow.selectLines') }}</n-button></n-form-item><n-form-item :label="t('invoices.invoiceDate')"><n-date-picker v-model:formatted-value="creditDate" value-format="yyyy-MM-dd" type="date" /></n-form-item><n-form-item :label="t('invoices.supplyOrAdvanceDate')"><n-date-picker v-model:formatted-value="creditSupplyDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.dueDate')"><n-date-picker v-model:formatted-value="creditDueDate" value-format="yyyy-MM-dd" type="date" clearable /></n-form-item><n-form-item :label="t('invoices.referenceNumber')"><n-input v-model:value="creditReference" /></n-form-item><template v-if="!creditFull"><n-alert type="info">{{ t('workflow.creditIntentHint') }}</n-alert><n-spin :show="busy"><n-space v-for="(_, index) in creditRows" :key="index" wrap style="margin-top: 8px"><n-select v-model:value="creditRows[index].source_basis_line_id" :options="(creditSourcePreview?.lines ?? []).map(line => ({ label: `${line.name} · ${line.quantity} · ${line.gross_amount}`, value: line.source_basis_line_id }))" :placeholder="t('workflow.basisLine')" style="min-width:220px" /><n-select v-model:value="creditRows[index].input_mode" :options="[{label: t('workflow.quantity'), value: 'QUANTITY'}, {label: t('workflow.gross'), value: 'GROSS_AMOUNT'}]" style="width: 150px" /><n-input v-model:value="creditRows[index].raw" :placeholder="t('workflow.rawInput')" /><n-button :aria-label="t('common.delete')" @click="creditRows.splice(index, 1)">×</n-button></n-space></n-spin><n-button style="margin-top: 8px" @click="creditRows.push({ source_basis_line_id: '', input_mode: 'QUANTITY', raw: '' })">{{ t('workflow.addSelection') }}</n-button></template><n-button style="margin-top: 12px" :disabled="busy" @click="calculateCredit">{{ t('workflow.calculate') }}</n-button><n-alert v-if="creditPreview" type="info" style="margin-top: 12px">{{ t('workflow.backendPreview') }}: {{ creditPreview.gross_amount }} · {{ t('workflow.remaining') }} {{ creditPreview.remaining_gross_amount }}</n-alert><n-button v-if="creditPreview" type="primary" :disabled="busy" style="margin-top: 12px" @click="createCredit">{{ t('workflow.createDraft') }}</n-button></n-form></n-modal>
  <n-modal v-model:show="showRefundConfirm" preset="dialog" class="workflow-modal" :title="t('workflow.refundConfirmTitle')" :positive-text="t('common.confirm')" :negative-text="t('common.cancel')" :loading="busy" @positive-click="confirmRefund"><n-alert v-if="error" type="error">{{ error }}</n-alert><n-text>{{ t('workflow.creditTitle') }}: {{ invoice?.invoice_number ?? invoice?.id }} · {{ t('workflow.basisLine') }}: {{ sourceLabel }} · {{ pendingRefundAction === 'delete' ? `${editingRefund?.payment_date ?? ''} · ${editingRefund?.amount ?? ''} · ${t('workflow.refundDeleteConfirm')}` : `${refundDate} · ${refundAmount}` }}</n-text></n-modal>
  <n-modal v-model:show="showFollowupConfirm" preset="dialog" class="workflow-modal" :title="t('workflow.followupConfirmTitle')" :positive-text="t('common.confirm')" :negative-text="t('common.cancel')" :loading="busy" @positive-click="createFollowup"><n-alert v-if="error" type="error">{{ error }}</n-alert><n-text>{{ t('workflow.creditTitle') }}: {{ invoice?.invoice_number ?? invoice?.id }} · {{ t('workflow.basisLine') }}: {{ nodeLabels.get(followupContext?.source_invoice_id ?? '') ?? sourceLabel }} · {{ t(`workflow.relations.${followupContext?.relation_type ?? ''}`, followupContext?.relation_type ?? '—') }} · {{ followupContext ? t(invoiceDocumentKindLabelKey(followupContext.target_document_kind)) : '—' }} · {{ followupContext?.gross_amount ?? '—' }}</n-text></n-modal>
  <n-modal v-model:show="showCancellation" preset="card" class="workflow-modal" :title="t('workflow.cancellationTitle')" style="max-width: 520px"><n-button :loading="busy" @click="previewCancellation">{{ t('workflow.previewCancellation') }}</n-button><n-list v-if="cancellationPreview" bordered class="workflow-scroll" style="margin-top: 12px"><n-list-item v-for="source in cancellationPreview.sources" :key="source.source_invoice_id">{{ source.source_invoice_number }} · {{ source.remaining_gross_amount }}</n-list-item></n-list><n-button v-if="cancellationPreview" type="error" :disabled="busy" style="margin-top: 12px" @click="requestCancellation">{{ t('workflow.createCancellationDrafts') }}</n-button></n-modal>
  <n-modal v-model:show="showCancellationConfirm" preset="dialog" class="workflow-modal" :title="t('workflow.cancellationConfirmTitle')" :positive-text="t('common.confirm')" :negative-text="t('common.cancel')" :loading="busy" @positive-click="createCancellation"><n-alert v-if="error" type="error">{{ error }}</n-alert><n-text>{{ t('workflow.cancellationConfirm') }}</n-text><n-list v-if="cancellationPreview" bordered style="margin-top:8px"><n-list-item v-for="source in cancellationPreview.sources" :key="source.source_invoice_id">{{ source.source_invoice_number }} · {{ t(invoiceDocumentKindLabelKey(source.document_kind)) }} · {{ source.remaining_net_amount }} + {{ source.remaining_vat_amount }} = {{ source.remaining_gross_amount }}</n-list-item></n-list></n-modal>
  <n-modal v-model:show="showArtifactUploadConfirm" preset="dialog" class="workflow-modal" :title="t('workflow.upload.confirmTitle')" :positive-text="t('workflow.upload.confirm')" :negative-text="t('common.cancel')" :loading="uploadingArtifact" @positive-click="confirmArtifactUpload"><n-text>{{ t('workflow.upload.confirmBody', { invoice: invoice?.invoice_number ?? invoiceId, filename: uploadFile?.name }) }}</n-text></n-modal>
  <PdfPreviewDialog v-model:show="refundPreviewShow" :src="refundPreviewSrc" fallback-filename="refund-confirmation.pdf" />
  <DocumentSendDialog v-if="refundSendId" v-model:show="refundSendShow" doc-type="refund" :doc-id="refundSendId" :customer-email="invoice?.party_snapshot_customer_email" :customer-locale="invoice?.party_snapshot_locale" @sent="refundSent" />
</template>

<style scoped>
.workflow { margin-bottom: 16px; }
.workflow-form { display: flex; flex-wrap: wrap; }
.workflow-scroll { overflow-x: auto; }
@media (max-width: 640px) {
  .workflow :deep(.n-descriptions-table), .workflow-scroll { display: block; overflow-x: auto; }
  .workflow-form { display: block; }
  :global(.workflow-modal) { width: calc(100vw - 24px) !important; max-height: calc(100vh - 24px); overflow: auto; }
}
</style>
