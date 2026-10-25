<script setup lang="ts">
import { onMounted, onBeforeUnmount, computed, h, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  useMessage, useDialog,
  NButton, NSpace, NInput, NDataTable, NAlert, NSpin,
  NPagination, NSelect, NTag, NDropdown,
} from 'naive-ui'
import { AddOutline, SearchOutline, CreateOutline, TrashOutline, DownloadOutline, MailOutline, EyeOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import DocumentSendDialog from '../../components/DocumentSendDialog.vue'
import PdfPreviewDialog from '../../components/PdfPreviewDialog.vue'
import { useQuotesStore } from '../../stores/quotes'
import type { QuoteListItem } from '../../stores/quotes'
import { get, downloadBlob } from '../../api/http'
import type { components } from '../../api/schema'

type CustomerRead = components['schemas']['CustomerRead']
type EmailLogRead = components['schemas']['EmailLogRead']

const router = useRouter()
const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const store = useQuotesStore()
const duplicatingId = ref<string | null>(null)

// Send dialog state
const sendDialogShow = ref(false)
const sendDialogQuoteId = ref('')
const sendDialogCustomerEmail = ref<string | null>(null)
const sendDialogCustomerLocale = ref<'en' | 'zh' | null>(null)

async function openSendDialog(row: QuoteListItem) {
  sendDialogQuoteId.value = row.id
  sendDialogCustomerEmail.value = null
  sendDialogCustomerLocale.value = null
  sendDialogShow.value = true
  try {
    const cust = await get<CustomerRead>(`/api/v1/customers/${row.customer_id}`)
    sendDialogCustomerEmail.value = cust.email ?? null
    sendDialogCustomerLocale.value = (cust.locale as 'en' | 'zh' | null | undefined) ?? null
  } catch {
    // Non-critical: dialog pre-fill is best-effort
  }
}

function handleSent(_log: EmailLogRead) {
  // Log was created; no further action needed in list view
}

// PDF download
const downloadingId = ref<string | null>(null)

async function handleDownloadPdf(id: string, locale?: 'en' | 'zh') {
  downloadingId.value = id
  try {
    const url = locale
      ? `/api/v1/quotes/${id}/pdf?locale=${locale}`
      : `/api/v1/quotes/${id}/pdf`
    await downloadBlob(url, `quote-${id}.pdf`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('pdf.downloadFailed'))
  } finally {
    downloadingId.value = null
  }
}

function pdfLocaleOptions(id: string) {
  return [
    { label: t('pdf.localeDefault'), key: `${id}:default` },
    { label: t('pdf.localeEn'), key: `${id}:en` },
    { label: t('pdf.localeZh'), key: `${id}:zh` },
  ]
}

function handlePdfLocaleSelect(key: string) {
  const [id, locale] = key.split(':')
  if (locale === 'default') {
    handleDownloadPdf(id)
  } else {
    handleDownloadPdf(id, locale as 'en' | 'zh')
  }
}

// PDF preview (in-app modal)
const previewShow = ref(false)
const previewSrc = ref<string | null>(null)
const previewFallback = ref('quote.pdf')

function openPreview(id: string, locale?: 'en' | 'zh') {
  previewSrc.value = locale
    ? `/api/v1/quotes/${id}/pdf?locale=${locale}`
    : `/api/v1/quotes/${id}/pdf`
  previewFallback.value = `quote-${id}.pdf`
  previewShow.value = true
}

function handlePreviewLocaleSelect(key: string) {
  const [id, locale] = key.split(':')
  if (locale === 'default') {
    openPreview(id)
  } else {
    openPreview(id, locale as 'en' | 'zh')
  }
}

const customers = ref<CustomerRead[]>([])

async function searchCustomers(q: string) {
  const res = await get<{ items: CustomerRead[] }>(`/api/v1/customers?q=${encodeURIComponent(q)}&limit=30`)
  customers.value = res.items
}

const customerOptions = computed(() => customers.value.map(c => ({ label: c.name, value: c.id })))

function handleCustomerFilter(val: string | null) {
  store.customerIdFilter = val
  store.offset = 0
  store.fetchQuotes()
}

onMounted(() => {
  store.fetchQuotes()
  searchCustomers('')
})

let searchTimer: ReturnType<typeof setTimeout> | null = null

function handleSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    store.offset = 0
    store.fetchQuotes()
  }, 300)
}

function handleSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  store.offset = 0
  store.fetchQuotes()
}

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

