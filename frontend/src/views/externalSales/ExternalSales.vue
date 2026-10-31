<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NCheckbox, NForm, NFormItem, NInput, NModal, NSelect, useMessage } from 'naive-ui'
import { ApiError, del, get, post, put } from '../../api/http'
import ExternalSaleSnapshot from '../../components/externalSales/ExternalSaleSnapshot.vue'
import type { components } from '../../api/schema'

type Source = components['schemas']['ExternalSalesSourceRead']
type SourceInput = components['schemas']['ExternalSalesSourceInput']
type SourceDraft = Required<SourceInput>
type TaxDraft = Required<Pick<components['schemas']['ExternalSalesTaxInput'], 'treatment_id' | 'rate_id'>> & { tax_summary_id?: string; net_amount: string | null; vat_amount: string | null }
type Treatment = components['schemas']['VatTreatmentRead']
type Rate = components['schemas']['VatRateRead']
type Original = components['schemas']['ExternalSalesFileRead']
type Sale = components['schemas']['ExternalSalesRead']
type Calculation = components['schemas']['ExternalSalesCalculation']
type CalculateRequest = components['schemas']['ExternalSalesCalculateRequest']
type ConfirmRequest = components['schemas']['ExternalSalesConfirmRequest']
type SaleList = components['schemas']['ExternalSalesListRead']
type SourceList = components['schemas']['ExternalSalesSourceListRead']
type SaleDraft = Required<Omit<components['schemas']['ExternalSalesInput'], 'tax_summaries'>> & { tax_summaries: TaxDraft[] }
type RevisionInput = components['schemas']['ExternalSalesRevisionInput']
type RevisionPreview = components['schemas']['ExternalSalesRevisionPreview']
type Revision = components['schemas']['ExternalSalesRevisionRead']
type RevisionList = components['schemas']['ExternalSalesRevisionListRead']

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const message = useMessage()
const sources = ref<Source[]>([])
const treatments = ref<Treatment[]>([])
const rates = ref<Rate[]>([])
const sales = ref<Sale[]>([])
const originalCandidates = ref<Sale[]>([])
const selectedOriginal = ref<Sale | null>(null)
const originalsOffset = ref(0)
const originalsTotal = ref(0)
const originalsLoading = ref(false)
const originalsError = ref('')
let originalsGeneration = 0
const originalOptions = computed(() => {
  const rows = new Map(originalCandidates.value.map((sale) => [sale.id, sale]))
  if (selectedOriginal.value) rows.set(selectedOriginal.value.id, selectedOriginal.value)
  return Array.from(rows.values()).filter((sale) => sale.status === 'POSTED' && sale.document_kind !== 'CREDIT')
    .map((sale) => ({ label: `${sale.source_name} · ${sale.original_number}`, value: sale.id }))
})
const salesTotal = ref(0)
const salesOffset = ref(0)
const salesLimit = 50
const current = ref<Sale | null>(null)
const savedDraftSnapshot = ref<string | null>(null)
const calculation = ref<Calculation | null>(null)
const acknowledgeMismatch = ref(false)
const mismatchReason = ref('')
const postingBusy = ref(false)
const revisionMode = ref(false)
const revisionReason = ref('')
const revisionPreview = ref<RevisionPreview | null>(null)
const revisionHistory = ref<Revision[]>([])
const revisionHistoryTotal = ref(0)
const revisionHistoryOffset = ref(0)
const revisionHistoryLoading = ref(false)
const voidReason = ref('')
const loading = ref(false)
let refreshGeneration = 0
let draftRevision = 0
const error = ref('')
const pickedFiles = ref<File[]>([])
const sourceModal = ref(false)
const editingSource = ref<string | null>(null)
const sourceDraft = reactive<SourceDraft>({ name: '', kind: 'OTHER', default_include_in_pl: null, active: true })
const draft = reactive<SaleDraft>({ source_id: null, original_number: null, document_kind: 'SALE', related_external_sale_id: null, historical_original_number: null, historical_original_explanation: null, document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: null, tax_summaries: [blankTax()] })
const isDetail = computed(() => typeof route.params.id === 'string')
const isNew = computed(() => route.name === 'external-sales-new')
const isEditor = computed(() => isDetail.value || isNew.value)
const isPosted = computed(() => current.value?.status === 'POSTED' || current.value?.status === 'VOID')
const isVoided = computed(() => current.value?.status === 'VOID')
const isDirty = computed(() => current.value !== null && savedDraftSnapshot.value !== JSON.stringify(payload()))
const editorReady = computed(() => !loading.value && (isDetail.value ? current.value?.id === route.params.id : isNew.value && !current.value))
const sourceOptions = computed(() => sources.value.filter((source) => source.active || source.id === draft.source_id).map((source) => ({ label: `${source.name} (${t(`externalSales.${source.kind}`)})${source.active ? '' : ` · ${t('externalSales.sourceDisabled')}`}`, value: source.id })))
const treatmentOptions = computed(() => treatments.value.filter((row) => row.active && row.side === 'SALES' && row.code === 'NL_DOMESTIC' && row.effect === 'APPLY_RATE' && !row.requires_icp).map((row) => ({ label: `${row.label} (${row.code})`, value: row.id })))
const rateOptions = computed(() => rates.value.filter((row) => row.active).map((row) => ({ label: `${row.label} (${row.percent}%)`, value: row.id })))
const kindOptions = computed(() => (['PLATFORM', 'ENERGY', 'OTHER'] as const).map((kind) => ({ label: t(`externalSales.${kind}`), value: kind })))
const includeOptions = computed(() => [{ label: t('common.yes'), value: 'true' }, { label: t('common.no'), value: 'false' }])
const conflictMessages: Record<string, string> = {
  EXTERNAL_SALE_VERSION_CONFLICT: 'versionConflict',
  EXTERNAL_SALE_ALREADY_POSTED: 'alreadyPosted',
  EXTERNAL_SALE_NOT_PENDING: 'notPending',
  DUPLICATE_SOURCE_NUMBER: 'duplicateNumber',
}

