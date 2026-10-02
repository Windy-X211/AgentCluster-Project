<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useCanvasStore, useAgentStore, useConvStore } from '@/stores'
import { api } from '@/api'
import { toolLabel, type ToolCallPhase } from '@/stores/conversation'
import { renderMarkdown } from '@/utils/markdown'

const canvases = useCanvasStore()
const agents = useAgentStore()
const convs = useConvStore()
import AgentEditDialog from './AgentEditDialog.vue'
import RulesDialog from './RulesDialog.vue'

// —— 对外广播的坐标契约：全局 WorkspaceOverlay 只消费这些，不自行推断画布位置 ——
const emit = defineEmits<{
  (e: 'canvas-bbox', bbox: { x: number; y: number; w: number; h: number; hasNodes: boolean }): void
  (e: 'canvas-activate', payload: {
    id: number; workdir: string | null; nodeBbox: { x: number; y: number; w: number; h: number; hasNodes: boolean } | null; scale: number
  }): void
  (e: 'open-workspace', pos: { clientX: number; clientY: number }): void
  (e: 'close-workspace'): void
}>()
const props = defineProps<{
  // 外层触发"设置工作区文件夹"对话框的信号(变化即触发)
  setupWdSignal?: number
}>()
watch(() => props.setupWdSignal, (v) => {
  if (v != null && v > 0) {
    wdPath.value = canvas.value?.workdir || ''
    wdOpen.value = true
  }
})

const boardRef = ref<HTMLElement | null>(null)
const viewportRef = ref<HTMLElement | null>(null)

// 视图变换: scale + offset（tx/ty 必须是 Vue ref，computed 才能追踪）
const scale = ref(1)
const tx = ref(0)
const ty = ref(0)
const MIN_ZOOM = 0.3, MAX_ZOOM = 2.5

// 节点拖动
const dragging = ref<{ node: any; dx: number; dy: number } | null>(null)
// 连线中
const linking = ref<{ from: number; mouseX: number; mouseY: number } | null>(null)
// 画布平移（中键或空格+左键）
const panning = ref<{ sx: number; sy: number; ox: number; oy: number } | null>(null)
const spaceDown = ref(false)

const ctxMenu = ref<{ node: any; x: number; y: number } | null>(null)
const chatPicker = ref<{ node: any; x: number; y: number } | null>(null)
// 画布空白区域右键菜单
const bgMenu = ref<{ x: number; y: number } | null>(null)
// 消息气泡：每个节点的隐藏状态（按画布 id 持久化在画布的 meta 字段里）
const bubbleHidden = ref<Record<number, boolean>>({})
const renameEdit = ref(false)
const renameValue = ref('')
const newCanvasDialog = ref(false)
const newCanvasName = ref('')
const toolbarCollapsed = ref(false)
const rulesOpen = ref(false)
const wdOpen = ref(false)
const wdPath = ref('')
const browsing = ref(false)

// AgentEditDialog 打开状态
const editingAgent = ref<any>(null)

// 触发设置
const triggerDlg = ref(false)
const triggerAgentId = ref<number | null>(null)
const triggerType = ref<'manual' | 'cron' | 'message'>('manual')
const triggerCron = ref('')
const triggerMsg = ref('')

const canvas = computed(() => canvases.current)
const nodes = computed(() => canvas.value?.nodes || [])
const edges = computed(() => canvas.value?.edges || [])

// —— 节点消息气泡：取本画布的集群对话（优先当前选中的）——
const bubbleConv = computed(() => {
  const cid = canvas.value?.id
  if (!cid) return null
  const cur = convs.currentData()
  if (cur && cur.mode === 'linked' && cur.canvas_id === cid) return cur
  const list = convs.list.filter((c: any) => c.mode === 'linked' && c.canvas_id === cid)
  return list.length ? list[list.length - 1] : null
})

type BubbleChunk = { kind: 'reasoning' | 'call' | 'text'; text: string }

function bubbleFor(node: any): { chunks: BubbleChunk[]; streaming: boolean } | null {
  const conv = bubbleConv.value
  if (!conv) return null
  const msgs = conv.messages || []
  const aid = node.agent_id
  const _trim = (s: string) => (s || '').replace(/^\s*\n+/, '').replace(/\n+\s*$/, '')
  const _stripAnno = (s: string) => (s || '').replace(/^\[[^\]]+\]\s*/, '').trim()

  const chunks: BubbleChunk[] = []
  let hasStreaming = false

  for (const m of msgs) {
    // ==== 1. 自己的深度思考（优先于正文显示在顶部，折叠/展开） ====
    if (m.role === 'assistant' && (m as any).agent_id === aid && (m as any).reasoning?.trim()) {
      chunks.push({ kind: 'reasoning', text: _stripAnno((m as any).reasoning) })
      if (m.streaming) hasStreaming = true
    }

    // ==== 2. 自己发的 cluster.send_message（我发给别人的消息） ====
    if (m.role === 'tool_call' && (m as any).agent_id === aid
        && (m as any).name === 'cluster.send_message') {
      const args = (m as any).args || {}
      const toName = args.to_agent != null ? String(args.to_agent) : '?'
      const msg = (args.message || '').trim()
      if (msg) chunks.push({ kind: 'call', text: `📤 → ${toName}：${msg}` })
    }

    // ==== 3. 自己的正文回复 ====
    if (m.role === 'assistant' && (m as any).agent_id === aid && (m.content || '').trim()) {
      chunks.push({ kind: 'text', text: m.content!.trim() })
      if (m.streaming) hasStreaming = true
    }

    // ==== 4. 自己发出的 A2A 信封（📤 我→对方） — 接收方的不显示 ====
    if (m.role === 'a2a') {
      const env = (m as any).envelope || {}
      const isSender = (m as any).from_agent_id === aid || env?.from?.agent_id === aid
      if (isSender) {
        const toName = env?.to?.name || String(env?.to?.agent_id || '?')
        const text = (env?.parts || [])
          .filter((p: any) => p?.type === 'text' && p?.text)
          .map((p: any) => p.text)
          .join('\n')
        if (text) chunks.push({ kind: 'call', text: `📤 我 → ${toName}：${_stripAnno(text)}` })
      }
    }
  }

  // 还在流式输出（刚 SPEAK 切换后，assistant 正文还没到）或正在调用能力
  const hasToolCall = !!toolCallFor(node)
  if (convs.streaming && convs.speakingAgent === aid && conv.id === convs.current) {
    if (!chunks.length && !hasToolCall) {
      return { chunks: [], streaming: true }
    }
  }

  if (!chunks.length && !hasToolCall) return null
  return { chunks, streaming: hasStreaming || hasToolCall }
}

function clipBubble(t: string): string {
  const s = (t || '').replace(/^\s*\n+/, '').replace(/\n+\s*$/, '').replace(/\s+/g, ' ').trim()
  return s.length > 150 ? s.slice(0, 150) + '…' : s
}

/** 当前节点正在流式调用的能力（无则 null）。独立于 bubbleFor，避免模板内嵌套 ! 断言。 */
function toolCallFor(node: any): ToolCallPhase | null {
  const conv = bubbleConv.value
  if (!conv) return null
  const aid = node.agent_id
  if (!(convs.streaming && convs.speakingAgent === aid && conv.id === convs.current)) return null
  return convs.toolCalls[aid] || null
}

// 节点活跃状态 → 用于视觉特效
// 优先级：speaking(正在输出 token) > calling(正在发起/处理 call) > waiting(集群在跑但自己闲置)
function nodeState(agentId: number): 'speaking' | 'calling' | 'waiting' | null {
  if (!convs.running && !convs.streaming) return null
  if (convs.streaming && convs.speakingAgent === agentId) return 'speaking'
  const edges = convs.callEdges || []
  if (edges.some(d => d.from === agentId || d.to === agentId)) return 'calling'
  return 'waiting'
}

// call 进行中：仅「调用方 → 对方」的一条连线流动；对方开始输出后（store 移除该条）结束
function isEdgeActive(e: any): boolean {
  if (!convs.streaming || !convs.callEdges.length) return false
  const from = nodes.value.find((x: any) => x.id === e.from_node)
  const to = nodes.value.find((x: any) => x.id === e.to_node)
  if (from == null || to == null) return false
  return convs.callEdges.some(d => {
    // 两节点间优先匹配正向（调用方→对方）连线；不存在正向时才回退反向
    const hasForward = edges.value.some((x: any) => {
      const f = nodes.value.find((n: any) => n.id === x.from_node)
      const t = nodes.value.find((n: any) => n.id === x.to_node)
      return f != null && t != null && f.agent_id === d.from && t.agent_id === d.to
    })
    if (hasForward) return from.agent_id === d.from && to.agent_id === d.to
    return (from.agent_id === d.from && to.agent_id === d.to) ||
           (from.agent_id === d.to && to.agent_id === d.from)
  })
}

function isEdgeDim(e: any): boolean {
  const from = nodes.value.find((x: any) => x.id === e.from_node)
  const to = nodes.value.find((x: any) => x.id === e.to_node)
  return (from && agentById(from.agent_id)?.enabled === false) ||
         (to && agentById(to.agent_id)?.enabled === false)
}

const agentById = (id: number) => agents.list.find((a: any) => a.id === id)

// 客户端坐标 ↔ 画布坐标（考虑 viewport 的 transform）
function clientToCanvas(clientX: number, clientY: number) {
  const rect = boardRef.value?.getBoundingClientRect()
  if (!rect) return { x: 0, y: 0 }
  return {
    x: (clientX - rect.left - tx.value) / scale.value,
    y: (clientY - rect.top - ty.value) / scale.value,
  }
}
function canvasToScreen(cx: number, cy: number) {
  const rect = boardRef.value?.getBoundingClientRect()
  if (!rect) return { x: cx, y: cy }
  return {
    x: rect.left + tx.value + cx * scale.value,
    y: rect.top + ty.value + cy * scale.value,
  }
}

