<script setup lang="ts">
/**
 * InvoicePaymentPanel – shows the payment aggregate for a single invoice,
 * allows recording new payments and editing/deleting existing ones.
 *
 * Design rules (M7 §步骤4):
 * - All amounts/status come exclusively from the backend aggregate (InvoicePaymentsResponse).
 *   Frontend never locally computes due_amount or paid_status.
 * - Form only sends raw input (payment_date, amount, payment_method_id, note, reference).
 * - DRAFT/CANCELLED invoices: panel is visible but record-payment form is hidden
 *   with an info alert telling the user to mark the invoice as sent first.
 * - Loading-prop + v-if bug avoidance:
 *   Action buttons that need a :loading prop use v-if/v-else to swap between
 *   a static "loading disabled" button and the real clickable button, so that
 *   production vite build does not silently drop @click (see memory: vue-loading-prop-vif-prod-bug).
 */

import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  useMessage, useDialog,
  NCard, NSpace, NTag, NText, NButton, NForm, NFormItem,
  NInputNumber, NSelect, NInput, NDatePicker, NAlert, NSpin,
  NDivider, NIcon, NEmpty, NDropdown,
} from 'naive-ui'
import { AddOutline, CreateOutline, TrashOutline, DownloadOutline, EyeOutline, SendOutline } from '@vicons/ionicons5'
import { usePaymentsStore } from '../stores/payments'
import type { InvoicePaymentsResponse, PaymentRead } from '../stores/payments'
import { get, downloadBlob } from '../api/http'
import PdfPreviewDialog from './PdfPreviewDialog.vue'
import DocumentSendDialog from './DocumentSendDialog.vue'
import type { components } from '../api/schema'
import { localDateStr, formatDate } from '../utils/date'
import { isPaymentMutationBusy } from '../utils/quotePaymentPanelState'
import { openReceiptDialog } from '../utils/receiptEmail'
import PaymentTaxBreakdown from './PaymentTaxBreakdown.vue'

type PaymentMethodRead = components['schemas']['PaymentMethodRead']
type PaymentMethodListResponse = components['schemas']['PaymentMethodListResponse']

const props = defineProps<{
  invoiceId: string
  invoiceStatus: string
  invoiceVersion?: string
  customerEmail?: string | null
  customerLocale?: 'en' | 'zh' | null
}>()

const emit = defineEmits<{
  (e: 'paymentsChanged', aggregate: InvoicePaymentsResponse): void
  (e: 'receiptSent', log: components['schemas']['EmailLogRead']): void
}>()

const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const store = usePaymentsStore()

// ---- state ----
const aggregate = ref<InvoicePaymentsResponse | null>(null)
const panelLoading = ref(false)
const aggregateError = ref(false)
const paymentMethods = ref<PaymentMethodRead[]>([])

// Form fields – only raw inputs per M7 design
const formDate = ref(localDateStr(new Date()))
const formAmount = ref<number | null>(null)
const formMethodId = ref<string | null>(null)
const formNote = ref<string | null>(null)
const formReference = ref<string | null>(null)
const showForm = ref(false)

// Edit state
const editingPayment = ref<PaymentRead | null>(null)
const editDate = ref('')
const editAmount = ref<number | null>(null)
const editMethodId = ref<string | null>(null)
const editNote = ref<string | null>(null)
const editReference = ref<string | null>(null)

// Saving flags feed one panel-wide mutation interlock.
const recordSaving = ref(false)
const editSaving = ref(false)
const deleteSaving = ref<string | null>(null) // stores the payment id being deleted

// ---- Receipt PDF download ----
const receiptDownloadingId = ref<string | null>(null)
const receiptSendPaymentId = ref<string | null>(null)
const receiptSendShow = ref(false)
const receiptSending = ref(false)

function receiptPdfLocaleOptions(paymentId: string) {
  return [
    { label: t('pdf.localeDefault'), key: `${paymentId}:default` },
    { label: t('pdf.localeEn'), key: `${paymentId}:en` },
    { label: t('pdf.localeZh'), key: `${paymentId}:zh` },
  ]
}

async function handleDownloadReceipt(paymentId: string, locale?: 'en' | 'zh') {
  receiptDownloadingId.value = paymentId
  try {
    const url = locale
      ? `/api/v1/payments/${paymentId}/receipt-pdf?locale=${locale}`
      : `/api/v1/payments/${paymentId}/receipt-pdf`
    await downloadBlob(url, `receipt-${paymentId}.pdf`)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('pdf.downloadFailed'))
  } finally {
    receiptDownloadingId.value = null
  }
}

