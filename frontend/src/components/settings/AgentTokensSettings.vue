<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { del, get, post } from '../../api/http'

type Scope =
  | 'receipt:intake'
  | 'customer:read'
  | 'customer:create'
  | 'catalog:read'
  | 'quote:read'
  | 'quote:write_draft'
  | 'invoice:read'
  | 'invoice:write_draft'
type TokenRow = {
  id: string
  name: string
  scopes: Scope[]
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}
type TokenList = { items: TokenRow[] }
type TokenCreated = TokenRow & { token: string }

const { t } = useI18n()
const scopes: Scope[] = [
  'receipt:intake', 'customer:read', 'customer:create', 'catalog:read',
  'quote:read', 'quote:write_draft', 'invoice:read', 'invoice:write_draft',
]
const name = ref('')
const selectedScopes = ref<Scope[]>([])
const rows = ref<TokenRow[]>([])
const oneTimeSecret = ref('')
const loading = ref(false)
const saving = ref(false)
const message = ref('')
const error = ref(false)

async function refresh() {
  loading.value = true
  try {
    rows.value = (await get<TokenList>('/api/v1/agent-tokens')).items
  } catch (e) {
    message.value = (e as Error).message || t('settings.agentTokens.loadFailed')
    error.value = true
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

async function createToken() {
  if (!name.value.trim() || selectedScopes.value.length === 0) return
  saving.value = true
  message.value = ''
  try {
    const created = await post<TokenCreated>('/api/v1/agent-tokens', {
      name: name.value.trim(), scopes: selectedScopes.value,
    })
    oneTimeSecret.value = created.token
    name.value = ''
    selectedScopes.value = []
    await refresh()
  } catch (e) {
    message.value = (e as Error).message || t('settings.agentTokens.createFailed')
    error.value = true
  } finally {
    saving.value = false
  }
}

async function copySecret() {
  try {
    const writeText = navigator.clipboard?.writeText
    if (!writeText) throw new Error('Clipboard API unavailable')
    await writeText.call(navigator.clipboard, oneTimeSecret.value)
    oneTimeSecret.value = ''
    message.value = t('settings.agentTokens.copied')
    error.value = false
  } catch {
    message.value = t('settings.agentTokens.copyFailed')
    error.value = true
  }
}

async function revoke(row: TokenRow) {
  try {
    await del(`/api/v1/agent-tokens/${row.id}`)
    await refresh()
  } catch (e) {
    message.value = (e as Error).message || t('settings.agentTokens.revokeFailed')
    error.value = true
  }
}
</script>

<template>
  <section class="agent-token-settings">
    <h3>{{ t('settings.agentTokens.title') }}</h3>
    <p>{{ t('settings.agentTokens.description') }}</p>
    <n-form @submit.prevent="createToken">
      <n-form-item :label="t('settings.agentTokens.name')">
        <n-input v-model:value="name" maxlength="100" />
      </n-form-item>
      <n-form-item :label="t('settings.agentTokens.scopes')">
        <n-checkbox-group v-model:value="selectedScopes">
          <n-space vertical>
            <n-checkbox v-for="scope in scopes" :key="scope" :value="scope" :label="scope" />
          </n-space>
        </n-checkbox-group>
      </n-form-item>
      <n-button type="primary" :loading="saving" :disabled="!name.trim() || !selectedScopes.length" @click="createToken">
        {{ t('settings.agentTokens.create') }}
      </n-button>
    </n-form>

    <n-alert v-if="oneTimeSecret" type="warning" class="secret-alert">
      <p>{{ t('settings.agentTokens.oneTimeWarning') }}</p>
      <n-input :value="oneTimeSecret" readonly type="password" show-password-on="click" />
      <n-button class="copy-button" @click="copySecret">{{ t('settings.agentTokens.copyOnce') }}</n-button>
    </n-alert>
    <n-alert v-if="message" :type="error ? 'error' : 'success'" class="status-message">{{ message }}</n-alert>

    <h4>{{ t('settings.agentTokens.existing') }}</h4>
    <n-spin :show="loading">
      <n-empty v-if="!loading && rows.length === 0" :description="t('settings.agentTokens.empty')" />
      <n-space v-else vertical>
        <n-card v-for="row in rows" :key="row.id" size="small">
          <div class="token-row">
            <div>
              <strong>{{ row.name }}</strong>
              <div>{{ row.scopes.join(', ') }}</div>
              <small>{{ row.revoked_at ? t('settings.agentTokens.revoked') : t('settings.agentTokens.active') }}</small>
            </div>
            <n-button v-if="!row.revoked_at" type="error" secondary @click="revoke(row)">
              {{ t('settings.agentTokens.revoke') }}
            </n-button>
          </div>
        </n-card>
      </n-space>
    </n-spin>
  </section>
</template>

<style scoped>
.agent-token-settings h3, .agent-token-settings h4 { margin: 0 0 12px; }
.agent-token-settings p { margin: 0 0 16px; }
.secret-alert, .status-message { margin-top: 16px; }
.copy-button { margin-top: 12px; }
.token-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.token-row small { color: var(--n-text-color-3); }
</style>
