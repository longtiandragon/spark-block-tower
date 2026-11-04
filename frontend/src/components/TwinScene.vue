<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as THREE from 'three'
import type { GameState } from '../types'

const props = defineProps<{ state: GameState | null; online: boolean; active: boolean }>()
const emit = defineEmits<{ select: [id: number] }>()
const host = ref<HTMLDivElement | null>(null), failure = ref('')
const label = computed(() => !props.online ? '连接已中断 · 最后状态' : props.state?.mode === 'ros' ? (props.state.robot.connected ? 'ROS 朝向反馈' : '等待 ROS 朝向') : '模拟场景 · 非实机位置')
let renderer: THREE.WebGLRenderer | undefined, scene: THREE.Scene, camera: THREE.PerspectiveCamera
let robot: THREE.Group, vision: THREE.Mesh, lamp: THREE.Mesh, players: THREE.Group[] = []
let resize: ResizeObserver | undefined, frame = 0, last = 0, yaw = 0, reduced = false
const resources: (THREE.BufferGeometry | THREE.Material)[] = []
function mesh(geometry: THREE.BufferGeometry, color: string, x: number, y: number, z: number, parent: THREE.Object3D, opacity = 1) {
  const material = new THREE.MeshStandardMaterial({ color, roughness: .58, metalness: .15, transparent: opacity < 1, opacity })
  const object = new THREE.Mesh(geometry, material)
  object.position.set(x, y, z); parent.add(object); resources.push(geometry, material)
  return object
}
function choose(event: PointerEvent) {
  if (!renderer) return
  const rect = renderer.domElement.getBoundingClientRect(), ray = new THREE.Raycaster()
  ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left)/rect.width*2-1, -(event.clientY-rect.top)/rect.height*2+1), camera)
  for (const hit of ray.intersectObjects(players, true)) {
    let object: THREE.Object3D | null = hit.object
    while (object && !object.userData.playerId) object = object.parent
    if (object?.userData.playerId) { emit('select', object.userData.playerId); break }
  }
}
function draw(now: number) {
  frame = requestAnimationFrame(draw)
  if (now-last < 32 || document.hidden || !props.active || !renderer) return
  const dt = Math.min(.1, (now-last)/1000); last = now
  const state = props.state
  if (state) {
    const physical = state.mode === 'ros'
    const target = physical ? -(state.robot.yaw-(state.face_yaw ?? 0)) : ['turning_away','green'].includes(state.phase) ? Math.PI : 0
    if (props.online && (!physical || state.robot.connected)) {
      const delta = Math.atan2(Math.sin(target-yaw), Math.cos(target-yaw))
      yaw += reduced ? delta : delta * Math.min(1, dt*7)
    }
    robot.rotation.y = yaw
    const red = ['turning_back','settling','red','estop'].includes(state.phase)
    const color = !props.online ? '#74817e' : red ? '#ee765d' : state.phase === 'green' ? '#a7e3b1' : '#dfc391'
    ;(lamp.material as THREE.MeshStandardMaterial).color.set(color)
    ;(vision.material as THREE.MeshStandardMaterial).color.set(color)
    players.forEach((group,i) => {
      const player = state.players[i]; group.visible = !!player
      if (!player) return
      const c = player.status === 'winner' ? '#eec76e' : player.status === 'eliminated' ? '#63716d' : player.status === 'pending' ? '#ee765d' : '#b8e2ce'
      group.children.forEach(child => { if (child instanceof THREE.Mesh) (child.material as THREE.MeshStandardMaterial).color.set(c) })
      group.scale.setScalar(player.visible ? 1 : .75)
    })
  }
  renderer.render(scene,camera)
}
onMounted(() => {
  if (!host.value) return
  try {
    reduced = matchMedia('(prefers-reduced-motion: reduce)').matches
    scene = new THREE.Scene(); scene.background = new THREE.Color('#15243b')
    camera = new THREE.PerspectiveCamera(43,1,.1,80); camera.position.set(7.3,7.8,10.4); camera.lookAt(0,0,0)
    renderer = new THREE.WebGLRenderer({ antialias:true, alpha:false }); renderer.setPixelRatio(Math.min(devicePixelRatio,2))
    renderer.domElement.setAttribute('aria-label','SPARK 比赛三维状态示意，玩家位于固定赛道，非真实空间定位')
    host.value.appendChild(renderer.domElement)
    renderer.domElement.addEventListener('pointerdown',choose)
    renderer.domElement.addEventListener('webglcontextlost',event => { event.preventDefault(); failure.value='三维画面暂不可用，下方实时状态仍可查看'; cancelAnimationFrame(frame) })
    scene.add(new THREE.HemisphereLight('#edf5ff','#26354e',2.3))
    const light = new THREE.DirectionalLight('#fff1cc',3); light.position.set(4,8,4); scene.add(light)
    mesh(new THREE.BoxGeometry(9,.12,9),'#243a56',0,-.12,0,scene)
    for (let x=-4;x<=4;x++) mesh(new THREE.BoxGeometry(.015,.01,8),'#3e5672',x,0,0,scene)
    for (let z=-4;z<=4;z++) mesh(new THREE.BoxGeometry(8,.01,.015),'#3e5672',0,0,z,scene)
    for(let i=0;i<4;i++) {
      const x=(i-1.5)*1.65
      mesh(new THREE.BoxGeometry(1.48,.02,4.9),'#375774',x,.01,1,scene)
      mesh(new THREE.BoxGeometry(1.48,.025,.06),'#b6c497',x,.03,3.45,scene)
      const group = new THREE.Group(); group.position.set(x,.04,2.2); group.userData.playerId=i+1
      scene.add(group); players.push(group)
      mesh(new THREE.CylinderGeometry(.18,.25,.7,16),'#b8e2ce',0,.55,0,group)
      mesh(new THREE.SphereGeometry(.21,16,12),'#b8e2ce',0,1.08,0,group)
      mesh(new THREE.CylinderGeometry(.35,.35,.04,24),'#b8e2ce',0,.03,0,group)
    }
    robot = new THREE.Group(); robot.position.set(0,0,-2.6); scene.add(robot)
    mesh(new THREE.CylinderGeometry(.55,.65,.28,32),'#e9e7d8',0,.28,0,robot)
    mesh(new THREE.BoxGeometry(.58,.8,.48),'#d8dbd0',0,.82,0,robot)
    mesh(new THREE.BoxGeometry(.72,.28,.4),'#d8dbd0',0,1.36,0,robot)
    mesh(new THREE.BoxGeometry(.52,.13,.05),'#152f2a',0,1.39,.225,robot)
    for (const x of [-.62,.62]) { const wheel=mesh(new THREE.CylinderGeometry(.25,.25,.12,20),'#101f1b',x,.24,0,robot); wheel.rotation.z=Math.PI/2 }
    lamp=mesh(new THREE.CylinderGeometry(.13,.13,.07,24),'#dfc391',0,1.55,0,robot)
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position',new THREE.Float32BufferAttribute([0,.035,.3,-2.1,.035,4.5,2.1,.035,4.5],3)); geometry.computeVertexNormals()
    vision=mesh(geometry,'#dfc391',0,0,0,robot,.17); (vision.material as THREE.Material).side=THREE.DoubleSide
    resize=new ResizeObserver(() => { if (!host.value || !renderer) return; const {width,height}=host.value.getBoundingClientRect(); if(width<=0 || height<=0)return;renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix() })
    resize.observe(host.value); frame=requestAnimationFrame(draw)
  } catch { failure.value='当前浏览器无法显示三维画面，下方实时状态仍可查看' }
})
onBeforeUnmount(() => { cancelAnimationFrame(frame);resize?.disconnect();renderer?.domElement.removeEventListener('pointerdown',choose);resources.forEach(x=>x.dispose());renderer?.dispose();renderer?.domElement.remove() })
</script>

<template>
  <div class="twin-visual"><div ref="host" class="three-host"></div><div class="twin-overlay"><span class="twin-live-dot"></span>{{ label }}</div><div v-if="failure" class="twin-failure">{{ failure }}</div><span class="twin-scale">朝向示意 · 视锥未标定 · 玩家位置仅代表赛道</span></div>
</template>

<style scoped>
.twin-visual{position:relative;background:#15243b;overflow:hidden;border-radius:10px}.three-host{height:470px;width:100%;cursor:pointer}.three-host :deep(canvas){display:block}.twin-overlay{position:absolute;top:18px;left:20px;font-size:12px;color:#dbe8f7;display:flex;align-items:center;gap:8px;pointer-events:none}.twin-live-dot{width:6px;height:6px;border-radius:50%;background:#d6f17f}.twin-scale{position:absolute;bottom:14px;left:20px;color:#bbcce2;font-size:11px;pointer-events:none}.twin-failure{position:absolute;inset:0;display:grid;place-content:center;color:#e3e6d8;padding:40px}.twin-overlay,.twin-scale{z-index:1}@media(max-width:600px){.three-host{height:290px}.twin-scale{font-size:10px}}
</style>
