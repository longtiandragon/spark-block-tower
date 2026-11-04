<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, toRef, watch } from 'vue'
import { AudioLines, Bot, LoaderCircle, Send, Volume2, ArrowUpRight, ShieldCheck, FileSearch, MessageSquare, Radio } from 'lucide-vue-next'
import SectionCard from './ui/SectionCard.vue'
import StatusBadge from './ui/StatusBadge.vue'
const TwinScene = defineAsyncComponent(() => import('./TwinScene.vue'))
import { useSpeech } from '../useSpeech'
import { request } from '../api'
import type { GameState } from '../types'
const props = defineProps<{ state: GameState | null; online: boolean; token: string; selectedEvidence?: string; view: 'twin' | 'assistant'; active: boolean }>()
const emit = defineEmits<{ state: [value: GameState]; settings: [] }>()
// Keep speech and assistant state mounted across navigation; load WebGL only on first visit.
const sceneVisited = ref(props.active)
watch(() => props.active, value => { if (value) sceneVisited.value = true })
const { unlocked, error: audioError, unlock } = useSpeech(toRef(props,'state'),toRef(props,'online'),toRef(props,'token'))
const question = ref('请解释当前比赛规则和最新判罚。'), evidenceId = ref(''), selectedLane = ref<number | null>(null)
const cloud = ref(false), configured = ref(false), model = ref(''), busy = ref(false), testing = ref(false), error = ref('')
interface Answer { answer: string; source: string; warning: string | null; model: string | null; tools: string[]; context: {session_id: string; revision: number; round: number; phase: string} }
const answer = ref<Answer | null>(null)
let alive = true, questionEpoch = 0
const speechLabel = computed(() => props.state?.speech ? ({pending:'等待播放端',claimed:'已领取口令',playing:'正在播放',done:'播放完成',failed:'播放失败'}[props.state.speech.status]) : '等待下一条口令')
const targetLabel = computed(() => ({off:'同步播报未启用',browser:'电脑扬声器',robot:'SPARK 扬声器'}[props.state?.config.speech_target ?? 'off']))
const angle = computed(() => props.state?.mode === 'ros' && props.state.robot.connected ? `${(props.state.robot.yaw * 180/Math.PI).toFixed(1)}°` : '待接实机')
const nearest = computed(() => props.state?.mode === 'ros' && props.state.robot.connected && props.state.robot.clearance != null ? `${props.state.robot.clearance.toFixed(2)} m` : '—')
const canTest = computed(() => props.online && props.state && ['lobby','finished'].includes(props.state.phase) && props.state.config.speech_target !== 'off')
const selectedPlayer = computed(() => props.state?.players.find(p=>p.id===selectedLane.value))
watch(() => props.selectedEvidence,id => { if(id) { evidenceId.value=id; question.value='请解释这条判罚：系统记录了什么，是否已经淘汰？' } })
watch(() => props.state?.session_id,() => { questionEpoch++;answer.value=null;evidenceId.value='';selectedLane.value=null;busy.value=false;error.value='' })
async function testAudio() {
  if (!canTest.value || testing.value) return
  testing.value=true;error.value=''
  const session = props.state?.session_id
  try {
    if(props.state?.config.speech_target==='browser')await unlock()
    if(props.state?.config.speech_target==='browser' && audioError.value)throw new Error(audioError.value)
    const data=await request<GameState>('/api/speech/test',{body:{},token:props.token})
    if(alive && props.state?.session_id===session)emit('state',data)
  }catch(e){error.value=e instanceof Error?e.message:'测试播放失败'}finally{testing.value=false}
}
async function ask() {
  if(!props.state || !props.online || busy.value || !question.value.trim())return
  const stamp=++questionEpoch;busy.value=true;error.value=''
  const session=props.state.session_id
  try{
    const result=await request<Answer>('/api/assistant/ask',{body:{question:question.value,expected_session_id:session,evidence_id:evidenceId.value||null,use_cloud:cloud.value},token:props.token,timeout:25000})
    if(alive && stamp===questionEpoch && props.state?.session_id===session)answer.value=result
  }catch(e){if(alive && stamp===questionEpoch)error.value=e instanceof Error?e.message:'请求失败'}
  finally{if(stamp===questionEpoch)busy.value=false}
}
onMounted(async()=>{try{const info=await request<{configured:boolean;model:string}>('/api/assistant/status');if(alive){configured.value=info.configured;model.value=info.model;cloud.value=info.configured}}catch{/* facts remain available */}})
onBeforeUnmount(()=>{alive=false;questionEpoch++})
</script>

