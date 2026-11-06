<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, h, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  useMessage, useDialog,
  NButton, NSpace, NInput, NSelect, NSwitch, NTabs, NTabPane,
  NDataTable, NSpin, NModal, NForm, NFormItem, NTag,
  NText, NInputNumber, NCard, NAlert, NUpload,
} from 'naive-ui'
import { AddOutline, CreateOutline, TrashOutline, EyeOutline, DownloadOutline, ArchiveOutline, RefreshOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { get, post, put, patch, del, http, downloadBlob } from '../../api/http'
import PdfPreviewDialog from '../../components/PdfPreviewDialog.vue'
import type { UploadFileInfo } from 'naive-ui'
import type { components } from '../../api/schema'

type DocumentTemplateRead = components['schemas']['DocumentTemplateRead']
type DocumentTemplateWrite = components['schemas']['DocumentTemplateWrite']
type DocumentTemplateLineWrite = components['schemas']['DocumentTemplateLineWrite']
type ContentBlockRead = components['schemas']['ContentBlockRead']
type ContentBlockWrite = components['schemas']['ContentBlockWrite']
type NoteTemplateRead = components['schemas']['NoteTemplateRead']
type NoteTemplateWrite = components['schemas']['NoteTemplateWrite']
type VatRateRead = components['schemas']['VatRateRead']
type PdfAppendixFileRead = components['schemas']['PdfAppendixFileRead']
type PdfAppendixLimitsRead = components['schemas']['PdfAppendixLimitsRead']
type PdfAppendixFileListResponse = components['schemas']['PdfAppendixFileListResponse']

const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()

// ------------------------------------------------------------------ Document Templates

const templates = ref<DocumentTemplateRead[]>([])
const templatesLoading = ref(false)
const showTemplateModal = ref(false)
const editingTemplate = ref<DocumentTemplateRead | null>(null)
const templateSaving = ref(false)
const templateName = ref('')
const templateAppliesTo = ref<'QUOTE' | 'INVOICE' | 'BOTH'>('BOTH')

interface TemplateLineForm {
  name: string
  description: string | null
  quantity: number
  unit_id: string | null
  unit_name: string | null
  unit_price: number | null
  discount_type: 'NONE' | 'PERCENTAGE' | 'FIXED'
  discount_value: number
  vat_rate_id: string | null
}

const templateLines = ref<TemplateLineForm[]>([])

function emptyTemplateLine(): TemplateLineForm {
  return { name: '', description: null, quantity: 1, unit_id: null, unit_name: null, unit_price: null, discount_type: 'NONE', discount_value: 0, vat_rate_id: null }
}

async function loadTemplates() {
  templatesLoading.value = true
  try {
    templates.value = await get<DocumentTemplateRead[]>('/api/v1/document-templates')
  } catch {
    message.error(t('contentLibrary.loadFailed'))
  } finally {
    templatesLoading.value = false
  }
}

function openCreateTemplate() {
  editingTemplate.value = null
  templateName.value = ''
  templateAppliesTo.value = 'BOTH'
  templateLines.value = [emptyTemplateLine()]
  showTemplateModal.value = true
}

function openEditTemplate(tmpl: DocumentTemplateRead) {
  editingTemplate.value = tmpl
  templateName.value = tmpl.name
  templateAppliesTo.value = tmpl.applies_to
  templateLines.value = (tmpl.lines ?? []).map(l => ({
    name: l.name,
    description: l.description ?? null,
    quantity: Number(l.quantity),
    unit_id: l.unit_id ?? null,
    unit_name: l.unit_name ?? null,
    unit_price: l.unit_price != null ? Number(l.unit_price) : null,
    discount_type: (l.discount_type as 'NONE' | 'PERCENTAGE' | 'FIXED') ?? 'NONE',
    discount_value: Number(l.discount_value),
    vat_rate_id: l.vat_rate_id ?? null,
  }))
  showTemplateModal.value = true
}

async function saveTemplate() {
  if (!templateName.value.trim() || templateLines.value.length === 0) return
  templateSaving.value = true
  try {
    const body: DocumentTemplateWrite = {
      name: templateName.value.trim(),
      applies_to: templateAppliesTo.value,
      lines: templateLines.value.map((l) => {
        const lineWrite: DocumentTemplateLineWrite = {
          name: l.name.trim(),
          description: l.description ?? undefined,
          quantity: String(l.quantity),
          unit_id: l.unit_id ?? undefined,
          unit_name: l.unit_name ?? undefined,
          unit_price: l.unit_price != null ? String(l.unit_price) : undefined,
          discount_type: l.discount_type,
          discount_value: String(l.discount_value),
          vat_rate_id: l.vat_rate_id ?? undefined,
        }
        return lineWrite
      }),
    }
    if (editingTemplate.value) {
      await put(`/api/v1/document-templates/${editingTemplate.value.id}`, body)
      message.success(t('contentLibrary.template.saveSuccess'))
    } else {
      await post('/api/v1/document-templates', body)
      message.success(t('contentLibrary.template.createSuccess'))
    }
    showTemplateModal.value = false
    await loadTemplates()
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.template.saveFailed'))
  } finally {
    templateSaving.value = false
  }
}

function deleteTemplate(tmpl: DocumentTemplateRead) {
  dialog.warning({
    title: t('contentLibrary.template.delete'),
    content: t('contentLibrary.template.deleteConfirm', { name: tmpl.name }),
    positiveText: t('contentLibrary.template.delete'),
    negativeText: t('contentLibrary.cancel'),
    onPositiveClick: async () => {
      try {
        await del(`/api/v1/document-templates/${tmpl.id}`)
        message.success(t('contentLibrary.template.deleteSuccess'))
        await loadTemplates()
      } catch {
        message.error(t('contentLibrary.template.deleteFailed'))
      }
    },
  })
}

const templateColumns = computed(() => [
  { title: t('contentLibrary.template.name'), key: 'name', ellipsis: { tooltip: true } },
  {
    title: t('contentLibrary.template.appliesTo'),
    key: 'applies_to',
    width: 120,
    align: 'center' as const,
    render(row: DocumentTemplateRead) {
      return h(NTag, { size: 'small', type: 'info' }, () => row.applies_to)
    },
  },
  {
    title: t('contentLibrary.template.lineCount'),
    key: 'lines',
    width: 80,
    align: 'center' as const,
    render(row: DocumentTemplateRead) {
      return String((row.lines ?? []).length)
    },
  },
  {
    title: t('contentLibrary.actions'),
    key: 'actions',
    width: 96,
    align: 'center' as const,
    render(row: DocumentTemplateRead) {
      return h(NSpace, { size: 4, justify: 'center', wrapItem: false }, () => [
        h(NButton, { size: 'small', quaternary: true, circle: true, onClick: () => openEditTemplate(row) },
          () => h(NIcon, null, { default: () => h(CreateOutline) })),
        h(NButton, { size: 'small', quaternary: true, circle: true, type: 'error', onClick: () => deleteTemplate(row) },
          () => h(NIcon, null, { default: () => h(TrashOutline) })),
      ])
    },
  },
])

// ------------------------------------------------------------------ Content Blocks

const blocks = ref<ContentBlockRead[]>([])
const blocksLoading = ref(false)
const showBlockModal = ref(false)
const editingBlock = ref<ContentBlockRead | null>(null)
const blockSaving = ref(false)
const blockKind = ref<'WARRANTY' | 'TERMS' | 'BANK' | 'PAYMENT_TERMS'>('WARRANTY')
const blockName = ref('')
const blockBody = ref('')
const blockIsDefault = ref(false)

const kindOptions = computed(() => [
  { label: t('contentLibrary.block.kindWARRANTY'), value: 'WARRANTY' },
  { label: t('contentLibrary.block.kindTERMS'), value: 'TERMS' },
  { label: t('contentLibrary.block.kindBANK'), value: 'BANK' },
  { label: t('contentLibrary.block.kindPAYMENT_TERMS'), value: 'PAYMENT_TERMS' },
])

async function loadBlocks() {
  blocksLoading.value = true
  try {
    blocks.value = await get<ContentBlockRead[]>('/api/v1/content-blocks')
  } catch {
    message.error(t('contentLibrary.loadFailed'))
  } finally {
    blocksLoading.value = false
  }
}

function openCreateBlock() {
  editingBlock.value = null
  blockKind.value = 'WARRANTY'
  blockName.value = ''
  blockBody.value = ''
  blockIsDefault.value = false
  showBlockModal.value = true
}

function openEditBlock(b: ContentBlockRead) {
  editingBlock.value = b
  blockKind.value = b.kind
  blockName.value = b.name
  blockBody.value = b.body
  blockIsDefault.value = b.is_default
  showBlockModal.value = true
}

async function saveBlock() {
  if (!blockName.value.trim() || !blockBody.value.trim()) return
  blockSaving.value = true
  try {
    const body: ContentBlockWrite = {
      kind: blockKind.value,
      name: blockName.value.trim(),
      body: blockBody.value.trim(),
      is_default: blockIsDefault.value,
    }
    if (editingBlock.value) {
      await put(`/api/v1/content-blocks/${editingBlock.value.id}`, body)
      message.success(t('contentLibrary.block.saveSuccess'))
    } else {
      await post('/api/v1/content-blocks', body)
      message.success(t('contentLibrary.block.createSuccess'))
    }
    showBlockModal.value = false
    await loadBlocks()
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.block.saveFailed'))
  } finally {
    blockSaving.value = false
  }
}

function deleteBlock(b: ContentBlockRead) {
  dialog.warning({
    title: t('contentLibrary.block.delete'),
    content: t('contentLibrary.block.deleteConfirm', { name: b.name }),
    positiveText: t('contentLibrary.block.delete'),
    negativeText: t('contentLibrary.cancel'),
    onPositiveClick: async () => {
      try {
        await del(`/api/v1/content-blocks/${b.id}`)
        message.success(t('contentLibrary.block.deleteSuccess'))
        await loadBlocks()
      } catch {
        message.error(t('contentLibrary.block.deleteFailed'))
      }
    },
  })
}

const blockColumns = computed(() => [
  {
    title: t('contentLibrary.block.kind'),
    key: 'kind',
    width: 140,
    render(row: ContentBlockRead) {
      return h(NTag, { size: 'small' }, () => t(`contentLibrary.block.kind${row.kind}`))
    },
  },
  { title: t('contentLibrary.block.name'), key: 'name' },
  {
    title: t('contentLibrary.block.isDefault'),
    key: 'is_default',
    width: 80,
    render(row: ContentBlockRead) {
      return row.is_default ? h(NTag, { size: 'small', type: 'success' }, () => t('contentLibrary.yes')) : '—'
    },
  },
  {
    title: t('contentLibrary.block.body'),
    key: 'body',
    ellipsis: { tooltip: true },
  },
  {
    title: t('contentLibrary.actions'),
    key: 'actions',
    width: 80,
    render(row: ContentBlockRead) {
      return h(NSpace, { size: 4, justify: 'center', wrapItem: false }, () => [
        h(NButton, { size: 'small', quaternary: true, circle: true, onClick: () => openEditBlock(row) },
          () => h(NIcon, null, { default: () => h(CreateOutline) })),
        h(NButton, { size: 'small', quaternary: true, circle: true, type: 'error', onClick: () => deleteBlock(row) },
          () => h(NIcon, null, { default: () => h(TrashOutline) })),
      ])
    },
  },
])

// ------------------------------------------------------------------ Note Templates

const noteTemplates = ref<NoteTemplateRead[]>([])
const noteTemplatesLoading = ref(false)
const showNoteTemplateModal = ref(false)
const editingNoteTemplate = ref<NoteTemplateRead | null>(null)
const noteSaving = ref(false)
const noteName = ref('')
const noteBody = ref('')

async function loadNoteTemplates() {
  noteTemplatesLoading.value = true
  try {
    noteTemplates.value = await get<NoteTemplateRead[]>('/api/v1/note-templates')
  } catch {
    message.error(t('contentLibrary.loadFailed'))
  } finally {
    noteTemplatesLoading.value = false
  }
}

function openCreateNote() {
  editingNoteTemplate.value = null
  noteName.value = ''
  noteBody.value = ''
  showNoteTemplateModal.value = true
}

function openEditNote(nt: NoteTemplateRead) {
  editingNoteTemplate.value = nt
  noteName.value = nt.name
  noteBody.value = nt.body
  showNoteTemplateModal.value = true
}

async function saveNote() {
  if (!noteName.value.trim() || !noteBody.value.trim()) return
  noteSaving.value = true
  try {
    const body: NoteTemplateWrite = { name: noteName.value.trim(), body: noteBody.value.trim() }
    if (editingNoteTemplate.value) {
      await put(`/api/v1/note-templates/${editingNoteTemplate.value.id}`, body)
      message.success(t('contentLibrary.note.saveSuccess'))
    } else {
      await post('/api/v1/note-templates', body)
      message.success(t('contentLibrary.note.createSuccess'))
    }
    showNoteTemplateModal.value = false
    await loadNoteTemplates()
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.note.saveFailed'))
  } finally {
    noteSaving.value = false
  }
}

function deleteNote(nt: NoteTemplateRead) {
  dialog.warning({
    title: t('contentLibrary.note.delete'),
    content: t('contentLibrary.note.deleteConfirm', { name: nt.name }),
    positiveText: t('contentLibrary.note.delete'),
    negativeText: t('contentLibrary.cancel'),
    onPositiveClick: async () => {
      try {
        await del(`/api/v1/note-templates/${nt.id}`)
        message.success(t('contentLibrary.note.deleteSuccess'))
        await loadNoteTemplates()
      } catch {
        message.error(t('contentLibrary.note.deleteFailed'))
      }
    },
  })
}

const noteColumns = computed(() => [
  { title: t('contentLibrary.note.name'), key: 'name', width: 200 },
  { title: t('contentLibrary.note.body'), key: 'body', ellipsis: { tooltip: true } },
  {
    title: t('contentLibrary.actions'),
    key: 'actions',
    width: 80,
    render(row: NoteTemplateRead) {
      return h(NSpace, { size: 4, justify: 'center', wrapItem: false }, () => [
        h(NButton, { size: 'small', quaternary: true, circle: true, onClick: () => openEditNote(row) },
          () => h(NIcon, null, { default: () => h(CreateOutline) })),
        h(NButton, { size: 'small', quaternary: true, circle: true, type: 'error', onClick: () => deleteNote(row) },
          () => h(NIcon, null, { default: () => h(TrashOutline) })),
      ])
    },
  },
])

// ------------------------------------------------------------------ PDF Materials

const pdfFiles = ref<PdfAppendixFileRead[]>([])
const pdfTotal = ref(0)
const pdfOffset = ref(0)
const pdfPageSize = 20
const pdfSearch = ref('')
const pdfIncludeArchived = ref(false)
const pdfLoading = ref(false)
const pdfLoadError = ref<string | null>(null)
const pdfLimits = ref<PdfAppendixLimitsRead | null>(null)
const pdfLimitsLoading = ref(false)
const pdfLimitsError = ref(false)
const pdfSelectedFile = ref<File | null>(null)
const pdfUploadTitle = ref('')
const pdfUploading = ref(false)
const pdfUploadError = ref<string | null>(null)
const pdfActionBusy = ref(new Set<string>())
const pdfRenameTarget = ref<PdfAppendixFileRead | null>(null)
const pdfRenameTitle = ref('')
const pdfRenameSaving = ref(false)
const pdfPreview = ref(false)
const pdfPreviewFile = ref<PdfAppendixFileRead | null>(null)
let pdfListGeneration = 0
let pdfLimitsGeneration = 0
let pdfMounted = true
let pdfSearchTimer: ReturnType<typeof setTimeout> | undefined

const pdfPage = computed(() => Math.floor(pdfOffset.value / pdfPageSize) + 1)
const pdfPageCount = computed(() => Math.max(1, Math.ceil(pdfTotal.value / pdfPageSize)))
const pdfSearchTerm = computed(() => pdfSearch.value.trim())
const pdfUploadTooLarge = computed(() => Boolean(
  pdfSelectedFile.value && pdfLimits.value && pdfSelectedFile.value.size > pdfLimits.value.max_appendix_file_bytes,
))

function pdfListUrl() {
  const params = new URLSearchParams({
    limit: String(pdfPageSize),
    offset: String(pdfOffset.value),
    include_archived: String(pdfIncludeArchived.value),
  })
  if (pdfSearchTerm.value) params.set('search', pdfSearchTerm.value)
  return `/api/v1/pdf-library?${params.toString()}`
}

async function loadPdfFiles() {
  clearTimeout(pdfSearchTimer)
  pdfSearchTimer = undefined
  const generation = ++pdfListGeneration
  pdfLoading.value = true
  pdfLoadError.value = null
  try {
    const result = await get<PdfAppendixFileListResponse>(pdfListUrl())
    if (!pdfMounted || generation !== pdfListGeneration) return
    const lastValidOffset = result.total === 0
      ? 0
      : Math.floor((result.total - 1) / pdfPageSize) * pdfPageSize
    if (pdfOffset.value > lastValidOffset) {
      pdfOffset.value = lastValidOffset
      await loadPdfFiles()
      return
    }
    pdfFiles.value = result.items
    pdfTotal.value = result.total
  } catch (e: unknown) {
    if (!pdfMounted || generation !== pdfListGeneration) return
    pdfLoadError.value = e instanceof Error ? e.message : t('contentLibrary.pdfMaterials.loadFailed')
  } finally {
    if (pdfMounted && generation === pdfListGeneration) pdfLoading.value = false
  }
}

async function loadPdfLimits() {
  const generation = ++pdfLimitsGeneration
  pdfLimitsLoading.value = true
  pdfLimitsError.value = false
  try {
    const limits = await get<PdfAppendixLimitsRead>('/api/v1/pdf-library/limits')
    if (!pdfMounted || generation !== pdfLimitsGeneration) return
    pdfLimits.value = limits
  } catch {
    if (!pdfMounted || generation !== pdfLimitsGeneration) return
    pdfLimitsError.value = true
  } finally {
    if (pdfMounted && generation === pdfLimitsGeneration) pdfLimitsLoading.value = false
  }
}

function changePdfSearch(value: string) {
  const previousTerm = pdfSearchTerm.value
  pdfSearch.value = value
  if (pdfSearchTerm.value === previousTerm) return

  pdfOffset.value = 0
  if (!pdfSearchTerm.value) {
    void loadPdfFiles()
    return
  }

  clearTimeout(pdfSearchTimer)
  // An in-flight result belongs to the old search, even during the debounce.
  pdfListGeneration += 1
  pdfLoading.value = true
  pdfLoadError.value = null
  pdfSearchTimer = setTimeout(loadPdfFiles, 300)
}

function changePdfArchiveFilter(value: boolean) {
  pdfOffset.value = 0
  pdfIncludeArchived.value = value
  void loadPdfFiles()
}

function setPdfPage(page: number) {
  pdfOffset.value = (page - 1) * pdfPageSize
  void loadPdfFiles()
}

function choosePdfFile({ file }: { file: UploadFileInfo }) {
  pdfSelectedFile.value = file.file ?? null
  pdfUploadError.value = null
}

async function uploadPdfFile() {
  const file = pdfSelectedFile.value
  if (!file || pdfUploading.value || !pdfLimits.value || pdfUploadTooLarge.value) return
  pdfUploading.value = true
  pdfUploadError.value = null
  const form = new FormData()
  form.append('file', file)
  const title = pdfUploadTitle.value.trim()
  if (title) form.append('title', title)
  try {
    await http<PdfAppendixFileRead>('/api/v1/pdf-library', { method: 'POST', body: form })
    pdfSelectedFile.value = null
    pdfUploadTitle.value = ''
    message.success(t('contentLibrary.pdfMaterials.uploadSuccess'))
    await loadPdfFiles()
  } catch (e: unknown) {
    pdfUploadError.value = e instanceof Error ? e.message : t('contentLibrary.pdfMaterials.uploadFailed')
  } finally {
    pdfUploading.value = false
  }
}

function openPdfRename(file: PdfAppendixFileRead) {
  pdfRenameTarget.value = file
  pdfRenameTitle.value = file.title
}

async function savePdfRename() {
  const file = pdfRenameTarget.value
  const title = pdfRenameTitle.value.trim()
  if (!file || !title || pdfRenameSaving.value) return
  pdfRenameSaving.value = true
  try {
    await patch<PdfAppendixFileRead>(`/api/v1/pdf-library/${file.id}`, { title })
    pdfRenameTarget.value = null
    await loadPdfFiles()
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.pdfMaterials.updateFailed'))
  } finally {
    pdfRenameSaving.value = false
  }
}

async function togglePdfArchived(file: PdfAppendixFileRead) {
  if (pdfActionBusy.value.has(file.id)) return
  const busy = new Set(pdfActionBusy.value)
  busy.add(file.id)
  pdfActionBusy.value = busy
  try {
    await patch<PdfAppendixFileRead>(`/api/v1/pdf-library/${file.id}`, { is_archived: !file.is_archived })
    await loadPdfFiles()
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.pdfMaterials.updateFailed'))
  } finally {
    const next = new Set(pdfActionBusy.value)
    next.delete(file.id)
    pdfActionBusy.value = next
  }
}

function previewPdfFile(file: PdfAppendixFileRead) {
  pdfPreviewFile.value = file
  pdfPreview.value = true
}

async function downloadPdfFile(file: PdfAppendixFileRead) {
  if (pdfActionBusy.value.has(file.id)) return
  const busy = new Set(pdfActionBusy.value)
  busy.add(file.id)
  pdfActionBusy.value = busy
  try {
    await downloadBlob(`/api/v1/pdf-files/${file.id}/content?disposition=attachment`, file.original_filename)
  } catch (e: unknown) {
    message.error(e instanceof Error ? e.message : t('contentLibrary.pdfMaterials.downloadFailed'))
  } finally {
    const next = new Set(pdfActionBusy.value)
    next.delete(file.id)
    pdfActionBusy.value = next
  }
}

const pdfColumns = computed(() => [
  { title: t('contentLibrary.pdfMaterials.titleColumn'), key: 'title', ellipsis: { tooltip: true } },
  { title: t('contentLibrary.pdfMaterials.source'), key: 'source', width: 130, render: () => t('contentLibrary.pdfMaterials.companyLibrary') },
  { title: t('contentLibrary.pdfMaterials.filename'), key: 'original_filename', ellipsis: { tooltip: true } },
  { title: t('contentLibrary.pdfMaterials.pages'), key: 'page_count', width: 72, align: 'center' as const },
  { title: t('contentLibrary.pdfMaterials.size'), key: 'size_bytes', width: 100, render: (row: PdfAppendixFileRead) => formatPdfSize(row.size_bytes) },
  {
    title: t('contentLibrary.pdfMaterials.status'), key: 'is_archived', width: 100,
    render: (row: PdfAppendixFileRead) => row.is_archived
      ? h(NTag, { size: 'small', type: 'warning' }, () => t('contentLibrary.pdfMaterials.archived'))
      : h(NTag, { size: 'small', type: 'success' }, () => t('contentLibrary.pdfMaterials.active')),
  },
  {
    title: t('contentLibrary.actions'), key: 'actions', width: 168,
    render: (row: PdfAppendixFileRead) => h(NSpace, { size: 4, wrap: false }, () => [
      h(NButton, { size: 'small', quaternary: true, circle: true, title: t('contentLibrary.pdfMaterials.preview'), onClick: () => previewPdfFile(row) }, () => h(NIcon, null, { default: () => h(EyeOutline) })),
      h(NButton, { size: 'small', quaternary: true, circle: true, title: t('contentLibrary.pdfMaterials.download'), loading: pdfActionBusy.value.has(row.id), onClick: () => downloadPdfFile(row) }, () => h(NIcon, null, { default: () => h(DownloadOutline) })),
      h(NButton, { size: 'small', quaternary: true, circle: true, title: t('contentLibrary.pdfMaterials.rename'), onClick: () => openPdfRename(row) }, () => h(NIcon, null, { default: () => h(CreateOutline) })),
      h(NButton, { size: 'small', quaternary: true, circle: true, title: row.is_archived ? t('contentLibrary.pdfMaterials.restore') : t('contentLibrary.pdfMaterials.archive'), loading: pdfActionBusy.value.has(row.id), onClick: () => togglePdfArchived(row) }, () => h(NIcon, null, { default: () => h(row.is_archived ? RefreshOutline : ArchiveOutline) })),
    ]),
  },
])

function formatPdfSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ------------------------------------------------------------------ VAT rates (for template line form)

const vatRates = ref<VatRateRead[]>([])

async function loadVatRates() {
  try {
    const res = await get<{ items: VatRateRead[] }>('/api/v1/vat-rates')
    vatRates.value = res.items
  } catch {
    // non-fatal
  }
}

const vatRateOptions = computed(() => vatRates.value.map(r => ({
  label: `${r.label} (${r.percent}%)`,
  value: r.id,
})))

const discountTypeOptions = [
  { label: 'None', value: 'NONE' },
  { label: 'Percentage (%)', value: 'PERCENTAGE' },
  { label: 'Fixed amount', value: 'FIXED' },
]

// ------------------------------------------------------------------ mount

onMounted(() => {
  loadTemplates()
  loadBlocks()
  loadNoteTemplates()
  loadVatRates()
  loadPdfFiles()
  loadPdfLimits()
})

onBeforeUnmount(() => {
  clearTimeout(pdfSearchTimer)
  pdfMounted = false
  pdfListGeneration += 1
  pdfLimitsGeneration += 1
})

const appliesToOptions = computed(() => [
  { label: t('contentLibrary.template.appliesToQUOTE'), value: 'QUOTE' },
  { label: t('contentLibrary.template.appliesToINVOICE'), value: 'INVOICE' },
  { label: t('contentLibrary.template.appliesToBOTH'), value: 'BOTH' },
])
</script>

<template>
  <div class="content-library-page">
    <div class="content-library-container">
          <h2>{{ t('contentLibrary.title') }}</h2>

          <n-tabs type="line" animated>

            <!-- Reusable company-private PDF originals -->
            <n-tab-pane name="pdf-materials" :tab="t('contentLibrary.pdfMaterials.tabTitle')">
              <div class="pdf-materials-intro">
                <n-text depth="3">{{ t('contentLibrary.pdfMaterials.archiveHint') }}</n-text>
                <n-alert v-if="pdfLimitsLoading" type="info" :show-icon="false">{{ t('contentLibrary.pdfMaterials.loadingLimits') }}</n-alert>
                <n-alert v-else-if="pdfLimitsError" type="error">
                  <div class="pdf-materials-error">
                    <span>{{ t('contentLibrary.pdfMaterials.limitsFailed') }}</span>
                    <n-button size="small" @click="loadPdfLimits">{{ t('common.retry') }}</n-button>
                  </div>
                </n-alert>
                <n-alert v-else-if="pdfLimits" type="info" :show-icon="false">
                  {{ t('contentLibrary.pdfMaterials.limits', {
                    size: formatPdfSize(pdfLimits.max_appendix_file_bytes),
                    files: pdfLimits.max_quote_appendices,
                    inputSize: formatPdfSize(pdfLimits.max_quote_appendix_input_bytes),
                    outputSize: formatPdfSize(pdfLimits.max_quote_pdf_bytes),
                    pages: pdfLimits.max_quote_pdf_pages,
                  }) }}
                  {{ t('contentLibrary.pdfMaterials.emailLimitHint') }}
                </n-alert>
              </div>

              <n-card size="small" class="pdf-upload-card">
                <div class="pdf-upload-grid">
                  <n-form-item :label="t('contentLibrary.pdfMaterials.file')" required>
                    <div class="pdf-picker">
                      <n-upload :file-list="[]" :show-file-list="false" :default-upload="false" accept="application/pdf,.pdf" :disabled="pdfUploading || !pdfLimits" @change="choosePdfFile">
                        <n-button :disabled="pdfUploading || !pdfLimits">{{ t('contentLibrary.pdfMaterials.chooseFile') }}</n-button>
                      </n-upload>
                      <n-text v-if="pdfSelectedFile" depth="3" class="pdf-selected-file">{{ pdfSelectedFile.name }} · {{ formatPdfSize(pdfSelectedFile.size) }}</n-text>
                    </div>
                  </n-form-item>
                  <n-form-item :label="t('contentLibrary.pdfMaterials.optionalTitle')">
                    <n-input v-model:value="pdfUploadTitle" :disabled="pdfUploading || !pdfLimits" :placeholder="t('contentLibrary.pdfMaterials.titleFromFilename')" />
                  </n-form-item>
                </div>
                <n-alert v-if="pdfUploadTooLarge && pdfLimits" type="warning" class="pdf-upload-error">
                  {{ t('contentLibrary.pdfMaterials.fileTooLarge', { size: formatPdfSize(pdfLimits.max_appendix_file_bytes) }) }}
                </n-alert>
                <n-alert v-if="pdfUploadError" type="error" class="pdf-upload-error">
                  <div class="pdf-materials-error">
                    <span>{{ pdfUploadError }}</span>
                    <n-button size="small" :disabled="!pdfSelectedFile || pdfUploading || !pdfLimits || pdfUploadTooLarge" :loading="pdfUploading" @click="uploadPdfFile">{{ t('common.retry') }}</n-button>
                  </div>
                </n-alert>
                <div class="pdf-upload-actions">
                  <n-button type="primary" :loading="pdfUploading" :disabled="!pdfSelectedFile || !pdfLimits || pdfUploadTooLarge" @click="uploadPdfFile">
                    <template #icon><n-icon><AddOutline /></n-icon></template>
                    {{ t('contentLibrary.pdfMaterials.upload') }}
                  </n-button>
                </div>
              </n-card>

              <div class="pdf-library-toolbar">
                <n-input
                  :value="pdfSearch"
                  clearable
                  :placeholder="t('contentLibrary.pdfMaterials.search')"
                  @update:value="changePdfSearch"
                  @keyup.enter="loadPdfFiles"
                />
                <div class="pdf-archive-filter">
                  <n-switch
                    :value="pdfIncludeArchived"
                    :aria-label="t('contentLibrary.pdfMaterials.showArchived')"
                    @update:value="changePdfArchiveFilter"
                  />
                  <span>{{ t('contentLibrary.pdfMaterials.showArchived') }}</span>
                </div>
                <n-button quaternary :loading="pdfLoading" @click="loadPdfFiles">
                  <template #icon><n-icon><RefreshOutline /></n-icon></template>
                  {{ t('contentLibrary.pdfMaterials.refresh') }}
                </n-button>
              </div>
              <n-alert v-if="pdfLoadError" type="error" class="pdf-library-error">
                <div class="pdf-materials-error">
                  <span>{{ pdfLoadError }}</span>
                  <n-button size="small" :loading="pdfLoading" @click="loadPdfFiles">{{ t('common.retry') }}</n-button>
                </div>
              </n-alert>
              <n-spin :show="pdfLoading">
                <n-data-table
                  :columns="pdfColumns"
                  :data="pdfFiles"
                  :bordered="false"
                  :row-key="(row: PdfAppendixFileRead) => row.id"
                  striped
                />
                <n-text v-if="!pdfLoading && !pdfLoadError && pdfFiles.length === 0" depth="3" class="pdf-library-empty">
                  {{ pdfSearchTerm ? t('contentLibrary.pdfMaterials.noResults') : t('contentLibrary.pdfMaterials.empty') }}
                </n-text>
              </n-spin>
              <div v-if="pdfTotal > pdfPageSize" class="pdf-library-pagination">
                <n-button size="small" :disabled="pdfPage <= 1 || pdfLoading" @click="setPdfPage(pdfPage - 1)">{{ t('contentLibrary.pdfMaterials.previous') }}</n-button>
                <n-text depth="3">{{ t('contentLibrary.pdfMaterials.page', { page: pdfPage, pages: pdfPageCount, total: pdfTotal }) }}</n-text>
                <n-button size="small" :disabled="pdfPage >= pdfPageCount || pdfLoading" @click="setPdfPage(pdfPage + 1)">{{ t('contentLibrary.pdfMaterials.next') }}</n-button>
              </div>
            </n-tab-pane>

            <!-- Document Templates -->
            <n-tab-pane name="templates" :tab="t('contentLibrary.template.tabTitle')">
              <div class="tab-toolbar">
                <n-button type="primary" @click="openCreateTemplate">
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('contentLibrary.template.create') }}
                </n-button>
              </div>
              <n-spin :show="templatesLoading">
                <n-data-table
                  :columns="templateColumns"
                  :data="templates"
                  :bordered="false"
                  :row-key="(row: DocumentTemplateRead) => row.id"
                  striped
                />
                <n-text v-if="!templatesLoading && templates.length === 0" depth="3" style="display: block; text-align: center; padding: 32px">
                  {{ t('contentLibrary.template.empty') }}
                </n-text>
              </n-spin>
            </n-tab-pane>

            <!-- Content Blocks -->
            <n-tab-pane name="blocks" :tab="t('contentLibrary.block.tabTitle')">
              <div class="tab-toolbar">
                <n-button type="primary" @click="openCreateBlock">
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('contentLibrary.block.create') }}
                </n-button>
              </div>
              <n-spin :show="blocksLoading">
                <n-data-table
                  :columns="blockColumns"
                  :data="blocks"
                  :bordered="false"
                  :row-key="(row: ContentBlockRead) => row.id"
                  striped
                />
                <n-text v-if="!blocksLoading && blocks.length === 0" depth="3" style="display: block; text-align: center; padding: 32px">
                  {{ t('contentLibrary.block.empty') }}
                </n-text>
              </n-spin>
            </n-tab-pane>

            <!-- Note Templates -->
            <n-tab-pane name="notes" :tab="t('contentLibrary.note.tabTitle')">
              <div class="tab-toolbar">
                <n-button type="primary" @click="openCreateNote">
                  <template #icon><n-icon><AddOutline /></n-icon></template>
                  {{ t('contentLibrary.note.create') }}
                </n-button>
              </div>
              <n-spin :show="noteTemplatesLoading">
                <n-data-table
                  :columns="noteColumns"
                  :data="noteTemplates"
                  :bordered="false"
                  :row-key="(row: NoteTemplateRead) => row.id"
                  striped
                />
                <n-text v-if="!noteTemplatesLoading && noteTemplates.length === 0" depth="3" style="display: block; text-align: center; padding: 32px">
                  {{ t('contentLibrary.note.empty') }}
                </n-text>
              </n-spin>
            </n-tab-pane>

          </n-tabs>
    </div>

    <!-- Document Template Modal -->
    <n-modal
      v-model:show="showTemplateModal"
      preset="card"
      :title="editingTemplate ? t('contentLibrary.template.edit') : t('contentLibrary.template.create')"
      style="max-width: 720px"
    >
      <n-form label-placement="top">
        <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-form-item :label="t('contentLibrary.template.name')" required>
              <n-input v-model:value="templateName" :placeholder="t('contentLibrary.template.namePlaceholder')" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('contentLibrary.template.appliesTo')">
              <n-select v-model:value="templateAppliesTo" :options="appliesToOptions" />
            </n-form-item>
          </n-gi>
        </n-grid>

        <!-- Template lines -->
        <div style="margin-top: 8px; margin-bottom: 4px; font-weight: 600">{{ t('contentLibrary.template.lines') }}</div>
        <div v-for="(line, i) in templateLines" :key="i" style="border: 1px solid var(--n-border-color); border-radius: 4px; padding: 12px; margin-bottom: 8px">
          <n-grid :cols="12" :x-gap="8" :y-gap="4">
            <n-gi :span="6">
              <n-form-item :label="t('contentLibrary.template.lineName')" size="small" required>
                <n-input v-model:value="line.name" :placeholder="t('contentLibrary.template.lineNamePlaceholder')" />
              </n-form-item>
            </n-gi>
            <n-gi :span="2">
              <n-form-item :label="t('contentLibrary.template.lineQty')" size="small">
                <n-input-number v-model:value="line.quantity" :min="0.001" :precision="3" />
              </n-form-item>
            </n-gi>
            <n-gi :span="2">
              <n-form-item :label="t('contentLibrary.template.lineUnit')" size="small">
                <n-input v-model:value="line.unit_name" clearable @update:value="() => { line.unit_id = null }" />
              </n-form-item>
            </n-gi>
            <n-gi :span="2">
              <n-form-item :label="t('contentLibrary.template.linePrice')" size="small">
                <n-input-number v-model:value="line.unit_price" :min="0" :precision="3" />
              </n-form-item>
            </n-gi>
            <n-gi :span="12">
              <n-form-item :label="t('contentLibrary.template.lineDesc')" size="small">
                <n-input v-model:value="line.description" type="textarea" :autosize="{ minRows: 1, maxRows: 6 }" clearable />
              </n-form-item>
            </n-gi>
            <n-gi :span="3">
              <n-form-item :label="t('contentLibrary.template.lineVatRate')" size="small">
                <n-select
                  v-model:value="line.vat_rate_id"
                  :options="vatRateOptions"
                  clearable
                  size="small"
                />
              </n-form-item>
            </n-gi>
            <n-gi :span="3">
              <n-form-item :label="t('contentLibrary.template.lineDiscountType')" size="small">
                <n-select v-model:value="line.discount_type" :options="discountTypeOptions" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi v-if="line.discount_type !== 'NONE'" :span="3">
              <n-form-item :label="t('contentLibrary.template.lineDiscountValue')" size="small">
                <n-input-number v-model:value="line.discount_value" :min="0" :precision="3" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi :span="line.discount_type !== 'NONE' ? 2 : 5" />
            <n-gi :span="1" style="display: flex; align-items: flex-end; padding-bottom: 2px; justify-content: flex-end">
              <n-button
                v-if="templateLines.length > 1"
                size="small"
                quaternary
                circle
                type="error"
                @click="templateLines.splice(i, 1)"
              >
                <template #icon><n-icon><TrashOutline /></n-icon></template>
              </n-button>
            </n-gi>
          </n-grid>
        </div>
        <n-button dashed block @click="templateLines.push(emptyTemplateLine())">
          <template #icon><n-icon><AddOutline /></n-icon></template>
          {{ t('contentLibrary.template.addLine') }}
        </n-button>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showTemplateModal = false">{{ t('contentLibrary.cancel') }}</n-button>
          <n-button type="primary" :loading="templateSaving" @click="saveTemplate">{{ t('contentLibrary.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Content Block Modal -->
    <n-modal
      v-model:show="showBlockModal"
      preset="card"
      :title="editingBlock ? t('contentLibrary.block.edit') : t('contentLibrary.block.create')"
      style="max-width: 560px"
    >
      <n-form label-placement="top">
        <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-form-item :label="t('contentLibrary.block.name')" required>
              <n-input v-model:value="blockName" :placeholder="t('contentLibrary.block.namePlaceholder')" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('contentLibrary.block.kind')">
              <n-select v-model:value="blockKind" :options="kindOptions" />
            </n-form-item>
          </n-gi>
          <n-gi :span="2">
            <n-form-item :label="t('contentLibrary.block.body')" required>
              <n-input v-model:value="blockBody" type="textarea" :autosize="{ minRows: 3 }" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('contentLibrary.block.isDefault')">
              <n-switch v-model:value="blockIsDefault" />
            </n-form-item>
          </n-gi>
        </n-grid>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showBlockModal = false">{{ t('contentLibrary.cancel') }}</n-button>
          <n-button type="primary" :loading="blockSaving" @click="saveBlock">{{ t('contentLibrary.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Note Template Modal -->
    <n-modal
      v-model:show="showNoteTemplateModal"
      preset="card"
      :title="editingNoteTemplate ? t('contentLibrary.note.edit') : t('contentLibrary.note.create')"
      style="max-width: 480px"
    >
      <n-form label-placement="top">
        <n-form-item :label="t('contentLibrary.note.name')" required>
          <n-input v-model:value="noteName" :placeholder="t('contentLibrary.note.namePlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('contentLibrary.note.body')" required>
          <n-input v-model:value="noteBody" type="textarea" :autosize="{ minRows: 3 }" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showNoteTemplateModal = false">{{ t('contentLibrary.cancel') }}</n-button>
          <n-button type="primary" :loading="noteSaving" @click="saveNote">{{ t('contentLibrary.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal :show="Boolean(pdfRenameTarget)" preset="card" :title="t('contentLibrary.pdfMaterials.rename')" style="max-width: 480px" @update:show="(show: boolean) => { if (!show) pdfRenameTarget = null }">
      <n-form-item :label="t('contentLibrary.pdfMaterials.titleColumn')" required>
        <n-input v-model:value="pdfRenameTitle" :disabled="pdfRenameSaving" @keyup.enter="savePdfRename" />
      </n-form-item>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="pdfRenameSaving" @click="pdfRenameTarget = null">{{ t('contentLibrary.cancel') }}</n-button>
          <n-button type="primary" :loading="pdfRenameSaving" :disabled="!pdfRenameTitle.trim()" @click="savePdfRename">{{ t('contentLibrary.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <PdfPreviewDialog
      v-model:show="pdfPreview"
      :src="pdfPreviewFile ? `/api/v1/pdf-files/${pdfPreviewFile.id}/content?disposition=inline` : null"
      :fallback-filename="pdfPreviewFile?.original_filename ?? 'material.pdf'"
    />
  </div>
</template>

<style scoped>
.content-library-container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}

.content-library-container h2 {
  margin-top: 0;
}

.tab-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.pdf-materials-intro,
.pdf-materials-error {
  display: grid;
  gap: 8px;
}

.pdf-upload-card {
  margin: 16px 0;
}

.pdf-upload-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}

.pdf-picker {
  min-width: 0;
}

.pdf-selected-file {
  overflow-wrap: anywhere;
}

.pdf-upload-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.pdf-selected-file,
.pdf-upload-error,
.pdf-library-error {
  display: block;
  margin-top: 8px;
}

.pdf-library-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 16px 0 12px;
}

.pdf-library-toolbar .n-input {
  max-width: 320px;
}

.pdf-archive-filter {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.pdf-library-empty {
  display: block;
  padding: 32px;
  text-align: center;
}

.pdf-library-pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 12px;
  padding: 16px;
}

@media (max-width: 720px) {
  .pdf-upload-grid {
    grid-template-columns: 1fr;
  }
}
</style>
