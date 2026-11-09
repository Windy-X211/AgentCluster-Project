<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount, onDeactivated } from 'vue'
import { useCanvasStore, useAgentStore, useConvStore, useSettingsStore, useLayoutStore } from '@/stores'
import { api } from '@/api'
import { toolLabel, extractToolFileName, cyclicWindow, stripContinueTag, type ToolCallPhase } from '@/stores/conversation'
import { renderMarkdown } from '@/utils/markdown'
import { getAvatarGradient, getAvatarRgb, getPaletteRgbPool, generateRgb, agentAvatarGradient, hexToRgbString } from '@/utils/avatar'

const canvases = useCanvasStore()
const agents = useAgentStore()
const convs = useConvStore()
const settings = useSettingsStore()
const layout = useLayoutStore()
import AgentEditDialog from './AgentEditDialog.vue'
import RulesDialog from './RulesDialog.vue'
import ColorSpectrumPopover from './ColorSpectrumPopover.vue'

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
const MIN_ZOOM = 0.3, MAX_ZOOM = 2.5, ZOOM_STEP = 1.25

// 连线箭头的反向缩放系数：画布缩小（CSS zoom < 1）时按比例放大箭头在画布内的尺寸，
// 让箭头的屏幕像素尺寸基本恒定。缩到 30% 仍保留约 0.75 倍大小（12–14px），
// 一眼能看清连线方向；放大时略微收缩，避免箭头相对节点过于夸张。
const arrowScale = computed(() => Math.min(2.5, Math.max(0.7, 1 / Math.max(scale.value, 0.001))))

// —— 画布坐标基础类型 / 连线形状 / 控制点 ——
type Pt = { x: number; y: number }
/** curve=平滑曲线 | straight=斜线直连 | ortho=直角折线 | mixed=逐段绘制（每段线型可不同） */
type EdgeShape = 'curve' | 'straight' | 'ortho' | 'mixed'
type EdgeHandle = {
  kind: 'point' | 'insert' | 'anchor' | 'vertex' | 'ctrl' | 'bulge'
  index: number
  x: number; y: number
  nx?: number; ny?: number
  end?: AnchorEnd
  /** 分段连线的段序号；存在时这个柄属于 segments 而不是 waypoints */
  seg?: number
  /** ctrl 柄对应的控制点下标（0/1） */
  ctrl?: number
}
/** 分段连线的一段线型：直线 / 平滑曲线（三次贝塞尔）/ 圆弧 / 垂线（垂直于上一段末端） */
type SegType = 'curve' | 'straight' | 'arc' | 'vert'
/** 落点确认后的一段：end 为段终点，ctrl 为曲线控制点（垂线段用 ctrl[0] 存单位法线方向），
 *  bulge 为圆弧带符号归一化矢高。 */
type EdgeSegment = { type: SegType; end: Pt; ctrl: Pt[]; bulge: number }
type DrawState = {
  from: number
  /** 起点锚点；未确认第一段前按鼠标方向动态取最近边点，确认后冻结 */
  anchor: AnchorSpec
  mouse: Pt
  type: SegType
  /** 右键呼出线型菜单时的画布坐标：决定当前段曲线/圆弧的弯向 */
  via: Pt | null
  segs: EdgeSegment[]
}
/** 手动锚点：吸附在节点某条边上，t 为沿边归一化位置（0..1，已含离角留白）。
 *  用「边 + 比例」而不是绝对坐标存储，节点移动后锚点自动跟着走、不会脱边。 */
type AnchorEnd = 'from' | 'to'
type AnchorSpec = { side: OrthoSide; t: number }

// 节点拖动（group 非空时表示 Ctrl 整体拖动）
const dragging = ref<{
  node: any
  dx: number
  dy: number
  x0: number
  y0: number
  group?: { node: any; x0: number; y0: number }[]
} | null>(null)
// 逐段绘制连线：左键落点确认一段、右键切换该段线型、点击节点结束、Esc 取消
const draw = ref<DrawState | null>(null)
// 绘制中的「本段线型」右键菜单（客户端坐标 + 右键时的画布坐标）
const segMenu = ref<{ x: number; y: number; via: Pt } | null>(null)
// 画布平移（左键）
const panning = ref<{ sx: number; sy: number; ox: number; oy: number } | null>(null)
const spaceDown = ref(false)

// 套索 / 多选
const selectedNodeIds = ref<Set<number>>(new Set())
const lasso = ref<{
  x0: number; y0: number; x1: number; y1: number
  originNodeId: number | null   // Shift+单击起点落在哪个节点上（用于点选切换）
  moved: boolean                // 是否已超过套索拖动阈值
  additive: boolean             // Shift+Ctrl 拖拽：连续框选（把命中节点加入现有选区）
} | null>(null)
// 拖动开始时备份相关连线的控制点；拖动过程中按总位移重算，避免累积误差
let dragWaypoints: { edge: any; points: Pt[] }[] = []
// 分段连线的顶点 / 曲线控制点同样是画布绝对坐标，节点移动时一并平移
let dragSegments: { edge: any; segs: { end: Pt; ctrl: Pt[]; type?: string }[] }[] = []
// 拖动结束后的 click 不应清空选区
let suppressBoardClick = false

// 连线控制点
const hoveringEdgeId = ref<number | null>(null)
/** 鼠标在画布坐标系里的位置：锚点只在鼠标靠近时才显现，避免整张画布都是小圆点 */
const pointerCanvas = ref<Pt | null>(null)
const edgeDrag = ref<{
  edge: any
  index: number
  anchor?: AnchorEnd
  seg?: number
  field?: 'vertex' | 'ctrl' | 'bulge'
  ctrl?: number
} | null>(null)

// —— 撤销 / 重做：只记录画布图结构（节点 / 连线 / 默认曲直） ——
type CanvasSnapshot = { nodes: any[]; edges: any[]; edge_shape: EdgeShape }
const undoStack = ref<CanvasSnapshot[]>([])
const redoStack = ref<CanvasSnapshot[]>([])
const HISTORY_LIMIT = 50
let pendingDragSnapshot: CanvasSnapshot | null = null
let pendingEdgeSnapshot: CanvasSnapshot | null = null

const ctxMenu = ref<{ node: any; x: number; y: number } | null>(null)
// 画布空白区域右键菜单
const bgMenu = ref<{ x: number; y: number } | null>(null)
// 连线右键菜单
const edgeMenu = ref<{ edge: any; x: number; y: number } | null>(null)
// 消息气泡：每个节点的隐藏状态（按画布 id 持久化在画布的 meta 字段里）
const bubbleHidden = ref<Record<number, boolean>>({})
const renameEdit = ref(false)
const renameValue = ref('')
const newCanvasDialog = ref(false)
const newCanvasName = ref('')
// 「用选中智能体创建画布」时暂存节点/连线
const pendingCanvasPayload = ref<{ nodes: any[]; edges: any[] } | null>(null)
const toolbarCollapsed = ref(true)
const rulesOpen = ref(false)
const wdOpen = ref(false)
const wdPath = ref('')
const browsing = ref(false)

// AgentEditDialog 打开状态
const editingAgent = ref<any>(null)

// 头像渐变色（根据avatar_mode决定）—— 统一用 agent_id 作为 hash 种子，与对话区保持一致
// 用户在画布调色板里手动指定过颜色的智能体，其自定义色优先于「统一/差异化」两种全局风格
const avatarGradient = computed(() => {
  const mode = settings.data.avatar_mode || 'differentiated'
  return (agentId: number) => {
    const a = agentById(agentId)
    if (a?.avatar_color) return agentAvatarGradient(a, mode)
    if (mode === 'unified') {
      return 'linear-gradient(135deg, var(--accent), var(--accent-2))'
    }
    return getAvatarGradient(agentId)
  }
})

// —— 画布调色板：选中颜色 → 点击智能体节点 → 改该智能体的头像颜色 ——
// pickedColor: null = 未选色；'' = 恢复默认配色；'#rrggbb' = 指定色
// 浮窗即「配色模式」的开关：关闭浮窗 = 退出配色（待应用色一并清除）
const paletteOpen = ref(false)
// 折叠态 / 展开态两处按钮互斥渲染，共用同一个 ref
const paletteAnchor = ref<HTMLElement | null>(null)
const palettePos = ref({ x: 0, y: 0 })
// 用户是否手动拖动过浮窗（拖过则下次打开沿用其位置，而不是回到按钮下方）
const palettePosUserSet = ref(false)
const pickedColor = ref<string | null>(null)
// 上次选过的颜色：浮窗关闭后待应用态被清除，但重新打开时色谱仍停在上次的位置
const lastPickedColor = ref<string | null>(null)
// 悬停预览：选色后鼠标悬停节点头像时实时预览该色
const hoverNodeId = ref<number | null>(null)
// 刚应用完颜色的时间戳：避免紧接着的双击误触发「编辑智能体 / 切换泡泡」
let colorAppliedAt = 0

// 打开/关闭调色板浮窗（首次定位在按钮下方；用户拖动过则沿用其位置）
function togglePalette() {
  if (paletteOpen.value) { closePalette(); return }
  if (!palettePosUserSet.value) {
    const el = paletteAnchor.value
    const r = el?.getBoundingClientRect()
    palettePos.value = { x: r ? r.left : 16, y: r ? r.bottom + 8 : 56 }
  }
  paletteOpen.value = true
}
// 浮窗被拖动 → 记住新位置（下次打开沿用）
function onPaletteMoved(pos: { x: number; y: number }) {
  palettePos.value = pos
  palettePosUserSet.value = true
}
// 选色（无需确认，实时生效）：进入「待应用」状态，等待用户点击智能体节点
function onColorChanged(hex: string) {
  pickedColor.value = hex || ''
  lastPickedColor.value = hex || ''
  hoverNodeId.value = null
}
// 关闭浮窗 = 停止配色：一并清除待应用色，点节点恢复正常行为
function closePalette() {
  paletteOpen.value = false
  cancelPickedColor()
}
// 浮窗自身发出的关闭事件（✕ / 底部「关闭」）
function onPaletteClosed() {
  closePalette()
}
// 取消「待应用」状态
function cancelPickedColor() {
  if (pickedColor.value === null) return
  pickedColor.value = null
  hoverNodeId.value = null
}
// 选色后点击节点：把颜色写到该智能体上（持久化到后端）
async function applyColorToNode(node: any) {
  const color = pickedColor.value
  if (color === null) return
  const a = agentById(node.agent_id)
  if (!a) return
  colorAppliedAt = Date.now()
  try {
    await agents.setAvatarColor(a.id, color)
    showToast(color
      ? `已将「${a.name}」的头像色改为 ${color.toUpperCase()}`
      : `已恢复「${a.name}」的默认头像色`)
  } catch (e: any) {
    showToast('设置头像颜色失败：' + (e?.message || e))
  }
}
// 节点头像背景：选色待应用时，悬停的节点实时预览该色
function nodeAvatarBg(node: any): string {
  if (pickedColor.value !== null && hoverNodeId.value === node.id) {
    return pickedColor.value
      ? agentAvatarGradient({ avatar_color: pickedColor.value })
      : avatarGradient.value(node.agent_id)
  }
  return avatarGradient.value(node.agent_id)
}

// 触发设置
const triggerDlg = ref(false)
const triggerAgentId = ref<number | null>(null)
const triggerType = ref<'manual' | 'cron'>('manual')
const triggerCron = ref('')
const triggerMsg = ref('')

const canvas = computed(() => canvases.current)
const nodes = computed(() => canvas.value?.nodes || [])
const edges = computed(() => canvas.value?.edges || [])

// —— 节点消息气泡：取本画布的会话（优先当前选中的，否则该画布最近活跃的）——
const bubbleConv = computed(() => {
  const cid = canvas.value?.id
  if (!cid) return null
  const cur = convs.currentData()
  if (cur && (cur.canvas_id ?? null) === cid) return cur
  const list = convs.list.filter((c: any) => (c.canvas_id ?? null) === cid)
  return list.length ? list[0] : null   // list 按活跃度降序：最近活跃的在最前
})

type BubbleChunk = {
  kind: 'reasoning' | 'call' | 'text' | 'tool'
  text?: string
  streaming?: boolean        // reasoning 专用：正在流式 → 单行流水；否则不显示
  toolName?: string
  toolRunning?: boolean
  toolArgs?: any
  toolResult?: any
  toolPhase?: 'args' | 'exec'
  toolFileName?: string      // 文件操作类工具：文件名
  toolStreamingArgs?: string // 参数流式中：累计的原始 JSON 片段
}

/** 预计算所有 agent 的气泡数据（单次遍历消息，避免 N×O(M) 的模板内重复计算） */
const bubbleMap = computed<Map<number, { chunks: BubbleChunk[]; streaming: boolean } | null>>(() => {
  const map = new Map<number, { chunks: BubbleChunk[]; streaming: boolean } | null>()
  const conv = bubbleConv.value
  if (!conv) return map
  const msgs = conv.messages || []
  const _stripAnno = (s: string) => (s || '').replace(/^\[[^\]]+\]\s*/, '').trim()

  // 预构建 tool_result 索引：tcId → result，以及有哪些 tcId 有结果
  const resultMap = new Map<string, any>()
  const hasResultSet = new Set<string>()
  for (const m of msgs) {
    if (m.role === 'tool_result' && m.tool_call_id) {
      hasResultSet.add(m.tool_call_id)
      resultMap.set(m.tool_call_id, m.result)
    }
  }

  // 按发送方 agent_id 收集 A2A 信封目标（用于去重 send_message 工具调用）
  const a2aTargetsBySender = new Map<number, Set<string>>()
  for (const m of msgs) {
    if (m.role === 'a2a') {
      const env = (m as any).envelope || {}
      const fromAgent = (m as any).from_agent_id ?? env?.from?.agent_id
      if (fromAgent != null) {
        let set = a2aTargetsBySender.get(fromAgent)
        if (!set) { set = new Set(); a2aTargetsBySender.set(fromAgent, set) }
        if (env?.to?.name) set.add(String(env.to.name))
        if (env?.to?.agent_id) set.add(String(env.to.agent_id))
      }
    }
  }

  // 遍历一次消息，按 agent_id 分组 chunks
  const perChunks = new Map<number, BubbleChunk[]>()
  const perStreaming = new Map<number, boolean>()
  const ensure = (aid: number) => {
    if (!perChunks.has(aid)) { perChunks.set(aid, []); perStreaming.set(aid, false) }
  }

  for (const m of msgs) {
    const aid = (m as any).agent_id
    if (aid == null) continue
    ensure(aid)
    const chunks = perChunks.get(aid)!

    // 1. 自己的深度思考
    if (m.role === 'assistant' && m.streaming && (m as any).reasoning?.trim()) {
      chunks.push({ kind: 'reasoning', text: _stripAnno(stripContinueTag((m as any).reasoning)), streaming: true })
      perStreaming.set(aid, true)
    }

    // 2. cluster.send_message（成功产生 A2A 信封则跳过）
    if (m.role === 'tool_call' && (m as any).name === 'cluster.send_message') {
      const args = (m as any).args || {}
      const toName = args.to_agent != null ? String(args.to_agent) : '?'
      const msg = (args.message || '').trim()
      if (msg && !a2aTargetsBySender.get(aid)?.has(toName)) {
        chunks.push({ kind: 'call', text: `📤 → ${toName}：${msg}` })
      }
    }

    // 2b. 其他工具调用
    if (m.role === 'tool_call' && (m as any).name !== 'cluster.send_message') {
      const tcId = (m as any).tool_call_id
      const running = !hasResultSet.has(tcId)
      const args = (m as any).args || {}
      chunks.push({
        kind: 'tool',
        toolName: toolLabel((m as any).name, args),
        toolRunning: running,
        toolArgs: args,
        toolResult: resultMap.get(tcId),
        toolFileName: extractToolFileName((m as any).name, args),
      })
    }

    // 3. 正文回复
    if (m.role === 'assistant' && (m.content || '').trim()) {
      chunks.push({ kind: 'text', text: stripContinueTag(m.content).trim() })
      if (m.streaming) perStreaming.set(aid, true)
    }

    // 4. 自己发出的 A2A 信封
    if (m.role === 'a2a') {
      const env = (m as any).envelope || {}
      const fromAgent = (m as any).from_agent_id ?? env?.from?.agent_id
      if (fromAgent === aid) {
        const toName = env?.to?.name || String(env?.to?.agent_id || '?')
        const text = (env?.parts || [])
          .filter((p: any) => p?.type === 'text' && p?.text)
          .map((p: any) => p.text)
          .join('\n')
        if (text) chunks.push({ kind: 'call', text: `📤 我 → ${toName}：${_stripAnno(text)}` })
      }
    }
  }

  // 附加 liveTC（流式工具调用，还没 push 到 msgs 的瞬间）
  const isCurStreaming = convs.streaming && conv.id === convs.current
  if (isCurStreaming) {
    for (const [aidStr, liveTC] of Object.entries(convs.toolCalls)) {
      const aid = Number(aidStr)
      if (!Number.isFinite(aid)) continue
      ensure(aid)
      const chunks = perChunks.get(aid)!
      const hasLive = chunks.some(c => c.kind === 'tool' && c.toolName === toolLabel(liveTC.name, liveTC.streamingArgs) && c.toolRunning)
      if (!hasLive) {
        chunks.push({
          kind: 'tool',
          toolName: toolLabel(liveTC.name, liveTC.streamingArgs),
          toolRunning: true,
          toolArgs: {},
          toolPhase: liveTC.phase,
          toolStreamingArgs: liveTC.streamingArgs,
        })
        perStreaming.set(aid, true)
      }
    }
  }

  // 构建最终 map
  for (const [aid, chunks] of perChunks) {
    const hasStrm = perStreaming.get(aid) || false
    if (chunks.length) {
      map.set(aid, { chunks, streaming: hasStrm })
    } else if (isCurStreaming && convs.speakingAgents.has(aid)) {
      map.set(aid, { chunks: [], streaming: true })
    } else {
      map.set(aid, null)
    }
  }

  return map
})

/** 模板内读取：按 node.agent_id 取气泡数据（O(1) Map.get，替代原来 O(M) 的 bubbleFor） */
function getBubbleForAgent(agentId: number) {
  return bubbleMap.value.get(agentId) ?? null
}

/**
 * 气泡防重叠布局：为每个可见气泡决定方向（上方/下方）。
 *
 * 根因：气泡宽度 230px > 节点宽度 180px，固定在节点正上方水平居中，
 * 多个智能体并行回复时，相邻节点的气泡包围盒极易相交 → 视觉重叠。
 *
 * 算法：按节点位置（x+y）升序贪心放置。每个气泡默认上方；若与已放置
 * 气泡包围盒相交则尝试下方；若两方向都相交，选相交面积较小的方向，
 * 并由 streaming 的 z-index 抬升保证流式气泡不被遮挡。
 *
 * 包围盒用气泡最大高度 140px 做保守预测（实际高度 ≤ 140）。
 */
const BUBBLE_W = 230, BUBBLE_H = 140, BUBBLE_GAP = 12
const bubbleLayout = computed<Map<number, 'top' | 'bottom'>>(() => {
  const layout = new Map<number, 'top' | 'bottom'>()
  const visible = nodes.value.filter((n: any) =>
    getBubbleForAgent(n.agent_id) && !bubbleHidden.value[n.id])
  if (visible.length <= 1) {
    if (visible.length === 1) layout.set(visible[0].id, 'top')
    return layout
  }
  // 包围盒工厂：dir='top' 气泡在节点上方，'bottom' 在下方
  const rect = (n: any, dir: 'top' | 'bottom') => {
    const left = n.x + NODE_W / 2 - BUBBLE_W / 2
    if (dir === 'top') return { l: left, r: left + BUBBLE_W, t: n.y - BUBBLE_GAP - BUBBLE_H, b: n.y - BUBBLE_GAP }
    return { l: left, r: left + BUBBLE_W, t: n.y + NODE_H + BUBBLE_GAP, b: n.y + NODE_H + BUBBLE_GAP + BUBBLE_H }
  }
  const intersects = (a: any, b: any) => a.l < b.r && a.r > b.l && a.t < b.b && a.b > b.t
  const overlapArea = (a: any, b: any) => {
    const dx = Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l))
    const dy = Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t))
    return dx * dy
  }
  // 按位置排序，从左上到右下贪心放置，减少后续冲突
  const ordered = [...visible].sort((a: any, b: any) => (a.x + a.y) - (b.x + b.y))
  const placed: { id: number; r: any }[] = []
  for (const n of ordered) {
    const rTop = rect(n, 'top')
    const rBot = rect(n, 'bottom')
    const hitTop = placed.some(p => intersects(p.r, rTop))
    const hitBot = placed.some(p => intersects(p.r, rBot))
    let dir: 'top' | 'bottom'
    if (!hitTop) dir = 'top'
    else if (!hitBot) dir = 'bottom'
    else {
      // 两方向都冲突：选与已放置气泡总重叠面积较小者
      const aTop = placed.reduce((s, p) => s + overlapArea(p.r, rTop), 0)
      const aBot = placed.reduce((s, p) => s + overlapArea(p.r, rBot), 0)
      dir = aTop <= aBot ? 'top' : 'bottom'
    }
    layout.set(n.id, dir)
    placed.push({ id: n.id, r: dir === 'top' ? rTop : rBot })
  }
  return layout
})

/** 模板内读取气泡方向 */
function bubbleDir(nodeId: number): 'top' | 'bottom' {
  return bubbleLayout.value.get(nodeId) ?? 'top'
}

function clipBubble(t: string): string {
  const s = (t || '').replace(/^\s*\n+/, '').replace(/\n+\s*$/, '').replace(/\s+/g, ' ').trim()
  return s.length > 150 ? s.slice(0, 150) + '…' : s
}

// 运行/流式状态按画布隔离：只有"运行中对话所属画布"才显示运行态
const runningCanvasId = computed<number | null>(() => {
  if (convs.running) return convs.runCanvasId ?? null
  if (convs.streaming) return convs.currentData()?.canvas_id ?? null
  return null
})

/** 画布上所有 agent_id 集合（判断"此画布是否有运行 agent"） */
const canvasAgentIds = computed<Set<number>>(() => {
  const s = new Set<number>()
  for (const n of (canvas.value?.nodes || [])) if (n?.agent_id != null) s.add(n.agent_id)
  return s
})

const runningOnThisCanvas = computed(() => {
  const cid = canvas.value?.id
  if (cid == null) return false
  // 常规路径：运行中的会话绑定了本画布
  if (runningCanvasId.value === cid) return true
  // 兜底：会话没绑 canvas_id，但 speakingAgents 里有此画布上的 agent
  if (convs.streaming) {
    const sp = convs.speakingAgents
    if (sp && sp.size > 0) {
      for (const aid of sp) if (canvasAgentIds.value.has(aid)) return true
    }
  }
  return false
})

// 节点活跃状态 → 用于视觉特效
// 优先级：speaking(正在输出 token) > calling(正在发起/处理 call) > waiting(集群在跑但自己闲置)
// 并行模式下可同时有多个节点处于 speaking（一个发送方同时唤起 N 个接收方）
function nodeState(agentId: number): 'speaking' | 'calling' | 'waiting' | null {
  if (!runningOnThisCanvas.value) return null
  if (convs.streaming && convs.speakingAgents.has(agentId)) return 'speaking'
  const callEdges = convs.callEdges || []
  if (callEdges.some(d => d.from === agentId || d.to === agentId)) return 'calling'
  return 'waiting'
}

// —— 连线配色：按「源智能体」定纯色 ——
// 规则：同一个智能体引出的所有连线同色；不同智能体引出的线不同色。
// 基础色仍取身份色（与头像同源），再与主题强调色混合以适配配色方案；
// 同一画布内若多个源智能体撞色，则依次顺延到调色板中未占用的色，保证「异源异色」。
// 用户手动指定过头像色的智能体：连线跟随其自定义色（保持"头像与连线同源"），
// 且该色优先占位，其余智能体顺延时避开它。
const edgeSourceColors = computed(() => {
  const map = new Map<number, string>()
  if ((settings.data.avatar_mode || 'differentiated') === 'unified') return map
  // 收集本画布所有连线源端对应的智能体（按 id 排序 → 分配结果稳定可预期）
  const srcAgents = new Set<number>()
  for (const e of edges.value) {
    const n = nodeById.value.get(e.from_node)
    if (n && n.agent_id != null) srcAgents.add(n.agent_id)
  }
  const sorted = [...srcAgents].sort((a, b) => a - b)
  const pool = getPaletteRgbPool()
  const used = new Set<string>()
  // ① 自定义头像色的智能体先占位（显式设置优先，不参与顺延改写）
  for (const aid of sorted) {
    const custom = agentById(aid)?.avatar_color
    if (!custom) continue
    const rgb = hexToRgbString(custom)
    used.add(rgb)
    map.set(aid, rgb)
  }
  // ② 其余按身份色分配；被占用则顺延取调色板中首个未用色
  for (const aid of sorted) {
    if (map.has(aid)) continue
    let color = getAvatarRgb(aid)
    if (used.has(color)) {
      const free = pool.find(c => !used.has(c))
      color = free ?? generateRgb(sorted.indexOf(aid))
    }
    used.add(color)
    map.set(aid, color)
  }
  return map
})
/** 连线颜色：只由源智能体决定（与目标智能体无关） */
function edgeSourceColor(e: any): string {
  const n = nodeById.value.get(e.from_node)
  const aid = n?.agent_id
  if (aid == null) return 'var(--accent)'
  return edgeSourceColors.value.get(aid) ?? 'var(--accent)'
}

// —— 查找表 computed：避免每次 O(nodes) 线性查找 ——
const nodeById = computed(() => {
  const m = new Map<number, any>()
  for (const n of nodes.value) m.set(n.id, n)
  return m
})
// (fromAgentId, toAgentId) → 该画布上是否有正向连线，用于判断 callEdge 匹配方向
const forwardAgentPairs = computed(() => {
  const set = new Set<string>()
  for (const e of edges.value) {
    const f = nodeById.value.get(e.from_node)
    const t = nodeById.value.get(e.to_node)
    if (f && t) set.add(`${f.agent_id}→${t.agent_id}`)
  }
  return set
})

// call 进行中：仅「调用方 → 对方」的一条连线流动；对方开始输出后（store 移除该条）结束
function isEdgeActive(e: any): boolean {
  if (!runningOnThisCanvas.value) return false
  if (!convs.streaming || !convs.callEdges.length) return false
  const from = nodeById.value.get(e.from_node)
  const to = nodeById.value.get(e.to_node)
  if (!from || !to) return false
  const fromAid = from.agent_id
  const toAid = to.agent_id
  return convs.callEdges.some(d => {
    const key = `${d.from}→${d.to}`
    const hasForward = forwardAgentPairs.value.has(key)
    if (hasForward) return fromAid === d.from && toAid === d.to
    return (fromAid === d.from && toAid === d.to) ||
           (fromAid === d.to && toAid === d.from)
  })
}

function isEdgeDim(e: any): boolean {
  const from = nodeById.value.get(e.from_node)
  const to = nodeById.value.get(e.to_node)
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
  // 注：监听以 passive 注册，preventDefault 无效也无需（board overflow:hidden 无默认滚动）
  // 同一帧内的多次滚轮增量合并（高分辨率滚轮一帧可触发多次 wheel）：
  // zoom 是布局级缩放，每次 scale 变化都要全量重排，逐 tick 提交 = 一帧多次全量布局 → 卡顿
  // 滚轮灵敏度：每档缩放 ±25%（Figma/Excalidraw 同级手感）
  const factor = -e.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP
  pendingZoom = { mx: e.clientX, my: e.clientY, factor: (pendingZoom?.factor ?? 1) * factor }
  if (!zoomRafId) zoomRafId = requestAnimationFrame(applyPendingZoom)
}

// 每帧最多提交一次缩放：锚点取帧内最后一次鼠标位置，factor 累乘
let pendingZoom: { mx: number; my: number; factor: number } | null = null
let zoomRafId = 0
function applyPendingZoom() {
  zoomRafId = 0
  const p = pendingZoom
  pendingZoom = null
  if (!p) return
  const rect = boardRef.value!.getBoundingClientRect()
  const mx = p.mx - rect.left
  const my = p.my - rect.top

  const prev = scale.value
  const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev * p.factor))
  if (next === prev) return

  // 以鼠标位置为锚点：缩放后鼠标下的画布点保持不动
  const newTx = mx - (mx - tx.value) * (next / prev)
  const newTy = my - (my - ty.value) * (next / prev)
  scale.value = next
  tx.value = newTx
  ty.value = newTy
  syncGrid()
}

// —— 画布平移 / 套索 ——
// 文字始终清晰方案：平移走外层 .pan-layer 的 transform（纯 translate，不缩放纹理），
// 缩放走内层 .viewport 的 CSS zoom（布局级缩放，浏览器按当前缩放的真实字号排版并矢量光栅化），
// 任何 scale 下文字都是原生渲染，无需 will-change 挂载/释放的重光栅化过渡
const PAN_IGNORE = '.canvas-toolbar, .ws-panel, .ws-trash, .ws-ctx, .ws-modal-bg, .ctx-menu, .modal-bg, .conv-float, .hint, .btn, input, label, select, textarea, button, a'
function startPan(e: MouseEvent) {
  if (e.button !== 0) return
  // 绘制中左键一律用于落点/结束连线（空格+拖拽仍可平移视图）
  if (draw.value && !spaceDown.value) return
  suppressBoardClick = false
  const target = e.target as HTMLElement
  if (target?.closest(PAN_IGNORE)) return
  if (e.shiftKey) {
    // Shift+左键 → 套索 / 点选；再按住 Ctrl → 连续框选（累加选区）
    const c = clientToCanvas(e.clientX, e.clientY)
    const nodeEl = target?.closest('.node') as HTMLElement | null
    const rawId = nodeEl?.dataset?.nodeId
    const idNum = rawId != null && rawId !== '' ? Number(rawId) : NaN
    lasso.value = {
      x0: c.x, y0: c.y, x1: c.x, y1: c.y,
      originNodeId: Number.isFinite(idNum) ? idNum : null,
      moved: false,
      additive: !!(e.ctrlKey || e.metaKey),
    }
    e.preventDefault()
    return
  }
  if (target?.closest('.node, .edge-hit, .edge-handle-hit, .edge-handle')) return
  panning.value = { sx: e.clientX, sy: e.clientY, ox: tx.value, oy: ty.value }
  e.preventDefault()
}
function doPan(e: MouseEvent) {
  if (!panning.value) return
  tx.value = panning.value.ox + (e.clientX - panning.value.sx)
  ty.value = panning.value.oy + (e.clientY - panning.value.sy)
  syncGrid()
  suppressBoardClick = true
}
function endPan() { if (panning.value) { panning.value = null } }

