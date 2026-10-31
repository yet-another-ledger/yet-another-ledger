<script setup lang="ts">
/**
 * BTW VAT Return report view – M10 step 2.
 *
 * Features:
 * - Year + quarter selector
 * - NL-only banner (D-COUNTRY)
 * - Warnings display (missing VAT-ID on ICP customers, non-NL fallback)
 * - BTW return boxes table (1a/1b/1c/1d/1e/2a/3a/3b/3c/4a/4b/5b)
 * - Totals (output VAT total, net payable/refundable)
 * - Disclaimer (D-DISCLAIMER)
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NGrid,
  NGridItem,
  NSelect,
  NSpin,
  NStatistic,
  NText,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useReportsStore } from '../../stores/reports'

const { t } = useI18n()
const store = useReportsStore()

// ─── Selectors ───────────────────────────────────────────────────────────────

const currentYear = new Date().getFullYear()
const selectedYear = ref<number>(currentYear)
const selectedQuarter = ref<number>(Math.ceil((new Date().getMonth() + 1) / 3))

const yearOptions = computed(() => {
  const years = []
  for (let y = currentYear; y >= currentYear - 5; y--) {
    years.push({ label: String(y), value: y })
  }
  return years
})

const quarterOptions = computed(() => [
  { label: t('reports.vat.q1'), value: 1 },
  { label: t('reports.vat.q2'), value: 2 },
  { label: t('reports.vat.q3'), value: 3 },
  { label: t('reports.vat.q4'), value: 4 },
])

// ─── Fetch ────────────────────────────────────────────────────────────────────

async function load() {
  await store.fetchVatReturn(selectedYear.value, selectedQuarter.value)
}

onMounted(load)

// ─── Data helpers ─────────────────────────────────────────────────────────────

function fmtMoney(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'string' ? parseFloat(v) : v
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// ─── Box rows ─────────────────────────────────────────────────────────────────

interface BoxRow {
  key: string
  label: string
  base: string | null
  vat: string | null
  isTotal?: boolean
}

const boxRows = computed<BoxRow[]>(() => {
  const r = store.vatReport
  if (!r) return []
  const b = r.boxes
  const rows: BoxRow[] = [
    {
      key: '1a',
      label: t('reports.vat.box1a'),
      base: fmtMoney(b.box_1a.base),
      vat: fmtMoney(b.box_1a.vat),
    },
    {
      key: '1b',
      label: t('reports.vat.box1b'),
      base: fmtMoney(b.box_1b.base),
      vat: fmtMoney(b.box_1b.vat),
    },
    {
      key: '1c',
      label: t('reports.vat.box1c'),
      base: fmtMoney(b.box_1c.base),
      vat: fmtMoney(b.box_1c.vat),
    },
    {
      key: '1d',
      label: t('reports.vat.box1d'),
      base: b.box_1d.base === null ? t('reports.vat.baseUnknown') : fmtMoney(b.box_1d.base),
      vat: fmtMoney(b.box_1d.vat),
    },
    {
      key: '1e',
      label: t('reports.vat.box1e'),
      base: fmtMoney(b.box_1e.base),
      vat: null,
    },
    {
      key: '2a',
      label: t('reports.vat.box2a'),
      base: fmtMoney(b.box_2a.base),
      vat: fmtMoney(b.box_2a.vat),
    },
    {
      key: '3a',
      label: t('reports.vat.box3a'),
      base: fmtMoney(b.box_3a.base),
      vat: null,
    },
    {
      key: '3b',
      label: t('reports.vat.box3b'),
      base: fmtMoney(b.box_3b.base),
      vat: null,
    },
    {
      key: '3c',
      label: t('reports.vat.box3c'),
      base: fmtMoney(b.box_3c.base),
      vat: null,
    },
    {
      key: '4a',
      label: t('reports.vat.box4a'),
      base: fmtMoney(b.box_4a.base),
      vat: fmtMoney(b.box_4a.vat),
    },
    {
      key: '4b',
      label: t('reports.vat.box4b'),
      base: fmtMoney(b.box_4b.base),
      vat: fmtMoney(b.box_4b.vat),
    },
    {
      key: '5b',
      label: t('reports.vat.box5b'),
      base: null,
      vat: fmtMoney(b.box_5b.vat),
    },
  ]
  return rows
})

const boxColumns: DataTableColumns<BoxRow> = [
  {
    title: t('reports.vat.boxes'),
    key: 'label',
    width: 400,
  },
  {
    title: t('reports.vat.base'),
    key: 'base',
    align: 'right',
    render: (row: BoxRow) => (row.base !== null ? row.base : '—'),
  },
  {
    title: t('reports.vat.vat'),
    key: 'vat',
    align: 'right',
    render: (row: BoxRow) => (row.vat !== null ? row.vat : '—'),
  },
]

// ─── Totals ───────────────────────────────────────────────────────────────────

const outputVatTotal = computed<string>(() => {
  const r = store.vatReport
  return r ? fmtMoney(r.totals.output_vat_total.vat) : '—'
})

const netPayable = computed<string>(() => {
  const r = store.vatReport
  return r ? fmtMoney(r.totals.net_payable_or_refundable.vat) : '—'
})

const netPayablePositive = computed<boolean>(() => {
  const r = store.vatReport
  if (!r) return false
  return parseFloat(String(r.totals.net_payable_or_refundable.vat)) >= 0
})

// ─── Period display ───────────────────────────────────────────────────────────

const periodLabel = computed<string>(() => {
  const r = store.vatReport
  if (!r) return ''
  return `${r.year} Q${r.quarter} (${r.from} – ${r.to})`
})
</script>

<template>
  <div class="vat-return-page page-content">
      <!-- Page header -->
      <div class="page-header">
        <h1 class="page-title">{{ t('reports.vat.title') }}</h1>
      </div>

      <!-- NL-only banner (D-COUNTRY) -->
      <n-alert type="info" :show-icon="true" class="nl-banner" style="margin-bottom: 16px">
        {{ t('reports.vat.bannerNlOnly') }}
      </n-alert>

      <!-- Selectors -->
      <n-card style="margin-bottom: 16px">
        <n-grid :cols="4" :x-gap="12" :y-gap="12">
          <n-grid-item>
            <div class="field-label">{{ t('reports.vat.year') }}</div>
            <n-select
              v-model:value="selectedYear"
              :options="yearOptions"
              style="width: 120px"
            />
          </n-grid-item>
          <n-grid-item>
            <div class="field-label">{{ t('reports.vat.quarter') }}</div>
            <n-select
              v-model:value="selectedQuarter"
              :options="quarterOptions"
              style="width: 180px"
            />
          </n-grid-item>
          <n-grid-item :span="2" style="display: flex; align-items: flex-end">
            <n-button type="primary" :loading="store.vatLoading" @click="load">
              {{ t('reports.vat.refresh') }}
            </n-button>
          </n-grid-item>
        </n-grid>
      </n-card>

      <!-- Loading / Error -->
      <div v-if="store.vatLoading" style="text-align: center; padding: 48px">
        <n-spin size="large" />
      </div>

      <n-alert v-else-if="store.vatError" type="error" :show-icon="true" style="margin-bottom: 16px">
        {{ store.vatError }}
      </n-alert>

      <template v-else-if="store.vatReport">
        <!-- Period label -->
        <div style="margin-bottom: 8px; color: #666; font-size: 13px">
          {{ t('reports.vat.period') }}: <strong>{{ periodLabel }}</strong>
        </div>

        <!-- Q4 note -->
        <n-alert
          v-if="store.vatReport.is_last_period_of_year"
          type="info"
          :show-icon="true"
          style="margin-bottom: 12px"
        >
          {{ t('reports.vat.q4Note') }}
        </n-alert>

        <!-- Warnings -->
        <n-alert
          v-if="(store.vatReport.warnings ?? []).length > 0"
          type="warning"
          :show-icon="true"
          style="margin-bottom: 16px"
        >
          <div style="font-weight: 600; margin-bottom: 4px">{{ t('reports.vat.warnings') }}</div>
          <ul style="margin: 0; padding-left: 20px">
            <li v-for="(w, i) in (store.vatReport.warnings ?? [])" :key="i">{{ w }}</li>
          </ul>
        </n-alert>

        <!-- Informational audit notices -->
        <n-alert
          v-if="(store.vatReport.infos ?? []).length > 0"
          type="info"
          :show-icon="true"
          style="margin-bottom: 16px"
        >
          <div style="font-weight: 600; margin-bottom: 4px">{{ t('reports.vat.infos') }}</div>
          <ul style="margin: 0; padding-left: 20px">
            <li v-for="(info, i) in (store.vatReport.infos ?? [])" :key="i">{{ info }}</li>
          </ul>
        </n-alert>

        <!-- Boxes table -->
        <n-card :title="t('reports.vat.boxes')" style="margin-bottom: 16px">
          <n-data-table
            :columns="boxColumns"
            :data="boxRows"
            :row-key="(row: BoxRow) => row.key"
            :pagination="false"
            size="small"
          />
        </n-card>

        <n-alert v-if="store.vatReport.is_last_period_of_year" :type="store.vatReport.boxes.box_1d.is_complete ? 'info' : 'warning'" style="margin-bottom: 16px">
          <div>{{ t('reports.vat.batterySubtotal', { base: fmtMoney(store.vatReport.boxes.box_1d.known_battery_base), vat: fmtMoney(store.vatReport.boxes.box_1d.known_battery_vat) }) }}</div>
          <ul v-if="(store.vatReport.boxes.box_1d.missing_inputs ?? []).length">
            <li v-for="missing in (store.vatReport.boxes.box_1d.missing_inputs ?? [])" :key="`${missing.asset_id}-${missing.year}`">
              <router-link :to="`/battery-assets/${missing.asset_id}?year=${missing.year}`">
                {{ t('reports.vat.batteryMissing', { year: missing.year, code: t(`reports.vat.missingCodes.${missing.code}`) }) }}
              </router-link>
            </li>
          </ul>
        </n-alert>

        <n-card :title="t('reports.vat.eventSources')" style="margin-bottom: 16px">
          <ul>
            <li
              v-for="(event, index) in (store.vatReport.event_rows ?? []).filter(row => row.source_type === 'EXTERNAL_SALE')"
              :key="`${event.external_sale_id}-${index}`"
            >
              <router-link :to="`/external-sales/${event.external_sale_id}`">
                {{ t('reports.vat.externalSale') }} · {{ event.external_source_name }} ·
                {{ event.external_original_number }} · {{ event.event_date }}
              </router-link>
            </li>
          </ul>
          <n-text v-if="!(store.vatReport.event_rows ?? []).some(row => row.source_type === 'EXTERNAL_SALE')" depth="3">
            —
          </n-text>
        </n-card>

        <!-- Totals -->
        <n-card :title="t('reports.vat.totals')" style="margin-bottom: 16px">
          <n-grid :cols="2" :x-gap="24" :y-gap="16">
            <n-grid-item>
              <n-statistic :label="t('reports.vat.outputVatTotal')">
                <n-text>{{ outputVatTotal }}</n-text>
              </n-statistic>
            </n-grid-item>
            <n-grid-item>
              <n-statistic :label="t('reports.vat.netPayable')">
                <n-text :type="netPayablePositive ? 'error' : 'success'">
                  {{ netPayable }}
                </n-text>
              </n-statistic>
            </n-grid-item>
          </n-grid>
        </n-card>

        <!-- Disclaimer (D-DISCLAIMER) -->
        <n-alert type="default" :show-icon="false" style="margin-bottom: 16px">
          <div style="font-weight: 600; margin-bottom: 4px">{{ t('reports.vat.disclaimer') }}</div>
          <n-text depth="3" style="font-size: 12px">
            {{ store.vatReport.disclaimer }}
          </n-text>
        </n-alert>
      </template>
  </div>
</template>

<style scoped>
.page-content {
  padding: 24px;
  max-width: 960px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 20px;
}

.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}

.field-label {
  font-size: 12px;
  color: #666;
  margin-bottom: 4px;
}

.nl-banner {
  border-left: 4px solid #2080f0;
}
</style>
