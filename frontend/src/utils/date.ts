/**
 * Format a Date as a local calendar YYYY-MM-DD string.
 * Do NOT use toISOString() — it converts to UTC first, which shifts the date
 * by 1–2 hours in CET/CEST timezones, causing dates to appear one day early
 * during the early hours of the morning.
 */
export function localDateStr(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/**
 * Convert a plain calendar date to a local-midnight timestamp for date-picker
 * controls.  Parsing `YYYY-MM-DD` with `new Date(value)` treats it as UTC,
 * which can otherwise shift the selected local day.
 */
export function localDateTimestamp(value: string | null | undefined): number | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(year, month - 1, day)
  return Number.isNaN(date.getTime()) ? null : date.getTime()
}

/**
 * Format a date value for display as an ISO calendar date (YYYY-MM-DD).
 *
 * Single, deterministic display format across the whole UI — never
 * `toLocaleDateString()`, which silently follows the browser locale (e.g.
 * en-US renders MM/DD/YYYY). The backend/data layer already speaks YYYY-MM-DD.
 *
 * Accepts a plain date string (`YYYY-MM-DD`, returned verbatim to avoid any
 * timezone parsing), a datetime string, a timestamp, or a Date. Empty/invalid
 * input yields an empty string.
 */
export function formatDate(value: string | number | Date | null | undefined): string {
  if (value == null || value === '') return ''
  // Already a plain calendar date — return as-is (no Date()/timezone round-trip).
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const d = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  return localDateStr(d)
}

/**
 * Format a datetime as `YYYY-MM-DD HH:mm`, optionally including seconds
 * (local time, 24h).
 * Deterministic counterpart to `formatDate` for timestamps that carry a time.
 */
export function formatDateTime(value: string | number | Date | null | undefined, includeSeconds = false): string {
  if (value == null || value === '') return ''
  const d = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const result = `${localDateStr(d)} ${hh}:${mm}`
  return includeSeconds ? `${result}:${String(d.getSeconds()).padStart(2, '0')}` : result
}
