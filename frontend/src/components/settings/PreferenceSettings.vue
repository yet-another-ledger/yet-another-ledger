<script setup lang="ts">
/**
 * Preference settings (no page chrome) – non-identity user/company preferences.
 *
 * Ships the interface language, persisted to the account (USER-level setting)
 * via useLocale().setLocale, with localStorage as a fast cache — mirroring the
 * theme preference.  Future defaults (date format, due days, …) land in their
 * own consuming milestones; a placeholder note marks the slot.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useLocale } from '../../composables/useLocale'
import type { LocalePreference } from '../../composables/userPreferences'
import { loaded, setShowBatteryAssets, showBatteryAssets } from '../../composables/userPreferences'

const { t } = useI18n()
const { currentLocale, setLocale } = useLocale()

const languageOptions = [
  { label: 'English', value: 'en' },
  { label: '中文', value: 'zh' },
]

const saving = ref(false)
const message = ref('')
const messageType = ref<'success' | 'error'>('success')

async function updateBatteryVisibility(visible: boolean) {
  saving.value = true
  message.value = ''
  try {
    await setShowBatteryAssets(visible)
    message.value = t('settings.preferences.saveSuccess')
    messageType.value = 'success'
  } catch {
    message.value = t('settings.preferences.saveFailed')
    messageType.value = 'error'
  } finally {
    saving.value = false
  }
}

const selectedLocale = computed({
  get: () => currentLocale.value,
  set: (val: LocalePreference) => {
    saving.value = true
    message.value = ''
    setLocale(val)
      .then(() => {
        message.value = t('settings.preferences.saveSuccess')
        messageType.value = 'success'
        saving.value = false
      })
      .catch(() => {
        message.value = t('settings.preferences.saveFailed')
        messageType.value = 'error'
        saving.value = false
      })
  },
})
</script>

<template>
  <n-form label-placement="left" label-width="100">
    <n-form-item :label="t('settings.preferences.language')">
      <n-select
        v-model:value="selectedLocale"
        :options="languageOptions"
        :disabled="saving"
        style="max-width: 200px"
      />
    </n-form-item>
    <n-text depth="3" style="font-size: 12px">
      {{ t('settings.preferences.languageHint') }}
    </n-text>

    <n-form-item :label="t('settings.preferences.batteryAssets')" label-placement="top" label-width="auto" style="margin-top: 20px">
      <n-switch :value="showBatteryAssets" :disabled="saving || !loaded" @update:value="updateBatteryVisibility" />
    </n-form-item>
    <n-text depth="3" style="font-size: 12px">
      {{ t('settings.preferences.batteryAssetsHint') }}
    </n-text>

    <n-alert v-if="message" :type="messageType" closable style="margin-top: 12px" @close="message = ''">
      {{ message }}
    </n-alert>

    <n-divider />
    <n-text depth="3" style="font-size: 12px">
      {{ t('settings.preferences.moreSoon') }}
    </n-text>
  </n-form>
</template>
