import { computed, ref } from 'vue'
import { get } from '../api/http'
import type { components } from '../api/schema'

type Category = components['schemas']['ExpenseCategoryRead']
type Treatments = components['schemas']['VatTreatmentListResponse']
type Rates = components['schemas']['VatRateListResponse']
type Categories = components['schemas']['ExpenseCategoryListResponse']
export type PurchaseExpenseFields = {
  expense_date: string; category_id: string; supplier_name: string | null; vat_treatment_id: string; vat_rate_id: string
  net_amount: number | string; vat_amount: number | string; deductible?: boolean | null; reference: string | null; note: string | null
  paid_by: components['schemas']['PaidBy']; business_percentage: number | string; depreciation_years: number
}

/** Build the raw contract payload shared by the Expense editor and inbox review form. */
export function toExpenseInput(fields: PurchaseExpenseFields): components['schemas']['ExpenseInput'] {
  return { ...fields }
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
