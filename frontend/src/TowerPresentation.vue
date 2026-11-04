<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
type Block={id:number;x:number;y:number;width:number;height:number;u:number;v:number}
type Scene={image:string;source:string;blocks:Block[];simulation:true;hardware_motion:false}
const scene=ref<Scene|null>(null), elapsed=ref(0), running=ref(false), error=ref(''), ready=ref(false)
let raf=0, origin=0, disposed=false
const period=6000, intro=1800
const completed=computed(()=>Math.min(3,Math.max(0,Math.floor((elapsed.value-intro)/period))))
const active=computed(()=>Math.min(2,completed.value))
const progress=computed(()=>Math.max(0,Math.min(1,(elapsed.value-intro-active.value*period)/period)))
const stage=computed(()=>elapsed.value<intro?'识别三个蓝色积木':completed.value===3?'三层搭建完成':progress.value<.17?'定位并抓取':progress.value<.4?'垂直抬升':progress.value<.7?'移动至塔基':progress.value<.9?'下降并放置':'释放 · 退出')
const ease=(p:number)=>{const t=Math.max(0,Math.min(1,p));return t*t*(3-2*t)}
const starts=[{x:145,y:335},{x:255,y:300},{x:340,y:365}]
function cube(i:number){
  const start=starts[i]!, target={x:590,y:380-i*66}
  if(i<completed.value)return target
  if(i>active.value||elapsed.value<intro)return start
  const t=progress.value
  if(t<.17)return start
  if(t<.4)return {x:start.x,y:start.y+(170-start.y)*ease((t-.17)/.23)}
  if(t<.7)return {x:start.x+(target.x-start.x)*ease((t-.4)/.3),y:170}
  return {x:target.x,y:170+(target.y-170)*ease((t-.7)/.2)}
}
const tool=computed(()=>{if(completed.value===3)return {x:680,y:140};const c=cube(active.value);return {x:c.x,y:c.y-77}})
const elbow=computed(()=>({x:440+(tool.value.x-440)*.45,y:Math.min(tool.value.y-90,130)}))
function tick(now:number){elapsed.value=Math.min(intro+3*period,now-origin);if(elapsed.value<intro+3*period&&running.value)raf=requestAnimationFrame(tick);else running.value=false}
async function start(){
  cancelAnimationFrame(raf);running.value=false;elapsed.value=0;error.value=''
  try{const response=await fetch('/api/fixed-tower/simulation-scene',{cache:'no-store'});if(!response.ok)throw Error('现场识别暂未就绪');const next=await response.json() as Scene;if(disposed)return;scene.value=next;ready.value=true;running.value=true;origin=performance.now();raf=requestAnimationFrame(tick)}catch(e){error.value=(e as Error).message}
}
function pause(){if(running.value){running.value=false;cancelAnimationFrame(raf)}else if(completed.value<3){running.value=true;origin=performance.now()-elapsed.value;raf=requestAnimationFrame(tick)}}
onMounted(start)
onBeforeUnmount(()=>{disposed=true;cancelAnimationFrame(raf)})
</script>

