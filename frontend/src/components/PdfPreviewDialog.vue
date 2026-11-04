<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { NModal, NSpin, NAlert, NButton, NSpace, NIcon } from 'naive-ui'
import { DownloadOutline } from '@vicons/ionicons5'
import { useI18n } from 'vue-i18n'
import { ApiError, fetchBlob, saveBlob } from '../api/http'

/**
 * In-app PDF preview dialog.
 *
 * Fetches the PDF bytes from a backend endpoint (with the session cookie),
 * wraps them in an object URL and renders them inline in an <iframe> using the
 * browser's built-in PDF viewer.  No backend change is required: the
 * `Content-Disposition: attachment` header on the original response has no
 * effect on an object URL, so the same bytes render inline here and can be
 * saved via the Download button (reusing the already-fetched blob).
 */
const props = defineProps<{
  show: boolean
  /** Backend PDF endpoint URL (e.g. `/api/v1/quotes/{id}/pdf?locale=zh`). */
  src: string | null
  /** Fallback filename if the backend does not advertise one. */
  fallbackFilename?: string
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
}>()

const { t } = useI18n()

const loading = ref(false)
const error = ref<string | null>(null)
const objectUrl = ref<string | null>(null)
let currentBlob: Blob | null = null
let currentFilename = 'document.pdf'
let requestGeneration = 0

function cleanup() {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = null
  }
  currentBlob = null
  error.value = null
}

async function load(src: string) {
  const generation = ++requestGeneration
  cleanup()
  loading.value = true
  try {
    const { blob, filename } = await fetchBlob(src, props.fallbackFilename ?? 'document.pdf')
    if (generation !== requestGeneration) return
    currentBlob = blob
    currentFilename = filename
    objectUrl.value = URL.createObjectURL(blob)
  } catch (e: unknown) {
    if (generation !== requestGeneration) return
    error.value =
      e instanceof ApiError || e instanceof Error ? e.message : t('pdf.previewFailed')
  } finally {
    if (generation === requestGeneration) loading.value = false
  }
}

watch(
  () => [props.show, props.src] as const,
  ([show, src]) => {
    if (show && src) {
      load(src)
    } else if (!show) {
      requestGeneration += 1
      loading.value = false
      cleanup()
    } else {
      requestGeneration += 1
      loading.value = false
      cleanup()
    }
  },
  { immediate: true },
)

function handleClose() {
  emit('update:show', false)
}

function handleDownload() {
  if (currentBlob) saveBlob(currentBlob, currentFilename)
}

onBeforeUnmount(() => {
  requestGeneration += 1
  loading.value = false
  cleanup()
})
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    :title="t('pdf.preview')"
    :bordered="false"
    style="width: 90vw; max-width: 1000px"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="pdf-preview-body">
      <div v-if="loading" class="pdf-preview-status">
        <n-spin :show="true" />
      </div>
      <div v-else-if="error" class="pdf-preview-error">
        <n-alert type="error">{{ error }}</n-alert>
        <n-button :disabled="!src" @click="src && load(src)">{{ t('common.retry') }}</n-button>
      </div>
      <iframe
        v-else-if="objectUrl"
        :src="objectUrl"
        class="pdf-preview-frame"
        title="PDF preview"
      />
    </div>
    <template #footer>
      <n-space justify="end">
        <n-button @click="handleClose">{{ t('common.close') }}</n-button>
        <n-button type="primary" :disabled="!objectUrl" @click="handleDownload">
          <template #icon>
            <n-icon><DownloadOutline /></n-icon>
          </template>
          {{ t('pdf.download') }}
        </n-button>
      </n-space>
    </template>
  </n-modal>
</template>

<style scoped>
.pdf-preview-body {
  height: 78vh;
}

.pdf-preview-status {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.pdf-preview-error {
  display: grid;
  gap: 12px;
}

.pdf-preview-frame {
  width: 100%;
  height: 100%;
  border: none;
}
</style>