function updateLasso(c: Pt) {
  const l = lasso.value
  if (!l) return
  const moved = l.moved || Math.hypot(c.x - l.x0, c.y - l.y0) * scale.value >= 4
  lasso.value = { ...l, x1: c.x, y1: c.y, moved }
}
function finishLasso() {
  const l = lasso.value
  if (!l) return
  lasso.value = null
  const dx = l.x1 - l.x0, dy = l.y1 - l.y0
  const moved = l.moved || Math.max(Math.abs(dx), Math.abs(dy)) * scale.value >= 4
  if (!moved) {
    // Shift+单击：点中节点 → 切换选中；Shift+Ctrl+单击 → 只增选不取消；点空白/连线 → 保持当前选区
    if (l.originNodeId != null) {
      if (l.additive) {
        const next = new Set(selectedNodeIds.value)
        next.add(l.originNodeId)
        selectedNodeIds.value = next
      } else {
        toggleNodeSelection(l.originNodeId)
      }
    }
    closeMenus()
    return
  }
  const minX = Math.min(l.x0, l.x1), maxX = Math.max(l.x0, l.x1)
  const minY = Math.min(l.y0, l.y1), maxY = Math.max(l.y0, l.y1)
  // 左→右：完全包含才选中；右→左：碰到即选中
  const contain = dx >= 0
  const hits = nodes.value.filter((n: any) => contain
    ? (n.x >= minX && n.x + NODE_W <= maxX && n.y >= minY && n.y + NODE_H <= maxY)
    : (n.x < maxX && n.x + NODE_W > minX && n.y < maxY && n.y + NODE_H > minY))
  if (l.additive) {
    // Shift+Ctrl：连续框选，把本轮命中节点并入现有选区
    const next = new Set(selectedNodeIds.value)
    for (const n of hits) next.add(n.id)
    selectedNodeIds.value = next
  } else {
    selectedNodeIds.value = new Set(hits.map((n: any) => n.id))
  }
  closeMenus()
}
const lassoRect = computed(() => {
  const l = lasso.value
  if (!l) return null
  const x = Math.min(l.x0, l.x1), y = Math.min(l.y0, l.y1)
  const contain = l.x1 >= l.x0
  return {
    left: `${x}px`,
    top: `${y}px`,
    width: `${Math.abs(l.x1 - l.x0)}px`,
    height: `${Math.abs(l.y1 - l.y0)}px`,
    class: contain ? 'contain' : 'touch',
    additive: l.additive,
  }
})

// 网格跟随平移/缩放 → 画布视觉上无限大
const gridRef = ref<HTMLElement | null>(null)
function syncGrid() {
  const g = gridRef.value
  if (!g) return
  g.style.backgroundPosition = `${tx.value}px ${ty.value}px`
  const size = 24 * scale.value
  g.style.backgroundSize = `${size}px ${size}px`
}

// —— zoom 缩放下的滚动位置锁定 ——
// 锁定方式是"相对滚动进度"而非 ratio 乘法：Chromium 对 CSS zoom 下 scroll offset 的
// 语义（layout 坐标自动保持 vs 需按视觉比例补偿）并不统一，ratio 乘法在其中一种语义下
// 是双重补偿，反而制造滚动。按 rel = scrollTop/(scrollHeight-clientHeight) 记录并在
// 新 zoom 布局后按新 max 恢复，两种语义下都精确锁定视觉阅读位置。
// 回写会派发 scroll 事件 → 用 suppressBubbleScroll 标志让 onBubbleScroll 忽略，
// 否则被误判"用户上滑"，5s 后 _scheduleResume 强拉底部造成二次跳动
const ZOOM_SCROLLABLES = '.node-bubble, .ws-body, .wt-body, .ws-console-output, .fv-body, .fv-table-wrap'
let suppressBubbleScroll = false
watch(scale, (next, prev) => {
  if (!prev || next === prev) return
  const vp = viewportRef.value
  if (!vp) return
  const saved: { el: HTMLElement; rel: number }[] = []
  vp.querySelectorAll<HTMLElement>(ZOOM_SCROLLABLES).forEach(el => {
    const max = el.scrollHeight - el.clientHeight
    if (max > 0) saved.push({ el, rel: el.scrollTop / max })
  })
  nextTick(() => {
    saved.forEach(({ el, rel }) => {
      const max = el.scrollHeight - el.clientHeight
      if (max > 0) el.scrollTop = rel * max
    })
    suppressBubbleScroll = true
    requestAnimationFrame(() => { suppressBubbleScroll = false })
  })
})

// —— 键盘 / 快捷键 ——
function isEditableTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el) return false
  const tag = (el.tagName || '').toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}
function hasTextSelection(): boolean {
  const sel = window.getSelection()
  return !!sel && !sel.isCollapsed && !!sel.toString().trim()
}
function isModalOpen(): boolean {
  return newCanvasDialog.value || wdOpen.value || rulesOpen.value || triggerDlg.value || !!editingAgent.value
}
// 监听空格 + 画布快捷键（复制/剪切/粘贴、撤销/重做、删除）
const onKeyDown = (e: KeyboardEvent) => {
  const editable = isEditableTarget(e.target)
  const modal = isModalOpen()
  if (e.code === 'Space' && !editable) spaceDown.value = true
  const mod = e.ctrlKey || e.metaKey
  if (mod && !e.altKey) {
    const k = e.key.toLowerCase()
    // Ctrl+A 全选节点；输入框/模态框内交还系统默认的“全选文本”
    if (k === 'a' && !e.shiftKey) {
      if (editable || modal) return
      e.preventDefault()
      selectAllNodes()
      return
    }
    // Ctrl+Z 撤销 / Ctrl+Y、Ctrl+Shift+Z 重做；输入框/模态框内交还系统默认行为
    if (k === 'z' || k === 'y') {
      if (editable || modal) return
      e.preventDefault()
      if (k === 'y' || e.shiftKey) redoCanvas()
      else undoCanvas()
      return
    }
    if (!e.shiftKey && (k === 'c' || k === 'x' || k === 'v')) {
      // 输入框 / 页面文字选区 / 模态框内：交还给系统默认行为
      if (editable || hasTextSelection() || modal) return
      e.preventDefault()
      if (k === 'c') copySelection()
      else if (k === 'x') cutSelection()
      else pasteClipboard()
      return
    }
  }
  // Delete / Backspace 删除选中的智能体；输入框 / 模态框内保持系统默认
  if ((e.key === 'Delete' || e.key === 'Backspace') && !editable && !modal) {
    if (e.key === 'Backspace') e.preventDefault()  // 避免浏览器后退
    if (selectedNodeIds.value.size) {
      e.preventDefault()
      deleteSelectedNodes()
    }
    return
  }
  if (e.code === 'Escape' && !editable) {
    // 绘制连线中：Esc 优先取消整条未完成的连线
    if (draw.value || segMenu.value) { cancelDraw(); return }
    // Esc = 彻底退出调色（关浮窗 + 清待应用色）
    if (paletteOpen.value || pickedColor.value !== null) {
      closePalette()
      return
    }
    if (lasso.value) { lasso.value = null; return }
    clearSelection()
    closeMenus()
  }
}
const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') spaceDown.value = false }

// —— 节点拖放 / 画布拖入 ——
function onDrop(e: DragEvent) {
  e.preventDefault()
  if (!canvas.value) return
  const dt = e.dataTransfer
  const dragCanvasId = dt?.getData('canvas')
  if (dragCanvasId) {
    // 从右侧画布列表拖入：复用该画布的节点与内部连线
    e.stopPropagation()
    const src = canvases.list.find((c: any) => String(c.id) === dragCanvasId)
    if (src) importCanvasPart(src, clientToCanvas(e.clientX, e.clientY))
    return
  }
  const data = dt?.getData('agent'); if (!data) return
  const agent = JSON.parse(data)
  const { x, y } = clientToCanvas(e.clientX, e.clientY)
  const nextId = Math.max(0, ...nodes.value.map((n: any) => n.id)) + 1
  pushHistory()
  canvas.value.nodes.push({ id: nextId, agent_id: agent.id, x: x - 90, y: y - 40 })
  canvases.saveCurrent()
}

function startDrag(e: MouseEvent, node: any) {
  // 逐段绘制中：点节点是「结束连线」，不能顺手把节点拖走
  if (draw.value) return
  if ((e.target as HTMLElement).closest('.ctx-action, button, label, input')) return
  if (spaceDown.value || e.button !== 0) return
  if (e.shiftKey) return   // Shift 拖动交给画布套索
  // 已选色待应用：点击节点 = 把颜色应用到该智能体（不进入拖拽）
  if (pickedColor.value !== null) { applyColorToNode(node); return }
  const c = clientToCanvas(e.clientX, e.clientY)
  const withCtrl = e.ctrlKey || e.metaKey
  const inSelection = selectedNodeIds.value.has(node.id)
  const multiSelected = selectedNodeIds.value.size > 1 && inSelection
  // 普通点击/拖动：把选区收窄到当前节点（Ctrl 保持选区，用于整体拖动）
  if (!withCtrl) selectedNodeIds.value = new Set([node.id])
  const group = withCtrl && multiSelected
    ? [...selectedNodeIds.value]
        .map(id => nodeById.value.get(id))
        .filter((n: any) => !!n)
        .map((n: any) => ({ node: n, x0: n.x, y0: n.y }))
    : undefined
  pendingDragSnapshot = snapshotCanvas()
  // 被移动节点（组）关联的连线控制点：备份原始坐标，拖动中按总位移重算 → 控制点跟着节点走
  const movingIds = group
    ? new Set(group.map(s => s.node.id))
    : new Set<number>([node.id])
  dragWaypoints = edges.value
    .filter((e: any) =>
      (movingIds.has(e.from_node) || movingIds.has(e.to_node)) &&
      Array.isArray(e.waypoints) && e.waypoints.length)
    .map((e: any) => ({
      edge: e,
      points: e.waypoints.map((p: any) => ({ x: p.x, y: p.y })),
    }))
  dragSegments = edges.value
    .filter((e: any) =>
      (movingIds.has(e.from_node) || movingIds.has(e.to_node)) &&
      Array.isArray(e.segments) && e.segments.length)
    .map((e: any) => ({
      edge: e,
      segs: e.segments.map((s: any) => ({
        end: { x: Number(s?.end?.x) || 0, y: Number(s?.end?.y) || 0 },
        // 垂线段的 ctrl[0] 是单位法线（方向），不是坐标：节点平移时不能跟着加位移
        type: s?.type === 'vert' ? 'vert' : '',
        ctrl: Array.isArray(s?.ctrl) ? s.ctrl.map((p: any) => ({ x: Number(p?.x) || 0, y: Number(p?.y) || 0 })) : [],
      })),
    }))
  dragging.value = { node, dx: c.x - node.x, dy: c.y - node.y, x0: node.x, y0: node.y, group }
}
/** 节点拖动时：把关联连线的控制点整体平移同样的画布位移 */
function translateDragWaypoints(dx: number, dy: number) {
  if (!dx && !dy) return
  for (const item of dragWaypoints) {
    const wps = item.edge.waypoints
    if (!Array.isArray(wps)) continue
    const n = Math.min(wps.length, item.points.length)
    for (let i = 0; i < n; i++) {
      const p = wps[i]
      if (!p || typeof p !== 'object') continue
      p.x = item.points[i].x + dx
      p.y = item.points[i].y + dy
    }
  }
  for (const item of dragSegments) {
    const segs = item.edge.segments
    if (!Array.isArray(segs)) continue
    const n = Math.min(segs.length, item.segs.length)
    for (let i = 0; i < n; i++) {
      const s = segs[i], snap = item.segs[i]
      if (!s || typeof s !== 'object') continue
      if (!s.end || typeof s.end !== 'object') s.end = {}
      s.end.x = snap.end.x + dx
      s.end.y = snap.end.y + dy
      if (snap.type === 'vert') continue   // 方向向量不参与平移
      if (!Array.isArray(s.ctrl)) s.ctrl = []
      for (let k = 0; k < Math.min(s.ctrl.length, snap.ctrl.length); k++) {
        if (!s.ctrl[k] || typeof s.ctrl[k] !== 'object') s.ctrl[k] = {}
        s.ctrl[k].x = snap.ctrl[k].x + dx
        s.ctrl[k].y = snap.ctrl[k].y + dy
      }
    }
  }
}
function onMouseMove(e: MouseEvent) {
  doPan(e)
  const c = clientToCanvas(e.clientX, e.clientY)
  pointerCanvas.value = c
  if (lasso.value) { updateLasso(c); suppressBoardClick = true }
  if (dragging.value) {
    // 第一次真正移动时才入栈：单击节点不产生空的撤销步骤
    if (pendingDragSnapshot) { pushSnapshot(pendingDragSnapshot); pendingDragSnapshot = null }
    const nx = c.x - dragging.value.dx
    const ny = c.y - dragging.value.dy
    const group = dragging.value.group
    const exclude = group ? new Set(group.map(s => s.node.id)) : undefined
    const snapped = snapToEdges(dragging.value.node, nx, ny, scale.value, exclude)
    const baseX = dragging.value.x0
    const baseY = dragging.value.y0
    if (group) {
      // 以被拖节点的吸附结果为准，整组按同一位移移动（相对拖动起点的总位移）
      const dx = snapped.x - baseX
      const dy = snapped.y - baseY
      for (const s of group) { s.node.x = s.x0 + dx; s.node.y = s.y0 + dy }
    } else {
      dragging.value.node.x = snapped.x
      dragging.value.node.y = snapped.y
    }
    // 连线控制点跟随节点位移
    translateDragWaypoints(snapped.x - baseX, snapped.y - baseY)
    suppressBoardClick = true
  }
  if (edgeDrag.value) {
    if (pendingEdgeSnapshot) { pushSnapshot(pendingEdgeSnapshot); pendingEdgeSnapshot = null }
    moveEdgeWaypoint(c)
    suppressBoardClick = true
  }
  // 线型菜单打开期间冻结预览终点，避免鼠标移到菜单上时预览乱跳
  if (draw.value && !segMenu.value) { draw.value.mouse = c }
}
function onMouseUpGlobal() {
  // 绘制模式是「点击式」的，鼠标抬起不能清空状态；只有落点 / 结束 / Esc 才结束
  endDrag(); endPan(); finishLasso(); endEdgeDrag()
}
function endDrag() {
  pendingDragSnapshot = null
  const wasDragging = !!dragging.value
  dragWaypoints = []
  dragSegments = []
  if (wasDragging) {
    dragging.value = null
    // 节点移动后化简 ortho 折线：只处理「用户手动设过拐点」的线。
    // 自动布线的线必须保留 waypoints 为空，否则渲染拐点被固化成手动控制点，
    // 之后顺/逆时针偏好会被这些硬约束锁死，方向切换就再也换不到另一侧。
    for (const e of edges.value) {
      if (edgeShapeOf(e) === 'ortho' && hasWaypoints(e)) commitOrthoGeometry(e, false)
    }
    canvases.saveCurrent()
  }
  snapGuides.value = { x: [], y: [] }
}

// —— 选区 ——
function clearSelection() { selectedNodeIds.value = new Set() }
// Ctrl+A：全选当前画布的所有节点
function selectAllNodes() {
  selectedNodeIds.value = new Set(nodes.value.map((n: any) => n.id))
}
function toggleNodeSelection(id: number) {
  const next = new Set(selectedNodeIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedNodeIds.value = next
}
function selectionNodes(): any[] {
  const ids = selectedNodeIds.value
  return ids.size ? nodes.value.filter((n: any) => ids.has(n.id)) : []
}
function deleteSelectedNodes() {
  if (!canvas.value || !selectedNodeIds.value.size) return
  pushHistory()
  const ids = new Set(selectedNodeIds.value)
  canvas.value.nodes = nodes.value.filter((n: any) => !ids.has(n.id))
  canvas.value.edges = edges.value.filter((e: any) => !ids.has(e.from_node) && !ids.has(e.to_node))
  selectedNodeIds.value = new Set()
  closeMenus()
  canvases.saveCurrent()
}
// 点击空白 / 拖动结束后的 click：只有"没有发生拖动"的空白点击才清空选区
function onBoardClick(e: MouseEvent) {
  // 逐段绘制中：空白左键 = 确认本段终点（节点上的点击由节点自己的处理器结束连线）
  if (draw.value) {
    suppressBoardClick = false
    if (e.button === 0) confirmDrawSegment(clientToCanvas(e.clientX, e.clientY))
    return
  }
  const swallow = suppressBoardClick
  suppressBoardClick = false
  closeMenus()
  if (swallow) return
  const target = e.target as HTMLElement
  if (target?.closest('.node, .edge-hit, .edge-handle-hit, .edge-handle, .canvas-toolbar, .ctx-menu, .modal-bg, .conv-float')) return
  // 按住 Shift 点击空白不取消选中（Shift 期间用于增选/点选切换）
  if (e.shiftKey) return
  clearSelection()
}

// 双击画布空白区：折叠智能体列表 + 收窄右侧对话区（再次双击恢复）。
// 命中节点 / 连线 / 控制柄 / 工具栏 / 菜单 / 气泡等交互元素时不触发。
function onBoardDblClick(e: MouseEvent) {
  const target = e.target as Element | null
  if (!target) return
  if (target.closest('.node, .edge-hit, .edge-handle-hit, .edge-handle, .edge-handle-group, .canvas-toolbar, .ctx-menu, .modal-bg, .conv-float, .node-bubble, .draw-hint, .exec-mode-toast, .selection-pill')) return
  layout.toggleFocusMode()
}

// —— 撤销 / 重做 ——
function snapshotCanvas(): CanvasSnapshot {
  return {
    nodes: JSON.parse(JSON.stringify(nodes.value || [])),
    edges: JSON.parse(JSON.stringify(edges.value || [])),
    edge_shape: defaultEdgeShape(),
  }
}
function pushSnapshot(snap: CanvasSnapshot) {
  const last = undoStack.value[undoStack.value.length - 1]
  if (last && JSON.stringify(last) === JSON.stringify(snap)) return
  undoStack.value.push(snap)
  if (undoStack.value.length > HISTORY_LIMIT) undoStack.value.shift()
  redoStack.value = []
}
function pushHistory() { if (canvas.value) pushSnapshot(snapshotCanvas()) }
function restoreSnapshot(snap: CanvasSnapshot) {
  if (!canvas.value) return
  canvas.value.nodes = JSON.parse(JSON.stringify(snap.nodes))
  canvas.value.edges = JSON.parse(JSON.stringify(snap.edges))
  canvas.value.edge_shape = snap.edge_shape
    pendingDragSnapshot = null
    pendingEdgeSnapshot = null
    dragWaypoints = []
    dragSegments = []
    cancelDraw()
    dragging.value = null
  edgeDrag.value = null
  lasso.value = null
  selectedNodeIds.value = new Set()
  hoveringEdgeId.value = null
  snapGuides.value = { x: [], y: [] }
  closeMenus()
  canvases.saveCurrent()
}
function undoCanvas() {
  if (!undoStack.value.length || !canvas.value) return
  const prev = undoStack.value.pop()!
  redoStack.value.push(snapshotCanvas())
  restoreSnapshot(prev)
  showToast('已撤销')
}
function redoCanvas() {
  if (!redoStack.value.length || !canvas.value) return
  const next = redoStack.value.pop()!
  undoStack.value.push(snapshotCanvas())
  if (undoStack.value.length > HISTORY_LIMIT) undoStack.value.shift()
  restoreSnapshot(next)
  showToast('已重做')
}

// —— 逐段绘制连线：按钮起笔 → 右键选本段线型 → 左键落点确认 → 继续画 → 点节点收笔 ——
/** 点节点上的「连线」按钮：以距离鼠标最近的边上点作为起点锚点开始绘制 */
function startLink(e: MouseEvent, nodeId: number) {
  e.stopPropagation()
  const node = nodeById.value.get(nodeId)
  if (!node) return
  const c = clientToCanvas(e.clientX, e.clientY)
  draw.value = {
    from: nodeId,
    anchor: nearestMidAnchor(nodeRect(node), c),
    mouse: c,
    type: 'curve',
    via: null,
    segs: [],
  }
  segMenu.value = null
  closeMenus()
  showToast('绘制连线：左键落点确认一段 · 右键切换线型 · 点击节点结束 · Esc 取消')
}
/** 未确认第一段之前，起点锚点跟随鼠标方向动态取最近边点；第一段确认后冻结 */
function drawStartSpec(d: DrawState): AnchorSpec {
  if (d.segs.length) return d.anchor
  const n = nodeById.value.get(d.from)
  return n ? nearestMidAnchor(nodeRect(n), d.mouse) : d.anchor
}
function drawStartPoint(d: DrawState): Pt {
  const n = nodeById.value.get(d.from)
  return n ? anchorPoint(drawStartSpec(d), nodeRect(n)) : d.mouse
}
/** 已确认各段串起来后，末尾处的单位切向。第一段没有上一段，用起点锚点的出边法线。 */
function drawTailTangent(d: DrawState): Pt {
  let tan = anchorNormal(drawStartSpec(d).side)
  let cur = drawStartPoint(d)
  for (const s of d.segs) {
    const normal = s.type === 'vert' ? normalOf(tan) : null
    const built = buildSegment(cur, s.end, s.type, s.ctrl, s.bulge, normal)
    tan = segEndTangent(cur, s.end, s.type, s.ctrl, s.bulge, built)
    cur = s.end
  }
  return tan
}
/** 预览终点：悬停在别的节点上时贴到该节点「朝向源节点那一侧的边中点」 */
function drawPendingEnd(d: DrawState): Pt {
  const hid = hoverNodeId.value
  if (hid != null && hid !== d.from) {
    const n = nodeById.value.get(hid)
    const src = nodeById.value.get(d.from)
    if (n && src) {
      const srcCenter = { x: src.x + NODE_W / 2, y: src.y + NODE_H / 2 }
      return anchorPoint(facingMidAnchor(nodeRect(n), srcCenter), nodeRect(n))
    }
  }
  return d.mouse
}
/** 左键落点：确认当前段，并把线型弯向烘焙进段数据；随后自动开始下一段（沿用当前线型） */
function confirmDrawSegment(c: Pt) {
  const d = draw.value
  if (!d) return
  d.mouse = c
  segMenu.value = null
  // 第二段起的起点是上一落点，不是节点锚点：曲线控制点 / 垂线方向都依赖正确的 p0
  const p0 = d.segs.length ? d.segs[d.segs.length - 1].end : drawStartPoint(d)
  if (Math.hypot(c.x - p0.x, c.y - p0.y) < SEG_MIN_LEN) return
  const normal = d.type === 'vert' ? normalOf(drawTailTangent(d)) : null
  if (normal) {
    const q = projectOnNormal(p0, c, normal)
    if (Math.hypot(q.x - p0.x, q.y - p0.y) < SEG_MIN_LEN) return
  }
  d.anchor = drawStartSpec(d)   // 冻结起点锚点
  // 第一段紧贴源节点锚点：曲线控制点沿出边法线外推，确保从节点外部指向锚点
  const n0 = d.segs.length === 0 ? anchorNormal(drawStartSpec(d).side) : null
  d.segs.push(makeDrawSegment(p0, c, d.type, d.via, normal, n0, null))
  d.via = null
}
/** 点击目标节点收笔：以距离鼠标最近的边上点作为终点锚点 */
function finishDraw(toNodeId: number | null, c: Pt) {
  const d = draw.value
  if (!d || toNodeId == null || !canvas.value) { cancelDraw(); return }
  const fromNode = nodeById.value.get(d.from), toNode = nodeById.value.get(toNodeId)
  if (!fromNode || !toNode || toNodeId === d.from) {
    showToast('请选择另一个节点作为终点')
    return
  }
  d.mouse = c
  const rf = nodeRect(fromNode), rt = nodeRect(toNode)
  const startSpec = drawStartSpec(d)
  const srcCenter = { x: rf.l + NODE_W / 2, y: rf.t + NODE_H / 2 }
  const endSpec = facingMidAnchor(rt, srcCenter)
  const p0 = d.segs.length ? d.segs[d.segs.length - 1].end : anchorPoint(startSpec, rf)
  const p1 = anchorPoint(endSpec, rt)
  const segs = d.segs.map(cloneSegment)
  if (Math.hypot(p1.x - p0.x, p1.y - p0.y) >= 2) {
    const normal = d.type === 'vert' ? normalOf(drawTailTangent(d)) : null
    // 首末段紧贴两端锚点：曲线控制点分别沿源/目标出边法线外推，两端都从节点外部指向锚点
    const n0 = d.segs.length === 0 ? anchorNormal(startSpec.side) : null
    const n1 = anchorNormal(endSpec.side)
    segs.push(makeDrawSegment(p0, p1, d.type, d.via, normal, n0, n1))
  }
  if (!segs.length) { cancelDraw(); showToast('连线太短，未创建'); return }
  segs[segs.length - 1].end = { x: p1.x, y: p1.y }
  const nextId = Math.max(0, ...edges.value.map((n: any) => n.id)) + 1
  pushHistory()
  canvas.value.edges.push({
    id: nextId,
    from_node: d.from,
    to_node: toNodeId,
    type: 'chat',
    shape: 'mixed',
    waypoints: [],
    ortho_dir: null,
    anchor_from: startSpec,
    anchor_to: endSpec,
    segments: segs.map(s => ({
      type: s.type,
      end: { x: s.end.x, y: s.end.y },
      ctrl: s.ctrl.map(p => ({ x: p.x, y: p.y })),
      bulge: s.bulge,
    })),
  })
  const count = segs.length
  cancelDraw()
  canvases.saveCurrent()
  showToast(`已创建 ${count} 段连线`)
}
function cancelDraw(notify = false) {
  draw.value = null
  segMenu.value = null
  if (notify) showToast('已取消绘制连线')
}
/** 绘制中点节点 = 收笔（阻止冒泡，避免被当成空白落点再确认一段） */
function onNodeClickInDraw(e: MouseEvent, nodeId: number) {
  if (!draw.value) return
  e.stopPropagation()
  if (e.button === 0) finishDraw(nodeId, clientToCanvas(e.clientX, e.clientY))
}
/** 绘制中的右键：呼出「本段线型」菜单，同时记下右键位置作为曲线/圆弧的弯向参考 */
function openSegMenu(e: MouseEvent) {
  e.preventDefault(); e.stopPropagation()
  ctxMenu.value = null; bgMenu.value = null; edgeMenu.value = null
  segMenu.value = { x: e.clientX, y: e.clientY, via: clientToCanvas(e.clientX, e.clientY) }
  clampToViewport(segMenu as any, '.seg-menu')
}
function chooseSegType(t: SegType) {
  if (draw.value) {
    draw.value.type = t
    draw.value.via = segMenu.value?.via || null
  }
  segMenu.value = null
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
  if (draw.value) { openSegMenu(e); return }
  e.preventDefault(); e.stopPropagation()
  // 右键未选中的节点时，先把选区收到该节点，保证复制/剪切目标明确
  if (!selectedNodeIds.value.has(node.id)) selectedNodeIds.value = new Set([node.id])
  ctxMenu.value = { node, x: e.clientX, y: e.clientY }
  clampToViewport(ctxMenu, '.ctx-menu')
  bgMenu.value = null
  edgeMenu.value = null
}
function showBgMenu(e: MouseEvent) {
  if (draw.value) { openSegMenu(e); return }
  // 仅在真正空白（没有点到节点/连线/工具栏/浮层）时弹出
  const target = e.target as HTMLElement
  if (target?.closest('.node, .edge, .edge-hit, .edge-mid, .edge-handle, .edge-handle-hit, .canvas-toolbar, .hint, .canvas-mini, .collapse-btn')) return
  if (target?.closest('.ctx-menu, .ctx-item, .modal-bg, .conv-float')) return  // 浮层自己会处理，别拦
  e.preventDefault()
  ctxMenu.value = null
  edgeMenu.value = null
  bgMenu.value = { x: e.clientX, y: e.clientY }
  clampToViewport(bgMenu as any, '.ctx-menu')
}
function showEdgeMenu(e: MouseEvent, edge: any) {
  if (draw.value) { openSegMenu(e); return }
  e.preventDefault(); e.stopPropagation()
  ctxMenu.value = null
  bgMenu.value = null
  edgeMenu.value = { edge, x: e.clientX, y: e.clientY }
  clampToViewport(edgeMenu as any, '.ctx-menu')
}
function closeMenus() {
  ctxMenu.value = null; bgMenu.value = null; edgeMenu.value = null; segMenu.value = null
}

// —— 剪贴板：复制 / 剪切 / 粘贴 ——
/** 分段路径都是画布绝对坐标：复制时按整体位移平移顶点与曲线控制点。
 *  垂线段的 ctrl[0] 是单位法线（方向向量），只拷贝不平移。 */
function mapSegments(e: any, dx = 0, dy = 0) {
  return readSegments(e).map(s => ({
    type: s.type,
    end: { x: s.end.x + dx, y: s.end.y + dy },
    ctrl: s.ctrl.map(p => s.type === 'vert'
      ? { x: p.x, y: p.y }
      : { x: p.x + dx, y: p.y + dy }),
    bulge: s.bulge,
  }))
}
function copySelection(silent = false): boolean {
  const sel = selectionNodes()
  if (!sel.length) return false
  const index = new Map<number, number>(sel.map((n: any, i: number) => [n.id, i]))
  const innerEdges = edges.value.filter((e: any) => index.has(e.from_node) && index.has(e.to_node))
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of sel) {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y)
    maxX = Math.max(maxX, n.x + NODE_W); maxY = Math.max(maxY, n.y + NODE_H)
  }
  canvases.clipboard = {
    nodes: sel.map((n: any) => ({ agent_id: n.agent_id, x: n.x, y: n.y })),
    edges: innerEdges.map((e: any) => ({
      from: index.get(e.from_node)!,
      to: index.get(e.to_node)!,
      type: e.type || 'chat',
      shape: edgeShapeOf(e),
      ortho_dir: orthoDirOf(e),
      anchor_from: anchorSpecOf(e, 'from'),
      anchor_to: anchorSpecOf(e, 'to'),
      segments: mapSegments(e),
      waypoints: sanitizeWaypoints(e.waypoints).map(p => ({ x: p.x, y: p.y })),
    })),
    bbox: { x: minX, y: minY, w: Math.max(NODE_W, maxX - minX), h: Math.max(NODE_H, maxY - minY) },
    pasteCount: 0,
  }
  if (!silent) showToast(`已复制 ${sel.length} 个智能体${innerEdges.length ? `、${innerEdges.length} 条连线` : ''}`)
  return true
}
function cutSelection() {
  if (!copySelection(true)) return
  const n = selectedNodeIds.value.size
  deleteSelectedNodes()
  showToast(`已剪切 ${n} 个智能体`)
}
function pasteClipboard(target?: Pt) {
  const clip = canvases.clipboard
  if (!clip || !clip.nodes.length || !canvas.value) return
  pushHistory()
  const cascade = (clip.pasteCount || 0) * 24
  let baseX: number, baseY: number
  if (target) {
    // 菜单粘贴：落在右键位置右下方，避免与源节点完全重叠
    baseX = target.x + 16 + cascade
    baseY = target.y + 16 + cascade
  } else {
    const rect = boardRef.value?.getBoundingClientRect()
    const center = clientToCanvas((rect?.left ?? 0) + (rect?.width ?? 800) / 2, (rect?.top ?? 0) + (rect?.height ?? 600) / 2)
    baseX = center.x - clip.bbox.w / 2 + cascade
    baseY = center.y - clip.bbox.h / 2 + cascade
  }
  const dx = baseX - clip.bbox.x
  const dy = baseY - clip.bbox.y
  const nextId = Math.max(0, ...nodes.value.map((n: any) => n.id)) + 1
  let nextEdgeId = Math.max(0, ...edges.value.map((e: any) => e.id)) + 1
  const idMap: number[] = []
  const newIds: number[] = []
  clip.nodes.forEach((n, i) => {
    const id = nextId + i
    idMap[i] = id
    newIds.push(id)
    canvas.value.nodes.push({ id, agent_id: n.agent_id, x: n.x + dx, y: n.y + dy })
  })
  for (const e of clip.edges) {
    const from = idMap[e.from], to = idMap[e.to]
    if (from == null || to == null || from === to) continue
    canvas.value.edges.push({
      id: nextEdgeId++,
      from_node: from,
      to_node: to,
      type: e.type || 'chat',
      shape: e.shape,
      ortho_dir: e.ortho_dir ?? null,
      anchor_from: e.anchor_from ?? null,
      anchor_to: e.anchor_to ?? null,
      segments: mapSegments(e, dx, dy),
      waypoints: (e.waypoints || []).map(p => ({ x: p.x + dx, y: p.y + dy })),
    })
  }
  clip.pasteCount = (clip.pasteCount || 0) + 1
  selectedNodeIds.value = new Set(newIds)
  closeMenus()
  canvases.saveCurrent()
  showToast(`已粘贴 ${newIds.length} 个智能体`)
}