<template>
  <main class="presentation">
    <header><div><span class="eyebrow">SPARK / BLUE BLOCKS</span><h1>看见积木，搭起三层。</h1></div><div class="status-dot"><i/>搭建演示</div></header>
    <div class="presentation-grid">
      <section class="vision-pane"><div class="pane-heading"><span>01 / 视觉输入</span><small>{{scene?.source||'正在连接相机'}}</small></div>
        <div class="snapshot"><img v-if="scene" :src="scene.image" alt="真实现场积木识别快照"/><svg v-if="scene" viewBox="0 0 640 480" aria-label="三个积木识别框"><g v-for="block in scene.blocks" :key="block.id"><rect :x="block.x-5" :y="block.y-5" :width="block.width+10" :height="block.height+10" rx="5"/><circle :cx="block.u" :cy="block.v" r="5"/><text :x="block.x" :y="block.y-12">BLUE / 0{{block.id}}</text></g></svg></div>
        <div class="observation"><strong>{{scene?.blocks.length||0}}<small> / 3</small></strong><div>蓝色积木已定位<p>视觉输入 → 取放顺序 → 三层直塔</p></div></div>
        <ol class="block-list"><li v-for="i in 3" :key="i"><span>0{{i}}</span><b>蓝色积木 {{i}}</b><em>{{i<=completed?'已放置':i===active+1&&ready?'处理中':'等待取料'}}</em></li></ol>
      </section>
      <section class="build-pane"><div class="pane-heading"><span>02 / 三层搭建</span><small>{{completed}} / 3 块</small></div>
        <svg class="build-scene" viewBox="0 0 800 500" role="img" aria-label="三块积木依次抬升并搭成三层塔的动画">
          <defs><pattern id="grid" width="40" height="24" patternUnits="userSpaceOnUse" patternTransform="skewX(-25)"><path d="M40 0H0V24" fill="none" stroke="#dfe5eb" stroke-width="1"/></pattern><filter id="shadow"><feGaussianBlur stdDeviation="7"/></filter></defs>
          <path d="M25 270L550 230L790 425L220 490Z" fill="#f1f4f7"/><path d="M25 270L550 230L790 425L220 490Z" fill="url(#grid)"/>
          <ellipse cx="590" cy="407" rx="64" ry="15" fill="#192f5020" filter="url(#shadow)"/>
          <path d="M537 375L590 349L643 375L590 402Z" fill="none" stroke="#91a4bc" stroke-dasharray="5 5"/><text x="590" y="449" text-anchor="middle" fill="#8a99ad" font-size="13">三层目标位置</text>
          <g opacity=".7"><ellipse cx="440" cy="192" rx="37" ry="13" fill="#9aa7b6"/><rect x="423" y="135" width="34" height="55" rx="7" fill="#bdc8d4"/></g>
          <path :d="`M440 145 L${elbow.x} ${elbow.y} L${tool.x} ${tool.y}`" fill="none" stroke="#24364a" stroke-width="15" stroke-linecap="round" stroke-linejoin="round"/>
          <circle :cx="elbow.x" :cy="elbow.y" r="13" fill="#e7ecf2" stroke="#24364a" stroke-width="5"/>
          <g v-for="(_,i) in starts" :key="i" :transform="`translate(${cube(i).x},${cube(i).y})`"><ellipse cy="15" rx="43" ry="10" fill="#1d4eaa19"/><path d="M-42 -58L0 -78L42 -58L0 -38Z" fill="#80b7ff"/><path d="M-42 -58L0 -38V20L-42 0Z" fill="#397de4"/><path d="M0 -38L42 -58V0L0 20Z" fill="#2057ba"/><text x="-23" y="-14" font-size="16" font-weight="bold" fill="#e6f2ff">{{i+1}}</text></g>
          <g :transform="`translate(${tool.x},${tool.y})`"><rect x="-12" y="-7" width="24" height="12" rx="4" fill="#efab38"/><path d="M-8 5H8L13 13H-13Z" fill="#26394d"/></g>
          <g v-if="completed===3"><circle cx="690" cy="230" r="22" fill="#e0f5e9"/><path d="M680 230L687 237L701 222" fill="none" stroke="#25825a" stroke-width="4" stroke-linecap="round"/></g>
        </svg>
        <div class="progress-track"><div :style="{width:`${elapsed/(intro+period*3)*100}%`}"/></div>
        <div class="build-footer"><div><small>{{completed===3?'COMPLETE':`STEP ${Math.min(completed+1,3)} / 3`}}</small><h2 role="status">{{stage}}</h2></div><div class="controls"><button v-if="ready&&completed<3" @click="pause">{{running?'暂停':'继续'}}</button><button class="primary" @click="start">{{ready?'重新识别并播放':'识别并开始'}}</button></div></div>
        <p v-if="error" role="alert">{{error}}</p>
      </section>
    </div>
    <footer>SPARK · 视觉驱动积木搭建<span>01 识别　／　02 规划　／　03 搭建</span></footer>
  </main>
