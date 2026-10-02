import { defineStore } from 'pinia'
import { api } from '@/api'
import { useCanvasStore } from './canvas'
import { useAgentStore } from './agent'
import { useSettingsStore } from './settings'

/**
 * 行协议转义还原：后端 yield T:/RN: 等行前，会把 payload 中的
 * \ → \\、\n → \\n、\r → \\r 转义，避免载荷内换行破坏行分隔。
 * 前端解析行后调用此函数还原。
 * @see backend/core/agent_runtime.py `_esc`
 */
function _unesc(s: string): string {
  return s.replace(/\\r/g, '\r').replace(/\\n/g, '\n').replace(/\\\\/g, '\\')
}

/** 结束末尾的流式 assistant；若它一个字都没有（不是任何工具行的锚点，因为锚点必在工具卡片之前）
 *  → 直接移除，避免留下空消息泡泡。 */
function _closeTailAssistant(msgs: RichMsg[]): void {
  const last = msgs[msgs.length - 1]
  if (!last || last.role !== 'assistant' || !last.streaming) return
  last.streaming = false
  if ((last.content || '').trim() || (last.reasoning || '').trim()) return
  msgs.pop()
}

// 集群编排命令结果 → 需要刷新的画布/智能体（保持画布不残留旧内容）
const CLUSTER_REFETCH = new Set([
  'cluster.create_agent', 'cluster.update_agent', 'cluster.delete_agent',
  'cluster.create_canvas', 'cluster.delete_canvas',
  'cluster.add_node', 'cluster.remove_node', 'cluster.connect', 'cluster.disconnect',
])

// 消息类型扩展：除了 role/user/assistant，还能有 tool_call_card / tool_result_card / a2a 信封
export interface RichMsg {
  role: 'user' | 'assistant' | 'tool_call' | 'tool_result' | 'a2a'
  content?: string
  reasoning?: string    // 深度思考模型的思考过程（<thinking>）
  streaming?: boolean
  tool_call_id?: string
  name?: string        // tool_call 的工具名
  args?: any           // tool_call 的参数
  result?: any         // tool_result 的执行结果
  envelope?: any       // a2a 消息信封（a2a-lite/0.1）
  ts?: string
  relay?: boolean      // 接力链消息（聊天区过滤，画布气泡保留）
  agent_id?: number
  from_agent_id?: number  // a2a 信封额外字段：发送方 agent_id
}

// —— 简化 cron 匹配：5 字段（分 时 日 月 周），支持 * , - / ——
function fieldMatch(field: string, value: number): boolean {
  if (!field || field === '*') return true
  for (const part of field.split(',')) {
    if (part.includes('/')) {
      const [range, stepStr] = part.split('/')
      const step = parseInt(stepStr, 10)
      if (!step || step < 1) continue
      const base = range === '*' ? 0 : parseInt(range, 10)
      if (Number.isNaN(base)) continue
      if (value >= base && (value - base) % step === 0) return true
    } else if (part.includes('-')) {
      const [a, b] = part.split('-').map(x => parseInt(x, 10))
      if (!Number.isNaN(a) && !Number.isNaN(b) && value >= a && value <= b) return true
    } else {
      if (parseInt(part, 10) === value) return true
    }
  }
  return false
}

export function cronMatch(expr: string, d: Date): boolean {
  const parts = (expr || '').trim().split(/\s+/)
  if (parts.length !== 5) return false
  const [min, hour, dom, mon, dow] = parts
  return fieldMatch(min, d.getMinutes()) && fieldMatch(hour, d.getHours()) &&
    fieldMatch(dom, d.getDate()) && fieldMatch(mon, d.getMonth() + 1) &&
    fieldMatch(dow, d.getDay())
}

/** 最后活动时间：最后一条消息 ts → created_at → epoch(id)，用于上新下旧排序 */
export function convLastTs(c: any): string {
  const msgs = c?.messages || []
  let last = ''
  for (const m of msgs) {
    if (m.ts && m.ts > last) last = m.ts
  }
  return last || c?.created_at || String(c?.id ?? 0).padStart(20, '0')
}

