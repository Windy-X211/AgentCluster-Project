
<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useConvStore, useAgentStore, useCanvasStore, useModelStore, useSettingsStore, useLayoutStore } from '@/stores'
import { convDisplayName, convTimeLabel, sortConvsByActivity, buildToolGroups } from '@/stores/conversation'
import { renderMarkdown } from '@/utils/markdown'
import { api } from '@/api'

/** 去掉消息首尾纯空行（保留中间换行）—— 用于修复气泡顶部空白 */
function trimMsgContent(s: string | undefined): string {
  if (!s) return ''
  return s.replace(/^\s*\n+/, '').replace(/\n+\s*$/, '')
}
/** 消息泡泡 Markdown 渲染（用户/助手统一走这里，含流式光标） */
function msgBubbleHtml(m: any): string {
  const html = renderMarkdown(trimMsgContent(m.content))
  return isStreamingMsg(m) ? html + '<span class="stream-cursor">▌</span>' : html
}

const convs = useConvStore()
const agents = useAgentStore()
const canvases = useCanvasStore()
const models = useModelStore()
const settings = useSettingsStore()
const layout = useLayoutStore()

/** 消息是否处于"活动流式"状态：仅当全局确实在流（convs.streaming）且该消息标了 streaming
 *  才视为流式。避免历史遗留的 streaming:true（异常中断被持久化）渲染出永不消失的空泡泡+闪烁光标 */
function isStreamingMsg(m: any): boolean {
  return !!(m?.streaming && convs.streaming)
}

const input = ref('')
const currentAgentId = ref<number | null>(null)
const msgBox = ref<HTMLElement | null>(null)
const taRef = ref<HTMLTextAreaElement | null>(null)
const renameId = ref<number | null>(null)
const renameVal = ref('')
const convsCollapsed = ref(true) // 对话记录列表默认折叠（展开时浮层覆盖画布）
const canvasesCollapsed = ref(false) // 画布列表默认展开
const convDesc = ref(true) // 对话记录排序：true = 新在上，false = 新在下

// 对话列表浮层的 right 偏移量跟随右侧面板宽度变化
const convFloatStyle = computed(() => ({ right: `${layout.rightWidth + 10}px` }))

// 对话列表右键菜单
const selMode = ref(false)
const selectedIds = ref<number[]>([])
const ctxMenu = ref({ show: false, x: 0, y: 0, id: 0 as number | null })
// 画布列表右键菜单
const cvCtxMenu = ref({ show: false, x: 0, y: 0, id: 0 as number | null })

const conv = computed(() => convs.currentData())
const canvas = computed(() => canvases.current)
// 展示用：始终按最后一条消息时间降序
const sortedConvs = computed(() => {
  const base = sortConvsByActivity(convs.list)
  return convDesc.value ? base : [...base].reverse()
})
/** 会话序号（按活跃度，最旧 = 1），与当前排序方向无关 */
function convSeqOf(id: number): number {
  return seqMap.value.get(id) ?? 0
}
const seqMap = computed(() => {
  const m = new Map<number, number>()
  const base = sortConvsByActivity(convs.list) // 新在上
  base.forEach((c, i) => m.set(c.id, base.length - i)) // 最旧 = 1
  return m
})

// ② relay 过滤：排除发给其他智能体的接力消息，A2A 信封卡片保留
const visibleMsgs = computed(() => (conv.value?.messages || []).filter((m: any) => !m.relay))

// ③ 连续工具调用分组折叠 + A2A 信封折叠
const expandedA2a = ref(new Set<number>())
const toolBarOpen = ref(new Set<number>())
const toolDetailOpen = ref(new Map<number, string>())

/** 按 assistant 索引 → 其后连续的工具卡片（同 agent_id）。
 *  分几轮调用工具时，轮与轮之间的空占位 assistant 会被跨过，前后工具并成一组
 *  → 只渲染一条工具行（智能体输出正文时界面更整洁） */
const msgToolsMap = computed(() => buildToolGroups(visibleMsgs.value))

/** 当前发言者已有"执行中"的 tool_call 消息（本轮，最近一条用户消息之后）→
 *  不再往无工具的空 assistant 注入虚拟 running pill，避免同一件事显示两遍 */
const hasLiveToolMsg = computed(() => {
  const msgs = visibleMsgs.value
  const sp = convs.speakingAgent
  if (sp == null) return false
  let start = 0
  for (let i = msgs.length - 1; i >= 0; i--) {
    if ((msgs[i] as any)?.role === 'user') { start = i + 1; break }
  }
  for (let i = start; i < msgs.length; i++) {
    const x = msgs[i] as any
    if (x?.role !== 'tool_call' || x.agent_id !== sp) continue
    const done = msgs.some((r: any) => r.role === 'tool_result' && r.tool_call_id === x.tool_call_id)
    if (!done) return true
  }
  return false
})

const speakingTool = computed(() => convs.speakingToolCall)

/** 本轮（最近一条用户消息之后）的最后一条 assistant 索引；没有则 -1 */
const lastAssistantIdx = computed(() => {
  const msgs = visibleMsgs.value
  let start = 0
  for (let i = msgs.length - 1; i >= 0; i--) {
    if ((msgs[i] as any)?.role === 'user') { start = i + 1; break }
  }
  for (let i = msgs.length - 1; i >= start; i--) {
    if ((msgs[i] as any)?.role === 'assistant') return i
  }
  return -1
})

function toolsOfAssistant(assistantIdx: number): any[] {
  const a = visibleMsgs.value[assistantIdx] as any
  if (!a) return []
  const archived = msgToolsMap.value.get(assistantIdx) || []
  // 虚拟 running pill 只注入「本轮最后一条 assistant」：
  // 否则 TS:（参数流式中、C: 尚未到达）阶段 speakingTool 已设置而消息里还没有任何
  // tool_call，同 agent 的每条历史 assistant 都会被注入同一个 pill → 一次性冒出
  // N 条重复的工具行
  if (convs.streaming && speakingTool.value && speakingAgentSameAs(assistantIdx)
      && assistantIdx === lastAssistantIdx.value && !hasLiveToolMsg.value) {
    const hasVirtual = archived.some((x: any) => x.role === 'tool_call' && x._running)
    if (!hasVirtual) {
      const lastCall = [...archived].reverse().find((x: any) => x.role === 'tool_call')
      if (!lastCall) {
        return [{ role: 'tool_call', name: speakingTool.value.name, tool_call_id: '_virtual_running', args: {}, _running: speakingTool.value.phase || 'args' }, ...archived]
      }
    }
  }
  return archived
}
function speakingAgentSameAs(assistantIdx: number): boolean {
  const a = visibleMsgs.value[assistantIdx] as any
  if (!a) return false
  return a.agent_id != null && a.agent_id === convs.speakingAgent
}
function isLastAssistant(idx: number): boolean {
  return idx === lastAssistantIdx.value
}
/** 工具行是否"有真实工具调用"（用于决定是否显示工具行） */
function hasTools(idx: number): boolean {
  const tools = toolsOfAssistant(idx)
  return tools.some((t: any) => t.role === 'tool_call')
}
function toolBarIsOpen(idx: number): boolean {
  if (toolBarOpen.value.has(idx)) return true
  if (convs.streaming && isLastAssistant(idx) && hasTools(idx)) return true
  return false
}
function toggleToolBar(idx: number) {
  const s = new Set(toolBarOpen.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  toolBarOpen.value = s
}
function toggleToolDetail(assistantIdx: number, tcId: string) {
  const m = new Map(toolDetailOpen.value)
  if (m.get(assistantIdx) === tcId) m.delete(assistantIdx)
  else m.set(assistantIdx, tcId)
  toolDetailOpen.value = m
}
function toolDetailIsOpen(assistantIdx: number, tcId: string): boolean {
  return toolDetailOpen.value.get(assistantIdx) === tcId
}
function toolIsRunning(tc: any): boolean {
  if (tc._running) return true
  if (tc.role !== 'tool_call') return false
  // 在整个 visibleMsgs 里搜索配对 tool_result（避免 bucket 归属错乱问题）
  const hasResult = visibleMsgs.value.some(
    (x: any) => x.role === 'tool_result' && x.tool_call_id === tc.tool_call_id
  )
  return !hasResult
}

const latestToolInfoMap = computed(() => {
  const map = new Map<number, { name: string; running: boolean; total: number; index: number } | null>()
  const msgs = visibleMsgs.value
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i] as any
    if (m.role !== 'assistant') continue
    const tools = toolsOfAssistant(i)
    const calls = tools.filter((t: any) => t.role === 'tool_call')
    if (calls.length === 0) continue
    // 正在执行的优先；否则取最后一个（最新完成的）
    const runningIdx = calls.findIndex((t: any) => toolIsRunning(t))
    const pickedIdx = runningIdx >= 0 ? runningIdx : calls.length - 1
    const picked = calls[pickedIdx]
    map.set(i, {
      name: toolLabel(picked.name),
      running: runningIdx >= 0,
      total: calls.length,          // 总工具调用数
      index: pickedIdx,             // 当前展示的是第几个（0-based）
    })
  }
  return map
})
function latestToolInfo(idx: number) {
  return latestToolInfoMap.value.get(idx) || null
}

