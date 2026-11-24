import { computed, ref } from 'vue'
import { get } from '../api/http'
import type { components } from '../api/schema'

type Category = components['schemas']['ExpenseCategoryRead']
type Treatments = components['schemas']['VatTreatmentListResponse']
type Rates = components['schemas']['VatRateListResponse']
type Categories = components['schemas']['ExpenseCategoryListResponse']
export type PurchaseTreatmentSuggestion = components['schemas']['ExpenseVatTreatmentSuggestion']
export type PurchaseExpenseFields = {
  expense_date: string; category_id: string; supplier_name: string | null; vat_treatment_id: string; vat_rate_id: string
  net_amount: number | string; vat_amount: number | string; deductible?: boolean | null; reference: string | null; note: string | null
  paid_by: components['schemas']['PaidBy']; business_percentage: number | string; depreciation_years: number
  include_in_pl: boolean; pl_cost_basis: components['schemas']['PlCostBasis']
  fixed_asset?: components['schemas']['ExpenseFixedAssetInput'] | null
  client_submission_id?: string | null
}

/** Build the raw contract payload shared by the Expense editor and inbox review form. */
export function toExpenseInput(fields: PurchaseExpenseFields): components['schemas']['ExpenseInput'] {
  return { ...fields }
}

/** Resolve a model suggestion only against an active, semantically matching purchase dictionary record. */
export function resolvePurchaseTreatmentSuggestion(
  suggestion: PurchaseTreatmentSuggestion | null | undefined,
  treatments: components['schemas']['VatTreatmentRead'][],
) {
  if (!suggestion) return null
  const expectedEffect = suggestion.code === 'NL_DOMESTIC_PURCH' || suggestion.code === 'EU_B2C_PURCH' ? 'APPLY_RATE'
    : suggestion.code === 'EU_B2B_REVERSE_PURCH' ? 'ZERO_REVERSE' : null
  if (!expectedEffect) return null
  return treatments.find(x => x.id === suggestion.id && x.code === suggestion.code && x.active && x.side === 'PURCHASE' && x.effect === expectedEffect) ?? null
}

/** Shared PURCHASE dictionary loading and option construction for Expense and inbox forms. */
export function usePurchaseExpenseDictionaries() {
  const categories = ref<Category[]>([])
  const treatments = ref<components['schemas']['VatTreatmentRead'][]>([])
  const rates = ref<components['schemas']['VatRateRead'][]>([])
  const loading = ref(false)
  const categoryOptions = computed(() => categories.value.map(x => ({ label: x.name, value: x.id })))
  const purchaseTreatmentOptions = computed(() => treatments.value.filter(x => x.side === 'PURCHASE').map(x => ({ label: `${x.code} – ${x.label}`, value: x.id })))
  const vatRateOptions = computed(() => rates.value.map(x => ({ label: `${x.percent}% – ${x.label}`, value: x.id })))
  async function load() {
    loading.value = true
    try {
      const [cats, vatTreatments, vatRates] = await Promise.all([
        get<Categories>('/api/v1/expense-categories'), get<Treatments>('/api/v1/vat-treatments'), get<Rates>('/api/v1/vat-rates'),
      ])
      categories.value = cats.items
      treatments.value = vatTreatments.items
      rates.value = vatRates.items
    } finally { loading.value = false }
  }
  return { categories, treatments, rates, loading, categoryOptions, purchaseTreatmentOptions, vatRateOptions, load }
}
