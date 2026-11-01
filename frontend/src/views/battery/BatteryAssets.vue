<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NCheckbox, NDatePicker, NFormItem, NInput, NInputNumber, NList, NListItem, NPagination, NSelect, NSpace, NTag, NText, NUpload } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { ApiError, get, post, postFile, put } from '../../api/http'
import type { components } from '../../api/schema'
import { localDateStr } from '../../utils/date'
import BatteryAuditSnapshot from '../../components/battery/BatteryAuditSnapshot.vue'

type Asset = components['schemas']['BatteryAssetRead']
type AssetPage = components['schemas']['BatteryAssetListRead']
type AnnualUse = components['schemas']['BatteryAnnualUseRead']
type Preview = components['schemas']['BatteryAssetPreview']
type AnnualCalc = components['schemas']['BatteryAnnualCalculation']
type AssetRevisionPreview = components['schemas']['BatteryAssetRevisionPreview']
type AssetRevision = components['schemas']['BatteryAssetRevisionRead']
type AnnualRevisionPreview = components['schemas']['BatteryAnnualRevisionPreview']
type AnnualRevision = components['schemas']['BatteryAnnualRevisionRead']
type Rate = components['schemas']['VatRateRead']

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const assets = ref<Asset[]>([])
const assetPage = ref(1)
const assetTotal = ref(0)
const assetsLoading = ref(false)
const listError = ref('')
const assetPageSize = 100
const selected = ref<Asset | null>(null)
const pageError = ref('')
const busy = ref(false)
const serviceDate = ref(localDateStr(new Date()))
const ownerConfirms = ref(false)
const historyAck = ref(false)
const preview = ref<Preview | null>(null)
const annual = ref<AnnualUse | null>(null)
const year = ref(Number(route.query.year) || new Date().getFullYear())
const percentage = ref<number | null>(null)
const basisNote = ref('')
const calculation = ref<AnnualCalc | null>(null)
const assetRevisions = ref<AssetRevision[]>([])
const annualRevisions = ref<AnnualRevision[]>([])
const assetHistoryLoading = ref(false)
const annualHistoryLoading = ref(false)
const assetHistoryError = ref('')
const annualHistoryError = ref('')
let assetHistoryGeneration = 0
let annualHistoryGeneration = 0
const vatRates = ref<Rate[]>([])
const assetRevisionPreview = ref<AssetRevisionPreview | null>(null)
const annualRevisionPreview = ref<AnnualRevisionPreview | null>(null)
const assetRevisionReason = ref('')
const annualRevisionReason = ref('')
const assetRevisionAck = ref(false)
const annualRevisionAck = ref(false)
const acquisitionDate = ref('')
const revisedServiceDate = ref('')
const revisedNetBasis = ref<number | null>(null)
const revisedPurchaseVat = ref<number | null>(null)
const revisedRateId = ref<string | null>(null)
const historicalYears = computed(() => [...new Set(assetRevisions.value.flatMap(revision => {
  const annuals = revision.before_snapshot.annual_before
  if (!Array.isArray(annuals)) return []
  return annuals.flatMap(item => {
    if (typeof item !== 'object' || item === null || !('year' in item)) return []
    const value = (item as { year?: unknown }).year
    return typeof value === 'number' && Number.isInteger(value) ? [value] : []
  })
}))].sort((left, right) => left - right))
const yearChoices = computed(() => [...new Set([...(selected.value?.applicable_years ?? []), ...historicalYears.value])]
  .sort((left, right) => left - right))
const isApplicableYear = computed(() => selected.value?.applicable_years.includes(year.value) ?? false)
const registerExpenseId = computed(() => typeof route.query.expense_id === 'string' ? route.query.expense_id : '')
const affectedPeriods = computed(() => preview.value?.affected_historical_periods ?? [])