// —— 用选区创建画布 ——
function uniqueCanvasName(base: string): string {
  let name = base
  let i = 2
  while (canvases.list.some((c: any) => c.name === name)) name = `${base} ${i++}`
  return name
}
function createCanvasFromSelection() {
  if (!canvas.value || !selectedNodeIds.value.size) return
  const sel = selectionNodes()
  const index = new Map<number, number>(sel.map((n: any, i: number) => [n.id, i]))
  const innerEdges = edges.value.filter((e: any) => index.has(e.from_node) && index.has(e.to_node))
  pendingCanvasPayload.value = {
    nodes: sel.map((n: any, i: number) => ({ id: i + 1, agent_id: n.agent_id, x: n.x, y: n.y })),
    edges: innerEdges.map((e: any, i: number) => ({
      id: i + 1,
      from_node: index.get(e.from_node)! + 1,
      to_node: index.get(e.to_node)! + 1,
      type: e.type || 'chat',
      shape: edgeShapeOf(e),
      ortho_dir: orthoDirOf(e),
      anchor_from: anchorSpecOf(e, 'from'),
      anchor_to: anchorSpecOf(e, 'to'),
      segments: mapSegments(e),
      waypoints: sanitizeWaypoints(e.waypoints).map(p => ({ x: p.x, y: p.y })),
    })),
  }
  newCanvasName.value = uniqueCanvasName(`${canvas.value.name} 副本`)
  newCanvasDialog.value = true
  closeMenus()
}

// —— 从画布列表拖入：复用源画布的智能体与内部连线 ——
function importCanvasPart(src: any, drop: Pt) {
  if (!canvas.value || !src) return
  const srcNodes: any[] = Array.isArray(src.nodes)
    ? src.nodes.filter((n: any) => n && n.agent_id != null && Number.isFinite(n.x) && Number.isFinite(n.y))
    : []
  if (!srcNodes.length) { showToast(`「${src.name || '画布'}」没有可导入的智能体`); return }
  pushHistory()
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of srcNodes) {
    minX = Math.min(minX, n.x); minY = Math.min(minY, n.y)
    maxX = Math.max(maxX, n.x + NODE_W); maxY = Math.max(maxY, n.y + NODE_H)
  }
  const bw = Math.max(NODE_W, maxX - minX), bh = Math.max(NODE_H, maxY - minY)
  const originX = drop.x - bw / 2, originY = drop.y - bh / 2
  const tx = originX - minX, ty = originY - minY

  const byAgent = new Map<number, any>()
  for (const n of nodes.value) byAgent.set(n.agent_id, n)
  const idMap = new Map<number, number>()
  const srcNodeById = new Map<number, any>()
  for (const n of srcNodes) srcNodeById.set(n.id, n)
  let nextNodeId = Math.max(0, ...nodes.value.map((n: any) => n.id)) + 1
  let addedNodes = 0
  for (const n of srcNodes) {
    const existing = byAgent.get(n.agent_id)
    if (existing) { idMap.set(n.id, existing.id); continue }
    const node = { id: nextNodeId++, agent_id: n.agent_id, x: n.x + tx, y: n.y + ty }
    canvas.value.nodes.push(node)
    idMap.set(n.id, node.id)
    byAgent.set(n.agent_id, node)
    addedNodes++
  }

  const srcEdges: any[] = Array.isArray(src.edges) ? src.edges : []
  let nextEdgeId = Math.max(0, ...edges.value.map((e: any) => e.id)) + 1
  let addedEdges = 0
  for (const e of srcEdges) {
    const from = idMap.get(e.from_node), to = idMap.get(e.to_node)
    if (from == null || to == null || from === to) continue
    if (edges.value.some((x: any) => x.from_node === from && x.to_node === to)) continue
    const srcFrom = srcNodeById.get(e.from_node)
    const destFrom = nodeById.value.get(from)
    const ddx = srcFrom && destFrom ? destFrom.x - srcFrom.x : tx
    const ddy = srcFrom && destFrom ? destFrom.y - srcFrom.y : ty
    const shape: EdgeShape = e.shape === 'curve' || e.shape === 'straight' || e.shape === 'ortho' || e.shape === 'mixed'
      ? e.shape
      : (src.edge_shape === 'ortho' || src.edge_shape === 'straight' ? src.edge_shape : 'curve')
    canvas.value.edges.push({
      id: nextEdgeId++,
      from_node: from,
      to_node: to,
      type: e.type || 'chat',
      shape,
      ortho_dir: e.ortho_dir ?? null,
      anchor_from: e.anchor_from ?? null,
      anchor_to: e.anchor_to ?? null,
      segments: mapSegments(e, ddx, ddy),
      waypoints: sanitizeWaypoints(e.waypoints).map(p => ({ x: p.x + ddx, y: p.y + ddy })),
    })
    addedEdges++
  }
  canvases.saveCurrent()
  showToast(addedNodes || addedEdges
    ? `已从「${src.name || '画布'}」添加 ${addedNodes} 个智能体、${addedEdges} 条连线`
    : `「${src.name || '画布'}」的智能体已全部存在，没有新增内容`)
}

// —— 工作区交互：全部 emit 给外层 MainView（全局单例 WorkspaceOverlay 持有者） ——
function toggleWorkspace() {
  const p = bgMenu.value || { x: window.innerWidth / 2, y: window.innerHeight / 2 }
  emit('open-workspace', { clientX: p.x, clientY: p.y })
  closeMenus()
}

// 双击消息气泡 / 节点右键菜单：显示或隐藏该节点的消息气泡
function toggleBubble(node: any) {
  bubbleHidden.value = { ...bubbleHidden.value, [node.id]: !bubbleHidden.value[node.id] }
}
// 双击节点头像：显示或隐藏该节点的消息泡泡
// 双击节点其他位置（非按钮/气泡区域）：直接打开智能体编辑
function onNodeDblClick(e: MouseEvent, node: any) {
  const t = e.target as HTMLElement | null
  if (t?.closest('button, input, label, a')) return
  // 刚用「点击节点」上色：紧接着的双击不要再触发编辑/切换泡泡，避免误操作
  if (Date.now() - colorAppliedAt < 400) return
  // 头像：切换消息泡泡（与双击气泡本身一致）
  if (t?.closest('.avatar')) { toggleBubble(node); return }
  openAgentEdit(node)
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
  if (!nodes.value.some((n: any) => n.id === nodeId)) return
  pushHistory()
  canvas.value.nodes = nodes.value.filter((n: any) => n.id !== nodeId)
  canvas.value.edges = edges.value.filter((e: any) => e.from_node !== nodeId && e.to_node !== nodeId)
  if (selectedNodeIds.value.has(nodeId)) {
    const next = new Set(selectedNodeIds.value)
    next.delete(nodeId)
    selectedNodeIds.value = next
  }
  canvases.saveCurrent(); closeMenus()
}
// 节点菜单里的移除：多选时移除整组
function removeCtxSelection() {
  const node = ctxMenu.value?.node
  if (selectedNodeIds.value.size > 1) deleteSelectedNodes()
  else if (node) removeNode(node.id)
}
function removeEdge(edgeId: number) {
  if (!canvas.value) return
  if (!edges.value.some((e: any) => e.id === edgeId)) return
  pushHistory()
  canvas.value.edges = edges.value.filter((e: any) => e.id !== edgeId)
  if (edgeMenu.value?.edge?.id === edgeId) edgeMenu.value = null
  canvases.saveCurrent()
}

async function doChat(node: any) {
  closeMenus()
  const canvasId = canvas.value?.id ?? null
  // 统一「对话」：确保当前会话绑定本画布（一个会话记录一个画布的全部消息）。
  // 当前会话已有历史但绑的是别的画布/未绑定 → 新建会话，避免不同画布消息混在一个记录里
  let conv = convs.currentData()
  const hasMsgs = !!(conv && Array.isArray(conv.messages) && conv.messages.length)
  if (!conv || ((conv.canvas_id ?? null) !== canvasId && hasMsgs)) {
    conv = await convs.create({
      name: '新对话',
      agent_ids: [node.agent_id],
      canvas_id: canvasId,
      workdir: canvas.value?.workdir ?? null,
      execution_mode: canvas.value?.execution_mode ?? null,
    })
  } else if ((conv.canvas_id ?? null) !== canvasId) {
    // 空会话：直接换绑本画布（工作区随会话落盘，未设置过则继承画布当前工作区）
    await convs.update(conv.id, {
      canvas_id: canvasId,
      agent_ids: Array.from(new Set([...(conv.agent_ids || []), node.agent_id])),
      workdir: conv.workdir ?? canvas.value?.workdir ?? null,
      execution_mode: conv.execution_mode ?? canvas.value?.execution_mode ?? null,
    })
  } else if (!(conv.agent_ids || []).includes(node.agent_id)) {
    await convs.update(conv.id, {
      agent_ids: Array.from(new Set([...(conv.agent_ids || []), node.agent_id])),
    })
  }
  convs.selectConv(conv!.id)
  // 自动和它对话：选中该智能体 + 聚焦输入框
  // 等 RightPanel 的 convs.current async watcher 跑完（它会用「最新回复的智能体」覆盖 selectedAgentId），
  // 再写入我们指定的 agent_id，避免被那个默认选中逻辑冲掉。
  await nextTick()
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
// ⑤ 运行/停止（按画布隔离：仅在当前画布处于运行态时才停止）
function toggleRun() {
  if (!canvas.value) return
  if (runningOnThisCanvas.value) convs.stopRun()
  else convs.startRun(canvas.value.id)
}

// —— 执行模式：运行时允许用户先切换图标/UI，待画布停止后再真正生效 ——
type ExecMode = 'serial' | 'parallel'
const pendingExecMode = ref<ExecMode | null>(null)
const displayExecMode = computed<ExecMode>(() => {
  if (runningOnThisCanvas.value && pendingExecMode.value) return pendingExecMode.value
  return (canvas.value?.execution_mode as ExecMode) || 'parallel'
})

// —— 极简 toast（执行模式提示 + 选区/剪贴板/导入反馈） ——
const toast = ref<{ msg: string; id: number } | null>(null)
let _toastSeq = 0
function showToast(msg: string) {
  const id = ++_toastSeq
  toast.value = { msg, id }
  setTimeout(() => {
    if (toast.value?.id === id) toast.value = null
  }, 2400)
}

async function applyExecMode(mode: ExecMode) {
  if (!canvas.value) return
  canvas.value.execution_mode = mode
  await canvases.saveCurrent()
  const cur = convs.currentData()
  if (cur && (cur.canvas_id ?? null) === canvas.value.id) {
    await convs.update(cur.id, { execution_mode: mode })
  }
}

// 运行态结束时：若存在待生效的模式，则立即应用
watch(runningOnThisCanvas, (running, wasRunning) => {
  if (wasRunning && !running && pendingExecMode.value && canvas.value) {
    const mode = pendingExecMode.value
    pendingExecMode.value = null
    applyExecMode(mode)
  }
})

// 串行/并行切换开关
async function toggleExecMode() {
  if (!canvas.value) return
  const curMode: ExecMode = (canvas.value.execution_mode as ExecMode) || 'parallel'
  const next: ExecMode = curMode === 'parallel' ? 'serial' : 'parallel'

  if (runningOnThisCanvas.value) {
    // 运行期间：只记录"待生效"的模式，UI 立即切换图标，真正的写入等到运行停止后
    pendingExecMode.value = next
    showToast(`已切换为「${next === 'parallel' ? '并行' : '串行'}」模式，画布停止运行后生效`)
    return
  }
  pendingExecMode.value = null
  await applyExecMode(next)
}

// —— 画布命名 ——
function startRename() { renameValue.value = canvas.value?.name || ''; renameEdit.value = true }
function commitRename() {
  const name = renameValue.value.trim()
  if (name && canvas.value?.name !== name) canvases.rename(canvas.value.id, name)
  renameEdit.value = false
}
function openNewCanvas() {
  pendingCanvasPayload.value = null
  newCanvasName.value = ''
  newCanvasDialog.value = true
}
async function commitNewCanvas() {
  const name = newCanvasName.value.trim() || `画布 ${canvases.list.length + 1}`
  const payload = pendingCanvasPayload.value
  pendingCanvasPayload.value = null
  newCanvasDialog.value = false
  const edgeShape: EdgeShape = defaultEdgeShape()
  await canvases.create({ name, edge_shape: edgeShape, ...(payload || {}) })
  if (payload) showToast(`已用 ${payload.nodes.length} 个智能体创建画布「${name}」`)
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
function snapToEdges(draggingNode: any, candidateX: number, candidateY: number, scaleNow: number, excludeIds?: Set<number>): { x: number; y: number } {
  const others = nodes.value.filter((n: any) => n.id !== draggingNode.id && !excludeIds?.has(n.id))
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

// —— 手动锚点：读取 / 换算 / 拖动吸附 ——
/** 读一条连线某一端的手动锚点；缺字段或非法值 = 自动布线 */
function anchorSpecOf(e: any, end: AnchorEnd): AnchorSpec | null {
  const raw = end === 'from' ? e?.anchor_from : e?.anchor_to
  if (!raw || !['l', 'r', 't', 'b'].includes(raw.side)) return null
  const t = Number(raw.t)
  if (!Number.isFinite(t)) return null
  return { side: raw.side, t: Math.max(0, Math.min(1, t)) }
}
function hasManualAnchors(e: any): boolean {
  return anchorSpecOf(e, 'from') !== null || anchorSpecOf(e, 'to') !== null
}
/** 归一化 t → 边的有效可变坐标（离角留白由 sideRange 保证） */
function anchorVar(spec: AnchorSpec, r: Rect): number {
  const [lo, hi] = sideRange(spec.side, r)
  return lo + (hi - lo) * spec.t
}
function anchorPoint(spec: AnchorSpec, r: Rect): Pt {
  return pointOnSide(spec.side, r, anchorVar(spec, r))
}
/** 四边中点吸附：鼠标靠近任一条边的中点（屏幕 10px 内）就精确吸到 t=0.5。
 *  四条边一起比，节点中心附近也能吸到最近的中点，不受「最近边」判定影响。 */
const ANCHOR_MID_SNAP_PX = 10
function snapToSideMid(r: Rect, p: Pt): AnchorSpec | null {
  const radius = ANCHOR_MID_SNAP_PX / (scale.value || 1)
  let best: { side: OrthoSide; d: number } | null = null
  for (const s of ORTHO_SIDES) {
    const [lo, hi] = sideRange(s, r)
    const q = pointOnSide(s, r, (lo + hi) / 2)
    const d = Math.hypot(q.x - p.x, q.y - p.y)
    if (d <= radius && (!best || d < best.d)) best = { side: s, d }
  }
  return best ? { side: best.side, t: 0.5 } : null
}
/** 拖动时把鼠标位置吸附到节点最合适的边：按相对中心的方向选边，
 *  再把沿边坐标换算成 t 并 clamp，锚点始终贴在节点边缘上。 */
function nearestAnchor(r: Rect, p: Pt): AnchorSpec {
  const mid = snapToSideMid(r, p)
  if (mid) return mid
  const w = (r.r - r.l) / 2 || 1, h = (r.b - r.t) / 2 || 1
  const dx = (p.x - (r.l + r.r) / 2) / w
  const dy = (p.y - (r.t + r.b) / 2) / h
  const side: OrthoSide = Math.abs(dx) >= Math.abs(dy)
    ? (dx >= 0 ? 'r' : 'l')
    : (dy >= 0 ? 'b' : 't')
  const [lo, hi] = sideRange(side, r)
  const v = sideAxis(side) === 'H' ? p.y : p.x
  const t = hi > lo ? (v - lo) / (hi - lo) : 0.5
  return { side, t: Math.max(0, Math.min(1, t)) }
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
  return smoothPath(offsetCubicSamples(p0, c1, c2, p3, dist))
}

/** 单条三次贝塞尔的均匀采样点（用于命中检测与路径中点） */
function cubicSamples(p0: Pt, c1: Pt, c2: Pt, p3: Pt, n = 48): Pt[] {
  const out: Pt[] = []
  for (let i = 0; i <= n; i++) out.push(cubicAt(p0, c1, c2, p3, i / n))
  return out
}

/** 沿贝塞尔法线等距偏移后的采样点（双向连线用，保证两条线处处平行） */
function offsetCubicSamples(p0: any, c1: any, c2: any, p3: any, dist: number): Pt[] {
  const pts: Pt[] = []
  for (let i = 0; i <= EDGE_SAMPLES; i++) {
    const t = i / EDGE_SAMPLES
    const B = cubicAt(p0, c1, c2, p3, t)
    const T = cubicTangentAt(p0, c1, c2, p3, t)
    const len = Math.hypot(T.x, T.y) || 1
    pts.push({ x: B.x - (T.y / len) * dist, y: B.y + (T.x / len) * dist })
  }
  return pts
}

// —— 连线形状 / 控制点 ——
/** 画布默认形状；旧数据或未知值一律回落为曲线 */
function defaultEdgeShape(): EdgeShape {
  const s = canvas.value?.edge_shape
  return s === 'straight' || s === 'ortho' ? s : 'curve'
}
function edgeShapeOf(e: any): EdgeShape {
  const s = e?.shape
  if (s === 'curve' || s === 'straight' || s === 'ortho' || s === 'mixed') return s
  return defaultEdgeShape()
}
/** 双向连线且两端共线时：按端点 id 稳定分配相反的边缘偏移，两条直线不会叠在一起 */
function orthoBias(e: any): number {
  if (!hasReverseEdge(e)) return 0
  return e.from_node < e.to_node ? BIDIR_OFFSET : -BIDIR_OFFSET
}
function sanitizeWaypoints(raw: any): Pt[] {
  if (!Array.isArray(raw)) return []
  const out: Pt[] = []
  for (const p of raw) {
    const x = Number(p?.x), y = Number(p?.y)
    if (Number.isFinite(x) && Number.isFinite(y)) out.push({ x, y })
  }
  return out
}
function ensureWaypoints(e: any): Pt[] {
  if (!Array.isArray(e.waypoints)) e.waypoints = []
  return e.waypoints
}
function hasWaypoints(e: any): boolean {
  return Array.isArray(e?.waypoints) && e.waypoints.length > 0
}

function linePath(pts: Pt[]): string {
  if (!pts.length) return ''
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 1; i < pts.length; i++) d += ` L ${pts[i].x.toFixed(1)} ${pts[i].y.toFixed(1)}`
  return d
}

// ————————————————————————————————————————————————————————————————————————
// 分段连线（shape=mixed）：一条边由若干段串成，每段独立保存线型。
//   顶点序列 = [起点锚点, seg0.end, seg1.end, …, 终点锚点]
//   起止锚点用「边 + 比例」存相对值，节点移动后仍贴合边缘；中间顶点是画布绝对坐标。
//   直线 = L；曲线 = 三次贝塞尔 C（两个控制点可拖）；圆弧 = 真正的 A 圆弧（弦 + 带符号矢高）；
//   垂线 = 垂直于「上一段末端方向」的直线，垂足是上一段末尾；落点投影到该直线上决定长度与朝向。
// ————————————————————————————————————————————————————————————————————————
const SEG_MIN_LEN = 6
const DEFAULT_ARC_BULGE = 0.22   // 圆弧默认矢高 / 弦长
const MAX_ARC_BULGE = 0.45       // 上限 0.5 就是半圆，留一点余量避免半径退化
const VERT_EPS = 1.5             // 终点已落在垂线上时的容差（不必补连接短线）
type BuiltSeg = { d: string; samples: Pt[]; mid: Pt; ctrl: Pt[]; endTan?: Pt }

function clampNum(v: number, lo: number, hi: number) { return Math.max(lo, Math.min(hi, v)) }
function isSegType(v: any): v is SegType { return v === 'straight' || v === 'curve' || v === 'arc' || v === 'vert' }
function asPt(v: any): Pt | null {
  const x = Number(v?.x), y = Number(v?.y)
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null
}
function segLabel(t: SegType): string {
  return t === 'curve' ? '曲线' : t === 'arc' ? '圆弧' : t === 'vert' ? '垂线' : '直线'
}
/** 单位向量；零向量返回 null */
function unitVec(v: Pt | null | undefined): Pt | null {
  if (!v) return null
  const x = Number(v.x), y = Number(v.y)
  const len = Math.hypot(x, y)
  return Number.isFinite(len) && len > 0.001 ? { x: x / len, y: y / len } : null
}
/** 左转 90°：与末端切向垂直的单位法线 */
function normalOf(t: Pt): Pt { return { x: -t.y, y: t.x } }
/** p 在「过 p0、方向 n」的直线上的垂足 */
function projectOnNormal(p0: Pt, p: Pt, n: Pt): Pt {
  const L = (p.x - p0.x) * n.x + (p.y - p0.y) * n.y
  return { x: p0.x + n.x * L, y: p0.y + n.y * L }
}
/** 起点锚点所在边的出边法线：第一段没有上一段，用节点边法线作为参考方向 */
function anchorNormal(side: OrthoSide): Pt {
  return side === 'l' ? { x: -1, y: 0 } : side === 'r' ? { x: 1, y: 0 }
    : side === 't' ? { x: 0, y: -1 } : { x: 0, y: 1 }
}
function segmentBaseNormal(e: any, start: Pt): Pt {
  const f = anchorSpecOf(e, 'from')
  if (f) return anchorNormal(f.side)
  const a = nodeById.value.get(e.from_node)
  if (a) {
    const t = unitVec({ x: start.x - (a.x + NODE_W / 2), y: start.y - (a.y + NODE_H / 2) })
    if (t) return t
  }
  return { x: 1, y: 0 }
}
function cloneSegment(s: EdgeSegment): EdgeSegment {
  return { type: s.type, end: { ...s.end }, ctrl: s.ctrl.map(p => ({ ...p })), bulge: s.bulge }
}
/** 读渲染用的分段（纯函数：返回副本，绝不在 computed 里改源数据） */
function readSegments(e: any): EdgeSegment[] {
  if (!Array.isArray(e?.segments)) return []
  const out: EdgeSegment[] = []
  for (const s of e.segments) {
    const end = asPt(s?.end)
    if (!end) continue
    out.push({
      type: isSegType(s?.type) ? s.type : 'straight',
      end,
      ctrl: Array.isArray(s?.ctrl)
        ? (s.ctrl as any[]).map(asPt).filter((p: Pt | null): p is Pt => !!p)
        : [],
      bulge: Number.isFinite(Number(s?.bulge))
        ? clampNum(Number(s.bulge), -MAX_ARC_BULGE, MAX_ARC_BULGE)
        : DEFAULT_ARC_BULGE,
    })
  }
  return out
}
function hasSegments(e: any): boolean { return readSegments(e).length > 0 }
/** 写回时用活对象（自己创建的数据一定是规范结构） */
function liveSegments(e: any): any[] { return Array.isArray(e?.segments) ? e.segments : [] }

/** 距离鼠标最近的「边上点」：四条边各取投影后比距离，沿边 clamp 保证离角留白。
 *  鼠标在节点内部时自然取最近的一条边；在外部时取该边上离鼠标最近的点。 */
function closestEdgeAnchor(r: Rect, p: Pt): AnchorSpec {
  const mid = snapToSideMid(r, p)
  if (mid) return mid
  let bestSide: OrthoSide = 'r', bestV = 0, bestD = Infinity
  for (const s of ORTHO_SIDES) {
    const [lo, hi] = sideRange(s, r)
    const along = sideAxis(s) === 'H' ? p.y : p.x
    const v = clampNum(along, lo, hi)
    const q = pointOnSide(s, r, v)
    const dd = (q.x - p.x) ** 2 + (q.y - p.y) ** 2
    if (dd < bestD) { bestD = dd; bestSide = s; bestV = v }
  }
  const [lo, hi] = sideRange(bestSide, r)
  return { side: bestSide, t: hi > lo ? clampNum((bestV - lo) / (hi - lo), 0, 1) : 0.5 }
}
/** 绘制连线的起点锚点：始终吸附到「四条边中最靠近鼠标的那条边的中点」。
 *  鼠标绕节点移动时锚点会在上/右/下/左四个边中点间平滑切换，不再按最近投影点游走。 */
function nearestMidAnchor(r: Rect, p: Pt): AnchorSpec {
  const cx = (r.l + r.r) / 2, cy = (r.t + r.b) / 2
  const dx = p.x - cx, dy = p.y - cy
  // 越靠外的轴越优先：鼠标在节点外时自然选朝向鼠标的一侧
  const side: OrthoSide = Math.abs(dx) >= Math.abs(dy) ? (dx >= 0 ? 'r' : 'l') : (dy >= 0 ? 'b' : 't')
  return { side, t: 0.5 }
}
/** 绘制连线的终点锚点：吸附到「朝向源节点那一侧的边中点」。
 *  从目标节点中心指向源节点中心，取分量最大的轴对应的边，t 恒为 0.5（正对中线）。 */
function facingMidAnchor(r: Rect, towards: Pt): AnchorSpec {
  const cx = (r.l + r.r) / 2, cy = (r.t + r.b) / 2
  const dx = towards.x - cx, dy = towards.y - cy
  const side: OrthoSide = Math.abs(dx) >= Math.abs(dy) ? (dx >= 0 ? 'r' : 'l') : (dy >= 0 ? 'b' : 't')
  return { side, t: 0.5 }
}

/** 右键位置相对当前段弦的位置：t=沿弦归一化位置，s=归一化垂距（带符号）。
 *  右键点正好落在端点或弦上时返回 null → 用默认弯向。 */
function viaBend(p0: Pt, p1: Pt, via: Pt | null | undefined): { t: number; s: number } | null {
  if (!via) return null
  const dx = p1.x - p0.x, dy = p1.y - p0.y
  const len = Math.hypot(dx, dy)
  if (len < 0.001) return null
  const ux = dx / len, uy = dy / len
  const t = ((via.x - p0.x) * ux + (via.y - p0.y) * uy) / len
  const s = ((via.x - p0.x) * -uy + (via.y - p0.y) * ux) / len
  if (t < 0.06 || t > 0.94 || Math.abs(s) < 0.02) return null
  return { t, s }
}
/** 曲线默认 S 形控制点；有右键弯向时把两个控制点一起沿弦法线平移，曲线随之鼓向右键侧 */
function segCurveCtrl(p0: Pt, p1: Pt, via?: Pt | null, n0?: Pt | null, n1?: Pt | null): Pt[] {
  const dx = p1.x - p0.x, dy = p1.y - p0.y
  const len = Math.hypot(dx, dy) || 1
  let c1: Pt, c2: Pt
  // 端点贴在节点锚点上时（n0/n1 = 该锚点的出边法线，指向节点外侧）：
  // 控制点沿法线外推 → 曲线在锚点处与边垂直，必定从节点外部指向锚点，
  // 不会出现「曲线从节点内部鼓出来再连到边上」的情形。
  if (n0 || n1) {
    const d = Math.min(len / 3, 90)
    const u0 = unitVec(n0) || { x: dx / len, y: dy / len }
    const u1 = unitVec(n1) || { x: -dx / len, y: -dy / len }
    c1 = { x: p0.x + u0.x * d, y: p0.y + u0.y * d }
    c2 = { x: p1.x + u1.x * d, y: p1.y + u1.y * d }
  } else if (Math.abs(dx) >= Math.abs(dy)) {
    c1 = { x: p0.x + dx / 3, y: p0.y }; c2 = { x: p1.x - dx / 3, y: p1.y }
  } else {
    c1 = { x: p0.x, y: p0.y + dy / 3 }; c2 = { x: p1.x, y: p1.y - dy / 3 }
  }
  const bend = viaBend(p0, p1, via)
  if (bend) {
    const m = clampNum((4 / 3) * bend.s * len, -1.2 * len, 1.2 * len)
    const nx = -dy / len, ny = dx / len
    c1 = { x: c1.x + nx * m, y: c1.y + ny * m }
    c2 = { x: c2.x + nx * m, y: c2.y + ny * m }
  }
  return [c1, c2]
}
/** 圆弧矢高：右键点在某处的垂距 s 反推圆弧中点矢高（抛物线近似 s(t)≈4b·t(1-t)） */
function segBulge(p0: Pt, p1: Pt, via?: Pt | null): number {
  const bend = viaBend(p0, p1, via)
  if (!bend) return DEFAULT_ARC_BULGE
  const b = bend.s / (4 * bend.t * (1 - bend.t))
  return clampNum(b, -MAX_ARC_BULGE, MAX_ARC_BULGE) || DEFAULT_ARC_BULGE
}
/** 弦 + 归一化矢高 → 真圆弧：半径 R=len·(1/8|b| + |b|/2)，圆心在弦中点的法线另一侧。
 *  b>0 鼓向法线正侧（sweep=0），b<0 鼓向另一侧（sweep=1）。 */
function arcGeom(p0: Pt, p1: Pt, b: number) {
  const dx = p1.x - p0.x, dy = p1.y - p0.y
  const len = Math.hypot(dx, dy)
  if (len < 0.001 || Math.abs(b) < 0.001) return null
  const bb = clampNum(b, -MAX_ARC_BULGE, MAX_ARC_BULGE)
  const R = len * (1 / (8 * Math.abs(bb)) + Math.abs(bb) / 2)
  const sag = Math.abs(bb) * len
  const nx = -dy / len, ny = dx / len
  const sign = bb > 0 ? 1 : -1
  const cx = (p0.x + p1.x) / 2 - sign * (R - sag) * nx
  const cy = (p0.y + p1.y) / 2 - sign * (R - sag) * ny
  const a0 = Math.atan2(p0.y - cy, p0.x - cx)
  const a1 = Math.atan2(p1.y - cy, p1.x - cx)
  const sweep = bb > 0 ? 0 : 1
  let delta = a1 - a0
  while (delta > Math.PI) delta -= 2 * Math.PI
  while (delta <= -Math.PI) delta += 2 * Math.PI
  if ((sweep === 1 && delta < 0) || (sweep === 0 && delta > 0)) delta += sweep === 1 ? 2 * Math.PI : -2 * Math.PI
  return { R, cx, cy, a0, delta, sweep }
}
function straightBuilt(p0: Pt, p1: Pt): BuiltSeg {
  const samples = [0, 0.25, 0.5, 0.75, 1].map(k => ({ x: p0.x + (p1.x - p0.x) * k, y: p0.y + (p1.y - p0.y) * k }))
  return {
    d: `L ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`, samples, mid: samples[2], ctrl: [],
    endTan: unitVec({ x: p1.x - p0.x, y: p1.y - p0.y }) || { x: 1, y: 0 },
  }
}
/** 垂线段：p0 = 垂足（上一段末尾），n = 垂直于上一段末端切向的单位法线。
 *  主体是 p0 → T（end 在垂线上的投影）；若 end 不在垂线上（最后一段 end 是目标节点锚点），
 *  再补一小段把线接到 end，保证「主段严格垂直」且「线仍然连到节点」。 */
function vertBuilt(p0: Pt, p1: Pt, normal: Pt | null | undefined, ctrl: Pt[]): BuiltSeg {
  const n = unitVec(normal) || unitVec(ctrl?.[0])
  if (!n) return straightBuilt(p0, p1)
  const T = projectOnNormal(p0, p1, n)
  const tail = { x: p1.x - T.x, y: p1.y - T.y }
  if (Math.hypot(tail.x, tail.y) <= VERT_EPS) return straightBuilt(p0, p1)
  const samples: Pt[] = [p0]
  for (const q of [T, p1]) {
    const from = samples[samples.length - 1]
    for (let k = 1; k <= 4; k++) {
      samples.push({ x: from.x + (q.x - from.x) * k / 4, y: from.y + (q.y - from.y) * k / 4 })
    }
  }
  samples[samples.length - 1] = { x: p1.x, y: p1.y }
  return {
    d: `L ${T.x.toFixed(1)} ${T.y.toFixed(1)} L ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`,
    samples, mid: { x: (p0.x + T.x) / 2, y: (p0.y + T.y) / 2 }, ctrl: [n],
    endTan: unitVec(tail) || n,
  }
}
/** 一段在终点处的单位切向：曲线看末端控制点、圆弧看终点切线、直线/垂线看最后一段方向。
 *  垂线方向必须取「上一段末端」的切向，不能用整段弦方向，否则曲线/圆弧段后面接的垂线会歪。 */
function segEndTangent(p0: Pt, p1: Pt, type: SegType, ctrl: Pt[], bulge: number, built?: BuiltSeg): Pt {
  if (built?.endTan) return built.endTan
  if (type === 'curve' && ctrl.length >= 2) {
    const t = unitVec({ x: p1.x - ctrl[1].x, y: p1.y - ctrl[1].y })
    if (t) return t
  }
  if (type === 'arc') {
    const g = arcGeom(p0, p1, bulge)
    if (g) {
      const r = { x: p1.x - g.cx, y: p1.y - g.cy }
      const s = g.delta >= 0 ? 1 : -1
      const t = unitVec({ x: -r.y * s, y: r.x * s })
      if (t) return t
    }
  }
  return unitVec({ x: p1.x - p0.x, y: p1.y - p0.y }) || { x: 1, y: 0 }
}
/** 生成一段的 SVG 片段（不含起点 M）+ 采样点（箭头中点与控制柄定位共用）。
 *  normal 只对垂线段有效，由调用方按「上一段末端切向」实时算出，上一段被编辑后垂线会自动跟着转。 */
function buildSegment(p0: Pt, p1: Pt, type: SegType, ctrl: Pt[], bulge: number, normal?: Pt | null): BuiltSeg {
  if (type === 'vert') return vertBuilt(p0, p1, normal, ctrl)
  if (type === 'arc') {
    const g = arcGeom(p0, p1, bulge)
    if (!g) return straightBuilt(p0, p1)
    const N = 24
    const samples: Pt[] = [p0]
    for (let k = 1; k <= N; k++) {
      const a = g.a0 + g.delta * k / N
      samples.push({ x: g.cx + g.R * Math.cos(a), y: g.cy + g.R * Math.sin(a) })
    }
    samples[samples.length - 1] = { x: p1.x, y: p1.y }
    return {
      d: `A ${g.R.toFixed(3)} ${g.R.toFixed(3)} 0 0 ${g.sweep} ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`,
      samples, mid: samples[N / 2], ctrl: [],
    }
  }
  if (type === 'curve') {
    const cs = ctrl.length >= 2 ? ctrl.slice(0, 2) : segCurveCtrl(p0, p1)
    const samples = cubicSamples(p0, cs[0], cs[1], p1, 20)
    return {
      d: `C ${cs[0].x.toFixed(1)} ${cs[0].y.toFixed(1)}, ${cs[1].x.toFixed(1)} ${cs[1].y.toFixed(1)}, ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`,
      samples, mid: samples[10], ctrl: cs,
    }
  }
  return straightBuilt(p0, p1)
}
/** 落点确认：把「右键弯向」烘焙成段自带的控制点 / 矢高，之后与菜单位置再无关系。
 *  垂线段另把落点投影到法线上（垂足 = p0），并记下单位法线，保证段本身就是一条垂直直线。 */
function makeDrawSegment(p0: Pt, p1: Pt, type: SegType, via: Pt | null, normal?: Pt | null, n0?: Pt | null, n1?: Pt | null): EdgeSegment {
  if (type === 'vert') {
    const n = unitVec(normal) || { x: 0, y: 1 }
    return { type, end: projectOnNormal(p0, p1, n), ctrl: [n], bulge: DEFAULT_ARC_BULGE }
  }
  return {
    type,
    end: { x: p1.x, y: p1.y },
    ctrl: type === 'curve' ? segCurveCtrl(p0, p1, via, n0, n1) : [],
    bulge: type === 'arc' ? segBulge(p0, p1, via) : DEFAULT_ARC_BULGE,
  }
}
/** 分段连线的顶点序列：首末锚点由节点实时派生，中间顶点取各段 end */
function mixedPoints(e: any): Pt[] {
  const a = nodeById.value.get(e.from_node), b = nodeById.value.get(e.to_node)
  if (!a || !b) return []
  const f = anchorSpecOf(e, 'from'), t = anchorSpecOf(e, 'to')
  const auto = sideAnchors(a, b)
  const start = f ? anchorPoint(f, nodeRect(a)) : auto.p1
  const end = t ? anchorPoint(t, nodeRect(b)) : auto.p2
  const segs = readSegments(e)
  return segs.length ? [start, ...segs.slice(0, -1).map(s => s.end), end] : [start, end]
}
/** 渲染一条分段连线；每段返回自己的可拖手柄（顶点 / 曲线控制点 / 圆弧矢高 / 拆分点） */
function computeMixedEdge(e: any, segs: EdgeSegment[]) {
  const pts = mixedPoints(e)
  if (pts.length < 2) return null
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  const samples: Pt[] = [pts[0]]
  const handles: EdgeHandle[] = []
  // 逐段推进末端切向：垂线段的方向由「上一段末端」实时决定，拖动上一段后垂线自动保持垂直
  let prevTan = segmentBaseNormal(e, pts[0])
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]
    const normal = s.type === 'vert' ? normalOf(prevTan) : null
    const built = buildSegment(pts[i], pts[i + 1], s.type, s.ctrl, s.bulge, normal)
    prevTan = segEndTangent(pts[i], pts[i + 1], s.type, s.ctrl, s.bulge, built)
    d += ' ' + built.d
    samples.push(...built.samples.slice(1))
    // 顶点柄（最后一段终点是目标锚点，交给锚点柄）
    if (i < segs.length - 1) handles.push({ kind: 'vertex', seg: i, index: i, x: pts[i + 1].x, y: pts[i + 1].y })
    if (s.type === 'curve') {
      built.ctrl.forEach((cp, k) => handles.push({ kind: 'ctrl', seg: i, ctrl: k, index: k, x: cp.x, y: cp.y }))
    }
    if (s.type === 'arc') handles.push({ kind: 'bulge', seg: i, index: 0, x: built.mid.x, y: built.mid.y })
    // 拆分柄放在 25% 处，避免与圆弧矢高柄（中点）重叠
    const q = built.samples[Math.max(1, Math.floor(built.samples.length / 4))] || built.mid
    handles.push({ kind: 'insert', seg: i, index: i, x: q.x, y: q.y })
  }
  const mid = polylineMid(samples)
  return {
    d, midX: mid.x, midY: mid.y, midAngle: mid.angle, handles,
    anchorFrom: pts[0], anchorTo: pts[pts.length - 1],
  }
}
/** 第 idx 段（垂线段）渲染时实际使用的单位法线：按前面各段的末端切向逐段推进。
 *  编辑垂线顶点时用它投影，避免「拖动时按旧方向、渲染时按新方向」造成跳动。 */
