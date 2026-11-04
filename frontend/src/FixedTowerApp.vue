<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import { request } from './api'
type State = { enabled:boolean; session:string; revision:number; phase:string; label:string; layer:number; placed:number; levels:number; structure?:string;
  agent_fresh:boolean; agent:{ready:boolean; stopped:boolean; progress:string;fault?:string|null;structures?:string[];sources?:number[][];source_note?:string;source_age?:number;slots_ready?:boolean;slot_version?:string}|null; camera_fresh?:boolean; frame_sequence?:number;
  events:{time:string;message:string}[]; error:string|null }
const state=ref<State|null>(null), error=ref(''), checked=ref(false), busy=ref(false), fresh=ref(false)
let storedToken=''
try{storedToken=sessionStorage.getItem('spark-blocks-token')||''}catch{/* Storage is optional. */}
const token=ref(storedToken)
const operator=Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('')
function saveToken(){try{sessionStorage.setItem('spark-blocks-token',token.value)}catch{/* Keep in memory. */}error.value=''}
let disposed=false, timer=0, expiry=0, epoch=0
const frame=ref('')
const selected=ref('tower3')
type Plan={steps:{index:number;label:string;supports:number[]}[];ik_audit:{passed:boolean;checked_points:number;matches_current:boolean;inspected_at:string;payload_geometry_passed?:boolean}|null}
const plan=ref<Plan|null>(null)
let planEpoch=0
watch(selected,async shape=>{
  const operation=++planEpoch;plan.value=null
  try{
    const next=await request<Plan>(`/api/fixed-tower/plan/${shape}`)
    if(!disposed&&operation===planEpoch)plan.value=next
  }catch{/* Optional planning evidence must not replace live control state. */}
},{immediate:true})
const choices=[{id:'tower3',name:'三层塔'}]
const levels=computed(()=>selected.value==='single'?1:selected.value==='tower2'?2:3)
const verified=computed(()=>(state.value?.agent?.structures||['single','tower2','tower3']).includes(selected.value))
const startReason=computed(()=>{
  if(!state.value||state.value.phase!=='idle')return ''
  if(!fresh.value)return '等待网页状态同步。'
  if(!token.value)return '请展开右侧“控制口令”并粘贴本机口令。'
  if(!state.value.agent_fresh)return '机器人执行端离线。'
  if(!state.value.agent?.ready)return state.value.agent?.fault||'等待机器人停稳或相机恢复。'
  if(!verified.value)return '这个结构还未通过现场实物验收。'
  if(!state.value.agent.slots_ready)return state.value.agent.source_note||'固定三料位及视觉核验尚未验收，启动保持关闭。'
  if((state.value.agent.sources?.length||0)!==3)return '相机需稳定识别到三个完整顶面。'
  if((state.value.agent.source_age??1e9)>.5)return '正在更新积木位置，请保持三块不动。'
  if(!checked.value)return '请勾选下方现场就绪确认。'
  return '已就绪，可以开始自动逐块取料。'
})
const waiting=computed(()=>['idle','awaiting_feed','awaiting_support','completed'].includes(state.value?.phase||''))
const action=computed(()=>({idle:'start',awaiting_feed:'feed',awaiting_support:'support',completed:'reset'}[state.value?.phase||'']||''))
const caption=computed(()=>({idle:'开始搭建所选结构',awaiting_feed:`第 ${(state.value?.layer||0)+1} 块已摆好，开始取放`,awaiting_support:'确认已落稳，释放并继续',completed:'场地已清空，准备新一轮',stopped:'动作已锁定，等待现场检查'}[state.value?.phase||'']||'机械臂正在执行'))
const instruction=computed(()=>({idle:'三块一次放到已验收的三个料位标记内，塔基留空；网页启动后逐块试提核验、搬运和放置。相机与底盘固定。',
  awaiting_feed:'在侧面固定送料区、抬高且停稳的吸盘正下方放一块积木，对准顶面中心，然后撤手。其他积木避开本轮路径即可。',
  awaiting_support:'只从侧面观察：积木底面已由地面或下层平稳支撑，没有倾斜、挤压，才确认释放。',
  preparing:'正在核对固定料位与取放路径，请保持积木不动、手在活动范围外。',
  building:'正在吸取、抬升、搬运和分层放置。请保持手在活动范围外。',
  releasing:'正在关泵并退出，随后核验放置结果。',completed:'三块抓取与放置均已核验。请结合现场检查稳定性，再导出运行记录。',
  stopped:'停止已锁定，吸力保持。先处理负载并检查现场；执行端需检查后重启，页面不会自动续动。'}[state.value?.phase||'']||'正在连接执行端。'))
