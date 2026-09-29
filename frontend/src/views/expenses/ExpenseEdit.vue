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
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { localDateStr } from '../../utils/date'
import { toExpenseInput, usePurchaseExpenseDictionaries } from '../../composables/usePurchaseExpenseDictionaries'
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
import { post, ApiError } from '../../api/http'
import type { components } from '../../api/schema'

type PaidBy = components['schemas']['PaidBy']
type ExpenseCalculateResult = components['schemas']['ExpenseCalculateResult']

const route = useRoute()
const router = useRouter()
const { t, locale } = useI18n()
const store = useExpensesStore()

const expenseId = computed(() => route.params.id as string | undefined)
const isEdit = computed(() => !!expenseId.value)

// ---- Form state (raw inputs only) ----
const expenseDate = ref(localDateStr(new Date()))
const categoryId = ref<string | null>(null)
const supplierName = ref('')
const vatTreatmentId = ref<string | null>(null)
const vatRateId = ref<string | null>(null)
const netAmount = ref<number | null>(null)
const vatAmount = ref<number | null>(null)
const deductible = ref<boolean | null>(null)
const reference = ref('')
const note = ref('')
// ---- Bookkeeping fields (M8.5) — raw inputs, no local computation ----
const paidBy = ref<PaidBy>('BUSINESS')
const businessPercentage = ref<number>(100)
const depreciationYears = ref<number>(1)

// ---- Backend-computed display fields (red-line 1: read from response only) ----
const grossAmountDisplay = ref<string | null>(null)
const isDraft = ref(false)
const recurringExpenseId = ref<string | null>(null)

// ---- Dictionary data ----
const { categories, rates: vatRates, categoryOptions, purchaseTreatmentOptions, vatRateOptions, load: loadDictionaries } = usePurchaseExpenseDictionaries()

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

// ---- AI state ----
const aiExtracting = ref(false)
const aiError = ref<string | null>(null)
const aiNote = ref<string | null>(null)
const lastAiAttachmentId = ref<string | null>(null)

// ---- Options ----
// ---- Load data ----
function fillFromExpense(expense: ExpenseRead) {
  expenseDate.value = expense.expense_date
  categoryId.value = expense.category_id ?? null
  supplierName.value = expense.supplier_name ?? ''
  vatTreatmentId.value = expense.vat_treatment_id ?? null
  vatRateId.value = expense.vat_rate_id ?? null
  netAmount.value = Number(expense.net_amount)
  vatAmount.value = Number(expense.vat_amount)
  deductible.value = expense.deductible
  reference.value = expense.reference ?? ''
  note.value = expense.note ?? ''
  // Bookkeeping fields (M8.5) — programmatic assignment, does NOT trigger @update:value
  paidBy.value = expense.paid_by
  businessPercentage.value = Number(expense.business_percentage)
  depreciationYears.value = expense.depreciation_years
  // Backend-computed display
  grossAmountDisplay.value = expense.gross_amount
  isDraft.value = expense.is_draft
  recurringExpenseId.value = expense.recurring_expense_id ?? null
}

// 仅在用户主动切换分类时触发：把可抵扣默认设为该分类的 default_deductible（为 null 则回落 true）。
// 编程式赋值（fillFromExpense 里的 categoryId.value = ...）不会触发 @update:value，
// 因此初始载入不会覆盖已保存的 deductible。
function onCategoryChange(newCatId: string | null) {
  const cat = categories.value.find(c => c.id === newCatId)
  deductible.value = cat && cat.default_deductible !== null ? cat.default_deductible : true
}

async function loadAttachments() {
  if (!expenseId.value) return
  attachLoading.value = true
  try {
    attachments.value = await store.listAttachments(expenseId.value)
  } catch {
    // silently ignore
  } finally {
    attachLoading.value = false
  }
}

