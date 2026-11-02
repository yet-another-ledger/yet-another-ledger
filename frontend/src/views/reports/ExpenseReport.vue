<script setup lang="ts">
/**
 * Expense report view – M10 step 4.
 *
 * Features:
 * - Date range picker (from / to), default: current year Jan 1 → today.
 * - Category breakdown table: name / net / vat / gross / deductible_net /
 *   non_deductible_net / share of total net (2 dp).
 * - Summary totals row at the bottom.
 * - Empty-state message when no confirmed expenses exist.
 */
import { computed, h, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDatePicker,
  NEmpty,
  NGrid,
  NGridItem,
  NSpace,
  NSpin,
  NStatistic,
  NSelect,
  NText,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useReportsStore } from '../../stores/reports'
import type { ExpenseCategoryRow } from '../../stores/reports'
import { localDateStr } from '../../utils/date'

const { t } = useI18n()
const store = useReportsStore()

// ─── Filters ─────────────────────────────────────────────────────────────────

const today = new Date()
const currentYear = today.getFullYear()

// Default: current year from Jan 1 to today
const dateRange = ref<[number, number]>([
  new Date(currentYear, 0, 1).getTime(),
  today.getTime(),
])
const plInclusion = ref<string>('all')

// ─── Fetch ────────────────────────────────────────────────────────────────────

async function load() {
  if (!dateRange.value) return
  const [fromTs, toTs] = dateRange.value
  const from = localDateStr(new Date(fromTs))
  const to = localDateStr(new Date(toTs))
  const include = plInclusion.value === 'all' ? null : plInclusion.value === 'included'
  await store.fetchExpenseReport(from, to, include)
}

const inclusionOptions = computed(() => [
  { label: t('expenses.plInclusionAll'), value: 'all' },
  { label: t('expenses.plInclusionIncluded'), value: 'included' },
  { label: t('expenses.plInclusionExcluded'), value: 'excluded' },
])

onMounted(load)

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtMoney(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'string' ? parseFloat(v) : v
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtPct(v: number): string {
  return v.toFixed(2)
}

// ─── Share calculation ────────────────────────────────────────────────────────

const totalNetNum = computed(() => {
  if (!store.expenseReport) return 0
  return parseFloat(store.expenseReport.total_net)
})

function sharePct(rowNet: string): string {
  const net = parseFloat(rowNet)
  if (totalNetNum.value === 0) return '0.00'
  return fmtPct((net / totalNetNum.value) * 100)
}

// ─── Summary KPI ──────────────────────────────────────────────────────────────

const summaryNet = computed(() =>
  store.expenseReport ? fmtMoney(store.expenseReport.total_net) : '—',
)
const summaryVat = computed(() =>
  store.expenseReport ? fmtMoney(store.expenseReport.total_vat) : '—',
)
const summaryGross = computed(() =>
  store.expenseReport ? fmtMoney(store.expenseReport.total_gross) : '—',
)
const summaryDeductible = computed(() =>
  store.expenseReport ? fmtMoney(store.expenseReport.total_deductible_net) : '—',
)
const summaryNonDeductible = computed(() =>
  store.expenseReport ? fmtMoney(store.expenseReport.total_non_deductible_net) : '—',
)

// ─── Table columns ────────────────────────────────────────────────────────────

const tableColumns = computed<DataTableColumns<ExpenseCategoryRow>>(() => [
  {
    title: t('reports.expenses.categoryName'),
    key: 'category_name',
    ellipsis: { tooltip: true },
    render(row) {
      return row.category_name || t('reports.expenses.uncategorised')
    },
  },
  {
    title: t('reports.expenses.net'),
    key: 'net',
    align: 'right',
    render(row) {
      return fmtMoney(row.net)
    },
  },
  {
    title: t('reports.expenses.vat'),
    key: 'vat',
    align: 'right',
    render(row) {
      return fmtMoney(row.vat)
    },
  },
  {
    title: t('reports.expenses.gross'),
    key: 'gross',
    align: 'right',
    render(row) {
      return fmtMoney(row.gross)
    },
  },
  {
    title: t('reports.expenses.deductibleNet'),
    key: 'deductible_net',
    align: 'right',
    render(row) {
      return fmtMoney(row.deductible_net)
    },
  },
  {
    title: t('reports.expenses.nonDeductibleNet'),
    key: 'non_deductible_net',
    align: 'right',
    render(row) {
      return fmtMoney(row.non_deductible_net)
    },
  },
  {
    title: t('reports.expenses.sharePct'),
    key: 'share',
    align: 'right',
    render(row) {
      const pct = sharePct(row.net)
      return h(NText, { type: 'info' }, () => `${pct}%`)
    },
  },
])

const hasRows = computed(
  () => store.expenseReport && store.expenseReport.by_category.length > 0,
)
</script>

<template>
  <div class="expense-report-page">
    <div class="report-container">
          <h2>{{ t('reports.expenses.title') }}</h2>

          <!-- Filters -->
          <n-space align="center" :wrap="true" class="filters-row">
            <n-select v-model:value="plInclusion" :options="inclusionOptions" style="width: 210px" @update:value="load" />
            <n-date-picker
              v-model:value="dateRange"
              type="daterange"
              :start-placeholder="t('reports.expenses.from')"
              :end-placeholder="t('reports.expenses.to')"
              clearable
              @update:value="load"
            />
            <n-button :loading="store.expenseLoading" @click="load">
              {{ t('reports.expenses.refresh') }}
            </n-button>
          </n-space>

          <!-- Error -->
          <n-alert
            v-if="store.expenseError"
            type="error"
            :title="t('common.error')"
            class="mt-4"
          >
            {{ store.expenseError }}
          </n-alert>

          <!-- Loading -->
          <n-spin v-if="store.expenseLoading" size="large" class="spinner" />

          <!-- Summary KPI cards -->
          <n-grid
            v-if="store.expenseReport && !store.expenseLoading"
            :cols="5"
            :x-gap="12"
            :y-gap="12"
            class="summary-grid"
          >
            <n-grid-item>
              <n-card size="small">
                <n-statistic :label="t('reports.expenses.totalNet')" :value="summaryNet" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card size="small">
                <n-statistic :label="t('reports.expenses.totalVat')" :value="summaryVat" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card size="small">
                <n-statistic :label="t('reports.expenses.totalGross')" :value="summaryGross" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card size="small">
                <n-statistic
                  :label="t('reports.expenses.totalDeductibleNet')"
                  :value="summaryDeductible"
                />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card size="small">
                <n-statistic
                  :label="t('reports.expenses.totalNonDeductibleNet')"
                  :value="summaryNonDeductible"
                />
              </n-card>
            </n-grid-item>
          </n-grid>

          <!-- Category breakdown table -->
          <n-card
            v-if="store.expenseReport && !store.expenseLoading"
            class="table-card"
          >
            <n-empty
              v-if="!hasRows"
              :description="t('reports.expenses.empty')"
              class="empty-state"
            />
            <n-data-table
              v-else
              :columns="tableColumns"
              :data="store.expenseReport.by_category"
              size="small"
            />
          </n-card>
    </div>
  </div>
</template>

<style scoped>

.report-container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px 16px;
}

.filters-row {
  margin-bottom: 16px;
}

.mt-4 {
  margin-top: 16px;
}

.spinner {
  display: flex;
  justify-content: center;
  margin: 40px 0;
}

.summary-grid {
  margin-bottom: 20px;
}

.table-card {
  margin-top: 8px;
}

.empty-state {
  padding: 32px 0;
}
</style>
