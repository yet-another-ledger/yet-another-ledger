<script setup lang="ts">
/**
 * P/L (Profit & Loss) report view – M10 step 1.
 *
 * Features:
 * - Date range picker (from / to)
 * - Period presets: MTD / QTD / YTD (replaces granularity toggle)
 * - Summary KPI cards (revenue, expense, profit)
 * - Time-series table with all monthly buckets
 */
import { computed, h, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDatePicker,
  NGrid,
  NGridItem,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NSpin,
  NStatistic,
  NText,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useReportsStore } from '../../stores/reports'
import type { ProfitLossSeriesItem } from '../../stores/reports'
import { localDateStr } from '../../utils/date'

const { t } = useI18n()
const store = useReportsStore()

// ─── Filters ─────────────────────────────────────────────────────────────────

const today = new Date()
const currentYear = today.getFullYear()

// Default: current year from Jan 1 to today (YTD)
const dateRange = ref<[number, number] | null>([
  new Date(currentYear, 0, 1).getTime(),
  today.getTime(),
])

// ─── Period presets ───────────────────────────────────────────────────────────

/** Compute the first month of the current quarter: 0/3/6/9 */
function quarterStartMonth(): number {
  return Math.floor(today.getMonth() / 3) * 3
}

/** Return [fromTs, toTs] for each preset using local date construction. */
function presetRange(key: 'mtd' | 'qtd' | 'ytd'): [number, number] {
  const y = today.getFullYear()
  const m = today.getMonth()
  const todayTs = today.getTime()
  if (key === 'mtd') {
    return [new Date(y, m, 1).getTime(), todayTs]
  }
  if (key === 'qtd') {
    return [new Date(y, quarterStartMonth(), 1).getTime(), todayTs]
  }
  // ytd
  return [new Date(y, 0, 1).getTime(), todayTs]
}

/**
 * Derived computed: which preset does the current dateRange match?
 * Returns null when dateRange is null or doesn't match any preset.
 * Comparison is done on the YYYY-MM-DD string level to avoid timestamp noise.
 */
const activePreset = computed<'mtd' | 'qtd' | 'ytd' | null>(() => {
  if (!dateRange.value) return null
  const [fromTs, toTs] = dateRange.value
  const fromStr = localDateStr(new Date(fromTs))
  const toStr = localDateStr(new Date(toTs))
  const todayStr = localDateStr(today)

  // To must always equal today for any preset
  if (toStr !== todayStr) return null

  for (const key of ['mtd', 'qtd', 'ytd'] as const) {
    const [pFrom] = presetRange(key)
    if (fromStr === localDateStr(new Date(pFrom))) return key
  }
  return null
})

/** Apply a period preset: set dateRange and reload. */
function applyPreset(key: string) {
  const range = presetRange(key as 'mtd' | 'qtd' | 'ytd')
  dateRange.value = range
  load()
}

// ─── Fetch ────────────────────────────────────────────────────────────────────

async function load() {
  if (!dateRange.value) return
  const [fromTs, toTs] = dateRange.value
  const from = localDateStr(new Date(fromTs))
  const to = localDateStr(new Date(toTs))
  await store.fetchProfitLoss(from, to, 'month')
}

onMounted(load)

// ─── Summary cards ───────────────────────────────────────────────────────────

