<script setup lang="ts">
/**
 * Reusable SMTP settings form (no page chrome).
 *
 * Loads from / saves to GET/PUT /settings/smtp and sends a test email via
 * POST /settings/smtp/test.  Used both inside the unified settings panel and
 * inside the onboarding-only /settings/smtp route wrapper.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { get, put, post } from '../../api/http'
import type { components } from '../../api/schema'

type SmtpSettingsRead = components['schemas']['SmtpSettingsRead']

const { t } = useI18n()

const host = ref('')
const port = ref(587)
const username = ref('')
const password = ref('')
const passwordSet = ref(false)
const fromEmail = ref('')
const fromName = ref('')
const useTls = ref(true)
const useSsl = ref(false)
const sentCopy = ref<components['schemas']['SentCopySettings']>({
  enabled: false, host: '', port: 993, security: 'SSL_TLS', folder: null,
})

// STARTTLS and SSL/TLS are mutually exclusive ways to establish TLS, so the UI
// exposes a single 3-way choice (mapped back to the two backend booleans).
// "none" = plaintext (both false).  use_ssl wins if a legacy value has both set.
type SecurityMode = 'starttls' | 'ssl' | 'none'
const securityMode = computed<SecurityMode>({
  get: () => (useSsl.value ? 'ssl' : useTls.value ? 'starttls' : 'none'),
  set: (mode) => {
    useSsl.value = mode === 'ssl'
    useTls.value = mode === 'starttls'
  },
})

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const clearPassword = ref(false)
const message = ref('')
const messageType = ref<'success' | 'error' | 'warning'>('success')

onMounted(async () => {
  loading.value = true
  try {
    const data = await get<SmtpSettingsRead>('/api/v1/settings/smtp')
    host.value = data.host ?? ''
    port.value = data.port ?? 587
    username.value = data.username ?? ''
    passwordSet.value = data.password_set ?? false
    fromEmail.value = data.from_email ?? ''
    fromName.value = data.from_name ?? ''
    useTls.value = data.use_tls ?? true
    useSsl.value = data.use_ssl ?? false
    if (data.sent_copy) sentCopy.value = data.sent_copy
  } catch {
    message.value = t('settings.smtp.loadFailed')
    messageType.value = 'error'
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  saving.value = true
  message.value = ''
  try {
    // password logic:
    // - clearPassword checked → send "" to clear saved password
    // - password field has input → send the new password
    // - password field empty & not clearing → send null to keep existing
    let passwordPayload: string | null = null
    if (clearPassword.value) {
      passwordPayload = ''
    } else if (password.value) {
      passwordPayload = password.value
    }
    const data = await put<SmtpSettingsRead>('/api/v1/settings/smtp', {
      host: host.value,
      port: port.value,
      username: username.value,
      password: passwordPayload,
      from_email: fromEmail.value,
      from_name: fromName.value,
      use_tls: useTls.value,
      use_ssl: useSsl.value,
      sent_copy: sentCopy.value,
    })
    passwordSet.value = data.password_set ?? false
    password.value = ''
    clearPassword.value = false
    message.value = t('settings.smtp.saveSuccess')
    messageType.value = 'success'
  } catch (e: unknown) {
    const err = e as { message?: string }
    message.value = err.message || t('settings.smtp.saveFailed')
    messageType.value = 'error'
  } finally {
    saving.value = false
  }
}

async function handleTestEmail() {
  testing.value = true
  message.value = ''
  try {
    const result = await post<components['schemas']['SmtpTestResult']>('/api/v1/settings/smtp/test', {})
    message.value = t('settings.smtp.testSuccess') + ' ' + t(`sentCopy.${result.sent_copy_status ?? 'NOT_ATTEMPTED'}`)
    messageType.value = result.sent_copy_status === 'FAILED' ? 'warning' : 'success'
  } catch (e: unknown) {
    const err = e as { message?: string }
    message.value = err.message || t('settings.smtp.testFailed')
    messageType.value = 'error'
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <n-spin :show="loading">
    <n-form label-placement="left" label-width="120" @submit.prevent="handleSave">
      <n-form-item :label="t('settings.smtp.host')">
        <n-input v-model:value="host" :placeholder="t('settings.smtp.hostPlaceholder')" />
      </n-form-item>

      <n-form-item :label="t('settings.smtp.port')">
        <n-input-number v-model:value="port" :min="1" :max="65535" style="width: 100%" />
      </n-form-item>

      <n-form-item :label="t('settings.smtp.username')">
        <n-input v-model:value="username" :placeholder="t('settings.smtp.usernamePlaceholder')" />
      </n-form-item>

      <n-form-item :label="t('settings.smtp.password')">
        <n-input
          v-model:value="password"
          type="password"
          show-password-on="click"
          :disabled="clearPassword"
          :placeholder="passwordSet ? t('settings.smtp.passwordPlaceholder') : t('settings.smtp.passwordPlaceholderNew')"
        />
        <template #feedback>
          <n-checkbox v-if="passwordSet" v-model:checked="clearPassword" style="font-size: 12px">
            {{ t('settings.smtp.clearPassword') }}
          </n-checkbox>
        </template>
      </n-form-item>

      <n-form-item :label="t('settings.smtp.fromEmail')">
        <n-input v-model:value="fromEmail" placeholder="noreply@example.com" />
      </n-form-item>

      <n-form-item :label="t('settings.smtp.fromName')">
        <n-input v-model:value="fromName" :placeholder="t('settings.smtp.fromNamePlaceholder')" />
      </n-form-item>

      <n-form-item :label="t('settings.smtp.security')">
        <n-radio-group v-model:value="securityMode">
          <n-space>
            <n-radio value="starttls">{{ t('settings.smtp.useTls') }}</n-radio>
            <n-radio value="ssl">{{ t('settings.smtp.useSsl') }}</n-radio>
            <n-radio value="none">{{ t('settings.smtp.securityNone') }}</n-radio>
          </n-space>
        </n-radio-group>
      </n-form-item>

      <n-form-item :label="t('sentCopy.enabled')">
        <n-switch v-model:value="sentCopy.enabled" :disabled="loading || saving || testing" />
      </n-form-item>
      <n-alert type="info" style="margin-bottom: 16px">{{ t('sentCopy.help') }}
      </n-alert>
      <template v-if="sentCopy.enabled">
        <n-form-item :label="t('sentCopy.host')">
          <n-input v-model:value="sentCopy.host" :disabled="saving || testing" />
        </n-form-item>
        <n-form-item :label="t('sentCopy.port')">
          <n-input-number v-model:value="sentCopy.port" :min="1" :max="65535" :disabled="saving || testing" />
        </n-form-item>
        <n-form-item :label="t('settings.smtp.security')">
          <n-radio-group v-model:value="sentCopy.security" :disabled="saving || testing">
            <n-radio value="SSL_TLS">SSL/TLS</n-radio>
            <n-radio value="STARTTLS">STARTTLS</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('sentCopy.folder')">
          <n-input v-model:value="sentCopy.folder" :placeholder="t('sentCopy.folderHelp')" :disabled="saving || testing" />
        </n-form-item>
      </template>

      <n-alert v-if="message" :type="messageType" style="margin-bottom: 16px">
        {{ message }}
      </n-alert>

      <n-space>
        <n-button type="primary" :loading="saving" :disabled="loading || testing" attr-type="submit">
          {{ t('settings.smtp.save') }}
        </n-button>
        <n-button :loading="testing" :disabled="loading || saving" @click="handleTestEmail">
          {{ t('settings.smtp.testEmail') }}
        </n-button>
      </n-space>
    </n-form>
  </n-spin>
</template>
