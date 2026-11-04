import { createApp } from 'vue'
import { request } from './api'
// Historical UI is opt-in; the default entry is the block-building project.
async function boot() {
  const health = await request<{ project?: string; fixed_tower?: boolean; simulation_default?:boolean }>('/api/health').catch(() => ({ project: 'spark-blocks', fixed_tower: false,simulation_default:false }))
  if (new URLSearchParams(location.search).get('simulation')==='1'||health.simulation_default) {
    const [module] = await Promise.all([import('./TowerPresentation.vue'), import('./blocks.css')])
    createApp(module.default).mount('#app')
  } else if (!health.project || new URLSearchParams(location.search).get('project') === 'freeze') {
    const [module] = await Promise.all([import('./FreezeApp.vue'), import('./console.css')])
    createApp(module.default).mount('#app')
  } else if (health.fixed_tower && new URLSearchParams(location.search).get('layout') !== '1') {
    const [module] = await Promise.all([import('./FixedTowerApp.vue'), import('./blocks.css')])
    createApp(module.default).mount('#app')
  } else {
    const [module] = await Promise.all([import('./App.vue'), import('./blocks.css')])
    createApp(module.default).mount('#app')
  }
}
void boot()
