<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
const props = defineProps<{ snapshot: Record<string, unknown> }>()
const { t } = useI18n()
const fields = [
  ['status', 'status'], ['version', 'version'], ['source_id', 'source'],
  ['original_number', 'number'], ['document_kind', 'documentKind'],
  ['related_external_sale_id', 'relatedOriginal'], ['historical_original_number', 'historicalNumber'],
  ['historical_original_explanation', 'historicalExplanation'], ['document_date', 'date'],
  ['covered_from', 'coveredFrom'], ['covered_to', 'coveredTo'], ['counterparty', 'counterparty'],
  ['description', 'description'], ['include_in_pl', 'includePl'],
  ['original_net_total', 'netTotal'], ['original_vat_total', 'vatTotal'],
  ['mismatch_ack_reason', 'mismatchReason'],
] as const
function text(value: unknown): string {
  if (value === true || value === false) return t(value ? 'common.yes' : 'common.no')
  return typeof value === 'string' || typeof value === 'number' ? String(value) : t('externalSales.none')
}
function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object' && !Array.isArray(item)) : []
}
const taxes = computed(() => records(props.snapshot.tax_summaries))
const originals = computed(() => records(props.snapshot.originals))
</script>

<template>
  <div class="business-snapshot">
    <dl><template v-for="[key, label] in fields" :key="key"><dt>{{ t(`externalSales.${label}`) }}</dt><dd>{{ text(snapshot[key]) }}</dd></template></dl>
    <h4>{{ t('externalSales.taxSummaries') }}</h4>
    <table><thead><tr><th>{{ t('externalSales.treatment') }}</th><th>{{ t('externalSales.rate') }}</th><th>{{ t('externalSales.net') }}</th><th>{{ t('externalSales.vat') }}</th></tr></thead>
      <tbody><tr v-for="(tax, index) in taxes" :key="index"><td>{{ text(tax.treatment_label) }} ({{ text(tax.treatment_code) }})</td><td>{{ text(tax.rate_label) }} ({{ text(tax.rate_percent) }}%)</td><td>{{ text(tax.net_amount) }}</td><td>{{ text(tax.vat_amount) }}</td></tr></tbody>
    </table>
    <h4>{{ t('externalSales.supportingEvidence') }}</h4>
    <ul><li v-for="(file, index) in originals" :key="index">{{ text(file.filename) }} · {{ text(file.mime_type) }} · {{ text(file.byte_size) }} bytes<br />{{ text(file.id) }}<br />SHA-256: {{ text(file.sha256) }}</li></ul>
  </div>
</template>

<style scoped>
dl { margin: 0; }
dt { font-weight: 600; }
dd { margin: 4px 0 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 6px; text-align: left; }
li { overflow-wrap: anywhere; }
</style>
