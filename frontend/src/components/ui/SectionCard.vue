<script setup lang="ts">
// Adapted from Project16 SectionCard: keep its slot API without UnoCSS coupling.
withDefaults(defineProps<{ title?: string; description?: string; flush?: boolean }>(), { flush: false })
</script>

<template>
  <section class="section-card">
    <header v-if="title || $slots.title || $slots.actions" class="section-card-header">
      <div><h2><slot name="title">{{ title }}</slot></h2><p v-if="description">{{ description }}</p></div>
      <div v-if="$slots.actions" class="section-card-actions"><slot name="actions" /></div>
    </header>
    <div :class="{ 'section-card-body': !flush }"><slot /></div>
  </section>
</template>

<style scoped>
.section-card{border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-elevated);min-width:0}
.section-card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:22px 24px 16px}
h2{font-size:16px;font-weight:600;color:var(--text-strong)}
p{font-size:12px;line-height:1.7;color:var(--text-muted);margin-top:6px}
.section-card-actions{display:flex;gap:8px;align-items:center}.section-card-body{padding:0 24px 24px}
@media(max-width:600px){.section-card-header{padding:18px 16px 14px}.section-card-body{padding:0 16px 16px}}
</style>