function mixedNormalAt(e: any, idx: number): Pt {
  const pts = mixedPoints(e), segs = readSegments(e)
  let tan: Pt = pts.length ? segmentBaseNormal(e, pts[0]) : { x: 1, y: 0 }
  for (let i = 0; i < idx && i < segs.length && pts[i + 1]; i++) {
    const s = segs[i]
    const normal = s.type === 'vert' ? normalOf(tan) : null
    const built = buildSegment(pts[i], pts[i + 1], s.type, s.ctrl, s.bulge, normal)
    tan = segEndTangent(pts[i], pts[i + 1], s.type, s.ctrl, s.bulge, built)
  }
  return normalOf(tan)
}

// —— 直角折线：路径 = [锚点A, 拐点…, 锚点B]，锚点为派生量、控制点严格为折角 ——
// 路由算法见下方 avoidRects 之后的「ortho 路由」段落（依赖 Rect 类型）。

/** 去掉连续重合点（共线或退化段会产生） */
function dedupePts(pts: Pt[]): Pt[] {
  const out: Pt[] = []
  for (const p of pts) {
    const last = out[out.length - 1]
    if (last && Math.abs(last.x - p.x) < 0.01 && Math.abs(last.y - p.y) < 0.01) continue
    out.push(p)
  }
  return out
}

/** 按「路径长度」取折线中点，并给出该点所在线段方向角（直角折线下恒为 0/90/180/270） */
function polylineMid(pts: Pt[]): Pt & { angle: number } {
  if (pts.length < 2) return { x: pts[0]?.x ?? 0, y: pts[0]?.y ?? 0, angle: 0 }
  const lens: number[] = []
  let total = 0
  for (let i = 0; i < pts.length - 1; i++) {
    const l = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y)
    lens.push(l); total += l
  }
  if (total < 0.001) return { x: pts[0].x, y: pts[0].y, angle: 0 }
  let acc = 0
  const half = total / 2
  for (let i = 0; i < lens.length; i++) {
    if (acc + lens[i] >= half || i === lens.length - 1) {
      const t = lens[i] > 0 ? (half - acc) / lens[i] : 0
      const p = pts[i], q = pts[i + 1]
      return {
        x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t,
        angle: Math.atan2(q.y - p.y, q.x - p.x) * 180 / Math.PI,
      }
    }
    acc += lens[i]
  }
  const p = pts[pts.length - 2], q = pts[pts.length - 1]
  return { x: q.x, y: q.y, angle: Math.atan2(q.y - p.y, q.x - p.x) * 180 / Math.PI }
}

/** Catmull-Rom → 三次 Bézier：曲线严格穿过每个控制点（区别于采样平滑用的 smoothPath） */
function smoothControls(pts: Pt[], i: number) {
  const p1 = pts[i], p2 = pts[i + 1]
  const p0 = pts[i - 1] || p1, p3 = pts[i + 2] || p2
  return {
    c1: { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 },
    c2: { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 },
  }
}
function smoothThrough(pts: Pt[]): string {
  if (!pts.length) return ''
  if (pts.length === 2) return linePath(pts)
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 0; i < pts.length - 1; i++) {
    const { c1, c2 } = smoothControls(pts, i)
    d += ` C ${c1.x.toFixed(1)} ${c1.y.toFixed(1)}, ${c2.x.toFixed(1)} ${c2.y.toFixed(1)}, ${pts[i + 1].x.toFixed(1)} ${pts[i + 1].y.toFixed(1)}`
  }
  return d
}

/** 把控制点序列按 Catmull-Rom 采样成折线，供「是否穿过节点」判定与路径中点共用 */
function smoothSamples(pts: Pt[], perSeg = 14): Pt[] {
  if (pts.length < 2) return pts.slice()
  if (pts.length === 2) return [pts[0], pts[1]]
  const out: Pt[] = [pts[0]]
  for (let i = 0; i < pts.length - 1; i++) {
    const { c1, c2 } = smoothControls(pts, i)
    for (let k = 1; k <= perSeg; k++) out.push(cubicAt(pts[i], c1, c2, pts[i + 1], k / perSeg))
  }
  return out
}

/** 第 i 段（pts[i] → pts[i+1]）的中点；smooth=按 Catmull-Rom 曲线取真实中点 */
function segmentMidpoint(pts: Pt[], i: number, smooth: boolean): Pt {
  const p1 = pts[i], p2 = pts[i + 1]
  if (!p1 || !p2) return { x: 0, y: 0 }
  if (!smooth) return { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 }
  const p0 = pts[i - 1] || p1
  const p3 = pts[i + 2] || p2
  const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 }
  const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 }
  return cubicAt(p1, c1, c2, p2, 0.5)
}

// —— 连线避让：曲线 / 直角折线尽量绕开其他智能体节点（斜线直连保持原样，不参与避让） ——
type Rect = { l: number; t: number; r: number; b: number }
const AVOID_PAD = 10     // 其它节点外扩，保证绕行后仍留出视觉间隙
const AVOID_INSET = 6    // 起止节点内缩：贴着边缘不算穿体，切进内部才算
const AVOID_GAP = 16     // 绕行路径与挡路节点边缘的额外间距
const AVOID_SAMPLES = 14 // 每段曲线的采样数（命中判定与取中点共用）

/** 参与避让的矩形：其它节点外扩留白，起止节点内缩 */
function avoidRects(fromNode: any, toNode?: any): Rect[] {
  const out: Rect[] = []
  const fid = fromNode?.id, tid = toNode?.id
  for (const n of nodes.value) {
    if (n.id === fid || n.id === tid) {
      out.push({
        l: n.x + AVOID_INSET, t: n.y + AVOID_INSET,
        r: n.x + NODE_W - AVOID_INSET, b: n.y + NODE_H - AVOID_INSET,
      })
    } else {
      out.push({
        l: n.x - AVOID_PAD, t: n.y - AVOID_PAD,
        r: n.x + NODE_W + AVOID_PAD, b: n.y + NODE_H + AVOID_PAD,
      })
    }
  }
  return out
}

/** 线段落在矩形内的长度（Liang–Barsky 裁剪），0 表示不相交 */
function segRectLen(p: Pt, q: Pt, r: Rect): number {
  const dx = q.x - p.x, dy = q.y - p.y
  const len = Math.hypot(dx, dy)
  if (len < 1e-6) return 0
  const P = [-dx, dx, -dy, dy]
  const Q = [p.x - r.l, r.r - p.x, p.y - r.t, r.b - p.y]
  let u1 = 0, u2 = 1
  for (let i = 0; i < 4; i++) {
    if (Math.abs(P[i]) < 1e-9) { if (Q[i] < 0) return 0; continue }
    const t = Q[i] / P[i]
    if (P[i] < 0) u1 = Math.max(u1, t)
    else u2 = Math.min(u2, t)
    if (u1 > u2) return 0
  }
  return Math.max(0, u2 - u1) * len
}

/** 折线穿过节点的程度：被穿过的节点个数 + 累计穿越长度（判定与候选评分共用） */
function routeHits(pts: Pt[], rects: Rect[]) {
  const hit = new Set<number>()
  let len = 0
  for (let i = 0; i < pts.length - 1; i++) {
    for (let j = 0; j < rects.length; j++) {
      const l = segRectLen(pts[i], pts[i + 1], rects[j])
      if (l > 0.5) { hit.add(j); len += l }
    }
  }
  return { n: hit.size, len }
}
function polylineLen(pts: Pt[]): number {
  let s = 0
  for (let i = 1; i < pts.length; i++) s += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
  return s
}

/**
 * 曲线绕行：每个原始段插入两个「抬升」控制点，沿该段法线把曲线推到挡路节点之外。
 * 返回的控制点序列恒为 [p0, a, b, p1, ...]，即每个原始段对应 3 个渲染段，
 * 因此插入控制点的柄可以按「原始段序号」精确映射到渲染曲线上。
 */
function detourCurve(pts: Pt[], rects: Rect[], sidePref: 1 | -1) {
  let best: { pts: Pt[]; score: number } | null = null
  for (const side of [sidePref, -sidePref] as (1 | -1)[]) {
    for (const gain of [1, 1.4, 2, 3]) {
      const built: Pt[] = [pts[0]]
      for (let i = 0; i < pts.length - 1; i++) {
        const p = pts[i], q = pts[i + 1]
        const dx = q.x - p.x, dy = q.y - p.y
        const len = Math.hypot(dx, dy) || 1
        const nx = -dy / len * side, ny = dx / len * side
        // 挡路矩形：弦本身穿过，或落在弦附近（曲线过冲也可能擦到）
        let need = 0
        for (const r of rects) {
          const rr = { l: r.l - 24, t: r.t - 24, r: r.r + 24, b: r.b + 24 }
          if (segRectLen(p, q, rr) <= 0) continue
          const proj = [
            { x: rr.l, y: rr.t }, { x: rr.r, y: rr.t },
            { x: rr.l, y: rr.b }, { x: rr.r, y: rr.b },
          ].map(c => (c.x - p.x) * -dy / len + (c.y - p.y) * dx / len)
          const lo = Math.min(...proj), hi = Math.max(...proj)
          need = Math.max(need, side > 0 ? hi + AVOID_GAP : -lo + AVOID_GAP)
        }
        const k = need * gain
        built.push(
          { x: p.x + dx * 0.28 + nx * k, y: p.y + dy * 0.28 + ny * k },
          { x: p.x + dx * 0.72 + nx * k, y: p.y + dy * 0.72 + ny * k },
          q,
        )
      }
      const samples = smoothSamples(built, AVOID_SAMPLES)
      const h = routeHits(samples, rects)
      const score = h.n * 10000 + h.len * 5 + (gain - 1) * 80 + (side === sidePref ? 0 : 120)
      if (!best || score < best.score) best = { pts: built, score }
    }
  }
  const chosen = best!
  const samples = smoothSamples(chosen.pts, AVOID_SAMPLES)
  // 每个原始段的插入柄：取该段在渲染曲线上的弧长中点
  const mids: Pt[] = []
  for (let i = 0; i < pts.length - 1; i++) {
    const s = 1 + 3 * i * AVOID_SAMPLES
    const t = Math.min(1 + 3 * (i + 1) * AVOID_SAMPLES, samples.length - 1)
    const m = polylineMid(samples.slice(s, t + 1))
    mids.push({ x: m.x, y: m.y })
  }
  return { pts: chosen.pts, d: smoothThrough(chosen.pts), samples, mids }
}

// ————————————————————————————————————————————————————————————————————————
// 直角折线路由（ortho）
//  1. 包围盒内无阻挡 → 顺时针 / 逆时针两条 L 形（各 1 个拐点），可手动切换方向
//  2. 一侧被挡 → 自动切到另一条；两侧都被挡 → 生成 Z / U 形多拐点通道绕行
//  3. 锚点由相邻拐点正交投影到节点边派生，随拐点移动、沿边滑动，取总长最短
//  4. 投影落不进节点边范围 → 自动插入拐点（L / Z），锚点仍合法落在节点上
//  5. 化简：去除重合点、合并共线段、消除折返 → 中间点恒为 90° 折角（控制点严格为拐点）
//  6. 禁止自交：任意两条非相邻段不得相交或共线重叠
// ————————————————————————————————————————————————————————————————————————
type OrthoDir = 'cw' | 'ccw'
type Axis = 'H' | 'V'
type OrthoSide = 'l' | 'r' | 't' | 'b'
type OrthoResult = {
  pts: Pt[]; corners: Pt[]; dir: OrthoDir
  hits: number; cross: number; score: number
  missing: number   // 未被保留成折角的用户控制点数
  valid: boolean    // 全水平/垂直段且中间点全是折角
}

const ORTHO_MARGIN = 10   // 锚点离节点角的最小距离
const ORTHO_EPS = 0.5
const ORTHO_BULGE = 24    // 插入控制点时的法向凸起量，保证新控制点立刻是折角
const ORTHO_SIDES: OrthoSide[] = ['l', 'r', 't', 'b']

function nodeRect(n: any): Rect {
  return { l: n.x, t: n.y, r: n.x + NODE_W, b: n.y + NODE_H }
}
function sideAxis(s: OrthoSide): Axis { return s === 'l' || s === 'r' ? 'H' : 'V' }
function sideFixed(s: OrthoSide, r: Rect): number {
  if (s === 'l') return r.l
  if (s === 'r') return r.r
  if (s === 't') return r.t
  return r.b
}
function sideRange(s: OrthoSide, r: Rect): [number, number] {
  return s === 'l' || s === 'r'
    ? [r.t + ORTHO_MARGIN, r.b - ORTHO_MARGIN]
    : [r.l + ORTHO_MARGIN, r.r - ORTHO_MARGIN]
}
/** 锚点：把可变坐标 clamp 在边的有效范围内，保证「落在节点上」且沿法线正出正入 */
function pointOnSide(s: OrthoSide, r: Rect, v: number): Pt {
  const [lo, hi] = sideRange(s, r)
  const c = Math.max(lo, Math.min(hi, v))
  return s === 'l' || s === 'r'
    ? { x: sideFixed(s, r), y: c }
    : { x: c, y: sideFixed(s, r) }
}
function segDir(p: Pt, q: Pt): Axis | null {
  if (Math.abs(p.y - q.y) < ORTHO_EPS) return 'H'
  if (Math.abs(p.x - q.x) < ORTHO_EPS) return 'V'
  return null
}
function segLen(p: Pt, q: Pt): number { return Math.abs(q.x - p.x) + Math.abs(q.y - p.y) }

/** 两条轴对齐线段是否相交（含端点相接与共线重叠） */
function segsOverlap(p: Pt, q: Pt, s: Pt, t: Pt): boolean {
  const lo = (a: number, b: number) => Math.min(a, b)
  const hi = (a: number, b: number) => Math.max(a, b)
  const h1 = Math.abs(p.y - q.y) < ORTHO_EPS, h2 = Math.abs(s.y - t.y) < ORTHO_EPS
  const v1 = Math.abs(p.x - q.x) < ORTHO_EPS, v2 = Math.abs(s.x - t.x) < ORTHO_EPS
  if (h1 && h2) return Math.abs(p.y - s.y) < ORTHO_EPS && Math.min(hi(p.x, q.x), hi(s.x, t.x)) >= Math.max(lo(p.x, q.x), lo(s.x, t.x)) - ORTHO_EPS
  if (v1 && v2) return Math.abs(p.x - s.x) < ORTHO_EPS && Math.min(hi(p.y, q.y), hi(s.y, t.y)) >= Math.max(lo(p.y, q.y), lo(s.y, t.y)) - ORTHO_EPS
  return Math.min(hi(p.x, q.x), hi(s.x, t.x)) >= Math.max(lo(p.x, q.x), lo(s.x, t.x)) - ORTHO_EPS
    && Math.min(hi(p.y, q.y), hi(s.y, t.y)) >= Math.max(lo(p.y, q.y), lo(s.y, t.y)) - ORTHO_EPS
}
/** 自交计数：相邻段共享端点是正常折角，跳过；非相邻段任何接触都算违规 */
function selfCrossCount(pts: Pt[]): number {
  const segs: [Pt, Pt][] = []
  for (let i = 0; i < pts.length - 1; i++) {
    if (segDir(pts[i], pts[i + 1]) && segLen(pts[i], pts[i + 1]) > ORTHO_EPS) segs.push([pts[i], pts[i + 1]])
  }
  let n = 0
  for (let i = 0; i < segs.length; i++) {
    for (let j = i + 2; j < segs.length; j++) {
      if (segsOverlap(segs[i][0], segs[i][1], segs[j][0], segs[j][1])) n++
    }
  }
  return n
}

/** 化简：三点共线（同向或 180° 折返）就去掉中间点；最终中间点全是 90° 折角 */
function simplifyOrtho(pts: Pt[]): Pt[] {
  let out = dedupePts(pts)
  for (let pass = 0; pass < 8 && out.length > 2; pass++) {
    const next: Pt[] = [out[0]]
    for (let i = 1; i < out.length - 1; i++) {
      const a = next[next.length - 1], b = out[i], c = out[i + 1]
      const h = Math.abs(a.y - b.y) < ORTHO_EPS && Math.abs(b.y - c.y) < ORTHO_EPS
      const v = Math.abs(a.x - b.x) < ORTHO_EPS && Math.abs(b.x - c.x) < ORTHO_EPS
      if (!h && !v) next.push(b)
    }
    next.push(out[out.length - 1])
    const cleaned = dedupePts(next)
    if (JSON.stringify(cleaned) === JSON.stringify(out)) return out
    out = cleaned
  }
  return out
}

