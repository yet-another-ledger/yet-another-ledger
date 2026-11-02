import { defineStore } from 'pinia'
import { ref } from 'vue'
import { del, get, post, put, http, ApiError } from '../api/http'
import type { components } from '../api/schema'

type ExpenseRead = components['schemas']['ExpenseRead']
type ExpenseKind = components['schemas']['ExpenseKind']
type ExpenseInput = components['schemas']['ExpenseInput']
type ExpenseListItem = components['schemas']['ExpenseListItem']
type ExpenseListResponse = components['schemas']['ExpenseListResponse']
type ExpenseAttachmentRead = components['schemas']['ExpenseAttachmentRead']
type ExpenseAttachmentListResponse = components['schemas']['ExpenseAttachmentListResponse']
type ExpenseAIPrefill = components['schemas']['ExpenseAIPrefill']

export type {
  ExpenseRead,
  ExpenseInput,
  ExpenseListItem,
  ExpenseAttachmentRead,
  ExpenseAIPrefill,
}

export const useExpensesStore = defineStore('expenses', () => {
  // List state
  const items = ref<ExpenseListItem[]>([])
  const total = ref(0)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  // Filters
  const query = ref('')
  const categoryIdFilter = ref<string | null>(null)
  const deductibleFilter = ref<boolean | null>(null)
  const isDraftFilter = ref<boolean | null>(null)
  const dateFrom = ref<string | null>(null)
  const dateTo = ref<string | null>(null)
  const limit = ref(50)
  const offset = ref(0)
  const sortBy = ref<'expense_date' | 'created_at'>('expense_date')

  async function fetchExpenses(kind?: ExpenseKind) {
    loading.value = true
    error.value = null
    try {
      const params = new URLSearchParams()
      if (query.value) params.set('q', query.value)
      if (categoryIdFilter.value) params.set('category_id', categoryIdFilter.value)
      if (deductibleFilter.value !== null) params.set('deductible', String(deductibleFilter.value))
      if (isDraftFilter.value !== null) params.set('is_draft', String(isDraftFilter.value))
      if (dateFrom.value) params.set('date_from', dateFrom.value)
      if (dateTo.value) params.set('date_to', dateTo.value)
      if (kind) params.set('kind', kind)
      params.set('limit', String(limit.value))
      params.set('offset', String(offset.value))
      params.set('sort_by', sortBy.value)
      const qs = params.toString()
      const url = `/api/v1/expenses${qs ? '?' + qs : ''}`
      const data = await get<ExpenseListResponse>(url)
      items.value = data.items
      total.value = data.total
    } catch (e: unknown) {
      error.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function getExpense(id: string): Promise<ExpenseRead> {
    return await get<ExpenseRead>(`/api/v1/expenses/${id}`)
  }

  async function createExpense(body: ExpenseInput): Promise<ExpenseRead> {
    saving.value = true
    error.value = null
    try {
      return await post<ExpenseRead>('/api/v1/expenses', body)
    } catch (e: unknown) {
      if (e instanceof ApiError) error.value = e.message
      else error.value = String(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function updateExpense(id: string, body: ExpenseInput): Promise<ExpenseRead> {
    saving.value = true
    error.value = null
    try {
      return await put<ExpenseRead>(`/api/v1/expenses/${id}`, body)
    } catch (e: unknown) {
      if (e instanceof ApiError) error.value = e.message
      else error.value = String(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function confirmDraft(id: string, expense: ExpenseRead): Promise<ExpenseRead> {
    // Confirm a draft via PUT – the backend sets is_draft=False on any PUT (step-1 contract).
    // ExpenseInput does NOT include is_draft (red-line 1); confirmation is derived by the service.
    // Guard: if category_id is null (category was deleted), confirmation requires re-editing;
    // callers must check expense.category_id before calling this function.
    saving.value = true
    error.value = null
    try {
      const body: ExpenseInput = {
        expense_date: expense.expense_date,
        category_id: expense.category_id ?? '',
        supplier_name: expense.supplier_name,
        vat_treatment_id: expense.vat_treatment_id ?? '',
        vat_rate_id: expense.vat_rate_id ?? '',
        net_amount: expense.net_amount,
        vat_amount: expense.vat_amount,
        deductible: expense.deductible,
        reference: expense.reference,
        note: expense.note,
        // Bookkeeping fields (M8.5) — preserve as-is when confirming draft
        paid_by: expense.paid_by,
        business_percentage: expense.business_percentage,
        depreciation_years: expense.depreciation_years,
        include_in_pl: expense.include_in_pl,
        pl_cost_basis: expense.pl_cost_basis,
      }
      return await put<ExpenseRead>(`/api/v1/expenses/${id}`, body)
    } catch (e: unknown) {
      if (e instanceof ApiError) error.value = e.message
      else error.value = String(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  async function deleteExpense(id: string): Promise<void> {
    saving.value = true
    error.value = null
    try {
      await del<null>(`/api/v1/expenses/${id}`)
    } catch (e: unknown) {
      if (e instanceof ApiError) error.value = e.message
      else error.value = String(e)
      throw e
    } finally {
      saving.value = false
    }
  }

  // Attachment operations
  async function listAttachments(expenseId: string): Promise<ExpenseAttachmentRead[]> {
    const data = await get<ExpenseAttachmentListResponse>(`/api/v1/expenses/${expenseId}/attachments`)
    return data.items
  }

  async function uploadAttachment(expenseId: string, file: File): Promise<ExpenseAttachmentRead> {
    const formData = new FormData()
    formData.append('file', file)
    return await http<ExpenseAttachmentRead>(`/api/v1/expenses/${expenseId}/attachments`, {
      method: 'POST',
      body: formData,
    })
  }

  async function deleteAttachment(attachmentId: string): Promise<void> {
    await del<null>(`/api/v1/attachments/${attachmentId}`)
  }

  // AI extraction
  async function aiExtract(attachmentId: string, language?: string): Promise<ExpenseAIPrefill> {
    return await post<ExpenseAIPrefill>('/api/v1/expenses/ai-extract', { attachment_id: attachmentId, language })
  }

  return {
    items,
    total,
    loading,
    saving,
    error,
    query,
    categoryIdFilter,
    deductibleFilter,
    isDraftFilter,
    dateFrom,
    dateTo,
    limit,
    offset,
    sortBy,
    fetchExpenses,
    getExpense,
    createExpense,
    updateExpense,
    confirmDraft,
    deleteExpense,
    listAttachments,
    uploadAttachment,
    deleteAttachment,
    aiExtract,
  }
})
