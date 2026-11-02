<script setup lang="ts">
import { onMounted, computed, h, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NButton, NSpace, NDataTable, NAlert, NSpin,
  NPagination, NSelect, NTag, NPopconfirm, NModal, NForm,
  NFormItem, NInput, NInputNumber, NCheckbox, NSwitch, NDatePicker,
  NRadioGroup, NRadioButton, NText,
} from 'naive-ui'
import { AddOutline, PlayOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { useRecurringExpensesStore } from '../../stores/recurringExpenses'
import type { RecurringExpenseRead, RecurringExpenseInput } from '../../stores/recurringExpenses'
import { get, ApiError } from '../../api/http'
import type { components } from '../../api/schema'
import { formatDate } from '../../utils/date'
import { useExpenseCostPreview } from '../../composables/useExpenseCostPreview'

type PaidBy = components['schemas']['PaidBy']
type PlCostBasis = components['schemas']['PlCostBasis']

type ExpenseCategoryRead = components['schemas']['ExpenseCategoryRead']
type ExpenseCategoryListResponse = components['schemas']['ExpenseCategoryListResponse']
type VatTreatmentRead = components['schemas']['VatTreatmentRead']
type VatTreatmentListResponse = components['schemas']['VatTreatmentListResponse']
type VatRateRead = components['schemas']['VatRateRead']
type VatRateListResponse = components['schemas']['VatRateListResponse']

const { t } = useI18n()
const store = useRecurringExpensesStore()

const categories = ref<ExpenseCategoryRead[]>([])
const vatTreatments = ref<VatTreatmentRead[]>([])
const vatRates = ref<VatRateRead[]>([])

async function loadDictionaries() {
  const [catRes, treatRes, rateRes] = await Promise.all([
    get<ExpenseCategoryListResponse>('/api/v1/expense-categories'),
    get<VatTreatmentListResponse>('/api/v1/vat-treatments'),
    get<VatRateListResponse>('/api/v1/vat-rates'),
  ])
  categories.value = catRes.items
  vatTreatments.value = treatRes.items.filter(item => item.side === 'PURCHASE')
  vatRates.value = rateRes.items
}

const categoryOptions = computed(() =>
  categories.value.map(c => ({ label: c.name, value: c.id }))
)
const treatmentOptions = computed(() =>
  vatTreatments.value.map(t => ({ label: `${t.code} – ${t.label}`, value: t.id }))
)
const rateOptions = computed(() =>
  vatRates.value.map(r => ({ label: `${r.percent}% – ${r.label}`, value: r.id }))
)
const frequencyOptions = computed(() => [
  { label: t('recurring.frequencyMonthly'), value: 'MONTHLY' },
  { label: t('recurring.frequencyQuarterly'), value: 'QUARTERLY' },
  { label: t('recurring.frequencyYearly'), value: 'YEARLY' },
])

const activeFilterOptions = computed(() => [
  { label: t('recurring.filterActive'), value: 'true' },
  { label: t('recurring.filterPaused'), value: 'false' },
])

// --- Form modal ---
const showModal = ref(false)
const editingId = ref<string | null>(null)
const modalError = ref<string | null>(null)
const modalSaving = ref(false)
const runningId = ref<string | null>(null)
const deletingId = ref<string | null>(null)
const runResult = ref<string | null>(null)

// Form fields
const formName = ref('')
const formCategoryId = ref<string | null>(null)
const formSupplierName = ref('')
const formVatTreatmentId = ref<string | null>(null)
const formVatRateId = ref<string | null>(null)
const formNetAmount = ref<number | null>(null)
const formVatAmount = ref<number | null>(null)
const formDeductible = ref<boolean | null>(null)
const formNote = ref('')
const formFrequency = ref<string>('MONTHLY')
const formStartDate = ref(new Date().toISOString().slice(0, 10))
const formEndDate = ref<string | null>(null)
const formMaxOccurrences = ref<number | null>(null)
const formActive = ref(true)
// Bookkeeping fields (M8.5)
const formPaidBy = ref<PaidBy>('BUSINESS')
const formBusinessPercentage = ref<number>(100)
const formDepreciationYears = ref<number>(1)
const formIncludeInPl = ref(true)
const formPlCostBasis = ref<PlCostBasis>('AUTO')

const {
  costPreview, originalCostPreview, costPreviewLoading, costPreviewError,
  previewPlCost, resetCostPreview,
} = useExpenseCostPreview(
  () => showModal.value && formVatTreatmentId.value ? {
    net_amount: formNetAmount.value ?? 0,
    vat_amount: formVatAmount.value ?? 0,
    vat_treatment_id: formVatTreatmentId.value,
    category_id: formCategoryId.value,
    deductible: formDeductible.value,
    pl_cost_basis: formPlCostBasis.value,
    include_in_pl: formIncludeInPl.value,
    business_percentage: formBusinessPercentage.value,
    depreciation_years: formDepreciationYears.value,
  } : null,
  () => showModal.value && editingId.value ? `/api/v1/recurring-expenses/${editingId.value}/cost-preview` : null,
)

const formStartDateTs = computed({
  get: () => formStartDate.value ? new Date(formStartDate.value).getTime() : null,
  set: (v: number | null) => { formStartDate.value = v ? new Date(v).toISOString().slice(0, 10) : '' },
})
const formEndDateTs = computed({
  get: () => formEndDate.value ? new Date(formEndDate.value).getTime() : null,
  set: (v: number | null) => { formEndDate.value = v ? new Date(v).toISOString().slice(0, 10) : null },
})

function resetForm() {
  formName.value = ''
  formCategoryId.value = null
  formSupplierName.value = ''
  formVatTreatmentId.value = null
  formVatRateId.value = null
  formNetAmount.value = null
  formVatAmount.value = null
  formDeductible.value = null
  formNote.value = ''
  formFrequency.value = 'MONTHLY'
  formStartDate.value = new Date().toISOString().slice(0, 10)
  formEndDate.value = null
  formMaxOccurrences.value = null
  formActive.value = true
  // Bookkeeping fields (M8.5) defaults
  formPaidBy.value = 'BUSINESS'
  formBusinessPercentage.value = 100
  formDepreciationYears.value = 1
  formIncludeInPl.value = true
  formPlCostBasis.value = 'AUTO'
  modalError.value = null
}

function openCreate() {
  editingId.value = null
  resetForm()
  showModal.value = true
}

function openEdit(row: RecurringExpenseRead) {
  editingId.value = row.id
  formName.value = row.name
  formCategoryId.value = row.category_id ?? null
  formSupplierName.value = row.supplier_name ?? ''
  formVatTreatmentId.value = row.vat_treatment_id ?? null
  formVatRateId.value = row.vat_rate_id ?? null
  formNetAmount.value = Number(row.net_amount)
  formVatAmount.value = Number(row.vat_amount)
  formDeductible.value = row.deductible
  formNote.value = row.note ?? ''
  formFrequency.value = row.frequency
  formStartDate.value = row.start_date
  formEndDate.value = row.end_date ?? null
  formMaxOccurrences.value = row.max_occurrences ?? null
  formActive.value = row.active
  // Bookkeeping fields (M8.5) — programmatic assignment, does NOT trigger @update:value
  formPaidBy.value = row.paid_by
  formBusinessPercentage.value = Number(row.business_percentage)
  formDepreciationYears.value = row.depreciation_years
  formIncludeInPl.value = row.include_in_pl
  formPlCostBasis.value = row.pl_cost_basis
  modalError.value = null
  showModal.value = true
}

async function handleSaveModal() {
  if (!formCategoryId.value || !formVatTreatmentId.value || !formVatRateId.value || !formName.value) {
    modalError.value = t('recurring.validationError')
    return
  }
  resetCostPreview()
  modalSaving.value = true
  modalError.value = null
  try {
    const body: RecurringExpenseInput = {
      name: formName.value,
      category_id: formCategoryId.value,
      supplier_name: formSupplierName.value || null,
      vat_treatment_id: formVatTreatmentId.value,
      vat_rate_id: formVatRateId.value,
      net_amount: formNetAmount.value ?? 0,
      vat_amount: formVatAmount.value ?? 0,
      deductible: formDeductible.value,
      note: formNote.value || null,
      frequency: formFrequency.value,
      start_date: formStartDate.value,
      end_date: formEndDate.value,
      max_occurrences: formMaxOccurrences.value,
      active: formActive.value,
      // Bookkeeping fields (M8.5) — passed as-is, no local computation
      paid_by: formPaidBy.value,
      business_percentage: formBusinessPercentage.value,
      depreciation_years: formDepreciationYears.value,
      include_in_pl: formIncludeInPl.value,
      pl_cost_basis: formPlCostBasis.value,
    }
    if (editingId.value) {
      await store.updateRecurringExpense(editingId.value, body)
    } else {
      await store.createRecurringExpense(body)
    }
    showModal.value = false
    void store.fetchRecurringExpenses()
  } catch (e: unknown) {
    modalError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    modalSaving.value = false
  }
}

async function handleRunNow(id: string) {
  runningId.value = id
  runResult.value = null
  try {
    const res = await store.runNow(id)
    runResult.value = t('recurring.runNowResult', { generated: res.generated, next: res.next_run_date })
    void store.fetchRecurringExpenses()
    // Also refresh expenses list if it's open
  } catch (e: unknown) {
    runResult.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    runningId.value = null
  }
}

async function handleDelete(id: string) {
  deletingId.value = id
  try {
    await store.deleteRecurringExpense(id)
    void store.fetchRecurringExpenses()
  } catch {
    // error in store
  } finally {
    deletingId.value = null
  }
}

function handleFilterChange(val: string | null) {
  store.activeFilter = val === null ? null : val === 'true'
  store.offset = 0
  void store.fetchRecurringExpenses()
}

function handlePageChange(page: number) {
  store.offset = (page - 1) * store.limit
  void store.fetchRecurringExpenses()
}

const currentPage = computed(() => Math.floor(store.offset / store.limit) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(store.total / store.limit)))
const fmtMoney = (v: string | number) => Number(v).toFixed(2)

