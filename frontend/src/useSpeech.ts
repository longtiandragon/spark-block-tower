import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { ApiError, request } from './api'
import type { GameState, Speech } from './types'

interface Clip { url: string; duration: number; text: string }
interface Catalog { ready: boolean; error: string | null; clips: Record<string, Clip> }

export function useSpeech(state: Ref<GameState | null>, online: Ref<boolean>, token: Ref<string>) {
  const unlocked = ref(false), error = ref(''), catalog = ref<Catalog | null>(null)
  const owner = globalThis.crypto?.randomUUID?.() ?? `browser-${Date.now()}-${Math.random().toString(16).slice(2)}`
  let context: AudioContext | null = null, source: AudioBufferSourceNode | null = null
  let generation = 0, disposed = false
  const buffers = new Map<string, AudioBuffer>()
  const attempted = new Set<string>()
  function cancel() { generation++; if (source) { source.onended = null; source.stop(); source.disconnect(); source = null } }
  async function unlock() {
    try {
      context ??= new AudioContext()
      await context.resume()
      if (context.state !== 'running') throw new Error('请允许浏览器播放声音，再点击启用。')
      unlocked.value = true
      error.value = ''
      catalog.value = await request<Catalog>('/api/speech/catalog')
      if (!catalog.value.ready) throw new Error(catalog.value.error ?? '口令资源尚未准备好')
      await sync()
    } catch (e) { error.value = e instanceof Error ? e.message : '音频初始化失败' }
  }
  function ack(speech: Speech, status: string, detail?: string) {
    return request('/api/speech/ack', { token: token.value, timeout: 1500,
      body: { speech_id: speech.id, session_id: speech.session_id, status, owner, error: detail } })
  }
  async function sync() {
    cancel()
    const speech = state.value?.speech
    if (!speech || speech.target !== 'browser' || !online.value || !unlocked.value || disposed || attempted.has(speech.id)) return
    if (speech.status !== 'pending') return
    attempted.add(speech.id)
    if (attempted.size > 500) { const latest = speech.id; attempted.clear(); attempted.add(latest) }
    const stamp = generation
    const current = () => stamp === generation && online.value && !disposed && state.value?.speech?.id === speech.id
    let claimed = false
    try {
      await ack(speech, 'claimed')
      claimed = true
      if (!current()) return
      catalog.value ??= await request<Catalog>('/api/speech/catalog')
      const clip = catalog.value.clips[speech.clip]
      if (!clip || !context) throw new Error('缺少对应口令文件')
      let buffer = buffers.get(clip.url)
      if (!buffer) {
        const controller = new AbortController(), timer = window.setTimeout(() => controller.abort(), 5000)
        try {
          const response = await fetch(clip.url, { signal: controller.signal })
          if (!response.ok) throw new Error('下载口令失败')
          buffer = await context.decodeAudioData(await response.arrayBuffer())
          buffers.set(clip.url, buffer)
        } finally { clearTimeout(timer) }
      }
      if (!current()) return
      if (context.state !== 'running') throw new Error('浏览器声音已挂起，请重新启用声音')
      await ack(speech, 'playing')
      if (!current()) return
      const node = context.createBufferSource()
      node.buffer = buffer
      node.connect(context.destination)
      source = node
      node.onended = async () => {
        node.disconnect()
        if (source === node) source = null
        if (!current()) return
        try { await ack(speech, 'done') }
        catch { if (current()) { try { await ack(speech, 'done') } catch { error.value = '播放完成回执未送达，比赛将等待或暂停' } } }
      }
      node.start()
    } catch (e) {
      if (!current()) return
      // Losing the claim to another enabled tab is normal, not a speaker failure.
      if (!claimed && e instanceof ApiError && e.status === 409) return
      error.value = e instanceof Error ? e.message : '播放失败'
      if (claimed) { try { await ack(speech, 'failed', '浏览器播放失败，请检查声音权限与音频设备') } catch { /* timeout will pause */ } }
    }
  }
  // Status acknowledgements do not restart/cancel an already-playing clip.
  // A getter returning a fresh array fires on every poll, even for the same id.
  // Watch each primitive independently so a state refresh cannot cut speech off.
  watch([() => state.value?.speech?.id, () => online.value], sync)
  watch(() => state.value?.speech?.status, status => { if (status === 'failed') cancel() })
  onBeforeUnmount(() => { disposed = true; cancel(); void context?.close() })
  return { unlocked, error, unlock }
}
