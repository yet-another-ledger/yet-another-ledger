<script setup lang="ts">
/**
 * Dashboard view – M10 step 5.
 *
 * Displays:
 * - Year selector
 * - KPI cards (YTD revenue / expense / profit, current-quarter VAT payable)
 * - ECharts monthly bar chart (revenue / expenses / profit)
 * - Top 5 expense categories table
 *
 * All data comes from GET /api/v1/reports/dashboard?year=
 * via useReportsStore().fetchDashboard / dashboardReport.
 *
 * ECharts is imported on-demand (tree-shaking) to keep the bundle lean.
 */
import { use } from 'echarts/core'
import { BarChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { computed, h, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NGi,
  NGrid,
  NInputNumber,
  NSpace,
  NSpin,
  NStatistic,
  NText,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useReportsStore } from '../stores/reports'
import type { DashboardMonthly, DashboardTopCategory } from '../stores/reports'

// Register only the ECharts components we need (tree-shaking).
use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const { t } = useI18n()
const store = useReportsStore()

// ─── Year picker ─────────────────────────────────────────────────────────────

const today = new Date()
const selectedYear = ref<number>(today.getFullYear())

// ─── Fetch ────────────────────────────────────────────────────────────────────

async function load() {
  await store.fetchDashboard(selectedYear.value)
}

onMounted(load)

// ─── Money formatter ──────────────────────────────────────────────────────────

function fmtMoney(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'string' ? parseFloat(v) : v
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// ─── KPI computed ─────────────────────────────────────────────────────────────

const kpiRevenue = computed(() =>
  store.dashboardReport ? fmtMoney(store.dashboardReport.kpi.ytd_revenue) : '—',
)
const kpiExpense = computed(() =>
  store.dashboardReport ? fmtMoney(store.dashboardReport.kpi.ytd_expense) : '—',
)
const kpiProfit = computed(() =>
  store.dashboardReport ? fmtMoney(store.dashboardReport.kpi.ytd_profit) : '—',
)
const kpiVat = computed(() =>
  store.dashboardReport
    ? fmtMoney(store.dashboardReport.kpi.current_quarter_vat_payable)
    : '—',
)
const currentQuarter = computed(() => store.dashboardReport?.quarter ?? '?')
const vatIncomplete = computed(() =>
  store.dashboardReport?.current_quarter_vat_status.is_complete === false,
)

const profitPositive = computed(() => {
  if (!store.dashboardReport) return true
  return parseFloat(store.dashboardReport.kpi.ytd_profit) >= 0
})
const vatPositive = computed(() => {
  if (!store.dashboardReport) return true
  return parseFloat(store.dashboardReport.kpi.current_quarter_vat_payable) >= 0
})

// ─── ECharts option ───────────────────────────────────────────────────────────

const chartOption = computed(() => {
  const report = store.dashboardReport
  if (!report) return {}

  const monthly: DashboardMonthly[] = report.monthly
  const months = monthly.map((m) => {
    // Convert "2026-03-01" → "Mar" style label
    const d = new Date(m.month)
    return d.toLocaleString(undefined, { month: 'short' })
  })

  const revenue = monthly.map((m) => parseFloat(m.revenue))
  const expense = monthly.map((m) => parseFloat(m.expense))
  const profit = monthly.map((m) => parseFloat(m.profit))

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: Array<{ seriesName: string; value: number; marker: string }>) => {
        const lines = params
          .map((p) => `${p.marker} ${p.seriesName}: ${fmtMoney(p.value)}`)
          .join('<br/>')
        return lines
      },
    },
    legend: {
      data: [
        t('dashboard.chartLegendRevenue'),
        t('dashboard.chartLegendExpense'),
        t('dashboard.chartLegendProfit'),
      ],
    },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'category',
      data: months,
    },
    yAxis: {
      type: 'value',
    },
    series: [
      {
        name: t('dashboard.chartLegendRevenue'),
        type: 'bar',
        data: revenue,
        itemStyle: { color: '#18a058' },
      },
      {
        name: t('dashboard.chartLegendExpense'),
        type: 'bar',
        data: expense,
        itemStyle: { color: '#f0a020' },
      },
      {
        name: t('dashboard.chartLegendProfit'),
        type: 'bar',
        data: profit,
        itemStyle: { color: '#2080f0' },
      },
    ],
  }
})

// ─── Top categories table ─────────────────────────────────────────────────────