const annualLoading = ref(false)
const annualBusy = ref(false)
const annualReady = ref(false)
let annualGeneration = 0
let assetGeneration = 0
let listGeneration = 0
let editRevision = 0
let registrationGeneration = 0
let assetRevisionGeneration = 0
const dirty = computed(() => percentage.value !== (
  annual.value?.private_use_percentage == null ? null : Number(annual.value.private_use_percentage)
) || basisNote.value !== (annual.value?.basis_note ?? ''))
const canEdit = computed(() => annualReady.value && isApplicableYear.value && !annualLoading.value && !annualBusy.value)
const canCalculate = computed(() => canEdit.value && !!annual.value && !dirty.value && percentage.value !== null && !!basisNote.value.trim())
const assetRevisionPeriods = computed(() => assetRevisionPreview.value?.affected_periods ?? [])

// Read every bounded API page so old annual-year entry points remain reachable.
async function loadAssetHistory(id: string) {
  const generation = ++assetHistoryGeneration
  const current = () => generation === assetHistoryGeneration && selected.value?.id === id
    && route.params.id === id
  assetRevisions.value = []
  assetHistoryError.value = ''
  assetHistoryLoading.value = true
  try {
    let offset = 0
    while (current()) {
      const history = await get<components['schemas']['BatteryAssetRevisionListRead']>(
        `/api/v1/battery-assets/${id}/revisions?limit=100${offset ? `&offset=${offset}` : ''}`,
      )
      if (!current()) return
      const items = (history?.items ?? []).filter(item => Array.isArray(item.affected_periods))
      assetRevisions.value.push(...items)
      offset += history?.items?.length ?? 0
      if (offset >= (history?.total ?? offset)) break
      if (!history?.items?.length) throw new Error(t('battery.historyIncomplete'))
    }
  } catch (error) {
    if (current()) assetHistoryError.value = error instanceof Error ? error.message : String(error)
  } finally { if (current()) assetHistoryLoading.value = false }
}

async function loadAnnualHistory(id: string, targetYear: number) {
  const generation = ++annualHistoryGeneration
  const target = annualTarget()
  if (!target || target.assetId !== id || target.year !== targetYear) return
  const current = () => generation === annualHistoryGeneration && isCurrent(target)
  annualRevisions.value = []
  annualHistoryError.value = ''
  annualHistoryLoading.value = true
  try {
    let offset = 0
    while (current()) {
      const history = await get<components['schemas']['BatteryAnnualRevisionListRead']>(
        `/api/v1/battery-assets/${id}/annual-use/${targetYear}/revisions?limit=100${offset ? `&offset=${offset}` : ''}`,
      )
      if (!current()) return
      annualRevisions.value.push(...(history?.items ?? []))
      offset += history?.items?.length ?? 0
      if (offset >= (history?.total ?? offset)) break
      if (!history?.items?.length) throw new Error(t('battery.historyIncomplete'))
    }
  } catch (error) {
    if (current()) {
      if (error instanceof ApiError && error.status === 404 && !annualRevisions.value.length) annualRevisions.value = []
      else annualHistoryError.value = error instanceof Error ? error.message : String(error)
    }
  } finally { if (current()) annualHistoryLoading.value = false }
}

async function loadRates() {
  try {
    const result = await get<{ items: Rate[] }>('/api/v1/vat-rates')
    vatRates.value = result?.items ?? []
  } catch (error) { showError(error) }
}

function resetAnnual() {
  annualGeneration += 1
  annualHistoryGeneration += 1
  annualHistoryLoading.value = false
  annualHistoryError.value = ''
  annualReady.value = false
  annualLoading.value = false
  annualBusy.value = false
  annual.value = null
  percentage.value = null
  basisNote.value = ''
  calculation.value = null
  annualRevisions.value = []
  annualRevisionPreview.value = null
  annualRevisionAck.value = false
  pageError.value = ''
}

function annualTarget() {
  if (!selected.value || selected.value.id !== route.params.id) return null
  return { generation: annualGeneration, assetId: selected.value.id, year: year.value }
}