// 所有节点的总包围盒（画布坐标），动态测量真实 DOM 高度
function computeNodeBbox(): { x: number; y: number; w: number; h: number; hasNodes: boolean } {
  const list = nodes.value || []
  if (!list.length) return { x: 0, y: 0, w: 0, h: 0, hasNodes: false }
  const boardRect = boardRef.value?.getBoundingClientRect()
  if (!boardRect) return { x: 0, y: 0, w: 0, h: 0, hasNodes: false }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  document.querySelectorAll<HTMLElement>('.viewport .node').forEach(el => {
    const r = el.getBoundingClientRect()
    const cx1 = (r.left - boardRect.left - tx.value) / scale.value
    const cy1 = (r.top - boardRect.top - ty.value) / scale.value
    const cx2 = (r.right - boardRect.left - tx.value) / scale.value
    const cy2 = (r.bottom - boardRect.top - ty.value) / scale.value
    if (cx1 < minX) minX = cx1; if (cy1 < minY) minY = cy1
    if (cx2 > maxX) maxX = cx2; if (cy2 > maxY) maxY = cy2
  })
  if (!isFinite(minX)) return { x: 0, y: 0, w: 0, h: 0, hasNodes: false }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY, hasNodes: true }
}
const nodeBbox = ref<ReturnType<typeof computeNodeBbox>>({ x: 0, y: 0, w: 0, h: 0, hasNodes: false })

// —— 缩放 ——
// 顶层浮层里的滚轮事件（右键菜单 / 编辑对话框 / 能力选择器 / 触发设置 / 模式选择）
// 不应冒泡到画布做缩放；node-bubble 已有 @wheel.stop，这里再兜底覆盖所有浮层
const FLOATY_GUARD = '.ctx-menu, .mode-picker, .modal-bg, .picker-mask, .node-bubble, .ws-panel, .ws-trash, .ws-ctx, .ws-modal-bg'
function onWheel(e: WheelEvent) {
  // 浮层内的滚轮 → 正常滚动浮层内容，画布不动
  if ((e.target as HTMLElement)?.closest(FLOATY_GUARD)) return
  e.preventDefault()
  kickWillMove(true)
  const rect = boardRef.value!.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top

  const prev = scale.value
  const delta = -e.deltaY
  const factor = delta > 0 ? 1.1 : 1 / 1.1
  const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev * factor))

  // 以鼠标位置为锚点：缩放后鼠标下的画布点保持不动
  const newTx = mx - (mx - tx.value) * (next / prev)
  const newTy = my - (my - ty.value) * (next / prev)
  scale.value = next
  tx.value = newTx
  ty.value = newTy
  syncGrid()
}

// —— 画布平移 ——
// will-change: transform 仅在交互期挂载：常驻合成层会让放大后的文字停留在旧光栅纹理上发糊，
// 交互结束移除 → 浏览器强制按当前 scale 重新光栅化，文字恢复锐利
const willMove = ref(false)
let willMoveTimer: ReturnType<typeof setTimeout> | null = null
function kickWillMove(autoRelease: boolean) {
  willMove.value = true
  if (willMoveTimer) clearTimeout(willMoveTimer)
  if (autoRelease) willMoveTimer = setTimeout(() => { willMove.value = false; willMoveTimer = null }, 200)
}
function releaseWillMove() {
  if (willMoveTimer) { clearTimeout(willMoveTimer); willMoveTimer = null }
  willMove.value = false
}
function startPan(e: MouseEvent) {
  if (e.button !== 1 && !(e.button === 0 && spaceDown.value)) return
  if ((e.target as HTMLElement).closest('.node, .btn, input, label, select, textarea, canvas-toolbar, .ws-panel, .ws-trash, .ws-ctx, .ws-modal-bg')) return
  kickWillMove(false)
  panning.value = { sx: e.clientX, sy: e.clientY, ox: tx.value, oy: ty.value }
  e.preventDefault()
}
function doPan(e: MouseEvent) {
  if (!panning.value) return
  tx.value = panning.value.ox + (e.clientX - panning.value.sx)
  ty.value = panning.value.oy + (e.clientY - panning.value.sy)
  syncGrid()
}
function endPan() { if (panning.value) { panning.value = null; releaseWillMove() } }

// 网格跟随平移/缩放 → 画布视觉上无限大
const gridRef = ref<HTMLElement | null>(null)
function syncGrid() {
  const g = gridRef.value
  if (!g) return
  g.style.backgroundPosition = `${tx.value}px ${ty.value}px`
  const size = 24 * scale.value
  g.style.backgroundSize = `${size}px ${size}px`
}

// 监听空格
const onKeyDown = (e: KeyboardEvent) => { if (e.code === 'Space') spaceDown.value = true }
const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') spaceDown.value = false }

// —— 节点拖放 ——
function onDrop(e: DragEvent) {
  e.preventDefault()
  if (!canvas.value) return
  const data = e.dataTransfer?.getData('agent'); if (!data) return
  const agent = JSON.parse(data)
  const { x, y } = clientToCanvas(e.clientX, e.clientY)
  const nextId = Math.max(0, ...nodes.value.map((n: any) => n.id)) + 1
  canvas.value.nodes.push({ id: nextId, agent_id: agent.id, x: x - 90, y: y - 40 })
  canvases.saveCurrent()
}

function startDrag(e: MouseEvent, node: any) {
  if ((e.target as HTMLElement).closest('.ctx-action, button, label, input')) return
  if (spaceDown.value || e.button !== 0) return
  const c = clientToCanvas(e.clientX, e.clientY)
  dragging.value = { node, dx: c.x - node.x, dy: c.y - node.y }
}
function onMouseMove(e: MouseEvent) {
  doPan(e)
  const c = clientToCanvas(e.clientX, e.clientY)
  if (dragging.value) {
    const nx = c.x - dragging.value.dx
    const ny = c.y - dragging.value.dy
    const snapped = snapToEdges(dragging.value.node, nx, ny, scale.value)
    dragging.value.node.x = snapped.x
    dragging.value.node.y = snapped.y
  }
  if (linking.value) { linking.value.mouseX = c.x; linking.value.mouseY = c.y }
}
function onMouseUpGlobal() {
  endDrag(); endPan()
  linking.value = null
}
function endDrag() {
  if (dragging.value) { dragging.value = null; canvases.saveCurrent() }
  snapGuides.value = { x: [], y: [] }
}

function startLink(e: MouseEvent, nodeId: number) {
  e.stopPropagation()
  const c = clientToCanvas(e.clientX, e.clientY)
  linking.value = { from: nodeId, mouseX: c.x, mouseY: c.y }
}
function endLink(e: MouseEvent, toNodeId: number | null) {
  if (linking.value && toNodeId != null && linking.value.from !== toNodeId && canvas.value) {
    const nextId = Math.max(0, ...edges.value.map((n: any) => n.id)) + 1
    canvas.value.edges.push({ id: nextId, from_node: linking.value.from, to_node: toNodeId, type: 'chat' })
    canvases.saveCurrent()
  }
  linking.value = null
}

// —— 浮层位置对齐（避免超出视口） ——
async function clampToViewport(ref: { value: { x: number; y: number } | null }, selector: string) {
  await nextTick()
  const el = document.querySelector<HTMLElement>(selector)
  if (!el || !ref.value) return
  const vw = window.innerWidth, vh = window.innerHeight
  const margin = 8
  const { offsetWidth: w, offsetHeight: h } = el
  ref.value = {
    ...ref.value,
    x: Math.max(margin, Math.min(ref.value.x, vw - w - margin)),
    y: Math.max(margin, Math.min(ref.value.y, vh - h - margin)),
  }
}

// —— 右键菜单 ——
function showCtxMenu(e: MouseEvent, node: any) {
  e.preventDefault(); e.stopPropagation()
  ctxMenu.value = { node, x: e.clientX, y: e.clientY }
  clampToViewport(ctxMenu, '.ctx-menu')
  bgMenu.value = null
}
function showBgMenu(e: MouseEvent) {
  // 仅在真正空白（没有点到节点/连线/工具栏/浮层）时弹出
  const target = e.target as HTMLElement
  if (target?.closest('.node, .edge, .edge-hit, .edge-mid, .canvas-toolbar, .hint, .canvas-mini, .collapse-btn')) return
  if (target?.closest('.ctx-menu, .mode-picker, .ctx-item, .modal-bg, .conv-float')) return  // 浮层自己会处理，别拦
  e.preventDefault()
  ctxMenu.value = null; chatPicker.value = null
  bgMenu.value = { x: e.clientX, y: e.clientY }
  clampToViewport(bgMenu as any, '.ctx-menu')
}
function closeMenus() { ctxMenu.value = null; chatPicker.value = null; bgMenu.value = null }

// —— 工作区交互：全部 emit 给外层 MainView（全局单例 WorkspaceOverlay 持有者） ——
function toggleWorkspace() {
  const p = bgMenu.value || { x: window.innerWidth / 2, y: window.innerHeight / 2 }
  emit('open-workspace', { clientX: p.x, clientY: p.y })
  closeMenus()
}

// 双击节点切换消息气泡显示
function toggleBubble(node: any) {
  bubbleHidden.value = { ...bubbleHidden.value, [node.id]: !bubbleHidden.value[node.id] }
}
function hideAllBubbles() { bubbleHidden.value = {} }  // 清空隐藏 = 全部显示
// 所有节点都隐藏
function hideAllBubblesForce() {
  const m: Record<number, boolean> = {}
  nodes.value.forEach((n: any) => { m[n.id] = true })
  bubbleHidden.value = m
}