function rateOptionsFor(row: TaxDraft) {
  const frozen = isPosted.value ? current.value?.tax_summaries.find((tax) => tax.id === row.tax_summary_id) : null
  if (!frozen?.rate_id || frozen.rate_id !== row.rate_id) return rateOptions.value
  return [...rateOptions.value.filter((option) => option.value !== frozen.rate_id), { label: `${frozen.rate_label} (${frozen.rate_percent}%)`, value: frozen.rate_id }]
}
function treatmentOptionsFor(row: TaxDraft) {
  const frozen = isPosted.value ? current.value?.tax_summaries.find((tax) => tax.id === row.tax_summary_id) : null
  if (!frozen?.treatment_id || frozen.treatment_id !== row.treatment_id) return treatmentOptions.value
  return [...treatmentOptions.value.filter((option) => option.value !== frozen.treatment_id), { label: `${frozen.treatment_label} (${frozen.treatment_code})`, value: frozen.treatment_id }]
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 409 && error.detail && typeof error.detail === 'object') {
    const detail = (error.detail as { detail?: unknown }).detail
    if (detail && typeof detail === 'object' && 'code' in detail) {
      const key = conflictMessages[String((detail as { code: unknown }).code)]
      if (key) return t(`externalSales.conflicts.${key}`)
    }
  }
  return error instanceof Error ? error.message : t('externalSales.error')
}

