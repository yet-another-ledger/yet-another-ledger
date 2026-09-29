<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NCard, NCheckbox, NEmpty, NForm, NFormItem, NInput, NInputNumber, NSelect, NSpin, NSpace, NTag, NUpload } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { ApiError, post } from '../../api/http'
import { useReceiptInboxStore, type Inbox, type InboxStatus } from '../../stores/receiptInbox'
import { toExpenseInput, usePurchaseExpenseDictionaries } from '../../composables/usePurchaseExpenseDictionaries'
import { localDateStr } from '../../utils/date'
import type { components } from '../../api/schema'

type ExpenseInput = components['schemas']['ExpenseInput']
type Calc = components['schemas']['ExpenseCalculateResult']
const route = useRoute(), router = useRouter(), { t, locale } = useI18n()
const inbox = useReceiptInboxStore()
const dict = usePurchaseExpenseDictionaries()
const item = ref<Inbox | null>(null)
const status = ref<InboxStatus>('PENDING')
const offset = ref(0)
const uploadKey = ref(0)
let loadVersion = 0
let previewVersion = 0
let routeVersion = 0
const busy = ref(false), action = ref(''), error = ref(''), success = ref('')
const selectedFiles = ref<File[]>([])
const expenseDate = ref(localDateStr(new Date())), categoryId = ref<string | null>(null), supplier = ref('')
const treatmentId = ref<string | null>(null), rateId = ref<string | null>(null), net = ref<number | null>(null), vat = ref<number | null>(null)
const deductible = ref<boolean | null>(true), paidBy = ref<'BUSINESS' | 'PRIVATE'>('BUSINESS'), businessPct = ref<number>(100), years = ref<number>(1), reference = ref(''), note = ref('')
const previewGross = ref<string | null>(null)
const edited = ref(new Set<string>())
const id = computed(() => String(route.params.id ?? ''))
const isDetail = computed(() => !!id.value)
const pageStart = computed(() => inbox.total ? offset.value + 1 : 0)
const pageEnd = computed(() => Math.min(offset.value + inbox.items.length, inbox.total))
const pending = computed(() => item.value?.status === 'PENDING')
const aiMissing = computed(() => {
  const s = item.value?.ai_suggestion
  return !s || !s.expense_date || !s.supplier_name || s.net_amount == null || s.vat_amount == null || (!s.suggested_category_id && !s.suggested_category_name)
})
function markEdited(field: string) { edited.value.add(field) }
function errorMessage(e: unknown) {
  if (e instanceof ApiError && e.status === 409) {
    const detail = e.detail as { detail?: string } | string | null
    const code = typeof detail === 'string' ? detail : detail?.detail
    if (code && code in i18nErrors) return t(`receiptInbox.errors.${code}`)
  }
  return e instanceof Error ? e.message : String(e)
}
const i18nErrors = { INBOX_NOT_PENDING: true, FILE_LIMIT_REACHED: true, LAST_FILE: true, ALREADY_CONFIRMED: true }