/** 评分：自交是硬约束，其次穿节点，再是总长度与拐点数量（倾向最简路径） */
function orthoCost(pts: Pt[], rects: Rect[]) {
  const h = routeHits(pts, rects)
  const cross = selfCrossCount(pts)
  return {
    hits: h.n,
    cross,
    score: cross * 100000 + h.n * 10000 + h.len * 3 + polylineLen(pts) + (pts.length - 2) * 12,
  }
}

/** 屏幕坐标 y 向下：首段向量到末段向量右转 = 顺时针 */
function turnDir(pts: Pt[]): OrthoDir {
  if (pts.length < 3) return 'cw'
  const p0 = pts[0], p1 = pts[1], q0 = pts[pts.length - 2], q1 = pts[pts.length - 1]
  const cross = (p1.x - p0.x) * (q1.y - q0.y) - (p1.y - p0.y) * (q1.x - q0.x)
  return cross >= 0 ? 'cw' : 'ccw'
}
function centerDelta(ra: Rect, rb: Rect) {
  return { dx: (rb.l + rb.r - ra.l - ra.r) / 2, dy: (rb.t + rb.b - ra.t - ra.b) / 2 }
}
/** 默认方向：水平主导走「先横后竖」，垂直主导走「先竖后横」，换算成顺/逆时针标签 */
function defaultTurnDir(ra: Rect, rb: Rect): OrthoDir {
  const { dx, dy } = centerDelta(ra, rb)
  const cwHV: OrthoDir = dx * dy >= 0 ? 'cw' : 'ccw'
  if (Math.abs(dx) >= Math.abs(dy)) return cwHV
  return cwHV === 'cw' ? 'ccw' : 'cw'
}
/** 两条标准 L 使用的出边 / 入边（各贴包围盒一侧） */
function standardLSides(ra: Rect, rb: Rect): [OrthoSide, OrthoSide][] {
  const { dx, dy } = centerDelta(ra, rb)
  return [
    [dx >= 0 ? 'r' : 'l', dy >= 0 ? 't' : 'b'],
    [dy >= 0 ? 'b' : 't', dx >= 0 ? 'l' : 'r'],
  ]
}

/** 把有序点序列正交化：相邻点不同 x 且不同 y 时自动插入 L 拐点。
 *  拐点朝向按「不穿节点 / 不自交 / 方向交替」就地择优；
 *  turnFlags 标记的点（用户控制点）必须成为 90° 折角，其前后段方向强制不同。 */
function orthogonalize(
  raw: Pt[], rects: Rect[],
  firstDir?: Axis, lastDir?: Axis, turnFlags?: boolean[],
  forceTail = false,
): Pt[] {
  const out: Pt[] = [raw[0]]
  let prevDir: Axis | null = null
  let forbidOut: Axis | null = null  // 上一必经折角的入射方向：本次出发必须换方向
  const last = raw.length - 2
  for (let i = 0; i <= last; i++) {
    const p = out[out.length - 1], q = raw[i + 1]
    if (!p || !q) continue
    const wantFirst = i === 0 ? firstDir : undefined
    const wantLast = i === last ? lastDir : undefined
    const isTurn = !!turnFlags?.[i + 1]
    const d = segDir(p, q)
    const direct = d && segLen(p, q) > ORTHO_EPS
      && (!forbidOut || d !== forbidOut)
      && (!wantFirst || d === wantFirst)
      && (!wantLast || d === wantLast)
    if (direct) {
      out.push(q)
      prevDir = d
      forbidOut = isTurn ? d : null
      continue
    }
    const cands: Pt[] = [{ x: q.x, y: p.y }, { x: p.x, y: q.y }]
    let best: Pt | null = null, bestCost = Infinity
    for (const c of cands) {
      if (segLen(p, c) < ORTHO_EPS || segLen(c, q) < ORTHO_EPS) continue
      const od = segDir(p, c), ad = segDir(c, q)
      if (!od || !ad) continue
      if (wantFirst && od !== wantFirst) continue
      if (wantLast && ad !== wantLast) continue
      if (forbidOut && od === forbidOut) continue
      const trial = simplifyOrtho([...out, c, q])
      const cost = orthoCost(trial, rects).score + (prevDir && od === prevDir ? 60 : 0)
      if (cost < bestCost) { bestCost = cost; best = c }
    }
    if (best) {
      out.push(best, q)
      prevDir = segDir(best, q)
    } else if (wantFirst && wantLast && wantFirst === wantLast && !isTurn) {
      // 出边与入边同向：一个 L 拐点不够，插入两个拐点组成 Z 形（H-V-H / V-H-V）
      const t = wantFirst === 'V' ? (p.y + q.y) / 2 : (p.x + q.x) / 2
      const z: Pt[] = wantFirst === 'V'
        ? [{ x: p.x, y: t }, { x: q.x, y: t }]
        : [{ x: t, y: p.y }, { x: t, y: q.y }]
      out.push(...z, q)
      prevDir = wantLast
    } else if (forceTail && wantLast) {
      // 手动锚点的入边是硬约束：局部贪心无解（出发方向与末段方向冲突、
      // 或端点对齐使 L 拐点退化）时，显式构造 3/4 段交替折线保证末段沿法线正入，
      // 而不是让候选被淘汰、进而退化成「牺牲全部用户拐点」的自动布线。
      const G = ORTHO_BULGE
      const pickT = (a: number, b: number) =>
        [(a + b) / 2, a + G, b - G, (a + b) / 2 + G].find(v =>
          Math.abs(v - a) > ORTHO_EPS && Math.abs(v - b) > ORTHO_EPS) ?? (a + G * 1.5)
      const needFour = forbidOut === wantLast
        || (wantLast === 'H' ? Math.abs(p.y - q.y) < ORTHO_EPS : Math.abs(p.x - q.x) < ORTHO_EPS)
      if (wantLast === 'H') {
        const t = pickT(p.x, q.x)
        if (needFour) {
          const s = pickT(p.y, q.y)
          out.push({ x: p.x, y: s }, { x: t, y: s }, { x: t, y: q.y }, q)
        } else out.push({ x: t, y: p.y }, { x: t, y: q.y }, q)
      } else {
        const t = pickT(p.y, q.y)
        if (needFour) {
          const s = pickT(p.x, q.x)
          out.push({ x: s, y: p.y }, { x: s, y: t }, { x: q.x, y: t }, q)
        } else out.push({ x: p.x, y: t }, { x: q.x, y: t }, q)
      }
      prevDir = wantLast
    } else {
      // 兜底：宁可多一个拐点，也不允许出现斜线段或抹掉用户拐点
      let c = cands.find(v => segLen(p, v) > ORTHO_EPS && segLen(v, q) > ORTHO_EPS
        && (!forbidOut || segDir(p, v) !== forbidOut))
      if (!c) c = cands.find(v => segLen(p, v) > ORTHO_EPS && segLen(v, q) > ORTHO_EPS)
      if (c) out.push(c)
      out.push(q)
      prevDir = c ? segDir(c, q) : prevDir
    }
    forbidOut = isTurn ? prevDir : null
  }
  return simplifyOrtho(out)
}

function missingWaypoints(pts: Pt[], wps: Pt[]): number {
  const corners = pts.slice(1, -1)
  return wps.filter(w => !corners.some(c =>
    Math.abs(c.x - w.x) < ORTHO_EPS && Math.abs(c.y - w.y) < ORTHO_EPS)).length
}
/** 确定性交替构造：段方向严格 H/V 交替，每个用户拐点必然落在段边界（即折角）上。
 *  作为局部贪心的补充候选，专门避免「用户拐点被化简抹掉」。 */
function alternatingPath(anchorA: Pt, anchorB: Pt, wps: Pt[], firstDir: Axis, lastDir: Axis): Pt[] | null {
  const pts: Pt[] = [anchorA]
  let d: Axis = firstDir
  const targets = [...wps, anchorB]
  for (let i = 0; i < targets.length; i++) {
    const p = pts[pts.length - 1], q = targets[i]
    const aligned = d === 'H' ? Math.abs(p.y - q.y) < ORTHO_EPS : Math.abs(p.x - q.x) < ORTHO_EPS
    if (aligned && segLen(p, q) > ORTHO_EPS) {
      pts.push(q)                                  // 1 段：到达方向 d，离开方向 !d
      d = d === 'H' ? 'V' : 'H'
    } else {
      const c = d === 'H' ? { x: q.x, y: p.y } : { x: p.x, y: q.y }
      if (segLen(p, c) < ORTHO_EPS || segLen(c, q) < ORTHO_EPS) return null
      pts.push(c, q)                               // 2 段：到达方向 !d，离开方向 d
    }
  }
  const lastSeg = segDir(pts[pts.length - 2], pts[pts.length - 1])
  if (lastSeg !== lastDir) return null
  return simplifyOrtho(pts)
}

/** 单条候选路径：给定出入边，派生锚点 → 正交化 → 化简 → 评分 */
function orthoCandidate(
  ra: Rect, rb: Rect, userWps: Pt[],
  sa: OrthoSide, sb: OrthoSide,
  bias: number, biasAxis: 'x' | 'y' | null,
  pref: OrthoDir | null, rects: Rect[],
  forced: Pt[] | null, strict: boolean,
  anchors?: { from: AnchorSpec | null; to: AnchorSpec | null } | null,
): OrthoResult | null {
  const sAxis = sideAxis(sa), eAxis = sideAxis(sb)
  const [aLo, aHi] = sideRange(sa, ra)
  const [bLo, bHi] = sideRange(sb, rb)
  const midA = (aLo + aHi) / 2, midB = (bLo + bHi) / 2
  const first = forced?.[0] || userWps[0] || null
  const last = forced?.[forced.length - 1] || userWps[userWps.length - 1] || null
  // 手动锚点是硬约束 > 相邻拐点的正交投影（线最短） > 自动布线的边中点
  const fromSpec = anchors?.from && anchors.from.side === sa ? anchors.from : null
  const toSpec = anchors?.to && anchors.to.side === sb ? anchors.to : null
  const aVar = fromSpec ? anchorVar(fromSpec, ra)
    : (first ? (sAxis === 'H' ? first.y : first.x) : midA)
      + (biasAxis === (sAxis === 'H' ? 'y' : 'x') ? bias : 0)
  const bVar = toSpec ? anchorVar(toSpec, rb)
    : (last ? (eAxis === 'H' ? last.y : last.x) : midB)
      + (biasAxis === (eAxis === 'H' ? 'y' : 'x') ? bias : 0)
  const anchorA = pointOnSide(sa, ra, aVar)
  const anchorB = pointOnSide(sb, rb, bVar)
  // 用户控制点必须落在折角上；自动绕行的通道点只是引导，可被化简合并
  const flags = [false, ...(forced || []).map(() => false), ...userWps.map(() => true), false]
  let pts = orthogonalize([anchorA, ...(forced || []), ...userWps, anchorB], rects, sAxis, eAxis, flags, !!toSpec)
  if (userWps.length && !forced) {
    // 补充候选：确定性交替构造，保证用户拐点不被化简抹掉
    const alt = alternatingPath(anchorA, anchorB, userWps, sAxis, eAxis)
    if (alt) {
      // 手动锚点方向也是硬约束：优先选用满足正出正入的构造，避免整个候选被淘汰、
      // 进而退化成「牺牲全部用户拐点」的自动布线
      const dirPenalty = (p: Pt[]) =>
        (fromSpec && segDir(p[0], p[1]) !== sAxis ? 100000 : 0)
        + (toSpec && segDir(p[p.length - 2], p[p.length - 1]) !== eAxis ? 100000 : 0)
      const rank = (p: Pt[]) => orthoCost(p, rects).score
        + missingWaypoints(p, userWps) * 20000 + dirPenalty(p)
      if (rank(alt) < rank(pts)) pts = alt
    }
  }
  if (pts.length < 2) return null
  // 手动锚点是硬约束：即使是非严格兜底候选，也必须沿指定边的法线正出正入。
  // 否则用户拐点顺序与入边冲突时，会选出「锚点在边上但线贴着边进来」的路径。
  if (fromSpec && segDir(pts[0], pts[1]) !== sAxis) return null
  if (toSpec && segDir(pts[pts.length - 2], pts[pts.length - 1]) !== eAxis) return null
  // 首尾段必须垂直于所选边（正出正入）；严格模式不满足即淘汰该候选
  if (strict) {
    if (segDir(pts[0], pts[1]) !== sAxis) return null
    if (segDir(pts[pts.length - 2], pts[pts.length - 1]) !== eAxis) return null
  }
  const { score, hits, cross } = orthoCost(pts, rects)
  const dir = turnDir(pts)
  const { dx, dy } = centerDelta(ra, rb)
  const natA = sa === (dx >= 0 ? 'r' : 'l') || sa === (dy >= 0 ? 'b' : 't')
  const natB = sb === (dx >= 0 ? 'l' : 'r') || sb === (dy >= 0 ? 't' : 'b')
  const missing = missingWaypoints(pts, userWps)
  const valid = pts.every((p, i) => i === 0 || segDir(pts[i - 1], p) !== null)
    && pts.every((p, i) => i === 0 || i === pts.length - 1
      || segDir(pts[i - 1], p) !== segDir(p, pts[i + 1]))
  return {
    pts, corners: pts.slice(1, -1), dir, hits, cross, missing, valid,
    score: score + missing * 20000 + (pref && dir !== pref ? 320 : 0)
      + (natA ? 0 : 180) + (natB ? 0 : 180),
  }
}

/** 两侧 L 都被挡时：沿阻挡节点外缘 / 包围盒外侧采样通道，生成 Z / U 形多拐点路径 */
function channelValues(ra: Rect, rb: Rect, rects: Rect[], axis: 'x' | 'y'): number[] {
  const vals = new Set<number>()
  const G = AVOID_GAP
  const box = {
    l: Math.min(ra.l, rb.l), t: Math.min(ra.t, rb.t),
    r: Math.max(ra.r, rb.r), b: Math.max(ra.b, rb.b),
  }
  if (axis === 'x') {
    vals.add(Math.round(box.l - G * 2)); vals.add(Math.round(box.r + G * 2))
    for (const r of rects) { vals.add(Math.round(r.l - G)); vals.add(Math.round(r.r + G)) }
  } else {
    vals.add(Math.round(box.t - G * 2)); vals.add(Math.round(box.b + G * 2))
    for (const r of rects) { vals.add(Math.round(r.t - G)); vals.add(Math.round(r.b + G)) }
  }
  return [...vals].sort((a, b) => a - b).slice(0, 10)
}
function detourCandidates(
  ra: Rect, rb: Rect, sa: OrthoSide, sb: OrthoSide,
  bias: number, biasAxis: 'x' | 'y' | null,
  pref: OrthoDir | null, rects: Rect[],
  anchors?: { from: AnchorSpec | null; to: AnchorSpec | null } | null,
): OrthoResult[] {
  const xs = channelValues(ra, rb, rects, 'x')
  const ys = channelValues(ra, rb, rects, 'y')
  const cyA = (ra.t + ra.b) / 2, cyB = (rb.t + rb.b) / 2
  const cxA = (ra.l + ra.r) / 2, cxB = (rb.l + rb.r) / 2
  const out: OrthoResult[] = []
  for (const x of xs) for (const y of ys) {
    const paths: Pt[][] = [
      [{ x, y: cyA }, { x, y: cyB }],                       // 竖直通道 Z
      [{ x: cxA, y }, { x: cxB, y }],                       // 水平通道 Z
      [{ x, y: cyA }, { x, y }, { x: cxB, y }],             // U 形绕外侧
      [{ x: cxA, y }, { x, y }, { x, y: cyB }],
    ]
    for (const forced of paths) {
      const r = orthoCandidate(ra, rb, [], sa, sb, bias, biasAxis, pref, rects, forced, true, anchors)
      if (r) out.push(r)
    }
  }
  return out
}

/** ortho 避让矩形：只看其他节点（起止节点本身已在边上，无需判定穿体） */
function orthoBlockRects(a: any, b?: any): Rect[] {
  const out: Rect[] = []
  for (const n of nodes.value) {
    if (n.id === a.id || n.id === b?.id) continue
    out.push({
      l: n.x - AVOID_PAD, t: n.y - AVOID_PAD,
      r: n.x + NODE_W + AVOID_PAD, b: n.y + NODE_H + AVOID_PAD,
    })
  }
  return out
}
function orthoDirOf(e: any): OrthoDir | null {
  return e?.ortho_dir === 'cw' || e?.ortho_dir === 'ccw' ? e.ortho_dir : null
}
function orthoBiasInfo(e: any, a: any, b: any): { bias: number; axis: 'x' | 'y' | null } {
  const bias = orthoBias(e)
  if (!bias) return { bias: 0, axis: null }
  const acy = a.y + NODE_H / 2, bcy = b.y + NODE_H / 2
  const acx = a.x + NODE_W / 2, bcx = b.x + NODE_W / 2
  if (Math.abs(bcy - acy) < ORTHO_EPS) return { bias, axis: 'y' }
  if (Math.abs(bcx - acx) < ORTHO_EPS) return { bias, axis: 'x' }
  return { bias: 0, axis: null }
}

/** 主入口：手动拐点为硬约束；自动布线按「双 L → 切换 → 多拐点绕行」逐级升级 */
function routeOrthoEdge(
  a: any, b: any, userWps: Pt[],
  pref: OrthoDir | null, bias: number, biasAxis: 'x' | 'y' | null,
  rects: Rect[],
  anchors?: { from: AnchorSpec | null; to: AnchorSpec | null } | null,
): OrthoResult {
  const ra = nodeRect(a), rb = nodeRect(b)
  const effectivePref = pref || defaultTurnDir(ra, rb)
  const fromSide = anchors?.from?.side ?? null
  const toSide = anchors?.to?.side ?? null
  // 手动锚点 = 硬约束：出入边只允许落在用户指定的那一条边上
  const limit = (list: [OrthoSide, OrthoSide][]) => list.filter(
    ([sa, sb]) => (!fromSide || sa === fromSide) && (!toSide || sb === toSide))
  const allSides: [OrthoSide, OrthoSide][] = []
  for (const sa of ORTHO_SIDES) for (const sb of ORTHO_SIDES) allSides.push([sa, sb])
  const full = limit(allSides)
  const build = (sides: [OrthoSide, OrthoSide][], strict: boolean) => {
    const out: OrthoResult[] = []
    for (const [sa, sb] of sides) {
      const r = orthoCandidate(ra, rb, userWps, sa, sb, bias, biasAxis, effectivePref, rects, null, strict, anchors)
      if (r) out.push(r)
    }
    return out
  }
  const fallbackSides = full.length ? full : allSides

  let cands: OrthoResult[]
  if (userWps.length) {
    cands = build(fallbackSides, true)
    if (!cands.length) cands = build(fallbackSides, false)
  } else {
    const two = limit(standardLSides(ra, rb))
    const sides = two.length ? two : fallbackSides
    cands = build(sides, true)
    if (!cands.length) cands = build(sides, false)
    // 两个方向都挡 → 增加拐点绕开节点；同时允许换出入边作为兜底
    if (cands.length && cands.every(c => c.hits > 0)) {
      for (const [sa, sb] of sides.slice(0, 4)) {
        cands.push(...detourCandidates(ra, rb, sa, sb, bias, biasAxis, effectivePref, rects, anchors))
      }
      cands.push(...build(fallbackSides, true))
    }
  }

  if (!cands.length) {
    // 手动锚点与用户拐点顺序冲突：牺牲拐点，按锚点重新自动布线（锚点仍是硬约束）
    if (anchors && userWps.length) {
      return routeOrthoEdge(a, b, [], pref, bias, biasAxis, rects, anchors)
    }
    const start = anchors?.from ? anchorPoint(anchors.from, ra)
      : { x: (ra.l + ra.r) / 2, y: (ra.t + ra.b) / 2 }
    const end = anchors?.to ? anchorPoint(anchors.to, rb)
      : { x: (rb.l + rb.r) / 2, y: (rb.t + rb.b) / 2 }
    const pts = orthogonalize([start, end], rects,
      fromSide ? sideAxis(fromSide) : undefined, toSide ? sideAxis(toSide) : undefined)
    return {
      pts, corners: pts.slice(1, -1), dir: 'cw', hits: 0, cross: 0, score: Infinity,
      missing: userWps.length, valid: false,
    }
  }
  const legal = cands.filter(c => c.valid)
  const base = legal.length ? legal : cands
  const best = (list: OrthoResult[]) => list.reduce((a, b) => b.score < a.score ? b : a)
  // 分级：优先「不穿节点 + 不自交 + 保留全部用户拐点」，逐级放宽；自交始终最后才考虑
  const levels = [
    base.filter(c => c.cross === 0 && c.hits === 0 && c.missing === 0),
    base.filter(c => c.cross === 0 && c.missing === 0),
    base.filter(c => c.cross === 0),
  ]
  for (const lv of levels) if (lv.length) return best(lv)
  // 所有候选都自交（例如用户拐点顺序本身绕圈）→ 重规划：忽略手动拐点按自动布线重算
  if (userWps.length) {
    const replanned = routeOrthoEdge(a, b, [], pref, bias, biasAxis, rects, anchors)
    if (replanned.cross === 0) return replanned
  }
  return best(base)
}

function computeEdge(e: any) {
  const a = nodeById.value.get(e.from_node)
  const b = nodeById.value.get(e.to_node)
  if (!a || !b) return null
  // 分段连线是用户逐段画出来的显式路径：优先渲染，不参与自动布线 / 避让
  const segs = readSegments(e)
  if (segs.length) return computeMixedEdge(e, segs)
  const ra = nodeRect(a), rb = nodeRect(b)
  const manualFrom = anchorSpecOf(e, 'from'), manualTo = anchorSpecOf(e, 'to')
  const anchors = (manualFrom || manualTo) ? { from: manualFrom, to: manualTo } : null
  let { p1, p2, horizontal } = sideAnchors(a, b)
  // 手动锚点覆盖自动锚点：曲线 / 斜线直连 / 直角折线通用
  if (manualFrom) p1 = anchorPoint(manualFrom, ra)
  if (manualTo) p2 = anchorPoint(manualTo, rb)
  const shape = edgeShapeOf(e)
  const wps = sanitizeWaypoints(e.waypoints)
  const rev = hasReverseEdge(e)
  // 斜线直连不参与避让；曲线避开所有节点（含起止节点内缩区）；直角折线只看其他节点
  const rects = shape === 'straight' ? [] : shape === 'ortho' ? orthoBlockRects(a, b) : avoidRects(a, b)

  // 1) 自动曲线（无控制点）：沿用原三次曲线 + 双向避让；穿过节点时改为绕行
  if (shape === 'curve' && wps.length === 0) {
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
    const check = rev ? offsetCubicSamples(p1, c1, c2, p2, BIDIR_OFFSET) : cubicSamples(p1, c1, c2, p2)
    const baseHits = routeHits(check, rects)
    // 穿过节点 → 沿法线绕行。绕行方向取同侧（反向线的法线相反，天然落到遮挡另一侧）。
    // 只有确实比原路径更干净时才改走绕行，避免绕行反而更糟。
    const det = baseHits.n > 0 ? detourCurve([p1, p2], rects, 1) : null
    const detScore = det ? routeHits(det.samples, rects).n * 10000 + routeHits(det.samples, rects).len * 5 : Infinity
    const baseScore = baseHits.n * 10000 + baseHits.len * 5
    if (!det || detScore >= baseScore) {
      // 双向两条线：沿曲线法线做等距偏移（采样+平滑）→ 两条线处处平行、垂直间距恒定
      const d = rev
        ? offsetEdgePath(p1, c1, c2, p2, BIDIR_OFFSET)
        : `M ${p1.x} ${p1.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${p2.x} ${p2.y}`
      const mid = cubicAt(p1, c1, c2, p2, 0.5)
      const tang = cubicTangentAt(p1, c1, c2, p2, 0.5)
      const tlen = Math.hypot(tang.x, tang.y) || 1
      const midX = mid.x + (rev ? -tang.y / tlen * BIDIR_OFFSET : 0)
      const midY = mid.y + (rev ? tang.x / tlen * BIDIR_OFFSET : 0)
      const handles: EdgeHandle[] = [{ kind: 'insert', index: 0, x: midX, y: midY }]
      return { d, midX, midY, midAngle: Math.atan2(tang.y, tang.x) * 180 / Math.PI, handles }
    }
    const mid = polylineMid(det.samples)
    return {
      d: det.d, midX: mid.x, midY: mid.y, midAngle: mid.angle,
      handles: [{ kind: 'insert', index: 0, x: mid.x, y: mid.y }],
    }
  }

  // 2) 直角折线：锚点派生、控制点严格等于折角
  if (shape === 'ortho') {
    const { bias, axis } = orthoBiasInfo(e, a, b)
    const route = routeOrthoEdge(a, b, wps, orthoDirOf(e), bias, axis, rects, anchors)
    const pts = route.pts
    const mid = polylineMid(pts)
    const handles: EdgeHandle[] = []
    // 渲染出的每个折角都是可拖动的控制点
    route.corners.forEach((p, i) => handles.push({ kind: 'point', index: i, x: p.x, y: p.y }))
    // 插入柄放在段中点并带法向：新控制点一插入就是折角，不会落在直线段中间
    for (let i = 0; i < pts.length - 1; i++) {
      const p = pts[i], q = pts[i + 1]
      const h = segDir(p, q)
      handles.push({
        kind: 'insert', index: i,
        x: (p.x + q.x) / 2, y: (p.y + q.y) / 2,
        nx: h === 'V' ? 1 : 0, ny: h === 'H' ? -1 : 0,
      })
    }
    return {
      d: linePath(pts), midX: mid.x, midY: mid.y, midAngle: mid.angle, handles,
      orthoCorners: route.corners, orthoDir: route.dir,
      anchorFrom: pts[0], anchorTo: pts[pts.length - 1],
    }
  }

  // 3) 直线 / 含控制点的曲线
  const pts: Pt[] = [p1, ...wps, p2]
  const smooth = shape === 'curve'
  if (smooth) {
    const base = smoothSamples(pts, AVOID_SAMPLES)
    const baseHits = routeHits(base, rects)
    // 穿过节点 → 在各段间插入绕行控制点；手动控制点仍被曲线严格穿过
    if (baseHits.n > 0) {
      const det = detourCurve(pts, rects, 1)
      const detHits = routeHits(det.samples, rects)
      if (detHits.n * 10000 + detHits.len * 5 < baseHits.n * 10000 + baseHits.len * 5) {
        const mid = polylineMid(det.samples)
        const handles: EdgeHandle[] = []
        wps.forEach((p, i) => handles.push({ kind: 'point', index: i, x: p.x, y: p.y }))
        det.mids.forEach((m, i) => handles.push({ kind: 'insert', index: i, x: m.x, y: m.y }))
        return { d: det.d, midX: mid.x, midY: mid.y, midAngle: mid.angle, handles }
      }
    }
  }
  const d = smooth ? smoothThrough(pts) : linePath(pts)
  const handles: EdgeHandle[] = []
  wps.forEach((p, i) => handles.push({ kind: 'point', index: i, x: p.x, y: p.y }))
  for (let i = 0; i < pts.length - 1; i++) {
    const m = segmentMidpoint(pts, i, smooth)
    handles.push({ kind: 'insert', index: i, x: m.x, y: m.y })
  }
  const midSeg = Math.max(0, Math.floor((pts.length - 2) / 2))
  const mid = segmentMidpoint(pts, midSeg, smooth)
  let angle = 0
  if (smooth) {
    const q1 = pts[midSeg], q2 = pts[midSeg + 1]
    const q0 = pts[midSeg - 1] || q1
    const q3 = pts[midSeg + 2] || q2
    const c1 = { x: q1.x + (q2.x - q0.x) / 6, y: q1.y + (q2.y - q0.y) / 6 }
    const c2 = { x: q2.x - (q3.x - q1.x) / 6, y: q2.y - (q3.y - q1.y) / 6 }
    const tang = cubicTangentAt(q1, c1, c2, q2, 0.5)
    angle = Math.atan2(tang.y, tang.x) * 180 / Math.PI
  } else {
    angle = Math.atan2(pts[midSeg + 1].y - pts[midSeg].y, pts[midSeg + 1].x - pts[midSeg].x) * 180 / Math.PI
  }
  return { d, midX: mid.x, midY: mid.y, midAngle: angle, handles }
}

/** 两端锚点手柄：手动锚点按设定落点；自动锚点用实际渲染出的首末点 */
function anchorHandlesFor(e: any, g: any): EdgeHandle[] {
  const a = nodeById.value.get(e.from_node), b = nodeById.value.get(e.to_node)
  if (!a || !b) return []
  const { p1, p2 } = sideAnchors(a, b)
  const f = anchorSpecOf(e, 'from'), t = anchorSpecOf(e, 'to')
  const from = f ? anchorPoint(f, nodeRect(a)) : (g?.anchorFrom || p1)
  const to = t ? anchorPoint(t, nodeRect(b)) : (g?.anchorTo || p2)
  return [
    { kind: 'anchor', end: 'from', index: 0, x: from.x, y: from.y },
    { kind: 'anchor', end: 'to', index: 1, x: to.x, y: to.y },
  ]
}
const edgeGeoms = computed(() => edges.value.map((e: any) => {
  const g = computeEdge(e)
  if (g) g.handles = [...anchorHandlesFor(e, g), ...(g.handles || [])]
  return { e, g, color: g ? edgeSourceColor(e) : 'var(--accent)', ortho: edgeShapeOf(e) === 'ortho' }
}))

