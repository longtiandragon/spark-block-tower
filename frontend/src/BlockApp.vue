<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowDownToLine, ArrowRight, AudioLines, Bot, Box, Check, ChevronRight, CircleHelp, Layers3, MapPin, Mic, Pause, Play, Plus, Radio, RotateCcw, Save, ShieldAlert, Square, Volume2 } from 'lucide-vue-next'
import { request } from './api'

type Cell = { row: number; col: number; layer: number }
type Layout = { name: string; cells: Cell[]; color: 'blue' }
type State = {
  session: string; revision: number; mode: 'demo' | 'robot'; phase: string; phase_label: string;
  stop_id: string | null;
  layout: Layout; index: number; stage: number; armed: boolean; home: number[] | null;
  placed: { key: string; cell: Cell; verification: string }[];
  plan: { key: string; cell: Cell; source: number }[];
  events: { id: number; time: string; message: string }[];
  readiness: string | null; bridge_fresh: boolean;
  workspace_recorded?: boolean;
  bridge: { operation_mode?: 'mobile' | 'stationary'; dry_run: boolean; slots: string[]; sources: number; camera_age: number; scan_age: number; odom_age: number; stopped: boolean } | null;
  camera?: { fresh: boolean; sequence: number; boxes: unknown[] };
  evidence: { command_id: string; observation: string } | null;
}
const state = ref<State | null>(null)
const draft = ref<Layout>({ name: '三层蓝色积木塔', color: 'blue', cells: [0, 1, 2].map(layer => ({ row: 1, col: 1, layer })) })
const layer = ref(0)
const tab = ref<'build' | 'observe' | 'assistant'>('build')
const connected = ref(false)
const error = ref('')
const note = ref('')
const busy = ref(false)
const aiBusy = ref(false)
const dirty = ref(false)
const text = ref('用三块蓝色积木搭一座塔')
const answer = ref('告诉我想搭什么，或询问现在进行到哪一步。')
const answerSource = ref('本地任务助手')
const cloud = ref(false)
const cloudReady = ref(false)
const voice = ref(false)
const listening = ref(false)
const recoveryChecked = ref(false)
const startChecked = ref(false)
const frameUrl = ref('')
let tokenValue = ''
try { tokenValue = sessionStorage.getItem('spark-blocks-token') || '' } catch { /* storage optional */ }
const token = ref(tokenValue)
const tokenOpen = ref(false)
let timer = 0
let freshnessTimer = 0
let stateDeadline = 0
let disposed = false
let initialized = false
let lastSpoken = ''
let operationEpoch = 0
let draftVersion = 0
let draftBase: { session: string; revision: number } | null = null
const retiredSessions = new Set<string>()
let recognition: { start(): void; stop(): void; lang: string; onresult: ((event: any) => void) | null; onerror: (() => void) | null; onend: (() => void) | null } | null = null
const stages = computed(() => state.value?.bridge?.operation_mode === 'stationary'
  ? ['准备取料', '吸附并抬起', '检查吸附', '准备放置', '放置目标层', '检查塔体']
  : ['前往抓取点', '吸附并抬起', '检查吸附', '返回起始区', '放置目标层', '检查塔体'])
