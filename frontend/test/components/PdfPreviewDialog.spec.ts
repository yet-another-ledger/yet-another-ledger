import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const http = vi.hoisted(() => ({ fetchBlob: vi.fn(), saveBlob: vi.fn() }))
vi.mock('../../src/api/http', () => ({
  ...http,
  ApiError: class ApiError extends Error {
    constructor(public status: number, public detail: unknown, message: string) { super(message) }
  },
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('naive-ui', async () => {
  const { defineComponent } = await import('vue')
  const slot = defineComponent({ template: '<div><slot /><slot name="footer" /></div>' })
  const button = defineComponent({ props: ['disabled'], emits: ['click'], template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /><slot name="icon" /></button>' })
  return { NModal: slot, NSpin: slot, NAlert: slot, NButton: button, NSpace: slot, NIcon: slot }
})

import PdfPreviewDialog from '../../src/components/PdfPreviewDialog.vue'

describe('PDF preview dialog object URL lifecycle', () => {
  const created: string[] = []
  const revoked: string[] = []
  beforeEach(() => {
    vi.clearAllMocks()
    created.length = 0
    revoked.length = 0
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => { const url = `blob:test-${created.length + 1}`; created.push(url); return url }),
      revokeObjectURL: vi.fn((url: string) => revoked.push(url)),
    })
    http.fetchBlob.mockResolvedValue({ blob: new Blob(['%PDF-test']), filename: '资料.pdf' })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('loads, retries a failed preview, and revokes the URL on close and unmount', async () => {
    http.fetchBlob.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(PdfPreviewDialog, { props: { show: true, src: '/pdf/a', fallbackFilename: '资料.pdf' } })
    await flushPromises()
    expect(wrapper.text()).toContain('offline')
    await wrapper.findAll('button').find(button => button.text() === 'common.retry')!.trigger('click')
    await flushPromises()
    expect(created).toEqual(['blob:test-1'])
    await wrapper.setProps({ show: false })
    expect(revoked).toEqual(['blob:test-1'])
    await wrapper.setProps({ show: true })
    await flushPromises()
    expect(created).toEqual(['blob:test-1', 'blob:test-2'])
    wrapper.unmount()
    expect(revoked).toEqual(['blob:test-1', 'blob:test-2'])
  })

  it('ignores late responses for replaced sources and after unmount', async () => {
    let finishOld!: (value: unknown) => void
    http.fetchBlob.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
    http.fetchBlob.mockResolvedValueOnce({ blob: new Blob(['new']), filename: '新.pdf' })
    const wrapper = mount(PdfPreviewDialog, { props: { show: true, src: '/pdf/old' } })
    await wrapper.setProps({ src: '/pdf/new' })
    await flushPromises()
    finishOld({ blob: new Blob(['old']), filename: 'old.pdf' })
    await flushPromises()
    expect(created).toEqual(['blob:test-1'])
    expect(wrapper.find('iframe').attributes('src')).toBe('blob:test-1')
    wrapper.unmount()
    expect(revoked).toEqual(['blob:test-1'])

    let finishUnmounted!: (value: unknown) => void
    http.fetchBlob.mockReturnValueOnce(new Promise(resolve => { finishUnmounted = resolve }))
    const pending = mount(PdfPreviewDialog, { props: { show: true, src: '/pdf/pending' } })
    pending.unmount()
    finishUnmounted({ blob: new Blob(['late']), filename: 'late.pdf' })
    await flushPromises()
    expect(created).toEqual(['blob:test-1'])
  })
})