type AnnualTarget = NonNullable<ReturnType<typeof annualTarget>>
function isCurrent(target: AnnualTarget) {
  return target.generation === annualGeneration && target.assetId === selected.value?.id
    && target.assetId === route.params.id && target.year === year.value
}
function annualUrl(target: AnnualTarget) {
  return `/api/v1/battery-assets/${target.assetId}/annual-use/${target.year}`
}
function showError(error: unknown) {
  if (error instanceof ApiError && error.detail && typeof error.detail === 'object'
    && 'detail' in error.detail) {
    const detail = error.detail.detail
    if (detail && typeof detail === 'object' && 'code' in detail
      && detail.code === 'BATTERY_PREVIEW_STALE') {
      pageError.value = t('battery.previewStale')
      return
    }
  }
  pageError.value = error instanceof ApiError ? error.message : String(error)
}
function applyAnnual(value: AnnualUse | null) {
  annual.value = value
  percentage.value = value?.private_use_percentage == null ? null : Number(value.private_use_percentage)
  basisNote.value = value?.basis_note ?? ''
  annualRevisionPreview.value = null
  annualRevisionAck.value = false
  annualRevisionReason.value = ''
}

async function loadAssets() {
  const generation = ++listGeneration
  const pageNumber = assetPage.value
  assetsLoading.value = true
  assets.value = []
  listError.value = ''
  try {
    const page = await get<AssetPage>(`/api/v1/battery-assets?limit=${assetPageSize}&offset=${(pageNumber - 1) * assetPageSize}`)
    if (generation !== listGeneration) return
    assets.value = page.items
    assetTotal.value = page.total
  } catch (error) {
    if (generation === listGeneration) listError.value = error instanceof ApiError ? error.message : String(error)
  } finally { if (generation === listGeneration) assetsLoading.value = false }
}

async function loadAsset() {
  const generation = ++assetGeneration
  assetRevisionGeneration += 1
  const id = route.params.id
  busy.value = false
  selected.value = null
  assetHistoryGeneration += 1
  assetRevisions.value = []
  assetHistoryError.value = ''
  assetHistoryLoading.value = false
  resetAnnual()
  if (typeof id !== 'string' || !id) return
  try {
    const asset = await get<Asset>(`/api/v1/battery-assets/${id}`)
    if (generation !== assetGeneration || id !== route.params.id) return
    selected.value = asset
    acquisitionDate.value = asset.acquisition_expense_date
    revisedServiceDate.value = asset.in_service_date
    revisedNetBasis.value = Number(asset.net_basis)
    revisedPurchaseVat.value = Number(asset.purchase_vat)
    revisedRateId.value = null
    assetRevisionPreview.value = null
    assetRevisionAck.value = false
    assetRevisionReason.value = ''
    await loadAssetHistory(asset.id)
    if (generation !== assetGeneration || id !== route.params.id) return
    const requestedYear = Number(route.query.year)
    const nextYear = yearChoices.value.includes(requestedYear)
      ? requestedYear : selected.value.applicable_years[0]!
    if (year.value !== nextYear) year.value = nextYear
    else await loadYear()
  } catch (error) {
    if (generation === assetGeneration && id === route.params.id) showError(error)
  }
}

async function doPreview() {
  if (!registerExpenseId.value) return
  const generation = registrationGeneration
  pageError.value = ''
  try {
    const value = await post<Preview>('/api/v1/battery-assets/preview', {
      acquisition_expense_id: registerExpenseId.value,
      in_service_date: serviceDate.value,
      owner_confirms_ordinary_vat_full_deduction: ownerConfirms.value,
    })
    if (generation === registrationGeneration) preview.value = value
  } catch (error) { if (generation === registrationGeneration) showError(error) }
}

async function registerAsset() {
  if (!registerExpenseId.value || busy.value) return
  const generation = registrationGeneration
  busy.value = true
  pageError.value = ''
  try {
    const asset = await post<Asset>('/api/v1/battery-assets', {
      acquisition_expense_id: registerExpenseId.value,
      in_service_date: serviceDate.value,
      owner_confirms_ordinary_vat_full_deduction: ownerConfirms.value,
      acknowledge_affected_periods: historyAck.value,
    })
    if (generation === registrationGeneration) {
      await router.replace(`/battery-assets/${asset.id}?year=${asset.applicable_years[0]}`)
    }
  } catch (error) { if (generation === registrationGeneration) showError(error) }
  finally { if (generation === registrationGeneration) busy.value = false }
}

