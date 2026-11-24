<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NCheckbox, NEmpty, NForm, NFormItem, NInput, NInputNumber, NSelect, NSpin, NSpace, NTag, NUpload } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { ApiError, post } from '../../api/http'
import { useReceiptInboxStore, type Inbox, type InboxStatus } from '../../stores/receiptInbox'
import { resolvePurchaseTreatmentSuggestion, toExpenseInput, usePurchaseExpenseDictionaries } from '../../composables/usePurchaseExpenseDictionaries'
import { localDateStr } from '../../utils/date'
import type { components } from '../../api/schema'
import ResidualEstimate from '../../components/fixed-assets/ResidualEstimate.vue'

type ExpenseInput = components['schemas']['ExpenseInput']
type Calc = components['schemas']['ExpenseCalculateResult']
type PlCostBasis = components['schemas']['PlCostBasis']
type AiSelectionSnapshot = { treatmentId: string | null; treatmentOrigin: 'none' | 'ai' | 'human'; rateId: string | null; rateOrigin: 'none' | 'ai' | 'human'; edited: Set<string> }
type RetryTask = { id: string; routeVersion: number; fileRevision: number; generation: number; startedUpdatedAt: string; startedAfterUnverified: boolean; reviewedUpdatedAt?: string; state: 'running' | 'staged' | 'failed'; result?: Inbox; confirmation?: number; resumeAfter?: number }
const retryTask = ref<RetryTask | null>(null)
let retryGeneration = 0
const route = useRoute(), router = useRouter(), { t, locale } = useI18n()
const inbox = useReceiptInboxStore()
const dict = usePurchaseExpenseDictionaries()
const item = ref<Inbox | null>(null)
const status = ref<InboxStatus>('PENDING')
const offset = ref(0)
const uploadKey = ref(0)
let loadVersion = 0
let previewVersion = 0
let routeVersion = 0
const busy = ref(false), action = ref(''), error = ref(''), success = ref('')
const fileGroupChangedNotice = ref(false)
const fileActionPending = ref(false)
const fileGroupUnverified = ref(false)
const retryPending = computed(() => retryTask.value?.state === 'running')
const selectedFiles = ref<File[]>([])
const expenseDate = ref(localDateStr(new Date())), categoryId = ref<string | null>(null), supplier = ref('')
const treatmentId = ref<string | null>(null), rateId = ref<string | null>(null), net = ref<number | null>(null), vat = ref<number | null>(null)
// Keep receipt evidence separate from values returned by ordinary VAT preview.
const receiptVatBaseline = ref<number | null>(null)
const vatFromOrdinaryPreview = ref(false)
const receiptVatSource = ref<'none' | 'ai' | 'human'>('none')
const currentExtractionVatMissing = ref(false)
const deductible = ref<boolean | null>(true), paidBy = ref<'BUSINESS' | 'PRIVATE'>('BUSINESS'), businessPct = ref<number>(100), years = ref<number>(1), reference = ref(''), note = ref('')
const includeInPl = ref(true), plCostBasis = ref<PlCostBasis>('AUTO')
const registerAsset = ref(false), assetName = ref(''), assetInServiceDate = ref('')
const assetUsefulLife = ref(5), assetResidualValue = ref<number>(0)
const residualEvidenceEpoch = ref(0)
const assetUseType = ref<'FULL_BUSINESS' | 'MIXED_USE'>('FULL_BUSINESS'), assetBusinessPercentage = ref<number | null>(null)
const residualEstimateCostInputs = computed<components['schemas']['ExpenseCostPreviewRequest'] | null>(() => {
  if (net.value == null || vat.value == null || !treatmentId.value) return null
  return {
    net_amount: net.value,
    vat_amount: vat.value,
    vat_treatment_id: treatmentId.value,
    category_id: categoryId.value,
    deductible: deductible.value,
    pl_cost_basis: plCostBasis.value,
    include_in_pl: includeInPl.value,
    business_percentage: businessPct.value,
    depreciation_years: years.value,
  }
})
const previewGross = ref<string | null>(null)
const edited = ref(new Set<string>())
const aiOwnedFields = new Set<string>()
const treatmentOrigin = ref<'none' | 'ai' | 'human'>('none')
const rateOrigin = ref<'none' | 'ai' | 'human'>('none')
const suggestionInvalidated = ref(false)
let extractionVersion = 0
const id = computed(() => String(route.params.id ?? ''))
const isDetail = computed(() => !!id.value)
const pageStart = computed(() => inbox.total ? offset.value + 1 : 0)
const pageEnd = computed(() => Math.min(offset.value + inbox.items.length, inbox.total))
const pending = computed(() => item.value?.status === 'PENDING')
const aiMissing = computed(() => {
  const s = item.value?.ai_suggestion
  return !s || !s.expense_date || !s.supplier_name || s.net_amount == null || s.vat_amount == null || (!s.suggested_category_id && !s.suggested_category_name)
})
function markEdited(field: string) { edited.value.add(field) }
function timestampMicros(value: string): number | null {
  const match = /^(.*?)(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!match) return null
  const wholeSecond = Date.parse(`${match[1]}${match[3]}`)
  if (!Number.isFinite(wholeSecond)) return null
  const fraction = (match[2] ?? '').padEnd(6, '0').slice(0, 6)
  return wholeSecond * 1000 + Number(fraction)
}
function isLaterTimestamp(left: string, right: string) {
  const leftMicros = timestampMicros(left), rightMicros = timestampMicros(right)
  return leftMicros != null && rightMicros != null && leftMicros > rightMicros
}
function rememberReviewedSnapshot(task: RetryTask, updatedAt: string) {
  if (!task.reviewedUpdatedAt || isLaterTimestamp(updatedAt, task.reviewedUpdatedAt)) task.reviewedUpdatedAt = updatedAt
}
function retryResultIsFresh(task: RetryTask, result: Inbox) {
  return !task.reviewedUpdatedAt || isLaterTimestamp(result.updated_at, task.reviewedUpdatedAt)
}
function clearAiOwnedFields() {
  const fields: Record<string, () => void> = {
    expenseDate: () => { expenseDate.value = localDateStr(new Date()) },
    supplier: () => { supplier.value = '' }, net: () => { net.value = null }, vat: () => { vat.value = null },
    categoryId: () => { categoryId.value = null }, deductible: () => { deductible.value = true }, note: () => { note.value = '' },
  }
  for (const field of aiOwnedFields) if (!edited.value.has(field)) fields[field]?.()
  receiptVatBaseline.value = edited.value.has('vat') ? vat.value : null
  receiptVatSource.value = edited.value.has('vat') ? 'human' : 'none'
  vatFromOrdinaryPreview.value = false
  aiOwnedFields.clear()
  if (treatmentOrigin.value === 'ai' && !edited.value.has('treatmentId')) { treatmentId.value = null; treatmentOrigin.value = 'none' }
  if (rateOrigin.value === 'ai' && !edited.value.has('rateId')) { rateId.value = null; rateOrigin.value = 'none' }
  previewVersion++; previewGross.value = null
}
const treatmentSuggestion = computed(() => suggestionInvalidated.value ? null : item.value?.ai_suggestion?.suggested_vat_treatment ?? null)
const treatmentSuggestionRecord = computed(() => {
  return resolvePurchaseTreatmentSuggestion(treatmentSuggestion.value, dict.treatments.value)
})
const canApplyTreatmentSuggestion = computed(() => !action.value && !retryPending.value && !fileGroupUnverified.value && !!treatmentSuggestionRecord.value && treatmentSuggestionRecord.value.id !== treatmentId.value)
function errorMessage(e: unknown) {
  if (e instanceof ApiError && e.status === 409) {
    const detail = e.detail as { detail?: string } | string | null
    const code = typeof detail === 'string' ? detail : detail?.detail
    if (code && code in i18nErrors) return t(`receiptInbox.errors.${code}`)
  }
  return e instanceof Error ? e.message : String(e)
}
const i18nErrors = { INBOX_NOT_PENDING: true, FILE_LIMIT_REACHED: true, LAST_FILE: true, ALREADY_CONFIRMED: true, FILE_GROUP_CHANGED: true }