function blankTax(): TaxDraft { return { treatment_id: null, rate_id: null, net_amount: null, vat_amount: null } }
function resetDraft() {
  Object.assign(draft, { source_id: null, original_number: null, document_kind: 'SALE', related_external_sale_id: null, historical_original_number: null, historical_original_explanation: null, document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: null, tax_summaries: [blankTax()] })
  originalsGeneration++
  originalCandidates.value = []
  selectedOriginal.value = null
  originalsOffset.value = 0
  originalsTotal.value = 0
  originalsLoading.value = false
  originalsError.value = ''
  current.value = null
  savedDraftSnapshot.value = null
  calculation.value = null
  acknowledgeMismatch.value = false
  mismatchReason.value = ''
  pickedFiles.value = []
  revisionMode.value = false
  revisionReason.value = ''
  revisionPreview.value = null
  revisionHistory.value = []
  revisionHistoryTotal.value = 0
  revisionHistoryOffset.value = 0
  voidReason.value = ''
}
function loadDraft(sale: Sale) {
  Object.assign(draft, { source_id: sale.source_id, original_number: sale.original_number, document_kind: sale.document_kind, related_external_sale_id: sale.related_external_sale_id, historical_original_number: sale.historical_original_number, historical_original_explanation: sale.historical_original_explanation, document_date: sale.document_date, covered_from: sale.covered_from, covered_to: sale.covered_to, counterparty: sale.counterparty, description: sale.description, include_in_pl: sale.include_in_pl, tax_summaries: sale.tax_summaries.length ? sale.tax_summaries.map(({ id, treatment_id, rate_id, net_amount, vat_amount }) => ({ tax_summary_id: id, treatment_id, rate_id, net_amount: net_amount == null ? null : String(net_amount), vat_amount: vat_amount == null ? null : String(vat_amount) })) : [blankTax()] })
  current.value = sale
  savedDraftSnapshot.value = JSON.stringify(payload())
  calculation.value = null
  acknowledgeMismatch.value = false
  mismatchReason.value = ''
}
function updateFiles(sale: Sale, generation: number) {
  if (generation === refreshGeneration && editorReady.value && current.value?.id === sale.id) {
    current.value.files = sale.files
    current.value.version = sale.version
  }
}
async function allSources(): Promise<Source[]> {
  const rows: Source[] = []
  let offset = 0
  while (true) {
    const page = await get<SourceList>(`/api/v1/external-sales-sources?limit=100&offset=${offset}`)
    rows.push(...page.items)
    offset += page.items.length
    if (offset >= page.total || !page.items.length) return rows
  }
}
async function loadSales(offset = salesOffset.value) {
  let page = await get<SaleList>(`/api/v1/external-sales?limit=${salesLimit}&offset=${offset}`)
  if (page.total > 0 && offset >= page.total) {
    offset = Math.floor((page.total - 1) / salesLimit) * salesLimit
    page = await get<SaleList>(`/api/v1/external-sales?limit=${salesLimit}&offset=${offset}`)
  }
  sales.value = page.items
  salesTotal.value = page.total
  salesOffset.value = page.total ? page.offset : 0
}
async function loadOriginals(offset = 0) {
  const generation = ++originalsGeneration
  const refresh = refreshGeneration
  originalsLoading.value = true
  originalsError.value = ''
  try {
    const page = await get<SaleList>(`/api/v1/external-sales?limit=50&offset=${offset}`)
    if (generation !== originalsGeneration || refresh !== refreshGeneration) return
    originalCandidates.value = page.items
    originalsTotal.value = page.total
    originalsOffset.value = page.offset
  } catch (e) {
    if (generation === originalsGeneration && refresh === refreshGeneration) originalsError.value = errorMessage(e)
  } finally {
    if (generation === originalsGeneration && refresh === refreshGeneration) originalsLoading.value = false
  }
}
watch(() => draft.document_kind, (kind) => {
  if (kind !== 'CREDIT') {
    draft.related_external_sale_id = null
    draft.historical_original_number = null
    draft.historical_original_explanation = null
  } else if (!isPosted.value) void loadOriginals()
}, { flush: 'sync' })
watch(() => draft.related_external_sale_id, async (id) => {
  selectedOriginal.value = null
  if (!id) return
  draft.historical_original_number = null
  draft.historical_original_explanation = null
  const generation = refreshGeneration
  try {
    const sale = await get<Sale>(`/api/v1/external-sales/${id}`)
    if (generation === refreshGeneration && draft.related_external_sale_id === id) selectedOriginal.value = sale
  } catch (e) { if (generation === refreshGeneration) message.error(errorMessage(e)) }
})
async function loadRevisionHistory(saleId: string, offset = revisionHistoryOffset.value) {
  const generation = refreshGeneration
  revisionHistoryLoading.value = true
  try {
    const result = await get<RevisionList>(`/api/v1/external-sales/${saleId}/revisions?offset=${offset}&limit=50`)
    if (generation !== refreshGeneration || current.value?.id !== saleId) return
    revisionHistory.value = result.items
    revisionHistoryTotal.value = result.total
    revisionHistoryOffset.value = result.offset ?? offset
  } catch (e) { if (generation === refreshGeneration) message.error(errorMessage(e)) }
  finally { if (generation === refreshGeneration) revisionHistoryLoading.value = false }
}
async function changeRevisionPage(offset: number) {
  if (current.value) await loadRevisionHistory(current.value.id, offset)
}
async function changePage(offset: number) {
  loading.value = true
  error.value = ''
  try { await loadSales(offset) }
  catch (e) { error.value = e instanceof Error ? e.message : t('externalSales.error') }
  finally { loading.value = false }
}
async function refresh() {
  const generation = ++refreshGeneration
  const id = typeof route.params.id === 'string' ? route.params.id : null
  loading.value = true
  error.value = ''
  resetDraft()
  try {
    const [sourceRows, , treatmentRows, rateRows] = await Promise.all([allSources(), loadSales(), get<{ items: Treatment[] }>('/api/v1/vat-treatments?side=SALES'), get<{ items: Rate[] }>('/api/v1/vat-rates')])
    if (generation !== refreshGeneration) return
    sources.value = sourceRows
    treatments.value = treatmentRows.items
    rates.value = rateRows.items
    if (id) {
      const sale = await get<Sale>(`/api/v1/external-sales/${id}`)
      if (generation === refreshGeneration) {
        loadDraft(sale)
        await loadRevisionHistory(sale.id, 0)
      }
    }
  } catch (e) {
    if (generation === refreshGeneration) error.value = e instanceof Error ? e.message : t('externalSales.error')
  } finally {
    if (generation === refreshGeneration) loading.value = false
  }
}
watch(() => [route.name, route.params.id], () => { void refresh() }, { flush: 'sync' })
onMounted(() => { void refresh() })
onUnmounted(() => { refreshGeneration++ })
watch(() => draft.source_id, (id) => {
  if (!id) return
  const selected = sources.value.find((source) => source.id === id)
  if (selected && draft.include_in_pl === null) draft.include_in_pl = selected.default_include_in_pl ?? null
})
watch(draft, () => { draftRevision++; calculation.value = null; revisionPreview.value = null }, { deep: true })
function openSource(source?: Source) {
  editingSource.value = source?.id ?? null
  Object.assign(sourceDraft, source ? { name: source.name, kind: source.kind, default_include_in_pl: source.default_include_in_pl, active: source.active } : { name: '', kind: 'OTHER', default_include_in_pl: null, active: true })
  sourceModal.value = true
}
async function saveSource() {
  try {
    const body = { ...sourceDraft }
    if (editingSource.value) await put(`/api/v1/external-sales-sources/${editingSource.value}`, body)
    else await post('/api/v1/external-sales-sources', body)
    message.success(t('externalSales.sourceSaved'))
    sourceModal.value = false
    sources.value = await allSources()
  } catch (e) { message.error(e instanceof Error ? e.message : t('externalSales.error')) }
}
function payload(): components['schemas']['ExternalSalesInput'] {
  return { ...draft,
    related_external_sale_id: draft.document_kind === 'CREDIT' ? draft.related_external_sale_id || null : null,
    historical_original_number: draft.document_kind === 'CREDIT' && !draft.related_external_sale_id ? draft.historical_original_number || null : null,
    historical_original_explanation: draft.document_kind === 'CREDIT' && !draft.related_external_sale_id ? draft.historical_original_explanation || null : null,
    document_date: draft.document_date || null, covered_from: draft.covered_from || null, covered_to: draft.covered_to || null, tax_summaries: draft.tax_summaries.map((row) => ({ treatment_id: row.treatment_id || null, rate_id: row.rate_id || null, net_amount: row.net_amount || null, vat_amount: row.vat_amount || null })) }
}
function setIncludeInPl(value: string | null) {
  if (value === 'true' || value === 'false') draft.include_in_pl = value === 'true'
  else if (!isPosted.value) draft.include_in_pl = null
}
function revisionPayload(): RevisionInput | null {
  if (!current.value || typeof draft.include_in_pl !== 'boolean') return null
  return { expected_version: current.value.version, reason: revisionReason.value.trim(), document_date: draft.document_date, covered_from: draft.covered_from, covered_to: draft.covered_to, counterparty: draft.counterparty, description: draft.description, include_in_pl: draft.include_in_pl, tax_summaries: draft.tax_summaries.map((row) => ({ tax_summary_id: row.tax_summary_id, treatment_id: row.treatment_id || null, rate_id: row.rate_id || null, net_amount: row.net_amount || null, vat_amount: row.vat_amount || null })), acknowledge_vat_mismatch: acknowledgeMismatch.value, mismatch_ack_reason: mismatchReason.value || null }
}
async function previewRevision() {
  if (!editorReady.value || !isPosted.value || isVoided.value || !current.value || !revisionReason.value.trim() || draft.include_in_pl === null) return
  const generation = refreshGeneration
  const version = current.value.version
  const revision = draftRevision
  revisionPreview.value = null
  try {
    const result = await post<RevisionPreview>(`/api/v1/external-sales/${current.value.id}/revisions/preview`, revisionPayload())
    if (generation === refreshGeneration && current.value?.version === version && revision === draftRevision) revisionPreview.value = result
  } catch (e) { if (generation === refreshGeneration) message.error(errorMessage(e)) }
}
async function saveRevision() {
  if (!editorReady.value || !current.value || !revisionPreview.value || revisionPreview.value.expected_version !== current.value.version) return
  const generation = refreshGeneration
  postingBusy.value = true
  try {
    const result = await post<Sale>(`/api/v1/external-sales/${current.value.id}/revisions`, revisionPayload())
    if (generation === refreshGeneration) { loadDraft(result); revisionMode.value = false; revisionPreview.value = null; await loadRevisionHistory(result.id, 0); message.success(t('externalSales.revised')) }
  } catch (e) { if (generation === refreshGeneration) message.error(errorMessage(e)) }
  finally { if (generation === refreshGeneration) postingBusy.value = false }
}
async function voidSale() {
  if (!editorReady.value || !current.value || isVoided.value || !voidReason.value.trim() || !window.confirm(t('externalSales.confirmVoid'))) return
  const generation = refreshGeneration
  try {
    const result = await post<Sale>(`/api/v1/external-sales/${current.value.id}/void`, { expected_version: current.value.version, reason: voidReason.value.trim() })
    if (generation === refreshGeneration) { loadDraft(result); revisionMode.value = false; await loadRevisionHistory(result.id, 0); message.success(t('externalSales.voidedSuccess')) }
  } catch (e) { if (generation === refreshGeneration) message.error(errorMessage(e)) }
}
async function saveDraft() {
  if (!editorReady.value || isPosted.value) return
  const generation = refreshGeneration
  const id = current.value?.id
  try {
    const result = id ? await put<Sale>(`/api/v1/external-sales/${id}`, payload()) : await post<Sale>('/api/v1/external-sales', payload())
    if (generation !== refreshGeneration) return
    loadDraft(result)
    message.success(t('externalSales.saved'))
    if (!id) await router.push(`/external-sales/${result.id}`)
    else await refresh()
  } catch (e) {
    if (generation === refreshGeneration) message.error(errorMessage(e))
  }
}
async function uploadFiles() {
  if (!editorReady.value || !pickedFiles.value.length || !current.value) return
  const generation = refreshGeneration
  const id = current.value.id
  const form = new FormData()
  pickedFiles.value.forEach((file) => form.append('files', file))
  try {
    const response = await fetch(`/api/v1/external-sales/${id}/files`, { method: 'POST', credentials: 'include', body: form })
    if (!response.ok) throw new Error(await response.text())
    updateFiles(await response.json() as Sale, generation)
    calculation.value = null
    if (generation === refreshGeneration) pickedFiles.value = []
  } catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
  }
}
async function downloadFile(file: Original) {
  if (!editorReady.value || !current.value) return
  const generation = refreshGeneration
  const id = current.value.id
  try {
    const response = await fetch(`/api/v1/external-sales/${id}/files/${file.id}`, { credentials: 'include' })
    if (!response.ok) throw new Error(t('externalSales.error'))
    const blob = await response.blob()
    if (generation !== refreshGeneration) return
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = file.filename || 'original'
    link.click()
    URL.revokeObjectURL(link.href)
  } catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
  }
}
async function removeFile(file: Original) {
  if (!editorReady.value || isPosted.value || !current.value) return
  const generation = refreshGeneration
  const id = current.value.id
  try { updateFiles(await del<Sale>(`/api/v1/external-sales/${id}/files/${file.id}`), generation); calculation.value = null }
  catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
  }
}
async function discard() {
  if (!editorReady.value || isPosted.value || !current.value || !window.confirm(t('externalSales.confirmDiscard'))) return
  const generation = refreshGeneration
  const id = current.value.id
  try {
    await del(`/api/v1/external-sales/${id}`)
    if (generation === refreshGeneration) await router.push('/external-sales')
  } catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
  }
}
async function calculate() {
  if (!editorReady.value || isPosted.value || !current.value) return
  if (isDirty.value) {
    message.error(t('externalSales.saveBeforeCalculate'))
    return
  }
  const generation = refreshGeneration
  const expectedVersion = current.value.version
  const expectedDraftRevision = draftRevision
  calculation.value = null
  try {
    const request: CalculateRequest = { expected_version: expectedVersion }
    const result = await post<Calculation>(`/api/v1/external-sales/${current.value.id}/calculate`, request)
    if (generation !== refreshGeneration || current.value?.version !== expectedVersion || draftRevision !== expectedDraftRevision) return
    calculation.value = result
    acknowledgeMismatch.value = false
    mismatchReason.value = ''
  } catch (e) {
    if (generation === refreshGeneration) message.error(errorMessage(e))
  }
}
async function confirmPosting() {
  if (!editorReady.value || isPosted.value || !current.value || !calculation.value) return
  if (calculation.value.expected_version !== current.value.version) { calculation.value = null; return }
  const generation = refreshGeneration
  postingBusy.value = true
  try {
    const request: ConfirmRequest = {
      expected_version: calculation.value.expected_version,
      acknowledge_vat_mismatch: acknowledgeMismatch.value,
      mismatch_ack_reason: mismatchReason.value || null,
    }
    const result = await post<Sale>(`/api/v1/external-sales/${current.value.id}/confirm`, request)
    if (generation !== refreshGeneration) return
    loadDraft(result)
    message.success(t('externalSales.posted'))
  } catch (e) {
    if (generation === refreshGeneration) message.error(errorMessage(e))
  } finally {
    if (generation === refreshGeneration) postingBusy.value = false
  }
}
</script>

