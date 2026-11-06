import { ApiError, get } from '../api/http'

type Translate = (key: string, values?: Record<string, string>) => string

type QuoteAppendixSummary = {
  appendices?: Array<{ file_id: string; title_snapshot: string; title: string }>
}

/** Turn stable Quote composition failures into bilingual, actionable UI copy. */
export async function quotePdfErrorMessage(
  error: unknown,
  quoteId: string,
  t: Translate,
): Promise<string | null> {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== 'object') return null
  const detail = (error.detail as { detail?: unknown }).detail
  if (!detail || typeof detail !== 'object') return null
  const stable = detail as { code?: unknown; file_id?: unknown }
  if (typeof stable.code !== 'string') return null

  const simpleMessages: Record<string, string> = {
    PDF_APPENDIX_INPUT_LIMIT: 'pdf.appendixInputLimit',
    PDF_APPENDIX_COUNT_LIMIT: 'pdf.appendixCountLimit',
    QUOTE_PDF_OUTPUT_LIMIT: 'pdf.appendixOutputLimit',
    QUOTE_PDF_PAGE_LIMIT: 'pdf.appendixPageLimit',
    QUOTE_PDF_COMPOSITION_FAILED: 'pdf.appendixCompositionFailed',
  }
  const simpleKey = simpleMessages[stable.code]
  if (!simpleKey) return null
  if (typeof stable.file_id !== 'string') return t(simpleKey)

  let title = stable.file_id
  try {
    const quote = await get<QuoteAppendixSummary>(`/api/v1/quotes/${quoteId}`)
    const appendix = quote.appendices?.find(item => item.file_id === stable.file_id)
    if (appendix) title = appendix.title_snapshot || appendix.title || stable.file_id
  } catch {
    // Preserve an actionable stable file identifier if the metadata lookup fails.
  }
  return t('pdf.appendixFileCompositionFailed', { title })
}
