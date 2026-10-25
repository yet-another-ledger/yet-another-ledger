<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import LineOrdering from '../../components/LineOrdering.vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  useMessage, useDialog,
  NButton, NSpace, NInput, NForm, NFormItem, NCard, NSpin, NAlert,
  NDivider, NInputNumber, NSelect, NGrid, NGi, NText, NTag,
} from 'naive-ui'
import { AddOutline, TrashOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { useEstimatesStore } from '../../stores/estimates'
import { get } from '../../api/http'
import { ApiError } from '../../api/http'
import type { components } from '../../api/schema'

type CustomerRead = components['schemas']['CustomerRead']
type VatRateRead = components['schemas']['VatRateRead']
type ProductRead = components['schemas']['ProductRead']
type UnitRead = components['schemas']['UnitRead']
type EstimateRead = components['schemas']['EstimateRead']
type EstimateCalculationRead = components['schemas']['EstimateCalculationRead']

// ------------------------------------------------------------------ line/group types

interface LineRow {
  _key: string
  product_id: string | null
  name: string
  description: string | null
  unit_cost_excl_vat: number
  quantity: number
  margin_rate_pct: number   // displayed as %, e.g. 20 for 20% markup
  unit_id: string | null
  unit_name: string | null
  group_ref: string | null
}

interface GroupRow {
  ref: string               // unique client-side ref, e.g. "g1"
  public_description: string
  vat_rate_id: string | null
}

let lineKeyCounter = 0
let groupRefCounter = 0
function nextLineKey() { return `l${++lineKeyCounter}` }
function nextGroupRef() { return `g${++groupRefCounter}` }

function emptyLine(): LineRow {
  return {
    _key: nextLineKey(),
    product_id: null,
    name: '',
    description: null,
    unit_cost_excl_vat: 0,
    quantity: 1,
    margin_rate_pct: 0,
    unit_id: null,
    unit_name: null,
    group_ref: null,
  }
}

// ------------------------------------------------------------------ component state

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const store = useEstimatesStore()

const isEdit = ref(false)
const savedEstimateId = ref<string | null>(null)
const pageLoading = ref(false)
const saving = ref(false)
const generating = ref(false)
const duplicating = ref(false)
const pageError = ref<string | null>(null)
const generatedQuoteId = ref<string | null>(null)
const generatedQuoteNumber = ref<string | null>(null)

// Form header fields
const estimateName = ref('')
const customerId = ref<string | null>(null)
const notes = ref<string | null>(null)

// Lines and groups
const lines = ref<LineRow[]>([emptyLine()])
const groups = ref<GroupRow[]>([])

// Reference data
const customers = ref<CustomerRead[]>([])
const vatRates = ref<VatRateRead[]>([])
const products = ref<ProductRead[]>([])
const units = ref<UnitRead[]>([])

// Preview / calculation state
const preview = ref<EstimateCalculationRead | null>(null)
const previewLoading = ref(false)
const previewError = ref<string | null>(null)
// Maps from client keys/refs → calculated amounts
const lineCalcMap = ref<Map<string, { line_total: string; margin_amount: string; line_sell_excl_vat: string }>>(new Map())
const groupCalcMap = ref<Map<string, string>>(new Map())

// ------------------------------------------------------------------ load reference data

async function loadReferenceData() {
  const [ratesRes, unitsRes] = await Promise.all([
    get<{ items: VatRateRead[] }>('/api/v1/vat-rates'),
    get<{ items: UnitRead[] }>('/api/v1/units'),
  ])
  vatRates.value = ratesRes.items
  units.value = unitsRes.items
}

async function searchCustomers(q: string) {
  const res = await get<{ items: CustomerRead[] }>(`/api/v1/customers?q=${encodeURIComponent(q)}&limit=20`)
  customers.value = res.items
}

async function searchProducts(q: string) {
  const res = await get<{ items: ProductRead[] }>(`/api/v1/products?q=${encodeURIComponent(q)}&limit=20`)
  products.value = res.items
}

// ------------------------------------------------------------------ computed options

const customerOptions = computed(() =>
  customers.value.map(c => ({ label: c.name, value: c.id }))
)

const vatRateOptions = computed(() =>
  vatRates.value.map(r => ({ label: `${r.label} (${r.percent}%)`, value: r.id }))
)

const productOptions = computed(() =>
  products.value.map(p => ({ label: p.name, value: p.id }))
)

const groupOptions = computed(() =>
  groups.value.map((g, i) => ({
    label: g.public_description.trim() || `${t('estimates.groupName')} ${i + 1}`,
    value: g.ref,
  }))
)

// ------------------------------------------------------------------ line management

function addLine() {
  lines.value.push(emptyLine())
}

function removeLine(i: number) {
  if (lines.value.length > 1) lines.value.splice(i, 1)
}

function handleProductSelect(lineIndex: number, productId: string | null) {
  const line = lines.value[lineIndex]
  line.product_id = productId
  if (productId) {
    const product = products.value.find(p => p.id === productId)
    if (product) {
      if (!line.name) line.name = product.name
      if (product.purchase_cost_excl_vat != null) {
        line.unit_cost_excl_vat = Number(product.purchase_cost_excl_vat)
      }
      if (product.effective_margin_rate != null) {
        line.margin_rate_pct = Number(product.effective_margin_rate) * 100
      }
      if (product.unit_id) {
        line.unit_id = product.unit_id
        const unit = units.value.find(u => u.id === product.unit_id)
        if (unit) line.unit_name = unit.name
      }
    }
  }
}

// ------------------------------------------------------------------ group management

function addGroup() {
  groups.value.push({ ref: nextGroupRef(), public_description: '', vat_rate_id: null })
}

function removeGroup(i: number) {
  const groupRef = groups.value[i].ref
  // Unassign lines that belonged to this group
  lines.value.forEach(l => { if (l.group_ref === groupRef) l.group_ref = null })
  groups.value.splice(i, 1)
}

// ------------------------------------------------------------------ preview / calculate

let previewTimer: ReturnType<typeof setTimeout> | null = null
let previewVersion = 0

function schedulePreview() {
  const version = ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
  preview.value = null
  previewError.value = null
  lineCalcMap.value = new Map()
  groupCalcMap.value = new Map()
  previewLoading.value = false
  previewTimer = setTimeout(() => { void runPreview(version) }, 600)
}

onBeforeUnmount(() => {
  ++previewVersion
  if (previewTimer) clearTimeout(previewTimer)
})

async function runPreview(version: number) {
  const validGroupRefs = new Set<string>()
  const requestGroups = groups.value
    .filter(g => g.public_description.trim())
    .map((g, i) => {
      validGroupRefs.add(g.ref)
      return {
        ref: g.ref,
        public_description: g.public_description.trim(),
        vat_rate_id: g.vat_rate_id ?? undefined,
        sort_order: i,
      }
    })

  const validPairs: Array<{ key: string; line: Record<string, unknown> }> = []
  lines.value.forEach(l => {
    if (l.name.trim() && (l.quantity ?? 0) > 0) {
      validPairs.push({
        key: l._key,
        line: {
          name: l.name.trim(),
          product_id: l.product_id ?? undefined,
          unit_cost_excl_vat: String(l.unit_cost_excl_vat ?? 0),
          quantity: String(l.quantity ?? 1),
          margin_rate: String((l.margin_rate_pct / 100).toFixed(4)),
          unit_id: l.unit_id ?? undefined,
          unit_name: l.unit_name || undefined,
          group_ref: (l.group_ref && validGroupRefs.has(l.group_ref)) ? l.group_ref : undefined,
        },
      })
    }
  })

  if (validPairs.length === 0) {
    preview.value = null
    lineCalcMap.value = new Map()
    groupCalcMap.value = new Map()
    return
  }

  previewLoading.value = true
  previewError.value = null
  try {
    const req = {
      groups: requestGroups,
      lines: validPairs.map(p => p.line),
    }
    const result = await store.calculatePreview(req as Parameters<typeof store.calculatePreview>[0])
    if (version !== previewVersion) return
    preview.value = result

    const newLineMap = new Map<string, { line_total: string; margin_amount: string; line_sell_excl_vat: string }>()
    result.lines.forEach((lr, i) => {
      const key = validPairs[i]?.key
      if (key) {
        newLineMap.set(key, {
          line_total: lr.line_total,
          margin_amount: lr.margin_amount,
          line_sell_excl_vat: lr.line_sell_excl_vat,
        })
      }
    })
    lineCalcMap.value = newLineMap

    const newGroupMap = new Map<string, string>()
    result.groups.forEach(gr => newGroupMap.set(gr.ref, gr.group_sell_excl_vat))
    groupCalcMap.value = newGroupMap
  } catch (e: unknown) {
    if (version !== previewVersion) return
    previewError.value = e instanceof ApiError ? e.message : (e instanceof Error ? e.message : String(e))
  } finally {
    if (version === previewVersion) previewLoading.value = false
  }
}

watch([lines, groups], schedulePreview, { deep: true })

// ------------------------------------------------------------------ build payload

function validateGroups(): string | null {
  const empty = groups.value.find(g => !g.public_description.trim())
  if (empty) return t('estimates.groupDescriptionRequired')
  return null
}

function buildWritePayload() {
  if (!estimateName.value.trim()) return null
  return {
    name: estimateName.value.trim(),
    customer_id: customerId.value ?? undefined,
    notes: notes.value?.trim() || undefined,
    groups: groups.value.map((g, i) => ({
      ref: g.ref,
      public_description: g.public_description.trim(),
      vat_rate_id: g.vat_rate_id ?? undefined,
      sort_order: i,
    })),
    lines: lines.value
      .filter(l => l.name.trim() && (l.quantity ?? 0) > 0)
      .map(l => ({
        product_id: l.product_id ?? undefined,
        name: l.name.trim(),
        description: l.description?.trim() || undefined,
        unit_cost_excl_vat: String(l.unit_cost_excl_vat ?? 0),
        quantity: String(l.quantity ?? 1),
        margin_rate: String((l.margin_rate_pct / 100).toFixed(4)),
        unit_id: l.unit_id ?? undefined,
        unit_name: l.unit_name?.trim() || undefined,
        group_ref: l.group_ref ?? undefined,
      })),
  }
}

// ------------------------------------------------------------------ save

async function handleSave() {
  const groupErr = validateGroups()
  if (groupErr) { message.error(groupErr); return }
  const payload = buildWritePayload()
  if (!payload) {
    message.error(t('estimates.nameRequired'))
    return
  }
  saving.value = true
  try {
    let saved: EstimateRead
    if (isEdit.value && savedEstimateId.value) {
      saved = await store.updateEstimate(savedEstimateId.value, payload as Parameters<typeof store.updateEstimate>[1])
      message.success(t('estimates.updateSuccess'))
    } else {
      saved = await store.createEstimate(payload as Parameters<typeof store.createEstimate>[0])
      message.success(t('estimates.createSuccess'))
      isEdit.value = true
      savedEstimateId.value = saved.id
      router.replace(`/estimates/${saved.id}/edit`)
    }
    generatedQuoteId.value = saved.generated_quote_id ?? null
    generatedQuoteNumber.value = saved.generated_quote_number ?? null
  } catch (e: unknown) {
    message.error(e instanceof ApiError ? e.message : t('estimates.saveFailed'))
  } finally {
    saving.value = false
  }
}

// ------------------------------------------------------------------ generate quote

function validateForGenerate(): string | null {
  const groupErr = validateGroups()
  if (groupErr) return groupErr
  if (!customerId.value) return t('estimates.generateValidationNoCustomer')
  const groupsWithLines = groups.value.filter(g =>
    lines.value.some(l => l.group_ref === g.ref && l.name.trim() && (l.quantity ?? 0) > 0)
  )
  if (groupsWithLines.length === 0) return t('estimates.generateValidationNoGroups')
  if (groupsWithLines.some(g => !g.vat_rate_id)) return t('estimates.generateValidationMissingVat')
  return null
}

async function handleGenerateQuote() {
  const validationError = validateForGenerate()
  if (validationError) { message.error(validationError); return }

  // Auto-save before generating
  const payload = buildWritePayload()
  if (!payload) { message.error(t('estimates.nameRequired')); return }
  generating.value = true
  try {
    let estimateId = savedEstimateId.value
    if (!estimateId) {
      const saved = await store.createEstimate(payload as Parameters<typeof store.createEstimate>[0])
      estimateId = saved.id
      savedEstimateId.value = estimateId
      isEdit.value = true
      router.replace(`/estimates/${estimateId}/edit`)
    } else {
      await store.updateEstimate(estimateId, payload as Parameters<typeof store.updateEstimate>[1])
    }

    const quote = await store.generateQuote(estimateId)
    message.success(t('estimates.generateSuccess'))
    generatedQuoteId.value = quote.id
    generatedQuoteNumber.value = quote.quote_number
    router.push(`/quotes/${quote.id}/edit`)
  } catch (e: unknown) {
    message.error(e instanceof ApiError ? e.message : t('estimates.generateFailed'))
  } finally {
    generating.value = false
  }
}

// ------------------------------------------------------------------ duplicate this estimate

async function handleDuplicateThis() {
  const groupErr = validateGroups()
  if (groupErr) { message.error(groupErr); return }
  const payload = buildWritePayload()
  if (!payload) { message.error(t('estimates.nameRequired')); return }
  duplicating.value = true
  try {
    let id = savedEstimateId.value
    if (!id) {
      const saved = await store.createEstimate(payload as Parameters<typeof store.createEstimate>[0])
      id = saved.id
    } else {
      await store.updateEstimate(id, payload as Parameters<typeof store.updateEstimate>[1])
    }
    const dup = await store.duplicateEstimate(id)
    message.success(t('estimates.duplicateSuccess'))
    router.push(`/estimates/${dup.id}/edit`)
  } catch (e: unknown) {
    message.error(e instanceof ApiError ? e.message : t('estimates.duplicateFailed'))
  } finally {
    duplicating.value = false
  }
}

// ------------------------------------------------------------------ load existing estimate

function populateFromEstimate(est: EstimateRead) {
  savedEstimateId.value = est.id
  estimateName.value = est.name
  customerId.value = est.customer_id ?? null
  notes.value = est.notes ?? null
  generatedQuoteId.value = est.generated_quote_id ?? null
  generatedQuoteNumber.value = est.generated_quote_number ?? null

  // Build group ref map: group.id → group.ref (we assign sequential refs)
  const groupIdToRef = new Map<string, string>()
  groups.value = est.groups.map(g => {
    const groupRef = nextGroupRef()
    groupIdToRef.set(g.id, groupRef)
    return {
      ref: groupRef,
      public_description: g.public_description,
      vat_rate_id: g.vat_rate_id ?? null,
    }
  })

  lines.value = est.lines.map(l => ({
    _key: nextLineKey(),
    product_id: l.product_id ?? null,
    name: l.name,
    description: l.description ?? null,
    unit_cost_excl_vat: Number(l.unit_cost_excl_vat),
    quantity: Number(l.quantity),
    margin_rate_pct: Number(l.margin_rate) * 100,
    unit_id: l.unit_id ?? null,
    unit_name: l.unit_name ?? null,
    group_ref: l.group_id ? (groupIdToRef.get(l.group_id) ?? null) : null,
  }))

  if (lines.value.length === 0) lines.value = [emptyLine()]
}

onMounted(async () => {
  pageLoading.value = true
  try {
    await Promise.all([
      loadReferenceData(),
      searchCustomers(''),
      searchProducts(''),
    ])

    const id = route.params.id as string | undefined
    if (id && id !== 'new') {
      isEdit.value = true
      const est = await store.fetchEstimate(id)
      populateFromEstimate(est)
      // Ensure customer appears in the dropdown
      if (est.customer_id && !customers.value.find(c => c.id === est.customer_id)) {
        const cust = await get<CustomerRead>(`/api/v1/customers/${est.customer_id}`)
        customers.value = [cust, ...customers.value]
      }
    }
  } catch (e: unknown) {
    pageError.value = e instanceof Error ? e.message : String(e)
  } finally {
    pageLoading.value = false
  }
})

// ------------------------------------------------------------------ helpers

const fmtMoney = (v: string | number | undefined) => v != null ? Number(v).toFixed(2) : '—'

function handleDeleteLine(i: number) {
  dialog.warning({
    title: t('estimates.removeLine'),
    content: t('estimates.removeLineConfirm'),
    positiveText: t('estimates.remove'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => removeLine(i),
  })
}

function handleDeleteGroup(i: number) {
  dialog.warning({
    title: t('estimates.removeGroup'),
    content: t('estimates.removeGroupConfirm'),
    positiveText: t('estimates.remove'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => removeGroup(i),
  })
}
</script>

<template>
  <div class="estimate-edit-page">
    <n-spin :show="pageLoading">
          <div class="estimate-edit-container">

            <!-- Page header -->
            <div class="page-header">
              <h2>
                {{ isEdit
                  ? (estimateName || t('estimates.edit'))
                  : t('estimates.new') }}
              </h2>
              <n-space align="center">
                <n-tag
                  v-if="generatedQuoteId"
                  type="success"
                  size="small"
                  style="cursor:pointer"
                  @click="router.push(`/quotes/${generatedQuoteId}/edit`)"
                >
                  {{ generatedQuoteNumber ? `${t('estimates.viewQuote')}: ${generatedQuoteNumber}` : t('estimates.viewQuote') }}
                </n-tag>
                <template v-if="isEdit && savedEstimateId">
                  <n-button v-if="duplicating" :loading="true" disabled>
                    {{ t('estimates.duplicateThis') }}
                  </n-button>
                  <n-button v-else @click="handleDuplicateThis">
                    {{ t('estimates.duplicateThis') }}
                  </n-button>
                </template>
                <n-button
                  type="success"
                  :loading="generating"
                  @click="handleGenerateQuote"
                >
                  {{ t('estimates.generateQuote') }}
                </n-button>
              </n-space>
            </div>

            <n-alert v-if="pageError" type="error" style="margin-bottom: 16px">
              {{ pageError }}
            </n-alert>

            <n-form label-placement="top">

              <!-- Basic Info -->
              <n-card :title="t('estimates.basicSection')" style="margin-bottom: 16px">
                <n-grid cols="1 600:2" :x-gap="16" :y-gap="0">
                  <n-gi>
                    <n-form-item :label="t('estimates.name')" required>
                      <n-input
                        v-model:value="estimateName"
                        :placeholder="t('estimates.namePlaceholder')"
                        clearable
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi>
                    <n-form-item :label="t('estimates.customer')">
                      <n-select
                        v-model:value="customerId"
                        filterable
                        clearable
                        remote
                        :options="customerOptions"
                        :placeholder="t('estimates.customerSearch')"
                        @search="searchCustomers"
                      />
                    </n-form-item>
                  </n-gi>
                  <n-gi span="1 600:2">
                    <n-form-item :label="t('estimates.notes')">
                      <n-input
                        v-model:value="notes"
                        type="textarea"
                        :autosize="{ minRows: 2 }"
                        :placeholder="t('estimates.notesPlaceholder')"
                        clearable
                      />
                    </n-form-item>
                  </n-gi>
                </n-grid>
              </n-card>

              <!-- Cost Lines -->
              <n-card :title="t('estimates.linesSection')" style="margin-bottom: 16px">

                <LineOrdering v-model="lines" :disabled="saving || pageLoading || generating || duplicating">
                  <template #default="{ line, index: i }">
                <div class="line-block">
                  <n-divider v-if="i > 0" />

                  <!-- Row 1: product / name / group / remove -->
                  <n-grid cols="1 600:12" :x-gap="8" :y-gap="4">
                    <n-gi span="1 600:3">
                      <n-form-item :label="t('estimates.product')" size="small">
                        <n-select
                          :value="line.product_id"
                          filterable
                          clearable
                          remote
                          :options="productOptions"
                          :placeholder="t('estimates.productSearch')"
                          size="small"
                          :consistent-menu-width="false"
                          @search="searchProducts"
                          @update:value="(v: string | null) => handleProductSelect(i, v)"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:4">
                      <n-form-item :label="t('estimates.lineName')" size="small" required>
                        <n-input
                          v-model:value="line.name"
                          :placeholder="t('estimates.lineNamePlaceholder')"
                          size="small"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:3">
                      <n-form-item :label="t('estimates.group')" size="small">
                        <n-select
                          v-model:value="line.group_ref"
                          :options="groupOptions"
                          :placeholder="t('estimates.noGroup')"
                          size="small"
                          clearable
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:2" style="display:flex; align-items:flex-end; padding-bottom:4px">
                      <n-button
                        v-if="lines.length > 1"
                        size="small"
                        quaternary
                        circle
                        type="error"
                        @click="handleDeleteLine(i)"
                      >
                        <template #icon><n-icon><TrashOutline /></n-icon></template>
                      </n-button>
                    </n-gi>

                    <!-- Row 2: cost / qty / margin / unit -->
                    <n-gi span="1 600:3">
                      <n-form-item :label="t('estimates.unitCost')" size="small">
                        <n-input-number
                          v-model:value="line.unit_cost_excl_vat"
                          :min="0"
                          :precision="3"
                          size="small"
                          style="width:100%"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:2">
                      <n-form-item :label="t('estimates.quantity')" size="small">
                        <n-input-number
                          v-model:value="line.quantity"
                          :min="0.001"
                          :precision="3"
                          size="small"
                          style="width:100%"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:2">
                      <n-form-item :label="t('estimates.marginRate')" size="small">
                        <n-input-number
                          v-model:value="line.margin_rate_pct"
                          :min="0"
                          :max="9999.99"
                          :precision="2"
                          size="small"
                          style="width:100%"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi span="1 600:2">
                      <n-form-item :label="t('estimates.unit')" size="small">
                        <n-input
                          v-model:value="line.unit_name"
                          :placeholder="t('estimates.unitPlaceholder')"
                          size="small"
                          clearable
                        />
                      </n-form-item>
                    </n-gi>

                    <!-- Row 3: description (optional) -->
                    <n-gi span="1 600:12">
                      <n-form-item :label="t('estimates.lineDescription')" size="small">
                        <n-input
                          v-model:value="line.description"
                          type="textarea"
                          :autosize="{ minRows: 1, maxRows: 3 }"
                          :placeholder="t('estimates.lineDescriptionPlaceholder')"
                          size="small"
                          clearable
                        />
                      </n-form-item>
                    </n-gi>

                    <!-- Row 4: calculated amounts (from preview) -->
                    <n-gi span="1 600:12">
                      <div class="line-calc-row">
                        <template v-if="lineCalcMap.get(line._key)">
                          <span class="calc-item">
                            <n-text depth="3" size="small">{{ t('estimates.lineTotal') }}:</n-text>
                            <n-text size="small">{{ fmtMoney(lineCalcMap.get(line._key)!.line_total) }}</n-text>
                          </span>
                          <span class="calc-sep">·</span>
                          <span class="calc-item">
                            <n-text depth="3" size="small">{{ t('estimates.marginAmount') }}:</n-text>
                            <n-text size="small" type="success">{{ fmtMoney(lineCalcMap.get(line._key)!.margin_amount) }}</n-text>
                          </span>
                          <span class="calc-sep">·</span>
                          <span class="calc-item">
                            <n-text depth="3" size="small">{{ t('estimates.lineSell') }}:</n-text>
                            <n-text size="small" strong>{{ fmtMoney(lineCalcMap.get(line._key)!.line_sell_excl_vat) }}</n-text>
                          </span>
                        </template>
                        <n-text v-else-if="line.name.trim()" depth="3" size="small">
                          {{ previewLoading ? '...' : t('estimates.calcPending') }}
                        </n-text>
                      </div>
                    </n-gi>
                  </n-grid>
                </div>
                  </template>
                </LineOrdering>

                <n-button dashed block style="margin-top: 12px" @click="addLine">
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('estimates.addLine') }}
                </n-button>
              </n-card>

              <!-- Groups -->
              <n-card :title="t('estimates.groupsSection')" style="margin-bottom: 16px">
                <n-alert v-if="groups.length === 0" type="info" style="margin-bottom: 12px">
                  {{ t('estimates.groupsHint') }}
                </n-alert>

                <div v-for="(group, i) in groups" :key="group.ref" class="group-block">
                  <n-divider v-if="i > 0" />
                  <div class="group-header">
                    <n-tag size="small" type="primary">
                      {{ `${t('estimates.groupName')} ${i + 1}` }}
                    </n-tag>
                    <n-space align="center">
                      <span v-if="groupCalcMap.get(group.ref)" class="group-sell">
                        <n-text depth="3" size="small">{{ t('estimates.groupSellPrice') }}:</n-text>
                        <n-text strong>{{ fmtMoney(groupCalcMap.get(group.ref)) }}</n-text>
                      </span>
                      <n-button
                        size="small"
                        quaternary
                        circle
                        type="error"
                        @click="handleDeleteGroup(i)"
                      >
                        <template #icon><n-icon><TrashOutline /></n-icon></template>
                      </n-button>
                    </n-space>
                  </div>

                  <n-grid cols="1 600:2" :x-gap="16" :y-gap="0" style="margin-top: 8px">
                    <n-gi>
                      <n-form-item :label="t('estimates.publicDescription')" size="small">
                        <n-input
                          v-model:value="group.public_description"
                          :placeholder="t('estimates.publicDescriptionPlaceholder')"
                          size="small"
                        />
                      </n-form-item>
                    </n-gi>
                    <n-gi>
                      <n-form-item :label="t('estimates.vatRate')" size="small">
                        <n-select
                          v-model:value="group.vat_rate_id"
                          :options="vatRateOptions"
                          :placeholder="t('estimates.vatRatePlaceholder')"
                          size="small"
                          clearable
                        />
                      </n-form-item>
                    </n-gi>
                  </n-grid>
                </div>

                <n-button dashed block style="margin-top: 12px" @click="addGroup">
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('estimates.addGroup') }}
                </n-button>
              </n-card>

              <!-- Summary -->
              <n-card :title="t('estimates.summarySection')" style="margin-bottom: 16px">
                <div v-if="previewLoading" style="text-align:center; padding:12px">
                  <n-spin />
                </div>
                <n-alert v-else-if="previewError" type="error">{{ previewError }}</n-alert>
                <div v-else-if="preview" class="summary-table">
                  <div class="summary-row">
                    <n-text depth="3">{{ t('estimates.totalMargin') }}</n-text>
                    <n-text type="success" strong>{{ fmtMoney(preview.total_margin) }}</n-text>
                  </div>
                  <div class="summary-row">
                    <n-text depth="3">{{ t('estimates.totalExclVat') }}</n-text>
                    <n-text strong>{{ fmtMoney(preview.total_excl_vat) }}</n-text>
                  </div>
                  <n-divider style="margin: 8px 0" />
                  <div class="summary-row">
                    <n-space :size="4">
                      <n-text depth="3">{{ t('estimates.totalInclVatIndicative') }}</n-text>
                      <n-text depth="3" style="font-size:12px">
                        {{ t('estimates.indicativeLabel') }}
                        {{ preview.indicative_vat_rate_percent ? `(${preview.indicative_vat_rate_percent}%)` : '' }}
                      </n-text>
                    </n-space>
                    <n-text depth="3">
                      {{ preview.total_incl_vat_indicative != null ? fmtMoney(preview.total_incl_vat_indicative) : '—' }}
                    </n-text>
                  </div>
                </div>
                <n-text v-else depth="3">{{ t('estimates.summaryPending') }}</n-text>
              </n-card>

            </n-form>

            <!-- Action buttons -->
            <n-space justify="space-between" style="margin-bottom: 24px">
              <n-button @click="router.push('/estimates')">{{ t('estimates.backToList') }}</n-button>
              <n-space>
                <n-button type="primary" :loading="saving" @click="handleSave">
                  {{ isEdit ? t('estimates.save') : t('estimates.create') }}
                </n-button>
                <n-button type="success" :loading="generating" @click="handleGenerateQuote">
                  {{ t('estimates.generateQuote') }}
                </n-button>
              </n-space>
            </n-space>

          </div>
    </n-spin>
  </div>
</template>

<style scoped>
.estimate-edit-container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  gap: 12px;
}

.page-header h2 {
  margin: 0;
}

.line-block {
  margin-bottom: 4px;
}

.line-calc-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0 4px 2px;
  background: var(--n-color-embedded);
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 13px;
}

.calc-item {
  display: flex;
  gap: 4px;
  align-items: baseline;
}

.calc-sep {
  color: var(--n-text-color-3);
  font-size: 12px;
}

.group-block {
  margin-bottom: 4px;
}

.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.group-sell {
  display: flex;
  gap: 6px;
  align-items: baseline;
}

.summary-table {
  max-width: 380px;
  margin-left: auto;
}

.summary-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 0;
}
</style>