</template>

<style scoped>
.presentation{min-height:100vh;background:#f6f7f9;color:#203247;padding:36px 4vw;font-family:'Microsoft YaHei','PingFang SC',sans-serif;box-sizing:border-box}.presentation header{display:flex;align-items:center;justify-content:space-between;margin-bottom:30px}.eyebrow{font:11px Consolas,monospace;letter-spacing:2px;color:#6d819b}h1{font-size:32px;margin:10px 0 0;letter-spacing:1px}.status-dot{display:flex;align-items:center;gap:9px;font-size:13px}.status-dot i{width:7px;height:7px;background:#388867;border-radius:50%}.presentation-grid{display:grid;grid-template-columns: minmax(300px,.8fr) minmax(480px,1.4fr);gap:25px}.vision-pane,.build-pane{background:white;border:1px solid #e0e5eb;border-radius:14px;overflow:hidden}.pane-heading{display:flex;justify-content:space-between;align-items:center;padding:21px 24px;border-bottom:1px solid #edf0f3;font-size:13px;letter-spacing:1px}.pane-heading small{font-size:11px;color:#7c8a9b;letter-spacing:0}.snapshot{position:relative;margin:20px 20px 0;aspect-ratio:4/3;background:#edf0f3;border-radius:7px;overflow:hidden}.snapshot img,.snapshot svg{position:absolute;width:100%;height:100%;inset:0}.snapshot rect{fill:#84ceff12;stroke:#a6f0ff;stroke-width:2}.snapshot circle{fill:#bcf5ff}.snapshot text{fill:#c4f8ff;font:bold 13px Consolas,monospace;paint-order:stroke;stroke:#183643;stroke-width:2px}.observation{padding:18px 25px;display:flex;align-items:center;gap:22px}.observation strong{font:46px Consolas,monospace}.observation strong small{font-size:20px;color:#b0bac7}.observation div{font-size:14px}.observation p{font-size:11px;color:#8996a7;margin-bottom:0}.block-list{list-style:none;padding:0 24px 12px;margin:0}.block-list li{display:flex;align-items:center;gap:16px;border-top:1px solid #edf0f3;padding:13px 0;font-size:12px}.block-list span{font:12px Consolas;color:#8a99ad}.block-list b{font-weight:500}.block-list em{margin-left:auto;font-style:normal;color:#4f78a8}.build-scene{width:100%;display:block;min-height:350px}.progress-track{height:3px;background:#e9eef4;margin:0 28px}.progress-track div{height:100%;background:#3278dc;transition:width .1s linear}.build-footer{display:flex;align-items:center;justify-content:space-between;padding:25px 28px;gap:12px}.build-footer small{font:10px Consolas;letter-spacing:2px;color:#94a2b3}.build-footer h2{font-size:23px;margin:7px 0}.controls{display:flex;gap:10px}.controls button{border:1px solid #dce3eb;background:white;color:#4e6178;border-radius:7px;padding:11px 16px;cursor:pointer;white-space:nowrap}.controls .primary{background:#245eaa;color:white;border-color:#245eaa}.presentation>footer{display:flex;justify-content:space-between;margin-top:24px;font-size:11px;color:#98a4b2;letter-spacing:1px}@media(max-width:900px){.presentation-grid{grid-template-columns:1fr}.presentation{padding:24px 16px}h1{font-size:26px}.build-scene{min-height:0}.presentation>footer span{display:none}.build-footer{padding:20px 16px}.build-footer h2{font-size:19px}}@media(prefers-reduced-motion:reduce){.progress-track div{transition:none}}
</style>