function apply(next:State){
  if(state.value?.session!==next.session||state.value?.phase!==next.phase) checked.value=false
  if(next.phase!=='idle') selected.value=next.structure||({1:'single',2:'tower2',3:'tower3'}[next.levels]||'tower3')
  state.value=next
}
async function sync(){
  const started=performance.now(), current=epoch
  try{
    const next=await request<State>('/api/fixed-tower/state',{timeout:800})
    if(disposed||current!==epoch)return
    if(performance.now()-started>900)throw new Error('状态响应过期')
    apply(next);fresh.value=true
    clearTimeout(expiry);expiry=window.setTimeout(()=>{fresh.value=false;checked.value=false},900)
    if(next.camera_fresh)frame.value=`/api/fixed-tower/frame.jpg?s=${next.frame_sequence}`
    else frame.value=''
    if(token.value)await request('/api/fixed-tower/heartbeat',{body:{action:'start',session:next.session,revision:next.revision,operator},token:token.value,timeout:800})
  }catch(e){fresh.value=false;error.value=(e as Error).message}
  finally{if(!disposed)timer=window.setTimeout(sync,200)}
}
async function act(name:string){
  if(!state.value)return
  if(name!=='stop'&&(!fresh.value||busy.value))return
  busy.value=true;error.value='';const operation=++epoch
  try{
    saveToken()
    const next=await request<State>('/api/fixed-tower/control',{body:{action:name,session:state.value.session,revision:state.value.revision,operator,checked:checked.value,levels:levels.value,structure:selected.value,automatic:true},token:token.value})
    if(operation===epoch){apply(next);checked.value=false}
  }catch(e){if(operation===epoch)error.value=(e as Error).message}
  finally{if(operation===epoch)busy.value=false}
}
onMounted(sync)
onBeforeUnmount(()=>{disposed=true;clearTimeout(timer);clearTimeout(expiry)})
</script>

<template>
  <main class="fixed-app">
    <header><div><small>SPARK · 固定工作区</small><h1>三块摆好，一键搭塔</h1><p>固定三料位 · 试提与放置核验 · 异常停止</p></div><button class="stop" @click="act('stop')">紧急停止</button></header>
    <div v-if="error||state?.error" class="notice error" role="alert">{{ error||state?.error }}</div>
    <section class="fixed-grid">
      <article class="panel"><div class="heading"><h2>当前任务</h2><span>{{ state?.placed||0 }} / {{ state?.phase==='idle'?levels:state?.levels||3 }} 块已核验放置</span></div>
        <fieldset :disabled="state?.phase!=='idle'||busy" class="structures"><legend>选择固定结构</legend><label v-for="choice in choices" :key="choice.id"><input v-model="selected" type="radio" :value="choice.id" name="structure" @change="checked=false" />{{ choice.name }}</label></fieldset>
        <div v-if="selected==='pyramid3'" class="pyramid-preview" aria-label="两底一顶预览"><div class="pyramid-top" :class="{done:(state?.placed||0)>2}">第 3 块 · 跨缝</div><div class="pyramid-bottom"><span :class="{done:(state?.placed||0)>0}">第 1 块</span><span :class="{done:(state?.placed||0)>1}">第 2 块</span></div></div>
        <div v-else class="tower-preview" aria-label="所选结构预览"><div v-for="level in Array.from({length:state?.phase==='idle'?levels:state?.levels||3},(_,i)=>i).reverse()" :key="level" :class="{done:(state?.placed||0)>level}">{{ level+1 }} 层</div></div>
        <p v-if="selected==='pyramid3'" class="instruction">底层两块并排接近，上层一块居中跨在两块底块上。先放稳两块底块，再放顶块。</p>
        <ol v-if="plan" class="plan-steps" aria-label="所选结构放置顺序"><li v-for="step in plan.steps" :key="step.index">{{ step.label }}<span v-if="step.supports.length"> · 支撑：第 {{ step.supports.map(i=>i+1).join('、') }} 块</span></li></ol>
        <p v-if="plan?.ik_audit" class="instruction" role="status">{{ plan.ik_audit.passed?'固件只读可达检查通过':'固件只读可达检查未通过' }} · {{ plan.ik_audit.checked_points }} 个采样点。{{ plan.ik_audit.matches_current?'标定文件与当前执行端一致。':'当前标定一致性尚待确认。' }}仍需现场验证接触、避碰与稳定性。</p>
        <p v-if="plan?.ik_audit?.payload_geometry_passed" class="hint">积木本体的名义支撑与路径余量检查通过；机械臂连杆和真实障碍物尚未完成避碰验收。</p>
        <p v-if="!verified" class="instruction" role="status">此结构可选择和预览，尚待现场标定与实机验收，实物启动保持关闭。</p>
        <h2 role="status">{{ state?.label||'正在连接…' }}</h2><p class="instruction">{{ instruction }}</p>
        <p v-if="state?.agent_fresh" class="muted">执行端：{{ state.agent?.progress||'等待任务' }}</p>
        <div class="recognition-status"><p class="instruction">自动取料：{{ state?.agent?.sources?.length||0 }} / 3 块稳定识别。{{ state?.agent?.source_note }}</p>
        <p role="status" class="instruction start-reason">{{ startReason||' ' }}</p></div>
        <p v-if="state?.agent?.fault" class="instruction" role="status">{{ state.agent.fault }}</p>
        <label v-if="waiting" class="check"><input v-model="checked" type="checkbox" />{{ state?.phase==='awaiting_support'?'我确认本层已受稳定支撑，可以关泵':state?.phase==='awaiting_feed'?'本块已摆好，手已移开，相机和底块未移动':'旧塔已清空，取放区域就绪，相机固定，现场可立即停止' }}</label>
        <button class="primary execute" :disabled="!action||!checked||busy||!fresh||!state?.agent_fresh||!state?.agent?.ready||!token||!verified||(state?.phase==='idle'&&(!state?.agent?.slots_ready||(state?.agent?.sources?.length||0)!==3||(state?.agent?.source_age??1e9)>.5))" @click="act(action)">{{ caption }}</button>
        <p v-if="!fresh||!state?.agent_fresh" class="hint">等待新鲜的网页与机器人状态，启动暂不可用。</p>
      </article>
      <article class="panel"><div class="heading"><h2>实时现场</h2><span>{{ fresh&&state?.camera_fresh?'实时相机':'等待新画面' }}</span></div>
        <img v-if="frame&&fresh" class="camera" :src="frame" alt="机器人实时工作区，固定取料区和三层塔" />
        <div v-else class="camera empty">等待机器人相机画面</div>
        <p class="instruction">请保持网页打开。抓取核验通过后才横移，释放退出并核验放置后才计数；异常会停止，已开启的吸力保持。几何到位不代表接触传感器确认。</p>
        <details><summary>控制口令</summary><label class="token">口令<input v-model="token" type="password" autocomplete="off" @change="saveToken" /></label></details>
        <div class="links"><a href="/api/fixed-tower/report" download>导出本轮运行记录</a><a href="/?layout=1">布局设计与演示</a></div>
      </article>
    </section>
    <section class="panel history"><h2>任务记录</h2><ol><li v-for="(event,i) in state?.events||[]" :key="i"><time>{{ event.time }}</time>{{ event.message }}</li></ol></section>
  </main>