<template>
  <main class="external-sales">
    <header class="page-header"><h1>{{ t('externalSales.title') }}</h1><n-button v-if="!isEditor" type="primary" @click="router.push('/external-sales/new')">{{ t('externalSales.new') }}</n-button><n-button v-else @click="router.push('/external-sales')">{{ t('externalSales.title') }}</n-button></header>
    <p v-if="error" class="error">{{ error }}</p>
    <template v-if="!isEditor">
      <n-card :title="t('externalSales.sources')" class="section">
        <n-button size="small" @click="openSource()">{{ t('externalSales.saveSource') }}</n-button>
        <div v-for="source in sources" :key="source.id" class="source-row">
          <span>{{ source.name }} · {{ t(`externalSales.${source.kind}`) }} · {{ source.active ? t('externalSales.active') : t('externalSales.sourceDisabled') }}</span>
          <n-button size="small" @click="openSource(source)">{{ t('externalSales.editSource') }}</n-button>
        </div>
      </n-card>
      <n-card class="section">
        <p v-if="loading">…</p><p v-else-if="!sales.length">{{ t('externalSales.empty') }}</p>
        <router-link v-for="sale in sales" :key="sale.id" class="sale-row" :to="`/external-sales/${sale.id}`">
          <strong>{{ sale.original_number || t('externalSales.none') }}</strong><span>{{ sale.source_name || t('externalSales.none') }}</span><span>{{ sale.document_date || t('externalSales.pending') }}</span><span>{{ sale.files.length }} · {{ t('externalSales.files') }}</span>
        </router-link>
        <div class="actions">
          <n-button :disabled="loading || salesOffset === 0" @click="changePage(Math.max(0, salesOffset - salesLimit))">{{ t('externalSales.previousPage') }}</n-button>
          <span>{{ t('externalSales.pageRange', { start: salesTotal ? salesOffset + 1 : 0, end: salesOffset + sales.length, total: salesTotal }) }}</span>
          <n-button :disabled="loading || salesOffset + sales.length >= salesTotal" @click="changePage(salesOffset + salesLimit)">{{ t('externalSales.nextPage') }}</n-button>
        </div>
      </n-card>
    </template>
    <n-card v-else class="section">
      <n-alert v-if="isPosted && !isVoided" type="success" :show-icon="true">
        {{ t('externalSales.posted') }} · {{ current?.posted_at }} · {{ current?.posted_source_name }} ·
        {{ current?.original_number }}
        <div>{{ t('externalSales.originalTotals') }}: {{ current?.original_net_total }} / {{ current?.original_vat_total }}</div>
        <div v-if="current?.mismatch_ack_reason">{{ t('externalSales.mismatchReason') }}: {{ current.mismatch_ack_reason }}</div>
      </n-alert>
      <section v-if="isPosted && current" class="posted-detail" data-testid="posted-detail">
        <n-alert v-if="isVoided" type="warning" :show-icon="true">{{ t('externalSales.voided') }}</n-alert>
        <dl class="grid">
          <div><dt>{{ t('externalSales.source') }}</dt><dd>{{ current.posted_source_name }}</dd></div>
          <div><dt>{{ t('externalSales.documentKind') }}</dt><dd>{{ t(`externalSales.${current.document_kind}`) }}</dd></div>
          <div v-if="current.related_external_sale_id"><dt>{{ t('externalSales.relatedOriginal') }}</dt><dd><router-link :to="`/external-sales/${current.related_external_sale_id}`">{{ selectedOriginal?.original_number || current.related_external_sale_id }}</router-link></dd></div>
          <div v-if="current.historical_original_number"><dt>{{ t('externalSales.historicalNumber') }}</dt><dd>{{ current.historical_original_number }} · {{ current.historical_original_explanation }}</dd></div>
          <div><dt>{{ t('externalSales.number') }}</dt><dd>{{ current.original_number }}</dd></div>
          <div><dt>{{ t('externalSales.date') }}</dt><dd>{{ current.document_date }}</dd></div>
          <div><dt>{{ t('externalSales.counterparty') }}</dt><dd>{{ current.counterparty || t('externalSales.none') }}</dd></div>
          <div><dt>{{ t('externalSales.coveredFrom') }}</dt><dd>{{ current.covered_from || t('externalSales.none') }}</dd></div>
          <div><dt>{{ t('externalSales.coveredTo') }}</dt><dd>{{ current.covered_to || t('externalSales.none') }}</dd></div>
          <div><dt>{{ t('externalSales.includePl') }}</dt><dd data-testid="posted-include-pl">{{ current.include_in_pl ? t('common.yes') : t('common.no') }}</dd></div>
          <div><dt>{{ t('externalSales.version') }}</dt><dd>{{ current.version }}</dd></div>
          <div class="description"><dt>{{ t('externalSales.description') }}</dt><dd>{{ current.description || t('externalSales.none') }}</dd></div>
        </dl>
        <h2>{{ t('externalSales.taxSummaries') }}</h2>
        <div class="table-scroll">
          <table data-testid="posted-tax-summaries">
            <thead><tr><th>{{ t('externalSales.treatment') }}</th><th>{{ t('externalSales.rate') }}</th><th>{{ t('externalSales.net') }}</th><th>{{ t('externalSales.vat') }}</th></tr></thead>
            <tbody><tr v-for="row in current.tax_summaries" :key="row.id">
              <td>{{ row.treatment_label }} ({{ row.treatment_code }})</td>
              <td>{{ row.rate_label }} ({{ row.rate_percent }}%)</td>
              <td>{{ row.net_amount }}</td><td>{{ row.vat_amount }}</td>
            </tr></tbody>
          </table>
        </div>
        <h2>{{ t('externalSales.postingHistory') }}</h2>
        <dl class="grid">
          <div><dt>{{ t('externalSales.postedAt') }}</dt><dd>{{ current.posted_at }}</dd></div>
          <div><dt>{{ t('externalSales.actor') }}</dt><dd>{{ current.posted_by }}</dd></div>
        </dl>
        <div class="table-scroll">
          <table data-testid="posting-history">
            <thead><tr><th>{{ t('externalSales.version') }}</th><th>{{ t('externalSales.actor') }}</th><th>{{ t('externalSales.postedAt') }}</th></tr></thead>
            <tbody><tr v-for="posting in current.postings" :key="posting.id">
              <td>{{ posting.version }}</td><td>{{ posting.actor_id || t('externalSales.none') }}</td><td>{{ posting.created_at }}</td>
            </tr></tbody>
          </table>
        </div>
        <div v-if="!isVoided" class="actions">
          <n-button :disabled="!editorReady || postingBusy" @click="revisionMode = !revisionMode; revisionPreview = null">{{ t('externalSales.revise') }}</n-button>
          <n-input v-model:value="voidReason" :placeholder="t('externalSales.voidReason')" />
          <n-button type="error" :disabled="!editorReady || postingBusy || !voidReason.trim()" @click="voidSale">{{ t('externalSales.voidEntry') }}</n-button>
        </div>
        <h2>{{ t('externalSales.revisionHistory') }}</h2>
        <div class="table-scroll">
          <table data-testid="revision-history">
            <thead><tr><th>{{ t('externalSales.version') }}</th><th>{{ t('externalSales.actor') }}</th><th>{{ t('externalSales.postedAt') }}</th><th>{{ t('externalSales.revisionReason') }}</th><th>{{ t('externalSales.snapshotDetails') }}</th></tr></thead>
            <tbody><tr v-for="revision in revisionHistory" :key="revision.id">
              <td>{{ revision.version }} · {{ revision.action }}</td><td>{{ revision.actor_id || t('externalSales.none') }}</td><td>{{ revision.created_at }}</td><td>{{ revision.reason }}</td><td><details><summary>{{ t('externalSales.snapshotDetails') }} · {{ revision.before_snapshot.document_date }} → {{ revision.after_snapshot.document_date }}</summary>
                <div class="snapshot-comparison"><section><h3>{{ t('externalSales.before') }}</h3><ExternalSaleSnapshot :snapshot="revision.before_snapshot" /></section><section><h3>{{ t('externalSales.after') }}</h3><ExternalSaleSnapshot :snapshot="revision.after_snapshot" /></section></div>
              </details></td>
            </tr></tbody>
          </table>
        </div>
        <div class="actions" data-testid="revision-pagination">
          <n-button :disabled="revisionHistoryLoading || revisionHistoryOffset === 0" @click="changeRevisionPage(Math.max(0, revisionHistoryOffset - 50))">{{ t('externalSales.previousPage') }}</n-button>
          <span>{{ t('externalSales.pageRange', { start: revisionHistoryTotal ? revisionHistoryOffset + 1 : 0, end: revisionHistoryOffset + revisionHistory.length, total: revisionHistoryTotal }) }}</span>
          <n-button :disabled="revisionHistoryLoading || revisionHistoryOffset + revisionHistory.length >= revisionHistoryTotal" @click="changeRevisionPage(revisionHistoryOffset + 50)">{{ t('externalSales.nextPage') }}</n-button>
        </div>
      </section>
      <n-form v-if="!isPosted || revisionMode" :disabled="postingBusy" label-placement="top" @submit.prevent="isPosted ? previewRevision() : saveDraft()">
        <div class="grid">
          <n-form-item :label="t('externalSales.source')"><n-select data-testid="sale-source" v-model:value="draft.source_id" clearable :options="sourceOptions" :disabled="isPosted" /></n-form-item>
          <n-form-item :label="t('externalSales.number')"><n-input data-testid="sale-number" v-model:value="draft.original_number" :disabled="isPosted" /></n-form-item>
          <n-form-item v-if="!isPosted" :label="t('externalSales.documentKind')"><n-select data-testid="document-kind" v-model:value="draft.document_kind" :options="(['SALE','CREDIT','SETTLEMENT_ADJUSTMENT'] as const).map((kind) => ({ label: t(`externalSales.${kind}`), value: kind }))" /></n-form-item>
          <n-form-item v-if="!isPosted && draft.document_kind === 'CREDIT'" :label="t('externalSales.relatedOriginal')"><n-select data-testid="related-original" v-model:value="draft.related_external_sale_id" clearable filterable :options="originalOptions" :loading="originalsLoading" /></n-form-item>
          <div v-if="!isPosted && draft.document_kind === 'CREDIT'" class="actions" data-testid="original-pagination">
            <n-button :disabled="originalsLoading || originalsOffset === 0" @click="loadOriginals(Math.max(0, originalsOffset - 50))">{{ t('externalSales.previousPage') }}</n-button>
            <span>{{ t('externalSales.pageRange', { start: originalsTotal ? originalsOffset + 1 : 0, end: originalsOffset + originalCandidates.length, total: originalsTotal }) }}</span>
            <n-button :disabled="originalsLoading || originalsOffset + originalCandidates.length >= originalsTotal" @click="loadOriginals(originalsOffset + 50)">{{ t('externalSales.nextPage') }}</n-button>
            <template v-if="originalsError"><span class="error">{{ originalsError }}</span><n-button @click="loadOriginals(originalsOffset)">{{ t('externalSales.retryOriginals') }}</n-button></template>
          </div>
          <n-form-item v-if="!isPosted && draft.document_kind === 'CREDIT' && !draft.related_external_sale_id" :label="t('externalSales.historicalNumber')"><n-input data-testid="historical-number" v-model:value="draft.historical_original_number" /></n-form-item>
          <n-form-item v-if="!isPosted && draft.document_kind === 'CREDIT' && !draft.related_external_sale_id" :label="t('externalSales.historicalExplanation')"><n-input data-testid="historical-explanation" v-model:value="draft.historical_original_explanation" type="textarea" /></n-form-item>
          <n-form-item :label="t('externalSales.date')"><input v-model="draft.document_date" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.counterparty')"><n-input v-model:value="draft.counterparty" /></n-form-item>
          <n-form-item :label="t('externalSales.coveredFrom')"><input v-model="draft.covered_from" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.coveredTo')"><input v-model="draft.covered_to" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.includePl')"><n-select data-testid="include-in-pl" :value="draft.include_in_pl === null ? null : String(draft.include_in_pl)" :clearable="!isPosted" :options="includeOptions" @update:value="setIncludeInPl" /></n-form-item>
        </div>
        <n-form-item :label="t('externalSales.description')"><n-input v-model:value="draft.description" type="textarea" /></n-form-item>
        <h2>{{ t('externalSales.taxSummaries') }}</h2>
        <div v-for="(row, index) in draft.tax_summaries" :key="index" class="tax-row">
          <n-select v-model:value="row.treatment_id" clearable :options="treatmentOptionsFor(row)" :placeholder="t('externalSales.treatment')" />
          <n-select v-model:value="row.rate_id" clearable :data-testid="`tax-rate-${index}`" :options="rateOptionsFor(row)" :placeholder="t('externalSales.rate')" />
          <n-input v-model:value="row.net_amount" :placeholder="t('externalSales.net')" inputmode="decimal" />
          <n-input v-model:value="row.vat_amount" :placeholder="t('externalSales.vat')" inputmode="decimal" />
          <n-button v-if="draft.tax_summaries.length > 1" @click="draft.tax_summaries.splice(index, 1)">×</n-button>
        </div>
        <n-button size="small" @click="draft.tax_summaries.push(blankTax())">{{ t('externalSales.addTax') }}</n-button>
        <n-form-item v-if="!isPosted" class="actions"><n-button type="primary" attr-type="submit" :disabled="!editorReady">{{ t('externalSales.saveDraft') }}</n-button><n-button v-if="current" type="error" :disabled="!editorReady" @click="discard">{{ t('externalSales.discard') }}</n-button></n-form-item>
        <template v-if="isPosted">
          <n-form-item :label="t('externalSales.revisionReason')"><n-input data-testid="revision-reason" v-model:value="revisionReason" type="textarea" /></n-form-item>
          <n-checkbox v-model:checked="acknowledgeMismatch">{{ t('externalSales.acknowledgeMismatch') }}</n-checkbox>
          <n-form-item v-if="acknowledgeMismatch" :label="t('externalSales.mismatchReason')"><n-input v-model:value="mismatchReason" type="textarea" /></n-form-item>
          <div class="actions"><n-button @click="previewRevision">{{ t('externalSales.previewRevision') }}</n-button><n-button type="primary" :disabled="!revisionPreview || revisionPreview.expected_version !== current?.version" @click="saveRevision">{{ t('externalSales.saveRevision') }}</n-button></div>
          <n-alert v-if="revisionPreview" type="info" :show-icon="true">{{ t('externalSales.oldPeriod') }}: {{ revisionPreview.old_period }} · {{ t('externalSales.newPeriod') }}: {{ revisionPreview.new_period }}<br />{{ t('externalSales.originalTotals') }}: {{ revisionPreview.old_net_total }} / {{ revisionPreview.old_vat_total }} → {{ revisionPreview.new_net_total }} / {{ revisionPreview.new_vat_total }}<br /><span v-for="warning in revisionPreview.warnings" :key="warning">{{ warning }}<br /></span></n-alert>
        </template>
      </n-form>
      <div v-if="current && !isPosted" class="posting-review">
        <n-button :disabled="!editorReady || current.files.length === 0 || isDirty" @click="calculate">
          {{ t('externalSales.calculate') }}
        </n-button>
        <p v-if="isDirty">{{ t('externalSales.saveBeforeCalculate') }}</p>
        <template v-if="calculation">
          <p>{{ t('externalSales.originalTotals') }}: {{ calculation.original_net_total }} / {{ calculation.original_vat_total }}</p>
          <p>{{ t('externalSales.derivedVat') }}: {{ calculation.derived_vat_total }}</p>
          <n-alert v-for="warning in calculation.warnings" :key="warning" type="warning" :show-icon="true">
            {{ warning }}
          </n-alert>
          <n-checkbox v-if="calculation.mismatch" v-model:checked="acknowledgeMismatch">
            {{ t('externalSales.acknowledgeMismatch') }}
          </n-checkbox>
          <n-form-item v-if="calculation.mismatch" :label="t('externalSales.mismatchReason')">
            <n-input v-model:value="mismatchReason" type="textarea" />
          </n-form-item>
          <n-button type="primary" :disabled="!editorReady || (calculation.mismatch && (!acknowledgeMismatch || !mismatchReason.trim()))" @click="confirmPosting">
            {{ t('externalSales.confirmPosting') }}
          </n-button>
        </template>
      </div>
      <div v-if="current" class="files">
        <h2>{{ isPosted ? t('externalSales.supportingEvidence') : t('externalSales.files') }} ({{ current.files.length }}/5)</h2>
        <div v-for="file in current.files" :key="file.id" class="file-row"><span>{{ file.filename || t('externalSales.none') }} · {{ file.byte_size }} bytes <strong v-if="file.duplicate_warning">{{ t('externalSales.duplicateWarning') }}</strong></span><n-button size="small" :disabled="!editorReady" @click="downloadFile(file)">{{ t('externalSales.download') }}</n-button><n-button v-if="!isPosted" size="small" :disabled="!editorReady" @click="removeFile(file)">{{ t('externalSales.removeFile') }}</n-button></div>
        <input type="file" multiple accept="image/png,image/jpeg,image/webp,application/pdf" :disabled="!editorReady || current.files.length >= 5" @change="pickedFiles = Array.from(($event.target as HTMLInputElement).files || []).slice(0, 5 - current!.files.length)" />
        <n-button :disabled="!editorReady || !pickedFiles.length" @click="uploadFiles">{{ isPosted ? t('externalSales.appendEvidence') : t('externalSales.upload') }}</n-button>
      </div>
    </n-card>
    <n-modal v-model:show="sourceModal" preset="card" style="width: min(520px, 92vw)" :title="t('externalSales.editSource')">
      <n-form label-placement="top">
        <n-form-item :label="t('externalSales.sourceName')"><n-input v-model:value="sourceDraft.name" /></n-form-item>
        <n-form-item :label="t('externalSales.kind')"><n-select v-model:value="sourceDraft.kind" :options="kindOptions" /></n-form-item>
        <n-form-item :label="t('externalSales.defaultPl')"><n-select :value="sourceDraft.default_include_in_pl === null ? null : String(sourceDraft.default_include_in_pl)" clearable :options="includeOptions" @update:value="sourceDraft.default_include_in_pl = $event === null ? null : $event === 'true'" /></n-form-item>
        <n-checkbox v-model:checked="sourceDraft.active">{{ t('externalSales.active') }}</n-checkbox>
        <div class="actions"><n-button type="primary" @click="saveSource">{{ t('externalSales.saveSource') }}</n-button></div>
      </n-form>
    </n-modal>
  </main>
