<script setup lang="ts">
/**
 * Expense Editor — create or edit a single expense.
 *
 * Key design rules:
 * - Front-end NEVER locally computes gross/base_* (red-line 1). The form
 *   only collects raw inputs; computed fields come from the backend response.
 * - ExpenseInput sent to backend: expense_date, category_id, supplier_name,
 *   vat_treatment_id, vat_rate_id, net_amount, vat_amount, deductible,
 *   reference, note. NO gross/base_x/is_draft sent.
 * - AI prefill: upload first, ai-extract, populate form fields, user
 *   confirms, POST /expenses. If AI is off (409) or fails (502), the form
 *   stays editable; never blocks manual entry.
 * - Prod-build button safety: avoid dynamic :loading on buttons inside v-if
 *   branches. Use v-if/v-else pattern for loading states.
 */
import { ref, onBeforeUnmount, onMounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { localDateStr } from '../../utils/date'
import { useExpenseCostPreview } from '../../composables/useExpenseCostPreview'
import { resolvePurchaseTreatmentSuggestion, toExpenseInput, usePurchaseExpenseDictionaries } from '../../composables/usePurchaseExpenseDictionaries'
import {
  NForm, NFormItem, NInput, NInputNumber, NSelect, NCheckbox,
  NButton, NSpace, NAlert, NSpin, NText, NTag,
  NUpload, NIcon, NCard, NPopconfirm, NRadioGroup, NRadioButton,
} from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import {
  ArrowBackOutline, CloudUploadOutline, TrashOutline,
  DownloadOutline, SparklesOutline,
} from '@vicons/ionicons5'
import { useExpensesStore } from '../../stores/expenses'
import type { ExpenseRead, ExpenseAttachmentRead } from '../../stores/expenses'
import { get, post, ApiError } from '../../api/http'
import type { components } from '../../api/schema'

type PaidBy = components['schemas']['PaidBy']
type PlCostBasis = components['schemas']['PlCostBasis']
type ExpenseCalculateResult = components['schemas']['ExpenseCalculateResult']
type FixedAssetPreviewResult = components['schemas']['FixedAssetPreviewResult']
type ExpenseCostPreviewResult = components['schemas']['ExpenseCostPreviewResult']

const route = useRoute()
const router = useRouter()
const { t, locale } = useI18n()
const store = useExpensesStore()

const expenseId = computed(() => route.params.id as string | undefined)
const isEdit = computed(() => !!expenseId.value)
const assetName = ref('')
const assetInServiceDate = ref('')
const assetUsefulLife = ref(5)
const assetResidualValue = ref<number>(0)
const assetUseType = ref<'FULL_BUSINESS' | 'MIXED_USE'>('FULL_BUSINESS')
const assetBusinessPercentage = ref(100)
const assetReportBusinessPercentage = computed(() =>
  assetUseType.value === 'FULL_BUSINESS' ? 100 : assetBusinessPercentage.value,
)
const assetPreview = ref<FixedAssetPreviewResult | null>(null)
const assetPreviewLoading = ref(false)
const assetPreviewError = ref<string | null>(null)
const assetConversionLoading = ref(false)
const linkedSourceCost = ref<ExpenseCostPreviewResult | null>(null)
const linkedSourceCostChecked = ref(false)
const savedExpenseDate = ref<string | null>(null)
let linkedSourceCostVersion = 0

// ---- Form state (raw inputs only) ----
const expenseDate = ref(localDateStr(new Date()))
const categoryId = ref<string | null>(null)
const supplierName = ref('')
const vatTreatmentId = ref<string | null>(null)
const vatRateId = ref<string | null>(null)
const netAmount = ref<number | null>(null)
const vatAmount = ref<number | null>(null)
// Keep the receipt/saved VAT amount separate from ordinary-rate preview output.
const receiptVatBaseline = ref<number | null>(null)
const ordinaryVatPreview = ref<{
  netAmount: number
  vatRateId: string
  netAttachmentId: string | null
} | null>(null)
const vatManuallyEdited = ref(false)
const netAmountNeedsReview = ref(false)
const vatAmountNeedsReview = ref(false)
const receiptVatSource = ref<'none' | 'saved' | 'ai' | 'human'>('none')
const receiptVatAttachmentId = ref<string | null>(null)
const aiNetAmountAttachmentId = ref<string | null>(null)
const currentExtractionVatMissing = ref(false)
const deductible = ref<boolean | null>(null)
const reference = ref('')
const note = ref('')
// ---- Bookkeeping fields (M8.5) — raw inputs, no local computation ----
const paidBy = ref<PaidBy>('BUSINESS')
const businessPercentage = ref<number>(100)
const depreciationYears = ref<number>(1)
const includeInPl = ref(true)
const plCostBasis = ref<PlCostBasis>('AUTO')
const {
  costPreview, originalCostPreview, costPreviewLoading, costPreviewError,
  previewPlCost, resetCostPreview,
} = useExpenseCostPreview(
  () => vatTreatmentId.value ? {
    net_amount: netAmount.value ?? 0,
    vat_amount: vatAmount.value ?? 0,
    vat_treatment_id: vatTreatmentId.value,
    category_id: categoryId.value,
    deductible: deductible.value,
    pl_cost_basis: plCostBasis.value,
    include_in_pl: includeInPl.value,
    business_percentage: businessPercentage.value,
    depreciation_years: depreciationYears.value,
  } : null,
  () => expenseId.value ? `/api/v1/expenses/${expenseId.value}/cost-preview` : null,
)

// ---- Backend-computed display fields (red-line 1: read from response only) ----
const grossAmountDisplay = ref<string | null>(null)
const isDraft = ref(false)
const recurringExpenseId = ref<string | null>(null)

// ---- Dictionary data ----
const { categories, treatments: purchaseTreatments, rates: vatRates, categoryOptions, purchaseTreatmentOptions, vatRateOptions, load: loadDictionaries } = usePurchaseExpenseDictionaries()
const treatmentOrigin = ref<'none' | 'ai' | 'human' | 'saved'>('none')
const rateOrigin = ref<'none' | 'ai' | 'human' | 'saved'>('none')
const treatmentSuggestion = ref<components['schemas']['ExpenseVatTreatmentSuggestion'] | null>(null)
let aiRequestVersion = 0
let vatPreviewVersion = 0
let pageVersion = 0
let dictionariesPromise: Promise<void> | null = null
let assetPreviewVersion = 0
let assetConversionVersion = 0

function invalidateAssetPreview() {
  assetPreviewVersion += 1
  assetPreview.value = null
  assetPreviewError.value = null
  assetPreviewLoading.value = false
}

async function loadLinkedSourceCost(targetId = expenseId.value, version = pageVersion) {
  const requestVersion = ++linkedSourceCostVersion
  linkedSourceCost.value = null
  linkedSourceCostChecked.value = false
  if (!targetId) {
    linkedSourceCostChecked.value = true
    return
  }
  try {
    const result = await get<ExpenseCostPreviewResult>(`/api/v1/expenses/${targetId}/cost-preview`)
    if (requestVersion === linkedSourceCostVersion && version === pageVersion && targetId === expenseId.value) {
      linkedSourceCost.value = result
    }
  } catch {
    // A failed preview must not prevent editing a normal purchase.
  } finally {
    if (requestVersion === linkedSourceCostVersion && version === pageVersion) linkedSourceCostChecked.value = true
  }
}

watch(
  [assetName, assetInServiceDate, assetUsefulLife, assetResidualValue, assetUseType, assetBusinessPercentage,
    expenseDate, netAmount, vatAmount, deductible, businessPercentage, depreciationYears, includeInPl, plCostBasis],
  invalidateAssetPreview,
)

// ---- Page state ----
const loading = ref(false)
const saving = ref(false)
const pageError = ref<string | null>(null)
const saveSuccess = ref(false)

// ---- Attachment state ----
const attachments = ref<ExpenseAttachmentRead[]>([])
const attachLoading = ref(false)
const attachUploading = ref(false)
const attachError = ref<string | null>(null)
const deletingAttachId = ref<string | null>(null)
const uncertainAttachmentId = ref<string | null>(null)
const recheckingAttachments = ref(false)

// ---- AI state ----
const aiExtracting = ref(false)
const aiError = ref<string | null>(null)
const aiNote = ref<string | null>(null)
const lastAiAttachmentId = ref<string | null>(null)
const lastAiExtractedAttachmentId = ref<string | null>(null)
const lastAiSuccessfulAttachmentId = ref<string | null>(null)

// ---- Options ----
// ---- Load data ----
function fillFromExpense(expense: ExpenseRead, preserveExtractionVatMissing = false) {
  const preserveManualVat = receiptVatSource.value === 'human' || vatManuallyEdited.value
  invalidateAssetPreview()
  resetCostPreview()
  expenseDate.value = expense.expense_date
  categoryId.value = expense.category_id ?? null
  supplierName.value = expense.supplier_name ?? ''
  assetName.value = expense.supplier_name || expense.category_name || ''
  assetBusinessPercentage.value = Number(expense.business_percentage)
  assetUseType.value = Number(expense.business_percentage) === 100 ? 'FULL_BUSINESS' : 'MIXED_USE'
  vatTreatmentId.value = expense.vat_treatment_id ?? null
  vatRateId.value = expense.vat_rate_id ?? null
  treatmentOrigin.value = 'saved'
  rateOrigin.value = 'saved'
  netAmount.value = Number(expense.net_amount)
  vatAmount.value = Number(expense.vat_amount)
  receiptVatBaseline.value = vatAmount.value
  ordinaryVatPreview.value = null
  vatManuallyEdited.value = preserveManualVat
  receiptVatSource.value = preserveManualVat ? 'human' : 'saved'
  receiptVatAttachmentId.value = null
  aiNetAmountAttachmentId.value = null
  netAmountNeedsReview.value = false
  vatAmountNeedsReview.value = false
  currentExtractionVatMissing.value = preserveExtractionVatMissing && currentExtractionVatMissing.value
  deductible.value = expense.deductible
  reference.value = expense.reference ?? ''
  note.value = expense.note ?? ''
  // Bookkeeping fields (M8.5) — programmatic assignment, does NOT trigger @update:value
  paidBy.value = expense.paid_by
  businessPercentage.value = Number(expense.business_percentage)
  depreciationYears.value = expense.depreciation_years
  includeInPl.value = expense.include_in_pl
  plCostBasis.value = expense.pl_cost_basis
  // Backend-computed display
  grossAmountDisplay.value = expense.gross_amount
  isDraft.value = expense.is_draft
  recurringExpenseId.value = expense.recurring_expense_id ?? null
}

async function previewFixedAsset() {
  if (!expenseId.value) return
  const requestVersion = ++assetPreviewVersion
  assetPreviewLoading.value = true
  assetPreviewError.value = null
  try {
    const result = await post<FixedAssetPreviewResult>(
      `/api/v1/fixed-assets/expenses/${expenseId.value}/conversion-preview`,
      {
        asset_name: assetName.value,
        in_service_date: assetInServiceDate.value || null,
        useful_life_years: assetUsefulLife.value,
        residual_value: assetResidualValue.value,
        business_percentage: assetReportBusinessPercentage.value,
        use_type: assetUseType.value,
      },
    )
    if (requestVersion === assetPreviewVersion) assetPreview.value = result
  } catch (error: unknown) {
    if (requestVersion === assetPreviewVersion) {
      assetPreviewError.value = error instanceof ApiError ? error.message : String(error)
      assetPreview.value = null
    }
  } finally {
    if (requestVersion === assetPreviewVersion) assetPreviewLoading.value = false
  }
}

async function confirmFixedAssetConversion() {
  const targetId = expenseId.value
  if (!targetId || assetConversionLoading.value) return
  const version = pageVersion
  const requestVersion = ++assetConversionVersion
  assetConversionLoading.value = true
  try {
    const converted = await post<components['schemas']['FixedAssetRead']>('/api/v1/fixed-assets/expenses/' + targetId + '/convert', {
      asset_name: assetName.value,
      in_service_date: assetInServiceDate.value || null,
      useful_life_years: assetUsefulLife.value,
      residual_value: assetResidualValue.value,
      business_percentage: assetReportBusinessPercentage.value,
      use_type: assetUseType.value,
      preview_token: assetPreview.value?.preview_token,
    })
    if (version === pageVersion && targetId === expenseId.value && requestVersion === assetConversionVersion) {
      await router.push({
        path: '/fixed-assets',
        query: { asset: converted.id, year: converted.acquisition_date.slice(0, 4) },
      })
    }
  } catch (error: unknown) {
    if (version === pageVersion && targetId === expenseId.value && requestVersion === assetConversionVersion) {
      assetPreviewError.value = error instanceof ApiError ? error.message : String(error)
    }
  } finally {
    if (version === pageVersion && targetId === expenseId.value && requestVersion === assetConversionVersion) {
      assetConversionLoading.value = false
    }
  }
}

type VatEvidenceTransition = {
  /** Drop an AI receipt baseline when its source is superseded or removed. */
  invalidateAiReceipt?: boolean
  /** Invalidate a preview only when it depends on this attachment. */
  removedAttachmentId?: string
  /** Attachment-backed previews are stale when extraction moves to a new source. */
  switchAttachmentSource?: boolean
  /** Reverse charge never uses the ordinary overlay, including over a human clear. */
  enteringReverseCharge?: boolean
  /** A completed extraction with no receipt VAT invalidates a saved non-human baseline. */
  missingReverseReceipt?: boolean
  /** Preview inputs expected after an automated form update. */
  previewInputs?: { netAmount: number | null; vatRateId: string | null; netAttachmentId: string | null }
}

function transitionVatEvidence(change: VatEvidenceTransition) {
  const hadVisibleAmount = vatAmount.value !== null
  const invalidateReceipt = (change.invalidateAiReceipt === true && receiptVatSource.value === 'ai')
    || (change.removedAttachmentId !== undefined && receiptVatSource.value === 'ai'
      && receiptVatAttachmentId.value === change.removedAttachmentId)
    || (change.missingReverseReceipt === true && receiptVatSource.value !== 'human'
      && !vatManuallyEdited.value && currentExtractionVatMissing.value)
  const previewSource = ordinaryVatPreview.value?.netAttachmentId ?? null
  const invalidatePreview = ordinaryVatPreview.value !== null && (
    change.enteringReverseCharge === true
    || (change.removedAttachmentId !== undefined && previewSource === change.removedAttachmentId)
    || (change.switchAttachmentSource === true && previewSource !== null)
    || (change.previewInputs !== undefined && (
      ordinaryVatPreview.value.netAmount !== change.previewInputs.netAmount
      || ordinaryVatPreview.value.vatRateId !== change.previewInputs.vatRateId
      || ordinaryVatPreview.value.netAttachmentId !== change.previewInputs.netAttachmentId
    ))
  )

  if (invalidateReceipt) {
    receiptVatBaseline.value = null
    receiptVatSource.value = 'none'
    receiptVatAttachmentId.value = null
  }
  if (invalidatePreview) ordinaryVatPreview.value = null

  // The visible VAT is an overlay on the receipt baseline. When either source
  // is withdrawn, restore the surviving baseline; if both sources disappeared,
  // require review instead of leaving a stale calculated number in the form.
  if ((invalidateReceipt || invalidatePreview) && ordinaryVatPreview.value === null) {
    if (receiptVatSource.value !== 'none' && receiptVatBaseline.value !== null) {
      vatAmount.value = receiptVatBaseline.value
    } else {
      vatAmount.value = null
      if (hadVisibleAmount) vatAmountNeedsReview.value = true
    }
  }
}

// 仅在用户主动切换分类时触发：把可抵扣默认设为该分类的 default_deductible（为 null 则回落 true）。
// 编程式赋值（fillFromExpense 里的 categoryId.value = ...）不会触发 @update:value，
// 因此初始载入不会覆盖已保存的 deductible。
function onCategoryChange(newCatId: string | null) {
  const cat = categories.value.find(c => c.id === newCatId)
  deductible.value = cat && cat.default_deductible !== null ? cat.default_deductible : true
}

async function loadAttachments(targetId = expenseId.value, version = pageVersion) {
  if (!targetId) return
  attachLoading.value = true
  try {
    const result = await store.listAttachments(targetId)
    if (version === pageVersion && targetId === expenseId.value) attachments.value = result
  } catch {
    // silently ignore
  } finally {
    if (version === pageVersion) attachLoading.value = false
  }
}

onMounted(async () => {
  const version = ++pageVersion
  const targetId = expenseId.value
  loading.value = true
  try {
    dictionariesPromise ??= loadDictionaries()
    await dictionariesPromise
    if (version !== pageVersion || targetId !== expenseId.value) return
    if (targetId) {
      const expense = await store.getExpense(targetId)
      if (version !== pageVersion || targetId !== expenseId.value) return
      fillFromExpense(expense)
      savedExpenseDate.value = expense.expense_date
      void loadLinkedSourceCost(targetId, version)
      await loadAttachments(targetId, version)
    }
  } catch (e: unknown) {
    if (version === pageVersion) pageError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    if (version === pageVersion) loading.value = false
  }
})
watch(expenseId, (targetId, previousId) => {
  if (targetId === previousId) return
  invalidateAssetPreview()
  ++assetConversionVersion
  assetConversionLoading.value = false
  linkedSourceCost.value = null; linkedSourceCostChecked.value = false; ++linkedSourceCostVersion
  savedExpenseDate.value = null
  const version = ++pageVersion
  ++aiRequestVersion; clearTreatmentSuggestion(); invalidateVatPreview()
  pageError.value = null; saveSuccess.value = false; saving.value = false; attachLoading.value = false; attachUploading.value = false; attachments.value = []; lastAiAttachmentId.value = null; lastAiExtractedAttachmentId.value = null; lastAiSuccessfulAttachmentId.value = null
  expenseDate.value = localDateStr(new Date()); categoryId.value = null; supplierName.value = ''; vatTreatmentId.value = null; vatRateId.value = null
  assetName.value = ''; assetInServiceDate.value = ''; assetUsefulLife.value = 5; assetResidualValue.value = 0
  assetBusinessPercentage.value = 100; assetUseType.value = 'FULL_BUSINESS'
  netAmount.value = null; vatAmount.value = null; deductible.value = null; note.value = ''; reference.value = ''; grossAmountDisplay.value = null
  receiptVatBaseline.value = null; ordinaryVatPreview.value = null
  vatManuallyEdited.value = false
  receiptVatSource.value = 'none'; currentExtractionVatMissing.value = false
  receiptVatAttachmentId.value = null; aiNetAmountAttachmentId.value = null
  netAmountNeedsReview.value = false; vatAmountNeedsReview.value = false
  uncertainAttachmentId.value = null; recheckingAttachments.value = false
  paidBy.value = 'BUSINESS'; businessPercentage.value = 100; depreciationYears.value = 1; includeInPl.value = true; plCostBasis.value = 'AUTO'
  aiError.value = null; aiNote.value = null; attachError.value = null; deletingAttachId.value = null; isDraft.value = false; recurringExpenseId.value = null
  treatmentOrigin.value = 'none'; rateOrigin.value = 'none'
  if (!targetId) {
    loading.value = false
    return
  }
  loading.value = true
  void (async () => {
    try {
      dictionariesPromise ??= loadDictionaries()
      await dictionariesPromise
      const expense = await store.getExpense(targetId)
      if (version !== pageVersion || targetId !== expenseId.value) return
      fillFromExpense(expense)
      savedExpenseDate.value = expense.expense_date
      void loadLinkedSourceCost(targetId, version)
      await loadAttachments(targetId, version)
    } catch (e: unknown) {
      if (version === pageVersion) pageError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      if (version === pageVersion) loading.value = false
    }
  })()
})

onBeforeUnmount(() => {
  ++pageVersion
  ++assetPreviewVersion
  ++assetConversionVersion
})

// ---- Save ----
async function handleSave() {
  if (deletingAttachmentHasAiValues.value) return
  if (isReverseCharge.value && vatAmount.value === null) {
    pageError.value = t('expenses.ai.reverseVatRequired')
    return
  }
  if (netAmountNeedsReview.value && netAmount.value === null) {
    pageError.value = t('expenses.ai.withdrawnNetRequired')
    return
  }
  if (vatAmountNeedsReview.value && vatAmount.value === null) {
    pageError.value = t('expenses.ai.withdrawnVatRequired')
    return
  }
  if (!categoryId.value || !vatTreatmentId.value || !vatRateId.value) {
    pageError.value = t('expenses.validationError')
    return
  }
  resetCostPreview()
  const version = pageVersion
  const targetId = expenseId.value
  ++aiRequestVersion
  aiExtracting.value = false
  invalidateVatPreview()
  saving.value = true
  saveSuccess.value = false
  pageError.value = null
  try {
    // Only send raw inputs — no gross/base_*/is_draft (red-line 1)
    // Bookkeeping fields (M8.5): passed as-is, no local computation
    const body = toExpenseInput({
      expense_date: expenseDate.value,
      category_id: categoryId.value,
      supplier_name: supplierName.value || null,
      vat_treatment_id: vatTreatmentId.value,
      vat_rate_id: vatRateId.value,
      net_amount: netAmount.value ?? 0,
      vat_amount: vatAmount.value ?? 0,
      deductible: deductible.value,
      reference: reference.value || null,
      note: note.value || null,
      paid_by: paidBy.value,
      business_percentage: businessPercentage.value,
      depreciation_years: depreciationYears.value,
      include_in_pl: includeInPl.value,
      pl_cost_basis: plCostBasis.value,
    })
    let result: ExpenseRead
    if (targetId) {
      result = await store.updateExpense(targetId, body)
    } else {
      result = await store.createExpense(body)
    }
    // Update display from backend response (red-line 1: gross comes from backend)
    if (version !== pageVersion || targetId !== expenseId.value) return
    fillFromExpense(result, lastAiAttachmentId.value !== null
      && lastAiAttachmentId.value === lastAiSuccessfulAttachmentId.value)
    saveSuccess.value = true
    if (!isEdit.value) {
      // Navigate to edit mode after create
      void router.replace({ path: `/expenses/${result.id}/edit`, query: { tab: 'purchase' } })
    }
  } catch (e: unknown) {
    if (version === pageVersion) pageError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    if (version === pageVersion) saving.value = false
  }
}

// ---- Attachment upload ----
async function handleFileUpload(options: { file: UploadFileInfo }) {
  const rawFile = options.file.file
  if (!rawFile || !expenseId.value || attachmentListBusy.value) return false
  const targetId = expenseId.value, version = pageVersion
  attachUploading.value = true
  attachError.value = null
  try {
    const newAttach = await store.uploadAttachment(targetId, rawFile)
    if (version !== pageVersion || targetId !== expenseId.value) return false
    attachments.value.push(newAttach)
    // After upload, offer AI extraction on this attachment
    lastAiAttachmentId.value = newAttach.id
    lastAiExtractedAttachmentId.value = null
    lastAiSuccessfulAttachmentId.value = null
    transitionVatEvidence({
      invalidateAiReceipt: true,
      switchAttachmentSource: true,
      previewInputs: {
        netAmount: netAmount.value,
        vatRateId: rateOrigin.value === 'ai' ? null : vatRateId.value,
        netAttachmentId: aiNetAmountAttachmentId.value,
      },
    })
    currentExtractionVatMissing.value = false
    ++aiRequestVersion; clearTreatmentSuggestion()
  } catch (e: unknown) {
    if (version === pageVersion && targetId === expenseId.value) attachError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    if (version === pageVersion && targetId === expenseId.value) attachUploading.value = false
  }
  // Return false to prevent naive-ui default upload behaviour
  return false
}

async function handleDeleteAttachment(id: string) {
  if (attachmentListBusy.value) return
  const version = pageVersion, targetExpense = expenseId.value
  deletingAttachId.value = id
  try {
    await store.deleteAttachment(id)
    if (version !== pageVersion || targetExpense !== expenseId.value) return
    attachments.value = attachments.value.filter(a => a.id !== id)
    withdrawAttachmentValues(id)
  } catch (e: unknown) {
    if (version === pageVersion && targetExpense === expenseId.value) {
      // A transport error, server error, or 404 does not prove that deletion
      // failed: the server may have committed before the response was lost.
      try {
        const current = await store.listAttachments(targetExpense!)
        if (version !== pageVersion || targetExpense !== expenseId.value) return
        reconcileAttachments(current)
        if (current.some(attachment => attachment.id === id)) {
          uncertainAttachmentId.value = null
          attachError.value = e instanceof ApiError ? e.message : String(e)
        } else {
          uncertainAttachmentId.value = null
          attachError.value = null
        }
      } catch {
        if (version === pageVersion && targetExpense === expenseId.value) {
          uncertainAttachmentId.value = id
          attachError.value = t('expenses.ai.attachmentStatusUncertain')
        }
      }
    }
  } finally {
    if (version === pageVersion && targetExpense === expenseId.value) deletingAttachId.value = null
  }
}

function withdrawAttachmentValues(id: string) {
  uncertainAttachmentId.value = null
  attachments.value = attachments.value.filter(attachment => attachment.id !== id)
  if (lastAiAttachmentId.value === id) {
    ++aiRequestVersion
    aiExtracting.value = false
    lastAiAttachmentId.value = null
    lastAiExtractedAttachmentId.value = null
    lastAiSuccessfulAttachmentId.value = null
    currentExtractionVatMissing.value = false
    clearTreatmentSuggestion()
  }
  const previewSourceRemoved = ordinaryVatPreview.value?.netAttachmentId === id
  transitionVatEvidence({
    removedAttachmentId: id,
    previewInputs: {
      netAmount: netAmount.value,
      vatRateId: vatRateId.value,
      netAttachmentId: aiNetAmountAttachmentId.value,
    },
  })
  if (previewSourceRemoved) invalidateVatPreview()
  if (aiNetAmountAttachmentId.value === id) {
    netAmount.value = null
    aiNetAmountAttachmentId.value = null
    netAmountNeedsReview.value = true
    invalidateVatPreview()
  }
}

function reconcileAttachments(current: ExpenseAttachmentRead[]) {
  attachments.value = current
  const presentIds = new Set(current.map(attachment => attachment.id))
  const sourceIds = new Set([
    lastAiAttachmentId.value,
    ...amountSourceAttachmentIds.value,
  ])
  for (const id of sourceIds) {
    if (id && !presentIds.has(id)) withdrawAttachmentValues(id)
  }
}

async function recheckAttachments() {
  const id = uncertainAttachmentId.value
  const targetExpense = expenseId.value
  const version = pageVersion
  if (!id || !targetExpense || attachmentListOperationBusy.value) return
  recheckingAttachments.value = true
  try {
    const current = await store.listAttachments(targetExpense)
    if (version !== pageVersion || targetExpense !== expenseId.value) return
    reconcileAttachments(current)
    uncertainAttachmentId.value = null
    attachError.value = null
  } catch {
    if (version === pageVersion && targetExpense === expenseId.value) {
      attachError.value = t('expenses.ai.attachmentStatusUncertain')
    }
  } finally {
    if (version === pageVersion && targetExpense === expenseId.value) recheckingAttachments.value = false
  }
}

function attachmentContentUrl(id: string) {
  return `/api/v1/attachments/${id}/content`
}

function isImage(mime: string) {
  return mime.startsWith('image/')
}

function isPdf(mime: string) {
  return mime === 'application/pdf'
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ---- VAT auto-compute (calls backend calculate endpoint, non-blocking) ----
let vatDebounceTimer: ReturnType<typeof setTimeout> | null = null

const treatmentSuggestionRecord = computed(() => resolvePurchaseTreatmentSuggestion(treatmentSuggestion.value, purchaseTreatments.value))
const attachmentListOperationBusy = computed(() => attachUploading.value || deletingAttachId.value !== null || recheckingAttachments.value)
const attachmentListBusy = computed(() => attachmentListOperationBusy.value || uncertainAttachmentId.value !== null)
const deletingCurrentAiAttachment = computed(() => (deletingAttachId.value !== null && deletingAttachId.value === lastAiAttachmentId.value)
  || (uncertainAttachmentId.value !== null && uncertainAttachmentId.value === lastAiAttachmentId.value))
const amountSourceAttachmentIds = computed(() => new Set([
  aiNetAmountAttachmentId.value,
  receiptVatAttachmentId.value,
  ordinaryVatPreview.value?.netAttachmentId ?? null,
].filter((id): id is string => id !== null)))
const deletingAttachmentHasAiValues = computed(() => {
  const changingId = deletingAttachId.value ?? uncertainAttachmentId.value
  return deletingCurrentAiAttachment.value || (changingId !== null && amountSourceAttachmentIds.value.has(changingId))
})
const isReverseCharge = computed(() => purchaseTreatments.value.find(x => x.id === vatTreatmentId.value)?.effect === 'ZERO_REVERSE')

function clearTreatmentSuggestion(preserveOrdinaryRate = false) {
  aiExtracting.value = false
  aiError.value = null; aiNote.value = null
  treatmentSuggestion.value = null
  if (treatmentOrigin.value === 'ai') { vatTreatmentId.value = null; treatmentOrigin.value = 'none' }
  if (!preserveOrdinaryRate && rateOrigin.value === 'ai') { vatRateId.value = null; rateOrigin.value = 'none' }
  invalidateVatPreview()
}
function onTreatmentChange(value: string | null) {
  vatTreatmentId.value = value
  treatmentOrigin.value = 'human'
  enterReverseChargeIfSelected()
  invalidateVatPreview()
  if (isReverseCharge.value && rateOrigin.value === 'ai') { vatRateId.value = null; rateOrigin.value = 'none' }
}
function applyTreatmentSuggestion() {
  if (saving.value || deletingCurrentAiAttachment.value || !treatmentSuggestionRecord.value) return
  vatTreatmentId.value = treatmentSuggestionRecord.value.id
  treatmentOrigin.value = 'human'
  enterReverseChargeIfSelected()
  if (treatmentSuggestion.value?.code === 'EU_B2B_REVERSE_PURCH' && rateOrigin.value === 'ai') { vatRateId.value = null; rateOrigin.value = 'none' }
  invalidateVatPreview()
}
function onRateChange(value: string | null) {
  vatRateId.value = value; rateOrigin.value = 'human'; invalidateVatPreview(); onVatRateChange()
}
function onVatInput() {
  vatManuallyEdited.value = true
  receiptVatBaseline.value = vatAmount.value
  ordinaryVatPreview.value = null
  receiptVatSource.value = 'human'
  receiptVatAttachmentId.value = null
  invalidateVatPreview()
}
function enterReverseChargeIfSelected() {
  if (!isReverseCharge.value) return
  transitionVatEvidence({ enteringReverseCharge: true, missingReverseReceipt: true })
}
function invalidateVatPreview() {
  vatPreviewVersion++
  if (vatDebounceTimer) { clearTimeout(vatDebounceTimer); vatDebounceTimer = null }
  grossAmountDisplay.value = null
}

async function recomputeVat() {
  const version = ++vatPreviewVersion
  const targetExpense = expenseId.value
  const net = netAmount.value, rate = vatRateId.value
  const netAttachmentId = aiNetAmountAttachmentId.value
  if (isReverseCharge.value || net === null || !rate) { grossAmountDisplay.value = null; return }
  try {
    const result = await post<ExpenseCalculateResult>('/api/v1/expenses/calculate', {
      net_amount: net,
      vat_rate_id: rate,
    })
    if (version === vatPreviewVersion && targetExpense === expenseId.value && net === netAmount.value && rate === vatRateId.value && !isReverseCharge.value) {
      vatAmount.value = Number(result.vat_amount)
      ordinaryVatPreview.value = { netAmount: net, vatRateId: rate, netAttachmentId }
      grossAmountDisplay.value = result.gross_amount
    }
  } catch {
    /* 非致命：保持现状，不打断手动录入 */
  }
}

function onVatRateChange() { void recomputeVat() }            // 选税率：立即

function onNetInput() {                                        // 改净额：防抖
  aiNetAmountAttachmentId.value = null
  invalidateVatPreview()
  if (vatDebounceTimer) clearTimeout(vatDebounceTimer)
  vatDebounceTimer = setTimeout(() => { vatDebounceTimer = null; void recomputeVat() }, 300)
}

// ---- AI extraction ----
async function handleAiExtract(attachmentId: string) {
  if (saving.value || deletingAttachId.value === attachmentId) return
  const preserveOrdinaryRate = lastAiExtractedAttachmentId.value === attachmentId
  if (!preserveOrdinaryRate) {
    currentExtractionVatMissing.value = false
    lastAiSuccessfulAttachmentId.value = null
  }
  if (lastAiExtractedAttachmentId.value !== attachmentId) {
    transitionVatEvidence({
      invalidateAiReceipt: true,
      switchAttachmentSource: true,
      previewInputs: {
        netAmount: netAmount.value,
        vatRateId: rateOrigin.value === 'ai' ? null : vatRateId.value,
        netAttachmentId: aiNetAmountAttachmentId.value,
      },
    })
  }
  lastAiExtractedAttachmentId.value = attachmentId
  clearTreatmentSuggestion(preserveOrdinaryRate)
  const version = ++aiRequestVersion
  const targetExpense = expenseId.value
  aiExtracting.value = true
  aiError.value = null
  aiNote.value = null
  try {
    const prefill = await store.aiExtract(attachmentId, locale.value)
    if (version !== aiRequestVersion || targetExpense !== expenseId.value || attachmentId !== lastAiAttachmentId.value) return
    lastAiSuccessfulAttachmentId.value = attachmentId
    // A new extraction can replace receipt VAT; invalidate both active and debounced previews first.
    invalidateVatPreview()
    treatmentSuggestion.value = prefill.suggested_vat_treatment ?? null
    // Populate form fields from prefill — user confirms before saving
    if (prefill.expense_date) expenseDate.value = prefill.expense_date
    if (prefill.supplier_name) supplierName.value = prefill.supplier_name
    if (prefill.net_amount !== null && prefill.net_amount !== undefined) {
      netAmount.value = Number(prefill.net_amount)
      aiNetAmountAttachmentId.value = attachmentId
    }
    currentExtractionVatMissing.value = prefill.vat_amount === null || prefill.vat_amount === undefined
    if (!vatManuallyEdited.value) {
      if (prefill.vat_amount !== null && prefill.vat_amount !== undefined) {
        receiptVatBaseline.value = Number(prefill.vat_amount)
        vatAmount.value = Number(prefill.vat_amount)
        receiptVatSource.value = 'ai'
        receiptVatAttachmentId.value = attachmentId
        ordinaryVatPreview.value = null
      }
    }
    // Try to match VAT rate by percent
    const currentSuggestion = treatmentSuggestionRecord.value
    if (currentSuggestion && (treatmentOrigin.value === 'none' || treatmentOrigin.value === 'ai')) {
      vatTreatmentId.value = currentSuggestion.id; treatmentOrigin.value = 'ai'
    }
    const reverseSuggestion = prefill.suggested_vat_treatment?.code === 'EU_B2B_REVERSE_PURCH'
    if (reverseSuggestion || isReverseCharge.value) {
      if (rateOrigin.value === 'ai') { vatRateId.value = null; rateOrigin.value = 'none' }
    } else if (prefill.vat_rate_percent) {
      const pct = Number(prefill.vat_rate_percent)
      const matchedRate = vatRates.value.find(r => Math.abs(Number(r.percent) - pct) < 0.01)
      if (matchedRate) { vatRateId.value = matchedRate.id; rateOrigin.value = 'ai' }
    }
    // Category suggestion
    if (prefill.suggested_category_id) {
      categoryId.value = prefill.suggested_category_id
    } else if (prefill.suggested_category_name) {
      // best-effort name match
      const lower = prefill.suggested_category_name.toLowerCase()
      const match = categories.value.find(c => c.name.toLowerCase() === lower)
      if (match) categoryId.value = match.id
    }
    enterReverseChargeIfSelected()
    transitionVatEvidence({ previewInputs: {
      netAmount: netAmount.value,
      vatRateId: vatRateId.value,
      netAttachmentId: aiNetAmountAttachmentId.value,
    } })
    if (prefill.raw_model_note) aiNote.value = prefill.raw_model_note
    // Fill note with AI summary only if user hasn't typed anything yet
    if (prefill.summary && !note.value) {
      note.value = prefill.summary
    }
    // After prefill, clear grossAmountDisplay so user knows to save to get backend value
    grossAmountDisplay.value = null
  } catch (e: unknown) {
    if (version !== aiRequestVersion || targetExpense !== expenseId.value || attachmentId !== lastAiAttachmentId.value) return
    if (e instanceof ApiError) {
      if (e.status === 409) {
        aiError.value = t('expenses.ai.disabled')
      } else if (e.status === 502) {
        aiError.value = t('expenses.ai.modelFailed')
      } else {
        aiError.value = e.message
      }
    } else {
      aiError.value = String(e)
    }
  } finally {
    if (version === aiRequestVersion) aiExtracting.value = false
  }
}
</script>

<template>
  <div class="expense-edit-page">
    <div class="expense-edit-container">
          <!-- Header -->
          <div class="page-header">
            <n-button text @click="router.push({ path: '/expenses', query: { tab: 'purchase' } })">
              <template #icon><n-icon><ArrowBackOutline /></n-icon></template>
              {{ t('expenses.backToList') }}
            </n-button>
            <h2>{{ isEdit ? t('expenses.edit') : t('expenses.new') }}</h2>
            <div class="header-badges">
              <n-button v-if="isEdit && expenseId" size="small" @click="router.push(`/battery-assets/new?expense_id=${expenseId}`)">{{ t('battery.registerFromExpense') }}</n-button>
              <n-tag v-if="isDraft" type="warning" size="small">{{ t('expenses.draft') }}</n-tag>
              <n-tag v-if="recurringExpenseId" type="info" size="small">{{ t('expenses.fromRecurring') }}</n-tag>
            </div>
          </div>

          <n-spin :show="loading">
            <n-alert v-if="pageError" type="error" closable style="margin-bottom: 16px">{{ pageError }}</n-alert>
            <n-alert v-if="saveSuccess" type="success" closable style="margin-bottom: 16px">{{ t('expenses.saveSuccess') }}</n-alert>

            <div class="edit-layout">
              <!-- Left: form -->
              <div class="form-col">
                <n-form label-placement="left" label-width="140" @submit.prevent="handleSave">
                  <!-- Date -->
                  <n-form-item :label="t('expenses.expenseDate')">
                    <n-date-picker
                      v-model:formatted-value="expenseDate"
                      value-format="yyyy-MM-dd"
                      type="date"
                      style="width: 100%"
                    />
                  </n-form-item>

                  <!-- Category -->
                  <n-form-item :label="t('expenses.category')">
                    <n-select
                      v-model:value="categoryId"
                      :options="categoryOptions"
                      :placeholder="t('expenses.categoryPlaceholder')"
                      clearable
                      filterable
                      @update:value="onCategoryChange"
                    />
                  </n-form-item>

                  <!-- Supplier -->
                  <n-form-item :label="t('expenses.supplier')">
                    <n-input
                      v-model:value="supplierName"
                      :placeholder="t('expenses.supplierPlaceholder')"
                    />
                  </n-form-item>

                  <!-- VAT Treatment (PURCHASE side only) -->
                  <n-form-item :label="t('expenses.vatTreatment')">
                    <div class="vat-field">
                      <n-select
                        :value="vatTreatmentId"
                        :options="purchaseTreatmentOptions"
                        :placeholder="t('expenses.vatTreatmentPlaceholder')"
                        clearable
                        @update:value="onTreatmentChange"
                      />
                      <div v-if="treatmentSuggestion" class="treatment-suggestion">
                        <span>{{ t('expenses.ai.treatmentOrigin') }} · {{ treatmentSuggestionRecord?.label ?? treatmentSuggestion.code }}: {{ treatmentSuggestion.reason }}</span>
                        <n-button v-if="!saving && treatmentSuggestionRecord && treatmentSuggestionRecord.id !== vatTreatmentId" size="tiny" attr-type="button" :disabled="deletingCurrentAiAttachment" @click="applyTreatmentSuggestion">{{ t('expenses.ai.applyTreatment') }}</n-button>
                      </div>
                      <n-text v-if="treatmentSuggestion && !treatmentSuggestionRecord" depth="3" style="font-size: 13px">{{ t('receiptInbox.treatmentUnavailable') }}</n-text>
                      <n-text v-if="vatTreatmentId" depth="3" style="font-size: 12px">
                        {{ t(treatmentOrigin === 'saved' ? 'expenses.ai.savedTreatmentOrigin' : treatmentOrigin === 'human' ? 'expenses.ai.humanTreatmentOrigin' : 'expenses.ai.treatmentOrigin') }}
                      </n-text>
                      <n-text v-if="treatmentSuggestion?.code === 'EU_B2B_REVERSE_PURCH' || isReverseCharge" depth="3" style="font-size: 13px">
                        {{ t('expenses.ai.nominalRateReview') }}
                      </n-text>
                    </div>
                  </n-form-item>

                  <!-- VAT Rate -->
                  <n-form-item :label="t('expenses.vatRate')">
                    <n-select
                      :value="vatRateId"
                      :options="vatRateOptions"
                      :placeholder="t('expenses.vatRatePlaceholder')"
                      clearable
                      @update:value="onRateChange"
                    />
                  </n-form-item>

                  <!-- Net amount -->
                  <n-form-item :label="t('expenses.netAmount')">
                    <n-input-number
                      v-model:value="netAmount"
                      :precision="2"
                      :min="0"
                      :placeholder="t('expenses.amountPlaceholder')"
                      style="width: 100%"
                      @update:value="onNetInput"
                    />
                    <n-text v-if="netAmountNeedsReview" type="warning">{{ t('expenses.ai.withdrawnNetRequired') }}</n-text>
                  </n-form-item>

                  <!-- VAT amount -->
                  <n-form-item :label="t('expenses.vatAmount')">
                    <n-input-number
                      v-model:value="vatAmount"
                      :precision="2"
                      :min="0"
                      :placeholder="t('expenses.amountPlaceholder')"
                      style="width: 100%"
                      @update:value="onVatInput"
                    />
                    <n-text v-if="vatAmountNeedsReview && !isReverseCharge" type="warning">{{ t('expenses.ai.withdrawnVatRequired') }}</n-text>
                  </n-form-item>

                  <!-- Gross (read-only, from backend) -->
                  <n-form-item :label="t('expenses.grossAmount')">
                    <n-text v-if="grossAmountDisplay" type="success" style="font-weight: 600; font-size: 16px">
                      {{ Number(grossAmountDisplay).toFixed(2) }}
                    </n-text>
                    <n-text v-else depth="3" style="font-size: 13px">
                      {{ t('expenses.grossHint') }}
                    </n-text>
                  </n-form-item>

                  <!-- Deductible -->
                  <n-form-item :label="t('expenses.deductible')">
                    <n-checkbox :checked="deductible ?? true" @update:checked="deductible = $event">
                      {{ t('expenses.deductibleLabel') }}
                    </n-checkbox>
                  </n-form-item>

                  <!-- Paid By (M8.5) -->
                  <n-form-item :label="t('expenses.paidBy')">
                    <n-radio-group
                      :value="paidBy"
                      @update:value="(v: PaidBy) => { paidBy = v }"
                    >
                      <n-radio-button value="BUSINESS">{{ t('expenses.paidByBusiness') }}</n-radio-button>
                      <n-radio-button value="PRIVATE">{{ t('expenses.paidByPrivate') }}</n-radio-button>
                    </n-radio-group>
                  </n-form-item>

                  <!-- Business % (M8.5) -->
                  <n-form-item :label="t('expenses.businessPercentage')">
                    <n-input-number
                      :value="businessPercentage"
                      :precision="1"
                      :min="0"
                      :max="100"
                      style="width: 160px"
                      @update:value="(v: number | null) => { businessPercentage = v ?? 100 }"
                    >
                      <template #suffix>%</template>
                    </n-input-number>
                  </n-form-item>

                  <!-- Depreciation years (M8.5) -->
                  <n-form-item :label="t('expenses.depreciationYears')">
                    <n-input-number
                      :value="depreciationYears"
                      :precision="0"
                      :min="1"
                      style="width: 160px"
                      @update:value="(v: number | null) => { depreciationYears = v ?? 1 }"
                    />
                  </n-form-item>

                  <n-form-item :label="t('expenses.includeInPl')">
                    <n-checkbox v-model:checked="includeInPl">{{ t('expenses.includeInPl') }}</n-checkbox>
                  </n-form-item>
                  <n-form-item :label="t('expenses.plCostBasis')">
                    <n-select v-model:value="plCostBasis" :options="[
                      { label: t('expenses.costBasisAuto'), value: 'AUTO' },
                      { label: t('expenses.costBasisNet'), value: 'NET' },
                      { label: t('expenses.costBasisGross'), value: 'GROSS' },
                    ]" />
                  </n-form-item>
                  <n-form-item :label="t('expenses.costPreview')">
                    <n-space align="center">
                      <n-button secondary :loading="costPreviewLoading" @click="previewPlCost">
                        {{ t('expenses.previewCost') }}
                      </n-button>
                      <n-text v-if="costPreviewError" type="error">{{ t('expenses.costPreviewError') }}</n-text>
                      <n-text v-if="costPreview">
                        <template v-if="linkedSourceCost?.linked_fixed_asset_id">{{ t('fixedAssets.savedPurchaseInputs') }}: {{ linkedSourceCost.annual_cost }} ({{ linkedSourceCost.included_in_pl ? t('expenses.plInclusionIncluded') : t('expenses.plInclusionExcluded') }}) ·
                        {{ t('fixedAssets.effectiveCurrentCost') }}: {{ linkedSourceCost.effective_annual_cost ?? linkedSourceCost.annual_cost }} · </template>
                        <template v-else-if="originalCostPreview">{{ t('expenses.costPreviewBefore') }}: {{ originalCostPreview.annual_cost }} · </template>
                        {{ t('expenses.costPreviewAfter') }}: {{ costPreview.annual_cost }}
                      </n-text>
                    </n-space>
                  </n-form-item>

                  <!-- Reference -->
                  <n-form-item :label="t('expenses.reference')">
                    <n-input
                      v-model:value="reference"
                      :placeholder="t('expenses.referencePlaceholder')"
                    />
                  </n-form-item>

                  <!-- Note -->
                  <n-form-item :label="t('expenses.note')">
                    <n-input
                      v-model:value="note"
                      type="textarea"
                      :placeholder="t('expenses.notePlaceholder')"
                      :rows="3"
                    />
                  </n-form-item>

                  <n-space>
                    <!-- Save button: prod-build safe using v-if/v-else -->
                    <n-button v-if="saving" type="primary" loading>{{ t('expenses.save') }}</n-button>
                    <n-button v-else type="primary" attr-type="submit" :disabled="deletingAttachmentHasAiValues">{{ t('expenses.save') }}</n-button>
                    <n-button @click="router.push({ path: '/expenses', query: { tab: 'purchase' } })">{{ t('expenses.cancel') }}</n-button>
                  </n-space>
                </n-form>
                <n-alert v-if="linkedSourceCost?.linked_fixed_asset_id" type="info" :show-icon="false">
                  {{ t('fixedAssets.linkedSourceNotice') }}
                  <n-button text @click="router.push({ path: '/fixed-assets', query: { asset: linkedSourceCost.linked_fixed_asset_id, year: savedExpenseDate?.slice(0, 4) } })">
                    {{ t('fixedAssets.openRegisteredAsset') }}
                  </n-button>
                </n-alert>
                <n-form v-if="isEdit && !isDraft && linkedSourceCostChecked && !linkedSourceCost?.linked_fixed_asset_id" class="asset-preview-form" @submit.prevent="previewFixedAsset">
                  <n-card v-if="isEdit && !isDraft" size="small" :title="t('fixedAssets.previewTitle')">
                    <n-text depth="3">{{ t('fixedAssets.previewReadOnly') }}</n-text>
                    <n-form-item :label="t('fixedAssets.assetName')">
                      <n-input v-model:value="assetName" />
                    </n-form-item>
                    <n-form-item :label="t('fixedAssets.inServiceDate')">
                      <n-input v-model:value="assetInServiceDate" type="text" placeholder="YYYY-MM-DD" />
                    </n-form-item>
                    <n-form-item :label="t('fixedAssets.usefulLifeYears')">
                      <n-input-number v-model:value="assetUsefulLife" :min="1" :max="100" :precision="0" />
                    </n-form-item>
                    <n-form-item :label="t('fixedAssets.residualValue')">
                      <n-input-number v-model:value="assetResidualValue" :min="0" :precision="2" />
                    </n-form-item>
                    <n-form-item :label="t('fixedAssets.useType')">
                      <n-select v-model:value="assetUseType" :options="[
                        { label: t('fixedAssets.fullBusiness'), value: 'FULL_BUSINESS' },
                        { label: t('fixedAssets.mixedUse'), value: 'MIXED_USE' },
                      ]" />
                    </n-form-item>
                    <n-form-item v-if="assetUseType === 'MIXED_USE'" :label="t('expenses.businessPercentage')">
                      <n-input-number v-model:value="assetBusinessPercentage" :min="0" :max="100" :precision="1" />
                    </n-form-item>
                    <n-space vertical>
                      <n-button secondary attr-type="submit" :loading="assetPreviewLoading" @click.prevent="previewFixedAsset">
                        {{ t('fixedAssets.previewButton') }}
                      </n-button>
                      <n-alert v-if="assetPreviewError" type="error" :show-icon="false">{{ assetPreviewError }}</n-alert>
                      <template v-if="assetPreview">
                        <n-text>{{ t('fixedAssets.acquisitionCost') }}: {{ assetPreview.acquisition_cost }}</n-text>
                        <n-text>{{ t('fixedAssets.annualBaseline') }}: {{ assetPreview.annual_baseline }}</n-text>
                        <n-text>{{ t('fixedAssets.coverageRange') }}: {{ assetPreview.preview_period_start }} – {{ assetPreview.preview_period_end }}</n-text>
                        <n-alert v-if="assetPreview.legacy_preview_truncated" type="warning" :show-icon="false">
                          {{ t('fixedAssets.legacyTruncated') }}
                        </n-alert>
                        <n-text v-if="assetPreview.pending_in_service">{{ t('fixedAssets.pendingInService') }}</n-text>
                        <n-alert v-if="assetPreview.conversion_blocked" type="warning" :show-icon="false">
                          {{ assetPreview.conversion_block_reason }}
                        </n-alert>
                        <n-button type="primary" :loading="assetConversionLoading" :disabled="assetPreview.conversion_blocked" @click="confirmFixedAssetConversion">
                          {{ t('fixedAssets.confirmConversion') }}
                        </n-button>
                        <n-text>{{ t('fixedAssets.costCue') }}</n-text>
                        <n-text v-for="period in assetPreview.period_deltas" :key="period.period_end">
                          {{ t('fixedAssets.period') }} {{ period.period_end }} ·
                          {{ t('fixedAssets.reportLegacyBaseline') }} ({{ period.legacy_attribution_date ?? '—' }}): {{ period.legacy_expense_amount }} ·
                          {{ t('fixedAssets.completedAssetDepreciation') }} ({{ period.fixed_asset_attribution_date ?? '—' }}): {{ period.fixed_asset_depreciation }} ·
                          {{ t('fixedAssets.reportPeriodDelta') }} {{ period.difference }} ·
                          {{ t('fixedAssets.futurePeriodPlan') }} ({{ t('fixedAssets.legacyTotal') }} / {{ t('fixedAssets.assetBusiness') }}) {{ period.planned_legacy_expense_amount }} / {{ period.planned_fixed_asset_depreciation }} ·
                          {{ t('fixedAssets.plannedDifference') }} {{ period.planned_difference }}
                        </n-text>
                        <n-text v-for="delta in assetPreview.yearly_deltas" :key="delta.year">
                          {{ delta.year }} · {{ t('fixedAssets.legacy') }} {{ delta.legacy_expense_amount }} ·
                          {{ t('fixedAssets.realizedAsset') }} ({{ t('fixedAssets.assetTotal') }} / {{ t('fixedAssets.assetBusiness') }}) {{ delta.realized_asset_depreciation }} / {{ delta.fixed_asset_depreciation }} ·
                          {{ t('fixedAssets.reportPeriodDelta') }} {{ delta.difference }} ·
                          {{ t('fixedAssets.fullYearPlan') }} ({{ t('fixedAssets.legacyTotal') }} / {{ t('fixedAssets.assetTotal') }} / {{ t('fixedAssets.assetBusiness') }}) {{ delta.planned_legacy_expense_amount }} / {{ delta.asset_depreciation }} / {{ delta.planned_fixed_asset_depreciation }} ·
                          {{ t('fixedAssets.plannedDifference') }} {{ delta.planned_difference }} ·
                          {{ t('fixedAssets.plannedBusinessReduction') }} {{ delta.planned_business_use_reduction }}
                          · {{ t('fixedAssets.bookValue') }} {{ delta.opening_book_value }} → {{ delta.closing_book_value }}
                          · {{ t('fixedAssets.plannedBookValue') }} {{ delta.planned_opening_book_value }} → {{ delta.planned_closing_book_value }}
                        </n-text>
                      </template>
                    </n-space>
                  </n-card>

                </n-form>
              </div>

              <!-- Right: attachments + AI -->
              <div v-if="isEdit" class="attach-col">
                <n-card :title="t('receipt.title')" size="small">
                  <!-- Upload -->
                  <n-upload
                    :custom-request="handleFileUpload"
                    :disabled="attachmentListBusy"
                    :show-file-list="false"
                    accept="image/png,image/jpeg,image/webp,application/pdf"
                    style="margin-bottom: 12px"
                  >
                    <n-button :disabled="attachmentListBusy">
                      <template #icon>
                        <n-icon v-if="!attachUploading"><CloudUploadOutline /></n-icon>
                        <n-spin v-else size="small" />
                      </template>
                      {{ t('receipt.upload') }}
                    </n-button>
                  </n-upload>

                  <n-alert v-if="attachError" type="error" size="small" closable style="margin-bottom: 8px">
                    {{ attachError }}
                  </n-alert>
                  <n-button v-if="uncertainAttachmentId" size="tiny" :loading="recheckingAttachments" :disabled="attachUploading || deletingAttachId !== null || recheckingAttachments" @click="recheckAttachments">
                    {{ t('expenses.ai.recheckAttachments') }}
                  </n-button>

                  <!-- AI extract button for last uploaded attachment -->
                  <div v-if="lastAiAttachmentId" style="margin-bottom: 12px">
                    <n-alert v-if="aiError" type="warning" size="small" closable style="margin-bottom: 8px" @close="aiError = null">
                      {{ aiError }}
                    </n-alert>
                    <n-alert v-if="aiNote" type="info" size="small" closable style="margin-bottom: 8px" @close="aiNote = null">
                      {{ t('expenses.ai.note') }}: {{ aiNote }}
                    </n-alert>
                    <!-- Prod-build safe: separate v-if/v-else -->
                    <n-button
                      v-if="aiExtracting"
                      type="info"
                      size="small"
                      loading
                    >
                      <template #icon><n-icon><SparklesOutline /></n-icon></template>
                      {{ t('expenses.ai.extracting') }}
                    </n-button>
                    <n-button
                      v-else
                      type="info"
                      size="small"
                      :disabled="saving || deletingAttachId === lastAiAttachmentId"
                      @click="handleAiExtract(lastAiAttachmentId!)"
                    >
                      <template #icon><n-icon><SparklesOutline /></n-icon></template>
                      {{ t('expenses.ai.autoFill') }}
                    </n-button>
                  </div>

                  <!-- Attachment list -->
                  <n-spin :show="attachLoading">
                    <n-empty v-if="!attachLoading && attachments.length === 0" :description="t('receipt.empty')" size="small" />
                    <div v-for="attach in attachments" :key="attach.id" class="attach-item">
                      <div class="attach-meta">
                        <span class="attach-name">{{ attach.filename ?? t('receipt.unnamed') }}</span>
                        <span class="attach-size">{{ formatBytes(attach.byte_size) }}</span>
                      </div>

                      <!-- Preview -->
                      <div v-if="isImage(attach.mime_type)" class="attach-preview">
                        <img :src="attachmentContentUrl(attach.id)" :alt="attach.filename ?? ''" />
                      </div>
                      <div v-else-if="isPdf(attach.mime_type)" class="attach-preview attach-pdf">
                        <embed
                          :src="attachmentContentUrl(attach.id)"
                          type="application/pdf"
                          width="100%"
                          height="300"
                        />
                      </div>

                      <div class="attach-actions">
                        <!-- AI extract for this specific attachment -->
                        <n-button
                          v-if="!aiExtracting"
                          size="tiny"
                          type="info"
                          :disabled="saving || deletingAttachId === attach.id"
                          @click="() => { lastAiAttachmentId = attach.id; void handleAiExtract(attach.id) }"
                        >
                          <template #icon><n-icon><SparklesOutline /></n-icon></template>
                          {{ t('expenses.ai.autoFill') }}
                        </n-button>
                        <n-button v-else size="tiny" type="info" loading>
                          {{ t('expenses.ai.extracting') }}
                        </n-button>

                        <a :href="attachmentContentUrl(attach.id)" :download="attach.filename ?? 'receipt'">
                          <n-button size="tiny">
                            <template #icon><n-icon><DownloadOutline /></n-icon></template>
                            {{ t('receipt.download') }}
                          </n-button>
                        </a>

                        <n-popconfirm @positive-click="() => handleDeleteAttachment(attach.id)">
                          <template #trigger>
                            <n-button
                              v-if="deletingAttachId === attach.id"
                              size="tiny"
                              type="error"
                              loading
                            />
                            <n-button
                              v-else
                              size="tiny"
                              type="error"
                              :disabled="attachUploading || deletingAttachId !== null || recheckingAttachments || uncertainAttachmentId !== null"
                            >
                              <template #icon><n-icon><TrashOutline /></n-icon></template>
                              {{ t('receipt.delete') }}
                            </n-button>
                          </template>
                          {{ t('receipt.deleteConfirm') }}
                        </n-popconfirm>
                      </div>
                    </div>
                  </n-spin>
                </n-card>
              </div>
              <div v-else class="attach-col-hint">
                <n-alert type="info" size="small">{{ t('receipt.saveFirstHint') }}</n-alert>
              </div>
            </div>
          </n-spin>
    </div>
  </div>
</template>

<style scoped>
.expense-edit-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}

.page-header h2 {
  margin: 0;
}

.header-badges {
  display: flex;
  gap: 8px;
}

.edit-layout {
  display: flex;
  gap: 24px;
  align-items: flex-start;
}

.form-col {
  flex: 1;
  min-width: 0;
}

.vat-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  min-width: 0;
}

.treatment-suggestion {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 8px;
  overflow-wrap: anywhere;
}

.attach-col {
  width: 340px;
  flex-shrink: 0;
}

.attach-col-hint {
  width: 340px;
  flex-shrink: 0;
}

.attach-item {
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 10px;
}

.attach-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.attach-name {
  font-weight: 500;
  font-size: 13px;
  word-break: break-all;
}

.attach-size {
  font-size: 11px;
  color: var(--n-text-color-3);
  white-space: nowrap;
  margin-left: 8px;
}

.attach-preview {
  margin-bottom: 8px;
}

.attach-preview img {
  max-width: 100%;
  border-radius: 4px;
}

.attach-pdf {
  background: #f5f5f5;
}

.attach-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