function handlePageChange(page: number) {
  store.offset = (page - 1) * store.limit
  store.fetchQuotes()
}

function handleCreate() {
  router.push('/quotes/new')
}

function handleEdit(id: string) {
  router.push(`/quotes/${id}/edit`)
}

async function handleDuplicate(id: string) {
  if (duplicatingId.value) return
  duplicatingId.value = id
  try {
    const duplicate = await store.duplicateQuote(id)
    message.success(t('quotes.duplicateSuccess'))
    await router.push(`/quotes/${duplicate.id}/edit`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('quotes.duplicateFailed'))
  } finally {
    duplicatingId.value = null
  }
}

function handleDelete(quoteNumber: string, id: string) {
  dialog.warning({
    title: t('quotes.delete'),
    content: t('quotes.deleteConfirm', { number: quoteNumber }),
    positiveText: t('quotes.delete'),
    negativeText: t('quotes.cancel'),
    onPositiveClick: async () => {
      try {
        await store.deleteQuote(id)
        message.success(t('quotes.deleteSuccess'))
        store.fetchQuotes()
      } catch {
        message.error(t('quotes.deleteFailed'))
      }
    },
  })
}

const currentPage = computed(() => Math.floor(store.offset / store.limit) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(store.total / store.limit)))

const sortOptions = computed(() => [
  { label: t('quotes.sortByDate'), value: 'quote_date' },
  { label: t('quotes.sortByCreatedAt'), value: 'created_at' },
  { label: t('quotes.sortByNumber'), value: 'quote_number' },
])

const statusOptions = computed(() => [
  { label: t('quotes.statusDRAFT'), value: 'DRAFT' },
  { label: t('quotes.statusSENT'), value: 'SENT' },
  { label: t('quotes.statusACCEPTED'), value: 'ACCEPTED' },
  { label: t('quotes.statusREJECTED'), value: 'REJECTED' },
  { label: t('quotes.statusEXPIRED'), value: 'EXPIRED' },
])

function handleSortChange(val: 'quote_date' | 'created_at' | 'quote_number') {
  store.sortBy = val
  store.offset = 0
  store.fetchQuotes()
}

function handleStatusFilter(val: string | null) {
  store.statusFilter = val
  store.offset = 0
  store.fetchQuotes()
}

function statusTagType(status: string): 'default' | 'info' | 'success' | 'warning' | 'error' {
  const map: Record<string, 'default' | 'info' | 'success' | 'warning' | 'error'> = {
    DRAFT: 'default',
    SENT: 'info',
    ACCEPTED: 'success',
    REJECTED: 'error',
    EXPIRED: 'warning',
  }
  return map[status] ?? 'default'
}

const columns = computed(() => [
  {
    title: t('quotes.quoteNumber'),
    key: 'quote_number',
    width: 140,
  },
  {
    title: t('quotes.customer'),
    key: 'customer_name',
    ellipsis: { tooltip: true },
  },
  {
    title: t('quotes.quoteDate'),
    key: 'quote_date',
    width: 110,
  },
  {
    title: t('quotes.validUntil'),
    key: 'valid_until',
    width: 110,
    render(row: QuoteListItem) {
      return row.valid_until ?? '—'
    },
  },
  {
    title: t('quotes.status'),
    key: 'status',
    width: 100,
    render(row: QuoteListItem) {
      return h(NTag, { type: statusTagType(row.status), size: 'small' }, () => t(`quotes.status${row.status}`))
    },
  },
  {
    title: t('quotes.totalInclVat'),
    key: 'total_incl_vat',
    align: 'right' as const,
    width: 120,
    render(row: QuoteListItem) {
      return h('span', {}, `${row.currency} ${Number(row.total_incl_vat).toFixed(2)}`)
    },
  },
  {
    title: t('quotes.actions'),
    key: 'actions',
    width: 270,
    align: 'center' as const,
    render(row: QuoteListItem) {
      const canDelete = row.status !== 'ACCEPTED'
      return h(NSpace, { size: 4, justify: 'center', wrapItem: false }, () => [
        h(
          NButton,
          {
            size: 'small',
            quaternary: true,
            circle: true,
            title: t('quotes.edit'),
            onClick: () => handleEdit(row.id),
          },
          () => h(NIcon, null, { default: () => h(CreateOutline) }),
        ),
        h(NButton, {
          size: 'small', loading: duplicatingId.value === row.id,
          disabled: duplicatingId.value !== null,
          onClick: () => handleDuplicate(row.id),
        }, () => t('quotes.duplicate')),
        // PDF preview dropdown
        h(
          NDropdown,
          {
            options: pdfLocaleOptions(row.id),
            trigger: 'click',
            onSelect: handlePreviewLocaleSelect,
          },
          () => h(
            NButton,
            {
              size: 'small',
              quaternary: true,
              circle: true,
              title: t('pdf.preview'),
            },
            () => h(NIcon, null, { default: () => h(EyeOutline) }),
          ),
        ),
        // PDF download dropdown
        h(
          NDropdown,
          {
            options: pdfLocaleOptions(row.id),
            trigger: 'click',
            onSelect: handlePdfLocaleSelect,
          },
          () => h(
            NButton,
            {
              size: 'small',
              quaternary: true,
              circle: true,
              title: t('pdf.download'),
              loading: downloadingId.value === row.id,
              disabled: downloadingId.value === row.id,
            },
            () => h(NIcon, null, { default: () => h(DownloadOutline) }),
          ),
        ),
        // Send email button
        h(
          NButton,
          {
            size: 'small',
            quaternary: true,
            circle: true,
            title: t('sendDialog.title'),
            onClick: () => openSendDialog(row),
          },
          () => h(NIcon, null, { default: () => h(MailOutline) }),
        ),
        ...(canDelete ? [h(
          NButton,
          {
            size: 'small',
            quaternary: true,
            circle: true,
            type: 'error',
            title: t('quotes.delete'),
            onClick: () => handleDelete(row.quote_number, row.id),
          },
          () => h(NIcon, null, { default: () => h(TrashOutline) }),
        )] : []),
      ])
    },
  },
])
</script>