async function load() {
  const version = ++loadVersion
  const targetId = id.value
  busy.value = true; error.value = ''
  try {
    if (targetId) {
      const result = await inbox.detail(targetId)
      if (version === loadVersion) {
        fileGroupUnverified.value = false
        suggestionInvalidated.value = false
        selectedFiles.value = []; uploadKey.value++
        acceptSnapshot(result)
      }
    } else await inbox.fetch(status.value, offset.value)
  } catch (e) { if (version === loadVersion) error.value = errorMessage(e) }
  finally { if (version === loadVersion) busy.value = false }
}
function resetDraft() {
  expenseDate.value = localDateStr(new Date()); categoryId.value = null; supplier.value = ''
  treatmentId.value = null; rateId.value = null; net.value = null; vat.value = null
  receiptVatBaseline.value = null; vatFromOrdinaryPreview.value = false
  receiptVatSource.value = 'none'
  currentExtractionVatMissing.value = false
  deductible.value = true; paidBy.value = 'BUSINESS'; businessPct.value = 100; years.value = 1
  includeInPl.value = true; plCostBasis.value = 'AUTO'
  registerAsset.value = false; assetName.value = ''; assetInServiceDate.value = ''
  assetUsefulLife.value = 5; assetResidualValue.value = 0
  assetUseType.value = 'FULL_BUSINESS'; assetBusinessPercentage.value = null
  reference.value = ''; note.value = ''; previewGross.value = null
  edited.value = new Set()
  treatmentOrigin.value = 'none'; rateOrigin.value = 'none'; ++extractionVersion
  suggestionInvalidated.value = false
  selectedFiles.value = []; uploadKey.value++; previewVersion++
  action.value = ''; error.value = ''; success.value = ''; fileGroupChangedNotice.value = false
  fileActionPending.value = false; fileGroupUnverified.value = false
  suggestionInvalidated.value = false
  aiOwnedFields.clear(); retryTask.value = null; retryGeneration++
}
watch(id, () => { ++routeVersion; ++loadVersion; item.value = null; resetDraft(); void load() }, { immediate: true })
onMounted(async () => {
  try { await dict.load() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  if (item.value) applySuggestion(item.value)
})
watch(() => JSON.stringify(dict.treatments.value), () => { if (item.value) applySuggestion(item.value) })
async function refresh(data?: Inbox, targetId = id.value, version = routeVersion, preserveOrdinaryRate = false) {
  const result = data ?? (targetId ? await inbox.detail(targetId) : null)
  if (version === routeVersion && targetId === id.value && result) acceptSnapshot(result, preserveOrdinaryRate)
}
function acceptSnapshot(result: Inbox, preserveOrdinaryRate = false) {
  const previousRevision = item.value?.file_revision
  if (previousRevision != null && result.file_revision !== previousRevision) {
    clearAiOwnedFields()
    currentExtractionVatMissing.value = false
    fileGroupChangedNotice.value = true
    retryTask.value = null; retryGeneration++
  }
  item.value = result
  fileGroupUnverified.value = false
  suggestionInvalidated.value = false
  applySuggestion(result, preserveOrdinaryRate)
}
function changeStatus(next: InboxStatus) { status.value = next; offset.value = 0; void load() }
function changePage(next: number) { offset.value = next; void load() }
function onFiles(info: { fileList: UploadFileInfo[] }) { selectedFiles.value = info.fileList.flatMap(x => x.file ? [x.file] : []) }
function captureAiSelection(): AiSelectionSnapshot {
  return { treatmentId: treatmentId.value, treatmentOrigin: treatmentOrigin.value, rateId: rateId.value, rateOrigin: rateOrigin.value, edited: new Set(edited.value) }
}
function restoreAiSelection(snapshot: AiSelectionSnapshot) {
  const wasEdited = (field: string) => !snapshot.edited.has(field) && edited.value.has(field)
  if (snapshot.treatmentOrigin === 'ai' && !wasEdited('treatmentId') && treatmentOrigin.value !== 'human') {
    treatmentId.value = snapshot.treatmentId; treatmentOrigin.value = 'ai'
  }
  if (snapshot.rateOrigin === 'ai' && !wasEdited('rateId') && rateOrigin.value !== 'human') {
    rateId.value = snapshot.rateId; rateOrigin.value = 'ai'
  }
}
async function upload() {
  if (!selectedFiles.value.length || action.value === 'confirm' || fileActionPending.value || fileGroupUnverified.value) return
  const targetId = id.value
  const version = routeVersion
  const extraction = ++extractionVersion
  const selectionSnapshot = captureAiSelection()
  action.value = 'upload'; error.value = ''
  if (targetId) { fileActionPending.value = true; fileGroupUnverified.value = true; invalidateFileSuggestion() }
  try {
    const result = targetId ? await inbox.append(targetId, selectedFiles.value, locale.value) : await inbox.upload(selectedFiles.value, locale.value)
    if (version !== routeVersion || extraction !== extractionVersion) return
    if (targetId) { fileGroupUnverified.value = false; invalidateFileSuggestion() }
    selectedFiles.value = []; uploadKey.value++; acceptSnapshot(result)
    if (!targetId) await router.push(`/receipt-inbox/${result.id}`)
  } catch (e) {
    if (version === routeVersion) {
      error.value = errorMessage(e)
      if (targetId && isDefinitiveFileRejection(e, 'append')) {
        fileGroupUnverified.value = false; suggestionInvalidated.value = false
        restoreAiSelection(selectionSnapshot)
        restoreAfterRejectedMutation(targetId)
      } else if (targetId) await recoverFileGroup(targetId, version, extraction)
    }
  }
  finally {
    if (version === routeVersion) fileActionPending.value = false
    if (version === routeVersion && extraction === extractionVersion) action.value = ''
  }
}
async function removeFile(fileId: string) {
  if (action.value === 'confirm' || fileActionPending.value || fileGroupUnverified.value) return
  const targetId = id.value
  const version = routeVersion
  const extraction = ++extractionVersion
  const selectionSnapshot = captureAiSelection()
  fileActionPending.value = true
  fileGroupUnverified.value = true
  invalidateFileSuggestion()
  action.value = `remove:${fileId}`; error.value = ''
  try { const result = await inbox.removeFile(targetId, fileId); if (extraction === extractionVersion) { fileGroupUnverified.value = false; invalidateFileSuggestion(); suggestionInvalidated.value = false; await refresh(result, targetId, version) } } catch (e) {
    if (version === routeVersion && extraction === extractionVersion) {
      error.value = errorMessage(e)
      if (isDefinitiveFileRejection(e, 'remove')) {
        fileGroupUnverified.value = false; suggestionInvalidated.value = false
        restoreAiSelection(selectionSnapshot)
        restoreAfterRejectedMutation(targetId)
      } else await recoverFileGroup(targetId, version, extraction)
    }
  }
  finally {
    if (version === routeVersion) fileActionPending.value = false
    if (version === routeVersion && extraction === extractionVersion) action.value = ''
  }
}
async function retry() {
  if (action.value === 'confirm' || fileActionPending.value || retryPending.value) return
  const targetId = id.value
  const version = routeVersion
  const extraction = ++extractionVersion
  const generation = ++retryGeneration
  const fileRevision = item.value?.file_revision ?? -1
  const task = reactive<RetryTask>({ id: targetId, routeVersion: version, fileRevision, generation, startedUpdatedAt: item.value?.updated_at ?? '', startedAfterUnverified: fileGroupUnverified.value, state: 'running' })
  retryTask.value = task
  action.value = 'retry'; error.value = ''
  try {
    const result = await inbox.retry(targetId, locale.value)
    const current = retryTask.value === task && version === routeVersion && targetId === id.value
      && (fileGroupUnverified.value || item.value?.file_revision === fileRevision)
    if (!current) return
    task.state = 'staged'; task.result = result
    if (task.confirmation != null) return
    if (fileGroupUnverified.value && !task.startedAfterUnverified) return
    if (fileGroupUnverified.value) {
      fileGroupUnverified.value = false; selectedFiles.value = []; uploadKey.value++
    }
    if (task.resumeAfter != null) {
      task.resumeAfter = undefined
      const notSuperseded = retryResultIsFresh(task, result)
      if (!notSuperseded) { retryTask.value = null; return }
      retryTask.value = null
    }
    acceptSnapshot(result, true)
  } catch (e) {
    if (retryTask.value === task && version === routeVersion && targetId === id.value) {
      task.state = 'failed'
      if (task.resumeAfter != null) {
        task.resumeAfter = undefined
        retryTask.value = null
      }
      if (task.confirmation == null) retryTask.value = null
      // The coherent detail snapshot remains authoritative if the retry transport failed.
      if (extraction === extractionVersion || task.confirmation != null) error.value = errorMessage(e)
    }
  }
  finally {
    if (retryTask.value === task && task.state === 'running') retryTask.value = null
    if (version === routeVersion && extraction === extractionVersion) action.value = ''
  }
}
function isDefinitiveFileRejection(e: unknown, actionKind: 'append' | 'remove') {
  if (!e || typeof e !== 'object') return false
  const failure = e as { status?: unknown; detail?: unknown }
  if (typeof failure.status !== 'number' || failure.status < 400 || failure.status >= 500) return false
  const detail = failure.detail as { detail?: string } | string | null
  const code = typeof detail === 'string' ? detail : detail?.detail
  return code === 'LAST_FILE' || code === 'FILE_LIMIT_REACHED' || (actionKind === 'append' && failure.status === 422)
}
function restoreAfterRejectedMutation(targetId: string) {
  const task = retryTask.value
  if (task?.id === targetId && task.fileRevision === item.value?.file_revision && task.state === 'staged' && !task.confirmation) {
    const completed = task.result; retryTask.value = null
    if (completed && retryResultIsFresh(task, completed)) { acceptSnapshot(completed, true); return }
  }
  if (item.value) applySuggestion(item.value, true)
}
async function recoverFileGroup(targetId: string, version: number, extraction: number) {
  fileGroupUnverified.value = true
  suggestionInvalidated.value = true
  try {
    const current = await inbox.detail(targetId)
    if (version === routeVersion && targetId === id.value && extraction === extractionVersion) {
      fileGroupUnverified.value = false
      suggestionInvalidated.value = false
      selectedFiles.value = []; uploadKey.value++
      const task = retryTask.value
      if (task?.id === targetId && task.fileRevision === current.file_revision) {
        if (task.state === 'staged' && task.result) {
          rememberReviewedSnapshot(task, current.updated_at)
          const retryIsNewer = retryResultIsFresh(task, task.result)
          if (retryIsNewer) { retryTask.value = null; acceptSnapshot(task.result, true); return }
          retryTask.value = null
        } else if (task.state === 'running') {
          rememberReviewedSnapshot(task, current.updated_at)
          task.resumeAfter = extraction
        }
      }
      acceptSnapshot(current)
    }
  } catch {
    // Keep confirmation blocked until a retry or detail response returns the current group.
  }
}
async function discard() {
  if (action.value === 'confirm' || fileActionPending.value || fileGroupUnverified.value) return
  const targetId = id.value
  const version = routeVersion
  const extraction = ++extractionVersion
  action.value = 'discard'; error.value = ''
  try { await inbox.discard(targetId); if (version === routeVersion && extraction === extractionVersion) await router.push('/receipt-inbox') } catch (e) { if (version === routeVersion && extraction === extractionVersion) error.value = errorMessage(e) }
  finally { if (version === routeVersion && extraction === extractionVersion) action.value = '' }
}
function applySuggestion(source: Inbox, preserveOrdinaryRate = false) {
  if (suggestionInvalidated.value || fileGroupUnverified.value) return
  const s = source.ai_suggestion
  // Only a completed extraction is evidence about receipt VAT. Running and failed
  // snapshots can carry no suggestion (or an older one), so they cannot erase it.
  if (source.ai_state === 'READY') {
    currentExtractionVatMissing.value = !s || s.vat_amount === null || s.vat_amount === undefined
  }
  if (!s) {
    if (treatmentOrigin.value === 'ai') { treatmentId.value = null; treatmentOrigin.value = 'none' }
    if (rateOrigin.value === 'ai' && (!preserveOrdinaryRate || isReverseCharge.value)) { rateId.value = null; rateOrigin.value = 'none' }
    return
  }
  const suggestedTreatment = s.suggested_vat_treatment
  if (treatmentOrigin.value === 'ai') { treatmentId.value = null; treatmentOrigin.value = 'none' }
  const reverseSuggestion = suggestedTreatment?.code === 'EU_B2B_REVERSE_PURCH'
  const matchedRateId = !reverseSuggestion && !isReverseCharge.value && s.vat_rate_percent != null
    ? dict.rates.value.find(x => Math.abs(Number(x.percent) - Number(s.vat_rate_percent)) < 0.01)?.id
    : undefined
  if (rateOrigin.value === 'ai' && (!preserveOrdinaryRate || reverseSuggestion || matchedRateId || isReverseCharge.value)) { rateId.value = null; rateOrigin.value = 'none' }
  if (!edited.value.has('expenseDate') && s.expense_date) { expenseDate.value = s.expense_date; aiOwnedFields.add('expenseDate') }
  if (!edited.value.has('supplier') && s.supplier_name) { supplier.value = s.supplier_name; aiOwnedFields.add('supplier') }
  if (!edited.value.has('net') && s.net_amount != null) { net.value = Number(s.net_amount); aiOwnedFields.add('net') }
  if (!edited.value.has('vat') && s.vat_amount != null) {
    vat.value = Number(s.vat_amount); receiptVatBaseline.value = vat.value
    receiptVatSource.value = 'ai'
    vatFromOrdinaryPreview.value = false; aiOwnedFields.add('vat')
  }
  if (!edited.value.has('categoryId')) {
    const suggestedCategory = s.suggested_category_id ?? (s.suggested_category_name ? dict.categories.value.find(x => x.name.toLowerCase() === s.suggested_category_name!.toLowerCase())?.id : undefined)
    if (suggestedCategory) { categoryId.value = suggestedCategory; aiOwnedFields.add('categoryId') }
    if (categoryId.value && !edited.value.has('deductible')) { onCategoryChange(categoryId.value); aiOwnedFields.add('deductible') }
  }
  if (suggestedTreatment && treatmentSuggestionRecord.value && treatmentOrigin.value === 'none' && !edited.value.has('treatmentId')) {
    treatmentId.value = treatmentSuggestionRecord.value.id; treatmentOrigin.value = 'ai'; aiOwnedFields.add('treatmentId')
  }
  enterReverseChargeIfSelected()
  const reverse = reverseSuggestion
  if (!reverse && !isReverseCharge.value && !edited.value.has('rateId') && matchedRateId) {
    if (rateOrigin.value === 'none') { rateId.value = matchedRateId; rateOrigin.value = 'ai'; aiOwnedFields.add('rateId') }
  }
  if (!edited.value.has('note') && s.summary) { note.value = s.summary; aiOwnedFields.add('note') }
  previewVersion++; previewGross.value = null
}
function invalidateFileSuggestion() {
  residualEvidenceEpoch.value++
  suggestionInvalidated.value = true
  if (treatmentOrigin.value === 'ai') { treatmentId.value = null; treatmentOrigin.value = 'none' }
  if (rateOrigin.value === 'ai') { rateId.value = null; rateOrigin.value = 'none' }
  invalidatePreview()
}
function onTreatmentChange(value: string | null) {
  markEdited('treatmentId'); treatmentId.value = value; treatmentOrigin.value = 'human'
  enterReverseChargeIfSelected()
  previewVersion++; previewGross.value = null
  if (isReverseCharge.value) { if (rateOrigin.value === 'ai') { rateId.value = null; rateOrigin.value = 'none' }; invalidatePreview() }
}
function applyTreatmentSuggestion() {
  if (!treatmentSuggestionRecord.value) return
  treatmentId.value = treatmentSuggestionRecord.value.id; treatmentOrigin.value = 'human'; markEdited('treatmentId')
  enterReverseChargeIfSelected()
  if (treatmentSuggestion.value?.code === 'EU_B2B_REVERSE_PURCH' && rateOrigin.value === 'ai') { rateId.value = null; rateOrigin.value = 'none' }
  invalidatePreview()
}
const isReverseCharge = computed(() => dict.treatments.value.find(x => x.id === treatmentId.value)?.effect === 'ZERO_REVERSE')
function invalidatePreview() { previewVersion++; previewGross.value = null }
function enterReverseChargeIfSelected() {
  if (isReverseCharge.value && currentExtractionVatMissing.value
    && receiptVatSource.value !== 'human' && !edited.value.has('vat')) {
    vat.value = null
    receiptVatBaseline.value = null
    receiptVatSource.value = 'none'
    aiOwnedFields.delete('vat')
    vatFromOrdinaryPreview.value = false
  }
  if (isReverseCharge.value && vatFromOrdinaryPreview.value) {
    vat.value = receiptVatBaseline.value
    vatFromOrdinaryPreview.value = false
  }
}
function onCategoryChange(newId: string | null) {
  const category = dict.categories.value.find(x => x.id === newId)
  if (!edited.value.has('deductible')) deductible.value = category?.default_deductible ?? true
}
function onVatChange() {
  receiptVatBaseline.value = vat.value
  receiptVatSource.value = 'human'
  vatFromOrdinaryPreview.value = false
  previewVersion++; previewGross.value = null
}
async function preview() {
  const version = ++previewVersion
  const targetId = id.value
  const amount = net.value, rate = rateId.value
  previewGross.value = null
  if (isReverseCharge.value || net.value == null || !rateId.value) { previewGross.value = null; return }
  try {
    const result = await post<Calc>('/api/v1/expenses/calculate', { net_amount: amount, vat_rate_id: rate })
    if (version === previewVersion && targetId === id.value && amount === net.value && rate === rateId.value && !isReverseCharge.value) {
      const calculatedVat = Number(result.vat_amount)
      if (!edited.value.has('vat')) {
        vat.value = calculatedVat
        vatFromOrdinaryPreview.value = true
        aiOwnedFields.add('vat')
      }
      previewGross.value = vat.value === calculatedVat ? result.gross_amount : null
    }
  } catch (e) { if (version === previewVersion && targetId === id.value) error.value = e instanceof Error ? e.message : String(e) }
}
async function confirmExpense() {
  if (fileActionPending.value || fileGroupUnverified.value || action.value === 'confirm') return
  if (!pending.value || item.value?.file_revision == null) {
    error.value = t('receiptInbox.reloadBeforeConfirm')
    return
  }
  if (!categoryId.value || !treatmentId.value || !rateId.value || net.value == null || vat.value == null) { error.value = t('receiptInbox.validation'); return }
  if (registerAsset.value && assetUseType.value === 'MIXED_USE' && assetBusinessPercentage.value == null) { error.value = t('expenses.businessPercentage'); return }
  const targetId = id.value
  const version = routeVersion
  const confirmationRetry = retryTask.value && retryTask.value.id === targetId && retryTask.value.routeVersion === version
    && retryTask.value.fileRevision === item.value.file_revision && retryTask.value.state === 'running' ? retryTask.value : null
  const reviewedFileRevision = item.value?.file_revision
  const extraction = ++extractionVersion
  if (confirmationRetry) confirmationRetry.confirmation = extraction
  action.value = 'confirm'; error.value = ''; success.value = ''
  const body: ExpenseInput = toExpenseInput({ expense_date: expenseDate.value, category_id: categoryId.value, supplier_name: supplier.value || null, vat_treatment_id: treatmentId.value, vat_rate_id: rateId.value, net_amount: net.value, vat_amount: vat.value, deductible: deductible.value, reference: reference.value || null, note: note.value || null, paid_by: paidBy.value, business_percentage: businessPct.value, depreciation_years: years.value, include_in_pl: includeInPl.value, pl_cost_basis: plCostBasis.value,
    ...(registerAsset.value ? {
      fixed_asset: {
        asset_name: assetName.value,
        in_service_date: assetInServiceDate.value || null,
        useful_life_years: assetUsefulLife.value,
        residual_value: assetResidualValue.value ?? 0,
        business_percentage: assetUseType.value === 'FULL_BUSINESS' ? 100 : assetBusinessPercentage.value!,
        use_type: assetUseType.value,
      },
    } : {}),
  })
  try {
    const result = await inbox.confirm(targetId, body, reviewedFileRevision)
    if (version !== routeVersion || extraction !== extractionVersion) return
    if (retryTask.value === confirmationRetry && confirmationRetry) { retryTask.value = null; retryGeneration++ }
    item.value = result.inbox; success.value = t('receiptInbox.confirmed')
    await router.push(`/expenses/${result.expense.id}/edit?tab=purchase`)
  } catch (e) {
    if (version !== routeVersion || extraction !== extractionVersion) return
    if (e instanceof ApiError && e.status === 409) {
      const detail = e.detail as { detail?: string; expense_id?: string } | null
      if (detail?.detail === 'FILE_GROUP_CHANGED') {
        selectedFiles.value = []; uploadKey.value++
        fileGroupChangedNotice.value = true
        fileGroupUnverified.value = true
        invalidateFileSuggestion()
        try {
          const latest = await inbox.detail(targetId)
          if (version === routeVersion && targetId === id.value) {
            acceptSnapshot(latest)
            fileGroupUnverified.value = false
          }
        } catch {
          // Keep confirmation disabled until a coherent detail/retry response arrives.
        }
        return
      }
      const expenseId = detail?.expense_id
      if (expenseId) { await refresh(undefined, targetId, version); if (version === routeVersion && item.value) { item.value.expense_id = expenseId; error.value = t('receiptInbox.alreadyConfirmed') } }
      else error.value = t('receiptInbox.conflict')
    } else {
      error.value = errorMessage(e)
      // A transport/server failure is ambiguous even without a retry. Keep the
      // same-group verification GET for concurrent retries after validation errors.
      const ambiguousFailure = !(e instanceof ApiError) || e.status >= 500
      if (confirmationRetry || ambiguousFailure) {
        try {
          const latest = await inbox.detail(targetId)
          if (version === routeVersion && targetId === id.value) {
            if (latest.status === 'CONFIRMED') {
              ++extractionVersion
              retryTask.value = null; retryGeneration++
              selectedFiles.value = []; uploadKey.value++
              fileGroupUnverified.value = false; suggestionInvalidated.value = true
              item.value = latest; action.value = ''
              if (ambiguousFailure) { error.value = ''; success.value = t('receiptInbox.confirmed') }
              else error.value = t('receiptInbox.alreadyConfirmed')
            } else if (latest.status === 'PENDING') {
              if (confirmationRetry && retryTask.value === confirmationRetry
                && latest.file_revision === confirmationRetry.fileRevision && !fileGroupUnverified.value) {
                if (confirmationRetry.state === 'staged' && confirmationRetry.result) {
                  rememberReviewedSnapshot(confirmationRetry, latest.updated_at)
                  acceptSnapshot(retryResultIsFresh(confirmationRetry, confirmationRetry.result) ? confirmationRetry.result : latest, true)
                  retryTask.value = null
                } else if (confirmationRetry.state === 'failed') {
                  retryTask.value = null
                  acceptSnapshot(latest, true)
                } else {
                  confirmationRetry.confirmation = undefined
                  confirmationRetry.resumeAfter = extraction
                  rememberReviewedSnapshot(confirmationRetry, latest.updated_at)
                  acceptSnapshot(latest, true)
                }
              } else acceptSnapshot(latest)
            }
          }
        } catch {
          // A failed GET must not strand a same-group retry behind this
          // confirmation. Keep its prior freshness threshold while releasing
          // this confirmation's temporary hold.
          if (confirmationRetry && version === routeVersion && targetId === id.value && !fileGroupUnverified.value
            && item.value?.file_revision === confirmationRetry.fileRevision
            && retryTask.value === confirmationRetry) {
            confirmationRetry.confirmation = undefined
            if (confirmationRetry.state === 'staged' && confirmationRetry.result) {
              const result = confirmationRetry.result
              if (retryResultIsFresh(confirmationRetry, result)) {
                retryTask.value = null
                acceptSnapshot(result, true)
              }
            }
          }
        }
      }
    }
  } finally { if (version === routeVersion && extraction === extractionVersion) action.value = '' }
}
function fileUrl(fileId: string) { return `/api/v1/attachments/${fileId}/content` }
function formatBytes(n: number) { return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB` }
</script>

<template>
  <main class="inbox-page">
    <header><h1>{{ t('receiptInbox.title') }}</h1><n-button v-if="isDetail" @click="router.push('/receipt-inbox')">{{ t('receiptInbox.back') }}</n-button></header>
    <n-alert v-if="error" type="error" closable role="alert">{{ error }} <router-link v-if="error && item?.expense_id" :to="`/expenses/${item.expense_id}/edit?tab=purchase`">{{ t('receiptInbox.openExpense') }}</router-link></n-alert>
    <n-alert v-if="fileGroupUnverified" type="warning" role="status">{{ t('receiptInbox.fileGroupUnverified') }}</n-alert>
    <n-alert v-if="fileGroupChangedNotice" type="warning" role="status">{{ t('receiptInbox.fileGroupChanged') }}</n-alert>
    <n-alert v-if="success" type="success" closable>{{ success }}</n-alert>
    <n-spin :show="busy">
      <template v-if="!isDetail">
        <n-space class="tabs"><n-button :type="status === 'PENDING' ? 'primary' : 'default'" @click="changeStatus('PENDING')">{{ t('receiptInbox.pending') }}</n-button><n-button :type="status === 'CONFIRMED' ? 'primary' : 'default'" @click="changeStatus('CONFIRMED')">{{ t('receiptInbox.processed') }}</n-button></n-space>
        <n-alert v-if="status === 'PENDING'" type="info" class="notice">{{ t('receiptInbox.providerNotice') }}</n-alert>
        <template v-if="status === 'PENDING'">
          <n-upload :key="uploadKey" multiple :max="5" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFiles"><n-button>{{ t('receiptInbox.chooseFiles') }}</n-button></n-upload>
          <p v-if="selectedFiles.length">{{ selectedFiles.map(f => f.name).join(', ') }}</p>
          <n-button type="primary" :disabled="!selectedFiles.length || action==='upload'" @click="upload">{{ action==='upload' ? t('receiptInbox.uploading') : t('receiptInbox.upload') }}</n-button>
        </template>
        <n-empty v-if="!inbox.items.length && !busy" :description="t('receiptInbox.empty')" />
        <div class="inbox-list">
          <router-link v-for="row in inbox.items" :key="row.id" class="inbox-card" :to="`/receipt-inbox/${row.id}`">
            <n-card hoverable class="inbox-item">
              <div class="inbox-item-heading">
                <strong :title="row.files[0]?.filename || t('receiptInbox.unnamed')">{{ row.files[0]?.filename || t('receiptInbox.unnamed') }}</strong>
                <time :datetime="row.created_at">{{ new Date(row.created_at).toLocaleString() }}</time>
              </div>
              <div class="inbox-item-footer">
                <n-tag :type="row.ai_state === 'READY' ? 'success' : row.ai_state === 'FAILED' ? 'error' : 'default'">{{ t(`receiptInbox.ai.${row.ai_state}`) }}</n-tag>
                <span class="file-count">{{ row.files.length }} {{ t(row.files.length === 1 ? 'receiptInbox.file' : 'receiptInbox.files') }}</span>
              </div>
            </n-card>
          </router-link>
        </div>
        <n-space v-if="inbox.total > 100" class="pagination"><n-button :disabled="offset === 0 || busy" @click="changePage(Math.max(0, offset-100))">{{ t('receiptInbox.previousPage') }}</n-button><span>{{ t('receiptInbox.pageRange', { start: pageStart, end: pageEnd, total: inbox.total }) }}</span><n-button :disabled="offset+100 >= inbox.total || busy" @click="changePage(offset+100)">{{ t('receiptInbox.nextPage') }}</n-button></n-space>
      </template>
      <template v-else-if="item">
        <h2>{{ item.files[0]?.filename || t('receiptInbox.unnamed') }}</h2>
        <n-tag>{{ item.status === 'PENDING' ? t('receiptInbox.pending') : t('receiptInbox.processed') }}</n-tag>
        <p>{{ t(`receiptInbox.ai.${retryPending ? 'RUNNING' : item.ai_state}`) }}<span v-if="item.ai_error_code"> · {{ t(`receiptInbox.errors.${item.ai_error_code}`) }}</span></p>
        <n-alert v-if="pending" type="info" class="notice">{{ t('receiptInbox.providerNotice') }}</n-alert>
        <n-alert v-if="pending && aiMissing" type="warning">{{ t('receiptInbox.missingFields') }}</n-alert>
        <n-space v-if="pending" class="actions"><n-button :disabled="retryPending || action==='confirm' || fileActionPending" @click="retry">{{ retryPending ? t('receiptInbox.retrying') : t('receiptInbox.retry') }}</n-button><n-button type="error" :disabled="action==='discard' || action==='confirm' || fileActionPending || fileGroupUnverified" @click="discard">{{ t('receiptInbox.discard') }}</n-button></n-space>
        <section class="files"><n-card v-for="file in item.files" :key="file.id" :title="file.filename || t('receiptInbox.unnamed')" size="small">
          <p class="file-meta">{{ file.mime_type }} · {{ formatBytes(file.byte_size) }}</p><img v-if="file.mime_type.startsWith('image/')" :src="fileUrl(file.id)" :alt="file.filename || t('receiptInbox.receiptPreview')" /><iframe v-else-if="file.mime_type==='application/pdf'" :src="fileUrl(file.id)" :title="file.filename || t('receiptInbox.receiptPreview')" />
          <div class="file-actions">
            <n-button tag="a" type="primary" :href="fileUrl(file.id)" :download="file.filename || 'receipt'">{{ t('receipt.download') }}</n-button>
            <n-button v-if="pending" type="error" :disabled="action===`remove:${file.id}` || action==='confirm' || fileActionPending || fileGroupUnverified" @click="removeFile(file.id)">{{ action===`remove:${file.id}` ? t('receiptInbox.removing') : t('receipt.delete') }}</n-button>
          </div>
        </n-card></section>
        <div v-if="pending" class="append"><n-upload :key="uploadKey" multiple :max="Math.max(0, 5-item.files.length)" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFiles"><n-button :disabled="item.files.length>=5 || action==='confirm' || fileActionPending || fileGroupUnverified">{{ t('receiptInbox.addFiles') }}</n-button></n-upload><n-button type="primary" :disabled="!selectedFiles.length || action==='upload' || action==='confirm' || fileActionPending || fileGroupUnverified" @click="upload">{{ action==='upload' ? t('receiptInbox.uploading') : t('receiptInbox.append') }}</n-button></div>
        <n-card v-if="pending" :title="t('receiptInbox.reviewTitle')" class="review">
          <n-form label-placement="top" @submit.prevent="confirmExpense">
            <n-form-item :label="t('expenses.expenseDate')"><input v-model="expenseDate" type="date" @input="markEdited('expenseDate')" /></n-form-item>
            <n-form-item :label="t('expenses.category')"><n-select v-model:value="categoryId" :options="dict.categoryOptions.value" filterable @update:value="(v) => { markEdited('categoryId'); onCategoryChange(v) }" /></n-form-item>
            <n-form-item :label="t('expenses.supplier')"><n-input v-model:value="supplier" @update:value="markEdited('supplier')" /></n-form-item>
            <n-form-item :label="t('expenses.vatTreatment')">
              <div class="vat-field">
                <n-select :value="treatmentId" :options="dict.purchaseTreatmentOptions.value" clearable @update:value="onTreatmentChange" />
                <div v-if="treatmentSuggestion" class="treatment-suggestion">
                  <span>{{ t('expenses.ai.treatmentOrigin') }} · {{ treatmentSuggestionRecord?.label ?? treatmentSuggestion.code }}: {{ treatmentSuggestion.reason }}</span>
                  <n-button v-if="canApplyTreatmentSuggestion" size="tiny" attr-type="button" @click="applyTreatmentSuggestion">{{ t('expenses.ai.applyTreatment') }}</n-button>
                </div>
                <p v-if="treatmentSuggestion && !treatmentSuggestionRecord" class="treatment-hint">{{ t('receiptInbox.treatmentUnavailable') }}</p>
                <p v-if="treatmentId" class="treatment-hint">{{ t(treatmentOrigin === 'human' ? 'expenses.ai.humanTreatmentOrigin' : 'expenses.ai.treatmentOrigin') }}</p>
                <p v-if="treatmentSuggestion?.code === 'EU_B2B_REVERSE_PURCH' && !treatmentSuggestionRecord" class="treatment-hint">{{ t('expenses.ai.nominalRateReview') }}</p>
              </div>
            </n-form-item>
            <n-form-item :label="t('expenses.vatRate')">
              <div class="vat-field">
                <n-select :value="rateId" :options="dict.vatRateOptions.value" clearable @update:value="(v) => { markEdited('rateId'); rateOrigin='human'; rateId=v; preview() }" />
                <p v-if="isReverseCharge" class="treatment-hint">{{ t('expenses.ai.nominalRateReview') }}</p>
              </div>
            </n-form-item>
            <n-form-item :label="t('expenses.netAmount')"><n-input-number v-model:value="net" :min="0" :precision="2" @update:value="(v) => { markEdited('net'); net=v; preview() }" /></n-form-item>
            <n-form-item :label="t('expenses.vatAmount')"><n-input-number v-model:value="vat" :min="0" :precision="2" @update:value="(v) => { markEdited('vat'); vat=v; onVatChange() }" /></n-form-item>
            <n-form-item :label="t('expenses.grossAmount')"><strong>{{ previewGross ?? t('expenses.grossHint') }}</strong></n-form-item>
            <n-form-item :label="t('expenses.deductible')"><n-checkbox :checked="deductible ?? true" @update:checked="($event) => { markEdited('deductible'); deductible=$event }">{{ t('expenses.deductibleLabel') }}</n-checkbox></n-form-item>
            <n-form-item :label="t('expenses.paidBy')"><n-select v-model:value="paidBy" :options="[{label:t('expenses.paidByBusiness'),value:'BUSINESS'},{label:t('expenses.paidByPrivate'),value:'PRIVATE'}]" /></n-form-item>
            <n-form-item :label="t('expenses.businessPercentage')"><n-input-number v-model:value="businessPct" :min="0" :max="100" :precision="1" /></n-form-item>
            <n-form-item :label="t('expenses.depreciationYears')"><n-input-number v-model:value="years" :min="1" :precision="0" /></n-form-item>
            <n-form-item :label="t('expenses.includeInPl')"><n-checkbox v-model:checked="includeInPl">{{ t('expenses.includeInPl') }}</n-checkbox></n-form-item>
            <n-form-item :label="t('expenses.plCostBasis')"><n-select v-model:value="plCostBasis" :options="[{label:t('expenses.costBasisAuto'),value:'AUTO'},{label:t('expenses.costBasisNet'),value:'NET'},{label:t('expenses.costBasisGross'),value:'GROSS'}]" /></n-form-item>
            <n-form-item :label="t('expenses.reference')"><n-input v-model:value="reference" /></n-form-item>
            <n-form-item :label="t('expenses.note')"><n-input v-model:value="note" type="textarea" @update:value="markEdited('note')" /></n-form-item>
            <n-form-item :label="t('fixedAssets.registerOption')">
              <n-checkbox :checked="registerAsset" @update:checked="(checked) => { registerAsset = checked; if (checked && !assetName) assetName = supplier || dict.categories.value.find(x => x.id === categoryId)?.name || '' }">{{ t('fixedAssets.registerOption') }}</n-checkbox>
            </n-form-item>
            <template v-if="registerAsset">
              <n-form-item :label="t('fixedAssets.assetName')"><n-input v-model:value="assetName" /></n-form-item>
              <n-form-item :label="t('fixedAssets.inServiceDate')"><input v-model="assetInServiceDate" type="date" /></n-form-item>
              <n-form-item :label="t('fixedAssets.usefulLifeYears')"><n-input-number v-model:value="assetUsefulLife" :min="1" :precision="0" /></n-form-item>
              <n-form-item :label="t('fixedAssets.residualValue')"><n-input-number v-model:value="assetResidualValue" :min="0" :precision="2" /></n-form-item>
              <ResidualEstimate v-model:value="assetResidualValue" :cost-inputs="residualEstimateCostInputs" :useful-life-years="assetUsefulLife" :suggestion-key="`${residualEvidenceEpoch}:${item?.id ?? ''}:${item?.file_revision ?? ''}:${item?.updated_at ?? ''}:${item?.ai_state ?? ''}`" :suggested-name="!suggestionInvalidated && !fileGroupUnverified && item?.ai_state === 'READY' ? item?.ai_suggestion?.suggested_product_name : null" :suggested-brand="!suggestionInvalidated && !fileGroupUnverified && item?.ai_state === 'READY' ? item?.ai_suggestion?.suggested_product_brand : null" :suggested-model="!suggestionInvalidated && !fileGroupUnverified && item?.ai_state === 'READY' ? item?.ai_suggestion?.suggested_product_model : null" />
              <n-form-item :label="t('fixedAssets.useType')"><n-select v-model:value="assetUseType" @update:value="(value) => { if (value === 'MIXED_USE') assetBusinessPercentage = null }" :options="[{label:t('fixedAssets.fullBusiness'),value:'FULL_BUSINESS'},{label:t('fixedAssets.mixedUse'),value:'MIXED_USE'}]" /></n-form-item>
              <n-form-item v-if="assetUseType === 'MIXED_USE'" :label="t('expenses.businessPercentage')"><n-input-number v-model:value="assetBusinessPercentage" :min="0" :max="100" :precision="1" /></n-form-item>
            </template>
            <n-button v-if="action==='confirm'" type="primary" loading>{{ t('receiptInbox.confirming') }}</n-button><n-button v-else type="primary" attr-type="submit" :disabled="fileActionPending || fileGroupUnverified">{{ t('receiptInbox.confirm') }}</n-button>
          </n-form>
        </n-card>
        <n-card v-else :title="t('receiptInbox.processed')"><p v-if="item.confirmed_at">{{ new Date(item.confirmed_at).toLocaleString() }}</p><router-link v-if="item.expense_id" :to="`/expenses/${item.expense_id}/edit?tab=purchase`">{{ t('receiptInbox.openExpense') }}</router-link><p v-else>{{ t('receiptInbox.expenseUnavailable') }}</p></n-card>
      </template>
    </n-spin>
  </main>
</template>

<style scoped>
.inbox-page{max-width:1100px;margin:auto;padding:24px}
.inbox-page header,.tabs,.actions,.append{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.inbox-page header{justify-content:space-between}
.notice{margin:16px 0}
.inbox-list,.files{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin:18px 0}
.inbox-card{display:block;min-width:0;color:inherit;text-decoration:none}
.inbox-item{height:100%}
.inbox-item :deep(.n-card__content){display:flex;min-height:112px;flex-direction:column;gap:18px}
.inbox-item-heading{display:flex;min-width:0;flex-direction:column;gap:8px}
.inbox-item-heading strong{overflow-wrap:anywhere;line-height:1.4}
.inbox-item-heading time{color:var(--n-text-color-3);font-size:13px}
.inbox-item-footer{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:auto;padding-top:14px;border-top:1px solid var(--n-border-color)}
.file-count{color:var(--n-text-color-3);white-space:nowrap}
.file-meta{margin:0 0 12px;color:var(--n-text-color-3)}
.files img{display:block;max-width:100%;max-height:480px;object-fit:contain}
.files iframe{display:block;width:100%;height:380px;border:0}
.file-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px}
.review{max-width:760px;margin-top:24px}
.review :deep(.n-input-number){width:100%}
.review input[type=date]{width:100%;padding:8px;border:1px solid var(--n-border-color);border-radius:4px}
.vat-field{display:flex;flex-direction:column;gap:6px;width:100%;min-width:0}
.treatment-suggestion{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 8px;overflow-wrap:anywhere}
.treatment-hint{margin:0;font-size:13px;line-height:1.45}
@media(max-width:640px){.inbox-page{padding:14px}.files{grid-template-columns:1fr}}
</style>
