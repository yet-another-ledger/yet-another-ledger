<script setup lang="ts">
/**
 * AI settings form — mirrors SmtpSettingsForm.vue.
 *
 * Loads from / saves to GET/PUT /settings/ai.
 * Tests connectivity via POST /settings/ai/test.
 *
 * API-key safety rules:
 * - On load: backend returns api_key_set (bool), never the raw key.
 * - On save: null = keep existing, "" = clear, non-empty = update.
 * - The raw key is never displayed back to the user.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { get, put, post } from '../../api/http'
import type { components } from '../../api/schema'

type AiSettingsRead = components['schemas']['AiSettingsRead']
type AiTestResult = components['schemas']['AiTestResult']

const { t } = useI18n()

const enabled = ref(false)
const apiProtocol = ref<'responses' | 'chat_completions'>('responses')
const baseUrl = ref('https://api.openai.com/v1')
const model = ref('')
const apiKey = ref('')
// Keep a newly entered credential bound to the provider root visible when the
// credential was entered. A Save response can refresh the root while the user
// is typing, but must not silently retarget that draft.
const apiKeyExpectedBaseUrl = ref<string | null>(null)
const apiKeySet = ref(false)
const clearApiKey = ref(false)
const receiptPrompt = ref('')

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const message = ref('')
const messageType = ref<'success' | 'error' | 'info'>('success')
const testResult = ref<AiTestResult | null>(null)
const loadedSettings = ref<AiSettingsRead | null>(null)
const environmentBaseUrl = computed(
  () => loadedSettings.value?.environment_base_url ?? 'https://api.openai.com/v1',
)

function handleBaseUrlInput(value: string) {
  baseUrl.value = value
  // Editing the provider root is an explicit choice to rebind the draft key.
  if (apiKey.value) apiKeyExpectedBaseUrl.value = value || environmentBaseUrl.value
}

function handleApiKeyInput(value: string) {
  if (!value) {
    apiKeyExpectedBaseUrl.value = null
  } else if (!apiKey.value) {
    apiKeyExpectedBaseUrl.value = baseUrl.value || environmentBaseUrl.value
  }
  apiKey.value = value
}

function invalidateKeyDraftForCurrentRoot(): boolean {
  if (!apiKey.value) return false

  const currentRoot = baseUrl.value || environmentBaseUrl.value
  const intendedRoot = apiKeyExpectedBaseUrl.value ?? currentRoot
  if (intendedRoot === currentRoot) return false

  apiKey.value = ''
  apiKeyExpectedBaseUrl.value = null
  return true
}

onMounted(async () => {
  loading.value = true
  try {
    const data = await get<AiSettingsRead>('/api/v1/settings/ai')
    loadedSettings.value = data
    enabled.value = data.enabled ?? false
    apiProtocol.value = data.api_protocol ?? 'responses'
    baseUrl.value = data.base_url ?? 'https://api.openai.com/v1'
    apiKeySet.value = data.api_key_set ?? false
    model.value = data.model ?? ''
    receiptPrompt.value = data.receipt_prompt ?? ''
    if (invalidateKeyDraftForCurrentRoot()) {
      message.value = t('settings.ai.apiKeyRootChanged')
      messageType.value = 'info'
    }
  } catch {
    // first load with no settings is fine
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  saving.value = true
  message.value = ''
  testResult.value = null
  try {
    // api_key semantics: null=keep, ""=clear, non-empty=update (mirrors SMTP password)
    const previous = loadedSettings.value
    const payload: Record<string, unknown> = {
    }
    if (!previous || apiProtocol.value !== previous.api_protocol) payload.api_protocol = apiProtocol.value
    if (clearApiKey.value) payload.api_key = ''
    else if (apiKey.value) {
      payload.api_key = apiKey.value
      payload.expected_base_url = apiKeyExpectedBaseUrl.value ?? (baseUrl.value || environmentBaseUrl.value)
    }
    if (!previous || enabled.value !== previous.enabled) payload.enabled = enabled.value
    if (!previous || baseUrl.value !== previous.base_url) payload.base_url = baseUrl.value
    if (!previous || model.value !== previous.model) payload.model = model.value
    if (!previous || receiptPrompt.value !== previous.receipt_prompt) {
      // An explicit empty prompt clears the stored addition.
      payload.receipt_prompt = receiptPrompt.value
    }
    const submitted = {
      enabled: enabled.value,
      apiProtocol: apiProtocol.value,
      baseUrl: baseUrl.value,
      model: model.value,
      receiptPrompt: receiptPrompt.value,
      apiKey: apiKey.value,
      clearApiKey: clearApiKey.value,
    }
    const data = await put<AiSettingsRead>('/api/v1/settings/ai', payload)
    loadedSettings.value = data
    apiKeySet.value = data.api_key_set ?? false
    if (apiProtocol.value === submitted.apiProtocol) {
      apiProtocol.value = data.api_protocol ?? apiProtocol.value
    }
    if (enabled.value === submitted.enabled) enabled.value = data.enabled ?? enabled.value
    if (baseUrl.value === submitted.baseUrl) baseUrl.value = data.base_url ?? baseUrl.value
    if (model.value === submitted.model) model.value = data.model ?? model.value
    if (receiptPrompt.value === submitted.receiptPrompt) {
      receiptPrompt.value = data.receipt_prompt ?? receiptPrompt.value
    }
    const keyDraftInvalidated = invalidateKeyDraftForCurrentRoot()
    if (apiKey.value === submitted.apiKey && clearApiKey.value === submitted.clearApiKey) {
      apiKey.value = ''
      apiKeyExpectedBaseUrl.value = null
      clearApiKey.value = false
    }
    message.value = keyDraftInvalidated
      ? t('settings.ai.apiKeyRootChanged')
      : t('settings.ai.saveSuccess')
    messageType.value = keyDraftInvalidated ? 'info' : 'success'
  } catch (e: unknown) {
    const err = e as { message?: string }
    message.value = err.message || t('settings.ai.saveFailed')
    messageType.value = 'error'
  } finally {
    saving.value = false
  }
}

async function handleTest() {
  testing.value = true
  message.value = ''
  testResult.value = null
  try {
    // Unchanged fields resolve from the saved/environment configuration, as
    // they will for a real workflow call. Only unsaved edits are overrides.
    const previous = loadedSettings.value
    const overrides: Record<string, unknown> = {}
    if (!previous || enabled.value !== previous.enabled) overrides.enabled = enabled.value
    if (!previous || apiProtocol.value !== previous.api_protocol) {
      overrides.api_protocol = apiProtocol.value
    }
    if (!previous || baseUrl.value !== previous.base_url) overrides.base_url = baseUrl.value
    if (!previous || model.value !== previous.model) overrides.model = model.value
    if (!previous || receiptPrompt.value !== previous.receipt_prompt) {
      overrides.receipt_prompt = receiptPrompt.value
    }
    if (clearApiKey.value) overrides.api_key = ''
    else if (apiKey.value) {
      overrides.api_key = apiKey.value
      overrides.expected_base_url = apiKeyExpectedBaseUrl.value ?? (baseUrl.value || environmentBaseUrl.value)
    }

    const result = await post<AiTestResult>('/api/v1/settings/ai/test', overrides)
    testResult.value = result
    if (result.ok) {
      message.value = result.multimodal
        ? t('settings.ai.testSuccessMultimodal')
        : t('settings.ai.testSuccessNoVision')
      messageType.value = 'success'
    } else {
      message.value = result.detail || t('settings.ai.testFailed')
      messageType.value = 'error'
    }
  } catch (e: unknown) {
    const err = e as { message?: string }
    message.value = err.message || t('settings.ai.testFailed')
    messageType.value = 'error'
  } finally {
    testing.value = false
  }
}

function handleResetPrompt() {
  receiptPrompt.value = ''
  message.value = t('settings.ai.promptReset')
  messageType.value = 'info'
}
</script>

<template>
  <n-spin :show="loading">
    <n-form label-placement="left" label-width="130" @submit.prevent="handleSave">
      <n-form-item :label="t('settings.ai.enabled')">
        <n-switch v-model:value="enabled" />
      </n-form-item>

      <n-form-item :label="t('settings.ai.apiProtocol')">
        <n-select
          v-model:value="apiProtocol"
          :options="[
            { label: t('settings.ai.responses'), value: 'responses' },
            { label: t('settings.ai.chatCompletions'), value: 'chat_completions' },
          ]"
        />
      </n-form-item>

      <n-form-item :label="t('settings.ai.baseUrl')">
        <n-input
          :value="baseUrl"
          @update:value="handleBaseUrlInput"
          :placeholder="environmentBaseUrl || t('settings.ai.baseUrlPlaceholder')"
        />
      </n-form-item>

      <n-form-item :label="t('settings.ai.model')">
        <n-input
          v-model:value="model"
          :placeholder="t('settings.ai.modelPlaceholder')"
        />
      </n-form-item>

      <n-form-item :label="t('settings.ai.apiKey')">
        <n-input
          :value="apiKey"
          @update:value="handleApiKeyInput"
          type="password"
          show-password-on="click"
          :disabled="clearApiKey"
          :placeholder="apiKeySet ? t('settings.ai.apiKeyPlaceholder') : t('settings.ai.apiKeyPlaceholderNew')"
        />
        <template #feedback>
          <n-checkbox v-if="apiKeySet" v-model:checked="clearApiKey" style="font-size: 12px">
            {{ t('settings.ai.clearApiKey') }}
          </n-checkbox>
        </template>
      </n-form-item>

      <n-form-item :label="t('settings.ai.receiptPrompt')">
        <div style="width: 100%">
          <n-input
            v-model:value="receiptPrompt"
            type="textarea"
            :rows="5"
            :placeholder="t('settings.ai.receiptPromptPlaceholder')"
          />
          <n-button
            size="tiny"
            style="margin-top: 4px"
            @click="handleResetPrompt"
          >
            {{ t('settings.ai.promptResetBtn') }}
          </n-button>
        </div>
      </n-form-item>

      <n-alert v-if="message" :type="messageType" style="margin-bottom: 16px">
        {{ message }}
      </n-alert>

      <!-- Test result detail -->
      <n-alert
        v-if="testResult"
        :type="testResult.ok ? 'success' : 'error'"
        style="margin-bottom: 16px"
      >
        <div>
          <div><strong>{{ t('settings.ai.testConnected') }}:</strong> {{ testResult.ok ? t('settings.ai.yes') : t('settings.ai.no') }}</div>
          <div v-if="testResult.ok"><strong>{{ t('settings.ai.testMultimodal') }}:</strong> {{ testResult.multimodal ? t('settings.ai.yes') : t('settings.ai.no') }}</div>
          <div v-if="testResult.detail"><strong>{{ t('settings.ai.testDetail') }}:</strong> {{ testResult.detail }}</div>
        </div>
      </n-alert>

      <n-space>
        <!-- Prod-build safe: separate v-if/v-else for dynamic loading buttons -->
        <n-button v-if="saving" type="primary" loading>{{ t('settings.ai.save') }}</n-button>
        <n-button v-else type="primary" attr-type="submit">{{ t('settings.ai.save') }}</n-button>

        <n-button v-if="testing" loading>{{ t('settings.ai.test') }}</n-button>
        <n-button v-else @click="handleTest">{{ t('settings.ai.test') }}</n-button>
      </n-space>
    </n-form>
  </n-spin>
</template>