onMounted(async () => {
  loading.value = true
  try {
    await loadDictionaries()
    if (isEdit.value && expenseId.value) {
      const expense = await store.getExpense(expenseId.value)
      fillFromExpense(expense)
      await loadAttachments()
    }
  } catch (e: unknown) {
    pageError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
})

// ---- Save ----
async function handleSave() {
  if (!categoryId.value || !vatTreatmentId.value || !vatRateId.value) {
    pageError.value = t('expenses.validationError')
    return
  }
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
    })
    let result: ExpenseRead
    if (isEdit.value && expenseId.value) {
      result = await store.updateExpense(expenseId.value, body)
    } else {
      result = await store.createExpense(body)
    }
    // Update display from backend response (red-line 1: gross comes from backend)
    fillFromExpense(result)
    saveSuccess.value = true
    if (!isEdit.value) {
      // Navigate to edit mode after create
      void router.replace({ path: `/expenses/${result.id}/edit`, query: { tab: 'purchase' } })
    }
  } catch (e: unknown) {
    pageError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

// ---- Attachment upload ----
async function handleFileUpload(options: { file: UploadFileInfo }) {
  const rawFile = options.file.file
  if (!rawFile || !expenseId.value) return
  attachUploading.value = true
  attachError.value = null
  try {
    const newAttach = await store.uploadAttachment(expenseId.value, rawFile)
    attachments.value.push(newAttach)
    // After upload, offer AI extraction on this attachment
    lastAiAttachmentId.value = newAttach.id
  } catch (e: unknown) {
    attachError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    attachUploading.value = false
  }
  // Return false to prevent naive-ui default upload behaviour
  return false
}

async function handleDeleteAttachment(id: string) {
  deletingAttachId.value = id
  try {
    await store.deleteAttachment(id)
    attachments.value = attachments.value.filter(a => a.id !== id)
    if (lastAiAttachmentId.value === id) lastAiAttachmentId.value = null
  } catch (e: unknown) {
    attachError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    deletingAttachId.value = null
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

async function recomputeVat() {
  if (netAmount.value === null || !vatRateId.value) return
  try {
    const result = await post<ExpenseCalculateResult>('/api/v1/expenses/calculate', {
      net_amount: netAmount.value,
      vat_rate_id: vatRateId.value,
    })
    vatAmount.value = Number(result.vat_amount)
    grossAmountDisplay.value = result.gross_amount   // 顺带实时显示 gross
  } catch {
    /* 非致命：保持现状，不打断手动录入 */
  }
}

function onVatRateChange() { void recomputeVat() }            // 选税率：立即

function onNetInput() {                                        // 改净额：防抖
  grossAmountDisplay.value = null
  if (vatDebounceTimer) clearTimeout(vatDebounceTimer)
  vatDebounceTimer = setTimeout(() => { void recomputeVat() }, 300)
}

// ---- AI extraction ----
async function handleAiExtract(attachmentId: string) {
  aiExtracting.value = true
  aiError.value = null
  aiNote.value = null
  try {
    const prefill = await store.aiExtract(attachmentId, locale.value)
    // Populate form fields from prefill — user confirms before saving
    if (prefill.expense_date) expenseDate.value = prefill.expense_date
    if (prefill.supplier_name) supplierName.value = prefill.supplier_name
    if (prefill.net_amount !== null && prefill.net_amount !== undefined) {
      netAmount.value = Number(prefill.net_amount)
    }
    if (prefill.vat_amount !== null && prefill.vat_amount !== undefined) {
      vatAmount.value = Number(prefill.vat_amount)
    }
    // Try to match VAT rate by percent
    if (prefill.vat_rate_percent) {
      const pct = Number(prefill.vat_rate_percent)
      const matchedRate = vatRates.value.find(r => Math.abs(Number(r.percent) - pct) < 0.01)
      if (matchedRate) vatRateId.value = matchedRate.id
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
    if (prefill.raw_model_note) aiNote.value = prefill.raw_model_note
    // Fill note with AI summary only if user hasn't typed anything yet
    if (prefill.summary && !note.value) {
      note.value = prefill.summary
    }
    // After prefill, clear grossAmountDisplay so user knows to save to get backend value
    grossAmountDisplay.value = null
  } catch (e: unknown) {
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
    aiExtracting.value = false
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
                    <n-select
                      v-model:value="vatTreatmentId"
                      :options="purchaseTreatmentOptions"
                      :placeholder="t('expenses.vatTreatmentPlaceholder')"
                      clearable
                    />
                  </n-form-item>

                  <!-- VAT Rate -->
                  <n-form-item :label="t('expenses.vatRate')">
                    <n-select
                      v-model:value="vatRateId"
                      :options="vatRateOptions"
                      :placeholder="t('expenses.vatRatePlaceholder')"
                      clearable
                      @update:value="onVatRateChange"
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
                  </n-form-item>

                  <!-- VAT amount -->
                  <n-form-item :label="t('expenses.vatAmount')">
                    <n-input-number
                      v-model:value="vatAmount"
                      :precision="2"
                      :min="0"
                      :placeholder="t('expenses.amountPlaceholder')"
                      style="width: 100%"
                      @update:value="grossAmountDisplay = null"
                    />
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
                    <n-button v-else type="primary" attr-type="submit">{{ t('expenses.save') }}</n-button>
                    <n-button @click="router.push({ path: '/expenses', query: { tab: 'purchase' } })">{{ t('expenses.cancel') }}</n-button>
                  </n-space>
                </n-form>
              </div>

              <!-- Right: attachments + AI -->
              <div v-if="isEdit" class="attach-col">
                <n-card :title="t('receipt.title')" size="small">
                  <!-- Upload -->
                  <n-upload
                    :custom-request="handleFileUpload"
                    :show-file-list="false"
                    accept="image/png,image/jpeg,image/webp,application/pdf"
                    style="margin-bottom: 12px"
                  >
                    <n-button :disabled="attachUploading">
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
