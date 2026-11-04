<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Bot, Box, LayoutDashboard, ScanLine, Activity, ArrowDownToLine, ArrowUpRight, Camera, Check, ChevronRight, CircleHelp, Flag, Footprints, Hand, LoaderCircle, Maximize2, Pause, Play, Radio, RotateCcw, Settings2, ShieldCheck, Square, Volume2, VolumeX, WifiOff, X } from 'lucide-vue-next'
import { request } from './api'
import ControlRoom from './components/ControlRoom.vue'
import StatusBadge from './components/ui/StatusBadge.vue'
import SegmentedControl from './components/ui/SegmentedControl.vue'
import type { Evidence, GameConfig, GameState, Mode, Phase, Player } from './types'

const state = ref<GameState | null>(null)
const connected = ref(false)
const connectionError = ref('')
const heartbeatError = ref('')
const everConnected = ref(false)
const lastSync = ref<Date | null>(null)
const busy = ref('')
const reviewBusy = ref('')
const exporting = ref(false)
const settingsOpen = ref(false)
const helpOpen = ref(false)
const voiceEnabled = ref(false)
const frameError = ref(false)
const frameLoaded = ref(false)
const frameSrc = ref('')
const feedElement = ref<HTMLElement | null>(null)
const dialogElement = ref<HTMLElement | null>(null)
function readStoredToken() {
  try { return sessionStorage.getItem('spark-freeze-control-token') ?? '' }
  catch { return '' }
}
const token = ref(readStoredToken())
const toast = ref<{ text: string; error: boolean } | null>(null)
const selectedDemoPlayer = ref(1)
const evidenceFilter = ref<'pending' | 'all'>('pending')
const winnerCandidate = ref<Player | null>(null)
const configMode = ref<Mode>('demo')
const selectedEvidence = ref('')
const workspaces = [
  { id: 'live', title: '比赛现场', icon: LayoutDashboard, code: 'LIVE ARENA', description: '看清每个动作，掌控比赛节奏。' },
  { id: 'twin', title: '数字孪生', icon: Box, code: 'DIGITAL TWIN', description: '从赛场状态，到每一声裁判口令。' },
  { id: 'review', title: '证据复核', icon: ScanLine, code: 'EVIDENCE REVIEW', description: '前后画面对照，让每一次判罚有据可查。' },
  { id: 'assistant', title: '裁判助手', icon: Bot, code: 'REFEREE ASSISTANT', description: '结合本场事实，理解规则与判罚。' },
] as const
type Workspace = typeof workspaces[number]['id']
function readWorkspace(): Workspace {
  const value = location.hash.slice(1)
  return workspaces.some(item => item.id === value) ? value as Workspace : 'live'
}
const workspace = ref<Workspace>(readWorkspace())
const currentWorkspace = computed(() => workspaces.find(item => item.id === workspace.value)!)
function syncWorkspace() { workspace.value = readWorkspace() }
function focusWorkspace() { document.getElementById('workspace-main')?.focus() }
function navigate(value: Workspace) {
  workspace.value = value
  if (location.hash !== '#' + value) location.hash = value
  window.scrollTo({ top: 0, behavior: 'instant' })
}

const form = reactive<GameConfig>({ players: 3, green_min: 3, green_max: 6, red_seconds: 5, settle_seconds: 1.2, game_seconds: 120, motion_threshold: 0.045, hold_seconds: 0.45, auto_eliminate: false, camera_index: 0, speech_target: 'off' })
let pollTimer = 0
let frameTimer = 0
let frameTimeout = 0
let heartbeatTimer = 0
let toastTimer = 0
let alive = true
let epoch = 0
let writes = 0
let previousFocus: HTMLElement | null = null
let settingsSessionId: string | undefined

const phase = computed<Phase>(() => state.value?.phase ?? 'lobby')
const online = computed(() => connected.value && state.value !== null)
// Normal writes are mutually exclusive. Emergency stop may interrupt one,
// and epoch prevents that older response from restoring pre-stop UI state.
const mutating = computed(() => !!busy.value || !!reviewBusy.value)
const configurable = computed(() => online.value && ['lobby', 'finished'].includes(phase.value))
const running = computed(() => ['countdown', 'turning_away', 'green', 'turning_back', 'settling', 'red'].includes(phase.value))
const canStart = computed(() => online.value && phase.value === 'lobby' && state.value?.camera.ready && (state.value.mode !== 'ros' || state.value.robot.armed))
const canAward = computed(() => online.value && (running.value || phase.value === 'paused'))
const modeName = computed(() => ({ demo: '模拟演示', webcam: '电脑摄像头', ros: 'SPARK 机器人' }[state.value?.mode ?? 'demo']))
const phaseTheme = computed(() => !state.value ? 'idle' : ['red', 'turning_back', 'settling', 'estop'].includes(phase.value) ? 'red' : phase.value === 'green' ? 'green' : phase.value === 'finished' ? 'finished' : 'idle')
const phaseTitle = computed(() => !state.value ? '等待连接' : ({ lobby: '准备好了吗？', countdown: '准备开场', turning_away: '裁判转身', green: '向前走！', turning_back: '木头人！', settling: '稳住，别动', red: '保持静止', paused: '中场暂停', finished: '本局结束', estop: '已停止运行' }[phase.value]))
const phaseHint = computed(() => !state.value ? '正在连接本地裁判服务' : ({ lobby: '站好赛道，等待裁判发令', countdown: '各就各位，游戏即将开始', turning_away: '等待裁判背过身去', green: '绿灯时间，可以自由移动', turning_back: '裁判正在回头，请停止移动', settling: '等待画面稳定，暂不判罚', red: '视觉裁判正在观察动作变化', paused: '恢复后先稳定画面，再继续判断', finished: '查看本局证据，导出比赛记录', estop: '需要主动重置才能开始下一局' }[phase.value]))
const phaseClock = computed(() => state.value ? Math.max(0, state.value.remaining).toFixed(1) : '—')
const gameClock = computed(() => {
  if (!state.value) return '--:--'
  const seconds = Math.max(0, Math.ceil(state.value.game_remaining))
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0')
})
const activeCount = computed(() => state.value?.players.filter(p => p.status === 'active').length ?? 0)
const pendingEvidence = computed(() => state.value?.evidence.filter(e => e.status === 'pending') ?? [])
const evidenceItems = computed(() => [...(state.value?.evidence ?? [])].reverse().filter(e => evidenceFilter.value === 'all' || e.status === 'pending'))
const events = computed(() => [...(state.value?.events ?? [])].reverse().slice(0, 40))
const activePlayers = computed(() => state.value?.players.filter(p => p.status === 'active') ?? [])
const cameraFresh = computed(() => state.value?.camera.ready && (state.value.camera.last_frame_age === null || state.value.camera.last_frame_age < 2))
const cameraStatus = computed(() => !online.value ? '等待后台' : frameError.value ? '画面传输中断' : state.value?.camera.error ? '输入异常' : !state.value?.camera.ready ? '等待画面' : !cameraFresh.value ? '画面已过期' : state.value?.camera.stable === false ? '画面不稳 · 暂不判罚' : '画面就绪')
const canSimulate = computed(() => online.value && state.value?.mode === 'demo' && phase.value === 'red' && activePlayers.value.length > 0 && !mutating.value)

