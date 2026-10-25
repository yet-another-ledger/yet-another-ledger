import { defineStore } from 'pinia'
import { ref } from 'vue'
import { del, get, post, put } from '../api/http'
import { ApiError } from '../api/http'
import type { components } from '../api/schema'

type QuoteRead = components['schemas']['QuoteRead']
type QuoteWrite = components['schemas']['QuoteWrite']
type QuoteListResponse = components['schemas']['QuoteListResponse']
type QuoteListItem = components['schemas']['QuoteListItem']
type QuoteStatusWrite = components['schemas']['QuoteStatusWrite']
type QuoteReactivateWrite = components['schemas']['QuoteReactivateWrite']
type QuoteCalculationRequest = components['schemas']['QuoteCalculationRequest']
type QuoteCalculationRead = components['schemas']['QuoteCalculationRead']
type InvoiceRead = components['schemas']['InvoiceRead']

export type {
  QuoteRead,
  QuoteWrite,
  QuoteListItem,
  QuoteStatusWrite,
  QuoteReactivateWrite,
  QuoteCalculationRequest,
  QuoteCalculationRead,
}

export const useQuotesStore = defineStore('quotes', () => {
  const items = ref<QuoteListItem[]>([])
  const total = ref(0)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  const query = ref('')
  const customerIdFilter = ref<string | null>(null)
  const statusFilter = ref<string | null>(null)
  const dateFrom = ref<string | null>(null)
  const dateTo = ref<string | null>(null)
  const limit = ref(50)
  const offset = ref(0)
  const sortBy = ref<'quote_date' | 'created_at' | 'quote_number'>('quote_date')

  async function fetchQuotes() {
    loading.value = true
    error.value = null
    try {
      const params = new URLSearchParams()
      if (query.value) params.set('q', query.value)
      if (customerIdFilter.value) params.set('customer_id', customerIdFilter.value)
      if (statusFilter.value) params.set('status', statusFilter.value)
      if (dateFrom.value) params.set('date_from', dateFrom.value)
      if (dateTo.value) params.set('date_to', dateTo.value)
      params.set('limit', String(limit.value))
      params.set('offset', String(offset.value))
      params.set('sort_by', sortBy.value)
      const qs = params.toString()
      const url = `/api/v1/quotes${qs ? '?' + qs : ''}`
      const data = await get<QuoteListResponse>(url)
      items.value = data.items
      total.value = data.total
    } catch (e: unknown) {
      error.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function fetchQuote(id: string): Promise<QuoteRead> {
    return await get<QuoteRead>(`/api/v1/quotes/${id}`)
  }

  async function createQuote(data: QuoteWrite): Promise<QuoteRead> {
    saving.value = true
    error.value = null
    try {
      return await post<QuoteRead>('/api/v1/quotes', data)
    } finally {
      saving.value = false
    }
  }

  async function updateQuote(id: string, data: QuoteWrite): Promise<QuoteRead> {
    saving.value = true
    error.value = null
    try {
      return await put<QuoteRead>(`/api/v1/quotes/${id}`, data)
    } finally {
      saving.value = false
    }
  }

  async function deleteQuote(id: string): Promise<void> {
    await del(`/api/v1/quotes/${id}`)
  }

  async function transitionStatus(id: string, body: QuoteStatusWrite): Promise<QuoteRead> {
    return await post<QuoteRead>(`/api/v1/quotes/${id}/status`, body)
  }

  async function duplicateQuote(id: string): Promise<QuoteRead> {
    return await post<QuoteRead>(`/api/v1/quotes/${id}/duplicate`, undefined)
  }

  async function convertQuote(id: string): Promise<InvoiceRead> {
    return await post<InvoiceRead>(`/api/v1/quotes/${id}/convert`, {})
  }

  async function reactivateQuote(id: string, body: QuoteReactivateWrite): Promise<QuoteRead> {
    return await post<QuoteRead>(`/api/v1/quotes/${id}/reactivate`, body)
  }

  async function calculatePreview(data: QuoteCalculationRequest): Promise<QuoteCalculationRead> {
    return await post<QuoteCalculationRead>('/api/v1/quotes/calculate', data)
  }

  return {
    items,
    total,
    loading,
    saving,
    error,
    query,
    customerIdFilter,
    statusFilter,
    dateFrom,
    dateTo,
    limit,
    offset,
    sortBy,
    fetchQuotes,
    fetchQuote,
    createQuote,
    updateQuote,
    deleteQuote,
    transitionStatus,
    duplicateQuote,
    convertQuote,
    reactivateQuote,
    calculatePreview,
  }
})