function fmtMoney(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'string' ? parseFloat(v) : v
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const summaryRevenue = computed(() =>
  store.plReport ? fmtMoney(store.plReport.revenue_net) : '—',
)
const summaryExpense = computed(() =>
  store.plReport ? fmtMoney(store.plReport.expense_actual) : '—',
)
const summaryMileageExpense = computed(() =>
  store.plReport ? fmtMoney(store.plReport.mileage_expense_actual) : '—',
)
const summaryOtherExpense = computed(() =>
  store.plReport ? fmtMoney(store.plReport.other_expense_actual) : '—',
)
const summaryProfitBeforeMileage = computed(() =>
  store.plReport ? fmtMoney(store.plReport.profit_before_mileage) : '—',
)
const summaryProfitAfterMileage = computed(() =>
  store.plReport ? fmtMoney(store.plReport.profit_after_mileage) : '—',
)

const profitPositive = computed(() => {
  if (!store.plReport) return true
  return parseFloat(store.plReport.profit_after_mileage) >= 0
})

// ─── Series table ─────────────────────────────────────────────────────────────

const tableColumns = computed<DataTableColumns<ProfitLossSeriesItem>>(() => [
  {
    title: t('reports.pl.period'),
    key: 'period',
    width: 120,
    render(row) {
      // Always monthly: YYYY-MM
      const d = new Date(row.period)
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    },
  },
  {
    title: t('reports.pl.revenueNet'),
    key: 'revenue_net',
    align: 'right',
    render(row) {
      return fmtMoney(row.revenue_net)
    },
  },
  {
    title: t('reports.pl.otherExpenseActual'),
    key: 'other_expense_actual',
    align: 'right',
    render(row) {
      return fmtMoney(row.other_expense_actual)
    },
  },
  {
    title: t('reports.pl.mileageExpenseActual'),
    key: 'mileage_expense_actual',
    align: 'right',
    render(row) {
      return fmtMoney(row.mileage_expense_actual)
    },
  },
  {
    title: t('reports.pl.expenseActual'),
    key: 'expense_actual',
    align: 'right',
    render(row) {
      return fmtMoney(row.expense_actual)
    },
  },
  {
    title: t('reports.pl.profitBeforeMileage'),
    key: 'profit_before_mileage',
    align: 'right',
    render(row) {
      return fmtMoney(row.profit_before_mileage)
    },
  },
  {
    title: t('reports.pl.profitAfterMileage'),
    key: 'profit_after_mileage',
    align: 'right',
    render(row) {
      const val = parseFloat(row.profit_after_mileage)
      return h(NText, { type: val >= 0 ? 'success' : 'error' }, () => fmtMoney(row.profit_after_mileage))
    },
  },
])

</script>

<template>
  <div class="pl-report-page">
    <div class="report-container">
          <h2>{{ t('reports.pl.title') }}</h2>

          <!-- Filters -->
          <n-space align="center" :wrap="true" class="filters-row">
            <n-date-picker
              v-model:value="dateRange"
              type="daterange"
              :start-placeholder="t('reports.pl.from')"
              :end-placeholder="t('reports.pl.to')"
              clearable
              @update:value="load"
            />
            <!-- Period presets: MTD / QTD / YTD -->
            <n-radio-group :value="activePreset" @update:value="applyPreset">
              <n-radio-button value="mtd">{{ t('reports.pl.periodMtd') }}</n-radio-button>
              <n-radio-button value="qtd">{{ t('reports.pl.periodQtd') }}</n-radio-button>
              <n-radio-button value="ytd">{{ t('reports.pl.periodYtd') }}</n-radio-button>
            </n-radio-group>
            <n-button :loading="store.plLoading" @click="load">
              {{ t('reports.pl.refresh') }}
            </n-button>
          </n-space>

          <!-- Error -->
          <n-alert v-if="store.plError" type="error" :title="t('common.error')" class="mt-4">
            {{ store.plError }}
          </n-alert>

          <!-- Loading -->
          <n-spin v-if="store.plLoading" :size="'large'" class="spinner" />

          <!-- Summary cards -->
          <n-grid
            v-if="store.plReport && !store.plLoading"
            :cols="3"
            :x-gap="16"
            :y-gap="16"
            class="summary-grid"
          >
            <n-grid-item>
              <n-card>
                <n-statistic :label="t('reports.pl.revenueNet')" :value="summaryRevenue" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic :label="t('reports.pl.expenseActual')" :value="summaryExpense" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic :label="t('reports.pl.mileageExpenseActual')" :value="summaryMileageExpense" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic :label="t('reports.pl.otherExpenseActual')" :value="summaryOtherExpense" />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic
                  :label="t('reports.pl.profitBeforeMileage')"
                  :value="summaryProfitBeforeMileage"
                />
              </n-card>
            </n-grid-item>
            <n-grid-item>
              <n-card>
                <n-statistic
                  :label="t('reports.pl.profitAfterMileage')"
                  :value="summaryProfitAfterMileage"
                  :value-style="{ color: profitPositive ? 'var(--n-color-success)' : 'var(--n-color-error)' }"
                />
              </n-card>
            </n-grid-item>
          </n-grid>

          <!-- Time-series table -->
          <n-card
            v-if="store.plReport && !store.plLoading"
            :title="t('reports.pl.seriesTitle')"
            class="series-card"
          >
            <n-data-table
              :columns="tableColumns"
              :data="store.plReport.series"
              size="small"
            />
          </n-card>
          <n-card
            v-if="store.plReport && store.plReport.external_sales?.length"
            :title="t('reports.pl.externalSales')"
            class="series-card"
          >
            <ul>
              <li v-for="sale in (store.plReport.external_sales ?? [])" :key="sale.external_sale_id">
                <router-link :to="`/external-sales/${sale.external_sale_id}`">
                  {{ sale.source_name }} · {{ sale.original_number }} · {{ sale.event_date }}
                </router-link>
              </li>
            </ul>
          </n-card>
    </div>
  </div>
</template>

<style scoped>
.report-container {
  max-width: 960px;
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

.series-card {
  margin-top: 8px;
}
</style>