</template>

<style scoped>
.external-sales { max-width: 1120px; margin: 0 auto; padding: 24px; }
.page-header, .sale-row, .source-row, .file-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-header { margin-bottom: 20px; }
.page-header h1 { margin: 0; }
.section { margin-bottom: 20px; }
.source-row, .file-row, .sale-row { padding: 12px 4px; border-bottom: 1px solid var(--n-border-color); }
.sale-row { color: inherit; text-decoration: none; }
.sale-row:hover { background: var(--n-color-hover); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.tax-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) auto; gap: 8px; margin: 8px 0; }
.actions { display: flex; gap: 12px; margin-top: 16px; }
.files { margin-top: 24px; }
.posted-detail dd { margin: 4px 0 16px; overflow-wrap: anywhere; white-space: pre-wrap; }
.posted-detail dt { font-weight: 600; }
.description { grid-column: 1 / -1; }
.snapshot-comparison { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 24px; }
.table-scroll { overflow-x: auto; }
.posted-detail table { width: 100%; border-collapse: collapse; }
.posted-detail th, .posted-detail td { text-align: left; padding: 8px; border-bottom: 1px solid var(--n-border-color); }
.error { color: #c33; }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } .tax-row { grid-template-columns: 1fr 1fr; } .sale-row { align-items: flex-start; flex-direction: column; } }
</style>
