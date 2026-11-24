<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { h } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NDataTable, NText, NSpace, NInputNumber, NInput, NList, NListItem } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useRouter } from 'vue-router'
import { useRoute } from 'vue-router'
import { get, post, put, ApiError } from '../api/http'
import type { components } from '../api/schema'
import { localDateTimestamp } from '../utils/date'

type Asset = components['schemas']['FixedAssetRead']
const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const MIN_YEAR = 1
const MAX_YEAR = 9999
const assets = ref<Asset[]>([])
const loading = ref(false)
const error = ref('')
const initialYear = new Date().getFullYear()
const year = ref(initialYear)
const selectedAsset = ref<Asset | null>(null)
const selectedAssetId = ref<string | null>(typeof route.query.asset === 'string' ? route.query.asset : null)
const annualUse = ref<components['schemas']['FixedAssetAnnualUseRead'] | null>(null)
const confirmationPreview = ref<components['schemas']['FixedAssetAnnualConfirmPreview'] | null>(null)
const annualPercentage = ref<number | null>(null)
const confirmationReason = ref('')
const correctionReason = ref('')
const revisionPreview = ref<components['schemas']['FixedAssetAnnualRevisionPreview'] | null>(null)
const revisionHistory = ref<components['schemas']['FixedAssetAnnualRevisionRead'][]>([])
const annualError = ref('')
const annualBusy = ref(false)
const serviceDateValue = ref<string | null>(null)
const serviceDateReason = ref('')
const serviceDatePreview = ref<components['schemas']['FixedAssetServiceDateRevisionPreview'] | null>(null)
const assetRevisionHistory = ref<components['schemas']['FixedAssetRevisionRead'][]>([])
const linkedYear = typeof route.query.year === 'string' ? Number(route.query.year) : NaN
if (selectedAssetId.value && Number.isInteger(linkedYear) && linkedYear >= MIN_YEAR && linkedYear <= MAX_YEAR) {
  year.value = linkedYear
}
let requestVersion = 0
const validYear = computed(() => Number.isInteger(year.value) && year.value >= MIN_YEAR && year.value <= MAX_YEAR)
function monthlyAuditRows(snapshot: Record<string, unknown>) {
  const rows = snapshot.monthly_period_deltas
  return Array.isArray(rows) ? rows.filter((item): item is Record<string, unknown> =>
    typeof item === 'object' && item !== null && !Array.isArray(item),
  ) : []
}
function annualAuditPercentage(snapshot: Record<string, unknown>) {
  return snapshot.effective_business_percentage ?? snapshot.actual_business_percentage ?? snapshot.estimated_business_percentage
}
function annualAuditRows(snapshot: Record<string, unknown>) {
  const rows = snapshot.period_deltas
  return Array.isArray(rows) ? rows.filter((item): item is Record<string, unknown> =>
    typeof item === 'object' && item !== null && !Array.isArray(item),
  ) : []
}
function yearly(row: Asset) { return row.yearly_deltas.find(item => item.year === year.value) }
const columns = computed<DataTableColumns<Asset>>(() => {
  void year.value
  return [
  { title: t('fixedAssets.assetName'), key: 'name', width: 153, ellipsis: { tooltip: true } },
  { title: t('fixedAssets.acquisitionCost'), key: 'acquisition_cost', width: 130, render: row => `${row.acquisition_cost} / ${row.residual_value}` },
  { title: t('fixedAssets.inServiceDate'), key: 'in_service_date', width: 150, render: row => row.pending_in_service ? t('fixedAssets.pendingInService') : row.in_service_date },
  { title: t('fixedAssets.completedPnl'), key: 'completed', width: 130, render: row => `${yearly(row)?.fixed_asset_depreciation ?? '0.00'}${yearly(row)?.provisional ? ` · ${t('fixedAssets.provisionalUse')}` : ''}` },
  { title: t('fixedAssets.fullYearPlan'), key: 'planned', width: 110, render: row => yearly(row)?.planned_fixed_asset_depreciation ?? '0.00' },
  { title: t('fixedAssets.bookValue'), key: 'book', width: 210, render: row => {
    const item = yearly(row)
    return item
      ? [
          h('div', `${t('fixedAssets.bookValue')}: ${item.opening_book_value} → ${item.closing_book_value}`),
          h('div', `${t('fixedAssets.plannedBookValue')}: ${item.planned_opening_book_value} → ${item.planned_closing_book_value}`),
        ]
      : '—'
  } },
  { title: t('fixedAssets.sourcePurchase'), key: 'source', width: 132, render: row => h(NButton, { text: true, onClick: () => router.push(`/expenses/${row.acquisition_expense_id}/edit`) }, { default: () => t('fixedAssets.openPurchase') }) },
  { title: t('fixedAssets.details'), key: 'details', width: 85, render: row => h(NButton, { text: true, onClick: () => selectAsset(row) }, { default: () => t('fixedAssets.details') }) },
  ]
})
const detail = computed(() => selectedAsset.value ? yearly(selectedAsset.value) : undefined)
const annualUseStatus = computed(() => {
  if (selectedAsset.value?.use_type !== 'MIXED_USE') return null
  const percentage = annualUse.value?.effective_business_percentage ?? selectedAsset.value.business_percentage
  const status = annualUse.value?.provisional ?? true
    ? t('fixedAssets.provisionalStatus')
    : t('fixedAssets.confirmedStatus')
  return `${percentage}% · ${status}`
})
async function load() {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  if (!validYear.value) {
    assets.value = []
    selectedAsset.value = null
    error.value = t('fixedAssets.invalidYear')
    loading.value = false
    return
  }
  assets.value = []
  selectedAsset.value = null
  try {
    const result = await get<Asset[]>(`/api/v1/fixed-assets?year=${year.value}`)
    if (version === requestVersion) {
      assets.value = result
      selectedAsset.value = selectedAssetId.value
        ? result.find(row => row.id === selectedAssetId.value) ?? null
        : null
      if (selectedAsset.value) void loadAnnualUse(selectedAsset.value)
    }
  } catch (e) {
    if (version === requestVersion) {
      assets.value = []
      selectedAssetId.value = null
      selectedAsset.value = null
      error.value = e instanceof ApiError ? e.message : String(e)
    }
  } finally {
    if (version === requestVersion) loading.value = false
  }
}
function selectAsset(row: Asset) {
  selectedAssetId.value = row.id
  selectedAsset.value = row
  void loadAnnualUse(row)
}
async function loadAnnualUse(row: Asset) {
  const targetYear = year.value
  annualError.value = ''
  revisionPreview.value = null
  annualUse.value = null
  confirmationPreview.value = null
  revisionHistory.value = []
  assetRevisionHistory.value = []
  serviceDateValue.value = null
  serviceDateReason.value = ''
  serviceDatePreview.value = null
  annualPercentage.value = Number(row.business_percentage)
  try {
    const assetHistory = await get<components['schemas']['FixedAssetRevisionRead'][]>(
      `/api/v1/fixed-assets/${row.id}/revisions`,
    )
    if (selectedAsset.value?.id !== row.id || year.value !== targetYear) return
    assetRevisionHistory.value = assetHistory.filter(entry => Array.isArray(entry.affected_periods))
    if (row.use_type !== 'MIXED_USE') return
    const result = await get<components['schemas']['FixedAssetAnnualUseRead'] | null>(
      `/api/v1/fixed-assets/${row.id}/annual-use/${targetYear}`,
    )
    if (selectedAsset.value?.id !== row.id || year.value !== targetYear) return
    annualUse.value = result
    if (result) annualPercentage.value = Number(result.effective_business_percentage)
    const annualHistory = await get<components['schemas']['FixedAssetAnnualRevisionRead'][]>(
      `/api/v1/fixed-assets/${row.id}/annual-use/${targetYear}/revisions`,
    )
    if (selectedAsset.value?.id !== row.id || year.value !== targetYear) return
    revisionHistory.value = annualHistory
  } catch (e) {
    if (selectedAsset.value?.id === row.id && year.value === targetYear) {
      annualError.value = e instanceof Error ? e.message : String(e)
    }
  }
}
async function previewServiceDate() {
  if (!selectedAsset.value || !serviceDateValue.value || !serviceDateReason.value.trim()) return
  annualError.value = ''
  try {
    serviceDatePreview.value = await post<components['schemas']['FixedAssetServiceDateRevisionPreview']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/service-date/revisions/preview`,
      { expected_version: selectedAsset.value.version, in_service_date: serviceDateValue.value,
        reason: serviceDateReason.value, preview_token: '0'.repeat(64) },
    )
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
}
async function confirmServiceDate() {
  if (!selectedAsset.value || !serviceDateValue.value || !serviceDatePreview.value) return
  annualBusy.value = true
  try {
    const updated = await post<Asset>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/service-date/revisions`,
      { expected_version: selectedAsset.value.version, in_service_date: serviceDateValue.value,
        reason: serviceDateReason.value, preview_token: serviceDatePreview.value.preview_token },
    )
    selectedAsset.value = updated
    serviceDatePreview.value = null
    await loadAnnualUse(updated)
    await load()
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
  finally { annualBusy.value = false }
}
async function saveEstimate() {
  if (!selectedAsset.value || annualPercentage.value === null) return
  annualBusy.value = true
  annualError.value = ''
  try {
    annualUse.value = await put<components['schemas']['FixedAssetAnnualUseRead']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/annual-use/${year.value}`,
      { estimated_business_percentage: annualPercentage.value },
    )
    await load()
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
  finally { annualBusy.value = false }
}
async function confirmActual() {
  if (!selectedAsset.value || annualPercentage.value === null || !confirmationPreview.value) return
  annualBusy.value = true
  annualError.value = ''
  try {
    annualUse.value = await post<components['schemas']['FixedAssetAnnualUseRead']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/annual-use/${year.value}/confirm`,
      { actual_business_percentage: annualPercentage.value,
        preview_token: confirmationPreview.value.preview_token,
        reason: confirmationReason.value },
    )
    confirmationPreview.value = null
    confirmationReason.value = ''
    await load()
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
  finally { annualBusy.value = false }
}
async function previewActual() {
  if (!selectedAsset.value || annualPercentage.value === null) return
  annualBusy.value = true
  annualError.value = ''
  try {
    confirmationPreview.value = await post<components['schemas']['FixedAssetAnnualConfirmPreview']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/annual-use/${year.value}/confirm/preview`,
      { actual_business_percentage: annualPercentage.value },
    )
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
  finally { annualBusy.value = false }
}
async function previewCorrection() {
  if (!selectedAsset.value || !annualUse.value || annualPercentage.value === null || !correctionReason.value.trim()) return
  annualError.value = ''
  try {
    revisionPreview.value = await post<components['schemas']['FixedAssetAnnualRevisionPreview']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/annual-use/${year.value}/revisions/preview`,
      { expected_version: annualUse.value.version, business_percentage: annualPercentage.value,
        reason: correctionReason.value, preview_token: '0'.repeat(64) },
    )
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
}
async function confirmCorrection() {
  if (!selectedAsset.value || !annualUse.value || !revisionPreview.value || annualPercentage.value === null) return
  annualBusy.value = true
  annualError.value = ''
  try {
    annualUse.value = await post<components['schemas']['FixedAssetAnnualUseRead']>(
      `/api/v1/fixed-assets/${selectedAsset.value.id}/annual-use/${year.value}/revisions`,
      { expected_version: annualUse.value.version, business_percentage: annualPercentage.value,
        reason: correctionReason.value, preview_token: revisionPreview.value.preview_token },
    )
    revisionPreview.value = null
    correctionReason.value = ''
    await loadAnnualUse(selectedAsset.value)
    await load()
  } catch (e) { annualError.value = e instanceof Error ? e.message : String(e) }
  finally { annualBusy.value = false }
}
onMounted(() => void load())
watch(year, () => void load())
</script>

