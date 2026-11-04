<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Draggable from 'vuedraggable'
import { get, postFile, fetchBlob, saveBlob } from '../api/http'
import PdfPreviewDialog from './PdfPreviewDialog.vue'
import type { components } from '../api/schema'

type FileRead = components['schemas']['PdfAppendixFileRead']
type FileList = components['schemas']['PdfAppendixFileListResponse']
type Limits = components['schemas']['PdfAppendixLimitsRead']
type SavedAppendix = components['schemas']['QuoteAppendixRead']

interface EditorItem {
  file_id: string
  owner_quote_id: string | null
  source: 'LIBRARY' | 'QUOTE'
  title_snapshot: string
  title: string
  original_filename: string
  sha256: string
  size_bytes: number
  page_count: number
  is_archived: boolean
}

const props = defineProps<{
  quoteId: string | null
  appendices: SavedAppendix[]
  readonly: boolean
  disabled: boolean
}>()
const emit = defineEmits<{ change: [items: EditorItem[]] }>()
const { t } = useI18n()
const selected = ref<EditorItem[]>([])
const limits = ref<Limits | null>(null)
const librarySearch = ref('')
const libraryPage = ref(0)
const ownPage = ref(0)
const library = ref<FileList>({ items: [], total: 0 })
const ownFiles = ref<FileList>({ items: [], total: 0 })
const ownFilesQuoteId = ref<string | null>(null)
const loading = ref(false)
const uploading = ref(false)
const error = ref<string | null>(null)
const previewShow = ref(false)
const previewSrc = ref<string | null>(null)
const previewFallback = ref('appendix.pdf')
const pageSize = 10
let requestGeneration = 0
let quoteGeneration = 0
let componentAlive = true
let uploadTask: Promise<void> = Promise.resolve()

function fromSaved(item: SavedAppendix): EditorItem {
  return {
    file_id: item.file_id, owner_quote_id: item.owner_quote_id, source: item.source,
    title_snapshot: item.title_snapshot, title: item.title,
    original_filename: item.original_filename, sha256: item.sha256,
    size_bytes: item.size_bytes, page_count: item.page_count, is_archived: item.is_archived,
  }
}
watch(() => [props.quoteId, props.appendices] as const, () => {
  selected.value = props.appendices.map(fromSaved)
  emit('change', selected.value)
}, { immediate: true, deep: true })
watch(() => props.quoteId, () => {
  quoteGeneration += 1
  ownPage.value = 0
  ownFiles.value = { items: [], total: 0 }
  ownFilesQuoteId.value = null
  uploading.value = false
  previewShow.value = false
  previewSrc.value = null
}, { flush: 'sync' })
watch(librarySearch, () => { libraryPage.value = 0 })

const selectedIds = computed(() => new Set(selected.value.map(item => item.file_id)))
const inputBytes = computed(() => selected.value.reduce((total, item) => total + item.size_bytes, 0))
const selectedPages = computed(() => selected.value.reduce((total, item) => total + item.page_count, 0))
const canMutate = computed(() => !props.readonly && !props.disabled)

async function loadFiles(): Promise<void> {
  const quoteId = props.quoteId
  const quoteToken = quoteGeneration
  const generation = ++requestGeneration
  loading.value = true
  error.value = null
  try {
    const [newLimits, libraryResult, ownResult] = await Promise.all([
      get<Limits>('/api/v1/pdf-library/limits'),
      get<FileList>(`/api/v1/pdf-library?search=${encodeURIComponent(librarySearch.value)}&limit=${pageSize}&offset=${libraryPage.value * pageSize}`),
      quoteId ? get<FileList>(`/api/v1/quotes/${quoteId}/appendix-files?limit=${pageSize}&offset=${ownPage.value * pageSize}`) : Promise.resolve({ items: [], total: 0 }),
    ])
    if (!componentAlive || quoteToken !== quoteGeneration || generation !== requestGeneration || quoteId !== props.quoteId) return
    limits.value = newLimits
    library.value = libraryResult
    ownFiles.value = ownResult
    ownFilesQuoteId.value = quoteId
  } catch (cause) {
    if (componentAlive && quoteToken === quoteGeneration && generation === requestGeneration && quoteId === props.quoteId) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (componentAlive && quoteToken === quoteGeneration && generation === requestGeneration && quoteId === props.quoteId) loading.value = false
  }
}
watch(() => [props.quoteId, librarySearch.value, libraryPage.value, ownPage.value] as const, () => { void loadFiles() }, { immediate: true })