async function removeNode(nodeId: number) {
  if (!canvas.value) return
  canvas.value.nodes = nodes.value.filter((n: any) => n.id !== nodeId)
  canvas.value.edges = edges.value.filter((e: any) => e.from_node !== nodeId && e.to_node !== nodeId)
  canvases.saveCurrent(); closeMenus()
}
function removeEdge(edgeId: number) {
  if (!canvas.value) return
  canvas.value.edges = edges.value.filter((e: any) => e.id !== edgeId)
  canvases.saveCurrent()
}

function openChatPicker(e: MouseEvent, node: any) {
  e.stopPropagation()
  closeMenus()
  chatPicker.value = { node, x: e.clientX, y: e.clientY }
  clampToViewport(chatPicker, '.mode-picker')
}

async function doChat(node: any, mode: 'independent' | 'linked') {
  closeMenus()
  const canvasId = canvas.value?.id ?? null
  // 复用当前会话（携带上下文），只有顶部「＋独立/＋集群」才新建重置
  let conv = convs.currentData()
  if (!conv) {
    conv = await convs.create({ name: '', mode, agent_ids: [node.agent_id], canvas_id: canvasId })
  } else if (conv.mode !== mode || (mode === 'linked' && (conv.canvas_id ?? null) !== canvasId)) {
    // 模式/画布不一致时原地切换，保留 messages 历史
    await convs.update(conv.id, {
      mode,
      canvas_id: mode === 'linked' ? canvasId : (conv.canvas_id ?? canvasId),
      agent_ids: Array.from(new Set([...(conv.agent_ids || []), node.agent_id])),
    })
  } else if (!(conv.agent_ids || []).includes(node.agent_id)) {
    await convs.update(conv.id, {
      agent_ids: Array.from(new Set([...(conv.agent_ids || []), node.agent_id])),
    })
  }
  convs.current = conv!.id
  // 自动和它对话：选中该智能体 + 聚焦输入框
  convs.selectedAgentId = node.agent_id
  convs.focusChatAt = Date.now()
}

// ⑤ 停用/启用
async function toggleEnabled(node: any) {
  const a = agentById(node.agent_id)
  if (!a) return
  await agents.update(a.id, { ...a, enabled: a.enabled === false })
  closeMenus()
}

// ⑥ 编辑智能体（打开 AgentEditDialog）
function openAgentEdit(node: any) {
  const a = agentById(node.agent_id)
  if (!a) return
  closeMenus()
  editingAgent.value = { ...a }
}

// ⑦ 感知范围切换
async function setPerceptionScope(node: any, scope: 'link' | 'global') {
  const a = agentById(node.agent_id)
  if (!a) return
  await agents.update(a.id, { ...a, perception_scope: scope })
  closeMenus()
}

function perceptionScopeOf(node: any): 'link' | 'global' {
  const a = agentById(node.agent_id)
  return (a?.perception_scope as any) === 'global' ? 'global' : 'link'
}

// ⑤ 触发设置
function openTriggerDlg(node: any) {
  const a = agentById(node.agent_id)
  if (!a) return
  closeMenus()
  triggerAgentId.value = a.id
  const t = a.trigger || {}
  triggerType.value = (t.type || 'manual') as any
  triggerCron.value = t.cron || ''
  triggerMsg.value = t.message || ''
  triggerDlg.value = true
}
async function saveTrigger() {
  const a = agentById(triggerAgentId.value!)
  if (!a) { triggerDlg.value = false; return }
  await agents.update(a.id, {
    ...a,
    trigger: { type: triggerType.value, cron: triggerCron.value, message: triggerMsg.value },
  })
  triggerDlg.value = false
}
// ⑤ 运行/停止
function toggleRun() {
  if (!canvas.value) return
  if (convs.running || convs.streaming) convs.stopRun()
  else convs.startRun(canvas.value.id)
}

// 串行/并行切换开关
async function toggleExecMode() {
  if (!canvas.value) return
  canvas.value.execution_mode = canvas.value.execution_mode === 'parallel' ? 'serial' : 'parallel'
  await canvases.saveCurrent()
}

// —— 画布命名 ——
function startRename() { renameValue.value = canvas.value?.name || ''; renameEdit.value = true }
function commitRename() {
  const name = renameValue.value.trim()
  if (name && canvas.value?.name !== name) canvases.rename(canvas.value.id, name)
  renameEdit.value = false
}
function openNewCanvas() { newCanvasName.value = ''; newCanvasDialog.value = true }
function commitNewCanvas() {
  const name = newCanvasName.value.trim() || `画布 ${canvases.list.length + 1}`
  canvases.create({ name }); newCanvasDialog.value = false
}

// —— 路径（画布坐标）：就近锚点 + 双向线垂直偏移防交叉 ——
const NODE_W = 180, NODE_H = 80
const BIDIR_OFFSET = 16 // 双向连线每条沿法线偏移的距离；两条线垂直间距恒为 2 × BIDIR_OFFSET

// —— 边缘自动对齐 ——
const SNAP_THRESHOLD = 8 // 吸附触发距离（画布坐标）
const snapGuides = ref<{ x: number[]; y: number[] }>({ x: [], y: [] }) // 当前激活的对齐辅助线（画布坐标）

/**
 * 对节点的候选位置进行边缘吸附：
 *   - 四条边 (left/right/top/bottom) 与其他节点对应边对齐
 *   - 两条中心线 (cx/cy) 与其他节点的中心线对齐
 * 返回吸附后的 { x, y } 并填充 snapGuides。
 */
function snapToEdges(draggingNode: any, candidateX: number, candidateY: number, scaleNow: number): { x: number; y: number } {
  const others = nodes.value.filter((n: any) => n.id !== draggingNode.id)
  if (!others.length) { snapGuides.value = { x: [], y: [] }; return { x: candidateX, y: candidateY } }

  const w = NODE_W, h = NODE_H
  const myEdges = {
    left: candidateX, right: candidateX + w,
    top: candidateY, bottom: candidateY + h,
    cx: candidateX + w / 2, cy: candidateY + h / 2,
  }

  type XKey = 'left' | 'right' | 'cx'
  type YKey = 'top' | 'bottom' | 'cy'
  const xTargets: { pos: number; from: XKey }[] = []
  const yTargets: { pos: number; from: YKey }[] = []

  for (const o of others) {
    const ox = { left: o.x, right: o.x + w, cx: o.x + w / 2 }
    const oy = { top: o.y, bottom: o.y + h, cy: o.y + h / 2 }
    // X: 我的 left/right/cx → 对方的 left/right/cx
    ;(['left', 'right', 'cx'] as XKey[]).forEach(myKey => {
      ;(['left', 'right', 'cx'] as XKey[]).forEach(oKey => {
        xTargets.push({ pos: ox[oKey], from: myKey })
      })
    })
    // Y: 我的 top/bottom/cy → 对方的 top/bottom/cy
    ;(['top', 'bottom', 'cy'] as YKey[]).forEach(myKey => {
      ;(['top', 'bottom', 'cy'] as YKey[]).forEach(oKey => {
        yTargets.push({ pos: oy[oKey], from: myKey })
      })
    })
  }

  const threshold = SNAP_THRESHOLD / scaleNow // 吸附阈值是屏幕像素，转换到画布坐标

  // 找 X 方向最近的一次吸附
  let bestDx = 0, bestXGuide: number | null = null
  for (const t of xTargets) {
    const cur = myEdges[t.from]
    const diff = t.pos - cur
    if (Math.abs(diff) <= threshold) {
      if (Math.abs(diff) < Math.abs(bestDx) || bestDx === 0) {
        bestDx = diff
        bestXGuide = t.pos
      }
    }
  }

  let bestDy = 0, bestYGuide: number | null = null
  for (const t of yTargets) {
    const cur = myEdges[t.from]
    const diff = t.pos - cur
    if (Math.abs(diff) <= threshold) {
      if (Math.abs(diff) < Math.abs(bestDy) || bestDy === 0) {
        bestDy = diff
        bestYGuide = t.pos
      }
    }
  }

  const finalX = candidateX + bestDx
  const finalY = candidateY + bestDy

  // 辅助线：激活的 X 线（画竖线）和 Y 线（画横线）
  const gx: number[] = []
  const gy: number[] = []
  if (bestXGuide != null) gx.push(bestXGuide)
  if (bestYGuide != null) gy.push(bestYGuide)

  // 吸附后再计算一次，看看新位置有没有额外的对齐可以同时触发（例：一次拖到两个节点的角上）
  const finalEdges = {
    left: finalX, right: finalX + w,
    top: finalY, bottom: finalY + h,
    cx: finalX + w / 2, cy: finalY + h / 2,
  }
  for (const o of others) {
    const ox = { left: o.x, right: o.x + w, cx: o.x + w / 2 }
    const oy = { top: o.y, bottom: o.y + h, cy: o.y + h / 2 }
    for (const v of Object.values(ox)) if (Math.abs(v - finalEdges.left) <= threshold || Math.abs(v - finalEdges.right) <= threshold || Math.abs(v - finalEdges.cx) <= threshold) {
      if (!gx.includes(v)) gx.push(v)
    }
    for (const v of Object.values(oy)) if (Math.abs(v - finalEdges.top) <= threshold || Math.abs(v - finalEdges.bottom) <= threshold || Math.abs(v - finalEdges.cy) <= threshold) {
      if (!gy.includes(v)) gy.push(v)
    }
  }

  snapGuides.value = { x: gx, y: gy }
  return { x: finalX, y: finalY }
}