function assetRevisionBody(acknowledge: boolean) {
  if (!selected.value) return null
  return {
    expected_version: selected.value.version,
    expected_rate_snapshot: acknowledge ? assetRevisionPreview.value?.rate_snapshot ?? null : null,
    reason: assetRevisionReason.value,
    acquisition_expense_date: acquisitionDate.value,
    in_service_date: revisedServiceDate.value,
    net_basis: revisedNetBasis.value == null ? null : String(revisedNetBasis.value),
    purchase_vat: revisedPurchaseVat.value == null ? null : String(revisedPurchaseVat.value),
    ...(revisedRateId.value ? { vat_rate_id: revisedRateId.value } : {}),
    acknowledge_affected_periods: acknowledge && assetRevisionPreview.value
      ? assetRevisionPreview.value.affected_periods
      : [],
    expected_annual_versions: acknowledge && assetRevisionPreview.value
      ? Object.fromEntries(assetRevisionPreview.value.annual_previews
          .filter(item => item.current_version != null)
          .map(item => [item.year, item.current_version]))
      : {},
  }
}

async function previewAssetRevision() {
  if (!selected.value || !assetRevisionReason.value.trim()) return
  const generation = assetRevisionGeneration
  const assetId = selected.value.id
  pageError.value = ''
  assetRevisionAck.value = false
  try {
    const result = await post<AssetRevisionPreview>(
      `/api/v1/battery-assets/${assetId}/revisions/preview`,
      assetRevisionBody(false),
    )
    if (generation === assetRevisionGeneration && selected.value?.id === assetId) {
      assetRevisionPreview.value = result
    }
  } catch (error) {
    if (generation === assetRevisionGeneration && selected.value?.id === assetId) showError(error)
  }
}

async function confirmAssetRevision() {
  if (!selected.value || !assetRevisionPreview.value || !assetRevisionAck.value) return
  const id = selected.value.id
  const generation = assetRevisionGeneration
  busy.value = true
  pageError.value = ''
  try {
    await post<Asset>(`/api/v1/battery-assets/${id}/revisions`, assetRevisionBody(true))
    if (generation !== assetRevisionGeneration || selected.value?.id !== id) return
    assetRevisionPreview.value = null
    assetRevisionAck.value = false
    await Promise.all([loadAssets(), loadAsset()])
  } catch (error) {
    if (generation === assetRevisionGeneration && selected.value?.id === id) {
      assetRevisionPreview.value = null
      assetRevisionAck.value = false
      showError(error)
    }
  } finally {
    if (generation === assetRevisionGeneration && selected.value?.id === id) busy.value = false
  }
}

async function loadYear() {
  resetAnnual()
  const target = annualTarget()
  if (!target) return
  annualLoading.value = true
  try {
    const value = await get<AnnualUse | null>(annualUrl(target))
    if (!isCurrent(target)) return
    applyAnnual(value)
    annualReady.value = true
    void loadAnnualHistory(target.assetId, target.year)
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualLoading.value = false }
}