const topCategoryColumns = computed<DataTableColumns<DashboardTopCategory>>(() => [
  {
    title: t('dashboard.categoryName'),
    key: 'category_name',
    ellipsis: { tooltip: true },
  },
  {
    title: t('dashboard.categoryNet'),
    key: 'net',
    align: 'right',
    render(row) {
      return h(NText, null, () => fmtMoney(row.net))
    },
  },
])
</script>

<template>
  <div class="dashboard-page">
    <div class="dashboard-container">
          <h2>{{ t('dashboard.title') }}</h2>

          <!-- Year selector + refresh -->
          <n-space align="center" :wrap="true" class="filters-row">
            <n-input-number
              v-model:value="selectedYear"
              :min="2000"
              :max="2100"
              :step="1"
              :show-button="true"
              style="width: 120px"
              @update:value="load"
            />
            <n-button :loading="store.dashboardLoading" @click="load">
              {{ t('dashboard.refresh') }}
            </n-button>
          </n-space>

          <!-- Error -->
          <n-alert
            v-if="store.dashboardError"
            type="error"
            :title="t('common.error')"
            class="mt-4"
          >
            {{ store.dashboardError }}
          </n-alert>

          <!-- Loading spinner -->
          <n-spin v-if="store.dashboardLoading" size="large" class="spinner" />

          <!-- Content: KPI + chart + top categories -->
          <template v-if="store.dashboardReport && !store.dashboardLoading">
            <!-- KPI cards -->
            <n-card :title="t('dashboard.kpiTitle')" class="section-card">
              <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
                <n-gi>
                  <n-statistic :label="t('dashboard.ytdRevenue')" :value="kpiRevenue" />
                </n-gi>
                <n-gi>
                  <n-statistic :label="t('dashboard.ytdExpense')" :value="kpiExpense" />
                </n-gi>
                <n-gi>
                  <n-statistic
                    :label="t('dashboard.ytdProfit')"
                    :value="kpiProfit"
                    :value-style="{
                      color: profitPositive ? 'var(--n-color-success)' : 'var(--n-color-error)',
                    }"
                  />
                </n-gi>
                <n-gi>
                  <n-statistic
                    :label="t(vatIncomplete ? 'dashboard.currentQuarterVatSubtotal' : 'dashboard.currentQuarterVat')"
                    :value="kpiVat"
                    :value-style="{
                      color: vatPositive ? 'var(--n-color-error)' : 'var(--n-color-success)',
                    }"
                  >
                    <template #suffix>
                      <span class="kpi-hint">
                        {{ t('dashboard.currentQuarterVatHint', { q: currentQuarter }) }}
                      </span>
                    </template>
                  </n-statistic>
                </n-gi>
              </n-grid>
              <n-alert v-if="vatIncomplete" type="warning" class="mt-4">
                {{ t('dashboard.vatIncomplete') }}
                <ul v-if="store.dashboardReport.current_quarter_vat_status.missing_inputs.length">
                  <li
                    v-for="missing in store.dashboardReport.current_quarter_vat_status.missing_inputs"
                    :key="`${missing.asset_id}-${missing.year}`"
                  >
                    <router-link :to="`/battery-assets/${missing.asset_id}?year=${missing.year}`">
                      {{ t('reports.vat.batteryMissing', { year: missing.year, code: t(`reports.vat.missingCodes.${missing.code}`) }) }}
                    </router-link>
                  </li>
                </ul>
              </n-alert>
            </n-card>

            <!-- Monthly bar chart -->
            <n-card :title="t('dashboard.monthlyTitle')" class="section-card">
              <v-chart
                class="chart"
                :option="chartOption"
                autoresize
              />
            </n-card>

            <!-- Top 5 expense categories -->
            <n-card :title="t('dashboard.topCategoriesTitle')" class="section-card">
              <n-data-table
                v-if="store.dashboardReport.top_expense_categories.length > 0"
                :columns="topCategoryColumns"
                :data="store.dashboardReport.top_expense_categories"
                size="small"
              />
              <n-text v-else depth="3">{{ t('dashboard.topCategoriesEmpty') }}</n-text>
            </n-card>
          </template>
    </div>
  </div>
</template>

<style scoped>
.dashboard-container {
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

.section-card {
  margin-top: 20px;
}

.chart {
  height: 360px;
  width: 100%;
}

.kpi-hint {
  font-size: 11px;
  opacity: 0.65;
  margin-left: 4px;
}
</style>
