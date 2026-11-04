<script setup lang="ts">
/**
 * 状态标记。
 * - dot：同步状态这类「圆点 + 文字」；
 * - label：绑定方式、「适合新手」这类小标签（浅底 + 深一档的字）。
 * 颜色有含义：success 正常 / 通过，brand 进行中，warning 需要处理，danger 失败，info 信息，neutral 中性；
 * 颜色之外总有文字，不能只靠颜色表达。
 */
withDefaults(
  defineProps<{
    tone?: 'success' | 'brand' | 'warning' | 'danger' | 'info' | 'neutral'
    variant?: 'dot' | 'label'
  }>(),
  { tone: 'neutral', variant: 'dot' }
)
</script>

<template>
  <span v-if="variant === 'dot'" class="ui-status-dot" :class="`tone-${tone}`">
    <span class="dot" aria-hidden="true" />
    <slot />
  </span>
  <span v-else class="status-label" :class="`tone-${tone}`">
    <slot />
  </span>
</template>

<style scoped>
.ui-status-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--tone-ink);
  font-size: 13px;
  line-height: 20px;
  white-space: nowrap;
}

.dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--tone-dot);
}

.status-label {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: var(--radius-xs);
  background: var(--tone-tint);
  color: var(--tone-ink);
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.tone-success {
  --tone-dot: var(--success);
  --tone-ink: var(--success-ink);
  --tone-tint: var(--success-soft);
}

.tone-brand {
  --tone-dot: var(--brand-400);
  --tone-ink: var(--brand-600);
  --tone-tint: var(--brand-soft);
}

.tone-warning {
  --tone-dot: var(--warning);
  --tone-ink: var(--warning-ink);
  --tone-tint: var(--warning-tint);
}

.tone-danger {
  --tone-dot: var(--danger);
  --tone-ink: var(--danger-ink);
  --tone-tint: var(--danger-tint);
}

.tone-info {
  --tone-dot: var(--info);
  --tone-ink: var(--info-ink);
  --tone-tint: var(--info-soft);
}

.tone-neutral {
  --tone-dot: var(--text-faint);
  --tone-ink: var(--text-muted);
  --tone-tint: var(--bg-subtle);
}
</style>