async function load() {
  const version = ++loadVersion
  const targetId = id.value
  busy.value = true; error.value = ''
  try {
    if (targetId) {
      const result = await inbox.detail(targetId)
      if (version === loadVersion) { item.value = result; applySuggestion(result) }
    } else await inbox.fetch(status.value, offset.value)
  } catch (e) { if (version === loadVersion) error.value = errorMessage(e) }
  finally { if (version === loadVersion) busy.value = false }
}
function resetDraft() {
  expenseDate.value = localDateStr(new Date()); categoryId.value = null; supplier.value = ''
  treatmentId.value = null; rateId.value = null; net.value = null; vat.value = null
  deductible.value = true; paidBy.value = 'BUSINESS'; businessPct.value = 100; years.value = 1
  reference.value = ''; note.value = ''; previewGross.value = null
  edited.value = new Set()
  selectedFiles.value = []; uploadKey.value++; previewVersion++
  action.value = ''; error.value = ''; success.value = ''
}
watch(id, () => { ++routeVersion; ++loadVersion; item.value = null; resetDraft(); void load() }, { immediate: true })
onMounted(async () => {
  try { await dict.load() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  if (item.value) applySuggestion(item.value)
})
async function refresh(data?: Inbox, targetId = id.value, version = routeVersion) {
  const result = data ?? (targetId ? await inbox.detail(targetId) : null)
  if (version === routeVersion && targetId === id.value) { item.value = result; if (result) applySuggestion(result) }
}
function changeStatus(next: InboxStatus) { status.value = next; offset.value = 0; void load() }
function changePage(next: number) { offset.value = next; void load() }
function onFiles(info: { fileList: UploadFileInfo[] }) { selectedFiles.value = info.fileList.flatMap(x => x.file ? [x.file] : []) }
async function upload() {
  if (!selectedFiles.value.length) return
  const targetId = id.value
  const version = routeVersion
  action.value = 'upload'; error.value = ''
  try {
    const result = targetId ? await inbox.append(targetId, selectedFiles.value, locale.value) : await inbox.upload(selectedFiles.value, locale.value)
    if (version !== routeVersion) return
    selectedFiles.value = []; uploadKey.value++; item.value = result; applySuggestion(result)
    if (!targetId) await router.push(`/receipt-inbox/${result.id}`)
  } catch (e) { if (version === routeVersion) error.value = errorMessage(e) }
  finally { action.value = '' }
}
async function removeFile(fileId: string) {
  const targetId = id.value
  const version = routeVersion
  action.value = `remove:${fileId}`; error.value = ''
  try { await refresh(await inbox.removeFile(targetId, fileId), targetId, version) } catch (e) { if (version === routeVersion) error.value = errorMessage(e) }
  finally { action.value = '' }
}
async function retry() {
  const targetId = id.value
  const version = routeVersion
  action.value = 'retry'; error.value = ''
  try { await refresh(await inbox.retry(targetId, locale.value), targetId, version) } catch (e) { if (version === routeVersion) error.value = errorMessage(e) }
  finally { action.value = '' }
}
async function discard() {
  const targetId = id.value
  const version = routeVersion
  action.value = 'discard'; error.value = ''
  try { await inbox.discard(targetId); if (version === routeVersion) await router.push('/receipt-inbox') } catch (e) { if (version === routeVersion) error.value = errorMessage(e) }
  finally { action.value = '' }
}
function applySuggestion(source: Inbox) {
  const s = source.ai_suggestion
  if (!s) return
  if (!edited.value.has('expenseDate') && s.expense_date) expenseDate.value = s.expense_date
  if (!edited.value.has('supplier') && s.supplier_name) supplier.value = s.supplier_name
  if (!edited.value.has('net') && s.net_amount != null) net.value = Number(s.net_amount)
  if (!edited.value.has('vat') && s.vat_amount != null) vat.value = Number(s.vat_amount)
  if (!edited.value.has('categoryId')) {
    const suggestedCategory = s.suggested_category_id ?? (s.suggested_category_name ? dict.categories.value.find(x => x.name.toLowerCase() === s.suggested_category_name!.toLowerCase())?.id : undefined)
    if (suggestedCategory) categoryId.value = suggestedCategory
    if (categoryId.value && !edited.value.has('deductible')) onCategoryChange(categoryId.value)
  }
  if (!edited.value.has('rateId') && s.vat_rate_percent) {
    const suggestedRate = dict.rates.value.find(x => Math.abs(Number(x.percent) - Number(s.vat_rate_percent)) < 0.01)?.id
    if (suggestedRate) rateId.value = suggestedRate
  }
  if (!edited.value.has('note') && s.summary) note.value = s.summary
  previewVersion++; previewGross.value = null
}
function onCategoryChange(newId: string | null) {
  const category = dict.categories.value.find(x => x.id === newId)
  if (!edited.value.has('deductible')) deductible.value = category?.default_deductible ?? true
}
function onVatChange() { previewVersion++; previewGross.value = null }
async function preview() {
  const version = ++previewVersion
  const targetId = id.value
  const amount = net.value, rate = rateId.value
  previewGross.value = null
  if (net.value == null || !rateId.value) { previewGross.value = null; return }
  try {
    const result = await post<Calc>('/api/v1/expenses/calculate', { net_amount: amount, vat_rate_id: rate })
    if (version === previewVersion && targetId === id.value && amount === net.value && rate === rateId.value) {
      vat.value = Number(result.vat_amount); previewGross.value = result.gross_amount
    }
  } catch (e) { if (version === previewVersion && targetId === id.value) error.value = e instanceof Error ? e.message : String(e) }
}
async function confirmExpense() {
  if (!categoryId.value || !treatmentId.value || !rateId.value || net.value == null || vat.value == null) { error.value = t('receiptInbox.validation'); return }
  const targetId = id.value
  const version = routeVersion
  action.value = 'confirm'; error.value = ''; success.value = ''
  const body: ExpenseInput = toExpenseInput({ expense_date: expenseDate.value, category_id: categoryId.value, supplier_name: supplier.value || null, vat_treatment_id: treatmentId.value, vat_rate_id: rateId.value, net_amount: net.value, vat_amount: vat.value, deductible: deductible.value, reference: reference.value || null, note: note.value || null, paid_by: paidBy.value, business_percentage: businessPct.value, depreciation_years: years.value })
  try {
    const result = await inbox.confirm(targetId, body)
    if (version !== routeVersion) return
    item.value = result.inbox; success.value = t('receiptInbox.confirmed')
    await router.push(`/expenses/${result.expense.id}/edit?tab=purchase`)
  } catch (e) {
    if (version !== routeVersion) return
    if (e instanceof ApiError && e.status === 409) {
      const expenseId = (e.detail as { expense_id?: string } | null)?.expense_id
      if (expenseId) { await refresh(undefined, targetId, version); if (version === routeVersion && item.value) { item.value.expense_id = expenseId; error.value = t('receiptInbox.alreadyConfirmed') } }
      else error.value = t('receiptInbox.conflict')
    } else error.value = errorMessage(e)
  } finally { action.value = '' }
}
function fileUrl(fileId: string) { return `/api/v1/attachments/${fileId}/content` }
function formatBytes(n: number) { return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB` }
</script>

<template>
  <main class="inbox-page">
    <header><h1>{{ t('receiptInbox.title') }}</h1><n-button v-if="isDetail" @click="router.push('/receipt-inbox')">{{ t('receiptInbox.back') }}</n-button></header>
    <n-alert v-if="error" type="error" closable role="alert">{{ error }} <router-link v-if="error && item?.expense_id" :to="`/expenses/${item.expense_id}/edit?tab=purchase`">{{ t('receiptInbox.openExpense') }}</router-link></n-alert>
    <n-alert v-if="success" type="success" closable>{{ success }}</n-alert>
    <n-spin :show="busy">
      <template v-if="!isDetail">
        <n-space class="tabs"><n-button :type="status === 'PENDING' ? 'primary' : 'default'" @click="changeStatus('PENDING')">{{ t('receiptInbox.pending') }}</n-button><n-button :type="status === 'CONFIRMED' ? 'primary' : 'default'" @click="changeStatus('CONFIRMED')">{{ t('receiptInbox.processed') }}</n-button></n-space>
        <n-alert v-if="status === 'PENDING'" type="info" class="notice">{{ t('receiptInbox.providerNotice') }}</n-alert>
        <template v-if="status === 'PENDING'">
          <n-upload :key="uploadKey" multiple :max="5" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFiles"><n-button>{{ t('receiptInbox.chooseFiles') }}</n-button></n-upload>
          <p v-if="selectedFiles.length">{{ selectedFiles.map(f => f.name).join(', ') }}</p>
          <n-button type="primary" :disabled="!selectedFiles.length || action==='upload'" @click="upload">{{ action==='upload' ? t('receiptInbox.uploading') : t('receiptInbox.upload') }}</n-button>
        </template>
        <n-empty v-if="!inbox.items.length && !busy" :description="t('receiptInbox.empty')" />
        <div class="inbox-list">
          <router-link v-for="row in inbox.items" :key="row.id" class="inbox-card" :to="`/receipt-inbox/${row.id}`">
            <n-card hoverable class="inbox-item">
              <div class="inbox-item-heading">
                <strong :title="row.files[0]?.filename || t('receiptInbox.unnamed')">{{ row.files[0]?.filename || t('receiptInbox.unnamed') }}</strong>
                <time :datetime="row.created_at">{{ new Date(row.created_at).toLocaleString() }}</time>
              </div>
              <div class="inbox-item-footer">
                <n-tag :type="row.ai_state === 'READY' ? 'success' : row.ai_state === 'FAILED' ? 'error' : 'default'">{{ t(`receiptInbox.ai.${row.ai_state}`) }}</n-tag>
                <span class="file-count">{{ row.files.length }} {{ t(row.files.length === 1 ? 'receiptInbox.file' : 'receiptInbox.files') }}</span>
              </div>
            </n-card>
          </router-link>
        </div>
        <n-space v-if="inbox.total > 100" class="pagination"><n-button :disabled="offset === 0 || busy" @click="changePage(Math.max(0, offset-100))">{{ t('receiptInbox.previousPage') }}</n-button><span>{{ t('receiptInbox.pageRange', { start: pageStart, end: pageEnd, total: inbox.total }) }}</span><n-button :disabled="offset+100 >= inbox.total || busy" @click="changePage(offset+100)">{{ t('receiptInbox.nextPage') }}</n-button></n-space>
      </template>
      <template v-else-if="item">
        <h2>{{ item.files[0]?.filename || t('receiptInbox.unnamed') }}</h2>
        <n-tag>{{ item.status === 'PENDING' ? t('receiptInbox.pending') : t('receiptInbox.processed') }}</n-tag>
        <p>{{ t(`receiptInbox.ai.${item.ai_state}`) }}<span v-if="item.ai_error_code"> · {{ t(`receiptInbox.errors.${item.ai_error_code}`) }}</span></p>
        <n-alert v-if="pending" type="info" class="notice">{{ t('receiptInbox.providerNotice') }}</n-alert>
        <n-alert v-if="pending && aiMissing" type="warning">{{ t('receiptInbox.missingFields') }}</n-alert>
        <n-space v-if="pending" class="actions"><n-button :disabled="action==='retry'" @click="retry">{{ action==='retry' ? t('receiptInbox.retrying') : t('receiptInbox.retry') }}</n-button><n-button type="error" :disabled="action==='discard'" @click="discard">{{ t('receiptInbox.discard') }}</n-button></n-space>
        <section class="files"><n-card v-for="file in item.files" :key="file.id" :title="file.filename || t('receiptInbox.unnamed')" size="small">
          <p class="file-meta">{{ file.mime_type }} · {{ formatBytes(file.byte_size) }}</p><img v-if="file.mime_type.startsWith('image/')" :src="fileUrl(file.id)" :alt="file.filename || t('receiptInbox.receiptPreview')" /><iframe v-else-if="file.mime_type==='application/pdf'" :src="fileUrl(file.id)" :title="file.filename || t('receiptInbox.receiptPreview')" />
          <div class="file-actions">
            <n-button tag="a" type="primary" :href="fileUrl(file.id)" :download="file.filename || 'receipt'">{{ t('receipt.download') }}</n-button>
            <n-button v-if="pending" type="error" :disabled="action===`remove:${file.id}`" @click="removeFile(file.id)">{{ action===`remove:${file.id}` ? t('receiptInbox.removing') : t('receipt.delete') }}</n-button>
          </div>
        </n-card></section>
        <div v-if="pending" class="append"><n-upload :key="uploadKey" multiple :max="Math.max(0, 5-item.files.length)" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFiles"><n-button :disabled="item.files.length>=5">{{ t('receiptInbox.addFiles') }}</n-button></n-upload><n-button type="primary" :disabled="!selectedFiles.length || action==='upload'" @click="upload">{{ action==='upload' ? t('receiptInbox.uploading') : t('receiptInbox.append') }}</n-button></div>
        <n-card v-if="pending" :title="t('receiptInbox.reviewTitle')" class="review">
          <n-form label-placement="top" @submit.prevent="confirmExpense">
            <n-form-item :label="t('expenses.expenseDate')"><input v-model="expenseDate" type="date" @input="markEdited('expenseDate')" /></n-form-item>
            <n-form-item :label="t('expenses.category')"><n-select v-model:value="categoryId" :options="dict.categoryOptions.value" filterable @update:value="(v) => { markEdited('categoryId'); onCategoryChange(v) }" /></n-form-item>
            <n-form-item :label="t('expenses.supplier')"><n-input v-model:value="supplier" @update:value="markEdited('supplier')" /></n-form-item>
            <n-form-item :label="t('expenses.vatTreatment')"><n-select v-model:value="treatmentId" :options="dict.purchaseTreatmentOptions.value" /></n-form-item>
            <n-form-item :label="t('expenses.vatRate')"><n-select v-model:value="rateId" :options="dict.vatRateOptions.value" @update:value="(v) => { markEdited('rateId'); rateId=v; preview() }" /></n-form-item>
            <n-form-item :label="t('expenses.netAmount')"><n-input-number v-model:value="net" :min="0" :precision="2" @update:value="(v) => { markEdited('net'); net=v; preview() }" /></n-form-item>
            <n-form-item :label="t('expenses.vatAmount')"><n-input-number v-model:value="vat" :min="0" :precision="2" @update:value="(v) => { markEdited('vat'); vat=v; onVatChange() }" /></n-form-item>
            <n-form-item :label="t('expenses.grossAmount')"><strong>{{ previewGross ?? t('expenses.grossHint') }}</strong></n-form-item>
            <n-form-item :label="t('expenses.deductible')"><n-checkbox :checked="deductible ?? true" @update:checked="($event) => { markEdited('deductible'); deductible=$event }">{{ t('expenses.deductibleLabel') }}</n-checkbox></n-form-item>
            <n-form-item :label="t('expenses.paidBy')"><n-select v-model:value="paidBy" :options="[{label:t('expenses.paidByBusiness'),value:'BUSINESS'},{label:t('expenses.paidByPrivate'),value:'PRIVATE'}]" /></n-form-item>
            <n-form-item :label="t('expenses.businessPercentage')"><n-input-number v-model:value="businessPct" :min="0" :max="100" :precision="1" /></n-form-item>
            <n-form-item :label="t('expenses.depreciationYears')"><n-input-number v-model:value="years" :min="1" :precision="0" /></n-form-item>
            <n-form-item :label="t('expenses.reference')"><n-input v-model:value="reference" /></n-form-item>
            <n-form-item :label="t('expenses.note')"><n-input v-model:value="note" type="textarea" @update:value="markEdited('note')" /></n-form-item>
            <n-button v-if="action==='confirm'" type="primary" loading>{{ t('receiptInbox.confirming') }}</n-button><n-button v-else type="primary" attr-type="submit">{{ t('receiptInbox.confirm') }}</n-button>
          </n-form>
        </n-card>
        <n-card v-else :title="t('receiptInbox.processed')"><p v-if="item.confirmed_at">{{ new Date(item.confirmed_at).toLocaleString() }}</p><router-link v-if="item.expense_id" :to="`/expenses/${item.expense_id}/edit?tab=purchase`">{{ t('receiptInbox.openExpense') }}</router-link><p v-else>{{ t('receiptInbox.expenseUnavailable') }}</p></n-card>
      </template>
    </n-spin>
  </main>
</template>

<style scoped>
.inbox-page{max-width:1100px;margin:auto;padding:24px}
.inbox-page header,.tabs,.actions,.append{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.inbox-page header{justify-content:space-between}
.notice{margin:16px 0}
.inbox-list,.files{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin:18px 0}
.inbox-card{display:block;min-width:0;color:inherit;text-decoration:none}
.inbox-item{height:100%}
.inbox-item :deep(.n-card__content){display:flex;min-height:112px;flex-direction:column;gap:18px}
.inbox-item-heading{display:flex;min-width:0;flex-direction:column;gap:8px}
.inbox-item-heading strong{overflow-wrap:anywhere;line-height:1.4}
.inbox-item-heading time{color:var(--n-text-color-3);font-size:13px}
.inbox-item-footer{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:auto;padding-top:14px;border-top:1px solid var(--n-border-color)}
.file-count{color:var(--n-text-color-3);white-space:nowrap}
.file-meta{margin:0 0 12px;color:var(--n-text-color-3)}
.files img{display:block;max-width:100%;max-height:480px;object-fit:contain}
.files iframe{display:block;width:100%;height:380px;border:0}
.file-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px}
.review{max-width:760px;margin-top:24px}
.review :deep(.n-input-number){width:100%}
.review input[type=date]{width:100%;padding:8px;border:1px solid var(--n-border-color);border-radius:4px}
@media(max-width:640px){.inbox-page{padding:14px}.files{grid-template-columns:1fr}}
</style>