onMounted(() => {
  void store.fetchRecurringExpenses()
  void loadDictionaries()
})

const columns = computed(() => [
  {
    title: t('recurring.name'),
    key: 'name',
    ellipsis: { tooltip: true },
  },
  {
    title: t('recurring.frequency'),
    key: 'frequency',
    width: 110,
    render(row: RecurringExpenseRead) {
      return t(`recurring.frequency${row.frequency.charAt(0) + row.frequency.slice(1).toLowerCase()}`)
    },
  },
  {
    title: t('recurring.netAmount'),
    key: 'net_amount',
    align: 'right' as const,
    width: 110,
    render(row: RecurringExpenseRead) {
      return fmtMoney(row.net_amount)
    },
  },
  {
    title: t('recurring.grossAmount'),
    key: 'gross_amount',
    align: 'right' as const,
    width: 110,
    render(row: RecurringExpenseRead) {
      return fmtMoney(row.gross_amount)
    },
  },
  {
    title: t('recurring.nextRun'),
    key: 'next_run_date',
    width: 120,
    render(row: RecurringExpenseRead) {
      return formatDate(row.next_run_date)
    },
  },
  {
    title: t('recurring.generated'),
    key: 'occurrences_generated',
    width: 90,
    align: 'center' as const,
  },
  {
    title: t('expenses.paidBy'),
    key: 'paid_by',
    width: 100,
    align: 'center' as const,
    render(row: RecurringExpenseRead) {
      return row.paid_by === 'PRIVATE'
        ? h(NTag, { type: 'warning', size: 'small' }, () => t('expenses.paidByPrivate'))
        : h(NTag, { type: 'default', size: 'small' }, () => t('expenses.paidByBusiness'))
    },
  },
  {
    title: t('expenses.bookkeeping'),
    key: 'bookkeeping',
    width: 120,
    align: 'center' as const,
    render(row: RecurringExpenseRead) {
      const tags: ReturnType<typeof h>[] = []
      const pct = Number(row.business_percentage)
      if (pct !== 100) {
        tags.push(h(NTag, { type: 'info', size: 'small', style: 'margin: 1px' }, () => `${pct.toFixed(0)}%`))
      }
      if (row.depreciation_years > 1) {
        tags.push(h(NTag, { type: 'info', size: 'small', style: 'margin: 1px' }, () => `${row.depreciation_years}y`))
      }
      return tags.length ? h(NSpace, { size: 2, align: 'center', wrap: true }, () => tags) : h(NText, { depth: 3 }, () => '—')
    },
  },
  {
    title: t('recurring.active'),
    key: 'active',
    width: 80,
    align: 'center' as const,
    render(row: RecurringExpenseRead) {
      return row.active
        ? h(NTag, { type: 'success', size: 'small' }, () => t('recurring.statusActive'))
        : h(NTag, { type: 'default', size: 'small' }, () => t('recurring.statusPaused'))
    },
  },
  {
    title: '',
    key: 'actions',
    width: 240,
    align: 'center' as const,
    render(row: RecurringExpenseRead) {
      const editBtn = h(NButton, {
        size: 'small',
        secondary: true,
        onClick: () => openEdit(row),
      }, () => t('recurring.edit'))

      // Prod-build safe: separate loading buttons
      const runBtn = runningId.value === row.id
        ? h(NButton, { size: 'small', type: 'primary', loading: true }, () => t('recurring.runNow'))
        : h(NButton, {
            size: 'small',
            type: 'primary',
            onClick: () => void handleRunNow(row.id),
          }, () => h(NSpace, { size: 4, align: 'center' }, () => [
            h(NIcon, null, () => h(PlayOutline)),
            t('recurring.runNow'),
          ]))

      const deleteBtn = h(NPopconfirm,
        { onPositiveClick: () => void handleDelete(row.id) },
        {
          trigger: () => (
            deletingId.value === row.id
              ? h(NButton, { size: 'small', type: 'error', loading: true }, () => t('recurring.delete'))
              : h(NButton, { size: 'small', type: 'error' }, () => t('recurring.delete'))
          ),
          default: () => t('recurring.deleteConfirm'),
        },
      )

      return h(NSpace, { size: 'small' }, () => [editBtn, runBtn, deleteBtn])
    },
  },
])
</script>