<template>
  <section class="control-room" aria-label="数字孪生与裁判助手">
    <div v-show="view === 'twin'" class="twin-workspace">
      <SectionCard class="scene-card" title="赛场数字孪生" description="固定赛道示意 · 点击人物查看玩家状态" flush>
        <template #actions><StatusBadge variant="label" :tone="state?.mode === 'ros' && state.robot.connected ? 'success' : 'neutral'">{{ state?.mode === 'ros' && state.robot.connected ? 'ROS 反馈' : '模拟场景' }}</StatusBadge></template>
        <div class="scene-wrap"><TwinScene v-if="sceneVisited" :state="state" :online="online" :active="active" @select="selectedLane=$event" /></div>
        <div class="lane-inspector">
          <div class="lane-selector" aria-label="查看赛道状态"><button v-for="player in state?.players" :key="player.id" :class="{selected:selectedLane===player.id}" :aria-pressed="selectedLane===player.id" @click="selectedLane=player.id"><span :class="player.status"></span>{{ player.id }} 号赛道</button></div>
          <p class="lane-detail" aria-live="polite">{{ selectedPlayer ? `${selectedPlayer.name} · 动作分数 ${selectedPlayer.motion.toFixed(4)} · ${selectedPlayer.visible ? '当前可见' : '当前不可见'}${selectedPlayer.ambiguous ? ' · 暂不可判定' : ''}` : '点击三维人物或赛道按钮，查看该玩家状态。' }}</p>
        </div>
      </SectionCard>
      <div class="twin-side">
        <SectionCard title="设备状态" description="实机连接后显示实际反馈">
          <template #actions><Radio :size="18" class="muted-icon" /></template>
          <div class="twin-telemetry"><div><span>机器人朝向</span><strong>{{ angle }}</strong></div><div><span>最近障碍</span><strong>{{ nearest }}</strong></div><div><span>当前阶段</span><StatusBadge variant="label" tone="brand">{{ state?.phase_label ?? '等待连接' }}</StatusBadge></div></div>
          <p class="device-footnote">{{ state?.mode === 'ros' ? state.robot.status : '当前使用电脑模拟，未连接 SPARK 实机。' }}</p>
        </SectionCard>
        <SectionCard title="裁判播报" description="固定中文口令 · 缓存音频离线播放">
          <template #actions><AudioLines :size="19" class="muted-icon" /></template>
          <div class="speaker-visual" aria-hidden="true"><span></span><span></span><span></span><Volume2 :size="27" /><span></span><span></span><span></span></div>
          <div class="speech-strip"><strong>{{ targetLabel }}</strong><span>{{ state?.speech?.text ?? '等待裁判发令' }}</span><small>{{ speechLabel }}</small></div>
          <div class="speech-controls"><button v-if="state?.config.speech_target==='browser' && !unlocked" class="button save-button" @click="unlock"><Volume2 :size="16" />启用本页声音</button><button v-else-if="state?.config.speech_target==='off'" class="button subtle" @click="emit('settings')">设置播报<ArrowUpRight :size="15" /></button><button class="button subtle" :disabled="!canTest || testing" @click="testAudio"><LoaderCircle v-if="testing" :size="15" class="spin" /><Volume2 v-else :size="15" />测试播放</button></div>
          <div class="audio-actions"><span v-if="state?.config.speech_target==='robot'">{{ state.robot.audio?.ready ? '机器人音频已就绪' : state.robot.audio?.error || '等待机器人音频模块' }}</span><span v-else>开场前请确认实际扬声器发声</span></div>
          <p v-if="audioError" class="room-error" role="alert">{{ audioError }}</p>
          <p v-if="error && view === 'twin'" class="room-error" role="alert">{{ error }}</p>
        </SectionCard>
      </div>
    </div>
    <div v-show="view === 'assistant'" class="assistant-workspace">
      <SectionCard class="assistant-column" title="裁判助手" description="使用比赛状态、规则与证据回答你的问题">
        <template #actions><StatusBadge variant="label" :tone="configured ? 'success' : 'neutral'">{{ configured ? 'AI 已就绪' : '本地事实模式' }}</StatusBadge></template>
        <div class="assistant-mode"><span>{{ configured ? `可用模型 · ${model}` : '云端未配置，仍可查询本场事实' }}</span><label><input v-model="cloud" type="checkbox" :disabled="!configured || busy" />使用云端 AI</label></div>
        <div class="question-presets" aria-label="常用问题"><button :disabled="busy" @click="question='请解释当前比赛规则。'"><MessageSquare :size="14" />比赛规则</button><button :disabled="busy" @click="question='请解释最新判罚：系统记录了什么，是否已经淘汰？'"><FileSearch :size="14" />最新判罚</button><button :disabled="busy" @click="question='请总结本场比赛目前的状态。'"><ShieldCheck :size="14" />本场概况</button></div>
        <form @submit.prevent="ask">
          <label class="room-field">关联证据<select v-model="evidenceId" :disabled="busy"><option value="">本场概况 / 最新记录</option><option v-for="item in state?.evidence" :key="item.id" :value="item.id">第 {{ item.round }} 回合 · {{ item.player_id }} 号 · {{ item.id.slice(0,6) }}</option></select></label>
          <label class="room-field question-field">想了解什么<textarea v-model="question" maxlength="600" rows="4" placeholder="例如：为什么产生这条判罚？" :disabled="busy"></textarea><span class="character-count">{{ question.length }} / 600</span></label>
          <div class="assistant-submit"><span><ShieldCheck :size="13" />只读查询 · 不会操作比赛</span><button class="button save-button" :disabled="!online || busy || !question.trim()" type="submit"><LoaderCircle v-if="busy" class="spin" :size="16" /><Send v-else :size="16" />{{ busy ? '正在分析' : cloud ? '询问 AI' : '查看事实' }}</button></div>
        </form>
        <div v-if="answer" class="assistant-answer" aria-live="polite"><div class="answer-source"><Bot :size="17" />{{ answer.source==='cloud_ai' ? 'AI 参考解释' : '本地事实摘要 · 非大模型回答' }}</div><p>{{ answer.answer }}</p><small v-if="answer.warning">{{ answer.warning }}</small><div class="answer-context">第 {{ answer.context.round }} 回合 · {{ answer.context.phase }} · 状态版本 {{ answer.context.revision }}<span v-if="state?.revision!==answer.context.revision">（比赛已有更新）</span></div><span v-if="answer.tools.length" class="tool-trace">已查询：比赛状态、规则与证据</span></div>
        <div v-else class="assistant-empty"><span class="assistant-empty-icon"><Bot :size="27" /></span><div><strong>每个判断，都回到事实。</strong><p>选择上方的问题，或关联一条判罚记录。<br />助手会结合本场数据给出解释。</p></div></div>
        <p v-if="error && view === 'assistant'" class="room-error" role="alert">{{ error }}</p>
      </SectionCard>
      <aside class="assistant-context">
        <SectionCard title="本场上下文" description="查询时使用当前场次的数据">
          <div class="context-session"><span>SESSION</span><strong>{{ state?.session_id.slice(0,8).toUpperCase() ?? '—' }}</strong></div>
          <dl class="context-facts"><div><dt>比赛阶段</dt><dd>{{ state?.phase_label ?? '等待连接' }}</dd></div><div><dt>当前回合</dt><dd>{{ state?.round ?? '—' }}</dd></div><div><dt>参与玩家</dt><dd>{{ state?.players.length ?? '—' }} 人</dd></div><div><dt>判罚证据</dt><dd>{{ state?.evidence.length ?? 0 }} 条</dd></div><div><dt>待人工复核</dt><dd>{{ state?.evidence.filter(e=>e.status==='pending').length ?? 0 }} 条</dd></div></dl>
        </SectionCard>
        <div class="assistant-boundary"><ShieldCheck :size="21" /><strong>裁判决定，始终由你掌握。</strong><p>助手负责解释，不能启动比赛、控制机器人或确认淘汰。AI 回答供参考，请结合原始证据复核。</p><span>FACTS FIRST / HUMAN IN CONTROL</span></div>
      </aside>
    </div>
  </section>