function choose(file: FileRead): void {
  if (!canMutate.value || selectedIds.value.has(file.id)) return
  if (file.source === 'QUOTE' && file.owner_quote_id !== props.quoteId) return
  if (!canChoose(file)) {
    error.value = t('quotes.appendices.selectionLimit')
    return
  }
  selected.value = [...selected.value, {
    file_id: file.id, owner_quote_id: file.owner_quote_id, source: file.source,
    title_snapshot: file.title, title: file.title, original_filename: file.original_filename,
    sha256: file.sha256, size_bytes: file.size_bytes, page_count: file.page_count,
    is_archived: file.is_archived,
  }]
  emit('change', selected.value)
}
function canChoose(file: FileRead): boolean {
  if (!limits.value) return false
  return selected.value.length < limits.value.max_quote_appendices
    && inputBytes.value + file.size_bytes <= limits.value.max_quote_appendix_input_bytes
    && selectedPages.value + file.page_count <= limits.value.max_quote_pdf_pages
}
function remove(fileId: string): void {
  if (!canMutate.value) return
  selected.value = selected.value.filter(item => item.file_id !== fileId)
  emit('change', selected.value)
}
function reorder(items: EditorItem[]): void {
  if (!canMutate.value) return
  selected.value = items
  emit('change', selected.value)
}
function upload(event: Event): Promise<void> {
  uploadTask = performUpload(event)
  return uploadTask
}
async function performUpload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const quoteId = props.quoteId
  if (!file || !quoteId || !canMutate.value || uploading.value) return
  if (!limits.value) {
    error.value = t('quotes.appendices.limitsUnavailable')
    return
  }
  if (limits.value && file.size > limits.value.max_appendix_file_bytes) {
    error.value = t('quotes.appendices.fileTooLarge')
    return
  }
  uploading.value = true
  error.value = null
  const quoteToken = quoteGeneration
  try {
    const uploaded = await postFile<FileRead>(`/api/v1/quotes/${quoteId}/appendix-files`, file)
    if (!componentAlive || quoteToken !== quoteGeneration || quoteId !== props.quoteId) return
    selected.value = [...selected.value, {
      file_id: uploaded.id, owner_quote_id: uploaded.owner_quote_id, source: uploaded.source,
      title_snapshot: uploaded.title, title: uploaded.title, original_filename: uploaded.original_filename,
      sha256: uploaded.sha256, size_bytes: uploaded.size_bytes, page_count: uploaded.page_count,
      is_archived: uploaded.is_archived,
    }]
    emit('change', selected.value)
    uploading.value = false
    await loadFiles()
  } catch (cause) {
    if (componentAlive && quoteToken === quoteGeneration && quoteId === props.quoteId) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (componentAlive && quoteToken === quoteGeneration && quoteId === props.quoteId) uploading.value = false
  }
}
function preview(item: Pick<EditorItem, 'file_id' | 'original_filename'> | FileRead): void {
  const id = 'id' in item ? item.id : item.file_id
  previewSrc.value = `/api/v1/pdf-files/${id}/content?disposition=inline`
  previewFallback.value = item.original_filename
  previewShow.value = true
}
async function download(item: Pick<EditorItem, 'file_id' | 'original_filename'> | FileRead): Promise<void> {
  const quoteId = props.quoteId
  const quoteToken = quoteGeneration
  try {
    const id = 'id' in item ? item.id : item.file_id
    const { blob, filename } = await fetchBlob(`/api/v1/pdf-files/${id}/content?disposition=attachment`, item.original_filename)
    if (!componentAlive || quoteToken !== quoteGeneration || quoteId !== props.quoteId) return
    saveBlob(blob, filename)
  } catch (cause) {
    if (componentAlive && quoteToken === quoteGeneration && quoteId === props.quoteId) error.value = cause instanceof Error ? cause.message : String(cause)
  }
}
function formatBytes(bytes: number): string {
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
async function waitForUpload(): Promise<void> {
  await uploadTask
}
defineExpose({ reload: loadFiles, waitForUpload, uploading })
onBeforeUnmount(() => {
  componentAlive = false
  quoteGeneration += 1
  requestGeneration += 1
})
</script>

<template>
  <n-card :title="t('quotes.appendices.title')" style="margin-bottom: 16px" data-testid="quote-appendices">
    <n-alert v-if="error" type="error" style="margin-bottom: 12px">
      {{ error }}
      <n-button v-if="!limits" size="small" @click="loadFiles">{{ t('quotes.appendices.retry') }}</n-button>
    </n-alert>
    <n-alert v-if="limits" type="info" style="margin-bottom: 12px">
      {{ t('quotes.appendices.limits', { count: selected.length, maxCount: limits.max_quote_appendices, input: formatBytes(inputBytes), maxInput: formatBytes(limits.max_quote_appendix_input_bytes), maxFile: formatBytes(limits.max_appendix_file_bytes), selectedPages, pages: limits.max_quote_pdf_pages, output: formatBytes(limits.max_quote_pdf_bytes) }) }}
    </n-alert>
    <n-alert v-if="!quoteId" type="info" style="margin-bottom: 12px">{{ t('quotes.appendices.saveToUpload') }}</n-alert>
    <div v-if="selected.length">
      <draggable :model-value="selected" item-key="file_id" handle=".appendix-drag-handle" :disabled="!canMutate" @update:model-value="reorder">
        <template #item="{ element }">
          <div class="appendix-row" :data-file-id="element.file_id">
            <button v-if="canMutate" class="appendix-drag-handle" type="button" :aria-label="t('quotes.appendices.reorder')">⠿</button>
            <div class="appendix-info">
              <strong>{{ element.title_snapshot }}</strong>
              <n-tag size="small">{{ t(`quotes.appendices.source${element.source}`) }}</n-tag>
              <n-tag v-if="element.is_archived" size="small" type="warning">{{ t('quotes.appendices.archived') }}</n-tag>
              <div>{{ element.original_filename }} · {{ element.page_count }} {{ t('quotes.appendices.pages') }} · {{ formatBytes(element.size_bytes) }}</div>
            </div>
            <n-space>
              <n-button size="small" @click="preview(element)">{{ t('pdf.preview') }}</n-button>
              <n-button size="small" @click="download(element)">{{ t('pdf.download') }}</n-button>
              <n-button v-if="canMutate" size="small" type="error" @click="remove(element.file_id)">{{ t('quotes.appendices.remove') }}</n-button>
            </n-space>
          </div>
        </template>
      </draggable>
    </div>
    <n-empty v-else :description="t('quotes.appendices.empty')" size="small" />

    <n-divider />
    <n-grid :cols="2" :x-gap="16" responsive="screen">
      <n-gi>
        <h3>{{ t('quotes.appendices.library') }}</h3>
        <n-input v-model:value="librarySearch" clearable :placeholder="t('quotes.appendices.search')" :disabled="!canMutate" />
        <n-spin :show="loading">
          <n-list>
            <n-list-item v-for="file in library.items" :key="file.id">
              <n-thing :title="file.title" :description="`${file.original_filename} · ${file.page_count} ${t('quotes.appendices.pages')} · ${formatBytes(file.size_bytes)}`" />
              <template #suffix>
                <n-button size="small" @click="preview(file)">{{ t('pdf.preview') }}</n-button>
                <n-button size="small" @click="download(file)">{{ t('pdf.download') }}</n-button>
                <n-button size="small" :disabled="!canMutate || selectedIds.has(file.id) || !canChoose(file)" @click="choose(file)">{{ selectedIds.has(file.id) ? t('quotes.appendices.selected') : t('quotes.appendices.add') }}</n-button>
              </template>
            </n-list-item>
          </n-list>
        </n-spin>
        <n-space justify="space-between">
          <n-button size="small" :disabled="libraryPage === 0 || loading" @click="libraryPage--">{{ t('quotes.appendices.previous') }}</n-button>
          <span>{{ libraryPage + 1 }} / {{ Math.max(1, Math.ceil(library.total / pageSize)) }}</span>
          <n-button size="small" :disabled="(libraryPage + 1) * pageSize >= library.total || loading" @click="libraryPage++">{{ t('quotes.appendices.next') }}</n-button>
        </n-space>
      </n-gi>
      <n-gi>
        <h3>{{ t('quotes.appendices.ownFiles') }}</h3>
        <label v-if="canMutate && quoteId" class="upload-label">
          {{ uploading ? t('quotes.appendices.uploading') : t('quotes.appendices.upload') }}
          <input type="file" accept="application/pdf,.pdf" :disabled="uploading || !limits" @change="upload" />
        </label>
        <n-empty v-if="ownFilesQuoteId !== quoteId || !ownFiles.items.length" :description="t('quotes.appendices.noOwnFiles')" size="small" />
        <n-list>
          <n-list-item v-for="file in ownFilesQuoteId === quoteId ? ownFiles.items : []" :key="file.id">
            <n-thing :title="file.title" :description="`${file.original_filename} · ${file.page_count} ${t('quotes.appendices.pages')} · ${formatBytes(file.size_bytes)}`" />
            <template #suffix>
                <n-button size="small" @click="preview(file)">{{ t('pdf.preview') }}</n-button>
                <n-button size="small" @click="download(file)">{{ t('pdf.download') }}</n-button>
              <n-button size="small" :disabled="!canMutate || file.owner_quote_id !== quoteId || selectedIds.has(file.id) || !canChoose(file)" @click="choose(file)">{{ selectedIds.has(file.id) ? t('quotes.appendices.selected') : t('quotes.appendices.add') }}</n-button>
            </template>
          </n-list-item>
        </n-list>
        <n-space v-if="ownFiles.total > pageSize" justify="space-between">
          <n-button size="small" :disabled="ownPage === 0 || loading" @click="ownPage--">{{ t('quotes.appendices.previous') }}</n-button>
          <span>{{ ownPage + 1 }} / {{ Math.max(1, Math.ceil(ownFiles.total / pageSize)) }}</span>
          <n-button size="small" :disabled="(ownPage + 1) * pageSize >= ownFiles.total || loading" @click="ownPage++">{{ t('quotes.appendices.next') }}</n-button>
        </n-space>
      </n-gi>
    </n-grid>
    <pdf-preview-dialog v-model:show="previewShow" :src="previewSrc" :fallback-filename="previewFallback" />
  </n-card>
</template>

<style scoped>
.appendix-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--n-border-color); }
.appendix-info { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.appendix-info > div { flex-basis: 100%; color: var(--n-text-color-3); font-size: 12px; }
.appendix-drag-handle { cursor: grab; border: 0; background: transparent; color: inherit; font-size: 22px; }
.upload-label { display: inline-block; cursor: pointer; padding: 6px 12px; margin-bottom: 8px; border: 1px solid var(--n-border-color); border-radius: 4px; }
.upload-label input { display: block; max-width: 240px; }
</style>
