<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

defineOptions({ name: 'BatteryAuditSnapshot' })
const props = defineProps<{ snapshot?: Record<string, unknown> }>()
const snapshot = computed(() => props.snapshot ?? {})
const { t } = useI18n()
const fields = [
  'version', 'year', 'acquisition_expense_date', 'in_service_date', 'net_basis', 'purchase_vat',
  'purchase_vat_rate_percent', 'currency', 'rule_version', 'private_use_percentage', 'basis_note',
  'confirmed', 'confirmed_at', 'calculation_base', 'calculation_vat', 'asset_version_used',
  'expense_date', 'exchange_rate', 'net_amount', 'vat_amount', 'gross_amount',
  'base_net_amount', 'base_vat_amount', 'base_gross_amount', 'vat_rate_id', 'vat_rate_percent',
  'vat_rate_label', 'current_version', 'current_confirmed', 'current_private_use_percentage',
  'current_calculation_base', 'current_calculation_vat', 'proposed_calculation_base',
  'proposed_calculation_vat', 'requires_reconfirmation',
]
const rows = computed(() => fields.filter(key => key in snapshot.value).map(key => ({
  key, value: snapshot.value[key] == null ? '—' : typeof snapshot.value[key] === 'boolean'
    ? t(snapshot.value[key] ? 'battery.auditYes' : 'battery.auditNo') : String(snapshot.value[key]),
})))
const sections = computed(() => ['expense_before', 'expense_after', 'annual_before', 'annual_after', 'annual_previews']
  .flatMap(key => {
    const value = snapshot.value[key]
    const items = Array.isArray(value) ? value : [value]
    const snapshots = items.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null && !Array.isArray(item))
    return snapshots.length ? [{ key, snapshots }] : []
  }))
</script>

<template>
  <dl class="audit-values">
    <template v-for="row in rows" :key="row.key">
      <dt>{{ t(`battery.auditFields.${row.key}`) }}</dt><dd>{{ row.value }}</dd>
    </template>
  </dl>
  <section v-for="section in sections" :key="section.key">
    <h5>{{ t(`battery.auditFields.${section.key}`) }}</h5>
    <BatteryAuditSnapshot v-for="(snapshot, index) in section.snapshots" :key="index" :snapshot="snapshot" />
  </section>
</template>

<style scoped>
.audit-values { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(120px, 2fr); gap: 4px 12px; }
dt { font-weight: 600; }
dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