/** 按相对方位选最近侧锚点：水平差为主走左右，垂直差为主走上下 */
function sideAnchors(a: any, b: any): { p1: { x: number; y: number }; p2: { x: number; y: number }; horizontal: boolean } {
  const acx = a.x + NODE_W / 2, acy = a.y + NODE_H / 2
  const bcx = b.x + NODE_W / 2, bcy = b.y + NODE_H / 2
  const dx = bcx - acx, dy = bcy - acy
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0
      ? { p1: { x: a.x + NODE_W, y: acy }, p2: { x: b.x, y: bcy }, horizontal: true }
      : { p1: { x: a.x, y: acy }, p2: { x: b.x + NODE_W, y: bcy }, horizontal: true }
  }
  return dy >= 0
    ? { p1: { x: acx, y: a.y + NODE_H }, p2: { x: bcx, y: b.y }, horizontal: false }
    : { p1: { x: acx, y: a.y }, p2: { x: bcx, y: b.y + NODE_H }, horizontal: false }
}

function hasReverseEdge(e: any): boolean {
  return edges.value.some((x: any) => x.from_node === e.to_node && x.to_node === e.from_node)
}

const EDGE_SAMPLES = 64

function cubicAt(p0: any, c1: any, c2: any, p3: any, t: number) {
  const it = 1 - t
  return {
    x: it * it * it * p0.x + 3 * it * it * t * c1.x + 3 * it * t * t * c2.x + t * t * t * p3.x,
    y: it * it * it * p0.y + 3 * it * it * t * c1.y + 3 * it * t * t * c2.y + t * t * t * p3.y,
  }
}

function cubicTangentAt(p0: any, c1: any, c2: any, p3: any, t: number) {
  const it = 1 - t
  return {
    x: 3 * it * it * (c1.x - p0.x) + 6 * it * t * (c2.x - c1.x) + 3 * t * t * (p3.x - c2.x),
    y: 3 * it * it * (c1.y - p0.y) + 6 * it * t * (c2.y - c1.y) + 3 * t * t * (p3.y - c2.y),
  }
}

