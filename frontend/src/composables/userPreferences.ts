/**
 * Server-backed user preferences — single source of truth for theme, locale
 * and optional navigation entries (stored at USER level and served
 * by ``GET/PUT /api/v1/settings/me``).
 *
 * Why a shared module (not two independent composables)
 * -----------------------------------------------------
 * The PUT endpoint *replaces* the whole ``UserPreferences`` object.  If theme
 * and locale were persisted independently with partial bodies, saving one
 * would reset the other to its default.  So both live here and every persist
 * sends all preferences together. Loading is likewise a single GET that
 * populates both (no duplicate request).
 *
 * ``useTheme`` and ``useLocale`` are thin wrappers over this state: the former
 * adds Naive UI theme resolution, the latter adds vue-i18n / ``<html lang>``
 * syncing.  ``localStorage`` is a fast cache; the server is the source of
 * truth for authenticated users.
 */
import { ref } from 'vue'

import { get, put } from '../api/http'
import { i18n } from '../i18n'
import type { components } from '../api/schema'

type UserPreferences = components['schemas']['UserPreferences']

export type ThemePreference = 'light' | 'dark' | 'system'
export type LocalePreference = 'en' | 'zh'

export const SUPPORTED_LOCALES: LocalePreference[] = ['en', 'zh']

const THEME_KEY = 'jai-theme'
const LOCALE_KEY = 'jai-locale'

function readStoredTheme(): ThemePreference {
  const stored = localStorage.getItem(THEME_KEY)
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
}

function readStoredLocale(): LocalePreference | null {
  const stored = localStorage.getItem(LOCALE_KEY)
  return stored === 'en' || stored === 'zh' ? stored : null
}

// Module-level shared state — every useTheme()/useLocale() caller sees these.
export const themePreference = ref<ThemePreference>(readStoredTheme())
export const localePreference = ref<LocalePreference>(readStoredLocale() ?? 'en')
// Account-specific navigation starts hidden until its server preference loads.
export const showBatteryAssets = ref(false)
// ``true`` only after a *successful* load of the current account's server
// values; before that (unauthenticated, mid-load, or a failed load) persisting
// is skipped so a partial local snapshot can't full-replace the server.
export const loaded = ref(false)

// Monotonic auth-context generation.  Bumped on every reset (logout / auth
// change) and at the start of each load, so any async work captured under an
// older generation — an in-flight GET, or a queued PUT — can detect it is
// stale and bail out instead of applying to / writing into a different account.
let authGeneration = 0

function bumpAuthGeneration(): number {
  authGeneration += 1
  return authGeneration
}

/** Apply the theme preference to shared state + localStorage cache. */
export function applyThemePreference(p: ThemePreference): void {
  themePreference.value = p
  localStorage.setItem(THEME_KEY, p)
}

/** Apply the locale to shared state, vue-i18n, ``<html lang>`` and the cache. */
export function applyLocale(lang: LocalePreference): void {
  localePreference.value = lang
  i18n.global.locale.value = lang
  document.documentElement.lang = lang
  localStorage.setItem(LOCALE_KEY, lang)
}

/** Restore the cached locale on boot, before the server load resolves. */
export function initLocaleFromCache(): void {
  const cached = readStoredLocale()
  if (cached) {
    applyLocale(cached)
  }
}

/**
 * Reset persistence state on logout / auth-context change.  Suspends saving
 * (``loaded = false``) *and* bumps the auth generation so any GET/PUT still in
 * flight from the previous account is invalidated — otherwise a leftover
 * snapshot could be applied to or written into the newly signed-in account.
 */
export function resetUserPreferencesLoaded(): void {
  loaded.value = false
  showBatteryAssets.value = false
  bumpAuthGeneration()
}

/** Load the full preferences snapshot after login with one GET. */
export async function loadUserPreferences(): Promise<void> {
  // Open a fresh auth context for this load and suspend persistence until it
  // resolves successfully.
  loaded.value = false
  showBatteryAssets.value = false
  const generation = bumpAuthGeneration()
  try {
    const data = await get<Partial<UserPreferences>>(
      '/api/v1/settings/me',
    )
    // A logout / newer load happened while this GET was in flight — discard the
    // (now stale) response so it can't clobber another account's state.
    if (generation !== authGeneration) {
      return
    }
    if (data?.theme) {
      applyThemePreference(data.theme)
    }
    if (data?.locale) {
      applyLocale(data.locale)
    }
    showBatteryAssets.value = data?.show_battery_assets === true
    // Only now does the module state provably match this account's server
    // state, so the full-replace PUT becomes safe.
    loaded.value = true
  } catch {
    // GET failed: keep ``loaded = false`` so later edits stay cache-only and a
    // partial local snapshot can't full-replace the server (one field would
    // otherwise overwrite the other field's stale cached value).  The next
    // successful load re-enables persistence.
  }
}

// Serialised write chain.  ``/settings/me`` replaces the whole object, so
// overlapping PUTs from independent controls (theme radio, language select,
// header globe) could otherwise let an earlier in-flight request clobber a
// newer value.  Chaining keeps them ordered, and each PUT reads the *latest*
// snapshot at the moment it actually fires — so the final server state always
// matches the last change.
let writeChain: Promise<void> = Promise.resolve()

/** Persist the full preferences object (PUT replaces — always send all fields). */
export function persistUserPreferences(): Promise<void> {
  // Capture the auth context at enqueue time (when the user acted).
  const generation = authGeneration
  writeChain = writeChain
    .catch(() => {
      // A failed write must not break the chain for subsequent saves.
    })
    .then(() => {
      // Drop a stale write: a logout / account switch (generation bump) or a
      // context that is no longer loaded since this save was queued.  Without
      // this, a queued PUT could fire under the next account's session.
      if (generation !== authGeneration || !loaded.value) {
        return
      }
      const preferences: UserPreferences = {
        theme: themePreference.value,
        locale: localePreference.value,
        show_battery_assets: showBatteryAssets.value,
      }
      return put<void>('/api/v1/settings/me', preferences)
    })
  return writeChain
}

/** Change navigation visibility without affecting battery data or calculations. */
export async function setShowBatteryAssets(visible: boolean): Promise<void> {
  if (!loaded.value) return
  const generation = authGeneration
  const previous = showBatteryAssets.value
  showBatteryAssets.value = visible
  try {
    await persistUserPreferences()
  } catch (error) {
    if (generation === authGeneration) showBatteryAssets.value = previous
    throw error
  }
}