const locked = computed(() => !connected.value || busy.value || state.value?.phase !== 'idle')
const cameraFresh = computed(() => connected.value && !!state.value?.camera?.fresh)
const progress = computed(() => state.value ? Math.round(state.value.placed.length / state.value.layout.cells.length * 100) : 0)
const activeCell = computed(() => state.value?.plan[state.value.index]?.cell)
const needReview = computed(() => state.value?.phase === 'awaiting_pick' || state.value?.phase === 'awaiting_place')
const needsRecovery = computed(() => state.value?.phase === 'paused' || state.value?.phase === 'estop')
const towerLevels = computed(() => [2, 1, 0].map(level => ({ level, cells: draft.value.cells.filter(c => c.layer === level) })))
const recognitionSupported = 'SpeechRecognition' in window || 'webkitSpeechRecognition' in window
function key(c: Cell) { return `${c.row}-${c.col}-${c.layer}` }
function filled(row: number, col: number) { return draft.value.cells.some(c => c.row === row && c.col === col && c.layer === layer.value) }
function completed(c: Cell) { return state.value?.placed.some(p => p.key === key(c)) }
function cancelSpeech() {
  try { window.speechSynthesis?.cancel() } catch { /* Audio failure must never block control. */ }
}
function stopListening() {
  try { recognition?.stop() } catch { /* Continue with the requested stop even if the browser fails. */ }
  listening.value = false
}
function speak(message: string) {
  if (!voice.value || !('speechSynthesis' in window)) return
  try {
    cancelSpeech()
    const utterance = new SpeechSynthesisUtterance(message)
    utterance.lang = 'zh-CN'; utterance.rate = 1
    window.speechSynthesis.speak(utterance)
  } catch { note.value = '浏览器播报不可用，任务控制仍可使用。' }
}
function toggleVoice() {
  voice.value = !voice.value
  if (voice.value) speak('搭建语音播报已开启')
  else cancelSpeech()
}
function applyState(next: State) {
    if (disposed || retiredSessions.has(next.session)) return
    const previous = state.value
    if (previous?.session === next.session && next.revision < previous.revision) return
    if (previous && previous.session !== next.session) retiredSessions.add(previous.session)
    if (previous?.session !== next.session || previous?.bridge_fresh !== next.bridge_fresh) frameUrl.value = ''
    if (!initialized || !dirty.value || next.phase !== 'idle') {
      if (dirty.value && next.phase !== 'idle') note.value = '任务已由其他操作更新，预览已同步为实际执行目标。'
      draft.value = structuredClone(next.layout)
      dirty.value = false
      initialized = true
    }
    if (!previous || next.session !== previous.session || next.stop_id !== previous.stop_id) recoveryChecked.value = false
    const initialConditions = (s: State) => JSON.stringify([s.session, s.mode, s.layout, s.home])
    if (!previous || initialConditions(next) !== initialConditions(previous)) startChecked.value = false
    state.value = next
    const speechKey = `${next.session}:${next.index}:${next.phase}`
    if (speechKey !== lastSpoken) { lastSpoken = speechKey; speak(next.phase_label) }
    if (next.mode === 'robot' && next.camera?.fresh) frameUrl.value = `/api/blocks/frame.jpg?seq=${next.camera.sequence}`
}
function markDraft() {
  if (!dirty.value && state.value) draftBase = { session: state.value.session, revision: state.value.revision }
  dirty.value = true
  draftVersion++
}
function discardDraft() {
  if (!state.value || locked.value) return
  draft.value = JSON.parse(JSON.stringify(state.value.layout)) as Layout
  dirty.value = false
  draftBase = null
  draftVersion++
  note.value = '草案已放弃，已同步当前保存的目标布局'
}
function expireConnection() {
  connected.value = false
  startChecked.value = false
  recoveryChecked.value = false
  cancelSpeech()
  stopListening()
}
async function sync() {
  const epoch = operationEpoch
  const sentAt = performance.now()
  try {
    const next = await request<State>('/api/blocks/state')
    if (disposed || epoch !== operationEpoch) return
    // Count request latency too: a delayed response is not a fresh observation.
    if (performance.now() - sentAt >= 1000) {
      expireConnection()
      error.value = '状态响应已过期，等待重新同步。'
      return
    }
    applyState(next)
    connected.value = true
    stateDeadline = sentAt + 1000
    clearTimeout(freshnessTimer)
    freshnessTimer = window.setTimeout(expireConnection, Math.max(0, stateDeadline - performance.now()))
    if (next.mode === 'robot') await request('/api/blocks/operator/heartbeat', { body: {}, token: token.value })
  } catch (e) {
    if (!disposed && epoch === operationEpoch) {
      expireConnection()
      error.value = (e as Error).message
    }
  } finally {
    if (!disposed) timer = window.setTimeout(sync, 350)
  }
}
async function run(action: string, extra: Record<string, unknown> = {}) {
  if (!state.value) return
  if (!['estop', 'pause', 'reject'].includes(action) && (!connected.value || performance.now() >= stateDeadline)) {
    expireConnection()
    error.value = '任务状态已过期，请等待同步后重新操作。'
    return
  }
  if (action === 'estop' || action === 'pause') {
    cancelSpeech()
    stopListening()
  }
  const epoch = ++operationEpoch
  // Stop remains usable even while another request is pending.
  busy.value = true; error.value = ''; note.value = ''
  try {
    const next = await request<State>('/api/blocks/control', { body: { action, session: state.value.session, revision: state.value.revision, ...extra }, token: token.value })
    if (epoch !== operationEpoch) return
    applyState(next)
    if (action === 'reset' || action === 'recover') { recoveryChecked.value = false; startChecked.value = false }
  } catch (e) { if (epoch === operationEpoch) error.value = (e as Error).message }
  finally { if (epoch === operationEpoch) busy.value = false }
}
function toggleCell(row: number, col: number) {
  if (locked.value) return
  if (filled(row, col)) {
    draft.value.cells = draft.value.cells.filter(c => !(c.row === row && c.col === col && c.layer >= layer.value))
  } else {
    if (draft.value.cells.length >= 9) { error.value = '最多使用九块积木'; return }
    if (layer.value > 0 && !draft.value.cells.some(c => c.row === row && c.col === col && c.layer === layer.value - 1)) {
      error.value = '请先在这一格的下一层放置支撑积木'; return
    }
    draft.value.cells.push({ row, col, layer: layer.value })
  }
  markDraft()
}
function preset(kind: 'tower' | 'row' | 'corner') {
  if (locked.value) return
  draft.value = { name: kind === 'tower' ? '三层蓝色积木塔' : kind === 'row' ? '蓝色积木直线' : '蓝色积木拐角', color: 'blue', cells:
    kind === 'tower' ? [0, 1, 2].map(layer => ({ row: 1, col: 1, layer })) :
      kind === 'row' ? [0, 1, 2].map(col => ({ row: 1, col, layer: 0 })) :
        [{ row: 0, col: 0, layer: 0 }, { row: 1, col: 0, layer: 0 }, { row: 1, col: 1, layer: 0 }] }
  markDraft(); layer.value = 0
}
async function save() {
  if (!state.value) return
  const epoch = ++operationEpoch
  const basis = draftBase ?? { session: state.value.session, revision: state.value.revision }
  busy.value = true; error.value = ''
  try {
    const next = await request<State>('/api/blocks/layout', { method: 'PUT', body: { ...basis, layout: draft.value }, token: token.value })
    if (epoch !== operationEpoch) return
    dirty.value = false; note.value = '目标布局已保存'
    draftBase = null
    applyState(next)
  } catch (e) { if (epoch === operationEpoch) error.value = (e as Error).message }
  finally { if (epoch === operationEpoch) busy.value = false }
}
async function propose() {
  const version = draftVersion
  const session = state.value?.session
  aiBusy.value = true; error.value = ''
  try {
    const result = await request<{ layout: Layout }>('/api/blocks/propose', { body: { text: text.value }, token: token.value })
    if (locked.value || version !== draftVersion || session !== state.value?.session) { note.value = '任务或草案已改变，请重新生成布局'; return }
    draft.value = result.layout; markDraft(); layer.value = 0; tab.value = 'build'
    note.value = '语言指令已生成草案，请检查并保存。尚未启动机器人。'
  } catch (e) { error.value = (e as Error).message }
  finally { aiBusy.value = false }
}
async function ask() {
  const epoch = operationEpoch
  aiBusy.value = true; error.value = ''
  try {
    const result = await request<{ answer: string; source: string; warning: string | null; session: string; revision: number }>('/api/blocks/assistant', { body: { text: text.value, use_cloud: cloud.value }, token: token.value, timeout: 15000 })
    if (epoch !== operationEpoch || result.session !== state.value?.session || result.revision !== state.value?.revision) {
      note.value = '任务状态已改变，已丢弃过期的助手回答，请重新询问。'
      return
    }
    answer.value = result.answer; answerSource.value = result.source === 'cloud_ai' ? '云端 AI · 只读解释' : '本地任务事实'
    if (result.warning) note.value = result.warning
    speak(result.answer)
  } catch (e) { error.value = (e as Error).message }
  finally { aiBusy.value = false }
}
function listen() {
  const Constructor = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
  if (!Constructor) { error.value = '浏览器不支持语音输入，请直接输入文字'; return }
  if (listening.value) { stopListening(); return }
  const instance = new Constructor()
  recognition = instance
  instance.lang = 'zh-CN'
  instance.onresult = (event: any) => { text.value = event.results[0][0].transcript }
  instance.onerror = () => { error.value = '语音识别未完成，请检查麦克风权限或使用文字'; listening.value = false }
  instance.onend = () => { listening.value = false }
  try { instance.start(); listening.value = true } catch { error.value = '无法启动语音输入'; listening.value = false }
}
function saveToken() {
  try { sessionStorage.setItem('spark-blocks-token', token.value) } catch { /* use in memory */ }
  tokenOpen.value = false; note.value = '控制口令已更新，仅保存在当前浏览器会话'
}
onMounted(() => {
  void sync()
  request<{ configured: boolean }>('/api/blocks/assistant/status').then(s => cloudReady.value = s.configured).catch(() => {})
})
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); clearTimeout(freshnessTimer); stopListening(); cancelSpeech() })
</script>

