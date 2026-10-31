<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NCard, NCheckbox, NForm, NFormItem, NInput, NModal, NSelect, useMessage } from 'naive-ui'
import { del, get, post, put } from '../../api/http'

type Source = { id: string; name: string; kind: 'PLATFORM' | 'ENERGY' | 'OTHER'; default_include_in_pl: boolean | null; active: boolean }
type Tax = { id?: string; treatment_id: string | null; treatment_code?: string | null; treatment_label?: string | null; rate_id: string | null; rate_label?: string | null; rate_percent?: string | null; net_amount: string | null; vat_amount: string | null }
type Treatment = { id: string; code: string; label: string; side: string; effect: string; requires_icp: boolean; active: boolean }
type Rate = { id: string; label: string; percent: string; active: boolean }
type Original = { id: string; filename: string | null; mime_type: string; byte_size: number; sha256: string; duplicate_warning: boolean; created_at: string }
type Sale = { id: string; source_id: string | null; source_name: string | null; original_number: string | null; document_date: string | null; covered_from: string | null; covered_to: string | null; counterparty: string | null; description: string | null; include_in_pl: boolean | null; status: 'PENDING'; tax_summaries: Tax[]; files: Original[]; created_at: string; updated_at: string }
type SaleList = { items: Sale[]; total: number; limit: number; offset: number }
type SourceList = { items: Source[]; total: number; limit: number; offset: number }
type SaleDraft = Omit<Sale, 'id' | 'source_name' | 'status' | 'files' | 'created_at' | 'updated_at' | 'tax_summaries'> & { tax_summaries: Tax[] }

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const message = useMessage()
const sources = ref<Source[]>([])
const treatments = ref<Treatment[]>([])
const rates = ref<Rate[]>([])
const sales = ref<Sale[]>([])
const salesTotal = ref(0)
const salesOffset = ref(0)
const salesLimit = 50
const current = ref<Sale | null>(null)
const loading = ref(false)
let refreshGeneration = 0
const error = ref('')
const pickedFiles = ref<File[]>([])
const sourceModal = ref(false)
const editingSource = ref<string | null>(null)
const sourceDraft = reactive({ name: '', kind: 'OTHER' as Source['kind'], default_include_in_pl: null as boolean | null, active: true })
const draft = reactive<SaleDraft>({ source_id: null, original_number: null, document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: null, tax_summaries: [blankTax()] })
const isDetail = computed(() => typeof route.params.id === 'string')
const isNew = computed(() => route.name === 'external-sales-new')
const isEditor = computed(() => isDetail.value || isNew.value)
const editorReady = computed(() => !loading.value && (isDetail.value ? current.value?.id === route.params.id : isNew.value && !current.value))
const sourceOptions = computed(() => sources.value.filter((source) => source.active || source.id === draft.source_id).map((source) => ({ label: `${source.name} (${t(`externalSales.${source.kind}`)})${source.active ? '' : ` · ${t('externalSales.sourceDisabled')}`}`, value: source.id })))
const treatmentOptions = computed(() => treatments.value.filter((row) => row.active && row.side === 'SALES' && row.code === 'NL_DOMESTIC' && row.effect === 'APPLY_RATE' && !row.requires_icp).map((row) => ({ label: `${row.label} (${row.code})`, value: row.id })))
const rateOptions = computed(() => rates.value.filter((row) => row.active).map((row) => ({ label: `${row.label} (${row.percent}%)`, value: row.id })))
const kindOptions = computed(() => (['PLATFORM', 'ENERGY', 'OTHER'] as const).map((kind) => ({ label: t(`externalSales.${kind}`), value: kind })))
const includeOptions = computed(() => [{ label: t('common.yes'), value: 'true' }, { label: t('common.no'), value: 'false' }])