function prettyArgs(args: any): string { try { return JSON.stringify(args, null, 2) } catch { return String(args) } }
function prettyResult(result: any): string { try { return JSON.stringify(result, null, 2) } catch { return String(result) } }
function toggleA2a(idx: number) {
  const s = new Set(expandedA2a.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  expandedA2a.value = s
}

// ==============================================================
// 右键菜单 + 文件操作历史还原
// ==============================================================

interface FileOp {
  tool_call_id: string
  agent_id: number
  agent_name?: string
  ts: string
  op: string
  path: string
  snapshot_dir: string
  before_exists: boolean
  note?: string
  restored?: string[]
  restored_at?: string
}

const msgCtxMenu = ref<{ x: number; y: number; msgIdx: number } | null>(null)
const msgMenuEl = ref<HTMLElement | null>(null)

function onMsgContextMenu(e: MouseEvent, idx: number) {
  e.preventDefault()
  const menuW = 260
  const menuH = 60
  const pad = 8
  const x = Math.max(pad, Math.min(e.clientX, window.innerWidth - menuW - pad))
  const y = Math.max(pad, Math.min(e.clientY, window.innerHeight - menuH - pad))
  msgCtxMenu.value = { x, y, msgIdx: idx }
  // 渲染后再按实际尺寸精确校正，避免估算偏差导致超出屏幕
  nextTick(() => {
    const el = msgMenuEl.value
    const v = msgCtxMenu.value
    if (!el || !v) return
    const r = el.getBoundingClientRect()
    let nx = v.x
    let ny = v.y
    if (r.right > window.innerWidth - pad) nx = Math.max(pad, window.innerWidth - r.width - pad)
    if (r.bottom > window.innerHeight - pad) ny = Math.max(pad, window.innerHeight - r.height - pad)
    if (nx !== v.x || ny !== v.y) msgCtxMenu.value = { ...v, x: nx, y: ny }
  })
}
function closeMsgCtx() { msgCtxMenu.value = null }

const fileOpsDlg = ref<{ open: boolean; loading: boolean; ops: FileOp[]; sinceTs: string; msgContent: string }>(
  { open: false, loading: false, ops: [], sinceTs: '', msgContent: '' }
)
const checkedOps = ref<Set<number>>(new Set())
const restoreResult = ref<string>('')

async function openFileOpsFromMsg() {
  const menu = msgCtxMenu.value
  closeMsgCtx()
  if (!menu) return
  const c = conv.value
  if (!c) return
  const msgs = visibleMsgs.value
  const m = msgs[menu.msgIdx]
  if (!m || m.role !== 'user') return

  fileOpsDlg.value = {
    open: true,
    loading: true,
    ops: [],
    sinceTs: m.ts || '',
    msgContent: (m.content || '').slice(0, 80) + ((m.content || '').length > 80 ? '…' : ''),
  }
  checkedOps.value = new Set()
  restoreResult.value = ''

  try {
    const r = await api.convs.fileOps(c.id, m.ts)
    const allOps = (await api.convs.fileOps(c.id, undefined)).ops as FileOp[]
    const fullIdxMap = new Map<string, number>()
    allOps.forEach((o, fi) => fullIdxMap.set(o.tool_call_id + '|' + o.path, fi))
    fileOpsDlg.value.ops = (r.ops || []).map((o: FileOp) => ({
      ...o,
      _fullIdx: fullIdxMap.get(o.tool_call_id + '|' + o.path) ?? -1,
    })) as any
  } catch (e: any) {
    restoreResult.value = '加载失败: ' + (e?.message || String(e))
  } finally {
    fileOpsDlg.value.loading = false
  }
}

function toggleCheckOp(i: number) {
  const idx = (fileOpsDlg.value.ops[i] as any)?._fullIdx
  if (idx < 0) return
  const s = new Set(checkedOps.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  checkedOps.value = s
}
function toggleCheckAll() {
  const allIdx = fileOpsDlg.value.ops
    .map((_, i) => (fileOpsDlg.value.ops[i] as any)._fullIdx)
    .filter((x: number) => x >= 0)
  if (checkedOps.value.size === allIdx.length) {
    checkedOps.value = new Set()
  } else {
    checkedOps.value = new Set(allIdx)
  }
}

async function restoreSelected() {
  const c = conv.value
  if (!c) return
  const idxList = [...checkedOps.value]
  if (!idxList.length) { restoreResult.value = '请先勾选要还原的操作'; return }
  try {
    const r = await api.convs.restoreFileOps(c.id, idxList)
    restoreResult.value = `✅ 已还原 ${r.count} 条`
    const mts = fileOpsDlg.value.sinceTs
    const r2 = await api.convs.fileOps(c.id, mts)
    const allOps = (await api.convs.fileOps(c.id, undefined)).ops as FileOp[]
    const fullIdxMap = new Map<string, number>()
    allOps.forEach((o, i) => fullIdxMap.set(o.tool_call_id + '|' + o.path, i))
    fileOpsDlg.value.ops = (r2.ops || []).map((o: FileOp) => ({
      ...o,
      _fullIdx: fullIdxMap.get(o.tool_call_id + '|' + o.path) ?? -1,
    })) as any
    checkedOps.value = new Set()
  } catch (e: any) {
    restoreResult.value = '还原失败: ' + (e?.message || String(e))
  }
}

async function restoreOneOp(i: number) {
  const c = conv.value
  if (!c) return
  const op = fileOpsDlg.value.ops[i] as any
  if (!op || op._fullIdx < 0) return
  try {
    const r = await api.convs.restoreOneFileOp(c.id, op._fullIdx)
    restoreResult.value = r?.ok ? `✅ 已还原：${op.path}` : `❌ ${r?.error || '失败'}`
    const mts = fileOpsDlg.value.sinceTs
    const r2 = await api.convs.fileOps(c.id, mts)
    const allOps = (await api.convs.fileOps(c.id, undefined)).ops as FileOp[]
    const fullIdxMap = new Map<string, number>()
    allOps.forEach((o, i) => fullIdxMap.set(o.tool_call_id + '|' + o.path, i))
    fileOpsDlg.value.ops = (r2.ops || []).map((o: FileOp) => ({
      ...o,
      _fullIdx: fullIdxMap.get(o.tool_call_id + '|' + o.path) ?? -1,
    })) as any
  } catch (e: any) {
    restoreResult.value = '还原失败: ' + (e?.message || String(e))
  }
}

function friendlyOp(op: string): string {
  const map: Record<string, string> = {
    'file.write': '✍️ 写文件',
    'file.delete': '🗑 删文件',
    'file.replace': '🔧 替换文本',
    'file.patch': '🩹 补丁编辑',
  }
  return map[op] ?? op
}

// ③ 流式自动滚动：streaming + 最后消息内容长度 + 消息数
const scrollKey = computed(() => {
  const msgs = conv.value?.messages || []
  const last = msgs[msgs.length - 1]
  return `${convs.streaming ? 1 : 0}:${msgs.length}:${(last?.content || '').length}`
})
watch(scrollKey, async () => {
  await nextTick()
  if (msgBox.value) msgBox.value.scrollTop = msgBox.value.scrollHeight
})

// ① 节点💬直接对话：回填选中智能体并聚焦输入框
watch(() => convs.selectedAgentId, (id) => {
  if (id != null) currentAgentId.value = id
})
watch(() => convs.focusChatAt, async (t) => {
  if (!t) return
  await nextTick()
  taRef.value?.focus()
})

async function send() {
  const text = input.value.trim(); if (!text) return
  input.value = ''  // ← 立刻清空，不等流式结束
  const agentId = currentAgentId.value ?? agents.list[0]?.id; if (!agentId) return alert('请先新建智能体')
  await convs.send(agentId, text)
  setTimeout(() => msgBox.value && (msgBox.value.scrollTop = msgBox.value.scrollHeight), 50)
}
function formatResult(r: any): string {
  if (!r) return '(空)'
  try { return JSON.stringify(r, null, 2).slice(0, 2000) } catch { return String(r).slice(0, 2000) }
}
function agentNameOf(agentId: number | null | undefined): string {
  if (agentId == null) return ''
  return agents.list.find((a: any) => a.id === agentId)?.name || ''
}
function kindLabel(kind: string): string {
  return ({ handoff: '成果移交', message: '交流讨论', artifact: '成果' } as Record<string, string>)[kind] || kind
}
function toolLabel(name: string): string {
  if (!name) return name
  const map: Record<string, string> = {
    'cluster.list_agents': '📋 列出智能体',
    'cluster.create_agent': '✨ 创建智能体',
    'cluster.update_agent': '✏️ 更新智能体',
    'cluster.delete_agent': '🗑 删除智能体',
    'cluster.list_canvases': '📋 列出画布',
    'cluster.create_canvas': '🎨 创建画布',
    'cluster.delete_canvas': '🗑 删除画布',
    'cluster.get_canvas': '🔍 查看画布',
    'cluster.add_node': '➕ 添加节点',
    'cluster.remove_node': '➖ 移除节点',
    'cluster.connect': '🔗 连接节点',
    'cluster.disconnect': '✂️ 断开连线',
    'cluster.list_rules': '📜 列出规章制度',
    'cluster.create_rule': '📜 新增规章制度',
    'cluster.update_rule': '✏️ 修改规章制度',
    'cluster.delete_rule': '🗑 删除规章制度',
    'cluster.handoff': '🤝 A2A 移交',
    'cluster.send_message': '💬 发送消息',
    'file.read': '📖 读文件',
    'file.write': '✍️ 写文件',
    'file.patch': '🩹 补丁编辑',
    'file.delete': '🗑 删文件',
    'file.replace': '🔧 替换文本',
    'file.search': '🔍 搜索内容',
    'file.structure': '🗂 文件树',
    'cmd.run': '⚙️ 运行命令',
  }
  return map[name] ?? name
}
async function newConv(mode: 'independent' | 'linked') {
  const names: Record<string, string> = { independent: '独立对话', linked: '集群对话' }
  // 重置画布上下文：清空当前画布绑定的集群对话历史（画布气泡的数据源）
  const cid = canvas.value?.id
  if (cid != null) {
    // 只重置「还有历史」的会话：空会话再 PUT 一次是纯浪费
    // （每次 PUT = 读+重写整个会话文件 + fsync，几十到上百毫秒），并行发请求
    const linked = convs.list.filter((c: any) =>
      c.mode === 'linked' && c.canvas_id === cid && !(Array.isArray(c.messages) && c.messages.length === 0))
    if (linked.length) {
      await Promise.all(linked.map((c: any) => convs.update(c.id, { reset_messages: true })))
    }
  }
  await convs.create({
    name: names[mode],
    mode,
    agent_ids: currentAgentId.value ? [currentAgentId.value] : [],
    canvas_id: canvas.value?.id
  })
}
function selectConv(id: number) {
  if (selMode.value) { toggleSel(id); return }
  convs.current = id
}
function startRenameCanvas(id: number, name: string) { renameId.value = id; renameVal.value = name }
// —— 画布列表右键菜单 ——
function onCanvasContext(e: MouseEvent, id: number) {
  e.preventDefault()
  e.stopPropagation()
  cvCtxMenu.value = { show: true, x: e.clientX, y: e.clientY, id }
  closeCtx()
}
function closeCvCtx() { cvCtxMenu.value.show = false }
function selectCanvas(id: number) { canvases.current = canvases.list.find((c: any) => c.id === id) || null }
async function deleteCanvas(id: number) {
  const c = canvases.list.find((x: any) => x.id === id); if (!c) return
  if (!confirm(`删除画布「${c.name}」？`)) return
  await canvases.remove(id); closeCvCtx()
}
async function commitRenameCanvas() {
  if (renameId.value != null && renameVal.value.trim()) await canvases.rename(renameId.value, renameVal.value.trim())
  renameId.value = null
}

// —— 画布列表拖拽排序 ——
const cvDragId = ref<number | null>(null)
const cvOverId = ref<number | null>(null)
const cvOverPos = ref<'before' | 'after'>('before')
function onCvDragStart(e: DragEvent, c: any) {
  cvDragId.value = c.id
  if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(c.id)) }
}
function onCvDragEnd() { cvDragId.value = null; cvOverId.value = null }
function onCvDragOver(e: DragEvent, c: any) {
  if (cvDragId.value == null) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  cvOverPos.value = (e.clientY - rect.top) < rect.height / 2 ? 'before' : 'after'
  cvOverId.value = c.id
}
function onCvDragLeave(c: any) { if (cvOverId.value === c.id) cvOverId.value = null }
function onCvDrop(e: DragEvent, c: any) {
  if (cvDragId.value == null) return
  e.preventDefault(); e.stopPropagation()
  const srcId = cvDragId.value
  cvDragId.value = null; cvOverId.value = null
  if (srcId === c.id) return
  const cur = canvases.list.map(x => x.id)
  if (!cur.includes(srcId)) return
  const without = cur.filter(id => id !== srcId)
  let to = without.indexOf(c.id)
  if (to < 0) to = without.length
  if (cvOverPos.value === 'after') to++
  without.splice(to, 0, srcId)
  canvases.reorder(without)
}

