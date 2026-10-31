<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NCheckbox, NDatePicker, NFormItem, NInput, NInputNumber, NList, NListItem, NPagination, NSelect, NSpace, NTag, NText, NUpload } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { ApiError, get, post, postFile, put } from '../../api/http'
import type { components } from '../../api/schema'
import { localDateStr } from '../../utils/date'

type Asset = components['schemas']['BatteryAssetRead']
type AssetPage = components['schemas']['BatteryAssetListRead']
type AnnualUse = components['schemas']['BatteryAnnualUseRead']
type Preview = components['schemas']['BatteryAssetPreview']
type AnnualCalc = components['schemas']['BatteryAnnualCalculation']

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
const yearChoices = computed(() => selected.value?.applicable_years ?? [])
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
const dirty = computed(() => percentage.value !== (
  annual.value?.private_use_percentage == null ? null : Number(annual.value.private_use_percentage)
) || basisNote.value !== (annual.value?.basis_note ?? ''))
const canEdit = computed(() => annualReady.value && !annualLoading.value && !annualBusy.value && !annual.value?.confirmed)
const canCalculate = computed(() => canEdit.value && !!annual.value && !dirty.value && percentage.value !== null && !!basisNote.value.trim())

function resetAnnual() {
  annualGeneration += 1
  annualReady.value = false
  annualLoading.value = false
  annualBusy.value = false
  annual.value = null
  percentage.value = null
  basisNote.value = ''
  calculation.value = null
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
  pageError.value = error instanceof ApiError ? error.message : String(error)
}
function applyAnnual(value: AnnualUse | null) {
  annual.value = value
  percentage.value = value?.private_use_percentage == null ? null : Number(value.private_use_percentage)
  basisNote.value = value?.basis_note ?? ''
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
  const id = route.params.id
  selected.value = null
  resetAnnual()
  if (typeof id !== 'string' || !id) return
  try {
    const asset = await get<Asset>(`/api/v1/battery-assets/${id}`)
    if (generation !== assetGeneration || id !== route.params.id) return
    selected.value = asset
    const requestedYear = Number(route.query.year)
    const nextYear = selected.value.applicable_years.includes(requestedYear)
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
  try {
    const value = await put<AnnualUse>(annualUrl(target), input)
    if (!isCurrent(target)) return
    // Retain edits made while a save was in flight; only the returned snapshot
    // is saved. Normal controls are disabled during the operation as well.
    if (revision === editRevision) applyAnnual(value)
    else annual.value = value
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
    const value = await post<AnnualCalc>(`${annualUrl(target)}/calculate`, { expected_version: annual.value.version })
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
    const value = await post<AnnualUse>(`${annualUrl(target)}/confirm`, { expected_version: annual.value.version })
    if (!isCurrent(target)) return
    applyAnnual(value)
    calculation.value = null
  } catch (error) { if (isCurrent(target)) showError(error) }
  finally { if (isCurrent(target)) annualBusy.value = false }
}

async function uploadEvidence(options: { file: UploadFileInfo }) {
  const target = annualTarget()
  if (!target || !canEdit.value || !annual.value || !options.file.file) return
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

watch([percentage, basisNote], () => {
  editRevision += 1
  calculation.value = null
}, { flush: 'sync' })
watch(year, () => { void loadYear() }, { flush: 'sync' })
watch(() => route.query.year, value => {
  const requestedYear = Number(value)
  if (Number.isInteger(requestedYear) && requestedYear > 0) year.value = requestedYear
}, { flush: 'sync' })
watch(assetPage, () => { void loadAssets() }, { flush: 'sync' })
watch(() => route.params.id, () => { void loadAssets(); void loadAsset() }, { immediate: true, flush: 'sync' })
watch([serviceDate, ownerConfirms, registerExpenseId, () => route.params.id], () => {
  registrationGeneration += 1
  preview.value = null
  historyAck.value = false
  busy.value = false
}, { flush: 'sync' })
onBeforeUnmount(() => { assetGeneration += 1; listGeneration += 1; registrationGeneration += 1; resetAnnual() })
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
          <n-button :disabled="!canEdit" :loading="annualBusy" @click="saveAnnual">{{ t('battery.saveAnnual') }}</n-button>
          <n-button :disabled="!canCalculate" @click="calculate">{{ t('battery.calculate') }}</n-button>
          <n-button type="primary" :disabled="!canCalculate || !calculation" :loading="annualBusy" @click="confirmAnnual">{{ t('battery.confirm') }}</n-button>
        </n-space>
        <n-alert v-if="calculation" type="info" style="margin-top: 12px">
          {{ t('battery.calculation', { base: calculation.calculation_base, vat: calculation.calculation_vat }) }}
        </n-alert>
        <section class="evidence">
          <h3>{{ t('battery.evidence') }}</h3>
          <n-upload :custom-request="uploadEvidence" :show-file-list="false" accept="image/png,image/jpeg,image/webp,application/pdf">
            <n-button :disabled="!canEdit || !annual">{{ t('battery.addEvidence') }}</n-button>
          </n-upload>
          <ul><li v-for="item in annual?.evidence ?? []" :key="item.id"><button class="link" @click="openEvidence(item.id)">{{ item.filename || item.media_type }}</button></li></ul>
        </section>
      </n-card>
    </div>
  </main>
</template>

<style scoped>
.battery-page { max-width: 1120px; margin: 0 auto; padding: 24px; }
.battery-layout { display: grid; grid-template-columns: minmax(260px, 0.8fr) minmax(420px, 1.2fr); gap: 16px; }
.asset-snapshot, .evidence { margin: 12px 0; }
.link { border: 0; background: none; color: var(--primary-color); cursor: pointer; text-decoration: underline; }
@media (max-width: 760px) { .battery-layout { grid-template-columns: 1fr; } }
</style>
