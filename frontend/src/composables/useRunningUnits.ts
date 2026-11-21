/**
 * 沉浸模式的「运行单元」聚合（只读，不新增任何后端轮询）
 *
 * 数据全部来自既有 store：
 *   convs.streamingConvIds      正在流式的会话集合
 *   convs._runAgents[cid]       该会话正在流式运行的 agentId 集合
 *   convs._speakingAgents[cid]  该会话并行发言集合
 *   convs._toolCalls[cid][aid]  能力调用阶段（args / exec）
 *   convs._toolPending[cid][aid] 未返回结果的工具数（长工具执行中）
 *   convs.running/runCanvasId   画布集群运行（等待/定时触发）
 *   conv.list[cid].messages     输出正文与流式标记
 *
 * 两个消费者（ImmersiveChrome / RunPeekPanel）共用本 composable，
 * 保证「状态徽标」与「输出浮窗」看到的是同一份运行单元。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useAgentStore, useCanvasStore, useConvStore } from '@/stores'
import { toolLabel } from '@/stores/conversation'
import { agentAvatarGradient } from '@/utils/avatar'

export interface RunUnit {
  key: string
  convId: number
  convName: string
  agentId: number
  agentName: string
  avatar: string
  /** 该智能体最近一条有正文的 assistant 内容（流式中则持续追加） */
  content: string
  /** 是否仍在流式输出（控制光标） */
  streaming: boolean
  /** 状态文案：正在执行 xxx / 执行中 / 正在输出 / 运行中 */
  activity: string
  /** 是否有能力调用在执行（状态文案高亮） */
  inTool: boolean
  startedAt: number
  elapsed: number
}

/** key → 首次被观测到的时间。模块级共享：徽标与浮窗看到的时长一致。 */
const firstSeen = new Map<string, number>()

export function useRunningUnits() {
  const convs = useConvStore()
  const agents = useAgentStore()
  const canvases = useCanvasStore()

  // 每秒 tick 一次，只驱动「已运行时长」文本更新
  const now = ref(Date.now())
  let timer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    timer = setInterval(() => { now.value = Date.now() }, 1000)
  })
  onBeforeUnmount(() => {
    if (timer) clearInterval(timer)
    timer = null
  })

  function msgsOf(cid: number): any[] {
    const c: any = convs.list.find((x: any) => x.id === cid)
    return Array.isArray(c?.messages) ? c!.messages : []
  }

  function pendingOf(cid: number, aid: number): number {
    return ((convs as any)._toolPending?.[cid] ?? {})[aid] ?? 0
  }

  function toolOf(cid: number, aid: number): any {
    return ((convs as any)._toolCalls?.[cid] ?? {})[aid] || null
  }

  /** 与 agentStreaming getter 同语义：工具执行窗口也算运行中，按会话独立判断 */
  function agentRunning(cid: number, aid: number): boolean {
    if (pendingOf(cid, aid) > 0) return true
    const msgs = msgsOf(cid)
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m: any = msgs[i]
      if (m?.role === 'assistant' && m.streaming === true && m.agent_id === aid) return true
    }
    return false
  }

  /** 该智能体最近的输出正文（优先流式中的那条） */
  function latestContent(cid: number, aid: number): { content: string; streaming: boolean } {
    const msgs = msgsOf(cid)
    let fallback: any = null
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m: any = msgs[i]
      if (m?.role !== 'assistant' || m.agent_id !== aid) continue
      if (m.streaming === true) return { content: String(m.content ?? ''), streaming: true }
      if (!fallback && String(m.content ?? '').trim()) fallback = m
    }
    return { content: String(fallback?.content ?? ''), streaming: false }
  }

  const units = computed<RunUnit[]>(() => {
    const cids = new Set<number>()
    convs.streamingConvIds.forEach((cid: number) => cids.add(cid))
    const ra = (convs as any)._runAgents as Record<string, Set<number>> | undefined
    if (ra) Object.keys(ra).forEach(k => cids.add(Number(k)))
    const sa = (convs as any)._speakingAgents as Record<string, Set<number>> | undefined
    if (sa) Object.keys(sa).forEach(k => cids.add(Number(k)))
    if (convs.current != null) cids.add(convs.current)

    const out: RunUnit[] = []
    for (const cid of cids) {
      if (!Number.isFinite(cid)) continue
      const aids = new Set<number>()
      const raSet = ra?.[cid]
      if (raSet) raSet.forEach((a: number) => aids.add(a))
      const saSet = sa?.[cid]
      if (saSet) saSet.forEach((a: number) => aids.add(a))
      // 兜底：消息里带 streaming 标记的智能体（接力/并行时集合可能滞后）
      for (const m of msgsOf(cid)) {
        if (m?.role === 'assistant' && m.streaming === true && m.agent_id != null) aids.add(m.agent_id)
      }
      for (const aid of aids) {
        if (aid == null || !agentRunning(cid, aid)) continue
        const key = `c:${cid}:${aid}`
        if (!firstSeen.has(key)) firstSeen.set(key, Date.now())
        const t0 = firstSeen.get(key)!
        const agent: any = agents.list.find((a: any) => a.id === aid)
        const conv: any = convs.list.find((x: any) => x.id === cid)
        const { content, streaming } = latestContent(cid, aid)
        const tc = toolOf(cid, aid)
        const pending = pendingOf(cid, aid)
        const label = tc?.name ? toolLabel(tc.name) : ''
        const activity = tc
          ? (tc.phase === 'args' ? `正在接收参数 · ${label}` : `正在执行 · ${label}`)
          : pending > 0 ? '执行中' : streaming ? '正在输出' : '运行中'
        out.push({
          key,
          convId: cid,
          convName: conv?.name || `会话 ${cid}`,
          agentId: aid,
          agentName: agent?.name || `智能体 ${aid}`,
          avatar: agentAvatarGradient(agent || { id: aid }),
          content,
          streaming,
          activity,
          inTool: !!tc || pending > 0,
          startedAt: t0,
          elapsed: Math.max(0, Math.round((now.value - t0) / 1000)),
        })
      }
    }
    // 早开始的排前面，避免 tab 在运行中来回跳位
    out.sort((a, b) => a.startedAt - b.startedAt)
    // 运行结束就丢掉起始时间：同一智能体下次重新运行时从 0 计时，而不是累计上次的时长
    const present = new Set(out.map(u => u.key))
    for (const k of firstSeen.keys()) {
      if (!present.has(k)) firstSeen.delete(k)
    }
    return out
  })

  /** 画布集群运行态（等待/定时触发中） */
  const canvasWaiting = computed<{ id: number; name: string } | null>(() => {
    if (!convs.running || convs.runCanvasId == null) return null
    const cv: any = canvases.list.find((c: any) => c.id === convs.runCanvasId)
    return { id: convs.runCanvasId, name: cv?.name || `画布 ${convs.runCanvasId}` }
  })

  const hasWork = computed(() => units.value.length > 0 || !!canvasWaiting.value)

  const summary = computed(() => {
    const n = units.value.length
    const c = canvasWaiting.value
    if (n > 0 && c) return `${n} 个智能体运行中 · 画布「${c.name}」待触发`
    if (n > 0) return `${n} 个智能体运行中`
    if (c) return `画布「${c.name}」等待触发`
    return ''
  })

  return { units, canvasWaiting, hasWork, summary, now }
}

/** 秒 → 12s / 1m32s */
export function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  if (m < 60) return `${m}m${s.toString().padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${(m % 60).toString().padStart(2, '0')}m`
}