</template>
<style scoped>
.control-room{min-width:0}.twin-workspace{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(270px,1fr);gap:22px;align-items:start}.twin-side{display:grid;gap:20px}.scene-wrap{padding:0 16px}.lane-inspector{padding:18px 24px 22px}.lane-selector{display:flex;flex-wrap:wrap;gap:8px}.lane-selector button{display:flex;align-items:center;gap:8px;border:1px solid var(--border);background:white;padding:9px 12px;border-radius:7px;font-size:12px;color:var(--text)}.lane-selector button.selected{border-color:#9cb4f3;background:var(--brand-soft);color:var(--brand-600)}.lane-selector span{width:6px;height:6px;border-radius:50%;background:var(--success)}.lane-selector .pending{background:var(--warning)}.lane-selector .eliminated{background:var(--text-faint)}.lane-selector .winner{background:#ac8515}.lane-detail{font-size:12px;color:var(--text-muted);line-height:1.9;margin-top:14px;min-height:23px}.muted-icon{color:#859bb8}.twin-telemetry>div{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 0;border-bottom:1px solid var(--border);font-size:12px;color:var(--text-muted)}.twin-telemetry strong{font-size:15px;font-weight:600;color:var(--text-strong)}.device-footnote{font-size:11px;color:var(--text-muted);line-height:1.8;margin-top:15px}.speaker-visual{display:flex;align-items:center;justify-content:center;gap:6px;color:#5577ba;background:#f2f5fc;border:1px solid #e5eaf5;border-radius:10px;height:76px;margin:2px 0 17px}.speaker-visual>span{height:12px;width:3px;background:#b1c3e6;border-radius:3px}.speaker-visual>span:nth-child(2),.speaker-visual>span:nth-child(6){height:22px}.speaker-visual>span:nth-child(3),.speaker-visual>span:nth-child(5){height:32px}.speaker-visual>svg{margin:0 9px}.speech-strip{text-align:center}.speech-strip strong,.speech-strip span,.speech-strip small{display:block}.speech-strip strong{font-size:14px;color:var(--text-strong)}.speech-strip span{font-size:12px;color:var(--text-muted);line-height:1.8;margin:8px 0}.speech-strip small{font-size:10px;color:var(--text-muted)}.speech-controls{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin-top:18px}.speech-controls .button{font-size:12px;padding:9px 12px}.audio-actions{text-align:center;font-size:10px;color:var(--text-muted);margin-top:13px;line-height:1.8}.assistant-workspace{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(255px,1fr);gap:22px;align-items:start}.assistant-mode{font-size:11px;color:var(--text-muted);display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap;padding:12px 0 18px;border-top:1px solid var(--border)}.assistant-mode label{display:flex;align-items:center;gap:6px}.question-presets{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:24px}.question-presets button{display:flex;align-items:center;gap:7px;padding:9px 11px;border:1px solid var(--border);background:#f8faff;border-radius:7px;font-size:12px;color:var(--text)}.question-presets button:hover{background:var(--brand-soft);color:var(--brand-600)}.room-field{display:block;font-size:12px;color:var(--text);margin-bottom:18px}.room-field select,.room-field textarea{display:block;width:100%;border:1px solid var(--border-strong);border-radius:8px;padding:11px 12px;margin-top:8px;background:white;color:var(--text-strong);font:inherit;line-height:1.8}.room-field textarea{resize:vertical;min-height:140px;padding-bottom:30px}.question-field{position:relative}.character-count{position:absolute;bottom:10px;right:13px;font:11px Outfit,sans-serif;color:var(--text-muted);pointer-events:none}.assistant-submit{display:flex;align-items:center;justify-content:space-between;gap:10px}.assistant-submit>span{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--text-muted)}.assistant-empty{display:flex;align-items:center;gap:18px;padding:30px 20px;border:1px dashed #ced9eb;border-radius:10px;margin-top:28px;background:#fafcff}.assistant-empty-icon{display:grid;place-items:center;width:52px;height:52px;border-radius:14px;background:#edf2ff;color:#7894cd;flex-shrink:0}.assistant-empty strong{font-size:15px;font-weight:500;color:var(--text)}.assistant-empty p{font-size:12px;color:var(--text-muted);line-height:1.9;margin-top:8px}.assistant-answer{margin-top:28px;border:1px solid #dbe5f5;background:#f8faff;padding:22px;border-radius:10px}.answer-source{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--brand-600);font-weight:600}.assistant-answer p{font-size:13px;line-height:2;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere;margin:16px 0}.assistant-answer small{display:block;font-size:11px;color:var(--warning-ink);line-height:1.8}.answer-context{font-size:10px;color:var(--text-muted);line-height:1.8;border-top:1px solid var(--border);padding-top:12px;margin-top:16px}.tool-trace{display:block;font-size:10px;color:var(--success-ink);margin-top:8px}.room-error{font-size:12px;color:var(--danger-ink);line-height:1.8;padding:12px;background:var(--danger-tint);border-radius:7px;margin-top:16px;overflow-wrap:anywhere}.context-session{display:flex;flex-direction:column;gap:7px;padding:18px;background:var(--bg-subtle);border-radius:8px}.context-session span{font:10px Outfit,sans-serif;letter-spacing:2px;color:var(--text-muted)}.context-session strong{font:400 24px Outfit,monospace;letter-spacing:2px;color:var(--text-strong)}.context-facts{margin:15px 0 0}.context-facts>div{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid var(--border);font-size:12px}.context-facts>div:last-child{border:0;padding-bottom:0}.context-facts dt{color:var(--text-muted)}.context-facts dd{margin:0;color:var(--text-strong)}.assistant-boundary{margin-top:20px;padding:24px;background:#eaf0fa;border-radius:12px;border:1px solid #dce4f3;color:#627fae}.assistant-boundary strong{display:block;font-size:13px;color:#425d86;margin:14px 0 10px}.assistant-boundary p{font-size:12px;line-height:1.9;color:#5f7494}.assistant-boundary>span{display:block;font:9px Outfit,sans-serif;letter-spacing:1px;margin-top:22px}
@media(max-width:1180px){.twin-workspace,.assistant-workspace{grid-template-columns:minmax(0,1.6fr) minmax(245px,1fr);gap:16px}.twin-side{gap:16px}.speech-controls .button{padding:8px;font-size:11px}}
@media(max-width:900px){.twin-workspace,.assistant-workspace{grid-template-columns:1fr}.twin-side{grid-template-columns:1fr 1fr}.assistant-context{display:grid;grid-template-columns:1fr 1fr;gap:16px}.assistant-boundary{margin:0}}
@media(max-width:600px){.twin-side,.assistant-context{grid-template-columns:1fr}.scene-wrap{padding:0 8px}.lane-inspector{padding:16px}.lane-selector{gap:6px}.lane-selector button{padding:9px;font-size:11px}.assistant-submit{flex-wrap:wrap}.assistant-submit .button{margin-left:auto}.assistant-empty{padding:22px 16px;gap:12px}.assistant-empty strong{font-size:13px}.assistant-empty-icon{width:40px;height:44px}.assistant-answer{padding:16px}.question-presets{gap:6px;margin-bottom:20px}.question-presets button{padding:9px 8px;font-size:11px}.assistant-boundary{padding:20px}.twin-telemetry>div{padding:12px 0}}
</style>
