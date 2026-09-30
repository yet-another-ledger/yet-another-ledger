<script setup lang="ts">
/**
 * Unified, Affine-style settings panel — a centered modal overlay with a
 * left category nav and a right detail pane (edit in place).
 *
 * Single entry point: opened by the gear icon via useSettingsPanel().  Hosts
 * preference / SMTP / theme categories; AI is a disabled "coming soon" slot.
 * Business identity (company profile) is intentionally NOT here — it lives
 * under the separate Company icon (/settings/company).
 */
import { computed, h, type Component } from 'vue'
import { NIcon, type MenuOption } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import {
  OptionsOutline,
  MailOutline,
  ColorPaletteOutline,
  BookOutline,
  SparklesOutline,
  DocumentOutline,
  CreateOutline,
  CarOutline,
  KeyOutline,
} from '@vicons/ionicons5'
import { useSettingsPanel, type SettingsCategory } from '../../composables/useSettingsPanel'
import PreferenceSettings from './PreferenceSettings.vue'
import SmtpSettingsForm from './SmtpSettingsForm.vue'
import ThemeSettings from './ThemeSettings.vue'
import DictionarySettings from './DictionarySettings.vue'
import AiSettingsForm from './AiSettingsForm.vue'
import DocumentDefaultsSettings from './DocumentDefaultsSettings.vue'
import EmailTemplatesSettings from './EmailTemplatesSettings.vue'
import MileageSettings from './MileageSettings.vue'
import AgentTokensSettings from './AgentTokensSettings.vue'

const { t } = useI18n()
const { isOpen, activeCategory, close } = useSettingsPanel()

function renderIcon(icon: Component) {
  return () => h(NIcon, null, { default: () => h(icon) })
}

const menuOptions = computed<MenuOption[]>(() => [
  {
    label: t('settings.panel.categoryPreference'),
    key: 'preference',
    icon: renderIcon(OptionsOutline),
  },
  {
    label: t('settings.panel.categorySmtp'),
    key: 'smtp',
    icon: renderIcon(MailOutline),
  },
  {
    label: t('settings.panel.categoryTheme'),
    key: 'theme',
    icon: renderIcon(ColorPaletteOutline),
  },
  {
    label: t('settings.panel.categoryDictionary'),
    key: 'dictionary',
    icon: renderIcon(BookOutline),
  },
  {
    label: t('settings.panel.categoryMileage'),
    key: 'mileage',
    icon: renderIcon(CarOutline),
  },
  {
    label: t('settings.panel.categoryAi'),
    key: 'ai',
    icon: renderIcon(SparklesOutline),
  },
  {
    label: t('settings.panel.categoryDocumentDefaults'),
    key: 'document-defaults',
    icon: renderIcon(DocumentOutline),
  },
  {
    label: t('settings.panel.categoryEmailTemplates'),
    key: 'email-templates',
    icon: renderIcon(CreateOutline),
  },
  {
    label: t('settings.panel.categoryAgentTokens'),
    key: 'agent-tokens',
    icon: renderIcon(KeyOutline),
  },
])

// n-modal expects a writable boolean; close() on dismiss.
const show = computed({
  get: () => isOpen.value,
  set: (v: boolean) => {
    if (!v) close()
  },
})

function onMenuUpdate(key: string) {
  activeCategory.value = key as SettingsCategory
}
</script>

<template>
  <n-modal
    v-model:show="show"
    preset="card"
    :title="t('settings.panel.title')"
    :bordered="false"
    :style="{ width: '760px', maxWidth: '92vw' }"
    class="settings-panel"
  >
    <div class="panel-body">
      <div class="panel-nav">
        <n-menu
          :value="activeCategory"
          :options="menuOptions"
          @update:value="onMenuUpdate"
        />
      </div>
      <div class="panel-detail">
        <PreferenceSettings v-if="activeCategory === 'preference'" />
        <SmtpSettingsForm v-else-if="activeCategory === 'smtp'" />
        <ThemeSettings v-else-if="activeCategory === 'theme'" />
        <DictionarySettings v-else-if="activeCategory === 'dictionary'" />
        <MileageSettings v-else-if="activeCategory === 'mileage'" />
        <AiSettingsForm v-else-if="activeCategory === 'ai'" />
        <DocumentDefaultsSettings v-else-if="activeCategory === 'document-defaults'" />
        <EmailTemplatesSettings v-else-if="activeCategory === 'email-templates'" />
        <AgentTokensSettings v-else-if="activeCategory === 'agent-tokens'" />
      </div>
    </div>
  </n-modal>
</template>

<style scoped>
.panel-body {
  display: flex;
  gap: 16px;
  min-height: 360px;
}

.panel-nav {
  width: 180px;
  flex-shrink: 0;
  border-right: 1px solid var(--n-border-color);
  padding-right: 8px;
}

.panel-detail {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  max-height: 60vh;
  padding-right: 4px;
}
</style>