<template>
  <div class="block-shell">
    <aside class="rail">
      <a class="brand" href="#" aria-label="SPARK Blocks 首页"><Box :size="29" /><span>SPARK<small>BLOCK STUDIO</small></span></a>
      <div class="rail-caption">搭建工作台</div>
      <nav aria-label="工作区">
        <button :class="{ selected: tab === 'build' }" @click="tab = 'build'"><Layers3 :size="19" />目标与搭建<ChevronRight :size="15" /></button>
        <button :class="{ selected: tab === 'observe' }" @click="tab = 'observe'"><Radio :size="19" />现场与检查<ChevronRight :size="15" /></button>
        <button :class="{ selected: tab === 'assistant' }" @click="tab = 'assistant'"><Bot :size="19" />搭建助手<ChevronRight :size="15" /></button>
      </nav>
      <div class="rail-project"><span class="blue-dot"></span> 默认蓝色积木<p>从一次搬运，<br />到一座小小的塔。</p><small>寻找 · 抬起 · 运回 · 叠放</small></div>
      <div class="rail-bottom"><span :class="['connection-dot', { online: connected }]"></span>{{ connected ? '工作台已连接' : '正在连接后台' }}</div>
    </aside>

    <main>
      <header class="topbar"><div><span class="eyebrow">EMBODIED BUILDING LAB</span><h1>{{ tab === 'build' ? '把想法，搭起来。' : tab === 'observe' ? '每一步，都看得见。' : '一起想，一起搭。' }}</h1></div><div class="top-actions"><button class="icon-button token-toggle" @click="tokenOpen = !tokenOpen">控制口令</button><button class="icon-button" :aria-pressed="voice" aria-label="切换浏览器语音播报" @click="toggleVoice"><Volume2 :size="19" />{{ voice ? '播报开' : '播报关' }}</button><button class="stop-button" @click="run('estop')"><Square :size="15" fill="currentColor" />紧急停止</button></div></header>
      <div v-if="tokenOpen" class="token-bar"><label for="control-token">控制口令</label><input id="control-token" v-model="token" type="password" autocomplete="off" /><button @click="saveToken">保存口令</button></div>
      <div v-if="error" class="notice error" role="alert">{{ error }}<button aria-label="关闭错误提示" @click="error = ''">×</button></div>
      <div v-if="note" class="notice" role="status">{{ note }}<button aria-label="关闭提示" @click="note = ''">×</button></div>

      <section class="mission-strip" aria-label="任务概览">
        <div><span class="eyebrow">CURRENT MISSION</span><strong>{{ state?.layout.name || '三层蓝色积木塔' }}</strong><span class="muted">{{ state?.mode === 'robot' ? '实机 · 每块人工确认' : '合成演示 · 不控制机器人' }}</span></div>
        <div class="mission-progress"><span><b>{{ state?.placed.length || 0 }}</b> / {{ state?.layout.cells.length || 3 }} 块已确认</span><div class="progress-track"><i :style="{ width: progress + '%' }"></i></div></div>
        <div class="mode-switch" role="group" aria-label="运行模式"><button :class="{ active: state?.mode === 'demo' }" :disabled="locked || busy" @click="run('mode', { mode: 'demo' })">合成演示</button><button :class="{ active: state?.mode === 'robot' }" :disabled="locked || busy" @click="run('mode', { mode: 'robot' })">真实机器人</button></div>
      </section>

      <div v-if="needsRecovery" class="recovery-panel"><ShieldAlert /><div><strong>{{ state?.phase_label }}</strong><p>现场确认设备已停止，安全处理吸附负载，并把积木复位、清空搭建区后再开始新任务。不会自动续接中断动作。</p><label><input v-model="recoveryChecked" type="checkbox" /> 我已完成现场检查和复位</label></div><button :disabled="!recoveryChecked || busy || !connected" @click="run('recover')">解除锁定并重置</button></div>

      <div v-if="tab === 'build'" class="build-grid">
        <section class="panel layout-panel">
          <div class="section-heading"><div><span class="eyebrow">01 / YOUR BLUEPRINT</span><h2>目标布局</h2></div><span class="pill">{{ dirty ? '有未保存修改' : '已保存' }}</span></div>
          <div class="preset-row"><button :disabled="locked" @click="preset('tower')">三层塔</button><button :disabled="locked" @click="preset('row')">一字排列</button><button :disabled="locked" @click="preset('corner')">拐角</button></div>
          <label class="layout-name">布局名称<input v-model="draft.name" maxlength="40" :disabled="locked" @input="markDraft" /></label>
          <div class="board-top"><span>点击格位添加 / 移除积木</span><span>俯视 · 3 × 3</span></div>
          <div class="board-workspace"><div class="layer-buttons" role="group" aria-label="编辑层数"><button v-for="level in [2, 1, 0]" :key="level" :class="{ active: layer === level }" @click="layer = level">L{{ level + 1 }}<small>{{ level === 0 ? '底层' : level === 1 ? '中层' : '顶层' }}</small></button></div><div class="layout-board" role="group" :aria-label="`第 ${layer + 1} 层布局`"><template v-for="row in [0, 1, 2]" :key="row"><button v-for="col in [0, 1, 2]" :key="col" :class="{ occupied: filled(row, col) }" :disabled="locked" :aria-label="`第${layer + 1}层 第${row + 1}行 第${col + 1}列`" :aria-pressed="filled(row, col)" @click="toggleCell(row, col)"><span v-if="filled(row, col)" class="board-block"><Box :size="26" /></span><Plus v-else :size="18" /><small>{{ row + 1 }}·{{ col + 1 }}</small></button></template></div></div>
          <p class="hint"><CircleHelp :size="15" />上层必须有支撑。删除下层时，同时移除其上方积木。</p>
          <button v-if="dirty" class="discard-draft" :disabled="locked" @click="discardDraft">放弃草案并同步</button><div class="layout-footer"><span><i class="blue-dot"></i>{{ draft.cells.length }} 块蓝色积木</span><button class="primary" :disabled="locked || busy || !draft.cells.length || !dirty" @click="save"><Save :size="16" />保存目标</button></div>
        </section>

        <section class="panel preview-panel">
          <div class="section-heading"><div><span class="eyebrow">02 / BUILD IN LAYERS</span><h2>搭建预览</h2></div><span class="pill light">{{ state?.mode === 'demo' ? '示意 · 非真实画面' : '目标示意' }}</span></div>
          <div class="tower-scene"><div class="scene-grid"></div><div class="tower-caption">BLUE BLOCKS<span>{{ Math.max(0, ...draft.cells.map(c => c.layer)) + 1 }} LEVELS</span></div><div class="layer-stack"><div v-for="level in towerLevels" :key="level.level" class="tower-level"><span class="level-mark">L{{ level.level + 1 }}</span><div class="level-blocks"><div v-for="cell in level.cells" :key="key(cell)" class="preview-cube" :class="{ placed: completed(cell), current: activeCell && key(activeCell) === key(cell) }"><Check v-if="completed(cell)" :size="22" /><span v-else>{{ cell.row + 1 }}·{{ cell.col + 1 }}</span></div><span v-if="!level.cells.length" class="empty-level">空层</span></div></div></div><div class="tower-base"><MapPin :size="15" />起始搭建区</div></div>
          <div class="preview-legend"><span><i></i>计划放置</span><span><i class="done"></i>已确认</span><span>从底层向上搭建</span></div>
          <div class="route"><div><span class="route-dot"></span><strong>取料区</strong><small>蓝色积木 / 标定接近点</small></div><ArrowRight :size="24" /><div><MapPin :size="22" /><strong>起始区</strong><small>逐层放置 / 每层检查</small></div></div>
        </section>
      </div>

      <section v-if="tab === 'observe'" class="observation-grid">
        <div class="panel"><div class="section-heading"><div><span class="eyebrow">LIVE OBSERVATION</span><h2>机器人相机</h2></div><span class="pill">{{ cameraFresh ? '画面新鲜' : '等待新画面' }}</span></div><div class="camera-view"><img v-if="frameUrl && state?.mode === 'robot'" :src="frameUrl" alt="机器人相机蓝色区域检测画面" /><div v-else class="camera-empty"><Radio :size="42" /><strong>{{ state?.mode === 'demo' ? '当前为合成演示' : '等待机器人上传相机画面' }}</strong><p>真实画面接入后显示蓝色区域标记</p></div><span v-if="frameUrl && !cameraFresh" class="stale-overlay">画面已过期，不可作为当前观察</span></div><p class="hint">同色积木叠在一起可能连成一个蓝色区域；检测框数量不等于积木层数。</p></div>
        <div class="panel connection-panel"><div class="section-heading"><div><span class="eyebrow">ROBOT LINK</span><h2>实机准备</h2></div></div><dl><dt>桥接</dt><dd>{{ connected && state?.bridge_fresh ? (state.bridge?.dry_run ? '在线 · dry-run' : '在线 · 实机') : '未连接' }}</dd><dt>已标定抓取站</dt><dd>{{ state?.bridge?.sources || 0 }} 个</dd><dt>已标定放置位</dt><dd>{{ state?.bridge?.slots.length || 0 }} 个</dd><dt>起始搭建区</dt><dd>{{ state?.workspace_recorded ? '已记录' : '未记录' }}</dd><dt>相机 / 雷达 / 里程计</dt><dd>{{ state?.bridge ? `${state.bridge.camera_age.toFixed(2)} / ${state.bridge.scan_age.toFixed(2)} / ${state.bridge.odom_age.toFixed(2)} s` : '等待数据' }}</dd></dl><button class="secondary" :disabled="locked || busy || state?.mode !== 'robot'" @click="run('record_home')"><MapPin :size="16" />{{ state?.bridge?.operation_mode === 'stationary' ? '确认固定工作区' : '记录当前位置为搭建区' }}</button><p class="hint">{{ state?.bridge?.operation_mode === 'stationary' ? '固定工作区模式：底盘与相机保持不动，取放路径从现场配置加载。每个取料站需预先放好积木。' : '在标定的搭建区停稳后记录。抓取接近点、每层高度和撤离路径从机器人现场配置加载。' }}</p><div class="readiness">{{ !connected ? '状态已过期，等待后台同步。' : (state?.readiness || (state?.mode === 'demo' ? '演示模式无需机器人连接。' : '链路就绪，启动前仍需确认场地与标定。')) }}</div></div>
      </section>

      <section v-if="tab === 'assistant'" class="assistant-grid"><div class="panel assistant-input"><span class="eyebrow">TALK TO YOUR BUILD PARTNER</span><h2>说出你想搭的样子。</h2><p>先生成布局草案，检查后保存；启动搭建始终由你决定。</p><label for="building-request">文字或语音指令</label><textarea id="building-request" v-model="text" maxlength="600" rows="5" placeholder="用三块蓝色积木搭一座塔"></textarea><div class="assistant-actions"><button :disabled="!recognitionSupported" :aria-pressed="listening" @click="listen"><Mic :size="17" />{{ listening ? '停止录音' : '语音输入' }}</button><button class="primary" :disabled="aiBusy || locked || !text.trim()" @click="propose">生成布局草案<ArrowRight :size="16" /></button></div><p class="hint">语音由浏览器识别，可能使用浏览器厂商在线服务。布局解析支持一行、拐角和最多三层塔。</p><div class="example-prompts"><button @click="text = '用三块蓝色积木搭一座塔'">三层积木塔</button><button @click="text = '现在进行到哪一步了？'">现在进行到哪一步？</button></div></div><div class="panel assistant-answer"><div class="section-heading"><h2><Bot :size="22" />搭建助手</h2><span class="pill">{{ answerSource }}</span></div><p class="answer-text">{{ answer }}</p><div class="assistant-bottom"><label><input v-model="cloud" type="checkbox" :disabled="!cloudReady" />使用云端 AI 解释{{ cloudReady ? '' : '（未配置）' }}</label><small>开启后发送文字问题与任务摘要，不发送相机图片。AI 没有动作控制权限。</small><button class="secondary" :disabled="aiBusy || !text.trim()" @click="ask"><AudioLines :size="17" />{{ aiBusy ? '正在处理…' : '询问任务与搭建建议' }}</button></div></div></section>

      <section v-if="needReview" class="review-panel" aria-label="实物结果复核"><div><span class="eyebrow">CHECK BEFORE CONTINUING</span><h2>{{ state?.phase_label }}</h2><p>{{ state?.evidence?.observation }}</p><p>必须结合现场确认；蓝色检测和动作回执不能证明塔体稳定。</p><div><button class="primary" :disabled="busy || !connected" @click="run(state?.phase === 'awaiting_pick' ? 'confirm_pick' : 'confirm_place')"><Check :size="17" />{{ state?.phase === 'awaiting_pick' ? (state?.bridge?.operation_mode === 'stationary' ? '确认吸稳并抬起，继续放置' : '确认吸稳并抬起，继续返回') : '确认本层放置稳定，继续' }}</button><button :disabled="busy" @click="run('reject')">结果不符，停止检查</button></div></div><img v-if="state?.evidence" :src="`/api/blocks/evidence/${state.evidence.command_id}.jpg`" alt="当前步骤完成后的检查画面" /></section>

      <section class="panel execution-panel"><div class="section-heading"><div><span class="eyebrow">03 / STEP BY STEP</span><h2>{{ state?.phase_label || '正在连接…' }}<small v-if="state?.plan.length">第 {{ Math.min(state.index + 1, state.plan.length) }} / {{ state.plan.length }} 块</small></h2></div><a class="report-link" href="/api/blocks/report" download><ArrowDownToLine :size="16" />导出记录</a></div><ol class="steps"><li v-for="(step, i) in stages" :key="step" :class="{ current: state?.stage === i && state?.plan.length && !['completed', 'estop', 'paused'].includes(state.phase), done: state?.phase === 'completed' || (state?.plan.length && state.stage > i) }"><span>{{ i + 1 }}</span>{{ step }}</li></ol><div class="execution-bottom"><p>{{ state?.mode === 'demo' ? '演示采用合成状态，帮助预演顺序，不代表实机通过。' : (state?.readiness || '使用现场标定的接近点和分层落点，逐块检查后继续。') }}</p><div><button v-if="state?.phase === 'completed'" :disabled="busy" @click="run('reset')"><RotateCcw :size="16" />新一轮搭建</button><button v-if="state && !['idle', 'completed', 'estop', 'paused'].includes(state.phase)" :disabled="busy" @click="run('pause')"><Pause :size="16" />暂停检查</button><button class="primary" :disabled="!connected || busy || locked || dirty || (state?.mode === 'robot' && (!startChecked || !!state.readiness || !state.workspace_recorded))" @click="run('start')"><Play :size="17" fill="currentColor" />{{ state?.mode === 'robot' ? '开始实机搭建' : '运行搭建演示' }}</button></div></div><label v-if="state?.mode === 'robot' && !locked" class="start-check"><input v-model="startChecked" type="checkbox" />已确认目标格位标定匹配、搭建区清空、取料站积木就绪、设备已交接，现场有人可立即停止。</label></section>

      <section class="event-log"><h2>搭建记录 <span>ACTIVITY</span></h2><div v-if="!state?.events.length" class="muted">保存你的第一个布局，开始一次搭建。</div><div v-for="event in state?.events.slice(-5).reverse()" :key="event.id"><time>{{ event.time }}</time><span>{{ event.message }}</span></div></section>
      <footer>SPARK BLOCK STUDIO <span>蓝色积木 · 真实观察 · 逐层搭建</span></footer>
    </main>
  </div>
</template>
