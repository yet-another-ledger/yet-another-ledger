import { defineStore } from 'pinia'
import { ref } from 'vue'
import { del, get, post, put } from '../api/http'
import { ApiError } from '../api/http'
import type { components } from '../api/schema'

type InvoiceRead = components['schemas']['InvoiceRead']
type InvoiceWrite = components['schemas']['InvoiceWrite']
type InvoiceListResponse = components['schemas']['InvoiceListResponse']
type InvoiceListItem = components['schemas']['InvoiceListItem']
type InvoiceStatusWrite = components['schemas']['InvoiceStatusWrite']
type InvoiceCalculationRequest = components['schemas']['InvoiceCalculationRequest']
type InvoiceCalculationRead = components['schemas']['InvoiceCalculationRead']
type InvoiceRevisionCalculateRequest = components['schemas']['InvoiceRevisionCalculateRequest']
type InvoiceRevisionCreate = components['schemas']['InvoiceRevisionCreate']
type InvoiceRevisionListRead = components['schemas']['InvoiceRevisionListRead']
type ProductInvoiceOptionListResponse = components['schemas']['ProductInvoiceOptionListResponse']
type ProductInvoiceOptionRead = components['schemas']['ProductInvoiceOptionRead']

export type {
  InvoiceRead,
  InvoiceWrite,
  InvoiceListItem,
  InvoiceCalculationRequest,
  InvoiceCalculationRead,
  ProductInvoiceOptionRead,
}

export const useInvoicesStore = defineStore('invoices', () => {
  const items = ref<InvoiceListItem[]>([])
  const total = ref(0)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  const query = ref('')
  const customerIdFilter = ref<string | null>(null)
  const statusFilter = ref<string | null>(null)
  const paidStatusFilter = ref<string | null>(null)
  const documentKindFilter = ref<components['schemas']['InvoiceDocumentKind'] | null>(null)
  const dateFrom = ref<string | null>(null)
  const dateTo = ref<string | null>(null)
  const limit = ref(50)
  const offset = ref(0)
  const sortBy = ref<'invoice_date' | 'created_at' | 'invoice_number'>('invoice_date')

  async function fetchInvoices() {
    loading.value = true
    error.value = null
    try {
      const params = new URLSearchParams()
      if (query.value) params.set('q', query.value)
      if (customerIdFilter.value) params.set('customer_id', customerIdFilter.value)
      if (statusFilter.value) params.set('status', statusFilter.value)
      if (paidStatusFilter.value) params.set('paid_status', paidStatusFilter.value)
      if (documentKindFilter.value) params.set('document_kind', documentKindFilter.value)
      if (dateFrom.value) params.set('date_from', dateFrom.value)
      if (dateTo.value) params.set('date_to', dateTo.value)
      params.set('limit', String(limit.value))
      params.set('offset', String(offset.value))
      params.set('sort_by', sortBy.value)
      const qs = params.toString()
      const url = `/api/v1/invoices${qs ? '?' + qs : ''}`
      const data = await get<InvoiceListResponse>(url)
      items.value = data.items
      total.value = data.total
    } catch (e: unknown) {
      if (e instanceof ApiError) {
        error.value = e.message
      } else {
        error.value = String(e)
      }
    } finally {
      loading.value = false
    }
  }

  async function fetchInvoice(id: string): Promise<InvoiceRead> {
    return await get<InvoiceRead>(`/api/v1/invoices/${id}`)
  }

  async function createInvoice(data: InvoiceWrite): Promise<InvoiceRead> {
    saving.value = true
    error.value = null
    try {
      return await post<InvoiceRead>('/api/v1/invoices', data)
    } finally {
      saving.value = false
    }
  }

  async function updateInvoice(id: string, data: InvoiceWrite): Promise<InvoiceRead> {
    saving.value = true
    error.value = null
    try {
      return await put<InvoiceRead>(`/api/v1/invoices/${id}`, data)
    } finally {
      saving.value = false
    }
  }

  async function deleteInvoice(id: string): Promise<void> {
    await del(`/api/v1/invoices/${id}`)
  }

  async function transitionStatus(id: string, body: InvoiceStatusWrite): Promise<InvoiceRead> {
    return await post<InvoiceRead>(`/api/v1/invoices/${id}/status`, body)
  }

  async function calculatePreview(data: InvoiceCalculationRequest): Promise<InvoiceCalculationRead> {
    return await post<InvoiceCalculationRead>('/api/v1/invoices/calculate', data)
  }

  async function calculateRevision(id: string, data: InvoiceRevisionCalculateRequest): Promise<InvoiceCalculationRead> {
    return await post<InvoiceCalculationRead>(`/api/v1/invoices/${id}/revisions/calculate`, data)
  }

  async function saveRevision(id: string, data: InvoiceRevisionCreate): Promise<InvoiceRead> {
    saving.value = true
    error.value = null
    try {
      return await post<InvoiceRead>(`/api/v1/invoices/${id}/revisions`, data)
    } finally {
      saving.value = false
    }
  }

  async function fetchRevisions(id: string, pagination?: { offset: number; limit: number }): Promise<InvoiceRevisionListRead> {
    const query = pagination ? `?offset=${pagination.offset}&limit=${pagination.limit}` : ''
    return await get<InvoiceRevisionListRead>(`/api/v1/invoices/${id}/revisions${query}`)
  }

  async function fetchProductOptions(q?: string): Promise<ProductInvoiceOptionRead[]> {
    const params = new URLSearchParams()
    if (q) params.set('q', q)
    params.set('limit', '30')
    const qs = params.toString()
    const url = `/api/v1/invoice-product-options${qs ? '?' + qs : ''}`
    const data = await get<ProductInvoiceOptionListResponse>(url)
    return data.items
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
    paidStatusFilter,
    documentKindFilter,
    dateFrom,
    dateTo,
    limit,
    offset,
    sortBy,
    fetchInvoices,
    fetchInvoice,
    createInvoice,
    updateInvoice,
    deleteInvoice,
    transitionStatus,
    calculatePreview,
    calculateRevision,
    saveRevision,
    fetchRevisions,
    fetchProductOptions,
  }
})