// —— 连线控制点交互 ——
function showEdgeHandles(e: any): boolean {
  return hoveringEdgeId.value === e.id || edgeDrag.value?.edge?.id === e.id
}
/** 鼠标进入锚点周围多少「屏幕像素」时，锚点才显现（除以 scale 换算成画布坐标） */
const ANCHOR_PEEK_PX = 26
/** 控制柄层内容：拖动中 / DOM-hover 边时显示整条边的手柄；否则用「到手柄包围盒的距离」
 *  判定（含一定外扩）。贝塞尔曲线完全落在其控制点+锚点构成的包围盒内，曲线控制点（离线
 *  虚线圆点）也带进包围盒 —— 鼠标从边线移向控制点中途不会失去显示；且距离判定不依赖
 *  DOM hover，鼠标踩上手柄（pointer-events:all）导致边组 mouseleave 时手柄也不会消失，
 *  避免「控制点一放上去就没了」的闪烁与抽搐。 */
function visibleHandles(e: any, g: any): EdgeHandle[] {
  const hs: EdgeHandle[] = g?.handles || []
  if (showEdgeHandles(e)) return hs
  const p = pointerCanvas.value
  if (!p || !hs.length) return []
  const inflate = 40 / (scale.value || 1)
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const h of hs) {
    if (h.x < minX) minX = h.x
    if (h.y < minY) minY = h.y
    if (h.x > maxX) maxX = h.x
    if (h.y > maxY) maxY = h.y
  }
  const hit = p.x >= minX - inflate && p.x <= maxX + inflate &&
              p.y >= minY - inflate && p.y <= maxY + inflate
  return hit ? hs : []
}
function orthoGeomOf(edge: any): any {
  return edgeGeoms.value.find((x: any) => x.e.id === edge.id)?.g
}
/** 把渲染出的折角固化回 waypoints：控制点严格等于拐点，重合/共线的多余拐点一并消除 */
function commitOrthoGeometry(edge: any, withHistory: boolean): boolean {
  const corners = orthoGeomOf(edge)?.orthoCorners as Pt[] | undefined
  if (!corners) return false
  const wps = ensureWaypoints(edge)
  const same = wps.length === corners.length && wps.every((p, i) =>
    Math.abs(p.x - corners[i].x) < ORTHO_EPS && Math.abs(p.y - corners[i].y) < ORTHO_EPS)
  if (same) return false
  if (withHistory) pushHistory()
  wps.splice(0, wps.length, ...corners.map(p => ({ x: p.x, y: p.y })))
  return true
}
function startEdgeHandleDrag(e: MouseEvent, edge: any, h: EdgeHandle) {
  if (e.button !== 0) return
  e.stopPropagation(); e.preventDefault()
  // 分段连线的柄属于 segments：顶点/曲线控制点/圆弧矢高可拖，插入柄用于拆分段
  if (h.seg != null) {
    if (h.kind === 'insert') { splitDrawSegment(edge, h.seg); return }
    pendingEdgeSnapshot = snapshotCanvas()
    edgeDrag.value = { edge, index: -1, seg: h.seg, field: h.kind as 'vertex' | 'ctrl' | 'bulge', ctrl: h.ctrl }
    closeMenus()
    return
  }
  // 拖锚点：走独立通道，不固化渲染拐点（否则会把自动布线锁死）
  if (h.kind === 'anchor' && h.end) {
    pendingEdgeSnapshot = snapshotCanvas()
    edgeDrag.value = { edge, index: -1, anchor: h.end }
    closeMenus()
    return
  }
  const isOrtho = edgeShapeOf(edge) === 'ortho'
  // ortho：先把渲染拐点固化，保证控制点就是折角、手柄 index 一定有效
  if (isOrtho) {
    const snap = snapshotCanvas()
    if (commitOrthoGeometry(edge, false)) pushSnapshot(snap)
  }
  const wps = ensureWaypoints(edge)
  if (h.kind === 'insert') {
    pushHistory()
    // 法向凸起：新控制点一插入就是折角，不会落在直线段中间
    const np = isOrtho
      ? { x: h.x + (h.nx || 0) * ORTHO_BULGE, y: h.y + (h.ny || 0) * ORTHO_BULGE }
      : { x: h.x, y: h.y }
    const at = Math.min(h.index, wps.length)
    wps.splice(at, 0, np)
    edgeDrag.value = { edge, index: at }
  } else {
    pendingEdgeSnapshot = snapshotCanvas()
    edgeDrag.value = { edge, index: h.index }
  }
  closeMenus()
}
/** 分段手柄拖动：顶点改 end、曲线控制点改 ctrl、圆弧按鼠标到弦的法向距离改 bulge */
function moveSegmentHandle(d: NonNullable<typeof edgeDrag.value>, c: Pt) {
  const segs = liveSegments(d.edge)
  const s = segs[d.seg!]
  if (!s || !d.field) return
  if (d.field === 'vertex') {
    // 垂线段的终点必须留在垂线上：拖动时把鼠标投影到当前法线，只改长度 / 朝向
    if (s.type === 'vert' && d.seg != null) {
      const p0 = mixedPoints(d.edge)[d.seg]
      if (p0) { s.end = projectOnNormal(p0, c, mixedNormalAt(d.edge, d.seg)); return }
    }
    s.end = { x: c.x, y: c.y }; return
  }
  if (d.field === 'ctrl') {
    if (!Array.isArray(s.ctrl)) s.ctrl = []
    while (s.ctrl.length < 2) s.ctrl.push({ x: c.x, y: c.y })
    s.ctrl[d.ctrl || 0] = { x: c.x, y: c.y }
    return
  }
  const pts = mixedPoints(d.edge)
  const p0 = pts[d.seg!], p1 = pts[d.seg! + 1]
  if (!p0 || !p1) return
  const dx = p1.x - p0.x, dy = p1.y - p0.y, len = Math.hypot(dx, dy)
  if (len < 1) return
  const nx = -dy / len, ny = dx / len
  const rel = (c.x - (p0.x + p1.x) / 2) * nx + (c.y - (p0.y + p1.y) / 2) * ny
  s.bulge = clampNum(rel / len, -MAX_ARC_BULGE, MAX_ARC_BULGE)
}
function moveEdgeWaypoint(c: Pt) {
  const d = edgeDrag.value
  if (!d) return
  if (d.seg != null) { moveSegmentHandle(d, c); return }
  if (d.anchor) {
    // 手动锚点只吸附在节点边缘：按方向选边，沿边坐标换算成归一化 t
    const node = nodeById.value.get(d.anchor === 'from' ? d.edge.from_node : d.edge.to_node)
    if (!node) return
    const spec = nearestAnchor(nodeRect(node), c)
    d.edge[d.anchor === 'from' ? 'anchor_from' : 'anchor_to'] = spec
    return
  }
  const p = ensureWaypoints(d.edge)[d.index]
  if (!p) return
  // 只改用户拐点；未手动固定的锚点跟随、自动插点、化简、消自交都由渲染层实时重算
  p.x = c.x
  p.y = c.y
}
function endEdgeDrag() {
  pendingEdgeSnapshot = null
  const d = edgeDrag.value
  if (!d) return
  edgeDrag.value = null
  // 拖锚点 / 分段手柄都无需固化拐点，避免把自动布线变成手动控制点
  if (d.anchor || d.seg != null) { canvases.saveCurrent(); return }
  if (edgeShapeOf(d.edge) === 'ortho') commitOrthoGeometry(d.edge, false)
  canvases.saveCurrent()
}
function removeWaypoint(edge: any, index: number) {
  const wps = ensureWaypoints(edge)
  if (index < 0 || index >= wps.length) return
  pushHistory()
  wps.splice(index, 1)
  edgeDrag.value = null
  // 删光控制点即恢复自动布线，别把渲染拐点又固化回去（否则方向偏好会被锁死）
  if (wps.length && edgeShapeOf(edge) === 'ortho') commitOrthoGeometry(edge, false)
  canvases.saveCurrent()
}
/** 双击顶点柄 = 删掉这一段：后面的段自动从上一顶点接过来，路径仍然连续 */
function removeDrawSegment(edge: any, index: number) {
  const segs = liveSegments(edge)
  if (index < 0 || index >= segs.length) return
  pushHistory()
  segs.splice(index, 1)
  if (!segs.length) { edge.segments = []; edge.shape = 'curve' }
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvases.saveCurrent()
  closeMenus()
}
/** 插入柄 / 菜单：把第 i 段在中点拆成两段同类型，保持曲线与圆弧形状不变 */
function splitDrawSegment(edge: any, index: number) {
  const segs = liveSegments(edge)
  const s = segs[index]
  const pts = mixedPoints(edge)
  if (!s || !pts[index] || !pts[index + 1]) return
  const p0 = pts[index], p1 = pts[index + 1]
  const type = isSegType(s.type) ? s.type : 'straight'
  const bulge = Number.isFinite(Number(s.bulge)) ? Number(s.bulge) : DEFAULT_ARC_BULGE
  const built = buildSegment(p0, p1, type, Array.isArray(s.ctrl) ? s.ctrl : [], bulge)
  const mid = { x: built.mid.x, y: built.mid.y }
  let first: any, second: any
  if (type === 'curve') {
    // de Casteljau：t=0.5 精确拆成两段三次贝塞尔，形状与原曲线完全一致
    const lerp = (a: Pt, b: Pt): Pt => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 })
    const c1 = built.ctrl[0], c2 = built.ctrl[1]
    const q0 = lerp(p0, c1), q1 = lerp(c1, c2), q2 = lerp(c2, p1)
    const r0 = lerp(q0, q1), r1 = lerp(q1, q2)
    const m = lerp(r0, r1)
    first = { type, end: { ...m }, ctrl: [q0, r0], bulge }
    second = { type, end: { ...p1 }, ctrl: [r1, q2], bulge }
  } else if (type === 'arc') {
    // 半弧角 = 原弧角/2，其归一化矢高 b'=(1−cos(Δ/4))/(2sin(Δ/4))，符号保持弯向
    const g = arcGeom(p0, p1, bulge)
    let b2 = DEFAULT_ARC_BULGE
    if (g) {
      const quarter = Math.abs(g.delta) / 4
      const denom = 2 * Math.sin(quarter)
      if (denom > 1e-6) b2 = clampNum(Math.sign(bulge) * (1 - Math.cos(quarter)) / denom, -MAX_ARC_BULGE, MAX_ARC_BULGE)
    }
    first = { type, end: { ...mid }, ctrl: [], bulge: b2 }
    second = { type, end: { ...p1 }, ctrl: [], bulge: b2 }
  } else if (type === 'vert') {
    // 拆出的两段仍是垂线，沿用同一条法线
    const n: Pt[] = Array.isArray(s.ctrl) ? s.ctrl.map((p: Pt) => ({ x: p.x, y: p.y })) : []
    first = { type, end: { ...mid }, ctrl: n.map((p: Pt) => ({ ...p })), bulge }
    second = { type, end: { ...p1 }, ctrl: n.map((p: Pt) => ({ ...p })), bulge }
  } else {
    first = { type, end: { ...mid }, ctrl: [], bulge }
    second = { type, end: { ...p1 }, ctrl: [], bulge }
  }
  pushHistory()
  segs.splice(index, 1, first, second)
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvases.saveCurrent()
  closeMenus()
}
/** 清除整条分段路径，退回画布默认单一线型 */
function clearEdgeSegments(edge: any) {
  if (!hasSegments(edge)) return
  pushHistory()
  edge.segments = []
  edge.shape = defaultEdgeShape() === 'mixed' ? 'curve' : defaultEdgeShape()
  edge.waypoints = []
  edge.anchor_from = null
  edge.anchor_to = null
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvases.saveCurrent()
  closeMenus()
  showToast('已清除分段路径，恢复单一线型')
}
function handleTip(h: EdgeHandle, edge?: any): string {
  if (h.kind === 'point') return '拖动改变连线形状；双击删除控制点'
  if (h.kind === 'anchor') return '拖动改变锚点在节点上的位置（自动吸附到节点边）；双击恢复默认'
  if (h.kind === 'vertex') {
    const s = edge && h.seg != null ? liveSegments(edge)[h.seg] : null
    if (s?.type === 'vert') return '拖动改变垂线长度（始终垂直于上一段）；双击删除这一段'
    return '拖动改变该段终点；双击删除这一段'
  }
  if (h.kind === 'ctrl') return '拖动调整曲线弯曲程度'
  if (h.kind === 'bulge') return '拖动调整圆弧弯向与弧度'
  return '拖动以在连线上添加控制点 / 拆出一段'
}
function onEdgeHandleDblClick(edge: any, h: EdgeHandle) {
  if (h.kind === 'vertex' && h.seg != null) { removeDrawSegment(edge, h.seg); return }
  if (h.kind === 'point') removeWaypoint(edge, h.index)
  else if (h.kind === 'anchor' && h.end) resetEdgeAnchors(edge, h.end)
}

/** 恢复锚点默认位置：不传 end 表示两端一起恢复为自动布线 */
function resetEdgeAnchors(edge: any, end?: AnchorEnd) {
  const keys: ('anchor_from' | 'anchor_to')[] = end
    ? [end === 'from' ? 'anchor_from' : 'anchor_to']
    : ['anchor_from', 'anchor_to']
  if (!keys.some(k => edge[k] != null)) return
  pushHistory()
  for (const k of keys) edge[k] = null
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvases.saveCurrent()
  closeMenus()
}
function setEdgeShape(edge: any, shape: EdgeShape) {
  const changed = edgeShapeOf(edge) !== shape
  const cleared = changed && (hasWaypoints(edge) || hasManualAnchors(edge) || hasSegments(edge))
  pushHistory()
  edge.shape = shape
  // 线型一变，旧形状的控制点语义不再适用：一律清除，恢复自动布线
  if (changed) {
    edge.waypoints = []
    // 从分段连线切到单一线型：整条自定义路径也不再保留
    edge.segments = []
    // 线型变了，手动锚点也不继承到新形状，恢复默认自动布线
    edge.anchor_from = null
    edge.anchor_to = null
    edgeDrag.value = null
    pendingEdgeSnapshot = null
    hoveringEdgeId.value = null
  }
  canvases.saveCurrent()
  closeMenus()
  showToast(`连线已设为${shapeLabel(shape)}${cleared ? '，控制点/锚点/分段已清除' : ''}`)
}
function shapeLabel(shape: EdgeShape): string {
  return shape === 'mixed' ? '分段连线' : shape === 'ortho' ? '直角折线'
    : shape === 'straight' ? '斜线直连' : '平滑曲线'
}
function setAllEdgeShapes(shape: EdgeShape) {
  if (!canvas.value) return
  let reset = 0
  pushHistory()
  for (const e of (canvas.value.edges || [])) {
    e.shape = shape
    // 只统一「线型」；顺/逆时针是每条线各自的 ortho_dir，这里绝不动它
    // 批量统一线型：所有连线都丢掉控制点与手动锚点，避免旧形状的设置被新线型继承
    if (hasWaypoints(e) || hasManualAnchors(e) || hasSegments(e)) reset++
    e.waypoints = []
    e.segments = []
    e.anchor_from = null
    e.anchor_to = null
  }
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvas.value.edge_shape = shape
  canvases.saveCurrent()
  closeMenus()
  showToast(`已把全部连线设为${shapeLabel(shape)}${reset ? `（${reset} 条线的控制点已清除）` : ''}`)
}
function addEdgeWaypoint(edge: any) {
  const g = orthoGeomOf(edge)
  if (!g) return
  const inserts = g.handles.filter((h: EdgeHandle) => h.kind === 'insert')
  const h = inserts[Math.floor(inserts.length / 2)]
  if (!h) return
  // 分段连线：中点插入柄 = 把该段拆成两段同类型
  if (h.seg != null) { splitDrawSegment(edge, h.seg); return }
  const isOrtho = edgeShapeOf(edge) === 'ortho'
  if (isOrtho) {
    const snap = snapshotCanvas()
    if (commitOrthoGeometry(edge, false)) pushSnapshot(snap)
  }
  pushHistory()
  const wps = ensureWaypoints(edge)
  const np = isOrtho
    ? { x: h.x + (h.nx || 0) * ORTHO_BULGE, y: h.y + (h.ny || 0) * ORTHO_BULGE }
    : { x: h.x, y: h.y }
  wps.splice(Math.min(h.index, wps.length), 0, np)
  // 自动新增的拐点也固化为可拖动控制点
  if (isOrtho) commitOrthoGeometry(edge, false)
  canvases.saveCurrent()
  closeMenus()
}
function resetEdgeWaypoints(edge: any) {
  if (!hasWaypoints(edge)) return
  pushHistory()
  edge.waypoints = []
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  canvases.saveCurrent()
  closeMenus()
}
/** 折线方向是每条连线各自独立的状态：只写当前 edge 的 ortho_dir，
 *  没有画布级方向字段，也绝不做「全部连线」的统一方向设置。
 *  保存回写由 store 按边 id 逐条补回方向，避免旧版后端吞字段时把其他线一起冲掉。
 *  dir=null 表示「自动（按布局）」，即清掉这条线自己的偏好。
 *  方向只对自动布线有意义：手动拐点会把路径锁死，因此改方向时一并清空控制点。 */
function setEdgeDir(edge: any, dir: OrthoDir | null) {
  // 重复选中同一项且没有待清的控制点 → 不产生无意义的历史
  if (orthoDirOf(edge) === dir && !hasWaypoints(edge)) { closeMenus(); return }
  pushHistory()
  edge.ortho_dir = dir
  const cleared = hasWaypoints(edge)
  if (cleared) edge.waypoints = []
  edgeDrag.value = null
  pendingEdgeSnapshot = null
  hoveringEdgeId.value = null
  // 保存回写由 store 统一按边 id 补回方向（含其他连线），这里不再单独补当前线
  void canvases.saveCurrent().catch(() => {})
  closeMenus()
  const label = dir === null ? '自动（按布局）' : dir === 'cw' ? '顺时针' : '逆时针'
  showToast(`本条连线方向已设为${label}${cleared ? '，控制点已清除' : ''}`)
}
function edgeDirChecked(edge: any, dir: OrthoDir | null): boolean {
  return orthoDirOf(edge) === dir
}
function edgeDirLabel(edge: any): string {
  // 显示这条线自己的偏好；因避让而实际走了另一侧时明确说明，别被误认为设置没生效
  const pref = orthoDirOf(edge)
  const actual = orthoGeomOf(edge)?.orthoDir
  const name = (d: string) => d === 'ccw' ? '逆时针' : '顺时针'
  if (!pref) return `自动（${name(actual || 'cw')}）`
  if (pref && actual && pref !== actual) return `${name(pref)}（遇阻挡已改走${name(actual)}）`
  return name(pref)
}

/** 逐段绘制时的预览色：与将要创建的连线一致（按源节点所属智能体） */
const drawSourceColor = computed(() => {
  if (!draw.value) return 'var(--accent)'
  const n = nodeById.value.get(draw.value.from)
  const aid = n?.agent_id
  if (aid == null) return 'var(--accent)'
  return edgeSourceColors.value.get(aid) ?? 'var(--accent)'
})
/** 已确认的落点（顶点），在预览里画成小圆点 */
const drawVertices = computed<Pt[]>(() => draw.value?.segs.map(s => s.end) || [])
const drawStartPt = computed<Pt>(() => draw.value ? drawStartPoint(draw.value) : { x: 0, y: 0 })
const drawEndPt = computed<Pt>(() => draw.value ? drawPendingEnd(draw.value) : { x: 0, y: 0 })
const drawType = computed<SegType>(() => draw.value?.type || 'straight')
const drawSegNo = computed(() => (draw.value?.segs.length || 0) + 1)
/** 预览路径 = 起点锚点 → 已确认各段（各自线型）→ 当前待确认段（跟随鼠标） */
function drawPath(): string {
  const d = draw.value
  if (!d) return ''
  const start = drawStartPoint(d)
  let path = `M ${start.x.toFixed(1)} ${start.y.toFixed(1)}`
  let cur = start
  let tan = anchorNormal(drawStartSpec(d).side)
  for (const s of d.segs) {
    const normal = s.type === 'vert' ? normalOf(tan) : null
    const built = buildSegment(cur, s.end, s.type, s.ctrl, s.bulge, normal)
    path += ' ' + built.d
    tan = segEndTangent(cur, s.end, s.type, s.ctrl, s.bulge, built)
    cur = s.end
  }
  const end = drawPendingEnd(d)
  const onNode = hoverNodeId.value != null && hoverNodeId.value !== d.from
  const normal = d.type === 'vert' ? normalOf(tan) : null
  // 预览段同样两端贴锚点：首段沿源锚点法线外推；悬停目标时末段沿目标锚点法线外推，
  // 让预览曲线与最终烘焙出的曲线形状一致（从节点外部指向锚点）
  const n0 = d.segs.length === 0 ? anchorNormal(drawStartSpec(d).side) : null
  let n1: Pt | null = null
  if (onNode) {
    const src = nodeById.value.get(d.from), tgt = nodeById.value.get(hoverNodeId.value!)
    if (src && tgt) {
      const srcC = { x: src.x + NODE_W / 2, y: src.y + NODE_H / 2 }
      n1 = anchorNormal(facingMidAnchor(nodeRect(tgt), srcC).side)
    }
  }
  // 终点落在节点上时不能投影成垂足（线就连不到节点了）：保留锚点，由 vertBuilt 补连接短线
  const pending: EdgeSegment = d.type === 'vert' && onNode && normal
    ? { type: 'vert', end, ctrl: [normal], bulge: DEFAULT_ARC_BULGE }
    : makeDrawSegment(cur, end, d.type, d.via, normal, n0, n1)
  return path + ' ' + buildSegment(cur, pending.end, pending.type, pending.ctrl, pending.bulge, normal).d
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
    // 用过即记录：进入工作区历史（输入框工作区下拉数据源）
    if (res.workdir) await settings.recordWorkspace(res.workdir)
    // 会话级记录：用户更改只更新当前激活且绑定本画布的会话
    const cur = convs.currentData()
    if (cur && (cur.canvas_id ?? null) === canvases.current!.id) {
      await convs.update(cur.id, { workdir: res.workdir ?? null })
    }
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
  // 取消待执行的 rAF（缩放提交 / 视图同步），避免卸载后回调访问空 ref
  if (zoomRafId) { cancelAnimationFrame(zoomRafId); zoomRafId = 0 }
  if (viewSyncRaf) { cancelAnimationFrame(viewSyncRaf); viewSyncRaf = 0 }
  // 清理所有气泡上挂的延迟恢复定时器，避免销毁后定时器仍持有 DOM
  resetBubbleStick()
})

// KeepAlive 缓存 Home 页面时，CanvasBoard 不卸载但被 deactivated。
// 此时 Teleport 到 body 的 modal/mask/ctx-menu 会残留在 body 上阻塞后续路由的点击，
// 必须在 deactivated 时主动关闭所有浮层和对话框。
onDeactivated(() => {
  ctxMenu.value = null
  bgMenu.value = null
  edgeMenu.value = null
  triggerDlg.value = false
  newCanvasDialog.value = false
  wdOpen.value = false
  paletteOpen.value = false
  rulesOpen.value = false
  editingAgent.value = null
  renameEdit.value = false
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
  // 切画布：清空选区、撤销/重做历史、悬停/拖动状态与所有菜单
  selectedNodeIds.value = new Set()
  lasso.value = null
  hoveringEdgeId.value = null
  edgeDrag.value = null
  pendingDragSnapshot = null
  pendingEdgeSnapshot = null
  dragWaypoints = []
  undoStack.value = []
  redoStack.value = []
  closeMenus()
  nextTick(() => { fitAll(); bubbleHidden.value = {} })
})
// 画布切换 / 挂载首次 → 立即广播激活（MainView 需尽早拿到 viewportEl / bbox）
watch(() => canvas.value?.id, () => {
  nextTick(emitCanvasActivate)
}, { immediate: true })

// 切换对话 / 打开画布 → 把会话记录的工作区与串并行模式同步回画布（画布当前设置跟随会话记录）。
// 画布存储值一并更新：运行时会话 workdir 为空的场景回退到画布值，保证「显示 == 实际运行」。
watch([() => canvas.value?.id, () => convs.current,
       () => convs.currentData()?.workdir, () => convs.currentData()?.execution_mode], () => {
  const cv = canvas.value
  const cur = convs.currentData() as any
  if (!cv || !cur || (cur.canvas_id ?? null) !== cv.id) return
  if (cur.workdir != null && (cv.workdir || null) !== cur.workdir) {
    void api.canvases.setWorkdir(cv.id, cur.workdir).then((res: any) => {
      if (canvas.value?.id !== cv.id) return   // 期间已切画布：不覆盖
      canvases.current = { ...canvas.value, workdir: res?.workdir ?? cur.workdir }
      const i = canvases.list.findIndex(x => x.id === cv.id)
      if (i >= 0) canvases.list[i] = canvases.current
      nextTick(emitCanvasActivate)
    })
  }
  if (cur.execution_mode && cv.execution_mode !== cur.execution_mode) {
    canvas.value.execution_mode = cur.execution_mode
    void canvases.saveCurrent()
  }
}, { immediate: true })
// 视图变换（平移/缩放/节点数）→ bbox 重算与 activate 广播合并到下一帧执行：
// post-flush watcher 若每次 tick 都同步跑 getBoundingClientRect，会在 zoom 重排之外
// 再触发强制同步布局（wheel/mousemove 频率高于帧率时一帧多次全量布局 → 缩放平移卡顿）；
// rAF 合并后每帧最多一次全量测量，MainView 侧 bbox/scale 更新延迟一帧无感知
let viewSyncRaf = 0
function scheduleViewSync() {
  if (viewSyncRaf) return
  viewSyncRaf = requestAnimationFrame(() => {
    viewSyncRaf = 0
    refreshBboxAndEmit()
    emitCanvasActivate()
  })
}
watch([() => nodes.value?.length, tx, ty, scale], scheduleViewSync, { flush: 'post' })

// 平移层 style：只做 translate（不缩放文字纹理）；缩放由 .viewport 的 CSS zoom 承担
const vTransform = computed(() => {
  return `translate(${tx.value}px, ${ty.value}px)`
})

// ③ 气泡流式自动滚到底：只依赖触发内容变化的响应式字段
//   不再遍历 msgs 算 streamLen（重复计算，bubbleMap 已遍历过一次）
//   speakingAgents.size 让"并行时新增一个接收方"也能触发滚动跟随
const bubbleScrollKey = computed(() => {
  const conv = bubbleConv.value
  if (!conv) return '0'
  const msgs = conv.messages || []
  const tcKey = Object.entries(convs.toolCalls).map(([k, v]) => `${k}:${(v as any).name}:${(v as any).phase}`).join('|')
  return `${convs.streaming ? 1 : 0}:${convs.speakingAgent ?? 0}:${convs.speakingAgents.size}:${convs.handoffActive ? 1 : 0}:${msgs.length}:${tcKey}`
})

// 气泡底部跟随：默认贴底；用户上滑即脱离，停止滑动 5s 后自动恢复跟随
const BUBBLE_BOTTOM_THRESHOLD = 32
const BUBBLE_RESUME_DELAY_MS = 5000

function isBubbleNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= BUBBLE_BOTTOM_THRESHOLD
}

function _clearFollowTimer(el: HTMLElement) {
  const t = (el as any).__followTimer
  if (t) { clearTimeout(t); (el as any).__followTimer = null }
}

function _scheduleResume(el: HTMLElement) {
  _clearFollowTimer(el)
  ;(el as any).__followTimer = setTimeout(() => {
    (el as any).__followTimer = null
    // 5s 内用户没再滚 → 恢复跟随，立刻把最新内容拉到底
    if (!isBubbleNearBottom(el)) {
      el.scrollTop = el.scrollHeight
    }
    ;(el as any).__stickBottom = undefined
  }, BUBBLE_RESUME_DELAY_MS)
}

function onBubbleScroll(e: Event) {
  const el = e.target as HTMLElement
  if (!el || !el.classList.contains('node-bubble')) return
  // 程序性 scrollTop 回写（zoom 缩放补偿）不是用户滚动，跳过贴底状态判定
  if (suppressBubbleScroll) return
  if (isBubbleNearBottom(el)) {
    // 滑回底部 → 取消待执行的恢复定时器，立即恢复跟随
    _clearFollowTimer(el)
    ;(el as any).__stickBottom = undefined
  } else {
    // 用户主动上滑 → 脱离跟随，启动 5s 无滑动后恢复的定时器
    ;(el as any).__stickBottom = false
    _scheduleResume(el)
  }
}

function resetBubbleStick() {
  const root = boardRef.value
  if (!root) return
  root.querySelectorAll<HTMLElement>('.node-bubble').forEach(el => {
    _clearFollowTimer(el)
    ;(el as any).__stickBottom = undefined
  })
}