<template>
  <div class="fixed-assets-page">
    <n-card :title="t('fixedAssets.registerTitle')">
      <n-space vertical>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <n-space align="center">
          <n-text depth="3">{{ t('fixedAssets.registerYear', { year }) }}</n-text>
          <n-input-number v-model:value="year" :precision="0" :min="MIN_YEAR" :max="MAX_YEAR" />
        </n-space>
        <n-data-table
          :columns="columns"
          :data="assets"
          :loading="loading"
          :scroll-x="1100"
          :row-key="row => row.id"
        />
        <n-card v-if="selectedAsset" :title="selectedAsset.name" size="small">
          <n-space vertical>
            <n-text>{{ t('fixedAssets.sourcePurchase') }}: <a @click="router.push(`/expenses/${selectedAsset.acquisition_expense_id}/edit`)">{{ t('fixedAssets.openPurchase') }}</a></n-text>
            <n-text>{{ t('fixedAssets.acquisitionCost') }}: {{ selectedAsset.acquisition_cost }} · {{ t('fixedAssets.residualValue') }}: {{ selectedAsset.residual_value }}</n-text>
            <n-text>{{ t('fixedAssets.inServiceDate') }}: {{ selectedAsset.in_service_date ?? t('fixedAssets.pendingInService') }} · {{ t('fixedAssets.useStatus') }}: {{ selectedAsset.use_type === 'MIXED_USE' ? annualUseStatus : `${selectedAsset.business_percentage}%` }}</n-text>
            <n-text v-if="detail">{{ year }} · {{ t('fixedAssets.completedPnl') }}: {{ detail.fixed_asset_depreciation }} · {{ t('fixedAssets.fullYearPlan') }}: {{ detail.planned_fixed_asset_depreciation }} · {{ t('fixedAssets.cumulative') }}: {{ detail.cumulative_depreciation }} · {{ t('fixedAssets.bookValue') }}: {{ detail.opening_book_value }} → {{ detail.closing_book_value }} · {{ t('fixedAssets.plannedBookValue') }}: {{ detail.planned_opening_book_value }} → {{ detail.planned_closing_book_value }}</n-text>
            <n-alert v-if="selectedAsset.use_type === 'MIXED_USE'" :type="annualUse?.provisional === false ? 'success' : 'warning'">{{ t('fixedAssets.useStatus') }}: {{ annualUseStatus }} · {{ t('fixedAssets.realized') }}: {{ annualUse?.full_depreciation ?? '—' }} · {{ t('fixedAssets.privateUseReduction') }}: {{ annualUse?.private_use_reduction ?? '—' }} · {{ t('fixedAssets.netPnl') }}: {{ annualUse?.net_pnl_effect ?? '—' }}</n-alert>
            <n-alert v-if="annualError" type="error">{{ annualError }}</n-alert>
            <n-space v-if="selectedAsset.pending_in_service" align="center">
              <n-date-picker
                v-model:formatted-value="serviceDateValue"
                value-format="yyyy-MM-dd"
                type="date"
                :default-calendar-start-time="localDateTimestamp(selectedAsset.acquisition_date) ?? undefined"
              />
              <n-input v-model:value="serviceDateReason" :placeholder="t('fixedAssets.correctionReason')" />
              <n-button @click="previewServiceDate">{{ t('fixedAssets.previewActivation') }}</n-button>
              <n-button v-if="serviceDatePreview" :loading="annualBusy" @click="confirmServiceDate">{{ t('fixedAssets.confirmActivation') }}</n-button>
            </n-space>
            <n-text v-if="serviceDatePreview">{{ t('fixedAssets.beforeAfter') }}: — → {{ serviceDatePreview.after_in_service_date }} · Δ P&amp;L {{ serviceDatePreview.pnl_delta }} · {{ serviceDatePreview.affected_periods.join(', ') }}</n-text>
            <n-space v-if="selectedAsset.use_type === 'MIXED_USE'" align="center">
              <n-input-number v-model:value="annualPercentage" :min="0" :max="100" :precision="3" />
              <n-input v-model:value="confirmationReason" :placeholder="t('fixedAssets.confirmationReason')" />
              <n-button :loading="annualBusy" @click="saveEstimate">{{ t('fixedAssets.saveEstimate') }}</n-button>
              <n-button :disabled="!confirmationReason.trim()" :loading="annualBusy" @click="previewActual">{{ t('fixedAssets.previewConfirmation') }}</n-button>
              <n-button v-if="confirmationPreview" :disabled="!confirmationReason.trim()" :loading="annualBusy" @click="confirmActual">{{ t('fixedAssets.confirmActual') }}</n-button>
            </n-space>
            <div v-if="confirmationPreview">
              <n-text>{{ t('fixedAssets.beforeAfter') }}: {{ confirmationPreview.before_business_percentage }}% → {{ confirmationPreview.after_business_percentage }}% · {{ confirmationPreview.affected_periods.join(', ') }}</n-text>
              <n-list bordered>
                <n-list-item v-for="item in confirmationPreview.period_deltas" :key="item.period">{{ item.period }} · {{ item.before_business_depreciation }} → {{ item.after_business_depreciation }} · Δ {{ item.pnl_delta }}</n-list-item>
              </n-list>
              <n-list v-if="confirmationPreview.monthly_period_deltas?.length" bordered>
                <n-list-item v-for="item in confirmationPreview.monthly_period_deltas" :key="item.period">{{ item.period }} · {{ item.before_business_depreciation }} → {{ item.after_business_depreciation }} · Δ {{ item.pnl_delta }}</n-list-item>
              </n-list>
            </div>
            <n-button v-if="selectedAsset.battery_asset_id || selectedAsset.use_type === 'MIXED_USE'" @click="router.push(selectedAsset.battery_asset_id ? `/battery-assets/${selectedAsset.battery_asset_id}` : `/battery-assets/new?expense_id=${selectedAsset.acquisition_expense_id}`)">{{ t('fixedAssets.batteryVat') }}</n-button>
            <n-space v-if="selectedAsset.use_type === 'MIXED_USE' && annualUse && annualUse.actual_business_percentage !== null" align="center">
              <n-input v-model:value="correctionReason" :placeholder="t('fixedAssets.correctionReason')" />
              <n-button @click="previewCorrection">{{ t('fixedAssets.previewCorrection') }}</n-button>
              <n-button v-if="revisionPreview" :loading="annualBusy" @click="confirmCorrection">{{ t('fixedAssets.confirmCorrection') }}</n-button>
            </n-space>
            <n-text v-if="revisionPreview">{{ t('fixedAssets.beforeAfter') }}: {{ revisionPreview.before.effective_business_percentage }}% → {{ revisionPreview.after_business_percentage }}% · Δ P&amp;L {{ revisionPreview.pnl_delta }} · {{ revisionPreview.affected_periods.join(', ') }}</n-text>
            <n-list v-if="revisionPreview?.period_deltas?.length" bordered>
              <n-list-item v-for="item in revisionPreview.period_deltas" :key="item.period">{{ item.period }} · {{ item.before_business_depreciation }} → {{ item.after_business_depreciation }} · Δ {{ item.pnl_delta }}</n-list-item>
            </n-list>
            <n-list v-if="revisionPreview?.monthly_period_deltas?.length" bordered>
              <n-list-item v-for="item in revisionPreview.monthly_period_deltas" :key="item.period">{{ item.period }} · {{ item.before_business_depreciation }} → {{ item.after_business_depreciation }} · Δ {{ item.pnl_delta }}</n-list-item>
            </n-list>
            <n-list v-if="revisionHistory.length" bordered>
              <n-list-item v-for="entry in revisionHistory" :key="entry.version">
                v{{ entry.version }} · {{ entry.reason }} · {{ entry.actor_id ?? '—' }} · {{ entry.created_at }}
                <div v-if="annualAuditPercentage(entry.before_snapshot) != null || annualAuditPercentage(entry.after_snapshot) != null">
                  {{ t('fixedAssets.useStatus') }}: {{ annualAuditPercentage(entry.before_snapshot) ?? '—' }}% → {{ annualAuditPercentage(entry.after_snapshot) ?? '—' }}%
                </div>
                <ul v-if="annualAuditRows(entry.after_snapshot).length">
                  <li v-for="item in annualAuditRows(entry.after_snapshot)" :key="String(item.period)">
                    {{ item.period }} · {{ item.before_business_depreciation ?? '—' }} → {{ item.after_business_depreciation ?? '—' }} · Δ {{ item.pnl_delta ?? '—' }}
                  </li>
                </ul>
                <ul v-if="monthlyAuditRows(entry.after_snapshot).length">
                  <li v-for="item in monthlyAuditRows(entry.after_snapshot)" :key="String(item.period)">
                    {{ item.period }} · {{ item.before_business_depreciation ?? '—' }} → {{ item.after_business_depreciation ?? '—' }} · Δ {{ item.pnl_delta ?? '—' }}
                  </li>
                </ul>
              </n-list-item>
            </n-list>
            <n-list v-if="assetRevisionHistory.length" bordered>
              <n-list-item v-for="entry in assetRevisionHistory" :key="entry.version">
                {{ t('fixedAssets.assetRevision') }} v{{ entry.version }} · {{ entry.reason }} · {{ entry.affected_periods.join(', ') }} · {{ entry.created_at }}
                <ul v-if="monthlyAuditRows(entry.after_snapshot).length">
                  <li v-for="item in monthlyAuditRows(entry.after_snapshot)" :key="String(item.period)">
                    {{ item.period }} · {{ item.before_depreciation ?? '—' }} → {{ item.after_depreciation ?? '—' }} · Δ {{ item.pnl_delta ?? '—' }}
                  </li>
                </ul>
              </n-list-item>
            </n-list>
          </n-space>
        </n-card>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.fixed-assets-page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}
</style>
