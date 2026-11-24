<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NFormItem, NInput, NSpace, NText } from 'naive-ui'
import { post } from '../../api/http'
import type { components } from '../../api/schema'

type CostInputs = components['schemas']['ExpenseCostPreviewRequest']
type Estimate = components['schemas']['FixedAssetResidualEstimateRead']

const props = defineProps<{
  value: number | null
  expenseId?: string | null
  costInputs?: CostInputs | null
  usefulLifeYears: number
  suggestionKey?: string | number | null
  suggestedName?: string | null
  suggestedBrand?: string | null
  suggestedModel?: string | null
}>()
const emit = defineEmits<{ 'update:value': [value: number | null] }>()
const { t } = useI18n()
const productName = ref('')
const brand = ref('')
const model = ref('')
const productOwnership = ref({ name: false, brand: false, model: false })
const productManuallyTouched = ref({ name: false, brand: false, model: false })
const condition = ref('')
const usage = ref('')
const result = ref<Estimate | null>(null)
const error = ref('')
const loading = ref(false)
let requestVersion = 0
const requestKey = computed(() => JSON.stringify([
  props.expenseId, props.costInputs, props.suggestionKey, props.usefulLifeYears,
  productName.value, brand.value, model.value, condition.value, usage.value,
]))
function invalidate() {
  requestVersion += 1
  result.value = null
  error.value = ''
  loading.value = false
}
function updateRecognizedProduct(field: 'name' | 'brand' | 'model', current: string, suggested: string | null | undefined) {
  const owned = productOwnership.value[field]
  if (owned || (!productManuallyTouched.value[field] && !current.trim())) {
    if (field === 'name') productName.value = suggested ?? ''
    else if (field === 'brand') brand.value = suggested ?? ''
    else model.value = suggested ?? ''
    productOwnership.value[field] = !!suggested
  }
}
watch(() => [props.suggestionKey, props.suggestedName, props.suggestedBrand, props.suggestedModel] as const, ([, name, newBrand, newModel]) => {
  updateRecognizedProduct('name', productName.value, name)
  updateRecognizedProduct('brand', brand.value, newBrand)
  updateRecognizedProduct('model', model.value, newModel)
}, { immediate: true })
watch(requestKey, invalidate)
watch(() => props.value, () => {
  if (result.value) invalidate()
  else if (loading.value) {
    requestVersion += 1
    loading.value = false
  }
})
watch(() => productName.value, value => {
  if (!value && result.value?.status === 'suggested') result.value = null
})

async function estimate() {
  if (!productName.value.trim() || (!props.expenseId && !props.costInputs)) return
  const version = ++requestVersion
  const key = requestKey.value
  loading.value = true
  result.value = null
  error.value = ''
  try {
    const response = await post<Estimate>('/api/v1/fixed-assets/residual-estimate', {
      expense_id: props.expenseId || null,
      cost_inputs: props.expenseId ? null : props.costInputs,
      product_name: productName.value,
      brand: brand.value || null,
      model: model.value || null,
      useful_life_years: props.usefulLifeYears,
      condition: condition.value || null,
      usage: usage.value || null,
    })
    if (version === requestVersion && key === requestKey.value) result.value = response
  } catch {
    if (version === requestVersion && key === requestKey.value) error.value = t('fixedAssets.estimateFailed')
  } finally {
    if (version === requestVersion && key === requestKey.value) loading.value = false
  }
}
function apply() {
  if (result.value?.suggested_residual_value != null) {
    emit('update:value', Number(result.value.suggested_residual_value))
    result.value = null
  }
}
function editProduct(field: 'name' | 'brand' | 'model', value: string) {
  productOwnership.value[field] = false
  productManuallyTouched.value[field] = true
  if (field === 'name') productName.value = value
  else if (field === 'brand') brand.value = value
  else model.value = value
}
function preventImplicitSubmit(event: KeyboardEvent) {
  // Keep text-entry Enter from submitting a surrounding expense form. During
  // IME composition Enter confirms the composed text and must retain its native behavior.
  if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) event.preventDefault()
}
</script>

<template>
  <div class="residual-estimate">
    <n-form-item :label="t('fixedAssets.estimateProduct')"><n-input :value="productName" @keydown="preventImplicitSubmit" @update:value="(value: string) => editProduct('name', value)" /></n-form-item>
    <n-space>
      <n-form-item :label="t('fixedAssets.estimateBrand')"><n-input :value="brand" @keydown="preventImplicitSubmit" @update:value="(value: string) => editProduct('brand', value)" /></n-form-item>
      <n-form-item :label="t('fixedAssets.estimateModel')"><n-input :value="model" @keydown="preventImplicitSubmit" @update:value="(value: string) => editProduct('model', value)" /></n-form-item>
    </n-space>
    <n-form-item :label="t('fixedAssets.estimateCondition')"><n-input v-model:value="condition" @keydown="preventImplicitSubmit" /></n-form-item>
    <n-form-item :label="t('fixedAssets.estimateUsage')"><n-input v-model:value="usage" @keydown="preventImplicitSubmit" /></n-form-item>
    <n-button secondary :loading="loading" :disabled="!productName.trim() || (!props.expenseId && !props.costInputs)" @click="estimate">{{ t('fixedAssets.estimateButton') }}</n-button>
    <n-alert v-if="error" type="error" :show-icon="false">{{ error }}</n-alert>
    <n-alert v-if="result" type="info" :show-icon="false" class="estimate-result">
      <n-text>{{ t('fixedAssets.modelEstimateUncertain') }}</n-text>
      <p>{{ result.reason }}</p>
      <p v-for="assumption in result.assumptions" :key="assumption">{{ assumption }}</p>
      <p v-if="result.status === 'insufficient_data'">{{ t('fixedAssets.estimateInsufficient') }}</p>
      <template v-else>
        <p>{{ t('fixedAssets.estimateSuggested') }}: {{ result.suggested_residual_value }} {{ result.currency_code }}</p>
        <n-space>
          <n-button size="small" type="primary" @click="apply">{{ t('fixedAssets.estimateApply') }}</n-button>
          <n-button size="small" @click="result = null">{{ t('fixedAssets.estimateIgnore') }}</n-button>
        </n-space>
      </template>
    </n-alert>
  </div>
</template>

<style scoped>
.residual-estimate{display:grid;gap:8px}.estimate-result{margin-top:8px}.estimate-result p{margin:6px 0}
</style>