function blankTax(): Tax { return { treatment_id: null, rate_id: null, net_amount: null, vat_amount: null } }
function resetDraft() {
  Object.assign(draft, { source_id: null, original_number: null, document_date: null, covered_from: null, covered_to: null, counterparty: null, description: null, include_in_pl: null, tax_summaries: [blankTax()] })
  current.value = null
  pickedFiles.value = []
}
function loadDraft(sale: Sale) {
  Object.assign(draft, { source_id: sale.source_id, original_number: sale.original_number, document_date: sale.document_date, covered_from: sale.covered_from, covered_to: sale.covered_to, counterparty: sale.counterparty, description: sale.description, include_in_pl: sale.include_in_pl, tax_summaries: sale.tax_summaries.length ? sale.tax_summaries.map(({ treatment_id, rate_id, net_amount, vat_amount }) => ({ treatment_id, rate_id, net_amount: net_amount == null ? null : String(net_amount), vat_amount: vat_amount == null ? null : String(vat_amount) })) : [blankTax()] })
  current.value = sale
}
function updateFiles(sale: Sale, generation: number) {
  if (generation === refreshGeneration && editorReady.value && current.value?.id === sale.id) current.value.files = sale.files
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
      if (generation === refreshGeneration) loadDraft(sale)
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
  if (selected && draft.include_in_pl === null) draft.include_in_pl = selected.default_include_in_pl
})
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
function payload() {
  return { ...draft, document_date: draft.document_date || null, covered_from: draft.covered_from || null, covered_to: draft.covered_to || null, tax_summaries: draft.tax_summaries.map((row) => ({ treatment_id: row.treatment_id || null, rate_id: row.rate_id || null, net_amount: row.net_amount || null, vat_amount: row.vat_amount || null })) }
}
async function saveDraft() {
  if (!editorReady.value) return
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
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
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
  if (!editorReady.value || !current.value) return
  const generation = refreshGeneration
  const id = current.value.id
  try { updateFiles(await del<Sale>(`/api/v1/external-sales/${id}/files/${file.id}`), generation) }
  catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
  }
}
async function discard() {
  if (!editorReady.value || !current.value || !window.confirm(t('externalSales.confirmDiscard'))) return
  const generation = refreshGeneration
  const id = current.value.id
  try {
    await del(`/api/v1/external-sales/${id}`)
    if (generation === refreshGeneration) await router.push('/external-sales')
  } catch (e) {
    if (generation === refreshGeneration) message.error(e instanceof Error ? e.message : t('externalSales.error'))
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
      <n-form label-placement="top" @submit.prevent="saveDraft">
        <div class="grid">
          <n-form-item :label="t('externalSales.source')"><n-select v-model:value="draft.source_id" clearable :options="sourceOptions" /></n-form-item>
          <n-form-item :label="t('externalSales.number')"><n-input v-model:value="draft.original_number" /></n-form-item>
          <n-form-item :label="t('externalSales.date')"><input v-model="draft.document_date" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.counterparty')"><n-input v-model:value="draft.counterparty" /></n-form-item>
          <n-form-item :label="t('externalSales.coveredFrom')"><input v-model="draft.covered_from" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.coveredTo')"><input v-model="draft.covered_to" type="date" /></n-form-item>
          <n-form-item :label="t('externalSales.includePl')"><n-select :value="draft.include_in_pl === null ? null : String(draft.include_in_pl)" clearable :options="includeOptions" @update:value="draft.include_in_pl = $event === null ? null : $event === 'true'" /></n-form-item>
        </div>
        <n-form-item :label="t('externalSales.description')"><n-input v-model:value="draft.description" type="textarea" /></n-form-item>
        <h2>{{ t('externalSales.taxSummaries') }}</h2>
        <div v-for="(row, index) in draft.tax_summaries" :key="index" class="tax-row">
          <n-select v-model:value="row.treatment_id" clearable :options="treatmentOptions" :placeholder="t('externalSales.treatment')" />
          <n-select v-model:value="row.rate_id" clearable :options="rateOptions" :placeholder="t('externalSales.rate')" />
          <n-input v-model:value="row.net_amount" :placeholder="t('externalSales.net')" inputmode="decimal" />
          <n-input v-model:value="row.vat_amount" :placeholder="t('externalSales.vat')" inputmode="decimal" />
          <n-button v-if="draft.tax_summaries.length > 1" @click="draft.tax_summaries.splice(index, 1)">×</n-button>
        </div>
        <n-button size="small" @click="draft.tax_summaries.push(blankTax())">{{ t('externalSales.addTax') }}</n-button>
        <n-form-item class="actions"><n-button type="primary" attr-type="submit" :disabled="!editorReady">{{ t('externalSales.saveDraft') }}</n-button><n-button v-if="current" type="error" :disabled="!editorReady" @click="discard">{{ t('externalSales.discard') }}</n-button></n-form-item>
      </n-form>
      <div v-if="current" class="files">
        <h2>{{ t('externalSales.files') }} ({{ current.files.length }}/5)</h2>
        <div v-for="file in current.files" :key="file.id" class="file-row"><span>{{ file.filename || t('externalSales.none') }} · {{ file.byte_size }} bytes <strong v-if="file.duplicate_warning">{{ t('externalSales.duplicateWarning') }}</strong></span><n-button size="small" :disabled="!editorReady" @click="downloadFile(file)">{{ t('externalSales.download') }}</n-button><n-button size="small" :disabled="!editorReady" @click="removeFile(file)">{{ t('externalSales.removeFile') }}</n-button></div>
        <input type="file" multiple accept="image/png,image/jpeg,image/webp,application/pdf" :disabled="!editorReady || current.files.length >= 5" @change="pickedFiles = Array.from(($event.target as HTMLInputElement).files || []).slice(0, 5 - current!.files.length)" />
        <n-button :disabled="!editorReady || !pickedFiles.length" @click="uploadFiles">{{ t('externalSales.upload') }}</n-button>
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
.error { color: #c33; }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } .tax-row { grid-template-columns: 1fr 1fr; } .sale-row { align-items: flex-start; flex-direction: column; } }
</style>
