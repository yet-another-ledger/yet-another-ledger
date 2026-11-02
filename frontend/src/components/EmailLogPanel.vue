<script setup lang="ts">
/**
 * EmailLogPanel – shows the email send log for a single Invoice or Quote.
 *
 * Displays: sent time / to / subject / status (SENT/FAILED) / error message.
 * Status uses tag colours: SENT=success, FAILED=error.
 *
 * Call `refresh()` via template ref after a successful send to reload.
 */

import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard, NTag, NText, NSpin, NEmpty, NAlert, NSpace, NDivider,
} from 'naive-ui'
import { get } from '../api/http'
import { formatDateTime } from '../utils/date'
import type { components } from '../api/schema'

type EmailLogRead = components['schemas']['EmailLogRead']
type EmailLogListResponse = components['schemas']['EmailLogListResponse']

const props = defineProps<{
  /** 'invoice' | 'quote' */
  docType: 'invoice' | 'quote'
  /** document id */
  docId: string
}>()

const { t } = useI18n()

const items = ref<EmailLogRead[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

async function refresh() {
  if (!props.docId) return
  loading.value = true
  error.value = null
  try {
    const endpointBase = props.docType === 'invoice' ? '/api/v1/invoices' : '/api/v1/quotes'
    const res = await get<EmailLogListResponse>(`${endpointBase}/${props.docId}/emails`)
    items.value = res.items
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// Expose refresh so parent can call it after a successful send
defineExpose({ refresh })

onMounted(() => {
  refresh()
})

function statusType(status: string): 'success' | 'error' {
  return status === 'SENT' ? 'success' : 'error'
}

function fmtDate(dt: string): string {
  return formatDateTime(dt)
}
</script>

<template>
  <n-card :title="t('emailLog.title')" style="margin-bottom: 16px">
    <n-spin :show="loading">
      <n-alert v-if="error" type="error" style="margin-bottom: 12px">
        {{ error }}
      </n-alert>

      <n-empty
        v-if="!loading && items.length === 0"
        :description="t('emailLog.empty')"
        size="small"
        style="padding: 12px 0"
      />

      <div v-else>
        <div
          v-for="(item, idx) in items"
          :key="item.id"
          class="log-row"
        >
          <n-space align="center" :wrap-item="false" style="gap: 8px; flex-wrap: wrap">
            <n-tag :type="statusType(item.status)" size="small">
              {{ t(`emailLog.status${item.status}`) }}
            </n-tag>
            <n-text strong style="font-size: 13px">{{ item.to_email }}</n-text>
            <n-text depth="3" style="font-size: 12px">{{ fmtDate(item.created_at) }}</n-text>
            <n-text v-if="item.locale" depth="3" style="font-size: 12px">· {{ item.locale }}</n-text>
          </n-space>
          <n-text style="font-size: 13px; display: block; margin-top: 2px">
            {{ item.subject }}
          </n-text>
          <n-text v-if="item.cc" depth="3" style="font-size: 12px; display: block">
            {{ t('emailLog.cc') }}: {{ item.cc }}
          </n-text>
          <n-tag v-if="item.sent_copy_status && item.sent_copy_status !== 'NOT_ATTEMPTED'"
            :type="item.sent_copy_status === 'SAVED' ? 'success' : 'warning'" size="small">
            {{ t(`sentCopy.${item.sent_copy_status}`) }}
          </n-tag>
          <n-text v-if="item.sent_copy_saved_at" depth="3" style="font-size: 12px">
            {{ fmtDate(item.sent_copy_saved_at) }}
          </n-text>
          <n-text v-if="item.sent_copy_error_code" depth="3" style="font-size: 12px">
            {{ item.sent_copy_error_code }}
          </n-text>
          <n-text
            v-if="item.error_message"
            type="error"
            style="font-size: 12px; display: block; margin-top: 2px"
          >
            {{ item.error_message }}
          </n-text>
          <n-divider
            v-if="idx < items.length - 1"
            style="margin: 8px 0"
          />
        </div>
      </div>
    </n-spin>
  </n-card>
</template>

<style scoped>
.log-row {
  padding: 4px 0;
}
</style>
