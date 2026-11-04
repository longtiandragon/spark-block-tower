<script setup lang="ts" generic="T extends string | number">
import { useId } from 'vue'

/**
 * 分段切换：近 7 天 / 近 30 天 / 累计 这类互斥的几个选项。
 * 用原生单选框实现：Tab 进入、方向键切换，读屏软件按单选组朗读。block 时占满一行、各段等宽。
 */
withDefaults(
  defineProps<{
    modelValue: T
    options: { label: string; value: T; disabled?: boolean }[]
    /** 这组选项的读屏名称，如「统计区间」 */
    label?: string
    block?: boolean
  }>(),
  { block: false }
)

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
const name = useId()
</script>

<template>
  <div role="radiogroup" :aria-label="label" class="segmented" :class="{ 'is-block': block }">
    <label
      v-for="option in options"
      :key="String(option.value)"
      class="segmented-item"
      :class="{ 'is-active': option.value === modelValue, 'is-disabled': option.disabled }"
    >
      <input
        class="segment-input"
        type="radio"
        :name="name"
        :value="option.value"
        :checked="option.value === modelValue"
        :disabled="option.disabled"
        @change="emit('update:modelValue', option.value)"
      />
      {{ option.label }}
    </label>
  </div>
</template>

<style scoped>
.segmented {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
}

.segmented.is-block {
  display: flex;
  width: 100%;
}

.segmented-item {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  height: 36px;
  padding: 0 14px;
  border-radius: 8px;
  color: var(--text-muted);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.segment-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  opacity: 0;
  cursor: inherit;
}

.is-block .segmented-item {
  flex: 1 1 0;
}

@media (min-width: 768px) {
  .segmented-item {
    height: 32px;
  }
}

.segmented-item:hover {
  color: var(--text-strong);
}

.segmented-item.is-active {
  background: var(--bg-elevated);
  color: var(--text-strong);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(18, 22, 41, 0.08);
}

.segmented-item:has(input:focus-visible) {
  outline: 2px solid var(--brand-500);
  outline-offset: 1px;
}

.segmented-item.is-disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