<template>
  <div class="quote-list-page">
    <div class="quote-list-container">
          <div class="quote-list-header">
            <h2>{{ t('quotes.title') }}</h2>
            <n-space align="center" wrap>
              <n-input
                v-model:value="store.query"
                :placeholder="t('quotes.search')"
                clearable
                style="width: 200px"
                @input="handleSearchInput"
                @keyup.enter="handleSearch"
                @clear="handleSearch"
              >
                <template #prefix>
                  <n-icon><SearchOutline /></n-icon>
                </template>
              </n-input>
              <n-select
                :value="store.customerIdFilter"
                :options="customerOptions"
                :placeholder="t('quotes.customerAll')"
                style="width: 160px"
                filterable
                clearable
                @search="searchCustomers"
                @update:value="handleCustomerFilter"
              />
              <n-select
                :value="store.statusFilter"
                :options="statusOptions"
                :placeholder="t('quotes.statusAll')"
                style="width: 130px"
                clearable
                @update:value="handleStatusFilter"
              />
              <n-select
                :value="store.sortBy"
                :options="sortOptions"
                style="width: 140px"
                @update:value="handleSortChange"
              />
              <n-button type="primary" @click="handleCreate">
                <template #icon>
                  <n-icon><AddOutline /></n-icon>
                </template>
                {{ t('quotes.create') }}
              </n-button>
            </n-space>
          </div>

          <n-alert v-if="store.error" type="error" style="margin-bottom: 16px">
            {{ store.error }}
          </n-alert>

          <n-spin :show="store.loading">
            <template v-if="!store.loading && store.items.length === 0">
              <n-empty
                v-if="store.total === 0 && !store.query && !store.statusFilter && !store.customerIdFilter"
                :description="t('quotes.empty')"
              />
              <n-empty v-else :description="t('quotes.noResults')" />
            </template>

            <n-data-table
              v-else
              :columns="columns"
              :data="store.items"
              :bordered="false"
              :row-key="(row: QuoteListItem) => row.id"
              striped
            />

            <div v-if="store.total > store.limit" class="pagination-container">
              <n-pagination
                :page="currentPage"
                :page-count="pageCount"
                @update:page="handlePageChange"
              />
            </div>
          </n-spin>
    </div>

    <!-- Send email dialog -->
    <DocumentSendDialog
      v-model:show="sendDialogShow"
      doc-type="quote"
      :doc-id="sendDialogQuoteId"
      :customer-email="sendDialogCustomerEmail"
      :customer-locale="sendDialogCustomerLocale"
      @sent="handleSent"
    />

    <!-- PDF preview dialog -->
    <PdfPreviewDialog
      v-model:show="previewShow"
      :src="previewSrc"
      :fallback-filename="previewFallback"
    />
  </div>
</template>

<style scoped>

.quote-list-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

.quote-list-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}

.quote-list-header h2 {
  margin: 0;
}

.pagination-container {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