function handleReceiptLocaleSelect(key: string) {
  const [id, locale] = key.split(':')
  if (locale === 'default') {
    handleDownloadReceipt(id)
  } else {
    handleDownloadReceipt(id, locale as 'en' | 'zh')
  }
}

// ---- Receipt PDF preview (in-app modal) ----
const receiptPreviewShow = ref(false)
const receiptPreviewSrc = ref<string | null>(null)
const receiptPreviewFallback = ref('receipt.pdf')

function openReceiptPreview(paymentId: string, locale?: 'en' | 'zh') {
  receiptPreviewSrc.value = locale
    ? `/api/v1/payments/${paymentId}/receipt-pdf?locale=${locale}`
    : `/api/v1/payments/${paymentId}/receipt-pdf`
  receiptPreviewFallback.value = `receipt-${paymentId}.pdf`
  receiptPreviewShow.value = true
}

function handleReceiptPreviewLocaleSelect(key: string) {
  const [id, locale] = key.split(':')
  if (locale === 'default') {
    openReceiptPreview(id)
  } else {
    openReceiptPreview(id, locale as 'en' | 'zh')
  }
}

function openReceiptSend(paymentId: string) {
  const next = openReceiptDialog(paymentId, receiptSendPaymentId.value, receiptSending.value)
  if (!next) return
  receiptSendPaymentId.value = next.paymentId
  receiptSendShow.value = next.show
}

function handleReceiptSent(log: components['schemas']['EmailLogRead']) {
  emit('receiptSent', log)
}

// ---- computed ----
const invoiceAllowsRecording = computed(() =>
  props.invoiceStatus === 'SENT' || props.invoiceStatus === 'COMPLETED',
)

const mutationBusy = computed(() => isPaymentMutationBusy(
  recordSaving.value,
  editSaving.value,
  deleteSaving.value,
))

const canRecord = computed(() => invoiceAllowsRecording.value && !mutationBusy.value)

function invoiceAllowsManaging(payment: PaymentRead): boolean {
  return invoiceAllowsRecording.value || (
    props.invoiceStatus === 'DRAFT' && payment.origin_type === 'QUOTE'
  )
}

function canManage(payment: PaymentRead): boolean {
  return invoiceAllowsManaging(payment) && !mutationBusy.value
}

const paymentMethodOptions = computed(() =>
  paymentMethods.value
    .filter(m => m.active)
    .map(m => ({ label: m.name, value: m.id })),
)

const fmtMoney = (v: string | number) => Number(v).toFixed(2)

function paidStatusType(status: string): 'default' | 'warning' | 'success' {
  if (status === 'PAID') return 'success'
  if (status === 'PARTIALLY_PAID') return 'warning'
  return 'default'
}

// ---- load ----
async function loadPaymentMethods() {
  const res = await get<PaymentMethodListResponse>('/api/v1/payment-methods')
  paymentMethods.value = res.items
}

let aggregateGeneration = 0
async function loadAggregate() {
  const generation = ++aggregateGeneration
  panelLoading.value = true
  try {
    const result = await store.listInvoicePayments(props.invoiceId)
    if (generation === aggregateGeneration) {
      aggregate.value = result
      aggregateError.value = false
    }
  } catch {
    if (generation === aggregateGeneration) aggregateError.value = true
  } finally {
    if (generation === aggregateGeneration) panelLoading.value = false
  }
}

watch(() => [props.invoiceId, props.invoiceVersion] as const, ([id], previous) => {
  if (previous && id !== previous[0]) aggregate.value = null
  void loadAggregate()
})

onMounted(async () => {
  await Promise.all([loadAggregate(), loadPaymentMethods()])
})

// ---- record payment ----
function openForm() {
  if (!canRecord.value) return
  formDate.value = localDateStr(new Date())
  formAmount.value = null
  formMethodId.value = null
  formNote.value = null
  formReference.value = null
  showForm.value = true
}

function cancelForm() {
  showForm.value = false
}

async function handleRecord() {
  if (!canRecord.value) return
  if (!formAmount.value || formAmount.value <= 0) {
    message.warning(t('payments.amountRequired'))
    return
  }
  recordSaving.value = true
  try {
    const result = await store.recordPayment(props.invoiceId, {
      payment_date: formDate.value,
      amount: formAmount.value,
      payment_method_id: formMethodId.value,
      note: formNote.value,
      reference: formReference.value,
    })
    aggregate.value = result
    emit('paymentsChanged', result)
    showForm.value = false
    message.success(t('payments.recordSuccess'))
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('payments.recordFailed'))
  } finally {
    recordSaving.value = false
  }
}

