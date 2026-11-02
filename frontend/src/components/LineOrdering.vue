<script setup lang="ts" generic="T extends { _key: string }">
import Draggable from 'vuedraggable'
import { useI18n } from 'vue-i18n'
const props = defineProps<{ modelValue: T[]; disabled: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [rows: T[]] }>()
const { t } = useI18n()
function update(rows: T[]) {
  if (!props.disabled) emit('update:modelValue', rows)
}
</script>

<template>
  <Draggable :model-value="modelValue" item-key="_key" handle=".line-drag-handle"
    :disabled="disabled" :animation="150" @update:model-value="update">
    <template #item="{ element, index }">
      <div class="ordered-line" :data-line-key="element._key">
        <button v-if="!disabled" type="button" class="line-drag-handle"
          :aria-label="t('common.reorderLine')" :title="t('common.reorderLine')">⠿</button>
        <slot :line="element" :index="index" />
      </div>
    </template>
  </Draggable>
</template>

<style scoped>
.ordered-line { position: relative; padding-left: 28px; }
.line-drag-handle { position: absolute; left: 0; top: 12px; cursor: grab;
  touch-action: none; border: 0; background: transparent; color: inherit; font-size: 22px; }
.line-drag-handle:active { cursor: grabbing; }
</style>