<template>
  <div class="recurring-list-page">
    <div class="recurring-container">
          <div class="recurring-header">
            <div>
              <h2>{{ t('recurring.title') }}</h2>
              <p style="margin: 4px 0 0; color: var(--n-text-color-3)">{{ t('recurring.subtitle') }}</p>
            </div>
            <n-space align="center">
              <n-select
                :value="store.activeFilter === null ? null : String(store.activeFilter)"
                :options="activeFilterOptions"
                :placeholder="t('recurring.filterAll')"
                style="width: 140px"
                clearable
                @update:value="handleFilterChange"
              />
              <n-button type="primary" @click="openCreate">
                <template #icon><n-icon><AddOutline /></n-icon></template>
                {{ t('recurring.new') }}
              </n-button>
            </n-space>
          </div>

          <n-alert v-if="store.error" type="error" style="margin-bottom: 16px">{{ store.error }}</n-alert>
          <n-alert v-if="runResult" type="info" closable style="margin-bottom: 16px" @close="runResult = null">
            {{ runResult }}
          </n-alert>

          <n-spin :show="store.loading">
            <n-empty v-if="!store.loading && store.items.length === 0" :description="t('recurring.empty')" />
            <n-data-table
              v-else
              :columns="columns"
              :data="store.items"
              :bordered="false"
              :row-key="(row: RecurringExpenseRead) => row.id"
              striped
            />
            <div v-if="store.total > store.limit" class="pagination-container">
              <n-pagination
                :page="currentPage"
                :page-count="pageCount"
                @update:page="handlePageChange"
              />
            </div>
          </n-spin>
    </div>

    <!-- Create / Edit modal -->
    <n-modal
      v-model:show="showModal"
      preset="card"
      :title="editingId ? t('recurring.editTitle') : t('recurring.newTitle')"
      :style="{ width: '600px', maxWidth: '95vw' }"
    >
      <n-form label-placement="left" label-width="130" @submit.prevent="handleSaveModal">
        <n-form-item :label="t('recurring.name')">
          <n-input v-model:value="formName" :placeholder="t('recurring.namePlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('recurring.category')">
          <n-select v-model:value="formCategoryId" :options="categoryOptions" clearable filterable />
        </n-form-item>
        <n-form-item :label="t('recurring.supplier')">
          <n-input v-model:value="formSupplierName" />
        </n-form-item>
        <n-form-item :label="t('recurring.vatTreatment')">
          <n-select v-model:value="formVatTreatmentId" :options="treatmentOptions" clearable />
        </n-form-item>
        <n-form-item :label="t('recurring.vatRate')">
          <n-select v-model:value="formVatRateId" :options="rateOptions" clearable />
        </n-form-item>
        <n-form-item :label="t('recurring.netAmount')">
          <n-input-number v-model:value="formNetAmount" :precision="2" :min="0" style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('recurring.vatAmount')">
          <n-input-number v-model:value="formVatAmount" :precision="2" :min="0" style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('recurring.frequency')">
          <n-select v-model:value="formFrequency" :options="frequencyOptions" />
        </n-form-item>
        <n-form-item :label="t('recurring.startDate')">
          <n-date-picker
            :value="formStartDateTs"
            type="date"
            style="width: 100%"
            @update:value="formStartDateTs = $event"
          />
        </n-form-item>
        <n-form-item :label="t('recurring.endDate')">
          <n-date-picker
            :value="formEndDateTs"
            type="date"
            clearable
            style="width: 100%"
            @update:value="formEndDateTs = $event"
          />
        </n-form-item>
        <n-form-item :label="t('recurring.maxOccurrences')">
          <n-input-number v-model:value="formMaxOccurrences" :min="1" clearable style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('recurring.deductible')">
          <n-checkbox :checked="formDeductible ?? true" @update:checked="formDeductible = $event">
            {{ t('expenses.deductibleLabel') }}
          </n-checkbox>
        </n-form-item>

        <!-- Paid By (M8.5) -->
        <n-form-item :label="t('expenses.paidBy')">
          <n-radio-group
            :value="formPaidBy"
            @update:value="(v: PaidBy) => { formPaidBy = v }"
          >
            <n-radio-button value="BUSINESS">{{ t('expenses.paidByBusiness') }}</n-radio-button>
            <n-radio-button value="PRIVATE">{{ t('expenses.paidByPrivate') }}</n-radio-button>
          </n-radio-group>
        </n-form-item>

        <!-- Business % (M8.5) -->
        <n-form-item :label="t('expenses.businessPercentage')">
          <n-input-number
            :value="formBusinessPercentage"
            :precision="1"
            :min="0"
            :max="100"
            style="width: 160px"
            @update:value="(v: number | null) => { formBusinessPercentage = v ?? 100 }"
          >
            <template #suffix>%</template>
          </n-input-number>
        </n-form-item>

        <!-- Depreciation years (M8.5) -->
        <n-form-item :label="t('expenses.depreciationYears')">
          <n-input-number
            :value="formDepreciationYears"
            :precision="0"
            :min="1"
            style="width: 160px"
            @update:value="(v: number | null) => { formDepreciationYears = v ?? 1 }"
          />
        </n-form-item>
        <n-form-item :label="t('expenses.includeInPl')">
          <n-checkbox v-model:checked="formIncludeInPl">{{ t('expenses.includeInPl') }}</n-checkbox>
        </n-form-item>
        <n-form-item :label="t('expenses.plCostBasis')">
          <n-select v-model:value="formPlCostBasis" :options="[
            { label: t('expenses.costBasisAuto'), value: 'AUTO' },
            { label: t('expenses.costBasisNet'), value: 'NET' },
            { label: t('expenses.costBasisGross'), value: 'GROSS' },
          ]" />
        </n-form-item>

        <n-form-item :label="t('expenses.costPreview')">
          <n-space vertical>
            <n-text depth="3">{{ t('recurring.costPreviewHint') }}</n-text>
            <n-button secondary :loading="costPreviewLoading" @click="previewPlCost">
              {{ t('expenses.previewCost') }}
            </n-button>
            <n-text v-if="costPreviewError" type="error">{{ t('expenses.costPreviewError') }}</n-text>
            <n-text v-if="costPreview">
              <template v-if="originalCostPreview">{{ t('expenses.costPreviewBefore') }}: {{ originalCostPreview.annual_cost }} · </template>
              {{ t('expenses.costPreviewAfter') }}: {{ costPreview.annual_cost }}
            </n-text>
          </n-space>
        </n-form-item>

        <n-form-item :label="t('recurring.active')">
          <n-switch v-model:value="formActive" />
        </n-form-item>
        <n-form-item :label="t('recurring.note')">
          <n-input v-model:value="formNote" type="textarea" :rows="2" />
        </n-form-item>

        <n-alert v-if="modalError" type="error" size="small" style="margin-bottom: 12px">{{ modalError }}</n-alert>

        <n-space>
          <n-button v-if="modalSaving" type="primary" loading>{{ t('recurring.save') }}</n-button>
          <n-button v-else type="primary" attr-type="submit">{{ t('recurring.save') }}</n-button>
          <n-button @click="showModal = false">{{ t('recurring.cancel') }}</n-button>
        </n-space>
      </n-form>
    </n-modal>
  </div>
</template>

<style scoped>
.recurring-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

.recurring-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}

.recurring-header h2 {
  margin: 0;
}

.pagination-container {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