// ---- edit payment ----
function openEdit(payment: PaymentRead) {
  if (!canManage(payment)) return
  editingPayment.value = payment
  editDate.value = payment.payment_date
  editAmount.value = Number(payment.amount)
  editMethodId.value = payment.payment_method_id ?? null
  editNote.value = payment.note ?? null
  editReference.value = payment.reference ?? null
}

function cancelEdit() {
  editingPayment.value = null
}

async function handleEdit() {
  const payment = editingPayment.value
  if (!payment || !canManage(payment)) return
  if (!editAmount.value || editAmount.value <= 0) {
    message.warning(t('payments.amountRequired'))
    return
  }
  editSaving.value = true
  try {
    const result = await store.updatePayment(payment.id, {
      payment_date: editDate.value,
      amount: editAmount.value,
      payment_method_id: editMethodId.value,
      note: editNote.value,
      reference: editReference.value,
    })
    if (!result.invoice) throw new Error(t('payments.missingInvoiceAggregate'))
    aggregate.value = result.invoice
    emit('paymentsChanged', result.invoice)
    editingPayment.value = null
    message.success(t('payments.updateSuccess'))
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('payments.updateFailed'))
  } finally {
    editSaving.value = false
  }
}

// ---- delete payment ----
function handleDelete(payment: PaymentRead) {
  if (!canManage(payment)) return
  dialog.warning({
    title: t('payments.deleteTitle'),
    content: t('payments.deleteConfirm'),
    positiveText: t('payments.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      if (!canManage(payment)) return
      deleteSaving.value = payment.id
      try {
        const result = await store.deletePayment(payment.id)
        if (!result.invoice) throw new Error(t('payments.missingInvoiceAggregate'))
        aggregate.value = result.invoice
        emit('paymentsChanged', result.invoice)
        message.success(t('payments.deleteSuccess'))
      } catch (e: unknown) {
        message.error(e instanceof Error ? e.message : t('payments.deleteFailed'))
      } finally {
        deleteSaving.value = null
      }
    },
  })
}
</script>