/** 把后端 ISO（可能无时区）转为 Date，无时区按 UTC 解释 */
function parseTs(s: string): Date | null {
  if (!s) return null
  const iso = /Z$|[+-]\d{2}:\d{2}$/.test(s) ? s : s + 'Z'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** 会话底部时间：今天 HH:MM / 今年 MM-DD HH:MM / 否则 YYYY-MM-DD HH:MM */
export function convTimeLabel(c: any): string {
  const d = parseTs(convLastTs(c))
  if (!d) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return hm
  if (d.getFullYear() === now.getFullYear()) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

/** 按最后消息时间降序（上新下旧），兜底 id 降序 */
export function sortConvsByActivity(list: any[]): any[] {
  return [...list].sort((a: any, b: any) => {
    const la = convLastTs(a), lb = convLastTs(b)
    if (la !== lb) return la < lb ? 1 : -1
    return (b.id || 0) - (a.id || 0)
  })
}

/** 会话展示名：最新一条有内容的用户/助手消息（非 relay），单行截 20 字；无消息回退 c.name */
export function convDisplayName(c: any): string {
  const msgs = c?.messages || []
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    if (m.relay) continue
    if (m.role !== 'user' && m.role !== 'assistant') continue
    const t = (m.content || '').replace(/\s+/g, ' ').trim()
    if (!t) continue
    return t.length > 20 ? t.slice(0, 20) + '…' : t
  }
  return c?.name || '新对话'
}

/** 解析 cluster.send_message 的 to_agent（id → 名称精确 → 名称包含），镜像后端 _resolve_target */
function resolveAgentId(to: any): number | null {
  if (to == null || String(to).trim() === '') return null
  const list = useAgentStore().list
  const n = Number(to)
  if (!Number.isNaN(n)) {
    const byId = list.find((a: any) => a.id === n)
    if (byId) return byId.id
  }
  const s = String(to)
  const exact = list.find((a: any) => a.name === s)
  if (exact) return exact.id
  const part = list.find((a: any) => (a.name || '').includes(s))
  return part ? part.id : null
}

// 特殊工具（不走后端 commandsMeta，如 handoff 由 A2A 协议特殊注入，仅界面展示用）
const SPECIAL_TOOL_LABELS: Record<string, string> = {
  'cluster.handoff': 'A2A 移交',
}
// 工具名 → 能力元数据（与列表卡片 / 编辑弹窗同源：后端 commandsMeta / pluginsMeta / skillsMeta）
// 带 _ / - 别名兜底，兼容 novel_writing ↔ novel-writing
function _resolveToolMeta(name: string): any {
  const s = useSettingsStore()
  let m = s.commandsMeta?.[name] || s.pluginsMeta?.[name] || s.skillsMeta?.[name]
  if (!m && name.includes('_')) {
    const a = name.replace(/_/g, '-')
    m = s.pluginsMeta?.[a] || s.skillsMeta?.[a]
  }
  if (!m && name.includes('-')) {
    const a = name.replace(/-/g, '_')
    m = s.pluginsMeta?.[a] || s.skillsMeta?.[a]
  }
  return m
}
/** 工具名 → 中文能力标签（画布气泡 / 聊天工具行 / 列表卡片共用同一份后端 label，无 emoji） */
export function toolLabel(name: string | undefined): string {
  if (!name) return ''
  return SPECIAL_TOOL_LABELS[name] || _resolveToolMeta(name)?.label || name
}

/** 消息 → 工具归属分组：assistant 索引 → 其后连续的工具卡片（同 agent_id）。
 *  分几轮调用工具时，轮与轮之间只起"锚点/占位"作用的空 assistant（无正文无思考，
 *  含流式占位）视为透明，跨过它把前后的工具并成一组 → 聊天区只渲染一条工具行，
 *  智能体输出正文时界面更整洁；有正文的 assistant 仍会截断分组（正文天然分隔轮次）。
 *  已认领的工具不会被后面的 assistant 重复分组（消费到哪条消息就从下一条继续）。 */
export function buildToolGroups(msgs: any[]): Map<number, any[]> {
  const map = new Map<number, any[]>()
  let i = 0
  while (i < msgs.length) {
    const m = msgs[i]
    if (!m || m.role !== 'assistant') { i++; continue }
    const bucket: any[] = []
    let j = i + 1
    let lastTool = -1
    while (j < msgs.length) {
      const n = msgs[j]
      if ((n.role === 'tool_call' || n.role === 'tool_result') && n.agent_id === m.agent_id) {
        bucket.push(n); lastTool = j; j++
      } else if (n.role === 'assistant' && n.agent_id === m.agent_id &&
                 !(n.content || '').trim() && !(n.reasoning || '').trim()) {
        j++   // 轮次间的空占位 assistant → 透明，继续向后合并
      } else {
        break
      }
    }
    if (bucket.length) map.set(i, bucket)
    i = lastTool >= 0 ? lastTool + 1 : i + 1
  }
  return map
}

/** 能力调用流式状态：args=正在接收参数（流式未完）→ exec=参数收齐、执行中 */
export interface ToolCallPhase {
  name: string
  phase: 'args' | 'exec'
}

export const useConvStore = defineStore('conv', {
  state: () => ({
    list: [] as any[],
    current: null as number | null,
    streaming: false,
    speakingAgent: null as number | null,
    selectedAgentId: null as number | null, // 节点💬直接对话选中的智能体
    focusChatAt: 0,                          // 触发对话区聚焦输入框
    running: false,                          // 画布运行中（等待/匹配触发）
    runCanvasId: null as number | null,
    _ctrl: null as AbortController | null,
    _timer: null as ReturnType<typeof setInterval> | null,
    _fired: {} as Record<string, string>,    // cron 防重 {agentId: 'YYYY-MM-DDTHH:MM'}
    _relayPhase: false,                      // 当前流是否已进入接力段
    handoffActive: false,                    // 正在 cluster.handoff / A2A 接力（连线流动）
    callEdges: [] as { from: number; to: number; tcId: string }[], // call 进行中的连线流动 {调用方, 对方, tool_call_id}
    // 能力调用流式状态：agent_id → { name, phase }
    //   phase='args' → 名称已确定、参数还在流（TS: 行）
    //   phase='exec' → 参数收齐（C: 行），工具执行中
    // R: 收到该 agent 最后一个结果后移除该条（见 toolPending）
    toolCalls: {} as Record<number, ToolCallPhase>,
    // 本批未返回结果的工具数：agent_id → 数量（C: +1，R: -1）
    // 一次性调用多个工具时，第一个 R: 不能立刻清掉 toolCalls，
    // 否则画布节点的"执行中"状态提前消失 → 渲染成空白气泡
    toolPending: {} as Record<number, number>,
    // —— 流式保存节流相关（2026-09 加入）——
    _flushTimer: null as ReturnType<typeof setTimeout> | null,
    _lastFlushAt: 0,           // 上次 flush 时间戳（ms）
    _unloadBound: false,       // 是否已绑定 beforeunload
  }),
  getters: {
    /** 当前发言智能体正在调用的能力（用于气泡顶部状态条） */
    speakingToolCall(state): ToolCallPhase | null {
      if (state.speakingAgent == null) return null
      return state.toolCalls[state.speakingAgent] || null
    },
  },
  actions: {
    async fetch() {
      const list: any[] = await api.convs.list()
      // 清洗历史遗留的 streaming 标记：fetch 时不可能存在进行中的流，
      // 防止之前异常中断被持久化的 streaming:true 在重开后渲染成"空泡泡+永久闪烁光标"
      for (const c of list) {
        for (const m of c?.messages || []) {
          if (m && typeof m === 'object' && m.streaming) m.streaming = false
        }
      }
      this.list = sortConvsByActivity(list)
    },
    async create(d: any = { name: '新对话', mode: 'independent', agent_ids: [] }) {
      const c = await api.convs.create(d); this.list = sortConvsByActivity([c, ...this.list]); this.current = c.id; return c
    },
    async update(id: number, d: any) {
      const c = await api.convs.update(id, d)
      const i = this.list.findIndex(x => x.id === id)
      if (i >= 0) this.list[i] = c
      return c
    },
    async remove(id: number) { await api.convs.remove(id); this.list = this.list.filter(x => x.id !== id); if (this.current === id) this.current = this.list[0]?.id ?? null },
    async removeMany(ids: number[]) {
      for (const id of ids) { try { await api.convs.remove(id) } catch { /* ignore */ } }
      const gone = new Set(ids)
      this.list = this.list.filter(x => !gone.has(x.id))
      if (this.current != null && gone.has(this.current)) this.current = this.list[0]?.id ?? null
    },
    currentData() { return this.list.find(x => x.id === this.current) },

    // ========== 流式持久化（2026-09 加入） ==========
    /** 强制把当前会话（含 streaming:true 的 assistant）写回后端。同步/异步都能跑。 */
    async flushCurrentNow(useSyncXHR = false): Promise<void> {
      if (this.current == null) return
      const c = this.currentData()
      if (!c) return
      const payload = {
        messages: (c.messages || []).map((m: any) => {
          // 去掉前端临时字段：streaming 保留（后端保存后重开会话能知道这条是半截），
          // reasoning / envelope / relay / agent_id / tool_call_id 等都保留，
          // 这里只确保不塞入循环引用或 BigInt 之类（messages 内容都是可 JSON 的）
          const o: any = { ...m }
          // 去掉纯前端 UI 字段（防止污染后端 schema）
          return o
        }),
      }
      try {
        if (useSyncXHR) {
          // beforeunload 里 fetch 会被浏览器取消，用同步 XHR 兜底（仅 Chrome/Edge 支持）
          const xhr = new XMLHttpRequest()
          xhr.open('PUT', `/api/conversations/${this.current}`, false) // false = 同步
          xhr.setRequestHeader('Content-Type', 'application/json')
          try { xhr.send(JSON.stringify(payload)) } catch { /* ignore */ }
        } else {
          await api.convs.update(this.current, payload)
        }
        this._lastFlushAt = Date.now()
      } catch {
        // 静默：流式过程中后端短暂不可达不应该炸掉整个流
      }
    },
    /** 节流 flush：调用后 1.2s 内只会真的发一次，避免每 chunk 都打一次 PUT */
    scheduleFlushCurrent(delayMs = 1200) {
      if (this._flushTimer) clearTimeout(this._flushTimer)
      this._flushTimer = setTimeout(() => {
        this._flushTimer = null
        void this.flushCurrentNow()
      }, delayMs)
    },
    /** 取消挂起的节流 flush（流式结束、手动 fetch 等场景） */
    cancelScheduledFlush() {
      if (this._flushTimer) { clearTimeout(this._flushTimer); this._flushTimer = null }
    },
    /** 绑定 window.beforeunload：关页/关标签时用同步 XHR 强制保存当前对话。
     *  只绑一次，幂等。 */
    bindBeforeUnload() {
      if (this._unloadBound || typeof window === 'undefined') return
      this._unloadBound = true
      const handler = () => {
        // 浏览器在 beforeunload 里只允许同步 XHR / sendBeacon
        try { this.flushCurrentNow(true) } catch { /* ignore */ }
        // 还尝试 sendBeacon 作为额外兜底（但 PUT 方法受限，这里先只走同步 XHR）
      }
      window.addEventListener('beforeunload', handler)
      // pagehide 也触发一次（iOS Safari 更倾向 pagehide）
      window.addEventListener('pagehide', handler)
    },

    async send(agentId: number, content: string) {
      if (this.current == null) await this.create()
      const c = this.currentData()

      // 文字触发：运行中且命中某智能体的 message 触发 → 替换目标
      if (this.running) {
        const hit = this._matchTextTrigger(content)
        if (hit) agentId = hit
      }

      // 链首不是接力段
      this._relayPhase = false
      this.handoffActive = false
      this.callEdges = []
      this.toolCalls = {}
      this.toolPending = {}

      // 绑定一次 beforeunload（幂等），关页/关标签会同步 XHR 强制保存
      this.bindBeforeUnload()

      // 构建当前轮消息数组（混合：用户 + 工具卡片 + 流式 assistant）
      const msgs: RichMsg[] = c.messages || []
      msgs.push({ role: 'user', content, ts: new Date().toISOString() })
      msgs.push({ role: 'assistant', content: '', streaming: true, ts: new Date().toISOString(), agent_id: agentId })
      c.messages = msgs
      this.streaming = true
      this.speakingAgent = agentId
      this._ctrl = new AbortController()

      // 先立刻落库一次（用户消息 + 空的 assistant 占位），中途 crash 也能保住用户输入
      await this.flushCurrentNow()

      try {
        // 逐行解析新协议（signal 支持「停止」中止 fetch → 后端断连取消接力链）
        const r = await fetch('/api/conversations/' + this.current + '/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: agentId, content }),
          signal: this._ctrl.signal,
        })
        if (!r.ok) throw new Error(r.statusText)
        const reader = r.body!.getReader()
        const decoder = new TextDecoder(); let buf = ''
        let tickCount = 0
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          let idx: number
          while ((idx = buf.indexOf('\n')) !== -1) {
            const line = buf.slice(0, idx); buf = buf.slice(idx + 1)
            if (!line.trim()) continue
            this._handleLine(c, line)
            // 每 ~6 行（约 1.2s）触发一次节流 flush —— 让后端逐步追上前端内存里的流式内容
            tickCount++
            if (tickCount % 6 === 0) this.scheduleFlushCurrent(1200)
          }
        }
        // 收尾
        const last = msgs[msgs.length - 1]
        if (last?.streaming) last.streaming = false
      } catch (e: any) {
        if (e?.name === 'AbortError') {
          const last = msgs[msgs.length - 1]
          if (last?.streaming) { last.streaming = false; last.content = (last.content || '') + ' [已停止]' }
        } else {
          // 传输/后端异常（HTTP 错误、断流、网络中断等）：给最后一条仍处于流式中的消息
          // 关闭流式位并补中断标记，避免它被 finally 的 flush 以 streaming:true + 空内容
          // 持久化——重开后会渲染成"空泡泡 + 永远闪烁的光标"
          const last = msgs[msgs.length - 1]
          if (last?.streaming) { last.streaming = false; last.content = (last.content || '') + ' [输出中断]' }
          throw e
        }
      } finally {
        this.streaming = false
        this.speakingAgent = null
        this._ctrl = null
        this._relayPhase = false
        this.handoffActive = false
        this.callEdges = []
        this.toolCalls = {}
        this.toolPending = {}
        // 兜底：清空本轮所有残留的 streaming 标记（正常结束 / 中断 / 异常统一收敛到这里），
        // 确保最终落库的 messages 不带"活的"流式标记，防止空泡泡+光标被持久化
        for (const m of msgs) {
          if ((m as any).streaming) (m as any).streaming = false
        }
        // 兜底：丢掉末尾仍然一个字都没有的 assistant 占位（模型整轮没输出正文时，
        // 不让空占位落库/渲染成空消息泡泡；带工具的锚点必在工具卡片之前，不会被误删）
        while (msgs.length) {
          const m = msgs[msgs.length - 1] as any
          if (m?.role !== 'assistant' || (m.content || '').trim() || (m.reasoning || '').trim() || m.tool_calls) break
          msgs.pop()
        }
        // 取消挂起的节流 flush（马上要手动 flush + fetch）
        this.cancelScheduledFlush()
        // 关键：先把最终状态（包含已停止标记）立刻写回后端，再 fetch 刷新
        await this.flushCurrentNow()
        // 刷新列表（保守：find 失败不把 current 清 null，避免内存里的 messages 被"闪空"）
        await this.fetch()
        if (this.current != null) {
          const found = this.list.find(x => x.id === this.current)
          if (found) this.current = found.id
          // 找不到就保留原 current —— abort 后后端 list 可能暂时没同步
        }
      }
    },

    // —— 运行/停止：按节点右键配置的触发方式（manual 等待 / cron 定时 / message 文字）——
    startRun(canvasId: number) {
      if (this.running) return
      this.running = true
      this.runCanvasId = canvasId
      this._fired = {}
      this._timer = setInterval(() => this._cronTick(), 20000)
      this._cronTick()
    },
    stopRun() {
      this.running = false
      this.runCanvasId = null
      if (this._timer) { clearInterval(this._timer); this._timer = null }
      if (this._ctrl) { this._ctrl.abort(); this._ctrl = null }
    },
    async _cronTick() {
      if (!this.running || this.streaming || this.runCanvasId == null) return
      const now = new Date()
      const minuteKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}T${now.getHours()}:${now.getMinutes()}`
      const canvas = useCanvasStore().list.find(x => x.id === this.runCanvasId)
      if (!canvas) return
      const agents = useAgentStore().list
      for (const n of canvas.nodes || []) {
        const a = agents.find((x: any) => x.id === n.agent_id)
        if (!a || a.enabled === false) continue
        const t = a.trigger || {}
        if (t.type !== 'cron' || !t.cron) continue
        if (!cronMatch(t.cron, now)) continue
        if (this._fired[a.id] === minuteKey) continue
        this._fired[a.id] = minuteKey
        // 找/建该画布的集群对话
        let conv = this.list.find(c => c.mode === 'linked' && c.canvas_id === canvas.id)
        if (!conv) conv = await this.create({ name: `⏱ ${canvas.name}`, mode: 'linked', agent_ids: canvas.nodes.map((x: any) => x.agent_id), canvas_id: canvas.id })
        else this.current = conv.id
        await this.send(a.id, t.message || '【定时触发】请执行任务')
        return // 一轮只触发一个，避免并发
      }
    },
    _matchTextTrigger(content: string): number | null {
      if (this.runCanvasId == null) return null
      const canvas = useCanvasStore().list.find(x => x.id === this.runCanvasId)
      if (!canvas) return null
      const agents = useAgentStore().list
      const text = (content || '').trim()
      if (!text) return null
      for (const n of canvas.nodes || []) {
        const a = agents.find((x: any) => x.id === n.agent_id)
        if (!a || a.enabled === false) continue
        const t = a.trigger || {}
        if (t.type !== 'message' || !t.message) continue
        if (text.includes(t.message)) return a.id
      }
      return null
    },

    /** 被消息目标开始输出（当前发言者是某条 call 连线的 to）→ 结束该条连线流动 */
    _callTargetOutputted() {
      if (!this.callEdges.length || this.speakingAgent == null) return
      const sp = this.speakingAgent
      const left = this.callEdges.filter(d => d.to !== sp)
      if (left.length !== this.callEdges.length) this.callEdges = left
    },

    _handleLine(c: any, line: string) {
      const msgs: RichMsg[] = c.messages || []
      const lastAssistant = msgs[msgs.length - 1]
      const curAgent = this.speakingAgent   // 当前发言者（工具调用的发起者）

      if (line === 'DONE') {
        this.handoffActive = false
        this.callEdges = []
        this.toolCalls = {}
        this.toolPending = {}
        return
      }

      if (line.startsWith('T:')) {
        // 对方开始输出文字 → 结束指向它的 call 连线流动
        this._callTargetOutputted()
        // 普通文字 → 追加到当前流式 assistant
        // ⚠️ 后端 _esc 转义了 payload 内的 \n/\r/\\，这里用 _unesc 还原
        const text = _unesc(line.slice(2))
        if (lastAssistant && lastAssistant.role === 'assistant' && lastAssistant.streaming) {
          lastAssistant.content = (lastAssistant.content || '') + text
        } else {
          // 懒创建：正文真正到达时才开新的 assistant（不在 R: 后预建空泡泡，
          // 一次性多工具调用会产生 N-1 条空 assistant → 空消息泡泡）
          msgs.push({ role: 'assistant', content: text, streaming: true, ts: new Date().toISOString(), relay: this._relayPhase, agent_id: curAgent ?? undefined })
        }
      } else if (line.startsWith('TS:')) {
        // 能力调用开始（名称已确定，参数仍在流式）→ 立即显示"正在调用: xxx"
        const name = line.slice(3).trim()
        if (name && curAgent != null) {
          this.toolCalls = { ...this.toolCalls, [curAgent]: { name, phase: 'args' } }
        }
      } else if (line.startsWith('C:')) {
        // tool_call: id|name|{json args}
        const rest = line.slice(2)
        const sep1 = rest.indexOf('|'); const sep2 = rest.indexOf('|', sep1 + 1)
        const tcId = rest.slice(0, sep1), tcName = rest.slice(sep1 + 1, sep2), argsJson = rest.slice(sep2 + 1)
        let args: any = {}; try { args = JSON.parse(argsJson) } catch { args = { _raw: argsJson } }
        // 参数收齐 → 阶段从 args 切到 exec（工具开始执行）
        if (curAgent != null && tcName) {
          this.toolCalls = { ...this.toolCalls, [curAgent]: { name: tcName, phase: 'exec' } }
          this.toolPending = { ...this.toolPending, [curAgent]: (this.toolPending[curAgent] || 0) + 1 }
        }
        // 沟通工具 cluster.handoff / cluster.send_message → 连线流动
        if (tcName === 'cluster.handoff' || tcName === 'cluster.send_message') this.handoffActive = true
        // 被消息目标以工具调用开场 → 结束指向它的流动
        this._callTargetOutputted()
        // call 调用 → 仅「调用方 → 对方」一条连线开始流动
        if (tcName === 'cluster.send_message') {
          const toId = resolveAgentId(args?.to_agent)
          if (toId != null && this.speakingAgent != null && toId !== this.speakingAgent) {
            this.callEdges.push({ from: this.speakingAgent, to: toId, tcId })
          }
        }
        // 结束当前流式 assistant
        if (lastAssistant?.streaming) lastAssistant.streaming = false
        // 工具卡片要挂在「前面最近一条同 agent 的 assistant」下才渲染得出来：
        // 末尾（跳过连续 tool_call/tool_result 后）不是同 agent 的 assistant
        // （接力切换发言者 / 正文还没到）→ 补一条空 assistant 作为工具行锚点
        let k = msgs.length - 1
        while (k >= 0 && (msgs[k].role === 'tool_call' || msgs[k].role === 'tool_result')) k--
        const anchor = k >= 0 ? msgs[k] : null
        if (anchor && anchor.role === 'assistant' && (anchor.agent_id ?? null) === (curAgent ?? null)) {
          if (anchor.agent_id == null && curAgent != null) anchor.agent_id = curAgent
        } else {
          msgs.push({ role: 'assistant', content: '', ts: new Date().toISOString(), relay: this._relayPhase, agent_id: curAgent ?? undefined })
        }
        msgs.push({ role: 'tool_call', content: '', name: tcName, tool_call_id: tcId, args, ts: new Date().toISOString(), relay: this._relayPhase, agent_id: curAgent ?? undefined })
      } else if (line.startsWith('R:')) {
        // tool_result: id|{json result}
        const rest = line.slice(2); const sep = rest.indexOf('|')
        const tcId = rest.slice(0, sep), resultJson = rest.slice(sep + 1)
        let result: any = {}; try { result = JSON.parse(resultJson) } catch { result = { _raw: resultJson } }
        // tool_result 的 agent_id = 对应 tool_call 的发起者
        const tc = msgs.find((m: RichMsg) => m.role === 'tool_call' && m.tool_call_id === tcId)
        const tcAgent = tc?.agent_id ?? curAgent ?? undefined
        msgs.push({ role: 'tool_result', content: '', tool_call_id: tcId, result, ts: new Date().toISOString(), relay: this._relayPhase, agent_id: tcAgent })
        // 工具执行完成 → 只有本批所有工具都返回结果后才移除能力调用流式状态
        // （一次性多个工具时，第一个 R: 就清掉会让画布/聊天的"执行中"提前消失 → 空白气泡）
        let remain = -1
        if (tcAgent != null) {
          const leftP = { ...this.toolPending }
          remain = (leftP[tcAgent] || 0) - 1
          if (remain > 0) leftP[tcAgent] = remain
          else delete leftP[tcAgent]
          this.toolPending = leftP
          if (remain <= 0 && this.toolCalls[tcAgent]) {
            const left = { ...this.toolCalls }
            delete left[tcAgent]
            this.toolCalls = left
          }
        }
        // 集群编排改动画布/智能体 → 立即拉取，避免画布停留在旧内容
        if (tc?.name && CLUSTER_REFETCH.has(tc.name)) {
          void useCanvasStore().fetch()
          void useAgentStore().fetch()
        }
        // call 调用失败（未连线/找不到目标等）→ 立即结束该条连线流动
        if (tc?.name === 'cluster.send_message' && result?.error) {
          this.callEdges = this.callEdges.filter(d => d.tcId !== tcId)
        }
        // 本批工具全部返回 → 才开一条流式 assistant 承接后续正文（"正在输入"指示）。
        // ⚠️ 不能每个 R: 都开：一次性调用多个工具会产生 N-1 条没有任何内容的空 assistant，
        // 它们永远收不到正文 → 渲染成空消息泡泡，还会把 tool_result 和 tool_call 拆散归属。
        if (remain <= 0) {
          let j = msgs.length - 1
          while (j >= 0 && (msgs[j].role === 'tool_call' || msgs[j].role === 'tool_result')) j--
          const tailA = j >= 0 ? msgs[j] : null
          const hasOpen = !!tailA && tailA.role === 'assistant' && tailA.streaming && !(tailA.content || '').trim()
          if (!hasOpen) {
            msgs.push({ role: 'assistant', content: '', streaming: true, ts: new Date().toISOString(), relay: this._relayPhase, agent_id: curAgent ?? undefined })
          }
        }
      } else if (line.startsWith('F:')) {
        // 最终完整文本（通常前端不需要，已经有流式拼接了）
        this._callTargetOutputted()
      } else if (line.startsWith('SPEAK:')) {
        // 切换发言者：结束当前流式消息（空泡泡直接丢弃），后续进入接力段（聊天区过滤）
        const id = parseInt(line.slice(6), 10)
        _closeTailAssistant(msgs)
        if (!Number.isNaN(id)) this.speakingAgent = id
        this._relayPhase = true
        this.handoffActive = true
      } else if (line.startsWith('A2A:')) {
        // A2A 消息信封：关闭当前流式消息 → 插入信封卡片（正文由后续 T: 懒创建）
        let env: any = null
        try { env = JSON.parse(line.slice(4)) } catch { env = { _raw: line.slice(4) } }
        this.handoffActive = true
        _closeTailAssistant(msgs)
        // 信封带双 agent_id：from.agent_id=发送方（画布源节点也应显示这条"我发出的"消息）
        //                 to.agent_id=接收方（画布目标节点应显示这条"我收到的"消息）
        msgs.push({ role: 'a2a', envelope: env, ts: new Date().toISOString(),
          agent_id: env?.to?.agent_id ?? curAgent ?? undefined,   // 主 agent_id 存接收方，给接收方节点显示
          from_agent_id: env?.from?.agent_id ?? undefined,       // 发送方，给源节点也显示"我发的"
        } as any)
      }
      c.messages = msgs
    }
  }
})
