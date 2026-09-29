import { defineStore } from 'pinia'
import { ref } from 'vue'
import { del, get, http, post } from '../api/http'
import type { components } from '../api/schema'

type Inbox = components['schemas']['ReceiptInboxRead']
type InboxPage = components['schemas']['ReceiptInboxListResponse']
type ExpenseInput = components['schemas']['ExpenseInput']
type Confirmation = components['schemas']['ReceiptInboxConfirmationRead']
type InboxStatus = components['schemas']['ReceiptInboxStatus']
export type { Inbox, InboxStatus }

export const useReceiptInboxStore = defineStore('receiptInbox', () => {
  const items = ref<Inbox[]>([])
  const total = ref(0)
  const loading = ref(false)
  const error = ref<string | null>(null)

  let fetchVersion = 0
  async function fetch(status: InboxStatus, offset = 0) {
    const version = ++fetchVersion
    loading.value = true
    error.value = null
    try {
      const page = await get<InboxPage>(`/api/v1/receipt-inbox?status=${status}&limit=100&offset=${offset}`)
      if (version === fetchVersion) { items.value = page.items; total.value = page.total }
    } catch (e) {
      if (version === fetchVersion) error.value = e instanceof Error ? e.message : String(e)
      throw e
    } finally { if (version === fetchVersion) loading.value = false }
  }
  const detail = (id: string) => get<Inbox>(`/api/v1/receipt-inbox/${id}`)
  function upload(files: File[], language: string) {
    const data = new FormData()
    files.forEach(file => data.append('files', file))
    return http<Inbox>(`/api/v1/receipt-inbox?language=${language}`, { method: 'POST', body: data })
  }
  function append(id: string, files: File[], language: string) {
    const data = new FormData()
    files.forEach(file => data.append('files', file))
    return http<Inbox>(`/api/v1/receipt-inbox/${id}/files?language=${language}`, { method: 'POST', body: data })
  }
  const removeFile = (id: string, fileId: string) => del<Inbox>(`/api/v1/receipt-inbox/${id}/files/${fileId}`)
  const discard = (id: string) => del<null>(`/api/v1/receipt-inbox/${id}`)
  const retry = (id: string, language: string) => post<Inbox>(`/api/v1/receipt-inbox/${id}/extract`, { language })
  const confirm = (id: string, body: ExpenseInput) => post<Confirmation>(`/api/v1/receipt-inbox/${id}/confirm`, body)
  return { items, total, loading, error, fetch, detail, upload, append, removeFile, discard, retry, confirm }
})