// —— 右键菜单 / 多选删除 ——
function onConvContext(e: MouseEvent, id: number) {
  e.preventDefault()
  ctxMenu.value = { show: true, x: e.clientX, y: e.clientY, id }
}
function closeCtx() { ctxMenu.value = { ...ctxMenu.value, show: false } }
function enterSelMode() {
  selMode.value = true
  const id = ctxMenu.value.id
  selectedIds.value = id != null && !selectedIds.value.includes(id) ? [...selectedIds.value, id] : selectedIds.value
  closeCtx()
}
function selectAll() {
  selectedIds.value = sortedConvs.value.map(c => c.id)
  selMode.value = true
  closeCtx()
}
function toggleSel(id: number) {
  const i = selectedIds.value.indexOf(id)
  if (i >= 0) selectedIds.value.splice(i, 1)
  else selectedIds.value.push(id)
  if (!selectedIds.value.length) selMode.value = false
}
function exitSel() { selMode.value = false; selectedIds.value = [] }
async function deleteSelected() {
  if (!selectedIds.value.length) return
  if (!confirm(`删除选中的 ${selectedIds.value.length} 条对话？`)) return
  await convs.removeMany(selectedIds.value)
  exitSel()
}
function onDocClick() { closeCtx(); closeCvCtx(); closeMsgCtx(); convsCollapsed.value = true }
function onDocKey(e: KeyboardEvent) {
    if (e.key === 'Escape') { closeCtx(); closeCvCtx(); closeMsgCtx(); convsCollapsed.value = true; if (selMode.value) exitSel() }
}

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onDocKey)
  // 父级 Home 的 fetch 可能还没返回，这里先确保拉到列表，避免误判为空而重复新建
  if (!convs.list.length) await convs.fetch()
  if (!convs.current && convs.list.length) convs.current = convs.list[0].id
  if (!currentAgentId.value && agents.list.length) currentAgentId.value = agents.list[0].id
  if (!convs.list.length) await newConv('independent')
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onDocKey)
})
</script>