</template>

<style scoped>
.recognition-status{min-height:156px}.start-reason{min-height:3.6em}
.structures{border:1px solid #e3e8ef;border-radius:10px;margin:18px 0 0;padding:12px;display:flex;gap:12px;flex-wrap:wrap}.structures label{display:flex;align-items:center;gap:6px;font-size:14px}.structures legend{font-size:13px;color:#64748b}.pyramid-preview{height:220px;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#f6f8fc;border-radius:12px;margin:20px 0}.pyramid-top,.pyramid-bottom span{height:60px;width:100px;border:2px solid #8dabf2;background:#d8e4ff;display:grid;place-items:center;color:#255bdf;font-size:13px;border-radius:4px}.pyramid-bottom{display:flex;gap:2px}.pyramid-preview .done{background:#255bdf;color:white}

.fixed-app{max-width:1240px;margin:auto;padding:32px 24px}.fixed-app header,.heading{display:flex;justify-content:space-between;gap:20px;align-items:center}.fixed-app header{margin-bottom:24px}header small{color:#255bdf;letter-spacing:2px}header p{margin-top:12px;color:#64748b}.stop{color:white;background:#bd2639;border-color:#bd2639;font-weight:700;min-height:48px}.fixed-grid{display:grid;grid-template-columns:1fr 1.25fr;gap:22px}.panel{background:white;border:1px solid #e3e8ef;border-radius:16px;padding:24px}.heading span,.muted{font-size:13px;color:#64748b}.tower-preview{height:220px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;background:#f6f8fc;border-radius:12px;margin:20px 0}.tower-preview div{width:104px;height:52px;background:#d8e4ff;border:2px solid #8dabf2;border-radius:5px;text-align:center;line-height:48px;color:#255bdf;box-shadow:8px 6px 0 #aec4f4}.tower-preview div.done{background:#255bdf;color:white;border-color:#255bdf;box-shadow:8px 6px 0 #163b98}.instruction{line-height:1.8;margin:14px 0;color:#475569}.check{display:flex;gap:10px;align-items:flex-start;line-height:1.7;margin:20px 0}.check input{flex-shrink:0;margin-top:5px}.execute{width:100%;min-height:48px}.camera{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#edf1f7;border-radius:10px;margin-top:20px}.camera.empty{display:grid;place-items:center;color:#64748b}.links{display:flex;justify-content:space-between;gap:20px;margin-top:22px;color:#255bdf;font-size:14px}.token{display:flex;gap:10px;align-items:center;margin-top:10px}.token input{flex:1}.history{margin-top:22px}.history ol{list-style:none;padding:0;margin-bottom:0}.history li{padding:10px 0;border-bottom:1px solid #f0f2f6;font-size:14px}.history time{color:#64748b;margin-right:18px}details summary{cursor:pointer}.notice{margin-bottom:18px}@media(max-width:760px){.fixed-grid{grid-template-columns:1fr}.fixed-app{padding:20px 12px}h1{font-size:25px}.panel{padding:18px}.fixed-app header{align-items:flex-start}.stop{white-space:nowrap}}
</style>