function scrollBubblesToBottom() {
  const root = boardRef.value
  if (!root) return
  // 只自动贴底「正在流式输出的发言者」那个气泡，已完成的让用户自由滚动
  if (!convs.streaming) return
  root.querySelectorAll<HTMLElement>('.node-bubble.streaming').forEach(el => {
    if ((el as any).__stickBottom === false) return
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

// 气泡流式自动滚到底：监听 bubbleMap（内容每次变化都重新计算，含 token 到达时的 content mutate）
// 以及 bubbleScrollKey（speakingAgent 切换、toolCalls 变化等）
watch([bubbleMap, bubbleScrollKey], async () => {
  await nextTick()
  // 等浏览器完成本帧布局后再滚，确保 scrollHeight 已包含最新文字
  requestAnimationFrame(scrollBubblesToBottom)
}, { flush: 'post' })

// 打开/切换对话记录时，把画布上所有当前显示的气泡一次性滑到底部
watch(() => bubbleConv.value?.id, async (id) => {
  if (id == null) return
  await nextTick()
  resetBubbleStick()
  requestAnimationFrame(scrollAllBubblesToBottom)
}, { immediate: true })

// 流式过程中持续贴底（token 逐段到达时 key 可能不变的兜底）
// 用 rAF 自调度替代 while+setInterval：浏览器每帧最多跑一次，不强制 nextTick 重渲染
watch(() => [convs.streaming, convs.speakingAgent] as const, async ([sp], oldVal) => {
  const prevSp = oldVal?.[0]
  if (sp && !prevSp) resetBubbleStick()
  if (!sp) return
  let rafId = 0
  const tick = () => {
    if (!convs.streaming) return
    scrollBubblesToBottom()
    rafId = requestAnimationFrame(tick)
  }
  rafId = requestAnimationFrame(tick)
  // 下一帧立即滚一次（内容刚到位）
  await nextTick()
  scrollBubblesToBottom()
})

// 暴露 viewportRef 给父组件（钉住模式下 WorkspaceOverlay 要嵌入 viewport 内部）
defineExpose({ viewportRef, computeNodeBbox })
</script>

<template>
  <div class="canvas-board glass" ref="boardRef"
       @dragover.prevent @drop="onDrop" @wheel.passive="onWheel"
       @mousedown="startPan" @mouseleave="pointerCanvas = null"
       @click="onBoardClick" @contextmenu="showBgMenu"
       @dblclick="onBoardDblClick">
    <!-- 点状网格已移除 -->


    <!-- 工具栏（可折叠） -->
    <div class="canvas-toolbar" :class="{ collapsed: toolbarCollapsed }" @click.stop>
      <!-- 折叠态：极简小条 -->
      <template v-if="toolbarCollapsed">
        <div class="toolbar-left collapsed-bar">
          <button class="btn ghost collapse-btn" @click.stop="toolbarCollapsed = false" title="展开工具栏">▾</button>
          <button class="btn ghost palette-btn" ref="paletteAnchor"
                  @click.stop="togglePalette" @mousedown.stop
                  :class="{ active: paletteOpen }"
                  title="选择颜色后点击智能体节点，即可更改该智能体的头像颜色">🎨</button>
          <span class="canvas-mini" :title="'当前画布：' + canvas?.name">{{ canvas?.name }}</span>
        </div>
        <div class="toolbar-right">
          <button class="btn ghost run-btn" :class="{ stop: runningOnThisCanvas }"
                  @click.stop="toggleRun">
            <span v-if="runningOnThisCanvas" class="stop-icon" aria-hidden="true"></span>
            <template v-else>▶</template>
          </button>
          <button class="btn ghost em-collapsed" @click.stop="toggleExecMode"
                  :class="{ pending: !!pendingExecMode }"
                  :title="displayExecMode === 'parallel' ? '并行 → 点击切换串行' : '串行 → 点击切换并行'">
            {{ displayExecMode === 'parallel' ? '⇉' : '⇥' }}
          </button>
          <button class="btn ghost close-btn collapsed-close" @click.stop="canvases.closeCurrent()" title="关闭画布（自动保存）">✕</button>
        </div>
      </template>
      <!-- 展开态：完整按钮组 -->
      <template v-else>
        <div class="toolbar-left">
          <button class="btn ghost collapse-btn" @click.stop="toolbarCollapsed = true" title="折叠工具栏">▸</button>
          <button class="btn ghost palette-btn" ref="paletteAnchor"
                  @click.stop="togglePalette" @mousedown.stop
                  :class="{ active: paletteOpen }"
                  title="选择颜色后点击智能体节点，即可更改该智能体的头像颜色">🎨</button>
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
          <button class="btn ghost run-btn" :class="{ stop: runningOnThisCanvas }"
                  @click.stop="toggleRun"
                  :title="runningOnThisCanvas ? '停止：中止定时触发与进行中的请求' : '运行：按节点右键配置的触发方式（等待/定时）运行集群'">
            <template v-if="runningOnThisCanvas"><span class="stop-icon" aria-hidden="true"></span>停止</template>
            <template v-else>▶ 运行</template>
          </button>
          <div class="exec-mode-toggle" @click.stop="toggleExecMode"
               :class="{ pending: !!pendingExecMode }"
               :title="displayExecMode === 'parallel' ? '并行：发送后继续运行' : '串行：阻塞等待对方回信'">
            <span class="em-label" :class="{ active: displayExecMode !== 'parallel' }">串行</span>
            <label class="em-switch">
              <input type="checkbox" :checked="displayExecMode === 'parallel'" @click.stop />
              <span class="em-slider"></span>
            </label>
            <span class="em-label" :class="{ active: displayExecMode === 'parallel' }">并行</span>
          </div>
          <button class="btn ghost" :class="{ activeWd: !!canvas?.workdir }"
                  @click.stop="wdPath = canvas?.workdir || ''; wdOpen = true"
                  :title="canvas?.workdir ? '📁 工作区已设置：' + canvas.workdir : '📁 设置工作区文件夹（智能体文件生成目标）'">
            📁 {{ canvas?.workdir ? '已设' : '工作区' }}
          </button>
          <button class="btn ghost" @click.stop="rulesOpen = true" title="📋 规章制度">📋 规则</button>
          <span class="zoom-indicator" :class="{ panning: spaceDown }">
            🔍 {{ Math.round(scale * 100) }}%
          </span>
          <button class="btn ghost" @click.stop="confirmRemoveCanvas" title="删除此画布">删除</button>
          <button class="btn ghost close-btn" @click.stop="canvases.closeCurrent()" title="关闭画布（自动保存）">✕</button>
        </div>
      </template>
    </div>

    <!-- 执行模式切换 / 选区 / 剪贴板提示 -->
    <div v-if="toast" class="exec-mode-toast">{{ toast.msg }}</div>

    <!-- 逐段绘制状态条 -->
    <div v-if="draw" class="draw-hint" @mousedown.stop @click.stop>
      <span class="draw-hint-badge">第 {{ drawSegNo }} 段</span>
      <span>当前线型：<b>{{ segLabel(drawType) }}</b></span>
      <span class="draw-hint-tip">左键落点确认一段 · 右键切换线型 · 点击节点结束 · Esc 取消</span>
      <button class="btn xs ghost" @click.stop="cancelDraw()">✕ 取消</button>
    </div>

    <!-- 色谱浮窗（调色板按钮）：Teleport 到 body，避免被画布缩放层裁剪 -->
    <Teleport to="body">
      <ColorSpectrumPopover
        v-if="paletteOpen"
        :pos="palettePos"
        :initial="lastPickedColor"
        @change="onColorChanged"
        @close="onPaletteClosed"
        @update:pos="onPaletteMoved" />
    </Teleport>

    <!-- 平移层（transform 纯平移） + 缩放层（CSS zoom 布局级缩放，文字始终矢量清晰） -->
    <div class="pan-layer" :style="{ transform: vTransform }">
      <div class="viewport" ref="viewportRef" :style="{ zoom: scale }" @contextmenu="showBgMenu">
        <svg class="edge-layer">
          <g v-for="item in edgeGeoms" :key="item.e.id"
             :style="{ '--eg-c': item.color }"
             @mouseenter="hoveringEdgeId = item.e.id"
             @mouseleave="hoveringEdgeId = null">
            <!-- 每条连线一个箭头，颜色跟随所属源智能体（与连线同色） -->
            <defs>
              <!-- userSpaceOnUse：箭头尺寸直接按画布坐标给，配合 arrowScale 抵消画布缩放；
                   细长三角形（16.4×8.4），refX 落在尖端，整个箭头都在端点之前，不会被目标节点盖住 -->
              <marker v-if="item.g" :id="`egm-${item.e.id}`" viewBox="0 0 20 20"
                      :markerWidth="20 * arrowScale" :markerHeight="20 * arrowScale"
                      refX="18.4" refY="10" orient="auto" markerUnits="userSpaceOnUse">
                <path d="M 2,5.8 L 18.4,10 L 2,14.2 Z"
                      :class="['eg-arrow', { dim: isEdgeDim(item.e) }]" />
              </marker>
            </defs>
            <path v-if="item.g" :d="item.g.d"
                  :class="['edge-hit', { active: isEdgeActive(item.e), dim: isEdgeDim(item.e) }]"
                  @dblclick="removeEdge(item.e.id)"
                  @contextmenu="showEdgeMenu($event, item.e)" />
            <path v-if="item.g" :d="item.g.d"
                  :class="['edge', { active: isEdgeActive(item.e), dim: isEdgeDim(item.e), ortho: item.ortho }]"
                  :marker-end="`url(#egm-${item.e.id})`" pointer-events="none" />
            <path v-if="item.g" class="edge-mid"
                  :class="{ dim: isEdgeDim(item.e) }"
                  :transform="`translate(${item.g.midX},${item.g.midY}) rotate(${item.g.midAngle}) scale(${arrowScale})`"
                  d="M -9,-4 L 9,0 L -9,4 Z" />
          </g>
          <!-- 逐段绘制预览：已确认段各自保留线型，最后一段跟随鼠标。
               控制点 / 锚点不放在这一层：连线层在节点之下，边缘锚点会被节点盖住抓不到，
               统一放到节点上方的 .handle-layer（见下方）。 -->
          <g v-if="draw" :style="{ '--eg-c': drawSourceColor }">
            <path :d="drawPath()" class="edge temp" />
          </g>
          <!-- 边缘对齐辅助线 -->
          <g v-if="dragging && (snapGuides.x.length || snapGuides.y.length)" class="snap-guides" pointer-events="none">
            <line v-for="gx in snapGuides.x" :key="'gx-' + gx"
                  :x1="gx" :y1="-9999" :x2="gx" :y2="9999"
                  stroke="var(--accent)" stroke-width="1" stroke-dasharray="4 4" opacity="0.85" />
            <line v-for="gy in snapGuides.y" :key="'gy-' + gy"
                  :x1="-9999" :y1="gy" :x2="9999" :y2="gy"
                  stroke="var(--accent)" stroke-width="1" stroke-dasharray="4 4" opacity="0.85" />
          </g>
        </svg>

        <div v-for="n in nodes" :key="n.id" class="node glass-soft"
             :data-node-id="n.id"
             :class="{
               disabled: agentById(n.agent_id)?.enabled === false,
               speaking: nodeState(n.agent_id) === 'speaking',
               calling: nodeState(n.agent_id) === 'calling',
               waiting: nodeState(n.agent_id) === 'waiting',
               selected: selectedNodeIds.has(n.id),
             }"
             :style="{ left: n.x + 'px', top: n.y + 'px' }"
             @mousedown="startDrag($event, n)"
             @click="onNodeClickInDraw($event, n.id)"
             @mouseenter="hoverNodeId = n.id"
             @mouseleave="hoverNodeId = null"
             @dblclick="onNodeDblClick($event, n)"
             @contextmenu="showCtxMenu($event, n)">
          <!-- 消息气泡：双击气泡可隐藏；流式输出中也尊重用户的隐藏设置 -->
          <div v-if="getBubbleForAgent(n.agent_id) && !bubbleHidden[n.id]"
               class="node-bubble"
               :class="{
                 streaming: getBubbleForAgent(n.agent_id)?.streaming,
                 below: bubbleDir(n.id) === 'bottom',
               }"
               :style="{ zIndex: getBubbleForAgent(n.agent_id)?.streaming ? 9 : 6 }"
               @dblclick.stop="toggleBubble(n)"
               @wheel.stop @scroll="onBubbleScroll">
            <!-- 深度思考 chunk（灰底折叠块，有区别特效） -->
            <div v-for="(chunk, ci) in getBubbleForAgent(n.agent_id)!.chunks" :key="ci"
                 class="b-chunk" :class="'b-' + chunk.kind">
              <template v-if="chunk.kind === 'reasoning'">
                <div class="b-think">
                  <span class="b-think-icon">🧠</span>
                  <span class="b-think-text">{{ cyclicWindow(chunk.text || '', 50) }}</span>
                  <span class="b-think-cursor">▌</span>
                </div>
              </template>
              <template v-else-if="chunk.kind === 'call'">
                <span class="b-call">{{ chunk.text }}</span>
              </template>
              <template v-else-if="chunk.kind === 'tool'">
                <div class="b-tool-row" :class="{ running: chunk.toolRunning }">
                  <span class="b-tool-icon" :class="{ spinning: chunk.toolRunning }">{{ chunk.toolRunning ? '⟳' : '✓' }}</span>
                  <span class="b-tool-label">{{ chunk.toolName }}<template v-if="chunk.toolFileName"> <span class="b-tool-fn">（{{ chunk.toolFileName }}）</span></template></span>
                </div>
              </template>
              <template v-else>
                <span class="b-text markdown" v-html="renderMarkdown(chunk.text)"></span>
              </template>
            </div>
          </div>
          <div class="node-head">
            <div class="avatar" :style="{ background: nodeAvatarBg(n) }"
                 title="双击显示/隐藏消息泡泡"
                 @dblclick.stop="toggleBubble(n)">{{ (agentById(n.agent_id)?.name || '?').slice(0,1).toUpperCase() }}</div>
            <div class="name">{{ agentById(n.agent_id)?.name || '未知' }}</div>
            <span v-if="agentById(n.agent_id)?.enabled === false" class="off-tag" title="已停用">⏸</span>
          </div>
          <div class="node-actions">
            <button class="btn xs ghost ctx-action" @click.stop="doChat(n)">💬 对话</button>
            <button class="btn xs ghost ctx-action" @mousedown.stop @click.stop="startLink($event, n.id)">🔗 连线</button>
          </div>
        </div>

        <!-- 控制柄层：必须浮在节点之上。锚点正好落在节点边缘，若与连线同层会被节点
             的背景 / 命中区整个盖住，鼠标一移到节点上连线又失去 hover，锚点根本抓不到。 -->
        <svg class="handle-layer">
          <g v-for="item in edgeGeoms" :key="'h-' + item.e.id"
             :style="{ '--eg-c': item.color }">
            <g v-for="h in visibleHandles(item.e, item.g)" class="edge-handle-group"
               :key="h.kind + '-' + (h.end || '') + '-' + (h.seg ?? '') + '-' + (h.ctrl ?? '') + '-' + h.index">
              <circle class="edge-handle-hit" :cx="h.x" :cy="h.y" :r="10 / scale"
                      @mousedown.stop.prevent="startEdgeHandleDrag($event, item.e, h)"
                      @dblclick.stop="onEdgeHandleDblClick(item.e, h)"
                      @contextmenu.stop.prevent="showEdgeMenu($event, item.e)">
                <title>{{ handleTip(h, item.e) }}</title>
              </circle>
              <circle class="edge-handle" :class="[h.kind, { peeking: !showEdgeHandles(item.e) }]"
                      :cx="h.x" :cy="h.y" :r="(h.kind === 'point' ? 4.5 : h.kind === 'anchor' ? 5.5 : 5) / scale" />
            </g>
          </g>
          <!-- 绘制中的起点锚点 / 已确认顶点 / 当前落点也浮在节点之上 -->
          <g v-if="draw" pointer-events="none">
            <circle :cx="drawStartPt.x" :cy="drawStartPt.y" :r="5 / scale" class="draw-anchor" />
            <circle v-for="(p, i) in drawVertices" :key="'dv-' + i" :cx="p.x" :cy="p.y" :r="4 / scale" class="draw-vertex" />
            <circle :cx="drawEndPt.x" :cy="drawEndPt.y" :r="3.5 / scale" class="draw-cursor" />
          </g>
        </svg>

        <!-- 套索选框（画布坐标，随 viewport 缩放） -->
        <div v-if="lassoRect" class="lasso-rect" :class="[lassoRect.class, { additive: lassoRect.additive }]"
             :style="{ left: lassoRect.left, top: lassoRect.top, width: lassoRect.width, height: lassoRect.height }" />
      </div>
    </div>

    <!-- 选区提示条 -->
    <div v-if="selectedNodeIds.size" class="selection-pill" @click.stop>
      已选 {{ selectedNodeIds.size }} 个智能体 · Ctrl 拖动整体移动 · Delete 删除 · 右键复制 / 剪切 / 创建画布
    </div>

    <!-- 画布空白区域右键菜单 -->
    <Teleport to="body">
      <div v-if="bgMenu" class="ctx-menu glass" :style="{ left: bgMenu.x + 'px', top: bgMenu.y + 'px' }" @click.stop>
        <template v-if="selectedNodeIds.size">
          <div class="ctx-title">已选 {{ selectedNodeIds.size }} 个智能体</div>
          <div class="ctx-item" @click="copySelection(); closeMenus()">📋 复制 <span class="ctx-kbd">Ctrl+C</span></div>
          <div class="ctx-item" @click="cutSelection(); closeMenus()">✂️ 剪切 <span class="ctx-kbd">Ctrl+X</span></div>
          <div class="ctx-item" :class="{ disabled: !canvases.clipboard }"
               @click="canvases.clipboard && pasteClipboard(clientToCanvas(bgMenu.x, bgMenu.y))">📥 粘贴 <span class="ctx-kbd">Ctrl+V</span></div>
          <div class="ctx-item sel-create-canvas" @click="createCanvasFromSelection()">🎨 用选中的智能体创建画布…</div>
          <div class="ctx-divider" />
        </template>
        <div class="ctx-item" :class="{ disabled: !undoStack.length }" @click="undoCanvas()">↶ 撤销 <span class="ctx-kbd">Ctrl+Z</span></div>
        <div class="ctx-item" :class="{ disabled: !redoStack.length }" @click="redoCanvas()">↷ 重做 <span class="ctx-kbd">Ctrl+Y</span></div>
        <div class="ctx-divider" />
        <div class="ctx-title">画布</div>
        <div class="ctx-item" @click="closeMenus(); resetView()">⟳ 重置视图</div>
        <div class="ctx-item submenu" @click.stop>
          <div class="submenu-label">〰 连线形状（全部连线）</div>
          <div class="submenu-body">
            <div class="sub-item edge-all-curve" :class="{ checked: defaultEdgeShape() === 'curve' }"
                 @click="setAllEdgeShapes('curve')">曲线（平滑）</div>
            <div class="sub-item edge-all-straight" :class="{ checked: defaultEdgeShape() === 'straight' }"
                 @click="setAllEdgeShapes('straight')">直线（斜线直连）</div>
            <div class="sub-item edge-all-ortho" :class="{ checked: defaultEdgeShape() === 'ortho' }"
                 @click="setAllEdgeShapes('ortho')">⌐ 直角折线</div>
          </div>
        </div>
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
        <div class="ctx-title">
          {{ selectedNodeIds.size > 1 ? `已选 ${selectedNodeIds.size} 个智能体` : (agentById(ctxMenu.node.agent_id)?.name || '未知') }}
        </div>
        <div class="ctx-item" @click="copySelection(); closeMenus()">📋 复制 <span class="ctx-kbd">Ctrl+C</span></div>
        <div class="ctx-item" @click="cutSelection(); closeMenus()">✂️ 剪切 <span class="ctx-kbd">Ctrl+X</span></div>
        <div class="ctx-item" :class="{ disabled: !canvases.clipboard }"
             @click="canvases.clipboard && pasteClipboard(clientToCanvas(ctxMenu.x, ctxMenu.y))">📥 粘贴 <span class="ctx-kbd">Ctrl+V</span></div>
        <div class="ctx-item sel-create-canvas" @click="createCanvasFromSelection()">🎨 用选中的智能体创建画布…</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="openAgentEdit(ctxMenu.node)">✏️ 编辑智能体…</div>
        <div class="ctx-item" @click="toggleBubble(ctxMenu.node); closeMenus()">💬 显示/隐藏消息泡泡</div>
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
        <div class="ctx-item danger" @click="removeCtxSelection()">
          🗑 {{ selectedNodeIds.size > 1 ? `从画布移除选中的 ${selectedNodeIds.size} 个` : '从此画布移除' }}
        </div>
      </div>
    </Teleport>

    <!-- 连线右键菜单 -->
    <Teleport to="body">
      <div v-if="edgeMenu" class="ctx-menu glass" :style="{ left: edgeMenu.x + 'px', top: edgeMenu.y + 'px' }" @click.stop>
        <div class="ctx-title">{{ edgeShapeOf(edgeMenu.edge) === 'mixed' ? `分段连线（${readSegments(edgeMenu.edge).length} 段）` : '连线' }}</div>
        <div class="ctx-item submenu" @click.stop>
          <div class="submenu-label">〰 连线形状{{ edgeShapeOf(edgeMenu.edge) === 'mixed' ? '（切换将清除分段）' : '' }}</div>
          <div class="submenu-body">
            <div class="sub-item edge-shape-curve" :class="{ checked: edgeShapeOf(edgeMenu.edge) === 'curve' }"
                 @click="setEdgeShape(edgeMenu.edge, 'curve')">曲线（平滑）</div>
            <div class="sub-item edge-shape-straight" :class="{ checked: edgeShapeOf(edgeMenu.edge) === 'straight' }"
                 @click="setEdgeShape(edgeMenu.edge, 'straight')">直线（斜线直连）</div>
            <div class="sub-item edge-shape-ortho" :class="{ checked: edgeShapeOf(edgeMenu.edge) === 'ortho' }"
                 @click="setEdgeShape(edgeMenu.edge, 'ortho')">⌐ 直角折线</div>
          </div>
        </div>
        <div v-if="edgeShapeOf(edgeMenu.edge) === 'ortho'" class="ctx-item submenu" @click.stop>
          <div class="submenu-label">⟳ 折线方向（仅本条：{{ edgeDirLabel(edgeMenu.edge) }}）</div>
          <div class="submenu-body">
            <div class="sub-item edge-dir-cw" :class="{ checked: edgeDirChecked(edgeMenu.edge, 'cw') }"
                 @click="setEdgeDir(edgeMenu.edge, 'cw')">↻ 顺时针</div>
            <div class="sub-item edge-dir-ccw" :class="{ checked: edgeDirChecked(edgeMenu.edge, 'ccw') }"
                 @click="setEdgeDir(edgeMenu.edge, 'ccw')">↺ 逆时针</div>
            <div class="sub-item edge-dir-auto" :class="{ checked: edgeDirChecked(edgeMenu.edge, null) }"
                 @click="setEdgeDir(edgeMenu.edge, null)">◎ 自动（按布局）</div>
          </div>
        </div>
        <div class="ctx-divider" />
        <div class="ctx-item edge-add-point" @click="addEdgeWaypoint(edgeMenu.edge)">
          {{ edgeShapeOf(edgeMenu.edge) === 'mixed' ? '➕ 在中点拆出新的一段' : '➕ 添加控制点' }}
        </div>
        <div v-if="edgeShapeOf(edgeMenu.edge) === 'mixed'" class="ctx-item edge-reset" @click="clearEdgeSegments(edgeMenu.edge)">↺ 清除分段路径</div>
        <div v-else-if="hasWaypoints(edgeMenu.edge)" class="ctx-item edge-reset" @click="resetEdgeWaypoints(edgeMenu.edge)">↺ 恢复自动布线</div>
        <div v-if="hasManualAnchors(edgeMenu.edge)" class="ctx-item edge-reset-anchors" @click="resetEdgeAnchors(edgeMenu.edge)">⌖ 恢复锚点默认位置</div>
        <div class="ctx-divider" />
        <div class="ctx-item danger edge-delete" @click="removeEdge(edgeMenu.edge.id); closeMenus()">🗑 删除连线</div>
      </div>
    </Teleport>

    <!-- 绘制中的「本段线型」右键菜单 -->
    <Teleport to="body">
      <div v-if="segMenu && draw" class="ctx-menu glass seg-menu"
           :style="{ left: segMenu.x + 'px', top: segMenu.y + 'px' }"
           @click.stop @contextmenu.prevent.stop>
        <div class="ctx-title">第 {{ drawSegNo }} 段 · 选择线型</div>
        <div class="ctx-item" :class="{ checked: drawType === 'curve' }" @click="chooseSegType('curve')">〰 曲线（平滑）</div>
        <div class="ctx-item" :class="{ checked: drawType === 'straight' }" @click="chooseSegType('straight')">╱ 直线</div>
        <div class="ctx-item" :class="{ checked: drawType === 'arc' }" @click="chooseSegType('arc')">◠ 圆弧</div>
        <div class="ctx-item" :class="{ checked: drawType === 'vert' }" @click="chooseSegType('vert')">⊥ 垂线（垂直于上一段）</div>
        <div class="ctx-divider" />
        <div class="ctx-item" @click="cancelDraw()">✕ 取消绘制 <span class="ctx-kbd">Esc</span></div>
        <div class="seg-menu-note">右键位置决定曲线 / 圆弧的弯向；垂线从上一段末尾垂直引出（第一段垂直于起点所在边），左键落点决定长度与朝向。左键确认本段后可继续画下一段</div>
      </div>
    </Teleport>

    <!-- AgentEditDialog（画布节点 → 编辑智能体） -->
    <AgentEditDialog v-if="editingAgent" :agent="editingAgent" @close="editingAgent = null" />

    <!-- 触发设置对话框 -->
    <Teleport to="body">
      <div v-if="triggerDlg" class="modal-bg">
        <div class="modal glass">
          <div class="modal-title">⏱ 触发设置 · {{ agentById(triggerAgentId!)?.name || '' }}</div>
          <div class="trigger-types">
            <label class="tr-opt"><input type="radio" value="manual" v-model="triggerType" /> 手动（运行后等待触发）</label>
            <label class="tr-opt"><input type="radio" value="cron" v-model="triggerType" /> 定时（cron）</label>
          </div>
          <label v-if="triggerType === 'cron'" class="lbl">
            cron 表达式（分 时 日 月 周）
            <input class="input" v-model="triggerCron" placeholder="如：*/5 * * * *（每 5 分钟）" />
          </label>
          <label v-if="triggerType === 'cron'" class="lbl">
            触发消息（留空则默认）
            <input class="input" v-model="triggerMsg" placeholder="【定时触发】请执行任务" />
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
          <div class="modal-title">{{ pendingCanvasPayload ? '用选中的智能体创建画布' : '新建画布' }}</div>
          <label class="lbl">画布名称<input class="input" v-model="newCanvasName" placeholder="如：产品研发、营销策划" ref="r => setTimeout(() => (r as HTMLInputElement)?.focus(), 50)" /></label>
          <div v-if="pendingCanvasPayload" class="new-cv-preview">
            将包含 {{ pendingCanvasPayload.nodes.length }} 个智能体、{{ pendingCanvasPayload.edges.length }} 条内部连线
          </div>
          <div class="actions">
            <button class="btn ghost" @click="newCanvasDialog = false; pendingCanvasPayload = null">取消</button>
            <button class="btn" @click="commitNewCanvas">创建</button>
          </div>
        </div>
      </div>
    </Teleport>

    <div v-if="!nodes.length" class="hint">从左侧拖拽智能体到此处开始编排集群<br/>
      <span class="hint-shortcuts">滚轮缩放 · 左键拖动画布 · Shift+拖拽框选（左→右包含 / 右→左触及）· Shift+Ctrl+拖拽连续框选<br/>
        Ctrl+拖动整体移动 · Delete/Backspace 删除选中 · Ctrl+Z 撤销 / Ctrl+Y 重做<br/>
        双击节点编辑 · 双击节点头像显示/隐藏消息泡泡 · 悬停连线拖动圆点改形状 · 右键菜单 · 双击连线删除</span>
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
  backdrop-filter: none; -webkit-backdrop-filter: none; background: transparent; }
.canvas-board.panning-active { cursor: grabbing; }
.grid-bg { position: absolute; inset: 0; background-image: radial-gradient(circle, rgba(var(--surface-rgb), 0.06) 1px, transparent 1px); background-size: 24px 24px; pointer-events: none; z-index: 0; }

/* 平移层：transform 只做纯平移，不产生纹理缩放，文字永不发糊 */
.pan-layer { position: absolute; inset: 0; z-index: 1; }
/* 缩放层：CSS zoom 为布局级缩放，浏览器按当前缩放的真实字号排版并矢量光栅化，
   任何缩放比例下文字都保持原生锐利，无需 will-change / 重光栅化过渡 */
.viewport { position: absolute; inset: 0; }

.canvas-toolbar { position: absolute; top: 10px; left: 10px; right: 10px; display: flex; justify-content: space-between; gap: 10px; z-index: 10; pointer-events: none;
  transition: max-height .3s ease, opacity .25s ease; overflow: hidden; }
.canvas-toolbar > * { pointer-events: auto; }

/* 左右两段各自独立的玻璃胶囊 */
.toolbar-left, .collapsed-bar, .toolbar-right {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 12px;
  background: rgba(var(--surface-rgb), 0.06);
  backdrop-filter: blur(14px) saturate(150%);
  -webkit-backdrop-filter: blur(14px) saturate(150%);
  border: 1px solid rgba(var(--surface-rgb), 0.10);
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(var(--shadow-rgb), 0.22);
  gap: 8px;
}
.toolbar-right { gap: 6px; }

/* 折叠态：只保留小胶囊，容器不再撑满 */
.canvas-toolbar.collapsed { justify-content: flex-start; }
.canvas-toolbar.collapsed .toolbar-right { margin-left: auto; }
.canvas-mini { font-size: calc(12px * var(--font-scale)); font-weight: 500; opacity: .8; padding: 4px 8px; border-radius: 6px; }
.canvas-mini:hover { background: rgba(var(--surface-rgb), 0.06); }
.collapse-btn { width: 26px; height: 26px; padding: 0; font-size: calc(13px * var(--font-scale)); flex-shrink: 0; }
/* 调色板按钮：选中颜色待应用时高亮，提示用户下一步去点节点 */
.palette-btn { width: 26px; height: 26px; padding: 0; font-size: calc(13px * var(--font-scale)); flex-shrink: 0; }
.palette-btn.active { background: rgba(var(--accent-rgb), 0.22); border-color: rgba(var(--accent-rgb), 0.5); }
.canvas-name { font-size: calc(14px * var(--font-scale)); font-weight: 600; cursor: text; padding: 4px 8px; border-radius: 6px; transition: background .15s; }
.canvas-name:hover { background: rgba(var(--surface-rgb), 0.06); }
.canvas-meta { font-size: calc(11px * var(--font-scale)); opacity: .72; font-family: Consolas, monospace; }
.rename-input { padding: 4px 10px; width: 180px; }

