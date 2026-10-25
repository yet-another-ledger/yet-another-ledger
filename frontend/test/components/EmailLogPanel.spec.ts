import { expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
const http = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../src/api/http', () => http)
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('naive-ui', () => {
  const box = { template: '<div><slot /></div>' }
  return { NCard: box, NTag: box, NText: box, NSpin: box, NEmpty: box, NAlert: box, NSpace: box, NDivider: box }
})
import EmailLogPanel from '../../src/components/EmailLogPanel.vue'
it('keeps SMTP SENT visible alongside saved and failed copy outcomes', async () => {
  http.get.mockResolvedValue({ items: [
    { id: 'saved', status: 'SENT', sent_copy_status: 'SAVED', sent_copy_saved_at: '2026-10-25T10:00:00Z', created_at: '2026-10-25T10:00:00Z' },
    { id: 'failed', status: 'SENT', sent_copy_status: 'FAILED', sent_copy_error_code: 'IMAP_TIMEOUT', created_at: '2026-10-25T10:00:00Z' },
    { id: 'old', status: 'SENT', sent_copy_status: 'NOT_ATTEMPTED', created_at: '2026-10-25T10:00:00Z' },
  ] })
  const wrapper = mount(EmailLogPanel, { props: { docType: 'invoice', docId: 'invoice-1' } })
  await flushPromises()
  expect(wrapper.text().match(/emailLog.statusSENT/g)).toHaveLength(3)
  expect(wrapper.text()).toContain('sentCopy.SAVED')
  expect(wrapper.text()).toContain('sentCopy.FAILED')
  expect(wrapper.text()).toContain('IMAP_TIMEOUT')
  expect(wrapper.text()).not.toContain('emailLog.statusFAILED')
  wrapper.unmount()
})