<template>
  <n-card :title="t('payments.panelTitle')" style="margin-bottom: 16px">
    <n-alert v-if="aggregateError" type="warning" style="margin-bottom: 12px">
      {{ t('payments.aggregateRefreshFailed') }}
      <n-button text type="primary" :loading="panelLoading" :disabled="panelLoading" @click="loadAggregate">{{ t('chain.retry') }}</n-button>
    </n-alert>
    <!-- Panel header: aggregate badges -->
    <template #header-extra>
      <n-space v-if="aggregate" align="center" size="small">
        <n-tag :type="paidStatusType(aggregate.paid_status)" size="small">
          {{ t(`payments.paidStatus.${aggregate.paid_status}`) }}
        </n-tag>
        <n-text depth="3" style="font-size: 13px">
          {{ t('payments.paidTotal') }}: {{ fmtMoney(aggregate.paid_total) }}
          &nbsp;|&nbsp;
          {{ t('payments.dueAmount') }}: {{ fmtMoney(aggregate.due_amount) }}
        </n-text>
      </n-space>
    </template>

    <n-spin :show="panelLoading">

      <!-- Guard: invoice not yet sent -->
      <n-alert
        v-if="!invoiceAllowsRecording"
        type="info"
        style="margin-bottom: 12px"
      >
        {{ aggregate?.items?.length
          ? t('payments.draftManageExisting')
          : t('payments.needSentStatus') }}
      </n-alert>

      <!-- Existing payment items -->
      <div v-if="aggregate && aggregate.items && aggregate.items.length > 0">
        <div
          v-for="payment in aggregate.items"
          :key="payment.id"
          class="payment-row"
        >
          <!-- Edit mode for this row -->
          <template v-if="editingPayment && editingPayment.id === payment.id">
            <n-form label-placement="left" label-width="90px" style="margin: 8px 0">
              <n-form-item :label="t('payments.paymentDate')">
                <n-date-picker
                  v-model:formatted-value="editDate"
                  value-format="yyyy-MM-dd"
                  type="date"
                  clearable
                  style="width: 160px"
                />
              </n-form-item>
              <n-form-item :label="t('payments.amount')">
                <n-input-number
                  v-model:value="editAmount"
                  :min="0.001"
                  :precision="3"
                  style="width: 160px"
                />
              </n-form-item>
              <n-form-item :label="t('payments.paymentMethod')">
                <n-select
                  v-model:value="editMethodId"
                  :options="paymentMethodOptions"
                  :placeholder="t('payments.methodOptional')"
                  clearable
                  style="width: 200px"
                />
              </n-form-item>
              <n-form-item :label="t('payments.reference')">
                <n-input v-model:value="editReference" clearable style="width: 200px" />
              </n-form-item>
              <n-form-item :label="t('payments.note')">
                <n-input v-model:value="editNote" type="textarea" :autosize="{ minRows: 1 }" clearable style="width: 200px" />
              </n-form-item>
            </n-form>
            <n-space size="small" style="margin-bottom: 8px">
              <!-- Save edit button: use v-if/v-else to avoid loading-prop+v-if prod bug -->
              <n-button v-if="editSaving" size="small" type="primary" loading disabled>
                {{ t('payments.save') }}
              </n-button>
              <n-button v-else size="small" type="primary" :disabled="!canManage(payment)" @click="handleEdit">
                {{ t('payments.save') }}
              </n-button>
              <n-button size="small" @click="cancelEdit">{{ t('common.cancel') }}</n-button>
            </n-space>
            <n-divider style="margin: 8px 0" />
          </template>

          <!-- Display mode for this row -->
          <template v-else>
            <div class="payment-item">
              <div class="payment-item-main">
                <n-text strong>{{ fmtMoney(payment.amount) }}</n-text>
                <n-tag
                  size="small"
                  :type="payment.origin_type === 'QUOTE' ? 'warning' : 'default'"
                  style="margin-left: 8px"
                >
                  {{ payment.origin_type === 'QUOTE'
                    ? t('payments.quoteOrigin', { number: payment.quote_number ?? '—' })
                    : t('payments.invoiceOrigin') }}
                </n-tag>
                <n-text depth="3" style="margin-left: 8px; font-size: 13px">
                  {{ formatDate(payment.payment_date) }}
                </n-text>
                <n-text v-if="payment.payment_method_name" depth="3" style="margin-left: 8px; font-size: 13px">
                  · {{ payment.payment_method_name }}
                </n-text>
                <n-text v-if="payment.reference" depth="3" style="margin-left: 8px; font-size: 13px">
                  · Ref: {{ payment.reference }}
                </n-text>
                <n-text v-if="payment.note" depth="3" style="margin-left: 8px; font-size: 13px">
                  · {{ payment.note }}
                </n-text>
                <PaymentTaxBreakdown :payment="payment" />
              </div>
              <n-space size="small" :wrap-item="false" class="payment-item-actions">
                <!-- Receipt PDF preview dropdown (always visible, not gated by canRecord) -->
                <n-dropdown
                  :options="receiptPdfLocaleOptions(payment.id)"
                  trigger="click"
                  @select="handleReceiptPreviewLocaleSelect"
                >
                  <n-button
                    size="small"
                    quaternary
                    circle
                    :title="t('pdf.preview')"
                  >
                    <template #icon><n-icon><EyeOutline /></n-icon></template>
                  </n-button>
                </n-dropdown>
                <n-button
                  size="small"
                  quaternary
                  circle
                  :title="t('payments.sendReceipt')"
                  :disabled="receiptSending"
                  @click="openReceiptSend(payment.id)"
                >
                  <template #icon><n-icon><SendOutline /></n-icon></template>
                </n-button>
                <!-- Receipt PDF download dropdown (always visible, not gated by canRecord) -->
                <n-dropdown
                  :options="receiptPdfLocaleOptions(payment.id)"
                  trigger="click"
                  @select="handleReceiptLocaleSelect"
                >
                  <n-button
                    size="small"
                    quaternary
                    circle
                    :title="t('payments.downloadReceipt')"
                    :disabled="receiptDownloadingId === payment.id"
                  >
                    <template #icon><n-icon><DownloadOutline /></n-icon></template>
                  </n-button>
                </n-dropdown>

                <template v-if="invoiceAllowsManaging(payment)">
                  <n-button
                    size="small"
                    quaternary
                    circle
                    :title="t('payments.edit')"
                    :disabled="!canManage(payment)"
                    @click="openEdit(payment)"
                  >
                    <template #icon><n-icon><CreateOutline /></n-icon></template>
                  </n-button>
                  <n-button
                    v-if="deleteSaving === payment.id"
                    size="small"
                    quaternary
                    circle
                    type="error"
                    :title="t('payments.delete')"
                    loading
                    disabled
                  >
                    <template #icon><n-icon><TrashOutline /></n-icon></template>
                  </n-button>
                  <n-button
                    v-else
                    size="small"
                    quaternary
                    circle
                    type="error"
                    :title="t('payments.delete')"
                    :disabled="!canManage(payment)"
                    @click="handleDelete(payment)"
                  >
                    <template #icon><n-icon><TrashOutline /></n-icon></template>
                  </n-button>
                </template>
              </n-space>
            </div>
            <n-divider v-if="aggregate && aggregate.items && aggregate.items.indexOf(payment) < aggregate.items.length - 1" style="margin: 6px 0" />
          </template>
        </div>
      </div>

      <n-empty
        v-else-if="aggregate && (!aggregate.items || aggregate.items.length === 0)"
        :description="t('payments.noPayments')"
        style="padding: 12px 0 16px"
        size="small"
      />

      <!-- Record new payment form -->
      <template v-if="invoiceAllowsRecording">
        <template v-if="showForm && !editingPayment">
          <n-divider style="margin: 12px 0" />
          <n-form label-placement="left" label-width="90px">
            <n-form-item :label="t('payments.paymentDate')" required>
              <n-date-picker
                v-model:formatted-value="formDate"
                value-format="yyyy-MM-dd"
                type="date"
                clearable
                style="width: 160px"
              />
            </n-form-item>
            <n-form-item :label="t('payments.amount')" required>
              <n-input-number
                v-model:value="formAmount"
                :min="0.001"
                :precision="3"
                :placeholder="t('payments.amountPlaceholder')"
                style="width: 160px"
              />
            </n-form-item>
            <n-form-item :label="t('payments.paymentMethod')">
              <n-select
                v-model:value="formMethodId"
                :options="paymentMethodOptions"
                :placeholder="t('payments.methodOptional')"
                clearable
                style="width: 200px"
              />
            </n-form-item>
            <n-form-item :label="t('payments.reference')">
              <n-input v-model:value="formReference" :placeholder="t('payments.referencePlaceholder')" clearable style="width: 200px" />
            </n-form-item>
            <n-form-item :label="t('payments.note')">
              <n-input v-model:value="formNote" type="textarea" :autosize="{ minRows: 1 }" :placeholder="t('payments.notePlaceholder')" clearable style="width: 200px" />
            </n-form-item>
          </n-form>
          <n-space size="small">
            <!-- Use v-if/v-else pattern to avoid loading-prop+v-if prod bug (see vue-loading-prop-vif-prod-bug memory) -->
            <n-button v-if="recordSaving" size="small" type="primary" loading disabled>
              {{ t('payments.record') }}
            </n-button>
            <n-button v-else size="small" type="primary" :disabled="!canRecord" @click="handleRecord">
              {{ t('payments.record') }}
            </n-button>
            <n-button size="small" @click="cancelForm">{{ t('common.cancel') }}</n-button>
          </n-space>
        </template>

        <div v-else-if="!editingPayment" style="margin-top: 12px">
          <n-button dashed size="small" :disabled="!canRecord" @click="openForm">
            <template #icon><n-icon><AddOutline /></n-icon></template>
            {{ t('payments.addPayment') }}
          </n-button>
        </div>
      </template>

    </n-spin>
  </n-card>

  <!-- Receipt PDF preview dialog -->
  <PdfPreviewDialog
    v-model:show="receiptPreviewShow"
    :src="receiptPreviewSrc"
    :fallback-filename="receiptPreviewFallback"
  />

  <DocumentSendDialog
    v-if="receiptSendPaymentId"
    v-model:show="receiptSendShow"
    v-model:sending="receiptSending"
    doc-type="receipt"
    :doc-id="receiptSendPaymentId"
    :customer-email="customerEmail"
    :customer-locale="customerLocale"
    @sent="handleReceiptSent"
  />
</template>

<style scoped>
.payment-row {
  padding: 2px 0;
}

.payment-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 6px 0;
  gap: 8px;
  flex-wrap: wrap;
}

.payment-item-main {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0;
  min-width: 0;
  flex: 1 1 auto;
}

.payment-item-actions {
  flex-shrink: 0;
}
</style>