function smoothPath(pts: { x: number; y: number }[]) {
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 1; i < pts.length - 1; i++) {
    const p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1]
    const p3 = pts[i + 2] || p2
    const cp1x = p1.x + (p2.x - p0.x) / 6
    const cp1y = p1.y + (p2.y - p0.y) / 6
    const cp2x = p2.x - (p3.x - p1.x) / 6
    const cp2y = p2.y - (p3.y - p1.y) / 6
    d += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`
  }
  return d
}

function offsetEdgePath(p0: any, c1: any, c2: any, p3: any, dist: number) {
  const pts: { x: number; y: number }[] = []
  for (let i = 0; i <= EDGE_SAMPLES; i++) {
    const t = i / EDGE_SAMPLES
    const B = cubicAt(p0, c1, c2, p3, t)
    const T = cubicTangentAt(p0, c1, c2, p3, t)
    const len = Math.hypot(T.x, T.y) || 1
    pts.push({ x: B.x - (T.y / len) * dist, y: B.y + (T.x / len) * dist })
  }
  return smoothPath(pts)
}

function computeEdge(e: any) {
  const a = nodes.value.find((n: any) => n.id === e.from_node)
  const b = nodes.value.find((n: any) => n.id === e.to_node)
  if (!a || !b) return null
  const { p1, p2, horizontal } = sideAnchors(a, b)
  let c1 = { ...p1 }, c2 = { ...p2 }
  if (horizontal) {
    const mx = (p1.x + p2.x) / 2
    c1 = { x: mx, y: p1.y }
    c2 = { x: mx, y: p2.y }
  } else {
    const my = (p1.y + p2.y) / 2
    c1 = { x: p1.x, y: my }
    c2 = { x: p2.x, y: my }
  }
  // 双向两条线：沿曲线法线做等距偏移（采样+平滑）→ 两条线处处平行、垂直间距恒定
  const rev = hasReverseEdge(e)
  const d = rev
    ? offsetEdgePath(p1, c1, c2, p2, BIDIR_OFFSET)
    : `M ${p1.x} ${p1.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${p2.x} ${p2.y}`
  const mid = cubicAt(p1, c1, c2, p2, 0.5)
  const tang = cubicTangentAt(p1, c1, c2, p2, 0.5)
  const tlen = Math.hypot(tang.x, tang.y) || 1
  const midX = mid.x + (rev ? -tang.y / tlen * BIDIR_OFFSET : 0)
  const midY = mid.y + (rev ? tang.x / tlen * BIDIR_OFFSET : 0)
  return { d, midX, midY, midAngle: Math.atan2(tang.y, tang.x) * 180 / Math.PI }
}

const edgeGeoms = computed(() => edges.value.map((e: any) => ({ e, g: computeEdge(e) })))

function tempPath() {
  if (!linking.value) return ''
  const a = nodes.value.find((n: any) => n.id === linking.value!.from)
  if (!a) return ''
  const acx = a.x + NODE_W / 2, acy = a.y + NODE_H / 2
  const dx = linking.value.mouseX - acx, dy = linking.value.mouseY - acy
  let p1: { x: number; y: number }
  if (Math.abs(dx) >= Math.abs(dy)) {
    p1 = dx >= 0 ? { x: a.x + NODE_W, y: acy } : { x: a.x, y: acy }
  } else {
    p1 = dy >= 0 ? { x: acx, y: a.y + NODE_H } : { x: acx, y: a.y }
  }
  const x2 = linking.value.mouseX, y2 = linking.value.mouseY
  const mx = (p1.x + x2) / 2, my = (p1.y + y2) / 2
  if (Math.abs(dx) >= Math.abs(dy)) {
    return `M ${p1.x} ${p1.y} C ${mx} ${p1.y}, ${mx} ${y2}, ${x2} ${y2}`
  }
  return `M ${p1.x} ${p1.y} C ${p1.x} ${my}, ${x2} ${my}, ${x2} ${y2}`
}

// 视图适配：缩放+平移视图，使画布上的所有节点都完整可见（并居中）
function fitAll() {
  const board = boardRef.value
  if (!board) return
  const rect = board.getBoundingClientRect()
  if (!nodes.value.length) {
    tx.value = 0; ty.value = 0; scale.value = 1
    syncGrid()
    return
  }
  const PAD = 60
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of nodes.value) {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y)
    maxX = Math.max(maxX, n.x + NODE_W); maxY = Math.max(maxY, n.y + NODE_H)
  }
  minX -= PAD; minY -= PAD; maxX += PAD; maxY += PAD
  const w = maxX - minX, h = maxY - minY
  const s = Math.min(1, Math.max(MIN_ZOOM, Math.min(rect.width / w, rect.height / h)))
  scale.value = s
  tx.value = rect.width / 2 - ((minX + maxX) / 2) * s
  ty.value = rect.height / 2 - ((minY + maxY) / 2) * s
  syncGrid()
}
function resetView() { fitAll() }
async function browseWorkdir() {
  browsing.value = true
  try {
    const res: any = await api.canvases.browse()
    if (res?.path) wdPath.value = res.path
  } catch (e: any) {
    alert('浏览失败：' + (e?.message || e) + '\n（后端需要在桌面环境下运行才能弹出文件夹选择框）')
  } finally {
    browsing.value = false
  }
}
async function saveWorkdir() {
  if (!canvas.value) return
  try {
    const res: any = await api.canvases.setWorkdir(canvas.value.id, wdPath.value.trim() || null)
    canvases.current = { ...canvas.value, workdir: res.workdir }
    const i = canvases.list.findIndex(x => x.id === canvas.value!.id)
    if (i >= 0) canvases.list[i] = canvases.current
    wdOpen.value = false
  } catch (e: any) {
    alert('保存工作区失败：' + (e?.message || e))
  }
}
function confirmRemoveCanvas() {
  if (!canvas.value) return
  if (window.confirm('删除此画布?')) canvases.remove(canvas.value.id)
}

onMounted(() => {
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('keyup', onKeyUp)
  window.addEventListener('mouseup', onMouseUpGlobal)
  window.addEventListener('mousemove', onMouseMove)
  nextTick(fitAll)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('keyup', onKeyUp)
  window.removeEventListener('mouseup', onMouseUpGlobal)
  window.removeEventListener('mousemove', onMouseMove)
})

watch([tx, ty], () => { nextTick(syncGrid) }, { immediate: true })

// 画布切换 / 节点变化 / 视图变换 → 重新计算 bbox 并广播
function refreshBboxAndEmit() {
  const nb = computeNodeBbox()
  nodeBbox.value = nb
  emit('canvas-bbox', nb)
}
// 画布激活事件（切换画布 / 挂载首次）
function emitCanvasActivate() {
  if (!canvas.value) return
  const nb = computeNodeBbox()
  nodeBbox.value = nb
  emit('canvas-activate', {
    id: canvas.value.id,
    workdir: canvas.value.workdir || null,
    nodeBbox: nb,
    scale: scale.value,
  })
}
watch(() => canvas.value?.id, () => {
  nextTick(() => { fitAll(); bubbleHidden.value = {} })
})
watch([() => canvas.value?.id, scale], () => {
  nextTick(emitCanvasActivate)
}, { immediate: true })
watch([() => nodes.value?.length, tx, ty, scale], () => {
  nextTick(refreshBboxAndEmit)
}, { flush: 'post' })

// 每次 transform 变化时强制 style 更新
const vTransform = computed(() => {
  return `translate(${tx.value}px, ${ty.value}px) scale(${scale.value})`
})

// ③ 气泡流式自动滚到底：发言者流式内容长度 + 消息数 + speakingAgent + 能力调用状态变化
const bubbleScrollKey = computed(() => {
  const conv = bubbleConv.value
  if (!conv) return '0'
  const msgs = conv.messages || []
  let streamLen = 0
  for (const m of msgs) {
    if (m.role === 'assistant' && m.streaming) streamLen += (m.content || '').length
  }
  const tcKey = Object.entries(convs.toolCalls).map(([k, v]) => `${k}:${(v as any).name}:${(v as any).phase}`).join('|')
  return `${convs.streaming ? 1 : 0}:${convs.speakingAgent ?? 0}:${convs.handoffActive ? 1 : 0}:${msgs.length}:${streamLen}:${tcKey}`
})

function scrollBubblesToBottom() {
  const root = boardRef.value
  if (!root) return
  // 只自动贴底「正在流式输出的发言者」那个气泡，已完成的让用户自由滚动
  if (!convs.streaming) return
  root.querySelectorAll<HTMLElement>('.node-bubble.streaming').forEach(el => {
    el.scrollTop = el.scrollHeight
  })
}

function scrollAllBubblesToBottom() {
  const root = boardRef.value
  if (!root) return
  root.querySelectorAll<HTMLElement>('.node-bubble').forEach(el => {
    el.scrollTop = el.scrollHeight
  })
}

watch(bubbleScrollKey, async () => {
  await nextTick()
  // 等浏览器完成本帧布局后再滚，确保 scrollHeight 已包含最新文字
  requestAnimationFrame(scrollBubblesToBottom)
}, { flush: 'post' })

// 打开/切换对话记录时，把画布上所有当前显示的气泡一次性滑到底部
watch(() => bubbleConv.value?.id, async (id) => {
  if (id == null) return
  await nextTick()
  requestAnimationFrame(scrollAllBubblesToBottom)
}, { immediate: true })

// 流式过程中持续贴底（token 逐段到达时 key 可能不变的兜底）
watch(() => [convs.streaming, convs.speakingAgent] as const, async ([sp]) => {
  if (!sp) return
  while (convs.streaming) {
    await nextTick()
    scrollBubblesToBottom()
    await new Promise(r => setTimeout(r, 120))
  }
  await nextTick()
  scrollBubblesToBottom()
}, { immediate: true })

// 暴露 viewportRef 给父组件（钉住模式下 WorkspaceOverlay 要嵌入 viewport 内部）
defineExpose({ viewportRef, computeNodeBbox })
</script>

<template>
  <div class="canvas-board glass" ref="boardRef"
       @dragover.prevent @drop="onDrop" @wheel.passive="onWheel"
       @mousedown="startPan"
       @click="closeMenus" @contextmenu="showBgMenu">
    <div class="grid-bg" ref="gridRef" />

    <!-- 工具栏（可折叠） -->
    <div class="canvas-toolbar" :class="{ collapsed: toolbarCollapsed }">
      <!-- 折叠态：极简小条 -->
      <template v-if="toolbarCollapsed">
        <div class="toolbar-left collapsed-bar">
          <button class="btn ghost collapse-btn" @click.stop="toolbarCollapsed = false" title="展开工具栏">▾</button>
          <span class="canvas-mini" :title="'当前画布：' + canvas?.name">🎨 {{ canvas?.name }}</span>
        </div>
        <div class="toolbar-right">
          <button class="btn ghost run-btn" :class="{ stop: convs.running || convs.streaming }"
                  @click.stop="toggleRun">
            {{ convs.running || convs.streaming ? '⏹' : '▶' }}
          </button>
          <button class="btn ghost em-collapsed" @click.stop="toggleExecMode"
                  :title="canvas?.execution_mode === 'parallel' ? '并行 → 点击切换串行' : '串行 → 点击切换并行'">
            {{ canvas?.execution_mode === 'parallel' ? '⇉' : '⇥' }}
          </button>
        </div>
      </template>
      <!-- 展开态：完整按钮组 -->
      <template v-else>
        <div class="toolbar-left">
          <button class="btn ghost collapse-btn" @click.stop="toolbarCollapsed = true" title="折叠工具栏">▸</button>
          <template v-if="renameEdit">
            <input class="input sm rename-input" :value="renameValue" @input="renameValue = ($event.target as HTMLInputElement).value"
                   @keydown.enter.prevent="commitRename" @keydown.esc="renameEdit = false"
                   @blur="commitRename" ref="r => (r as HTMLInputElement)?.focus()" />
          </template>
          <template v-else>
            <span class="canvas-name" @dblclick="startRename" :title="'双击重命名：' + canvas?.name">{{ canvas?.name }}</span>
            <span class="canvas-meta">{{ nodes.length }} 节点 · {{ edges.length }} 连线</span>
          </template>
        </div>
        <div class="toolbar-right">
          <button class="btn ghost run-btn" :class="{ stop: convs.running || convs.streaming }"
                  @click.stop="toggleRun"
                  :title="convs.running ? '停止：中止定时/文字触发与进行中的请求' : '运行：按节点右键配置的触发方式（等待/定时/文字）运行集群'">
            {{ convs.running || convs.streaming ? '⏹ 停止' : '▶ 运行' }}
          </button>
          <div class="exec-mode-toggle" @click.stop
               :title="canvas?.execution_mode === 'parallel' ? '并行：发送消息后继续运行' : '串行：发送消息后停止运行'">
            <span class="em-label" :class="{ active: canvas?.execution_mode !== 'parallel' }">串行</span>
            <label class="em-switch">
              <input type="checkbox" :checked="canvas?.execution_mode === 'parallel'" @change="toggleExecMode" @click.stop />
              <span class="em-slider"></span>
            </label>
            <span class="em-label" :class="{ active: canvas?.execution_mode === 'parallel' }">并行</span>
          </div>
          <button class="btn ghost" :class="{ activeWd: !!canvas?.workdir }"
                  @click.stop="wdPath = canvas?.workdir || ''; wdOpen = true"
                  :title="canvas?.workdir ? '📁 工作区已设置：' + canvas.workdir : '📁 设置工作区文件夹（智能体文件生成目标）'">
            📁 {{ canvas?.workdir ? '已设' : '工作区' }}
          </button>
          <button class="btn ghost" @click.stop="rulesOpen = true" title="📋 规章制度">📋 规则</button>
          <span class="zoom-indicator" :class="{ panning: spaceDown }">
            🔍 {{ Math.round(scale * 100) }}%
            <span class="zoom-tip" v-if="spaceDown"> 拖动平移中…</span>
          </span>
          <button class="btn ghost" @click.stop="canvases.saveCurrent()" title="保存画布">💾</button>
          <button class="btn ghost" @click.stop="confirmRemoveCanvas" title="删除此画布">🗑</button>
        </div>
      </template>
    </div>

    <!-- 可缩放 / 可平移的视口 -->
    <div class="viewport" ref="viewportRef" :class="{ 'will-move': willMove }" :style="{ transform: vTransform }"
         @contextmenu="showBgMenu">
      <svg class="edge-layer">
        <g v-for="item in edgeGeoms" :key="item.e.id">
          <path v-if="item.g" :d="item.g.d"
                :class="['edge-hit', { active: isEdgeActive(item.e), dim: isEdgeDim(item.e) }]"
                @dblclick="removeEdge(item.e.id)" />
          <path v-if="item.g" :d="item.g.d"
                :class="['edge', { active: isEdgeActive(item.e), dim: isEdgeDim(item.e) }]"
                marker-end="url(#arrow)" pointer-events="none" />
          <path v-if="item.g" class="edge-mid"
                :class="{ dim: isEdgeDim(item.e) }"
                :transform="`translate(${item.g.midX},${item.g.midY}) rotate(${item.g.midAngle})`"
                d="M -5,-4 L 5,0 L -5,4 Z" />
        </g>
        <path v-if="linking" :d="tempPath()" class="edge temp" />
        <!-- 边缘对齐辅助线 -->
        <g v-if="dragging && (snapGuides.x.length || snapGuides.y.length)" class="snap-guides" pointer-events="none">
          <line v-for="gx in snapGuides.x" :key="'gx-' + gx"
                :x1="gx" :y1="-9999" :x2="gx" :y2="9999"
                stroke="#b472ff" stroke-width="1" stroke-dasharray="4 4" opacity="0.85" />
          <line v-for="gy in snapGuides.y" :key="'gy-' + gy"
                :x1="-9999" :y1="gy" :x2="9999" :y2="gy"
                stroke="#b472ff" stroke-width="1" stroke-dasharray="4 4" opacity="0.85" />
        </g>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 Z" fill="#6ea8ff" /></marker>
        </defs>
      </svg>

      <div v-for="n in nodes" :key="n.id" class="node glass-soft"
           :class="{
             disabled: agentById(n.agent_id)?.enabled === false,
             speaking: nodeState(n.agent_id) === 'speaking',
             calling: nodeState(n.agent_id) === 'calling',
             waiting: nodeState(n.agent_id) === 'waiting',
           }"
           :style="{ left: n.x + 'px', top: n.y + 'px' }"
           @mousedown="startDrag($event, n)"
           @mouseup="endLink($event, n.id)"
           @dblclick="toggleBubble(n)"
           @contextmenu="showCtxMenu($event, n)">
        <!-- 消息气泡：双击节点可隐藏；流式输出中强制显示 -->
        <div v-if="bubbleFor(n) && (!bubbleHidden[n.id] || bubbleFor(n)?.streaming)"
             class="node-bubble" :class="{ streaming: bubbleFor(n)?.streaming }" @wheel.stop>
          <!-- 能力调用流式状态：名称已确定即显示（不等参数收齐），参数收齐后切换为"执行中" -->
          <div v-if="toolCallFor(n)" class="b-tool-status"
               :class="'b-tool-' + toolCallFor(n)!.phase">
            <span class="b-tool-ico">{{ toolCallFor(n)!.phase === 'args' ? '🔧' : '⚙️' }}</span>
            <span class="b-tool-text">
              {{ toolCallFor(n)!.phase === 'args' ? '正在调用' : '执行中' }}：
              <span class="b-tool-name">{{ toolLabel(toolCallFor(n)!.name) }}</span>
              <span v-if="toolCallFor(n)!.phase === 'args'" class="b-tool-dots">…</span>
            </span>
          </div>
          <!-- 深度思考 chunk（灰底折叠块，有区别特效） -->
          <div v-for="(chunk, ci) in bubbleFor(n)!.chunks" :key="ci"
               class="b-chunk" :class="'b-' + chunk.kind">
            <template v-if="chunk.kind === 'reasoning'">
              <details class="b-reason"><summary>🧠 思考过程</summary>{{ chunk.text }}</details>
            </template>
            <template v-else-if="chunk.kind === 'call'">
              <span class="b-call">{{ chunk.text }}</span>
            </template>
            <template v-else>
              <span class="b-text markdown" v-html="renderMarkdown(chunk.text)"></span>
            </template>
          </div>
        </div>
        <div class="node-head">
          <div class="avatar">{{ (agentById(n.agent_id)?.name || '?').slice(0,1).toUpperCase() }}</div>
          <div class="name">{{ agentById(n.agent_id)?.name || '未知' }}</div>
          <span v-if="agentById(n.agent_id)?.enabled === false" class="off-tag" title="已停用">⏸</span>
        </div>
        <div class="node-actions">
          <button class="btn xs ghost ctx-action" @click.stop="openChatPicker($event, n)">💬 对话</button>
          <button class="btn xs ghost ctx-action" @mousedown.stop @click.stop="startLink($event, n.id)">🔗 连线</button>
        </div>
      </div>
    </div>

    <!-- 画布空白区域右键菜单 -->
    <Teleport to="body">
      <div v-if="bgMenu" class="ctx-menu glass" :style="{ left: bgMenu.x + 'px', top: bgMenu.y + 'px' }" @click.stop>
        <div class="ctx-title">画布</div>
        <div class="ctx-item" @click="closeMenus(); resetView()">⟳ 重置视图</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="toggleWorkspace()">🗂 显示/隐藏工作区</div>
        <div class="ctx-item" @click="closeMenus(); wdPath = canvas?.workdir || ''; wdOpen = true">📁 设置工作区文件夹…</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="closeMenus(); hideAllBubblesForce()">🙈 隐藏所有消息泡泡</div>
        <div class="ctx-item" @click="closeMenus(); hideAllBubbles()">👁 显示所有消息泡泡</div>
      </div>
    </Teleport>

    <!-- 节点右键菜单（Teleport 到 body，fixed 定位不受画布 transform 影响） -->
    <Teleport to="body">
      <div v-if="ctxMenu" class="ctx-menu glass" :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }" @click.stop>
        <div class="ctx-title">{{ agentById(ctxMenu.node.agent_id)?.name || '未知' }}</div>
        <div class="ctx-item" @click="openAgentEdit(ctxMenu.node)">✏️ 编辑智能体…</div>
        <div class="ctx-divider" />
        <div class="ctx-item submenu" @click.stop>
          <div class="submenu-label">👁 感知范围</div>
          <div class="submenu-body">
            <div class="sub-item" :class="{ checked: perceptionScopeOf(ctxMenu.node) === 'link' }"
                 @click="setPerceptionScope(ctxMenu.node, 'link')">仅连线邻居（默认）</div>
            <div class="sub-item" :class="{ checked: perceptionScopeOf(ctxMenu.node) === 'global' }"
                 @click="setPerceptionScope(ctxMenu.node, 'global')">画布全体成员</div>
          </div>
        </div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="openTriggerDlg(ctxMenu.node)">⏱ 触发设置</div>
        <div class="ctx-item" @click="toggleEnabled(ctxMenu.node)">
          {{ agentById(ctxMenu.node.agent_id)?.enabled === false ? '▶ 启用' : '⏸ 停用' }}
        </div>
        <div class="ctx-divider" />
        <div class="ctx-item danger" @click="removeNode(ctxMenu.node.id)">🗑 从此画布移除</div>
      </div>
    </Teleport>

    <!-- AgentEditDialog（画布节点 → 编辑智能体） -->
    <AgentEditDialog v-if="editingAgent" :agent="editingAgent" @close="editingAgent = null" />

    <!-- 💬 对话模式选择器 -->
    <Teleport to="body">
      <div v-if="chatPicker" class="mode-picker glass" :style="{ left: chatPicker.x + 'px', top: chatPicker.y + 'px' }" @click.stop>
        <div class="mp-title">{{ agentById(chatPicker.node.agent_id)?.name || '未知' }} · 选择对话模式</div>
        <div class="mp-item independent" @click="doChat(chatPicker.node, 'independent')">
          <div class="mp-icon">⚡</div>
          <div class="mp-body">
            <div class="mp-label">独立对话</div>
            <div class="mp-desc">此智能体单独工作，感知不到集群其他成员</div>
          </div>
        </div>
        <div class="mp-item linked" @click="doChat(chatPicker.node, 'linked')">
          <div class="mp-icon">🌐</div>
          <div class="mp-body">
            <div class="mp-label">集群对话</div>
            <div class="mp-desc">按画布编排协作，感知节点间分工与成果</div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 触发设置对话框 -->
    <Teleport to="body">
      <div v-if="triggerDlg" class="modal-bg">
        <div class="modal glass">
          <div class="modal-title">⏱ 触发设置 · {{ agentById(triggerAgentId!)?.name || '' }}</div>
          <div class="trigger-types">
            <label class="tr-opt"><input type="radio" value="manual" v-model="triggerType" /> 手动（运行后等待触发）</label>
            <label class="tr-opt"><input type="radio" value="cron" v-model="triggerType" /> 定时（cron）</label>
            <label class="tr-opt"><input type="radio" value="message" v-model="triggerType" /> 文字触发</label>
          </div>
          <label v-if="triggerType === 'cron'" class="lbl">
            cron 表达式（分 时 日 月 周）
            <input class="input" v-model="triggerCron" placeholder="如：*/5 * * * *（每 5 分钟）" />
          </label>
          <label v-if="triggerType === 'cron' || triggerType === 'message'" class="lbl">
            触发消息{{ triggerType === 'message' ? '（包含此文字时发送）' : '（留空则默认）' }}
            <input class="input" v-model="triggerMsg"
                   :placeholder="triggerType === 'message' ? '如：开始' : '【定时触发】请执行任务'" />
          </label>
          <div class="actions">
            <button class="btn ghost" @click="triggerDlg = false">取消</button>
            <button class="btn" @click="saveTrigger">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 新建画布对话框 -->
    <Teleport to="body">
      <div v-if="newCanvasDialog" class="modal-bg">
        <div class="modal glass new-cv-modal">
          <div class="modal-title">新建画布</div>
          <label class="lbl">画布名称<input class="input" v-model="newCanvasName" placeholder="如：产品研发、营销策划" ref="r => setTimeout(() => (r as HTMLInputElement)?.focus(), 50)" /></label>
          <div class="actions">
            <button class="btn ghost" @click="newCanvasDialog = false">取消</button>
            <button class="btn" @click="commitNewCanvas">创建</button>
          </div>
        </div>
      </div>
    </Teleport>

    <div v-if="!nodes.length" class="hint">从左侧拖拽智能体到此处开始编排集群<br/>
      <span class="hint-shortcuts">滚轮缩放 · 空格+左键 或 中键 拖动画布 · 右键节点操作 · 双击连线删除</span>
    </div>

    <RulesDialog :open="rulesOpen" @close="rulesOpen = false" />

    <!-- 工作区文件夹对话框 -->
    <div v-if="wdOpen" class="modal-bg" @click.self="wdOpen = false">
      <div class="modal-card" style="width: 500px;">
        <div class="modal-title">📁 设置工作区文件夹</div>
        <div class="modal-body">
          <p class="hint">画布中所有智能体的文件操作会以此目录为 cwd，生成的文件直接存放到这里。</p>
          <div class="wd-row">
            <input v-model="wdPath" class="input wd-input" placeholder="例：C:\Users\你\Desktop\agent-out">
            <button class="btn ghost wd-browse" @click.stop="browseWorkdir" title="弹出系统文件夹选择框"
                    :disabled="browsing">📂 {{ browsing ? '…' : '浏览' }}</button>
          </div>
          <div class="wd-existing" v-if="wdPath.trim() === ''">当前无工作区 — 粘贴路径或点右边「浏览」</div>
        </div>
        <div class="modal-footer">
          <button class="btn ghost" @click.stop="wdOpen = false">取消</button>
          <button class="btn ghost danger" v-if="canvas?.workdir"
                  @click.stop="wdPath = ''; saveWorkdir()">清除</button>
          <button class="btn purple" @click.stop="saveWorkdir">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.canvas-board { flex: 1; position: relative; overflow: hidden; min-height: 0; cursor: default;
  backdrop-filter: none; -webkit-backdrop-filter: none; background: rgba(255,255,255,0.04); }
.canvas-board.panning-active { cursor: grabbing; }
.grid-bg { position: absolute; inset: 0; background-image: radial-gradient(circle, rgba(255,255,255,0.06) 1px, transparent 1px); background-size: 24px 24px; pointer-events: none; z-index: 0; }

.viewport { position: absolute; inset: 0; transform-origin: 0 0; z-index: 1; }
.viewport.will-move { will-change: transform; }

.canvas-toolbar { position: absolute; top: 10px; left: 10px; right: 10px; display: flex; justify-content: space-between; gap: 10px; z-index: 10; pointer-events: none;
  transition: max-height .3s ease, opacity .25s ease; overflow: hidden; }
.canvas-toolbar > * { pointer-events: auto; }

/* 左右两段各自独立的玻璃胶囊 */
.toolbar-left, .collapsed-bar, .toolbar-right {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 12px;
  background: rgba(255,255,255,0.06);
  backdrop-filter: blur(14px) saturate(150%);
  -webkit-backdrop-filter: blur(14px) saturate(150%);
  border: 1px solid rgba(255,255,255,0.10);
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.22);
  gap: 8px;
}
.toolbar-right { gap: 6px; }

/* 折叠态：只保留小胶囊，容器不再撑满 */
.canvas-toolbar.collapsed { justify-content: flex-start; }
.canvas-toolbar.collapsed .toolbar-right { margin-left: auto; }
.canvas-mini { font-size: 12px; font-weight: 500; opacity: .8; padding: 4px 8px; border-radius: 6px; }
.canvas-mini:hover { background: rgba(255,255,255,0.06); }
.collapse-btn { width: 26px; height: 26px; padding: 0; font-size: 13px; flex-shrink: 0; }
.canvas-name { font-size: 14px; font-weight: 600; cursor: text; padding: 4px 8px; border-radius: 6px; transition: background .15s; }
.canvas-name:hover { background: rgba(255,255,255,0.06); }
.canvas-meta { font-size: 11px; opacity: .5; font-family: Consolas, monospace; }
.rename-input { padding: 4px 10px; width: 180px; }

.zoom-indicator { font-size: 11px; font-family: Consolas, monospace; opacity: .6; padding: 0 6px; }
.zoom-indicator.panning .zoom-tip { color: #b472ff; opacity: 1; }

.edge-layer { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
.edge { fill: none; stroke: #6ea8ff; stroke-width: 2; pointer-events: stroke; cursor: pointer; stroke-dasharray: 8 6; transition: stroke .3s, stroke-width .3s, filter .3s; }
.edge.temp { stroke: rgba(180,114,255,0.7); stroke-dasharray: 3,3; animation: none; }
.edge.active { stroke: #b472ff; stroke-width: 2.5; filter: drop-shadow(0 0 6px rgba(180,114,255,0.8)); animation: edgeFlow 0.6s linear infinite; }
.edge.dim { stroke: rgba(120,130,150,0.45); stroke-width: 1.5; stroke-dasharray: 4 4; cursor: default; animation: none; filter: grayscale(0.8); }
.edge.active.dim { stroke: rgba(180,114,255,0.35); filter: grayscale(0.8) drop-shadow(0 0 4px rgba(180,114,255,0.2)); }
.edge-hit { fill: none; stroke: transparent; stroke-width: 14; pointer-events: stroke; cursor: pointer; }
.edge-hit.dim { cursor: default; }
.edge-hit.active { cursor: pointer; }
.edge-mid { fill: #6ea8ff; pointer-events: none; }
.edge-mid.dim { fill: rgba(120,130,150,0.45); }
@keyframes edgeFlow { to { stroke-dashoffset: -28; } }

.node { position: absolute; width: 180px; padding: 10px; cursor: grab; user-select: none; transition: box-shadow .2s, opacity .2s, filter .2s, border-color .25s;
  border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
  backdrop-filter: none; -webkit-backdrop-filter: none; background: rgba(20,26,44,0.72); }
.node:hover { box-shadow: 0 8px 28px rgba(110,168,255,0.25); }
.node:active { cursor: grabbing; }
.node.disabled { opacity: 0.45; filter: grayscale(0.85); }
.off-tag { margin-left: auto; font-size: 11px; opacity: .8; }

/* ====== 节点活跃状态特效 ====== */

/* 最强：正在输出 token — 紫色外发光 + 边框 + 头像呼吸 */
.node.speaking {
  border-color: rgba(180, 114, 255, 0.8);
  box-shadow: 0 0 0 2px rgba(180, 114, 255, 0.35),
              0 0 24px 6px rgba(180, 114, 255, 0.35),
              0 8px 28px rgba(180, 114, 255, 0.3);
  animation: nodePulse 1.6s ease-in-out infinite;
}
.node.speaking .avatar {
  animation: avatarGlow 1.2s ease-in-out infinite;
  box-shadow: 0 0 12px 3px rgba(180, 114, 255, 0.55);
}

/* 次要：正在发起/处理 call — 蓝色边框脉冲 */
.node.calling {
  border-color: rgba(110, 168, 255, 0.75);
  box-shadow: 0 0 0 2px rgba(110, 168, 255, 0.3),
              0 0 16px 4px rgba(110, 168, 255, 0.25);
  animation: nodePulse 2.2s ease-in-out infinite;
}

/* 最轻：集群在跑但自己闲置 — 左下角小蓝点 */
.node.waiting::after {
  content: ''; position: absolute; bottom: 6px; left: 8px;
  width: 6px; height: 6px; border-radius: 50%;
  background: #6ea8ff;
  animation: dotBlink 1.4s ease-in-out infinite;
  box-shadow: 0 0 6px 1px rgba(110, 168, 255, 0.6);
}
.node.disabled.waiting::after { display: none; }

@keyframes nodePulse {
  0%, 100% { filter: brightness(1); }
  50%      { filter: brightness(1.15); }
}
@keyframes avatarGlow {
  0%, 100% { transform: scale(1);    box-shadow: 0 0 10px 2px rgba(180, 114, 255, 0.45); }
  50%      { transform: scale(1.08); box-shadow: 0 0 18px 5px rgba(180, 114, 255, 0.7); }
}
@keyframes dotBlink {
  0%, 100% { opacity: 0.35; transform: scale(0.8); }
  50%      { opacity: 1;    transform: scale(1.15); }
}

.node-bubble { position: absolute; bottom: calc(100% + 12px); left: 50%; transform: translateX(-50%); width: 230px; padding: 8px 10px; font-size: 11.5px; line-height: 1.5; color: #dfe6f2; white-space: pre-wrap; word-break: break-word; background: rgba(18, 24, 38, 0.94); border: 1px solid rgba(110, 168, 255, 0.4); border-radius: 10px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45); max-height: 140px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; pointer-events: auto; z-index: 6; cursor: default; scrollbar-width: thin; scrollbar-color: rgba(110,168,255,0.5) transparent; animation: bubbleIn 0.25s ease; }
.node-bubble::-webkit-scrollbar { width: 6px; }
.node-bubble::-webkit-scrollbar-track { background: transparent; }
.node-bubble::-webkit-scrollbar-thumb { background: rgba(110,168,255,0.45); border-radius: 3px; }
.node-bubble::-webkit-scrollbar-thumb:hover { background: rgba(110,168,255,0.7); }
.node-bubble::after { content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 6px solid transparent; border-top-color: rgba(110, 168, 255, 0.4); }
.node-bubble.streaming { border-color: rgba(180, 114, 255, 0.65); }
.node-bubble.streaming::after { border-top-color: rgba(180, 114, 255, 0.65); }
@keyframes bubbleIn { from { opacity: 0; transform: translateX(-50%) translateY(6px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }

/* —— 气泡 chunk 样式 —— */
.b-chunk + .b-chunk { margin-top: 6px; }
.b-text { display: block; white-space: pre-wrap; }
.b-call { display: block; font-size: 11px; color: #d4a8ff; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
/* 深度思考 — 灰底 + 可折叠 + 展开后有区别的斜体/低对比度 */
.b-reason { background: rgba(255,255,255,0.035); border: 1px dashed rgba(255,255,255,0.15); border-radius: 6px; padding: 3px 6px; font-size: 10.5px; color: rgba(232,236,244,0.55); line-height: 1.45; }
.b-reason summary { cursor: pointer; user-select: none; font-size: 10.5px; color: rgba(232,236,244,0.55); padding: 0 0 2px; font-style: italic; }
.b-reason summary:hover { color: rgba(232,236,244,0.8); }
.b-reason summary::-webkit-details-marker { display: none; }
.b-reason::before { content: '…'; font-style: italic; }
.b-reason[open]::before { content: ''; }
.b-reason[open] summary { border-bottom: 1px dashed rgba(255,255,255,0.1); margin-bottom: 4px; padding-bottom: 2px; }

/* —— 能力调用流式状态条 —— */
.b-tool-status {
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; line-height: 1.4; border-radius: 6px; padding: 4px 6px;
  margin-bottom: 6px;
}
.b-tool-status + .b-chunk { margin-top: 0; }
/* 参数还在流式：蓝色脉动，提示"正在调用" */
.b-tool-args {
  background: rgba(110,168,255,0.12); border: 1px solid rgba(110,168,255,0.45);
  color: #a8c8ff;
}
.b-tool-args .b-tool-dots { animation: toolDots 1s steps(2) infinite; }
/* 参数收齐、执行中：紫色态 */
.b-tool-exec {
  background: rgba(180,114,255,0.14); border: 1px solid rgba(180,114,255,0.45);
  color: #d4a8ff;
}
.b-tool-ico { flex-shrink: 0; }
.b-tool-name { font-weight: 600; }
.b-tool-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.b-tool-dots { color: inherit; }
@keyframes toolDots { 50% { opacity: 0.25; } }
.bubble.streaming .b-tool-args { animation: toolPulse 1.4s ease-in-out infinite; }
@keyframes toolPulse {
  0%, 100% { border-color: rgba(110,168,255,0.45); }
  50%      { border-color: rgba(110,168,255,0.85); }
}

/* —— 气泡内 Markdown —— */
.b-text.markdown { white-space: normal; word-break: break-word; }
.b-text.markdown > :first-child { margin-top: 0; }
.b-text.markdown > :last-child { margin-bottom: 0; }
.b-text.markdown p { margin: 0 0 5px; }
.b-text.markdown p:last-child { margin-bottom: 0; }
.b-text.markdown h1, .b-text.markdown h2, .b-text.markdown h3,
.b-text.markdown h4, .b-text.markdown h5, .b-text.markdown h6 {
  font-size: 12px; font-weight: 600; margin: 6px 0 3px; line-height: 1.35;
}
.b-text.markdown ul, .b-text.markdown ol { margin: 3px 0 5px; padding-left: 16px; }
.b-text.markdown li { margin: 1px 0; }
.b-text.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: 10.5px;
  background: rgba(0,0,0,0.3); padding: 0 3px; border-radius: 3px;
}
.b-text.markdown pre {
  background: rgba(0,0,0,0.35); border-radius: 5px; padding: 5px 7px;
  margin: 5px 0; overflow-x: auto; white-space: pre; font-size: 10.5px;
}
.b-text.markdown pre code { background: none; padding: 0; color: #e8ecf4; }
.b-text.markdown blockquote {
  border-left: 2px solid rgba(110,168,255,0.4); padding: 1px 7px; margin: 4px 0;
  color: rgba(232,236,244,0.75);
}
.b-text.markdown blockquote p { margin: 0; }
.b-text.markdown a { color: #a8c8ff; text-decoration: underline; text-underline-offset: 2px; }
.b-text.markdown hr { border: none; border-top: 1px solid rgba(255,255,255,0.12); margin: 6px 0; }
.b-text.markdown table { border-collapse: collapse; margin: 4px 0; font-size: 10.5px; }
.b-text.markdown th, .b-text.markdown td {
  border: 1px solid rgba(255,255,255,0.12); padding: 2px 6px;
}
.b-text.markdown th { background: rgba(255,255,255,0.06); }
.b-text.markdown strong { font-weight: 600; }
.b-text.markdown em { font-style: italic; }
.b-text.markdown img { max-width: 100%; border-radius: 5px; margin: 3px 0; }

.node-head { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.avatar { width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, #6ea8ff, #b472ff); display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; }
.name { font-size: 13px; font-weight: 500; }
.node-actions { display: flex; gap: 4px; }
.node .btn { backdrop-filter: none; -webkit-backdrop-filter: none; }
.xs { padding: 3px 8px; font-size: 11px; }

.hint { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); text-align: center; opacity: .5; font-size: 13px; line-height: 1.8; max-width: 420px; z-index: 5; pointer-events: none; }
.hint-shortcuts { display: block; margin-top: 10px; font-size: 11px; opacity: .7; }

/* 右键菜单 */
.ctx-menu { position: fixed; min-width: 220px; padding: 6px; z-index: 9999; box-shadow: 0 12px 40px rgba(0,0,0,0.5); }
.ctx-title { font-size: 11px; padding: 6px 10px; color: rgba(232,236,244,0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 4px; }
.ctx-item { padding: 8px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; transition: background .15s; }
.ctx-item:hover { background: rgba(110,168,255,0.18); }
.ctx-item.danger:hover { background: rgba(255,80,80,0.18); color: #ff9090; }
.ctx-divider { height: 1px; background: rgba(255,255,255,0.08); margin: 4px 6px; }
.ctx-item.submenu { display: flex; flex-direction: column; gap: 2px; }
.submenu-label { padding: 0; font-size: 13px; cursor: default; }
.submenu-body { display: flex; flex-direction: column; gap: 1px; margin-top: 2px; }
.sub-item { padding: 6px 10px; border-radius: 5px; font-size: 12px; color: rgba(232,236,244,0.75); cursor: pointer; transition: background .15s; }
.sub-item:hover { background: rgba(180,114,255,0.15); color: #d4a8ff; }
.sub-item.checked { color: #d4a8ff; background: rgba(180,114,255,0.12); }
.sub-item.checked::before { content: '✓ '; color: #b472ff; }

/* 💬 模式选择器 */
.mode-picker { position: fixed; min-width: 260px; padding: 10px; z-index: 9998; box-shadow: 0 12px 40px rgba(0,0,0,0.5); }
.mp-title { font-size: 11px; padding: 4px 6px 8px; color: rgba(232,236,244,0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 6px; }
.mp-item { display: flex; gap: 10px; padding: 10px; border-radius: 8px; cursor: pointer; font-size: 12px; margin-bottom: 4px; border: 1px solid transparent; transition: all .15s;
  .independent:hover { background: rgba(100,220,160,0.15); border-color: rgba(100,220,160,0.3); }
  .linked:hover { background: rgba(180,114,255,0.15); border-color: rgba(180,114,255,0.3); }
}
.mp-icon { font-size: 20px; flex-shrink: 0; }
.mp-label { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
.mp-desc { font-size: 11px; opacity: .55; line-height: 1.4; }

/* 新建画布对话框 */
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 360px; padding: 20px; }
.new-cv-modal .lbl { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: rgba(232,236,244,0.7); }
.modal-title { font-size: 15px; font-weight: 600; margin-bottom: 14px; }
.modal-body { display: flex; flex-direction: column; gap: 10px; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.modal-card { background: rgba(18,24,38,0.98); border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; padding: 22px 24px; box-shadow: 0 24px 64px rgba(0,0,0,0.55); }
.wd-row { display: flex; gap: 8px; align-items: stretch; }
.wd-input { flex: 1; width: auto; padding: 8px 10px; font-size: 12.5px; font-family: Consolas, monospace; }
.wd-browse { flex-shrink: 0; padding: 0 12px; height: 34px; font-size: 12px; }
.wd-existing { font-size: 11px; color: rgba(232,236,244,0.4); padding: 4px 2px; }
.btn.activeWd { background: rgba(100,220,160,0.15); border-color: rgba(100,220,160,0.4); color: #80e4b8; }
.modal-body .hint { position: static; transform: none; font-size: 12px; opacity: .7; line-height: 1.6; pointer-events: auto; text-align: left; max-width: none; margin: 0; padding: 0; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }

/* 运行/停止 + 触发 */
.run-btn.stop { background: rgba(255,80,80,0.15); border-color: rgba(255,80,80,0.4); color: #ff9090; }
.run-btn.stop:hover { background: rgba(255,80,80,0.25); }
.run-btn:not(.stop) { background: rgba(100,220,160,0.12); border-color: rgba(100,220,160,0.35); color: #80e4b8; }
.run-btn:not(.stop):hover { background: rgba(100,220,160,0.22); }
/* 串行/并行切换 */
.em-collapsed { font-size: 14px; padding: 0 8px; line-height: 24px; }
.em-collapsed:hover { background: rgba(180,114,255,0.15); color: #d4a8ff; }
.exec-mode-toggle { display: flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 8px; background: rgba(255,255,255,0.04); cursor: pointer; }
.em-label { font-size: 11px; opacity: 0.45; transition: opacity 0.2s, color 0.2s; white-space: nowrap; }
.em-label.active { opacity: 1; color: #d4a8ff; font-weight: 500; }
.em-switch { position: relative; width: 32px; height: 16px; display: inline-block; flex-shrink: 0; }
.em-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.em-slider { position: absolute; inset: 0; background: rgba(255,255,255,0.15); border-radius: 16px; cursor: pointer; transition: background 0.25s; }
.em-slider::before { content: ''; position: absolute; width: 12px; height: 12px; left: 2px; top: 2px; background: #fff; border-radius: 50%; transition: transform 0.25s; }
.em-switch input:checked + .em-slider { background: rgba(180,114,255,0.6); }
.em-switch input:checked + .em-slider::before { transform: translateX(16px); }
.em-switch:hover .em-slider { background: rgba(255,255,255,0.25); }
.em-switch input:checked:hover + .em-slider { background: rgba(180,114,255,0.75); }
.trigger-types { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.tr-opt { display: flex; gap: 8px; align-items: center; font-size: 13px; cursor: pointer; color: rgba(232,236,244,0.85); }
.modal .lbl { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: rgba(232,236,244,0.7); margin-bottom: 10px; }
</style>

<!-- 全局（非 scoped）样式：v-html 注入的 markdown 元素无法被 scoped 选择器命中，必须用全局样式 -->
<style>
/* —— 气泡内 Markdown —— */
.b-text.markdown { white-space: normal; word-break: break-word; }
.b-text.markdown > :first-child { margin-top: 0; }
.b-text.markdown > :last-child { margin-bottom: 0; }
.b-text.markdown p { margin: 0 0 5px; }
.b-text.markdown p:last-child { margin-bottom: 0; }
.b-text.markdown h1, .b-text.markdown h2, .b-text.markdown h3,
.b-text.markdown h4, .b-text.markdown h5, .b-text.markdown h6 {
  font-size: 12px; font-weight: 600; margin: 6px 0 3px; line-height: 1.35;
}
.b-text.markdown h1:first-child, .b-text.markdown h2:first-child,
.b-text.markdown h3:first-child { margin-top: 0; }
.b-text.markdown ul, .b-text.markdown ol { margin: 3px 0 5px; padding-left: 16px; }
.b-text.markdown li { margin: 1px 0; }
.b-text.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: 10.5px;
  background: rgba(0,0,0,0.3); padding: 0 3px; border-radius: 3px;
}
.b-text.markdown pre {
  background: rgba(0,0,0,0.35); border-radius: 5px; padding: 5px 7px;
  margin: 5px 0; overflow-x: auto; white-space: pre; font-size: 10.5px;
}
.b-text.markdown pre code { background: none; padding: 0; color: #e8ecf4; }
.b-text.markdown blockquote {
  border-left: 2px solid rgba(110,168,255,0.4); padding: 1px 7px; margin: 4px 0;
  color: rgba(232,236,244,0.75);
}
.b-text.markdown blockquote p { margin: 0; }
.b-text.markdown a { color: #a8c8ff; text-decoration: underline; text-underline-offset: 2px; }
.b-text.markdown hr { border: none; border-top: 1px solid rgba(255,255,255,0.12); margin: 6px 0; }
.b-text.markdown table { border-collapse: collapse; margin: 4px 0; font-size: 10.5px; }
.b-text.markdown th, .b-text.markdown td {
  border: 1px solid rgba(255,255,255,0.12); padding: 2px 6px;
}
.b-text.markdown th { background: rgba(255,255,255,0.06); }
.b-text.markdown strong { font-weight: 600; }
.b-text.markdown em { font-style: italic; }
.b-text.markdown img { max-width: 100%; border-radius: 5px; margin: 3px 0; }
</style>