<template>
  <div class="right-panel glass">
    <!-- 画布列表 -->
    <div class="canvases-pane">
      <div class="section-title">
        <button class="collapse-btn" type="button"
                @click.stop="canvasesCollapsed = !canvasesCollapsed"
                :title="canvasesCollapsed ? '展开画布列表' : '折叠画布列表'">
          {{ canvasesCollapsed ? '▸' : '▾' }}
        </button>
        <span>画布列表</span>
        <span class="conv-count">{{ canvases.list.length }}</span>
        <span class="flex-spacer"></span>
        <button class="btn sm ghost" @click="canvases.create()" title="新建画布">+</button>
      </div>
      <div class="cv-list" v-show="!canvasesCollapsed">
        <div v-for="c in canvases.list" :key="c.id"
             :class="['cv-item', { active: canvas?.id === c.id, dragging: cvDragId === c.id, 'drop-before': cvOverId === c.id && cvOverPos === 'before' && cvDragId !== c.id, 'drop-after': cvOverId === c.id && cvOverPos === 'after' && cvDragId !== c.id }]"
             :draggable="renameId !== c.id"
             @dragstart="onCvDragStart($event, c)"
             @dragend="onCvDragEnd"
             @dragover="onCvDragOver($event, c)"
             @dragleave="onCvDragLeave(c)"
             @drop="onCvDrop($event, c)"
             @click="selectCanvas(c.id)"
             @contextmenu="onCanvasContext($event, c.id)">
          <template v-if="renameId === c.id">
            <input class="cv-rename" :value="renameVal"
                   @input="renameVal = ($event.target as HTMLInputElement).value"
                   @keydown.enter.prevent="commitRenameCanvas"
                   @keydown.esc="renameId = null"
                   @blur="commitRenameCanvas"
                   ref="r => setTimeout(() => (r as HTMLInputElement)?.focus(), 10)" />
          </template>
          <template v-else>
            <span class="cv-label" @dblclick.stop="startRenameCanvas(c.id, c.name)">🎨 {{ c.name }}</span>
            <span class="cv-count">{{ c.nodes?.length || 0 }}</span>
          </template>
          <span v-if="canvases.list.length > 1" class="del" @click.stop="deleteCanvas(c.id)">×</span>
        </div>
        <div v-if="!canvases.list.length" class="empty">还没有画布</div>
      </div>
    </div>
    <!-- 画布列表右键菜单 -->
    <Teleport to="body">
      <div v-if="cvCtxMenu.show" class="ctx-menu"
           :style="{ left: cvCtxMenu.x + 'px', top: cvCtxMenu.y + 'px' }"
           @click.stop>
        <button type="button" @click="renameId = cvCtxMenu.id; renameVal = canvases.list.find(c => c.id === cvCtxMenu.id)?.name || ''; closeCvCtx()">重命名</button>
        <button type="button" v-if="cvCtxMenu.id != null && canvases.list.length > 1"
                @click="deleteCanvas(cvCtxMenu.id)">删除此画布</button>
      </div>
    </Teleport>
    <div class="divider" ></div>
    <!-- 对话 -->
    <div class="chat-pane">
      <div class="chat-header">
        <div class="section-title conv-title">
          <button class="collapse-btn" type="button"
                  @click.stop="convsCollapsed = !convsCollapsed"
                  :title="convsCollapsed ? '展开对话记录（覆盖在画布上）' : '折叠对话记录'">
            {{ convsCollapsed ? '▾' : '▸' }}
          </button>
          <span class="conv-title-text">对话</span>
          <span class="conv-count">{{ convs.list.length }}</span>
          <span class="flex-spacer"></span>
          <div class="new-conv-bar">
            <button class="btn sm ghost" type="button" @click="newConv('independent')" title="新建独立对话（智能体单独工作）">＋ 独立</button>
            <button class="btn sm ghost purple" type="button" @click="newConv('linked')" title="新建集群对话（按画布编排协作）">＋ 集群</button>
          </div>
        </div>
      </div>
      <!-- 对话列表：固定定位的浮层，覆盖在画布区域，不压缩对话区高度 -->
      <Teleport to="body">
        <div v-if="!convsCollapsed" class="conv-float glass" :style="convFloatStyle" @click.stop>
          <div class="conv-float-head">
            <span class="conv-title-text">对话记录</span>
            <span class="conv-count">{{ convs.list.length }}</span>
            <button class="sort-btn" type="button"
                    @click.stop="convDesc = !convDesc"
                    :title="convDesc ? '当前：新在上。点击切换为顺序（旧在上）' : '当前：旧在上。点击切换为倒序（新在上）'">
              {{ convDesc ? '↓' : '↑' }}
            </button>
            <span class="flex-spacer"></span>
            <button class="collapse-btn" type="button" @click.stop="convsCollapsed = true" title="折叠">✕</button>
          </div>
          <!-- 多选操作条（仅选择模式出现） -->
          <div class="sel-bar" v-if="selMode" @click.stop>
            <span class="sel-count">已选 {{ selectedIds.length }}</span>
            <button class="btn sm ghost" type="button" @click="selectAll">全选</button>
            <button class="btn sm danger" type="button" :disabled="!selectedIds.length" @click="deleteSelected">删除</button>
            <button class="btn sm ghost" type="button" @click="exitSel">取消</button>
          </div>
          <div class="conv-list-wrap">
            <div class="conv-list">
              <div v-for="c in sortedConvs" :key="c.id"
                   :class="['conv-item', { active: conv?.id === c.id, checked: selectedIds.includes(c.id) }]"
                   @click="selectConv(c.id)"
                   @contextmenu="onConvContext($event, c.id)">
                <span v-if="selMode" class="chk">{{ selectedIds.includes(c.id) ? '☑' : '☐' }}</span>
                <span class="seq" title="序号：按活跃度从旧到新，最旧为 1（与排序方向无关）">{{ convSeqOf(c.id) }}</span>
                <span class="mode-tag" :class="c.mode">{{ c.mode === 'linked' ? '集群' : '独立' }}</span>
                <span class="conv-body">
                  <span class="conv-name">{{ convDisplayName(c) }}</span>
                  <span class="conv-time">{{ convTimeLabel(c) }}</span>
                </span>
                <span class="del" @click.stop="convs.remove(c.id)">×</span>
              </div>
              <div v-if="!sortedConvs.length" class="empty">暂无对话，点「＋」新建</div>
            </div>
          </div>
        </div>
      </Teleport>
      <!-- 对话列表右键菜单 -->
      <Teleport to="body">
        <div v-if="ctxMenu.show" class="ctx-menu"
             :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
             @click.stop>
          <button type="button" @click="enterSelMode">多选</button>
          <button type="button" @click="selectAll">全选</button>
          <button type="button" v-if="ctxMenu.id != null"
                  @click="convs.remove(ctxMenu.id); closeCtx()">删除此对话</button>
        </div>
      </Teleport>
      <!-- 消息右键菜单 + 文件操作历史还原 -->
      <Teleport to="body">
        <div v-if="msgCtxMenu"
             ref="msgMenuEl"
             class="msg-ctx-menu"
             :style="{ left: msgCtxMenu.x + 'px', top: msgCtxMenu.y + 'px' }"
             @click.stop @mousedown.stop>
          <div class="msg-ctx-item" @click.stop="openFileOpsFromMsg">
            <span class="msg-ctx-icon">📂</span>
            <span>查看此消息后的文件操作历史</span>
          </div>
        </div>
        <!-- 文件操作历史还原对话框 -->
        <div v-if="fileOpsDlg.open" class="modal-backdrop" @click.self="fileOpsDlg.open = false">
          <div class="modal file-ops-modal" @click.stop>
            <div class="modal-header">
              <span class="modal-title">🔄 文件操作历史与还原</span>
              <button class="btn sm ghost" @click="fileOpsDlg.open = false">✕</button>
            </div>
            <div class="modal-sub">
              来源消息：「{{ fileOpsDlg.msgContent || '（空）' }}」 之后智能体对文件的操作
            </div>
            <div class="modal-body">
              <div v-if="fileOpsDlg.loading" class="ops-loading">加载中…</div>
              <div v-else-if="fileOpsDlg.ops.length === 0" class="ops-empty">
                此消息之后没有文件修改操作 ✅
              </div>
              <template v-else>
                <div class="ops-toolbar">
                  <label class="cb">
                    <input type="checkbox"
                           :checked="checkedOps.size === fileOpsDlg.ops.length && fileOpsDlg.ops.length > 0"
                           :indeterminate="checkedOps.size > 0 && checkedOps.size < fileOpsDlg.ops.length"
                           @click.prevent="toggleCheckAll" @change="toggleCheckAll">
                    <span>全选 ({{ checkedOps.size }}/{{ fileOpsDlg.ops.length }})</span>
                  </label>
                  <button class="btn sm danger" :disabled="checkedOps.size === 0" @click="restoreSelected">
                    ↩️ 批量还原选中（倒序撤销）
                  </button>
                </div>
                <div class="ops-list">
                  <div v-for="(op, i) in fileOpsDlg.ops" :key="op.tool_call_id + '-' + i"
                       class="op-row" :class="{ restored: op.restored }">
                    <label class="cb op-cb" @click.stop>
                      <input type="checkbox"
                             :checked="checkedOps.has((op as any)._fullIdx)"
                             :disabled="!!op.restored"
                             @change="toggleCheckOp(i)">
                    </label>
                    <span class="op-ico">{{ op.before_exists ? '📝' : '🆕' }}</span>
                    <span class="op-type">{{ friendlyOp(op.op) }}</span>
                    <span class="op-agent">{{ op.agent_name || '?' }}</span>
                    <span class="op-path" :title="op.path">{{ op.path }}</span>
                    <span class="op-time">{{ op.ts?.slice(11, 19) }}</span>
                    <button v-if="!op.restored" class="btn sm ghost op-restore"
                            :disabled="checkedOps.size > 0"
                            @click="restoreOneOp(i)">↩️还原</button>
                    <span v-else class="op-restored-tag">已还原</span>
                  </div>
                </div>
              </template>
              <div v-if="restoreResult" class="restore-result">{{ restoreResult }}</div>
            </div>
          </div>
        </div>
      </Teleport>
      <div class="chat-box" ref="msgBox">
        <div v-if="!conv" class="no-conv">请新建对话</div>
        <!-- ============ 消息列表 ============ -->
        <template v-for="(m, idx) in visibleMsgs" :key="m.ts + '-' + idx">
          <!-- 跳过 tool_call / tool_result：它们被归属到前面最近的 assistant 消息下 -->
          <template v-if="m.role !== 'tool_call' && m.role !== 'tool_result'">

            <!-- ===== 用户消息（靠右） ===== -->
            <div v-if="m.role === 'user'" class="msg user" @contextmenu.prevent.stop="onMsgContextMenu($event, idx)">
              <div class="role" title="用户">👤</div>
              <div class="bubble markdown user-bubble" v-html="msgBubbleHtml(m)"></div>
              <span class="hint-right">右键查看文件操作历史</span>
            </div>

            <!-- ===== A2A 信封卡片 ===== -->
            <div v-else-if="m.role === 'a2a'" class="tool-card a2a">
              <div class="tc-header a2a-toggle" @click="toggleA2a(idx)">
                <span class="tc-ico">🤝</span>
                <span class="tc-name">{{ kindLabel(m.envelope?.kind) }}</span>
                <span class="a2a-route">{{ m.envelope?.from?.name }} → {{ m.envelope?.to?.name }}</span>
                <span class="tc-id">{{ m.envelope?.protocol }} · {{ m.envelope?.task?.state }}</span>
                <span class="tg-chev">{{ expandedA2a.has(idx) ? '▾' : '▸' }}</span>
              </div>
              <div v-if="expandedA2a.has(idx)" class="a2a-body">
                <div class="a2a-meta">
                  <span class="a2a-chip">{{ m.envelope?.task_id }}</span>
                  <span class="a2a-chip dim">{{ m.envelope?.message_id }}</span>
                </div>
                <template v-for="(p, pi) in m.envelope?.parts || []" :key="pi">
                  <div v-if="p.type === 'text'" class="a2a-text">{{ p.text }}</div>
                  <details v-else-if="p.type === 'artifact'" class="tc-body" open>
                    <summary>📎 {{ p.name }}（{{ p.mime_type }}）</summary>
                    <pre>{{ p.content }}</pre>
                  </details>
                </template>
              </div>
            </div>

            <!-- ===== 助手消息（靠左）===== -->
            <div v-else-if="m.role === 'assistant'"
                 class="msg assistant"
                 :class="{ empty: !trimMsgContent(m.content) && !isStreamingMsg(m) && !hasTools(idx), streaming: isStreamingMsg(m) }">
              <div class="col-wrap">
                <!-- 头像 + 名字行 -->
                <div class="head-row">
                  <div class="avatar">{{ (agentNameOf(m.agent_id) || '?').slice(0,1).toUpperCase() }}</div>
                  <div v-if="agentNameOf(m.agent_id)" class="who-assistant">{{ agentNameOf(m.agent_id) }}</div>
                </div>

                <!-- 文本泡泡：有内容走 markdown 渲染；有工具但没内容则省略泡泡；无内容无工具但在流式显示光标 -->
                <template v-if="trimMsgContent(m.content)">
                  <div class="bubble markdown" v-html="msgBubbleHtml(m)"></div>
                </template>
                <div v-else-if="isStreamingMsg(m) && !hasTools(idx)" class="bubble streaming-placeholder">
                  <span class="stream-cursor">▌</span>
                </div>

                <!-- 工具调用行：只在有真实 tool_call 时出现，汇总 pill + 可展开历史清单 -->
                <div v-if="hasTools(idx)" class="tool-row">
                  <div class="tr-summary"
                       :class="{ open: toolBarIsOpen(idx), running: latestToolInfo(idx)?.running }"
                       @click="toggleToolBar(idx)">
                    <span class="tr-icon" :class="{ spinning: latestToolInfo(idx)?.running }">{{ latestToolInfo(idx)?.running ? '⟳' : '✓' }}</span>
                    <span class="tr-summary-name">{{ latestToolInfo(idx)?.name }}</span>
                    <span class="tr-status">{{ latestToolInfo(idx)?.running ? '执行中' : '已完成' }}</span>
                    <span class="tr-count">共 {{ latestToolInfo(idx)?.total || 0 }} 个工具</span>
                    <span class="tr-chev">{{ toolBarIsOpen(idx) ? '▾' : '▸' }}</span>
                  </div>
                  <div v-if="toolBarIsOpen(idx)" class="tr-items">
                    <div v-for="t in toolsOfAssistant(idx)" :key="t.tool_call_id || ('tc-' + idx + '-' + t.name)" class="tr-item">
                      <div v-if="t.role === 'tool_call'" class="tr-pill"
                           :class="{ running: toolIsRunning(t), expanded: toolDetailIsOpen(idx, t.tool_call_id) }"
                           @click="toggleToolDetail(idx, t.tool_call_id)">
                        <span class="tr-icon" :class="{ spinning: toolIsRunning(t) }">{{ toolIsRunning(t) ? '⟳' : '✓' }}</span>
                        <span class="tr-name">{{ toolLabel(t.name) }}</span>
                        <span class="tr-toggle">{{ toolDetailIsOpen(idx, t.tool_call_id) ? '▾' : '▸' }}</span>
                      </div>
                      <div v-if="t.role === 'tool_call' && toolDetailIsOpen(idx, t.tool_call_id)" class="tr-detail">
                        <div class="tr-block"><div class="tr-block-title">参数</div><pre>{{ prettyArgs(t.args) }}</pre></div>
                        <template v-for="res in toolsOfAssistant(idx).filter(x => x.role === 'tool_result' && x.tool_call_id === t.tool_call_id)" :key="res.tool_call_id + '-r'">
                          <div class="tr-block"><div class="tr-block-title">📥 结果</div><pre>{{ prettyResult(res.result) }}</pre></div>
                        </template>
                        <div v-if="toolIsRunning(t)" class="tr-block running">
                          <span class="ti-spinner"></span><span>执行中…{{ speakingTool?.phase === 'args' ? '（参数流式中）' : '' }}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

          </template>
        </template>
      </div>
      <div class="chat-input">
        <div class="row1">
          <select class="input sm" v-model="currentAgentId">
            <option v-for="a in agents.list" :key="a.id" :value="a.id">🧠 {{ a.name }}</option>
          </select>
          <span class="cur-title">{{ conv ? convDisplayName(conv) : '— 未选择 —' }}</span>
        </div>
        <div class="row2">
          <textarea ref="taRef" class="input input-ta" v-model="input" placeholder="输入消息或指令..." @keydown.enter.exact.prevent="send" ></textarea>
          <button class="btn send" :disabled="convs.streaming" @click="send">发送</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.right-panel { display: flex; flex-direction: column; overflow: hidden; }