.zoom-indicator { font-size: calc(11px * var(--font-scale)); font-family: Consolas, monospace; opacity: .78; padding: 0 6px; }
.close-btn:hover { background: rgba(232, 92, 92, 0.14) !important; color: #e85c5c; }
.zoom-indicator.panning .zoom-tip { color: var(--accent-2); opacity: 1; }

.edge-layer { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
/* 控制柄单独一层、盖在节点上方（z-index 7 > 节点默认层），否则节点边缘的锚点抓不到。
   层本身不吃事件，只有 .edge-handle-hit 接收，节点其余区域照常可点可拖。 */
.handle-layer { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; z-index: 7; }
/* —— 连线配色：纯色，颜色只由「源智能体」决定 ——
   同一智能体引出的所有连线同色；不同智能体引出的线不同色。
   --eg-c 由模板按源智能体注入，与主题强调色混合后仍跟随配色方案 */
/* 端点箭头：细长三角形，和连线同色，但加一圈画布底色描边（paint-order 让描边在外、
   填充在内），把箭头从节点边缘和背景里"抠"出来；尺寸由模板的 arrowScale 反向补偿画布缩放 */
.eg-arrow {
  fill: color-mix(in oklab, var(--accent) var(--edge-accent-share, 45%), var(--eg-c, var(--accent)));
  stroke: rgb(var(--edge-halo-rgb, 10, 10, 10));
  stroke-width: 2.4;
  stroke-linejoin: round;
  paint-order: stroke fill;
}
.eg-arrow.dim { opacity: 0.6; }
.edge {
  fill: none;
  stroke: color-mix(in oklab, var(--accent) var(--edge-accent-share, 45%), var(--eg-c, var(--accent)));
  stroke-width: 2; pointer-events: stroke; cursor: pointer; stroke-dasharray: 8 6;
  transition: stroke .3s, stroke-width .3s, filter .3s;
}
.edge.temp { stroke: color-mix(in oklab, var(--accent) var(--edge-accent-share, 45%), var(--eg-c, var(--accent))); stroke-dasharray: 3,3; animation: none; }
/* 逐段绘制预览：起点锚点 / 已确认顶点 / 当前落点 */
.draw-anchor { fill: var(--accent); stroke: rgba(var(--surface-rgb), 0.95); stroke-width: 2; pointer-events: none; }
.draw-vertex { fill: rgba(var(--node-bg-rgb), 0.95); stroke: var(--accent-2); stroke-width: 2; pointer-events: none; }
.draw-cursor { fill: var(--accent-2); stroke: rgba(var(--surface-rgb), 0.9); stroke-width: 1.5; pointer-events: none; }
.draw-hint { position: absolute; left: 50%; top: 54px; transform: translateX(-50%); z-index: 60;
  display: flex; align-items: center; gap: 10px; padding: 7px 10px 7px 8px; border-radius: 10px;
  background: rgba(var(--node-bg-rgb), 0.96); border: 1px solid rgba(var(--accent-rgb), 0.35);
  box-shadow: 0 8px 26px rgba(var(--shadow-rgb), 0.4); font-size: calc(12px * var(--font-scale));
  color: var(--text); white-space: nowrap; }
.draw-hint-badge { padding: 2px 8px; border-radius: 6px; background: rgba(var(--accent-rgb), 0.18); color: var(--accent-light); }
.draw-hint-tip { color: rgba(var(--text-rgb), 0.6); }
.draw-hint .btn { padding: 2px 8px; }
/* 直角折线：拐角描边接缝做圆润处理（几何仍是 90° 直角），避免虚线断点处出现毛刺尖角 */
.edge.ortho { stroke-linejoin: round; stroke-linecap: round; }
/* 调用进行中：加粗 + 强调色外发光 + 流动虚线 */
.edge.active { stroke-width: 2.8; filter: drop-shadow(0 0 7px rgba(var(--accent-rgb), 0.9)) saturate(1.35) brightness(1.12); animation: edgeFlow 0.6s linear infinite; }
/* 端点智能体已停用：整体去色并压暗 */
.edge.dim { stroke-width: 1.5; stroke-dasharray: 4 4; cursor: default; animation: none; filter: grayscale(0.85); opacity: 0.55; }
.edge.active.dim { filter: grayscale(0.85) drop-shadow(0 0 4px rgba(var(--accent-rgb), 0.25)); opacity: 0.7; }
.edge-hit { fill: none; stroke: transparent; stroke-width: 14; pointer-events: stroke; cursor: pointer; }
.edge-hit.dim { cursor: default; }
.edge-hit.active { cursor: pointer; }
/* 中点箭头：方向指示。18×8 的细长三角形，并加一圈底色描边（paint-order 让描边在外、
   填充在内），把箭头从虚线中"抠"出来，轮廓更锐利又不显笨重。
   transform 里的 scale(arrowScale) 抵消画布缩放：画布缩小时箭头在屏幕上不会跟着变小。 */
.edge-mid {
  fill: color-mix(in oklab, var(--accent) var(--edge-accent-share, 45%), var(--eg-c, var(--accent)));
  stroke: rgb(var(--edge-halo-rgb, 10, 10, 10));
  stroke-width: 3.6;
  stroke-linejoin: round;
  paint-order: stroke fill;
  pointer-events: none;
}
.edge-mid.dim { opacity: 0.45; }
/* 连线控制点：命中圆透明且更大，可见圆只负责展示 */
.handle-layer .edge-handle-group { pointer-events: none; }
/* 靠近才出现的锚点：出现时就清晰可见，鼠标移到柄上再完全实色 */
.edge-handle.peeking { opacity: .9; }
.edge-handle-group:hover .edge-handle { opacity: 1; }
.edge-handle-hit { fill: transparent; stroke: none; pointer-events: all; cursor: grab; }
.edge-handle-hit:active { cursor: grabbing; }
.edge-handle { fill: var(--accent-2); stroke: rgba(var(--surface-rgb), 0.95); stroke-width: 1.5; pointer-events: none; filter: drop-shadow(0 1px 3px rgba(var(--shadow-rgb), 0.5)); }
.edge-handle.insert { fill: rgba(var(--node-bg-rgb), 0.85); stroke: var(--accent-2); stroke-dasharray: 2.5 2.5; }
/* 锚点手柄用另一种实色，和拐点 / 插入柄区分开 */
.edge-handle.anchor { fill: var(--accent); stroke: rgba(var(--surface-rgb), 0.95); stroke-width: 2; }
/* 分段连线：顶点=方色实心、曲线控制点=空心环、圆弧矢高=菱形感的小点 */
.edge-handle.vertex { fill: var(--accent); stroke: rgba(var(--surface-rgb), 0.95); stroke-width: 2; }
.edge-handle.ctrl { fill: rgba(var(--node-bg-rgb), 0.9); stroke: var(--accent-2); stroke-width: 2; stroke-dasharray: 2 2; }
.edge-handle.bulge { fill: var(--accent-2); stroke: rgba(var(--surface-rgb), 0.95); stroke-width: 2; }
@keyframes edgeFlow { to { stroke-dashoffset: -28; } }
.snap-guides line { stroke: var(--accent-2); }

.node { position: absolute; width: 180px; padding: 10px; cursor: grab; user-select: none; transition: box-shadow .2s, opacity .2s, filter .2s, border-color .25s;
  border: 1px solid rgba(var(--text-rgb), 0.14); border-radius: 12px;
  backdrop-filter: none; -webkit-backdrop-filter: none; background: rgba(var(--node-bg-rgb), 0.88); }
.node:hover { box-shadow: 0 8px 28px rgba(var(--accent-rgb), 0.25); }
.node:active { cursor: grabbing; }
.node.disabled { opacity: 0.45; filter: grayscale(0.85); }
/* 套索选中：outline 不占用 box-shadow，可与 speaking/calling 光效并存 */
.node.selected { outline: 2px solid var(--accent-2); outline-offset: 2px; }
.off-tag { margin-left: auto; font-size: calc(11px * var(--font-scale)); opacity: .8; }

/* ====== 节点活跃状态特效 ====== */

/* 最强：正在输出 token — 紫色外发光 + 边框 + 头像呼吸 */
.node.speaking {
  border-color: rgba(var(--accent2-rgb), 0.8);
  box-shadow: 0 0 0 2px rgba(var(--accent2-rgb), 0.35),
              0 0 24px 6px rgba(var(--accent2-rgb), 0.35),
              0 8px 28px rgba(var(--accent2-rgb), 0.3);
  animation: nodePulse 1.6s ease-in-out infinite;
}
.node.speaking .avatar {
  animation: avatarGlow 1.2s ease-in-out infinite;
  box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45),
              inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32),
              0 0 12px 3px rgba(var(--accent2-rgb), 0.55);
}

/* 次要：正在发起/处理 call — 蓝色边框脉冲 */
.node.calling {
  border-color: rgba(var(--accent-rgb), 0.75);
  box-shadow: 0 0 0 2px rgba(var(--accent-rgb), 0.3),
              0 0 16px 4px rgba(var(--accent-rgb), 0.25);
  animation: nodePulse 2.2s ease-in-out infinite;
}

/* 最轻：集群在跑但自己闲置 — 左下角小蓝点 */
.node.waiting::after {
  content: ''; position: absolute; bottom: 6px; left: 8px;
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  animation: dotBlink 1.4s ease-in-out infinite;
  box-shadow: 0 0 6px 1px rgba(var(--accent-rgb), 0.6);
}
.node.disabled.waiting::after { display: none; }

@keyframes nodePulse {
  0%, 100% { filter: brightness(1); }
  50%      { filter: brightness(1.15); }
}
@keyframes avatarGlow {
  0%, 100% { transform: scale(1);    box-shadow: 0 0 10px 2px rgba(var(--accent2-rgb), 0.45); }
  50%      { transform: scale(1.08); box-shadow: 0 0 18px 5px rgba(var(--accent2-rgb), 0.7); }
}
@keyframes dotBlink {
  0%, 100% { opacity: 0.35; transform: scale(0.8); }
  50%      { opacity: 1;    transform: scale(1.15); }
}

.node-bubble { position: absolute; bottom: calc(100% + 12px); left: 50%; transform: translateX(-50%); width: 230px; padding: 8px 10px 16px; font-size: calc(11.5px * var(--font-scale)); line-height: 1.5; color: var(--text); white-space: pre-wrap; word-break: break-word; background: rgba(var(--node-bg-rgb), 0.94); border: 1px solid rgba(var(--accent-rgb), 0.4); border-radius: 10px; box-shadow: 0 8px 24px rgba(var(--shadow-rgb), 0.45); max-height: 140px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; overflow-anchor: none; pointer-events: auto; z-index: 6; cursor: default; scrollbar-width: thin; scrollbar-color: rgba(var(--accent-rgb), 0.5) transparent; animation: bubbleIn 0.25s ease; }
.node-bubble::-webkit-scrollbar { width: 6px; }
.node-bubble::-webkit-scrollbar-track { background: transparent; }
.node-bubble::-webkit-scrollbar-thumb { background: rgba(var(--accent-rgb), 0.45); border-radius: 3px; }
.node-bubble::-webkit-scrollbar-thumb:hover { background: rgba(var(--accent-rgb), 0.7); }
.node-bubble::after { content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 6px solid transparent; border-top-color: rgba(var(--accent-rgb), 0.4); }
.node-bubble.streaming { border-color: rgba(var(--accent2-rgb), 0.65); }
.node-bubble.streaming::after { border-top-color: rgba(var(--accent2-rgb), 0.65); }
/* 气泡避让到节点下方时：翻转定位与箭头方向 */
.node-bubble.below { bottom: auto; top: calc(100% + 12px); }
.node-bubble.below::after { top: auto; bottom: 100%; border-top-color: transparent; border-bottom-color: rgba(var(--accent-rgb), 0.4); }
.node-bubble.below.streaming::after { border-bottom-color: rgba(var(--accent2-rgb), 0.65); }
@keyframes bubbleIn { from { opacity: 0; transform: translateX(-50%) translateY(6px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }

/* —— 气泡 chunk 样式 —— */
.b-chunk + .b-chunk { margin-top: 6px; }
.b-text { display: block; white-space: pre-wrap; }
.b-call { display: block; font-size: calc(11px * var(--font-scale)); color: var(--accent2-light); line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
/* 深度思考流水 — 单行流式打字机效果，思考完毕即消失 */
.b-think {
  display: flex; align-items: center; gap: 4px;
  padding: 3px 8px; border-radius: 6px;
  background: rgba(var(--accent2-rgb), 0.08);
  border: 1px solid rgba(var(--accent2-rgb), 0.22);
  font-size: calc(10.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.72);
  max-width: 100%; min-width: 0;
}
.b-think-icon { flex-shrink: 0; font-size: calc(10.5px * var(--font-scale)); }
.b-think-text {
  flex: 1; min-width: 0;
  overflow: hidden; white-space: nowrap;
  font-style: italic; letter-spacing: 0.2px;
}
.b-think-cursor {
  flex-shrink: 0; color: var(--accent-2); font-weight: 600;
  animation: cursorBlink 1s steps(2) infinite;
}

/* —— 气泡内工具行（和对话界面 tr-pill 同样式） —— */
.b-tool-row {
  display: flex; align-items: center; gap: 5px; padding: 3px 8px;
  border-radius: 8px; background: rgba(var(--shadow-rgb), 0.12);
  border: 1px solid rgba(var(--surface-rgb), 0.08);
  font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.85); user-select: none;
}
.b-tool-row.running {
  border-color: rgba(var(--accent-rgb), 0.55); background: rgba(var(--accent-rgb), 0.1); color: var(--accent-light);
}
.b-tool-icon { width: 12px; text-align: center; font-size: calc(11px * var(--font-scale)); transition: transform .3s; }
.b-tool-icon.spinning { display: inline-block; animation: tiSpin .8s linear infinite; }
@keyframes tiSpin { to { transform: rotate(360deg); } }
.b-tool-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.b-tool-fn { color: var(--success-light); font-size: calc(10px * var(--font-scale)); font-family: Consolas, monospace; max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; flex-shrink: 0; }
.b-tool-status-text { font-size: calc(10px * var(--font-scale)); opacity: .65; flex-shrink: 0; }

/* —— 气泡内 Markdown —— */
.b-text.markdown { white-space: normal; word-break: break-word; }
.b-text.markdown > :first-child { margin-top: 0; }
.b-text.markdown > :last-child { margin-bottom: 0; }
.b-text.markdown p { margin: 0 0 5px; }
.b-text.markdown p:last-child { margin-bottom: 0; }
.b-text.markdown h1, .b-text.markdown h2, .b-text.markdown h3,
.b-text.markdown h4, .b-text.markdown h5, .b-text.markdown h6 {
  font-size: calc(12px * var(--font-scale)); font-weight: 600; margin: 6px 0 3px; line-height: 1.35;
}
.b-text.markdown ul, .b-text.markdown ol { margin: 3px 0 5px; padding-left: 16px; }
.b-text.markdown li { margin: 1px 0; }
.b-text.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: calc(10.5px * var(--font-scale));
  background: rgba(var(--shadow-rgb), 0.18); padding: 0 3px; border-radius: 3px;
}
.b-text.markdown pre {
  background: rgba(var(--shadow-rgb), 0.22); border-radius: 5px; padding: 5px 7px;
  margin: 5px 0; overflow-x: auto; white-space: pre; font-size: calc(10.5px * var(--font-scale));
}
.b-text.markdown pre code { background: none; padding: 0; color: var(--text); }
.b-text.markdown blockquote {
  border-left: 2px solid rgba(var(--accent-rgb), 0.4); padding: 1px 7px; margin: 4px 0;
  color: rgba(var(--text-rgb), 0.75);
}
.b-text.markdown blockquote p { margin: 0; }
.b-text.markdown a { color: var(--accent-light); text-decoration: underline; text-underline-offset: 2px; }
.b-text.markdown hr { border: none; border-top: 1px solid rgba(var(--surface-rgb), 0.12); margin: 6px 0; }
.b-text.markdown table { border-collapse: collapse; margin: 4px 0; font-size: calc(10.5px * var(--font-scale)); }
.b-text.markdown th, .b-text.markdown td {
  border: 1px solid rgba(var(--surface-rgb), 0.12); padding: 2px 6px;
}
.b-text.markdown th { background: rgba(var(--surface-rgb), 0.06); }
.b-text.markdown strong { font-weight: 600; }
.b-text.markdown em { font-style: italic; }
.b-text.markdown img { max-width: 100%; border-radius: 5px; margin: 3px 0; }

.node-head { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.avatar { position: relative; overflow: hidden; width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: calc(12px * var(--font-scale)); font-weight: 600; color: #fff; text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.3); box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45), inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32), inset 0 0 0 1px rgba(var(--surface-rgb), 0.08), 0 3px 8px rgba(var(--shadow-rgb), 0.4); }
.avatar::before { content: ''; position: absolute; inset: 0; background: linear-gradient(165deg, rgba(var(--surface-rgb), 0.45) 0%, rgba(var(--surface-rgb), 0.10) 32%, rgba(var(--surface-rgb), 0) 52%), linear-gradient(0deg, rgba(var(--shadow-rgb), 0.24) 0%, rgba(var(--shadow-rgb), 0) 42%); pointer-events: none; }
.name { font-size: calc(13px * var(--font-scale)); font-weight: 500; }
.node-actions { display: flex; gap: 4px; }
.node .btn { backdrop-filter: none; -webkit-backdrop-filter: none; }
.xs { padding: 3px 8px; font-size: calc(11px * var(--font-scale)); }

.hint { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); line-height: 1.8; max-width: 460px; z-index: 5; pointer-events: none; }
.hint-shortcuts { display: block; margin-top: 10px; font-size: calc(11px * var(--font-scale)); opacity: .7; line-height: 1.9; }

/* 套索选框 */
.lasso-rect { position: absolute; z-index: 8; pointer-events: none; border-radius: 2px; }
.lasso-rect.contain { border: 1px solid rgba(var(--accent2-rgb), 0.95); background: rgba(var(--accent2-rgb), 0.10); }
.lasso-rect.touch { border: 1px dashed rgba(var(--accent-rgb), 0.95); background: rgba(var(--accent-rgb), 0.09); }
/* Shift+Ctrl 连续框选：加一圈外发光提示处于累加模式 */
.lasso-rect.additive { box-shadow: 0 0 0 1px rgba(var(--accent2-rgb), 0.55), 0 0 14px rgba(var(--accent2-rgb), 0.28); }

/* 选区提示条 */
.selection-pill {
  position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%);
  z-index: 15; padding: 6px 14px; white-space: nowrap; pointer-events: none;
  font-size: calc(11.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.92);
  background: rgba(var(--node-bg-rgb), 0.92); border: 1px solid rgba(var(--accent2-rgb), 0.45);
  border-radius: 18px; box-shadow: 0 6px 20px rgba(var(--shadow-rgb), 0.4);
  backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
}

/* 右键菜单快捷键提示 */
.ctx-kbd { float: right; margin-left: 14px; font-size: calc(10px * var(--font-scale)); opacity: .55; font-family: Consolas, monospace; }
.ctx-item.disabled { opacity: .4; pointer-events: none; }
.new-cv-preview { font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.6); margin-top: -2px; }

/* 右键菜单 */
.ctx-menu { position: fixed; min-width: 220px; padding: 6px; z-index: 9999; background: rgba(var(--node-bg-rgb), 0.98); box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.5); }
.ctx-title { font-size: calc(11px * var(--font-scale)); padding: 6px 10px; color: rgba(var(--text-rgb), 0.68); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); margin-bottom: 4px; }
.ctx-item { padding: 8px 12px; border-radius: 6px; font-size: calc(13px * var(--font-scale)); cursor: pointer; transition: background .15s; }
.ctx-item:hover { background: rgba(var(--accent-rgb), 0.18); }
.ctx-item.danger:hover { background: rgba(255,80,80,0.18); color: #ff9090; }
.ctx-divider { height: 1px; background: rgba(var(--surface-rgb), 0.08); margin: 4px 6px; }
.ctx-item.submenu { display: flex; flex-direction: column; gap: 2px; }
.submenu-label { padding: 0; font-size: calc(13px * var(--font-scale)); cursor: default; }
.submenu-body { display: flex; flex-direction: column; gap: 1px; margin-top: 2px; }
.sub-item { padding: 6px 10px; border-radius: 5px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.75); cursor: pointer; transition: background .15s; }
.sub-item:hover { background: rgba(var(--accent2-rgb), 0.15); color: var(--accent2-light); }
.sub-item.checked { color: var(--accent2-light); background: rgba(var(--accent2-rgb), 0.12); }
.sub-item.checked::before { content: '✓ '; color: var(--accent-2); }
/* 绘制中的线型菜单：当前选中项 */
.ctx-item.checked { color: var(--accent2-light); background: rgba(var(--accent2-rgb), 0.14); }
.ctx-item.checked::before { content: '✓ '; color: var(--accent-2); }
.seg-menu { min-width: 250px; }
.seg-menu-note { padding: 4px 10px 6px; font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.55); line-height: 1.5; white-space: normal; }

/* 新建画布对话框 */
.modal-bg { position: fixed; inset: 0; background: rgba(var(--shadow-rgb), 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 360px; padding: 20px; }
.new-cv-modal .lbl { display: flex; flex-direction: column; gap: 6px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); }
.modal-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 14px; }
.modal-body { display: flex; flex-direction: column; gap: 10px; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.modal-card { background: rgba(var(--node-bg-rgb), 0.98); border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 12px; padding: 22px 24px; box-shadow: 0 24px 64px rgba(var(--shadow-rgb), 0.55); }
.wd-row { display: flex; gap: 8px; align-items: stretch; }
.wd-input { flex: 1; width: auto; padding: 8px 10px; font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, monospace; }
.wd-browse { flex-shrink: 0; padding: 0 12px; height: 34px; font-size: calc(12px * var(--font-scale)); }
.wd-existing { font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.58); padding: 4px 2px; }
.btn.activeWd { background: rgba(100,220,160,0.15); border-color: rgba(100,220,160,0.4); color: var(--success-light); }
.modal-body .hint { position: static; transform: none; font-size: calc(12px * var(--font-scale)); opacity: .7; line-height: 1.6; pointer-events: auto; text-align: left; max-width: none; margin: 0; padding: 0; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }

/* 运行/停止 + 触发 */
.run-btn.stop {
  position: relative; z-index: 1;
  /* 停止/危险态保留红色，但改为单色平涂，去掉双色渐变 */
  background: rgba(255, 90, 95, 0.55);
  border-color: rgba(255,140,150,0.7);
  color: rgb(var(--text-rgb));
  animation: stopBtnPulse 1.4s ease-in-out infinite;
}
.run-btn.stop:hover {
  background: rgba(255, 90, 95, 0.68);
  transform: translateY(-1px) scale(1.03);
  box-shadow: 0 4px 18px rgba(255,80,100,0.45);
}
.run-btn.stop .stop-icon {
  position: relative; display: inline-block; width: 14px; height: 14px; border-radius: 3px;
  background: #fff; margin-right: 7px; vertical-align: middle; flex-shrink: 0;
  box-shadow: 0 0 8px rgba(var(--surface-rgb), 0.55);
  animation: stopIconPulse 1.4s ease-in-out infinite;
}
.run-btn.stop .stop-icon::before,
.run-btn.stop .stop-icon::after {
  content: ''; position: absolute; inset: -6px; border-radius: 50%;
  border: 1.5px dashed rgba(var(--surface-rgb), 0.65);
  animation: stopRingSpin 3s linear infinite;
}
.run-btn.stop .stop-icon::after {
  inset: -10px; border-style: dotted;
  border-color: rgba(var(--surface-rgb), 0.35);
  animation: stopRingSpin 6s linear infinite reverse;
}
.canvas-toolbar.collapsed .run-btn.stop .stop-icon { margin-right: 0; }
@keyframes stopBtnPulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,80,100,0.5); } 50% { box-shadow: 0 0 0 6px rgba(255,80,100,0); } }
@keyframes stopIconPulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(0.75); opacity: 0.85; } }
@keyframes stopRingSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.run-btn:not(.stop) { background: rgba(100,220,160,0.12); border-color: rgba(100,220,160,0.35); color: var(--success-light); }
.run-btn:not(.stop):hover { background: rgba(100,220,160,0.22); }
/* 串行/并行切换 */
.em-collapsed { font-size: calc(14px * var(--font-scale)); padding: 0 8px; line-height: 24px; }
.em-collapsed:hover { background: rgba(var(--accent2-rgb), 0.15); color: var(--accent2-light); }
.collapsed-close { font-size: calc(14px * var(--font-scale)); padding: 0 8px; line-height: 24px; }
.exec-mode-toggle { display: flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 8px; background: rgba(var(--surface-rgb), 0.04); cursor: pointer; }
.em-label { font-size: calc(11px * var(--font-scale)); opacity: 0.45; transition: opacity 0.2s, color 0.2s; white-space: nowrap; }
.em-label.active { opacity: 1; color: var(--accent2-light); font-weight: 500; }
.em-switch { position: relative; width: 32px; height: 16px; display: inline-block; flex-shrink: 0; }
.em-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.em-slider { position: absolute; inset: 0; background: rgba(var(--surface-rgb), 0.15); border-radius: 16px; cursor: pointer; transition: background 0.25s; }
.em-slider::before { content: ''; position: absolute; width: 12px; height: 12px; left: 2px; top: 2px; background: #fff; border-radius: 50%; transition: transform 0.25s; }
.em-switch input:checked + .em-slider { background: rgba(var(--accent2-rgb), 0.6); }
.em-switch input:checked + .em-slider::before { transform: translateX(16px); }
.em-switch:hover .em-slider { background: rgba(var(--surface-rgb), 0.25); }
.em-switch input:checked:hover + .em-slider { background: rgba(var(--accent2-rgb), 0.75); }
/* 运行时挂起态：图标/开关下方有一条淡色"待生效"下划线 */
.em-collapsed.pending { border-bottom: 2px dashed rgba(var(--accent2-rgb), 0.5); padding-bottom: 0; }
.exec-mode-toggle.pending { box-shadow: 0 0 0 1px rgba(var(--accent2-rgb), 0.4) inset; }
/* 执行模式 toast */
.exec-mode-toast {
  position: absolute; top: 52px; left: 50%; transform: translateX(-50%);
  z-index: 20; padding: 8px 14px;
  font-size: calc(12px * var(--font-scale));
  color: rgba(var(--text-rgb), 0.95);
  background: rgba(var(--node-bg-rgb), 0.92);
  border: 1px solid rgba(var(--accent2-rgb), 0.45);
  border-radius: 20px;
  box-shadow: 0 6px 20px rgba(var(--shadow-rgb), 0.45);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  animation: emToastIn .22s ease-out;
  white-space: nowrap; pointer-events: none;
}
@keyframes emToastIn {
  from { opacity: 0; transform: translate(-50%, -4px); }
  to   { opacity: 1; transform: translate(-50%, 0); }
}
.trigger-types { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.tr-opt { display: flex; gap: 8px; align-items: center; font-size: calc(13px * var(--font-scale)); cursor: pointer; color: rgba(var(--text-rgb), 0.85); }
.modal .lbl { display: flex; flex-direction: column; gap: 6px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); margin-bottom: 10px; }
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
  font-size: calc(12px * var(--font-scale)); font-weight: 600; margin: 6px 0 3px; line-height: 1.35;
}
.b-text.markdown h1:first-child, .b-text.markdown h2:first-child,
.b-text.markdown h3:first-child { margin-top: 0; }
.b-text.markdown ul, .b-text.markdown ol { margin: 3px 0 5px; padding-left: 16px; }
.b-text.markdown li { margin: 1px 0; }
.b-text.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: calc(10.5px * var(--font-scale));
  background: rgba(var(--shadow-rgb), 0.18); padding: 0 3px; border-radius: 3px;
}
.b-text.markdown pre {
  background: rgba(var(--shadow-rgb), 0.22); border-radius: 5px; padding: 5px 7px;
  margin: 5px 0; overflow-x: auto; white-space: pre; font-size: calc(10.5px * var(--font-scale));
}
.b-text.markdown pre code { background: none; padding: 0; color: var(--text); }
.b-text.markdown blockquote {
  border-left: 2px solid rgba(var(--accent-rgb), 0.4); padding: 1px 7px; margin: 4px 0;
  color: rgba(var(--text-rgb), 0.75);
}
.b-text.markdown blockquote p { margin: 0; }
.b-text.markdown a { color: var(--accent-light); text-decoration: underline; text-underline-offset: 2px; }
.b-text.markdown hr { border: none; border-top: 1px solid rgba(var(--surface-rgb), 0.12); margin: 6px 0; }
.b-text.markdown table { border-collapse: collapse; margin: 4px 0; font-size: calc(10.5px * var(--font-scale)); }
.b-text.markdown th, .b-text.markdown td {
  border: 1px solid rgba(var(--surface-rgb), 0.12); padding: 2px 6px;
}
.b-text.markdown th { background: rgba(var(--surface-rgb), 0.06); }
.b-text.markdown strong { font-weight: 600; }
.b-text.markdown em { font-style: italic; }
.b-text.markdown img { max-width: 100%; border-radius: 5px; margin: 3px 0; }
</style>
