<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { h } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NDataTable, NText, NSpace, NInputNumber } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useRouter } from 'vue-router'
import { useRoute } from 'vue-router'
import { get, ApiError } from '../api/http'
import type { components } from '../api/schema'

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
const linkedYear = typeof route.query.year === 'string' ? Number(route.query.year) : NaN
if (selectedAssetId.value && Number.isInteger(linkedYear) && linkedYear >= MIN_YEAR && linkedYear <= MAX_YEAR) {
  year.value = linkedYear
}
let requestVersion = 0
const validYear = computed(() => Number.isInteger(year.value) && year.value >= MIN_YEAR && year.value <= MAX_YEAR)
function yearly(row: Asset) { return row.yearly_deltas.find(item => item.year === year.value) }
const columns = computed<DataTableColumns<Asset>>(() => {
  void year.value
  return [
  { title: t('fixedAssets.assetName'), key: 'name' },
  { title: t('fixedAssets.acquisitionCost'), key: 'acquisition_cost', render: row => `${row.acquisition_cost} / ${row.residual_value}` },
  { title: t('fixedAssets.inServiceDate'), key: 'in_service_date', render: row => row.pending_in_service ? t('fixedAssets.pendingInService') : row.in_service_date },
  { title: t('fixedAssets.completedPnl'), key: 'completed', render: row => yearly(row)?.fixed_asset_depreciation ?? '0.00' },
  { title: t('fixedAssets.fullYearPlan'), key: 'planned', render: row => yearly(row)?.planned_fixed_asset_depreciation ?? '0.00' },
  { title: t('fixedAssets.bookValue'), key: 'book', render: row => {
    const item = yearly(row)
    return item
      ? `${t('fixedAssets.bookValue')}: ${item.opening_book_value} → ${item.closing_book_value}; ${t('fixedAssets.plannedBookValue')}: ${item.planned_opening_book_value} → ${item.planned_closing_book_value}`
      : '—'
  } },
  { title: t('fixedAssets.sourcePurchase'), key: 'source', render: row => h(NButton, { text: true, onClick: () => router.push(`/expenses/${row.acquisition_expense_id}/edit`) }, { default: () => t('fixedAssets.openPurchase') }) },
  { title: t('fixedAssets.details'), key: 'details', render: row => h(NButton, { text: true, onClick: () => selectAsset(row) }, { default: () => t('fixedAssets.details') }) },
  ]
})
const detail = computed(() => selectedAsset.value ? yearly(selectedAsset.value) : undefined)
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
        <n-data-table :columns="columns" :data="assets" :loading="loading" :row-key="row => row.id" />
        <n-card v-if="selectedAsset" :title="selectedAsset.name" size="small">
          <n-space vertical>
            <n-text>{{ t('fixedAssets.sourcePurchase') }}: <a @click="router.push(`/expenses/${selectedAsset.acquisition_expense_id}/edit`)">{{ t('fixedAssets.openPurchase') }}</a></n-text>
            <n-text>{{ t('fixedAssets.acquisitionCost') }}: {{ selectedAsset.acquisition_cost }} · {{ t('fixedAssets.residualValue') }}: {{ selectedAsset.residual_value }}</n-text>
            <n-text>{{ t('fixedAssets.inServiceDate') }}: {{ selectedAsset.in_service_date ?? t('fixedAssets.pendingInService') }} · {{ t('fixedAssets.useStatus') }}: {{ selectedAsset.business_percentage }}%</n-text>
            <n-text v-if="detail">{{ year }} · {{ t('fixedAssets.completedPnl') }}: {{ detail.fixed_asset_depreciation }} · {{ t('fixedAssets.fullYearPlan') }}: {{ detail.planned_fixed_asset_depreciation }} · {{ t('fixedAssets.cumulative') }}: {{ detail.cumulative_depreciation }} · {{ t('fixedAssets.bookValue') }}: {{ detail.opening_book_value }} → {{ detail.closing_book_value }} · {{ t('fixedAssets.plannedBookValue') }}: {{ detail.planned_opening_book_value }} → {{ detail.planned_closing_book_value }}</n-text>
          </n-space>
        </n-card>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.fixed-assets-page { padding: 24px; }
</style>