.canvases-pane { padding: 4px 6px; }
.cv-list { padding: 2px 4px; max-height: 180px; overflow-y: auto; }
.cv-item { padding: 6px 10px; border-radius: 8px; font-size: 12px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 6px; position: relative; }
.cv-item:hover { background: rgba(255,255,255,0.05); }
.cv-item.active { background: rgba(110,168,255,0.15); border: 1px solid rgba(110,168,255,0.25); }
.cv-item[draggable="true"] { cursor: grab; }
.cv-item[draggable="true"]:active { cursor: grabbing; }
.cv-item.dragging { opacity: .4; }
/* 拖拽落点指示线：上/下各一条高亮边 */
.cv-item.drop-before { box-shadow: 0 -2px 0 0 #6ea8ff, 0 1px 0 0 rgba(110,168,255,0.25); }
.cv-item.drop-after { box-shadow: 0 2px 0 0 #6ea8ff, 0 -1px 0 0 rgba(110,168,255,0.25); }
.cv-item .cv-label { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.cv-item .cv-label:hover::after { content: ' ✎'; opacity: .35; font-size: 11px; }
.cv-item .cv-count { font-size: 10px; opacity: .6; flex-shrink: 0; font-family: Consolas, monospace; }
.cv-item .del { opacity: 0; font-size: 14px; flex-shrink: 0; color: rgba(232,236,244,0.7); }
.cv-item:hover .del { opacity: .6; }
.cv-item .del:hover { opacity: 1; color: #ff9090; }
.cv-rename { width: 100%; font-size: 12px; padding: 2px 6px; border-radius: 4px; background: rgba(0,0,0,0.3); border: 1px solid rgba(110,168,255,0.4); color: #fff; }
.divider { height: 1px; background: rgba(255,255,255,0.08); margin: 4px 10px; }

.chat-pane { flex: 1; display: flex; flex-direction: column; min-height: 0; position: relative; }
.chat-header { flex-shrink: 0; display: flex; flex-direction: column; min-height: 0; border-bottom: 1px solid rgba(255,255,255,0.06); }
/* 对话列表浮层：贴右侧面板左边缘，覆盖在画布区域上，不压缩对话区高度 */
.conv-float {
  position: fixed;
  top: 58px;
  right: 310px;
  width: 240px;
  max-height: calc(100vh - 68px);
  z-index: 40;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.1);
  border-right: none;
  border-radius: 10px 0 0 10px;
  box-shadow: -8px 0 24px rgba(0,0,0,0.4);
}
.conv-float-head { flex-shrink: 0; display: flex; justify-content: flex-start; align-items: center; gap: 6px; padding: 8px 8px 4px; min-width: 0; }
.conv-title { justify-content: flex-start !important; gap: 6px; padding: 6px 8px 4px; flex-wrap: nowrap; min-width: 0; flex-shrink: 0; }
.conv-title-text { flex-shrink: 0; }
.flex-spacer { flex: 1; min-width: 0; }
.collapse-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; color: rgba(232,236,244,0.7); font-size: 11px; cursor: pointer; line-height: 1; }
.collapse-btn:hover { background: rgba(255,255,255,0.08); color: #fff; }
/* 排序方向切换按钮：↓ 新在上（倒序）／↑ 旧在上（顺序） */
.sort-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; color: #6ea8ff; font-size: 12px; cursor: pointer; line-height: 1; }
.sort-btn:hover { background: rgba(110,168,255,0.18); color: #dce8ff; }
.conv-count { font-size: 10px; opacity: .5; font-family: Consolas, monospace; flex-shrink: 0; }
.new-conv-bar { display: flex; gap: 4px; flex-shrink: 0; padding: 0; margin-left: auto; }
.btn.purple { background: rgba(180,114,255,0.15); border-color: rgba(180,114,255,0.35); color: #d4a8ff; }
.btn.purple:hover { background: rgba(180,114,255,0.25); }
.btn.danger { background: rgba(255,80,80,0.18); border-color: rgba(255,80,80,0.4); color: #ffb0b0; }
.btn.danger:hover { background: rgba(255,80,80,0.3); }
.btn.danger:disabled { opacity: .4; cursor: not-allowed; }
.sel-bar { flex-shrink: 0; display: flex; align-items: center; gap: 6px; padding: 4px 8px; background: rgba(255,80,80,0.08); border-top: 1px dashed rgba(255,80,80,0.25); border-bottom: 1px dashed rgba(255,80,80,0.25); }
.sel-count { font-size: 11px; color: #ffb0b0; flex-shrink: 0; }
.conv-list-wrap { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 4px 6px 6px; }
.conv-item { padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; display: flex; align-items: center; gap: 6px; margin-bottom: 2px; position: relative; }
/* 对话序号：纯文本小字，低调不抢行内其它标签的视觉 */
.conv-item .seq { flex-shrink: 0; min-width: 18px; text-align: right; color: rgba(232,236,244,0.45); font-family: Consolas, monospace; font-size: 11.5px; }
.conv-item.active .seq { color: #8ab8ff; }
.conv-item:hover .seq { color: rgba(232,236,244,0.75); }
.conv-item:hover { background: rgba(255,255,255,0.05); }
.conv-item.active { background: rgba(110,168,255,0.15); }
.conv-item.checked { background: rgba(255,80,80,0.12); border: 1px solid rgba(255,80,80,0.35); }
.conv-item .chk { flex-shrink: 0; font-size: 13px; opacity: .85; }
.conv-item .conv-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.conv-item .conv-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-item .conv-time { font-size: 10px; opacity: .45; font-family: Consolas, monospace; line-height: 1.2; }
.conv-item .del { opacity: 0; font-size: 14px; flex-shrink: 0; }
.conv-item:hover .del { opacity: .6; }
.mode-tag { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: rgba(255,255,255,0.1); flex-shrink: 0; }
.mode-tag.linked { background: rgba(180,114,255,0.2); color: #d4a8ff; }

.chat-box { flex: 1; overflow-y: auto; padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; }
.no-conv { text-align: center; opacity: .5; margin: 30px auto; font-size: 13px; }

/* ============ 消息容器 ============ */
.msg { display: flex; gap: 6px; max-width: 90%; }
/* 用户消息靠右（row-reverse：头像在右，泡泡在左） */
.msg.user { align-self: flex-end; flex-direction: row-reverse; }
.msg.user .bubble {
  background: linear-gradient(135deg, rgba(110,168,255,0.35), rgba(180,114,255,0.3));
  border-color: rgba(110,168,255,0.35);
  border-top-right-radius: 4px;
}
/* 助手消息靠左，纵向堆叠（头像行 + 泡泡 + 工具行） */
.msg.assistant { align-self: flex-start; flex-direction: column; }
.msg.assistant .bubble {
  background: rgba(255,255,255,0.06);
  border-top-left-radius: 4px;
}
.msg.assistant.streaming .bubble { border-color: rgba(110,168,255,0.4); box-shadow: 0 0 0 1px rgba(110,168,255,0.1); }

.col-wrap { display: flex; flex-direction: column; gap: 3px; }
.head-row { display: flex; align-items: center; gap: 6px; }
.avatar {
  width: 24px; height: 24px; border-radius: 7px;
  background: linear-gradient(135deg, #6ea8ff, #b472ff);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; flex-shrink: 0; color: #fff;
}
.who-assistant { font-size: 11.5px; color: rgba(232,236,244,0.75); opacity: .9; line-height: 1; }
.role { font-size: 16px; }

/* ============ 消息泡泡（基础） ============ */
.bubble {
  padding: 9px 13px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;        /* 默认保留换行 */
  border: 1px solid rgba(255,255,255,0.08);
  position: relative;
  transition: border-color .15s, box-shadow .15s;
}
/* 当 bubble 带 .markdown class 时，由全局样式接管排版 */
.bubble.markdown { white-space: normal; }

/* 用户消息泡泡额外样式 */
.bubble.user-bubble { color: rgba(232,236,244,0.95); }
.bubble.user-bubble.markdown a { color: #ffd088; }
.bubble.user-bubble.markdown code { background: rgba(0,0,0,0.25); color: #ffe0b0; }
.bubble.user-bubble.markdown blockquote { background: rgba(255,255,255,0.08); border-left-color: rgba(255,208,136,0.5); }

/* 流式占位泡泡（只有光标闪烁） */
.bubble.streaming-placeholder {
  background: rgba(255,255,255,0.04);
  border-style: dashed;
  border-color: rgba(110,168,255,0.3);
  padding: 6px 12px;
  min-height: 14px;
  line-height: 1;
}
.bubble.streaming-placeholder .stream-cursor { color: #6ea8ff; }

/* 空输出提示 */
.bubble.empty-bubble {
  background: transparent;
  border-style: dashed;
  border-color: rgba(255,255,255,0.12);
  color: rgba(232,236,244,0.4);
  font-style: italic;
  font-size: 11px;
  padding: 6px 12px;
}

.chat-input { padding: 8px 12px; border-top: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; gap: 6px; }
.row1 { display: flex; gap: 6px; align-items: center; }
.cur-title { font-size: 11px; opacity: .6; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row2 { display: flex; gap: 6px; align-items: flex-end; }
.sm { padding: 3px 8px; font-size: 11px; }
.input-ta { min-height: 44px; max-height: 100px; resize: vertical; font-family: inherit; }
.msg.assistant.empty { display: none; }
.tool-card { max-width: 95%; margin: 4px 0; align-self: flex-start; border-radius: 10px; padding: 8px 12px; border: 1px solid rgba(255,255,255,0.1); font-size: 12px; }
.tc-card { background: rgba(110,168,255,0.1); border-color: rgba(110,168,255,0.25); }
.tr-card { background: rgba(100,220,160,0.08); border-color: rgba(100,220,160,0.25); align-self: flex-start; }
.a2a-card { background: rgba(180,114,255,0.12); border-color: rgba(180,114,255,0.4); }
.a2a-card .tc-title { color: #d4a8ff; }
.a2a-route { font-weight: 600; color: #d4a8ff; text-transform: none; letter-spacing: 0; font-size: 12px; }
.a2a-state { margin-left: auto; font-family: Consolas, monospace; font-size: 10px; background: rgba(180,114,255,0.2); color: #d4a8ff; padding: 1px 6px; border-radius: 3px; text-transform: none; }
.a2a-text { font-size: 12px; line-height: 1.55; white-space: pre-wrap; margin-top: 6px; max-height: 180px; overflow-y: auto; }
.a2a-art summary { cursor: pointer; font-size: 11px; opacity: .7; margin-top: 6px; user-select: none; }
.a2a-art summary::-webkit-details-marker { display: none; }
.tc-head { display: flex; gap: 6px; align-items: center; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: rgba(232,236,244,0.6); }
.tc-icon { font-size: 14px; }
.tc-title { font-weight: 600; }
.tc-name { font-family: Consolas, monospace; font-size: 12px; background: rgba(0,0,0,0.3); padding: 2px 8px; border-radius: 4px; color: #a8c8ff; text-transform: none; letter-spacing: 0; margin-left: 4px; }
.tc-args { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.arg-chip { background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; font-size: 11px; display: inline-flex; gap: 2px; }
.ak { color: #d4a8ff; }
.av { color: #e8ecf4; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-error { color: #ff9090; background: rgba(255,80,80,0.15); padding: 1px 6px; border-radius: 3px; font-size: 10px; }
.tr-body { font-family: Consolas, monospace; font-size: 11px; background: rgba(0,0,0,0.2); padding: 6px 10px; border-radius: 6px; margin: 6px 0 0; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; color: #80e4b8; }

/* —— 助手消息下的工具行 —— */
.tool-row { margin-top: 5px; display: flex; flex-direction: column; gap: 3px; }
.tr-summary { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 9px; background: rgba(110,168,255,0.08); border: 1px solid rgba(110,168,255,0.22); cursor: pointer; font-size: 11px; color: rgba(232,236,244,0.72); user-select: none; width: fit-content; max-width: 100%; transition: background .15s; }
.tr-summary:hover { background: rgba(110,168,255,0.18); }
.tr-summary:hover .tr-chev { color: #a8c8ff; }
.tr-summary.open { background: rgba(110,168,255,0.22); border-color: rgba(110,168,255,0.45); color: rgba(232,236,244,0.95); }
.tr-summary.open .tr-chev { color: #a8c8ff; }
.tr-summary.running { border-color: rgba(110,168,255,0.55); background: rgba(110,168,255,0.12); color: #a8c8ff; }
.tr-summary.running .tr-icon { color: #a8c8ff; }
.tr-summary-name { font-weight: 500; color: rgba(232,236,244,0.92); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
.tr-status { font-size: 10px; opacity: .6; flex-shrink: 0; }
.tr-items { display: flex; flex-direction: column; gap: 3px; padding-left: 2px; }
.tr-ico { font-size: 11px; }
.tr-count { font-size: 11px; color: rgba(232,236,244,0.65); margin-left: auto; padding-right: 4px; }
.tr-label { opacity: .75; }
.tr-list { display: inline-flex; gap: 2px; flex-wrap: wrap; max-width: 220px; overflow: hidden; }
.tr-chip { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: rgba(0,0,0,0.28); color: rgba(232,236,244,0.7); white-space: nowrap; }
.tr-chip.more { background: rgba(110,168,255,0.2); color: #a8c8ff; }
.tr-chev { color: rgba(232,236,244,0.4); font-size: 10px; margin-left: 3px; transition: color .15s; }
.tr-item { display: flex; flex-direction: column; gap: 2px; }
.tr-pill { display: flex; align-items: center; gap: 5px; padding: 2px 7px; border-radius: 7px; background: rgba(0,0,0,0.18); border: 1px solid rgba(255,255,255,0.08); cursor: pointer; font-size: 10.5px; color: rgba(232,236,244,0.85); user-select: none; }
.tr-pill:hover { background: rgba(0,0,0,0.28); border-color: rgba(110,168,255,0.3); }
.tr-pill.running { border-color: rgba(110,168,255,0.55); background: rgba(110,168,255,0.1); color: #a8c8ff; }
.tr-pill.expanded { border-color: rgba(110,168,255,0.4); }
.tr-icon { width: 11px; text-align: center; font-size: 10px; transition: transform .3s; }
.tr-icon.spinning { display: inline-block; animation: tiSpin .8s linear infinite; }
@keyframes tiSpin { to { transform: rotate(360deg); } }
.tr-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tr-toggle { color: rgba(232,236,244,0.4); font-size: 10px; flex-shrink: 0; }
.tr-detail { padding-left: 12px; border-left: 2px solid rgba(110,168,255,0.2); margin-top: 2px; display: flex; flex-direction: column; gap: 3px; }
.tr-block { padding: 3px 7px; border-radius: 5px; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.07); font-size: 10.5px; }
.tr-block.running { display: inline-flex; align-items: center; gap: 5px; background: rgba(110,168,255,0.1); border-color: rgba(110,168,255,0.3); color: #a8c8ff; padding: 2px 7px; width: fit-content; }
.tr-block-title { font-size: 9.5px; opacity: .65; margin-bottom: 1px; font-family: Consolas, monospace; }
.tr-block pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 10px; max-height: 180px; overflow-y: auto; }
.ti-spinner { width: 9px; height: 9px; border-radius: 50%; border: 1.5px solid rgba(110,168,255,0.25); border-top-color: #6ea8ff; animation: tiSpin .8s linear infinite; flex-shrink: 0; display: inline-block; }
.a2a-toggle { cursor: pointer; user-select: none; }
.a2a-toggle:hover { opacity: 0.85; }
.send { height: 34px; padding: 0 16px; }
.empty { text-align: center; padding: 16px; font-size: 12px; opacity: .5; }

.hint-right { font-size: 10px; opacity: .35; align-self: flex-end; margin-left: 8px; user-select: none; pointer-events: none; }
</style>

<style>
/* —— 泡泡内 Markdown（v-html 注入的元素无法被 scoped 命中，必须用全局样式）—— */
.bubble.markdown { white-space: normal; word-break: break-word; overflow-wrap: anywhere; line-height: 1.6; }
.bubble.markdown > :first-child { margin-top: 0 !important; }
.bubble.markdown > :last-child { margin-bottom: 0 !important; }

/* —— 段落 —— */
.bubble.markdown p { margin: 0 0 6px; line-height: 1.65; }
.bubble.markdown p:last-child { margin-bottom: 0; }
.bubble.markdown br { display: block; }

/* —— 标题 —— */
.bubble.markdown h1, .bubble.markdown h2, .bubble.markdown h3,
.bubble.markdown h4, .bubble.markdown h5, .bubble.markdown h6 {
  margin: 10px 0 5px; line-height: 1.35; font-weight: 600; color: inherit;
}
.bubble.markdown h1 { font-size: 15px; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 3px; }
.bubble.markdown h2 { font-size: 14px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 2px; }
.bubble.markdown h3 { font-size: 13px; }
.bubble.markdown h4, .bubble.markdown h5, .bubble.markdown h6 { font-size: 12px; opacity: .9; }
.bubble.markdown h1:first-child, .bubble.markdown h2:first-child, .bubble.markdown h3:first-child { margin-top: 0; }

/* —— 列表 —— */
.bubble.markdown ul, .bubble.markdown ol { margin: 4px 0 6px; padding-left: 20px; }
.bubble.markdown li { margin: 2px 0; line-height: 1.55; }
.bubble.markdown li > ul, .bubble.markdown li > ol { margin: 2px 0; }

/* —— 行内代码 —— */
.bubble.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: 11.5px;
  background: rgba(0,0,0,0.35); padding: 1px 5px; border-radius: 4px; color: #ffb088;
  word-break: break-word;
}

/* —— 代码块 —— */
.bubble.markdown pre {
  background: rgba(0,0,0,0.42); padding: 10px 12px; border-radius: 6px;
  overflow-x: auto; margin: 6px 0; border: 1px solid rgba(255,255,255,0.1);
  max-height: 400px;
}
.bubble.markdown pre code { background: none; padding: 0; color: #e8ecf4; font-size: 11.5px; white-space: pre; word-break: normal; }

/* —— 引用块 —— */
.bubble.markdown blockquote {
  margin: 6px 0; padding: 4px 12px; border-left: 3px solid rgba(110,168,255,0.5);
  background: rgba(110,168,255,0.08); color: rgba(232,236,244,0.85); border-radius: 0 6px 6px 0;
}
.bubble.markdown blockquote p { margin: 0; }

/* —— 链接（核心：默认 target=_blank 由 JS 注入，这里管视觉）—— */
.bubble.markdown a {
  color: #a8c8ff;
  text-decoration: underline;
  text-underline-offset: 2px;
  transition: color .15s, background-color .15s;
  word-break: break-all;
  padding: 0 1px;
  border-radius: 3px;
}
.bubble.markdown a:hover {
  color: #d4a8ff;
  background: rgba(110,168,255,0.12);
}
.bubble.markdown a:active { color: #ffb088; }
/* 外链提示小箭头（通过 CSS ::after，避免污染 HTML） */
.bubble.markdown a[href^="http"]::after {
  content: '↗';
  display: inline-block;
  margin-left: 1px;
  font-size: 9px;
  opacity: .6;
  vertical-align: super;
  text-decoration: none;
}

/* —— 分割线 —— */
.bubble.markdown hr { border: none; border-top: 1px solid rgba(255,255,255,0.12); margin: 10px 0; }

/* —— 加粗 / 斜体 / 删除线 —— */
.bubble.markdown strong { font-weight: 700; color: inherit; }
.bubble.markdown em { font-style: italic; }
.bubble.markdown del, .bubble.markdown s { text-decoration: line-through; opacity: .55; }

/* —— 表格 —— */
.bubble.markdown table {
  border-collapse: collapse; border: 1px solid rgba(255,255,255,0.3);
  background: rgba(0,0,0,0.45); margin: 8px 0; font-size: 11.5px;
  width: max-content; max-width: 100%; border-radius: 6px; overflow: hidden;
}
.bubble.markdown th, .bubble.markdown td {
  border: 1px solid rgba(255,255,255,0.28); padding: 5px 9px;
  text-align: left; vertical-align: top; max-width: 350px;
}
.bubble.markdown th {
  background: rgba(255,255,255,0.12); font-weight: 600;
  border-bottom: 2px solid rgba(255,255,255,0.4); white-space: nowrap;
}
.bubble.markdown tbody tr:nth-child(even) td { background: rgba(255,255,255,0.04); }

/* —— 图片 —— */
.bubble.markdown img { max-width: 100%; border-radius: 6px; margin: 4px 0; border: 1px solid rgba(255,255,255,0.1); display: inline-block; }

/* —— GFM task list —— */
.bubble.markdown li input[type="checkbox"] { margin-right: 5px; vertical-align: middle; accent-color: #6ea8ff; }
.bubble.markdown li input[type="checkbox"] + * { vertical-align: middle; }

/* —— details/summary —— */
.bubble.markdown details { margin: 4px 0; border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; overflow: hidden; }
.bubble.markdown summary {
  cursor: pointer; padding: 6px 10px; background: rgba(0,0,0,0.2);
  font-size: 11.5px; color: rgba(232,236,244,0.8); user-select: none;
  list-style: none; outline: none;
}
.bubble.markdown summary::-webkit-details-marker { display: none; }
.bubble.markdown summary::before { content: '▶'; display: inline-block; margin-right: 6px; font-size: 9px; transition: transform .15s; }
.bubble.markdown details[open] > summary::before { content: '▼'; }
.bubble.markdown details > *:not(summary) { padding: 6px 10px; }

/* —— kbd —— */
.bubble.markdown kbd {
  font-family: Consolas, "Courier New", monospace; font-size: 11px;
  background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.25);
  border-radius: 4px; padding: 1px 5px; color: #e8ecf4;
}

/* —— 流式输出光标 —— */
.bubble.markdown .stream-cursor,
.stream-cursor {
  display: inline-block;
  margin-left: 2px;
  color: #6ea8ff;
  font-weight: 700;
  animation: cursorBlink 1s steps(2) infinite;
}
@keyframes cursorBlink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

.ctx-menu {
  position: fixed; z-index: 9999; min-width: 120px;
  background: rgba(28, 32, 44, 0.96); border: 1px solid rgba(255,255,255,0.14);
  border-radius: 8px; padding: 4px; box-shadow: 0 8px 24px rgba(0,0,0,0.45);
  display: flex; flex-direction: column; gap: 2px;
}
.ctx-menu button {
  appearance: none; border: 0; background: transparent; color: #e8ecf4;
  font-size: 12px; text-align: left; padding: 6px 10px; border-radius: 6px; cursor: pointer;
}
.ctx-menu button:hover { background: rgba(110,168,255,0.2); }

/* —— 消息右键菜单 —— */
.msg-ctx-menu {
  position: fixed;
  min-width: 240px;
  max-width: 320px;
  padding: 6px;
  z-index: 9999;
  background: rgba(28, 32, 44, 0.98);
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(0,0,0,0.55);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}
.msg-ctx-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-radius: 6px; font-size: 13px;
  cursor: pointer; transition: background .15s;
}
.msg-ctx-item:hover { background: rgba(110,168,255,0.2); }
.msg-ctx-icon { font-size: 14px; }

/* —— 文件操作历史还原对话框 —— */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1500;
}
.modal {
  background: rgba(16, 22, 40, 0.98);
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.modal.file-ops-modal {
  width: 900px;
  max-width: 94vw;
  max-height: 86vh;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.modal-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}
.modal-title { font-size: 15px; font-weight: 600; flex: 1; }
.modal-sub {
  padding: 8px 16px; font-size: 12px; color: rgba(232,236,244,0.6);
  background: rgba(0,0,0,0.15);
  border-bottom: 1px solid rgba(255,255,255,0.06);
  line-height: 1.5;
}
.modal-body { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }

/* 工具栏 */
.ops-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 6px 0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  margin-bottom: 4px;
}
.cb {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; cursor: pointer; user-select: none;
}
.cb input[type="checkbox"] { cursor: pointer; }

/* 操作列表 */
.ops-list { display: flex; flex-direction: column; gap: 4px; }
.op-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 8px;
  background: rgba(0,0,0,0.18);
  border: 1px solid rgba(255,255,255,0.06);
  font-size: 12px;
  transition: background .15s;
}
.op-row:hover { background: rgba(0,0,0,0.28); }
.op-row.restored { opacity: .55; }
.op-row .op-cb { flex-shrink: 0; }
.op-ico { font-size: 14px; flex-shrink: 0; }
.op-type { font-weight: 600; color: #a8c8ff; flex-shrink: 0; }
.op-agent {
  font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: rgba(180,114,255,0.15); color: #d4a8ff;
  flex-shrink: 0; max-width: 120px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.op-path {
  flex: 1; min-width: 0;
  font-family: Consolas, monospace; font-size: 11.5px;
  color: rgba(232,236,244,0.85);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.op-time {
  font-family: Consolas, monospace; font-size: 10.5px;
  color: rgba(232,236,244,0.45); flex-shrink: 0;
}
.op-restore { flex-shrink: 0; }
.op-restored-tag {
  font-size: 11px; padding: 2px 8px; border-radius: 4px;
  background: rgba(100,220,160,0.15); color: #80e4b8;
  flex-shrink: 0;
}

/* 空/加载状态 */
.ops-loading, .ops-empty {
  padding: 24px; text-align: center; font-size: 13px; opacity: .7;
}
.restore-result {
  padding: 8px 12px; border-radius: 8px; font-size: 12px;
  background: rgba(110,168,255,0.12);
  border: 1px solid rgba(110,168,255,0.3);
  color: rgba(232,236,244,0.9);
  word-break: break-word;
}

/* 按钮变体 */
.btn.sm.danger {
  background: rgba(255,80,80,0.15);
  color: #ff9090;
  border-color: rgba(255,80,80,0.25);
}
.btn.sm.danger:hover { background: rgba(255,80,80,0.28); }
.btn.sm.danger:disabled { opacity: .4; cursor: not-allowed; }
.btn.sm.ghost { padding: 3px 8px; font-size: 11px; background: transparent; }
.btn.sm.ghost:hover { background: rgba(255,255,255,0.08); }
</style>