function notify(text: string, error = false) {
  toast.value = { text, error }
  clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = null }, error ? 6500 : 3500)
}
function errorMessage(error: unknown) { return error instanceof Error ? error.message : '操作失败，请稍后重试。' }
function applyState(data: GameState) {
  if (state.value?.session_id === data.session_id && state.value.revision > data.revision) return
  if (settingsOpen.value && settingsSessionId !== data.session_id && busy.value !== 'config') {
    settingsOpen.value = false
    notify('场次已被其他操作切换，请重新打开设置，避免覆盖新场次参数。', true)
  }
  if (winnerCandidate.value && (state.value?.session_id !== data.session_id
    || !data.players.some(p => p.id === winnerCandidate.value?.id && p.status === 'active')
    || !['countdown', 'turning_away', 'green', 'turning_back', 'settling', 'red', 'paused'].includes(data.phase))) {
    winnerCandidate.value = null
  }
  state.value = data
  connected.value = true
  everConnected.value = true
  lastSync.value = new Date()
  connectionError.value = ''
  if (!data.players.some(p => p.id === selectedDemoPlayer.value && p.status === 'active')) {
    selectedDemoPlayer.value = data.players.find(p => p.status === 'active')?.id ?? 1
  }
}
async function pollState() {
  const startedEpoch = epoch
  const startedDuringWrite = writes > 0
  try {
    const data = await request<GameState>('/api/state', { timeout: 2000 })
    if (!alive) return
    if (epoch === startedEpoch && !startedDuringWrite && writes === 0) applyState(data)
  } catch (error) {
    if (!alive) return
    connected.value = false
    connectionError.value = errorMessage(error)
    window.speechSynthesis?.cancel()
  } finally {
    if (alive) pollTimer = window.setTimeout(pollState, 250)
  }
}
async function heartbeat() {
  if (online.value) {
    try {
      await request('/api/operator/heartbeat', { body: {}, token: token.value, timeout: 2000 })
      heartbeatError.value = ''
    } catch (error) { heartbeatError.value = errorMessage(error) }
  }
  if (alive) heartbeatTimer = window.setTimeout(heartbeat, 1000)
}
function loadFrame() {
  if (!alive) return
  clearTimeout(frameTimeout)
  frameSrc.value = '/api/frame.jpg?t=' + Date.now()
  // An img request can hang without firing either load or error. Do not keep
  // showing the last picture as live indefinitely while state polling works.
  frameTimeout = window.setTimeout(frameFailed, 2500)
}
function frameReady() {
  if (!alive) return
  clearTimeout(frameTimeout)
  frameLoaded.value = true
  frameError.value = false
  clearTimeout(frameTimer)
  frameTimer = window.setTimeout(loadFrame, 125)
}
function frameFailed() {
  if (!alive) return
  clearTimeout(frameTimeout)
  frameError.value = true
  clearTimeout(frameTimer)
  frameTimer = window.setTimeout(loadFrame, 1000)
}
async function control(action: string, playerId?: number) {
  if (!online.value || (mutating.value && action !== 'estop')) return
  if (action === 'start' && voiceEnabled.value) window.speechSynthesis?.resume()
  const thisEpoch = ++epoch
  writes++
  busy.value = action
  try {
    const data = await request<GameState>('/api/control', { body: {
      action, ...(playerId === undefined ? {} : { player_id: playerId }),
      ...(action === 'estop' ? {} : { expected_session_id: state.value?.session_id }),
      ...(['clear_estop', 'resume', 'declare_winner'].includes(action) ? { expected_revision: state.value?.revision } : {}),
    }, token: token.value })
    if (thisEpoch === epoch) {
      applyState(data)
      if (action === 'demo_move') notify('已注入模拟动作，等待裁判按持续时间判断。')
      if (action === 'reset' || action === 'clear_estop') notify('已重置本局，可以重新开场。')
    }
  } catch (error) { if (thisEpoch === epoch) notify(errorMessage(error), true) }
  finally { writes--; if (thisEpoch === epoch) busy.value = '' }
}
async function review(item: Evidence, decision: 'confirm' | 'dismiss') {
  if (!online.value || mutating.value || item.saving || item.status !== 'pending') return
  const thisEpoch = ++epoch
  writes++
  reviewBusy.value = item.id
  try {
    const data = await request<GameState>('/api/evidence/' + encodeURIComponent(item.id) + '/review', { body: { decision, expected_session_id: state.value?.session_id }, token: token.value })
    if (thisEpoch === epoch) {
      applyState(data)
      notify(decision === 'confirm' ? '已确认本次偷动判罚。' : '已撤销本次判罚。')
    }
  } catch (error) { if (thisEpoch === epoch) notify(errorMessage(error), true) }
  finally { writes--; reviewBusy.value = '' }
}
async function setRobotArmed(armed: boolean) {
  if (!online.value || mutating.value) return
  const thisEpoch = ++epoch
  writes++
  busy.value = 'robot'
  try {
    const data = await request<GameState>('/api/robot/arm', { body: {
      armed, expected_session_id: state.value?.session_id, expected_revision: state.value?.revision,
    }, token: token.value })
    if (thisEpoch === epoch) {
      applyState(data)
      notify(armed ? '机器人已启用，开始比赛后才转向。' : '机器人已停用。')
    }
  } catch (error) { if (thisEpoch === epoch) notify(errorMessage(error), true) }
  finally { writes--; if (thisEpoch === epoch) busy.value = '' }
}
function openSettings() {
  if (state.value) { Object.assign(form, state.value.config); configMode.value = state.value.mode }
  settingsSessionId = state.value?.session_id
  previousFocus = document.activeElement as HTMLElement
  settingsOpen.value = true
}
async function saveSettings() {
  if (!configurable.value || mutating.value) return
  if (form.green_min > form.green_max) { notify('绿灯最短时长不能大于最长时长。', true); return }
  const thisEpoch = ++epoch
  writes++
  busy.value = 'config'
  try {
    const data = await request<GameState>('/api/config', { body: { ...form, mode: configMode.value, expected_session_id: settingsSessionId }, token: token.value, timeout: 12000 })
    if (thisEpoch === epoch) {
      applyState(data)
      settingsOpen.value = false
      notify('设置已保存。' + (configMode.value === 'webcam' ? '正在等待摄像头与姿态模型就绪。' : ''))
    }
  } catch (error) { if (thisEpoch === epoch) notify(errorMessage(error), true) }
  finally { writes--; if (thisEpoch === epoch) busy.value = '' }
}
async function exportReport() {
  if (!online.value || exporting.value) return
  exporting.value = true
  try {
    const report = await request<GameState>('/api/report', { token: token.value })
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'spark-freeze-' + report.session_id.slice(0, 12) + '.json'
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    notify('本局记录已导出。')
  } catch (error) { notify(errorMessage(error), true) }
  finally { exporting.value = false }
}
function speak(text: string) {
  if (state.value?.config.speech_target !== 'off' || !voiceEnabled.value || !('speechSynthesis' in window)) return
  window.speechSynthesis.cancel()
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'zh-CN'
  utterance.rate = 1.05
  window.speechSynthesis.speak(utterance)
}
function toggleVoice() {
  if (state.value?.config.speech_target !== 'off') { notify('同步口令已启用，请在赛场数字孪生面板测试播放。'); return }
  if (!('speechSynthesis' in window)) { notify('当前浏览器不支持语音播报。', true); return }
  voiceEnabled.value = !voiceEnabled.value
  if (voiceEnabled.value) speak('语音播报已开启')
  else window.speechSynthesis.cancel()
}
function statusLabel(status: Player['status']) { return ({ active: '正在比赛', pending: '等待复核', eliminated: '已淘汰', winner: '已到终点' })[status] }
function motionWidth(player: Player) { return Math.min(100, Math.max(0, player.motion / (state.value?.config.motion_threshold || 0.045) * 100)) + '%' }
function formatNumber(value: number | undefined, digits = 3) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—' }
function imageUrl(url: string | null) { return url?.startsWith('/api/') ? url : '' }
async function fullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else await feedElement.value?.requestFullscreen()
  } catch { notify('浏览器暂不支持此视窗全屏。', true) }
}
function closeDialogs() { settingsOpen.value = false; helpOpen.value = false; winnerCandidate.value = null }
function handleKey(event: KeyboardEvent) {
  if (event.key === 'Escape') closeDialogs()
  if (event.key === 'Tab' && settingsOpen.value && dialogElement.value) {
    const focusable = [...dialogElement.value.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]')]
    const first = focusable[0], last = focusable[focusable.length - 1]
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogElement.value)) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
}
watch(token, value => {
  try { if (value) sessionStorage.setItem('spark-freeze-control-token', value); else sessionStorage.removeItem('spark-freeze-control-token') }
  catch { notify('浏览器无法保存会话令牌；本次打开期间仍可使用。', true) }
})
watch(settingsOpen, async open => {
  if (open) { await nextTick(); dialogElement.value?.focus() }
  else previousFocus?.focus()
})
watch(() => state.value?.config.speech_target, target => { if(target !== 'off') { voiceEnabled.value=false;window.speechSynthesis?.cancel() } })
function explainEvidence(id: string) { selectedEvidence.value=id;navigate('assistant') }
watch(() => state.value ? state.value.session_id + ':' + state.value.round + ':' + state.value.phase : '', (value, oldValue) => {
  if (!value || !oldValue || !online.value) return
  const phrases: Partial<Record<Phase, string>> = { countdown: '准备开始', green: '一，二，三', turning_back: '木头人', red: '保持静止', paused: '游戏已暂停', finished: '本局结束', estop: '已停止' }
  const phrase = phrases[phase.value]
  if (phrase) speak(phrase)
})
onMounted(() => { pollState(); heartbeat(); loadFrame(); document.addEventListener('keydown', handleKey);window.addEventListener('hashchange', syncWorkspace) })
onBeforeUnmount(() => {
  alive = false
  ;[pollTimer, frameTimer, frameTimeout, heartbeatTimer, toastTimer].forEach(clearTimeout)
  document.removeEventListener('keydown', handleKey)
  window.removeEventListener('hashchange', syncWorkspace)
  window.speechSynthesis?.cancel()
})
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#workspace-main" @click.prevent="focusWorkspace">跳到主要内容</a>
    <aside class="app-sidebar">
      <a class="brand" href="#live" @click.prevent="navigate('live')" aria-label="SPARK FREEZE 首页"><span class="brand-mark"><Footprints :size="24" /></span><span>SPARK<span class="brand-caption">FREEZE / 视觉裁判</span></span></a>
      <div class="sidebar-label">裁判工作台</div>
      <nav class="workspace-nav" aria-label="工作区导航">
        <a v-for="item in workspaces" :key="item.id" :href="'#' + item.id" :aria-label="item.title" :aria-current="workspace === item.id ? 'page' : undefined" :class="{ active: workspace === item.id }" @click.prevent="navigate(item.id)"><component :is="item.icon" :size="19" /><span>{{ item.title }}</span><span v-if="item.id === 'review' && pendingEvidence.length" class="nav-count">{{ pendingEvidence.length }}</span></a>
      </nav>
      <div class="sidebar-session"><span class="sidebar-label">当前场次</span><strong>{{ state?.session_id.slice(0, 8).toUpperCase() ?? '等待连接' }}</strong><span>{{ state ? modeName : '等待服务' }}</span><div class="sidebar-session-line"><span class="status-dot"></span>{{ state?.phase_label ?? '等待连接' }}</div></div>
      <div class="sidebar-bottom"><span class="sidebar-orbit"><Radio :size="20" /></span><div><strong>SPARK / ROS</strong><small>{{ state?.mode === 'ros' && state?.robot.connected ? '机器人已连接' : '电脑工作站' }}</small></div><span class="version-tag">01</span></div>
    </aside>
    <div class="workspace-shell">
    <header class="topbar">
      <div class="breadcrumb"><span>SPARK FREEZE</span><ChevronRight :size="14" /><strong>{{ currentWorkspace.title }}</strong></div>
      <div class="topbar-right">
        <StatusBadge :tone="connected ? 'success' : 'danger'">{{ connected ? '裁判台已连接' : everConnected ? '连接已中断' : '连接中' }}</StatusBadge>
        <button class="icon-button help-button" @click="helpOpen = true" aria-label="玩法与判罚说明"><CircleHelp :size="19" /></button>
        <button class="button subtle settings-trigger" aria-label="比赛设置" @click="openSettings"><Settings2 :size="16" /><span>比赛设置</span></button>
      </div>
    </header>
    <main id="workspace-main" tabindex="-1">
      <section class="page-heading">
        <div><div class="eyebrow"><span></span>{{ currentWorkspace.code }} <span class="heading-slash">/</span> SPARK FREEZE</div><h1>{{ currentWorkspace.title }}<span class="heading-dot">.</span></h1><p>{{ currentWorkspace.description }}</p></div>
        <div class="session-marker"><span>本局编号</span><strong>{{ state?.session_id.slice(0, 8).toUpperCase() ?? '— — — —' }}</strong><StatusBadge variant="label" :tone="state?.mode === 'demo' ? 'neutral' : 'info'">{{ state ? modeName : '等待服务' }}</StatusBadge></div>
      </section>
      <div v-if="workspace !== 'live'" class="match-toolbar">
        <span class="toolbar-phase"><span class="status-dot" :class="phaseTheme"></span>{{ state?.phase_label ?? '等待连接' }}</span><span class="toolbar-clock">{{ gameClock }}</span><span class="toolbar-round">第 {{ state?.round ?? 0 }} 回合</span>
        <div class="toolbar-actions"><button v-if="running" class="button subtle" :disabled="!online || mutating" @click="control('pause')"><Pause :size="15" />暂停比赛</button><button v-else-if="phase === 'paused'" class="button subtle" :disabled="!online || mutating" @click="control('resume')"><Play :size="15" />恢复比赛</button><button class="button subtle back-live" @click="navigate('live')">返回现场</button><button class="button emergency-button" :disabled="!online" @click="control('estop')"><Square :size="14" fill="currentColor" />停止</button></div>
      </div>
      <div v-if="!connected || state?.camera.error || state?.storage?.error || heartbeatError" class="system-alert" :class="{ error: !connected }" role="status">
        <WifiOff v-if="!connected" :size="19" /><Camera v-else :size="19" />
        <div><strong>{{ !connected ? '裁判服务暂未连接' : state?.camera.error ? '画面输入需要处理' : state?.storage?.error ? '比赛记录保存失败' : '操作心跳异常' }}</strong><span>{{ !connected ? (connectionError || '正在连接本机后台，页面会自动重试。') : state?.camera.error || state?.storage?.error || heartbeatError }}</span><small v-if="!connected">服务地址 127.0.0.1:8765 · 控制按钮已停用{{ everConnected ? ' · 当前保留的是最后一次状态' : '' }}</small></div>
        <button v-if="connected" class="text-button" @click="openSettings">查看设置 <ChevronRight :size="16" /></button>
      </div>

      <div v-show="workspace === 'live'" class="live-workspace">
      <div class="arena-grid">
        <section class="camera-panel panel">
          <div class="panel-header"><div class="section-label"><span class="section-number">01</span><h2>裁判视野</h2></div><div class="feed-kind" :class="{ synthetic: state?.mode === 'demo' }"><span class="status-dot"></span>{{ !state ? '等待输入' : state.mode === 'demo' ? 'DEMO · 合成演示画面' : state.mode === 'webcam' ? 'LIVE · 电脑摄像头' : 'ROS · 机器人画面' }}</div></div>
          <div ref="feedElement" class="camera-viewport" :class="{ unavailable: !connected || frameError }">
            <img :src="frameSrc || undefined" class="camera-image" alt="后台提供的实时画面与检测标注" @load="frameReady" @error="frameFailed" />
            <div v-if="!frameLoaded || frameError" class="feed-placeholder"><Camera :size="38" :stroke-width="1.2" /><strong>{{ frameError ? '暂时没有收到画面' : '正在等待画面' }}</strong><span>后台会在输入就绪后自动恢复</span></div>
            <div class="viewfinder corner-tl"></div><div class="viewfinder corner-tr"></div><div class="viewfinder corner-bl"></div><div class="viewfinder corner-br"></div>
            <div class="video-topline"><span class="camera-id">CAM / 01</span><span class="video-state" :class="phaseTheme">{{ state?.phase_label ?? '等待连接' }}</span></div>
            <div class="video-bottomline"><span><span class="live-square" :class="{ stale: !connected || !cameraFresh || frameError }"></span>{{ cameraStatus }}</span><button class="fullscreen-button" @click="fullscreen" aria-label="全屏查看裁判画面"><Maximize2 :size="17" /></button></div>
            <div v-if="!connected && frameLoaded" class="frozen-feed-label"><WifiOff :size="16" />连接中断 · 此画面未更新</div>
          </div>
          <div class="camera-metadata"><span><Camera :size="14" />{{ state?.camera.model || '模型待就绪' }}</span><span class="numeric">{{ formatNumber(state?.camera.fps, 1) }} FPS</span><span class="numeric">{{ state?.camera.width || '—' }} × {{ state?.camera.height || '—' }}</span><span class="frame-stale" v-if="online && !cameraFresh">画面尚未就绪</span></div>
          <div class="mode-note"><ShieldCheck :size="17" /><span v-if="!state">连接后展示后台实际检测状态。</span><span v-else-if="state.mode === 'demo'">当前为合成演示，无需摄像头；偷动事件由模拟观测产生。</span><span v-else-if="state.mode === 'webcam'">玩家固定在各自赛道；遮挡、跨道或画面不稳时暂不判断。</span><span v-else>实机适配待验证。{{ state.robot.status }} · {{ state.robot.armed ? '已启用控制' : '未启用控制' }}</span><button v-if="state?.mode === 'ros'" class="text-button" :disabled="!online || mutating || (!state.robot.armed && (phase !== 'lobby' || !state.robot.connected || !cameraFresh))" @click="setRobotArmed(!state.robot.armed)">{{ state.robot.armed ? '停用机器人' : '启用转向' }}</button></div>
        </section>

        <section class="scoreboard" :class="phaseTheme" aria-label="比赛状态">
          <div class="scoreboard-top"><span class="eyebrow">GAME STATUS</span><span class="round-tag">ROUND <strong>{{ String(state?.round ?? 0).padStart(2, '0') }}</strong></span></div>
          <div class="traffic-lights" aria-hidden="true"><span :class="{ on: phaseTheme === 'red' }"></span><span :class="{ on: phaseTheme === 'idle' }"></span><span :class="{ on: phaseTheme === 'green' || phaseTheme === 'finished' }"></span></div>
          <h2>{{ phaseTitle }}</h2><p class="phase-hint">{{ phaseHint }}</p>
          <div class="phase-timer"><span class="timer-digits">{{ ['lobby', 'finished', 'estop', 'paused'].includes(phase) ? (phase === 'lobby' ? 'READY' : phase === 'finished' ? 'END' : 'STOP') : phaseClock }}</span><span v-if="running" class="timer-unit">秒</span></div>
          <div class="match-stats"><div><span>本局剩余</span><strong>{{ gameClock }}</strong></div><div><span>仍在比赛</span><strong>{{ activeCount }}<small> / {{ state?.players.length ?? '—' }}</small></strong></div></div>
          <div class="main-control">
            <button v-if="phase === 'lobby'" class="button start-button" :disabled="!canStart || mutating" @click="control('start')"><LoaderCircle v-if="busy === 'start'" class="spin" :size="20" /><Play v-else :size="20" fill="currentColor" />开始比赛<ArrowUpRight :size="19" /></button>
            <button v-else-if="phase === 'paused'" class="button start-button" :disabled="!online || mutating" @click="control('resume')"><Play :size="20" fill="currentColor" />恢复比赛<ArrowUpRight :size="19" /></button>
            <button v-else-if="phase === 'finished' || phase === 'estop'" class="button start-button" :disabled="!online || mutating" @click="control(phase === 'estop' ? 'clear_estop' : 'reset')"><RotateCcw :size="19" />{{ phase === 'estop' ? '解除停止并重置' : '重置，准备下一局' }}</button>
            <button v-else class="button start-button pause-button" :disabled="!online || mutating" @click="control('pause')"><Pause :size="20" fill="currentColor" />暂停比赛</button>
          </div>
          <div class="secondary-controls"><button class="board-button" :disabled="!online || mutating || phase === 'estop'" @click="control('reset')"><RotateCcw :size="15" />重置</button><button class="board-button" :class="{ enabled: voiceEnabled }" @click="toggleVoice" :aria-pressed="voiceEnabled"><Volume2 v-if="voiceEnabled" :size="16" /><VolumeX v-else :size="16" />播报{{ voiceEnabled ? '开' : '关' }}</button><button v-if="canAward" class="board-button" :disabled="!online || mutating" @click="control('finish')"><Flag :size="14" />结束</button><button class="board-button stop-control" :disabled="!online" @click="control('estop')"><Square :size="14" fill="currentColor" />停止</button></div>
          <p class="backend-message" :title="state?.message">{{ state?.message || '等待本地服务发送比赛状态' }}</p>
        </section>
      </div>

      <section class="players-section">
        <div class="section-top"><div class="section-label"><span class="section-number">02</span><h2>选手赛道</h2><span class="small-label">固定赛道 · 无人脸识别</span></div><span class="threshold-note">动作阈值 <strong class="numeric">{{ formatNumber(state?.config.motion_threshold) }}</strong><button class="inline-icon" @click="helpOpen = true" aria-label="了解动作幅度"><CircleHelp :size="14" /></button></span></div>
        <div v-if="state" class="player-grid" :style="{ '--players': Math.min(state.players.length, 4) }">
          <article v-for="player in state.players" :key="player.id" class="player-card" :class="[player.status, { invisible: !player.visible }]">
            <div class="player-card-top"><span class="lane-number">{{ String(player.id).padStart(2, '0') }}<small>LANE</small></span><span class="player-status"><span class="status-dot"></span>{{ statusLabel(player.status) }}</span></div>
            <div class="player-name-row"><h3>{{ player.name }}</h3><Flag v-if="player.status === 'winner'" :size="23" /><Hand v-else-if="player.status === 'pending'" :size="23" /><X v-else-if="player.status === 'eliminated'" :size="22" /><Footprints v-else :size="23" /></div>
            <div class="motion-caption"><span>{{ player.visible ? '动作幅度' : '暂未看见玩家' }}</span><strong>{{ formatNumber(player.motion) }}<small> / {{ formatNumber(state.config.motion_threshold) }}</small></strong></div>
            <div class="motion-meter" :aria-label="'动作幅度 ' + formatNumber(player.motion)"><span :style="{ width: motionWidth(player) }"></span><i></i></div>
            <div class="player-card-bottom"><span><span class="visibility-dot" :class="{ visible: player.visible }"></span>{{ player.ambiguous && player.visible ? (player.reason === 'low_detection_confidence' ? '置信度不足 · 暂不判罚' : '遮挡或跨道 · 暂不判罚') : player.visible ? '画面中可见' : '等待回到赛道' }}</span><button v-if="player.status === 'active'" class="award-button" :disabled="!canAward || mutating" @click="winnerCandidate = player" :aria-label="'裁判确认' + player.name + '到达终点'"><Flag :size="13" />确认到终点</button><span v-else class="player-state-hint">{{ player.status === 'pending' ? '请前往证据复核' : player.status === 'winner' ? '裁判人工确认' : '本局停止计分' }}</span></div>
          </article>
        </div>
        <div v-else class="players-empty"><LoaderCircle class="spin" :size="20" />连接后加载选手赛道</div>
        <div v-if="state?.mode === 'demo'" class="demo-strip"><span class="demo-tag">DEMO LAB</span><p>试试让一位玩家在红灯时偷动。</p><div class="demo-actions"><select v-model.number="selectedDemoPlayer" aria-label="选择演示偷动的玩家" :disabled="!canSimulate"><option v-for="player in activePlayers" :key="player.id" :value="player.id">{{ player.name }}</option><option v-if="!activePlayers.length" :value="1">无可用玩家</option></select><button class="button demo-button" :disabled="!canSimulate" @click="control('demo_move', selectedDemoPlayer)"><Activity :size="16" />模拟偷动</button></div><span class="demo-caption">{{ phase === 'red' ? '只影响合成演示，不冒充真实检测' : '等待进入红灯阶段后可用' }}</span></div>
      </section>

      <button v-if="pendingEvidence.length" class="review-inbox" @click="navigate('review')"><ScanLine :size="20" /><span><strong>{{ pendingEvidence.length }} 条判罚等待复核</strong><small>查看前后画面，再作出裁判决定</small></span><ChevronRight :size="20" /></button>
      </div>
      <ControlRoom v-show="workspace === 'twin' || workspace === 'assistant'" :view="workspace === 'assistant' ? 'assistant' : 'twin'" :active="workspace === 'twin'" :state="state" :online="online" :token="token" :selected-evidence="selectedEvidence" @state="applyState" @settings="openSettings" />
      <div v-show="workspace === 'review'" class="bottom-grid">
        <section class="evidence-panel panel">
          <div class="panel-header"><div class="section-label"><span class="section-number">03</span><h2>判罚复核</h2><span class="count-badge">{{ pendingEvidence.length }}</span></div><SegmentedControl v-model="evidenceFilter" :options="[{label:'待复核',value:'pending'}, {label:'全部',value:'all'}]" label="证据筛选" /></div>
          <div v-if="evidenceItems.length === 0" class="evidence-empty"><div class="empty-frame"><ShieldCheck :size="31" :stroke-width="1.25" /><span class="empty-plus">+</span></div><h3>{{ evidenceFilter === 'pending' ? '当前没有待复核判罚' : '还没有产生判罚证据' }}</h3><p>{{ evidenceFilter === 'pending' ? '当前没有待复核判罚。发现持续动作后，' : '尚未产生证据。发现持续动作后，' }}<br />这里会展示前后画面，由你确认。</p><span class="review-policy"><span class="status-dot"></span>{{ state?.config.auto_eliminate ? '自动判罚已开启' : '默认人工复核后淘汰' }}</span></div>
          <div v-else class="evidence-list">
            <article v-for="item in evidenceItems" :key="item.id" class="evidence-card">
              <div class="evidence-title"><div><strong>{{ String(item.player_id).padStart(2, '0') }} 号玩家</strong><span>第 {{ item.round }} 回合 · 幅度 {{ formatNumber(item.score) }}</span></div><span class="evidence-status" :class="item.status">{{ item.saving ? '证据保存中' : { pending: '待复核', confirmed: '已确认', dismissed: '已撤销' }[item.status] }}</span></div>
              <button class="text-button" @click="explainEvidence(item.id)">让裁判助手解释这条记录</button>
              <div class="comparison"><figure><img v-if="item.before_url" :src="imageUrl(item.before_url)" alt="本次判罚的基准画面" loading="lazy" /><p v-else class="field-help">基准图不可用</p><figcaption><span>BEFORE</span>基准画面</figcaption></figure><span class="comparison-arrow"><ChevronRight :size="18" /></span><figure><img v-if="item.after_url" :src="imageUrl(item.after_url)" alt="检测到动作后的画面" loading="lazy" /><p v-else class="field-help">图片未保存，请检查磁盘</p><figcaption><span>AFTER</span>检测画面</figcaption></figure></div>
              <div v-if="item.status === 'pending'" class="review-actions"><button class="button dismiss-button" :disabled="!online || mutating || item.saving" @click="review(item, 'dismiss')"><RotateCcw :size="15" />撤销判罚</button><button class="button confirm-button" :disabled="!online || mutating || item.saving" @click="review(item, 'confirm')"><LoaderCircle v-if="reviewBusy === item.id || item.saving" class="spin" :size="16" /><Check v-else :size="16" />确认偷动 · 淘汰</button></div>
            </article>
          </div>
        </section>
        <section class="events-panel panel">
          <div class="panel-header"><div class="section-label"><span class="section-number">04</span><h2>比赛日志</h2></div><button class="text-button export-button" :disabled="!online || exporting" @click="exportReport"><LoaderCircle v-if="exporting" class="spin" :size="15" /><ArrowDownToLine v-else :size="15" />导出 JSON</button></div>
          <p v-if="state?.storage?.pending" class="field-help" role="status">比赛记录正在保存，仍可导出当前记录。</p>
          <div v-if="!events.length" class="events-empty"><Radio :size="25" :stroke-width="1.4" /><p>等待第一声发令。</p><span>比赛事件会按时间记录在这里</span></div>
          <ol v-else class="event-list"><li v-for="event in events" :key="event.id" :class="event.kind"><time>{{ event.time }}</time><span class="event-dot"></span><p>{{ event.text }}</p></li></ol>
          <div class="log-footer"><span class="status-dot" :class="{ disconnected: !connected }"></span>{{ connected ? '状态实时同步' : '等待重新连接' }}<span class="numeric">{{ lastSync?.toLocaleTimeString('zh-CN', { hour12: false }) ?? '--:--:--' }}</span></div>
        </section>
      </div>
      <footer class="page-footer"><span class="footer-brand">SPARK / FREEZE</span><p>站稳你的赛道，玩好这一局。</p><span>{{ state?.mode === 'demo' ? '电脑演示 · 无实机连接' : state?.mode === 'ros' ? 'ROS 适配 · 待实机验证' : '本地视觉裁判工作台' }}</span></footer>
    </main>
    </div>

    <Teleport to="body">
      <Transition name="toast"><div v-if="toast" class="toast-message" :class="{ error: toast.error }" role="status"><X v-if="toast.error" :size="18" /><Check v-else :size="18" />{{ toast.text }}<button @click="toast = null" aria-label="关闭提示"><X :size="15" /></button></div></Transition>
      <Transition name="drawer"><div v-if="settingsOpen" class="modal-backdrop" @click.self="settingsOpen = false"><aside ref="dialogElement" class="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabindex="-1"><div class="drawer-header"><div><span class="eyebrow">MATCH CONFIGURATION</span><h2 id="settings-title">比赛设置</h2></div><button class="icon-button" @click="settingsOpen = false" aria-label="关闭设置"><X :size="22" /></button></div>
        <form class="settings-form" @submit.prevent="saveSettings">
          <div v-if="!configurable" class="settings-notice"><Hand :size="17" />比赛参数仅可在等待开场或本局结束时更改。</div>
          <fieldset :disabled="!configurable || busy === 'config'"><legend>01 / 画面来源</legend><div class="mode-options"><label :class="{ checked: configMode === 'demo' }"><input v-model="configMode" type="radio" value="demo" /><Activity :size="19" /><strong>模拟演示</strong><small>无需额外设备</small></label><label :class="{ checked: configMode === 'webcam' }"><input v-model="configMode" type="radio" value="webcam" /><Camera :size="19" /><strong>电脑摄像头</strong><small>实际姿态检测</small></label><label :class="{ checked: configMode === 'ros' }"><input v-model="configMode" type="radio" value="ros" /><Radio :size="19" /><strong>SPARK / ROS</strong><small>待实机联调</small></label></div><p class="field-help" v-if="configMode === 'demo'">合成画面由后台生成，方便完整体验发令与复核流程。</p><p class="field-help" v-else-if="configMode === 'webcam'">需安装视觉依赖和姿态模型。加载与设备错误会直接显示在裁判台。</p><p class="field-help" v-else>需要机器人桥接程序与实机连接；当前电脑演示无需使用此模式。</p><label v-if="configMode === 'webcam'" class="field-row"><span>摄像头编号<small>通常内置摄像头为 0</small></span><input v-model.number="form.camera_index" type="number" min="0" max="10" step="1" required /></label></fieldset>
          <fieldset :disabled="!configurable || busy === 'config'"><legend>02 / 比赛节奏</legend><div class="field-grid"><label>玩家人数<input v-model.number="form.players" type="number" min="1" max="4" step="1" required /></label><label>整局时长 / 秒<input v-model.number="form.game_seconds" type="number" min="20" max="900" step="1" required /></label><label>绿灯最短 / 秒<input v-model.number="form.green_min" type="number" min="1" max="20" step="0.5" required /></label><label>绿灯最长 / 秒<input v-model.number="form.green_max" type="number" min="1" max="30" step="0.5" required /></label><label>红灯时长 / 秒<input v-model.number="form.red_seconds" type="number" min="2" max="20" step="0.5" required /></label><label>画面稳定等待 / 秒<input v-model.number="form.settle_seconds" type="number" min="0.6" max="5" step="0.1" required /></label></div><p class="field-help">保存设置会新建场次，旧场次数据保留。每位玩家固定使用一条赛道。</p></fieldset>
          <fieldset :disabled="!configurable || busy === 'config'"><legend>03 / 判罚规则</legend><div class="field-grid"><label>动作幅度阈值<input v-model.number="form.motion_threshold" type="number" min="0.015" max="0.3" step="0.005" required /></label><label>持续动作时间 / 秒<input v-model.number="form.hold_seconds" type="number" min="0.2" max="2" step="0.05" required /></label></div><p class="field-help">幅度按身体高度归一化；数值越小越敏感。持续达到阈值后才生成判罚证据。</p><label class="toggle-row"><span><strong>自动判罚</strong><small>关闭时，由裁判复核证据后淘汰</small></span><input v-model="form.auto_eliminate" type="checkbox" class="switch-input" /><span class="switch-track" aria-hidden="true"></span></label></fieldset>
          <fieldset><legend>04 / 控制权限</legend><label class="token-field">控制令牌<input v-model.trim="token" type="password" placeholder="后台未设置令牌时留空" autocomplete="off" /></label><p class="field-help">仅保存在当前浏览器会话中，不写入比赛报告。</p><label class="toggle-row"><span><strong>浏览器语音播报</strong><small>使用系统中文语音，需浏览器支持</small></span><input :checked="voiceEnabled" type="checkbox" class="switch-input" @change="toggleVoice" /><span class="switch-track" aria-hidden="true"></span></label></fieldset>
          <fieldset :disabled="!configurable || busy === 'config'"><legend>05 / 同步口令播报</legend><label class="token-field">播放端<select v-model="form.speech_target" aria-label="播放端"><option value="off">关闭同步口令</option><option value="browser">电脑扬声器 · 缓存中文语音</option><option value="robot" :disabled="configMode !== 'ros'">SPARK 扬声器 · 需要 ROS 音频模块</option></select></label><p class="field-help">同步口令播放完成后才推进关键阶段；失败会暂停比赛。选择电脑播放后，请在数字孪生面板先点击“测试播放”。启用后不再使用上方系统语音辅助播报。</p></fieldset>
          <div class="drawer-footer"><button type="button" class="button subtle" @click="settingsOpen = false">返回比赛</button><button type="submit" class="button save-button" :disabled="!configurable || mutating"><LoaderCircle v-if="busy === 'config'" class="spin" :size="17" /><Check v-else :size="17" />保存比赛设置</button></div>
        </form>
      </aside></div></Transition>
      <div v-if="helpOpen" class="modal-backdrop centered" @click.self="helpOpen = false"><section class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="help-title"><button class="icon-button dialog-close" @click="helpOpen = false" aria-label="关闭说明"><X :size="21" /></button><span class="eyebrow">HOW TO PLAY</span><h2 id="help-title">绿灯走，红灯停。</h2><ol class="rules-list"><li><span>01</span><p><strong>站好赛道</strong>每位玩家固定站在自己的竖向区域内。画面中多人重叠、遮挡或跨道时，暂不作出判断。</p></li><li><span>02</span><p><strong>听令行动</strong>绿灯可以动；转身后先等待画面稳定，红灯期间检测持续动作。</p></li><li><span>03</span><p><strong>有证据再判罚</strong>动作幅度按身体高度归一化。默认产生前后画面，由裁判确认或撤销；到达终点也需裁判人工确认。</p></li></ol><p class="info-note">电脑模式不会驱动 SPARK。实际摄像头需要完整拍到身体；本项目不做身份识别，也不承诺无误判。</p><button class="button save-button full-width" @click="helpOpen = false">明白了，回到比赛<ArrowUpRight :size="17" /></button></section></div>
      <div v-if="winnerCandidate" class="modal-backdrop centered" @click.self="winnerCandidate = null"><section class="info-dialog winner-dialog" role="dialog" aria-modal="true" aria-labelledby="winner-title"><Flag :size="35" /><span class="eyebrow">REFEREE DECISION</span><h2 id="winner-title">确认 {{ winnerCandidate.name }}<br />已到达终点？</h2><p>这是裁判的人工决定，系统不会从画面自动判断到达终点。</p><div class="review-actions"><button class="button subtle" @click="winnerCandidate = null">再看一下</button><button class="button save-button" :disabled="!canAward || mutating" @click="control('declare_winner', winnerCandidate.id); winnerCandidate = null"><Check :size="17" />确认到达</button></div></section></div>
    </Teleport>
  </div>
</template>