async function saveAnnual() {
  const target = annualTarget()
  if (!target || !canEdit.value) return
  const revision = editRevision
  const input = {
    expected_version: annual.value?.version ?? null,
    private_use_percentage: percentage.value,
    basis_note: basisNote.value,
  }
  annualBusy.value = true
  pageError.value = ''
  calculation.value = null
  annualRevisionPreview.value = null
  try {
    if (annual.value) {
      if (!annualRevisionReason.value.trim()) return
      const revisionBody = {
        expected_version: annual.value.version,
        expected_asset_version: selected.value!.version,
        private_use_percentage: percentage.value,
        basis_note: basisNote.value,
        reason: annualRevisionReason.value,
        acknowledge_affected_periods: annualRevisionAck.value
          ? [`${target.year}-Q4`]
          : [],
      }
      const preview = await post<AnnualRevisionPreview>(
        `${annualUrl(target)}/revisions/preview`, revisionBody,
      )
      if (isCurrent(target) && revision === editRevision
        && annual.value?.version === revisionBody.expected_version) {
        annualRevisionPreview.value = preview
      }
      return
    }
    const value = await put<AnnualUse>(annualUrl(target), input)
    if (!isCurrent(target)) return
    // Retain edits made while a save was in flight; only the returned snapshot
    // is saved. Normal controls are disabled during the operation as well.
    if (revision === editRevision) applyAnnual(value)
    else annual.value = value
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

async function confirmAnnualRevision() {
  const target = annualTarget()
  if (!target || !annual.value || !annualRevisionPreview.value || !annualRevisionAck.value) return
  annualBusy.value = true
  pageError.value = ''
  try {
    const value = await post<AnnualUse>(`${annualUrl(target)}/revisions`, {
      expected_version: annual.value.version,
      expected_asset_version: selected.value!.version,
      private_use_percentage: percentage.value,
      basis_note: basisNote.value,
      reason: annualRevisionReason.value,
      acknowledge_affected_periods: annualRevisionPreview.value.affected_periods,
    })
    if (isCurrent(target)) {
      applyAnnual(value)
      await loadAnnualHistory(target.assetId, target.year)
    }
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

async function calculate() {
  const target = annualTarget()
  if (!target || !canCalculate.value || !annual.value) return
  const revision = editRevision
  annualBusy.value = true
  pageError.value = ''
  calculation.value = null
  try {
    const value = await post<AnnualCalc>(`${annualUrl(target)}/calculate`, {
      expected_version: annual.value.version,
      expected_asset_version: selected.value!.version,
    })
    if (isCurrent(target) && revision === editRevision && !dirty.value) calculation.value = value
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

async function confirmAnnual() {
  const target = annualTarget()
  if (!target || !canCalculate.value || !calculation.value || !annual.value) return
  annualBusy.value = true
  pageError.value = ''
  calculation.value = null
  try {
    const value = await post<AnnualUse>(`${annualUrl(target)}/confirm`, {
      expected_version: annual.value.version,
      expected_asset_version: selected.value!.version,
    })
    if (!isCurrent(target)) return
    applyAnnual(value)
    calculation.value = null
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

async function uploadEvidence(options: { file: UploadFileInfo }) {
  const target = annualTarget()
  if (!target || !canEdit.value || annualBusy.value || !annual.value || !options.file.file) return
  annualBusy.value = true
  pageError.value = ''
  calculation.value = null
  try {
    await postFile(`${annualUrl(target)}/evidence`, options.file.file)
    if (!isCurrent(target)) return
    const value = await get<AnnualUse | null>(annualUrl(target))
    if (isCurrent(target)) annual.value = value
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

function openEvidence(id: string) {
  const target = annualTarget()
  if (!target || !annualReady.value) return
  window.open(`${annualUrl(target)}/evidence/${id}/content`, '_blank', 'noopener')
}

watch([percentage, basisNote, annualRevisionReason], () => {
  editRevision += 1
  calculation.value = null
  annualRevisionPreview.value = null
  annualRevisionAck.value = false
}, { flush: 'sync' })
watch(
  [acquisitionDate, revisedServiceDate, revisedNetBasis, revisedPurchaseVat, revisedRateId, assetRevisionReason],
  () => {
    assetRevisionGeneration += 1
    assetRevisionPreview.value = null
    assetRevisionAck.value = false
  },
  { flush: 'sync' },
)
watch(year, () => { void loadYear() }, { flush: 'sync' })
watch(() => route.query.year, value => {
  const requestedYear = Number(value)
  if (Number.isInteger(requestedYear) && requestedYear > 0) year.value = requestedYear
}, { flush: 'sync' })
watch(assetPage, () => { void loadAssets() }, { flush: 'sync' })
watch(() => route.params.id, () => { void loadAssets(); void loadAsset() }, { immediate: true, flush: 'sync' })
void loadRates()
watch([serviceDate, ownerConfirms, registerExpenseId, () => route.params.id], () => {
  registrationGeneration += 1
  preview.value = null
  historyAck.value = false
  busy.value = false
}, { flush: 'sync' })
onBeforeUnmount(() => { assetHistoryGeneration += 1; assetRevisionGeneration += 1; assetGeneration += 1; listGeneration += 1; registrationGeneration += 1; resetAnnual() })
</script>

<template>
  <main class="battery-page">
    <h1>{{ t('battery.title') }}</h1>
    <n-alert v-if="pageError" type="error" style="margin-bottom: 16px">{{ pageError }}</n-alert>

    <n-card v-if="registerExpenseId" :title="t('battery.registerTitle')" style="margin-bottom: 20px">
      <p>{{ t('battery.eligibility') }}</p>
      <n-form-item :label="t('battery.inServiceDate')"><n-date-picker v-model:formatted-value="serviceDate" value-format="yyyy-MM-dd" type="date" /></n-form-item>
      <n-checkbox v-model:checked="ownerConfirms">{{ t('battery.ownerConfirm') }}</n-checkbox>
      <n-space style="margin-top: 12px">
        <n-button @click="doPreview">{{ t('battery.preview') }}</n-button>
        <n-button type="primary" :disabled="!preview?.eligible || (affectedPeriods.length > 0 && !historyAck)" :loading="busy" @click="registerAsset">{{ t('battery.register') }}</n-button>
      </n-space>
      <n-alert v-if="preview" :type="preview.eligible ? 'info' : 'warning'" style="margin-top: 12px">
        <div>{{ t('battery.purchaseSnapshot', { net: preview.net_basis ?? '—', vat: preview.purchase_vat ?? '—', rate: preview.purchase_vat_rate_percent ?? '—' }) }}</div>
        <div v-if="preview.conflict_code">{{ preview.conflict_code }}</div>
        <div v-if="affectedPeriods.length">{{ t('battery.affectedPeriods', { periods: affectedPeriods.join(', ') }) }}</div>
      </n-alert>
      <n-checkbox v-if="affectedPeriods.length" v-model:checked="historyAck">{{ t('battery.historyAck') }}</n-checkbox>
    </n-card>

    <div class="battery-layout">
      <n-card :title="t('battery.assets')">
        <n-alert v-if="listError" type="error">
          {{ listError }} <n-button :disabled="assetsLoading" @click="loadAssets">{{ t('common.retry') }}</n-button>
        </n-alert>
        <n-list clickable>
          <n-list-item v-for="asset in assets" :key="asset.id" @click="router.push(`/battery-assets/${asset.id}?year=${asset.applicable_years[0]}`)">
            <div>{{ asset.acquisition_expense_date }} · {{ asset.net_basis }} {{ asset.currency }}</div>
            <n-tag size="small">{{ asset.in_service_date }} – {{ asset.applicable_years.at(-1) }}</n-tag>
          </n-list-item>
        </n-list>
        <n-text v-if="!assets.length && !assetsLoading && !listError">{{ t('battery.empty') }}</n-text>
        <n-pagination v-if="assetTotal > assetPageSize" v-model:page="assetPage" :page-size="assetPageSize" :item-count="assetTotal" :disabled="assetsLoading" />
      </n-card>

      <n-card v-if="selected" :title="t('battery.annualTitle')">
        <div class="asset-snapshot">{{ t('battery.snapshot', { basis: selected.net_basis, rate: selected.purchase_vat_rate_percent }) }}</div>
        <n-alert v-if="!isApplicableYear" type="info" style="margin: 10px 0">{{ t('battery.historicalAnnualReadOnly') }}</n-alert>
        <n-form-item :label="t('battery.year')">
          <n-select v-model:value="year" :options="yearChoices.map(value => ({ label: String(value), value }))" />
        </n-form-item>
        <n-tag v-if="annual?.confirmed" type="success">{{ t('battery.confirmed') }}</n-tag>
        <n-alert v-if="annual?.confirmed" type="info" style="margin: 10px 0">
          {{ t('battery.confirmedCalculation', { base: annual.calculation_base, vat: annual.calculation_vat }) }}
        </n-alert>
        <n-form-item :label="t('battery.percentage')">
          <n-input-number v-model:value="percentage" :min="0" :max="100" :precision="3" :disabled="!canEdit" />
        </n-form-item>
        <n-form-item :label="t('battery.basisNote')">
          <n-input v-model:value="basisNote" type="textarea" :rows="3" :disabled="!canEdit" />
        </n-form-item>
        <n-space>
          <n-button :disabled="!canEdit || (annual != null && !annualRevisionReason.trim())" :loading="annualBusy" @click="saveAnnual">{{ annual ? t('battery.previewAnnualRevision') : t('battery.saveAnnual') }}</n-button>
          <n-button :disabled="!canCalculate" @click="calculate">{{ t('battery.calculate') }}</n-button>
          <n-button type="primary" :disabled="!canCalculate || !calculation" :loading="annualBusy" @click="confirmAnnual">{{ t('battery.confirm') }}</n-button>
        </n-space>
        <template v-if="annual">
          <n-form-item :label="t('battery.revisionReason')" style="margin-top: 16px">
            <n-input v-model:value="annualRevisionReason" :disabled="!canEdit" />
          </n-form-item>
          <n-alert v-if="annualRevisionPreview" type="info" style="margin: 10px 0">
            {{ t('battery.annualRevisionImpact', {
              period: annualRevisionPreview.affected_periods.join(', '),
              oldBase: annualRevisionPreview.current_calculation_base ?? '—',
              oldVat: annualRevisionPreview.current_calculation_vat ?? '—',
              newBase: annualRevisionPreview.proposed_calculation_base ?? '—',
              newVat: annualRevisionPreview.proposed_calculation_vat ?? '—',
            }) }}
          </n-alert>
          <n-checkbox v-if="annualRevisionPreview" v-model:checked="annualRevisionAck">
            {{ t('battery.historyAck') }}
          </n-checkbox>
          <n-button v-if="annualRevisionPreview" type="primary" :disabled="!annualRevisionAck || !annualRevisionReason.trim()" :loading="annualBusy" @click="confirmAnnualRevision">
            {{ t('battery.confirmAnnualRevision') }}
          </n-button>
        </template>
        <n-alert v-if="calculation" type="info" style="margin-top: 12px">
          {{ t('battery.calculation', { base: calculation.calculation_base, vat: calculation.calculation_vat }) }}
        </n-alert>
        <section class="evidence">
          <h3>{{ t('battery.evidence') }}</h3>
          <n-upload :custom-request="uploadEvidence" :show-file-list="false" :disabled="!canEdit || !annual || annualBusy" accept="image/png,image/jpeg,image/webp,application/pdf">
            <n-button :disabled="!canEdit || !annual || annualBusy">{{ t('battery.addEvidence') }}</n-button>
          </n-upload>
          <n-list v-if="annual?.evidence?.length" bordered class="evidence-list">
            <n-list-item v-for="item in annual?.evidence ?? []" :key="item.id">
              <div class="evidence-row">
                <n-text class="evidence-filename">{{ item.filename || item.media_type }}</n-text>
                <n-button class="evidence-open" text type="primary" @click="openEvidence(item.id)">{{ t('battery.openEvidence') }}</n-button>
              </div>
            </n-list-item>
          </n-list>
          <n-text v-else-if="annual" class="evidence-empty">{{ t('battery.noEvidence') }}</n-text>
        </section>
        <section class="history annual-history">
          <n-text v-if="annualHistoryLoading">{{ t('battery.historyLoading') }}</n-text>
          <n-alert v-if="annualHistoryError" type="error">{{ annualHistoryError }}
            <n-button @click="loadAnnualHistory(selected!.id, year)">{{ t('battery.retryHistory') }}</n-button>
          </n-alert>
          <h3>{{ t('battery.revisionHistory') }}</h3>
          <ul><li v-for="revision in annualRevisions" :key="revision.id">
            <p>{{ t('battery.auditMeta', { version: revision.version, actor: revision.actor_id ?? '—', time: revision.created_at }) }} · {{ revision.reason }}</p>
            <h4>{{ t('battery.auditBefore') }}</h4>
            <BatteryAuditSnapshot :snapshot="revision.before_snapshot" />
            <h4>{{ t('battery.auditAfter') }}</h4>
            <BatteryAuditSnapshot :snapshot="revision.after_snapshot" />
          </li></ul>
        </section>
      </n-card>
    </div>

    <n-card v-if="selected" :title="t('battery.assetRevisionTitle')" style="margin-top: 16px">
      <n-form-item :label="t('battery.acquisitionDate')">
        <n-date-picker v-model:formatted-value="acquisitionDate" value-format="yyyy-MM-dd" type="date" />
      </n-form-item>
      <n-form-item :label="t('battery.inServiceDate')">
        <n-date-picker v-model:formatted-value="revisedServiceDate" value-format="yyyy-MM-dd" type="date" />
      </n-form-item>
      <n-form-item :label="t('battery.netBasis')">
        <n-input-number v-model:value="revisedNetBasis" :min="0" :precision="2" />
      </n-form-item>
      <n-form-item :label="t('battery.purchaseVat')">
        <n-input-number v-model:value="revisedPurchaseVat" :min="0" :precision="2" />
      </n-form-item>
      <n-form-item :label="t('battery.purchaseRate')">
        <n-select v-model:value="revisedRateId" clearable :options="vatRates.filter(rate => rate.active).map(rate => ({ label: `${rate.label} (${rate.percent}%)`, value: rate.id }))" />
      </n-form-item>
      <n-form-item :label="t('battery.revisionReason')">
        <n-input v-model:value="assetRevisionReason" type="textarea" :rows="2" />
      </n-form-item>
      <n-space>
        <n-button :disabled="!assetRevisionReason.trim()" :loading="busy" @click="previewAssetRevision">{{ t('battery.previewRevision') }}</n-button>
        <n-button type="primary" v-if="assetRevisionPreview" :disabled="!assetRevisionAck" :loading="busy" @click="confirmAssetRevision">{{ t('battery.confirmRevision') }}</n-button>
      </n-space>
      <n-alert v-if="assetRevisionPreview" type="info" style="margin-top: 12px">
        <div>{{ t('battery.expenseImpact', {
          oldDate: assetRevisionPreview.expense_before.expense_date,
          oldNet: assetRevisionPreview.expense_before.net_amount,
          oldVat: assetRevisionPreview.expense_before.vat_amount,
          newDate: assetRevisionPreview.expense_after.expense_date,
          newNet: assetRevisionPreview.expense_after.net_amount,
          newVat: assetRevisionPreview.expense_after.vat_amount,
        }) }}</div>
        <div>{{ t('battery.periodImpact', { periods: assetRevisionPeriods.join(', ') }) }}</div>
        <ul><li v-for="item in assetRevisionPreview.annual_previews" :key="item.year">
          {{ t('battery.annualImpact', {
            year: item.year,
            oldBase: item.current_calculation_base ?? '—',
            oldVat: item.current_calculation_vat ?? '—',
            newBase: item.proposed_calculation_base ?? '—',
            newVat: item.proposed_calculation_vat ?? '—',
          }) }}
        </li></ul>
      </n-alert>
      <n-checkbox v-if="assetRevisionPreview" v-model:checked="assetRevisionAck">
        {{ t('battery.historyAck') }}
      </n-checkbox>
      <section class="history asset-history">
        <n-text v-if="assetHistoryLoading">{{ t('battery.historyLoading') }}</n-text>
        <n-alert v-if="assetHistoryError" type="error">{{ assetHistoryError }}
          <n-button @click="loadAssetHistory(selected!.id)">{{ t('battery.retryHistory') }}</n-button>
        </n-alert>
        <h3>{{ t('battery.revisionHistory') }}</h3>
        <ul><li v-for="revision in assetRevisions" :key="revision.id">
          <p>{{ t('battery.auditMeta', { version: revision.version, actor: revision.actor_id ?? '—', time: revision.created_at }) }} · {{ revision.reason }} · {{ revision.affected_periods?.join(', ') ?? '' }}</p>
          <h4>{{ t('battery.auditBefore') }}</h4>
          <BatteryAuditSnapshot :snapshot="revision.before_snapshot" />
          <h4>{{ t('battery.auditAfter') }}</h4>
          <BatteryAuditSnapshot :snapshot="revision.after_snapshot" />
        </li></ul>
      </section>
    </n-card>
  </main>
</template>

<style scoped>
.battery-page { max-width: 1120px; margin: 0 auto; padding: 24px; }
.battery-layout { display: grid; grid-template-columns: minmax(260px, 0.8fr) minmax(420px, 1.2fr); gap: 16px; }
.asset-snapshot, .evidence, .history { margin: 12px 0; }
.evidence-list { margin-top: 12px; }
.evidence-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; min-width: 0; }
.evidence-filename { min-width: 0; overflow-wrap: anywhere; }
.evidence-open { flex: 0 0 auto; }
.evidence-empty { display: inline-block; margin-top: 8px; }
@media (max-width: 760px) { .battery-layout { grid-template-columns: 1fr; } }
</style>
