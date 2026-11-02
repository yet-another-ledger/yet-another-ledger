<script setup lang="ts">
import { ref, useId } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NCollapse, NCollapseItem, NList, NListItem } from 'naive-ui'

interface SourceItem {
  id: string
  label: string
}

const props = defineProps<{
  title: string
  actionLabel: string
  emptyLabel: string
  items: SourceItem[]
}>()

const router = useRouter()
const expandedNames = ref<string[]>([])
const panelId = useId()

function togglePanel(): void {
  expandedNames.value = expandedNames.value.length ? [] : ['external-sale-sources']
}

function openRecord(id: string): void {
  void router.push(`/external-sales/${encodeURIComponent(id)}`)
}
</script>

<template>
  <section class="report-source-panel">
    <NCollapse v-model:expanded-names="expandedNames" display-directive="show">
      <NCollapseItem name="external-sale-sources">
        <template #header="{ collapsed }">
          <NButton
            :id="`${panelId}-toggle`"
            class="report-source-panel__toggle"
            text
            :aria-expanded="!collapsed"
            :aria-controls="`${panelId}-content`"
            @click.stop="togglePanel"
          >
            {{ title }}
          </NButton>
        </template>
        <div :id="`${panelId}-content`" role="region" :aria-labelledby="`${panelId}-toggle`">
          <NList v-if="items.length" bordered>
            <NListItem v-for="item in items" :key="item.id">
              <div class="report-source-panel__row">
                <span class="report-source-panel__label">{{ item.label }}</span>
                <NButton
                  class="report-source-panel__action"
                  type="primary"
                  text
                  size="small"
                  :aria-label="`${actionLabel}: ${item.label}`"
                  @click="openRecord(item.id)"
                >
                  {{ actionLabel }}
                </NButton>
              </div>
            </NListItem>
          </NList>
          <p v-else class="report-source-panel__empty">{{ emptyLabel }}</p>
        </div>
      </NCollapseItem>
    </NCollapse>
  </section>
</template>

<style scoped>
.report-source-panel {
  margin-bottom: 16px;
  min-width: 0;
}

.report-source-panel__row {
  align-items: flex-start;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  justify-content: space-between;
  min-width: 0;
  width: 100%;
}

.report-source-panel__toggle {
  min-width: 0;
  white-space: normal;
  text-align: left;
  overflow-wrap: anywhere;
}

.report-source-panel__label {
  flex: 1 1 240px;
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
}

.report-source-panel__action {
  flex: 0 0 auto;
}

.report-source-panel__empty {
  color: var(--n-text-color-3, #999);
  margin: 8px 16px 16px;
}
</style>
