import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), http: vi.fn(), del: vi.fn(), post: vi.fn() }))
vi.mock('../../src/api/http', () => api)
import { useReceiptInboxStore } from '../../src/stores/receiptInbox'

describe('receipt inbox upload contract', () => {
  beforeEach(() => { vi.clearAllMocks(); setActivePinia(createPinia()) })

  it('uploads and appends every selected file in one multipart group, preserving locale and refreshed item response', async () => {
    const refreshed = { id: 'inbox-1', files: [{ id: '1' }, { id: '2' }, { id: '3' }], ai_state: 'READY' }
    api.http.mockResolvedValue(refreshed)
    const store = useReceiptInboxStore()
    const files = [new File(['one'], 'one.jpg', { type: 'image/jpeg' }), new File(['two'], 'two.pdf', { type: 'application/pdf' })]
    const created = await store.upload(files, 'zh')
    expect(created).toBe(refreshed)
    expect(api.http.mock.calls[0][0]).toBe('/api/v1/receipt-inbox?language=zh')
    expect([...api.http.mock.calls[0][1].body.getAll('files')]).toEqual(files)
    const amended = await store.append('inbox-1', files, 'en')
    expect(amended.files).toHaveLength(3)
    expect(api.http.mock.calls[1][0]).toBe('/api/v1/receipt-inbox/inbox-1/files?language=en')
    expect([...api.http.mock.calls[1][1].body.getAll('files')]).toEqual(files)
  })

  it('requests the selected list page and keeps a late older page from replacing it', async () => {
    const store = useReceiptInboxStore()
    let finishFirst!: (value: unknown) => void
    api.get.mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve }))
    api.get.mockResolvedValueOnce({ items: [{ id: 'oldest' }], total: 101 })
    const first = store.fetch('PENDING', 0)
    await store.fetch('PENDING', 100)
    finishFirst({ items: [{ id: 'newest' }], total: 101 })
    await first
    expect(api.get.mock.calls.map(call => call[0])).toEqual([
      '/api/v1/receipt-inbox?status=PENDING&limit=100&offset=0',
      '/api/v1/receipt-inbox?status=PENDING&limit=100&offset=100',
    ])
    expect(store.items.map(item => item.id)).toEqual(['oldest'])
    expect(store.total).toBe(101)
  })
})
