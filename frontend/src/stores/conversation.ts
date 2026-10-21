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

/**
 * 剥离 <continue> 续跑标记（大小写/标签内空白不敏感）。
 * 快通道 system prompt 会让模型在任务未结束时输出该标记，后端据此切换慢通道；
 * 标记本身任何情况下都不允许出现在对话界面或持久化内容里。
 * @see backend/core/agent_runtime.py `strip_continue`
 */
export function stripContinueTag(s: string | null | undefined): string {
  return (s || '').replace(/<\s*continue\s*>/gi, '')
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
  startedAt?: number    // 流式开始毫秒时间戳（前端思考计时用，不依赖后端 ts）
  relay?: boolean      // 接力链消息（聊天区过滤，画布气泡保留）
  agent_id?: number
  from_agent_id?: number  // a2a 信封额外字段：发送方 agent_id
  // purpose=share 的截图：data URL 直接落在 tool_result 上，前端渲染缩略图并落库到 AppData 对话记录
  image_data_url?: string
  image_content_type?: string
  purpose?: string
  // 历史兼容：上游 IMAGE: 流式行留下的本地文件路径数组，fetch 时映射到 image_refs
  images?: string[]
  /** 统一的图片引用列表（后端 I: 行 / 持久化 images 字段）。
   *  type="data_url" → src 是 data:image/...base64 直接渲染
   *  type="file" → src 是本地绝对路径，前端通过 /api/fs/image 加载 */
  image_refs?: Array<{ type: 'data_url' | 'file'; src: string }>
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
  'cluster.handoff': '🤝 A2A 移交',
}

// 前端 emoji 兜底：当后端 meta 没有 label（新插件/技能未配置 zh_Hans label）时，
// 按工具名关键词推断 emoji，保证工具行始终有视觉辨识度。
// 后端 COMMAND_CN_LABELS 已带精准 emoji，这里仅覆盖 plugins/skills 等缺 label 的场景。
const _EMOJI_FALLBACK: Array<[RegExp, string]> = [
  // 文件类
  [/^file\.read/, '📖'],
  [/^file\.write/, '✍️'],
  [/^file\.delete/, '🗑'],
  [/^file\.replace/, '🔄'],
  [/^file\.patch/, '🩹'],
  [/^file\.structure/, '📁'],
  [/^file\.search/, '🔍'],
  [/^file\.find/, '🔎'],
  [/^file\./, '📄'],
  // 命令类
  [/^cmd\.run/, '🚀'],
  [/^cmd\.bg_list/, '📋'],
  [/^cmd\.bg_stop/, '🛑'],
  [/^cmd\.bg_log/, '📜'],
  [/^cmd\./, '⚙️'],
  // cluster 类（按具体动作细分）
  [/^cluster\.list_agents/, '👥'],
  [/^cluster\.create_agent/, '🤖'],
  [/^cluster\.update_agent/, '✏️'],
  [/^cluster\.delete_agent/, '🗑'],
  [/^cluster\.list_canvases/, '🎨'],
  [/^cluster\.create_canvas/, '🌟'],
  [/^cluster\.delete_canvas/, '🗑'],
  [/^cluster\.get_canvas/, '👁'],
  [/^cluster\.add_node/, '📌'],
  [/^cluster\.remove_node/, '❌'],
  [/^cluster\.connect/, '🔗'],
  [/^cluster\.disconnect/, '✂️'],
  [/^cluster\.list_rules/, '📜'],
  [/^cluster\.create_rule/, '📋'],
  [/^cluster\.update_rule/, '✏️'],
  [/^cluster\.delete_rule/, '🗑'],
  [/^cluster\.send_message/, '📨'],
  [/^cluster\.handoff/, '🤝'],
  [/^cluster\./, '🎯'],
  // 工作区类
  [/^workspace\.search/, '🌐'],
  [/^workspace\./, '🌍'],
  // 通用关键词（plugins / skills）
  [/browser|navigate|web|page/i, '🌐'],
  [/screenshot|capture|photo/i, '📸'],
  [/http|request|fetch|api|url/i, '🌐'],
  [/search|find|grep|query/i, '🔍'],
  [/write|create|generate|make|build/i, '✍️'],
  [/delete|remove|erase/i, '🗑'],
  [/run|exec|cmd|shell|bash/i, '🚀'],
  [/read|view|get|fetch/i, '📖'],
  [/code|edit|patch|fix/i, '🛠'],
  [/image|img|picture|photo/i, '🖼'],
  [/video|media|movie/i, '🎬'],
  [/audio|voice|speech/i, '🎙'],
  [/math|calc|compute|number/i, '🧮'],
  [/data|json|csv|table|sql/i, '📊'],
  [/email|mail|message/i, '📨'],
  [/file|path|dir|folder/i, '📁'],
  [/skill/i, '🧠'],
  [/plugin/i, '🔌'],
  [/agent/i, '🤖'],
  [/canvas|board|node/i, '🎨'],
]
function _inferEmoji(name: string): string {
  for (const [re, e] of _EMOJI_FALLBACK) {
    if (re.test(name)) return e + ' '
  }
  return '🔧 '
}

// —— 清洗正则：无论 settings meta 是否已加载，始终生效 ——
const _PSEUDO_TAG_RE = /<\|[^|]*\|>/g
const _COMMENTARY_TAIL_RE = /commentary\s*:?/g
const _GARBAGE_PREFIX_RE = /^(functions?)\./i
const _TRAILING_JUNK_RE = /[^a-zA-Z0-9._-]+$/

/** 从脏工具名字符串中提取真正的工具名（两阶段策略，与后端 _clean_tool_name 同源）：
 *  阶段 1（始终生效）：正则剥掉已知垃圾模式——不依赖 settings 加载。
 *  阶段 2（settings 已加载时）：在清洗后字符串里查 known names 子串，精确捞回工具名。 */
function _sanitizeToolName(name: string | undefined): string {
  if (!name) return ''
  // —— 阶段 1：纯正则清洗（始终生效）——
  let s = name.replace(_PSEUDO_TAG_RE, '')
  s = s.replace(_GARBAGE_PREFIX_RE, '')
  s = s.replace(_COMMENTARY_TAIL_RE, '')
  s = s.replace(_TRAILING_JUNK_RE, '').trim().replace(/[:\-_]+$/g, '')

  // —— 阶段 2：有 known names → 子串匹配兜底 ——
  const known = new Set<string>()
  try {
    const st = useSettingsStore()
    for (const meta of [st.commandsMeta, st.pluginsMeta, st.skillsMeta]) {
      if (meta) {
        for (const k of Object.keys(meta)) known.add(k)
        for (const k of Object.keys(meta)) {
          if (k.includes('_')) known.add(k.replace(/_/g, '-'))
          if (k.includes('-')) known.add(k.replace(/-/g, '_'))
        }
      }
    }
  } catch { /* store 还没初始化 → 跳过阶段 2 */ }

  if (known.size) {
    if (known.has(s)) return s
    for (const candidate of [...known].sort((a, b) => b.length - a.length)) {
      if (candidate && s.includes(candidate)) return candidate
    }
  }
  return s || name
}

// 工具名 → 能力元数据（直接按清洗后的名字查；别名兼容已在 _sanitizeToolName 里处理）
function _resolveToolMeta(name: string): any {
  const s = useSettingsStore()
  return s.commandsMeta?.[name] || s.pluginsMeta?.[name] || s.skillsMeta?.[name]
}
/** 从原始（可能不完整的）JSON 字符串片段里正则提取 skill_name 值。 */
const _RAW_SKILL_NAME_RE = /"skill_name"\s*:\s*"((?:[^"\\]|\\.)*)"?/
function _extractSkillName(rawArgs: any): string {
  if (!rawArgs) return ''
  // 完整 args 对象
  if (typeof rawArgs === 'object') {
    const sn = rawArgs.skill_name
    if (typeof sn === 'string' && sn.trim()) return sn.trim()
    // 兜底 _raw 字段
    if (typeof rawArgs._raw === 'string' && rawArgs._raw) {
      const m = _RAW_SKILL_NAME_RE.exec(rawArgs._raw)
      if (m?.[1]) return m[1].trim()
    }
    return ''
  }
  // 纯字符串（流式 args 片段）
  if (typeof rawArgs === 'string') {
    const m = _RAW_SKILL_NAME_RE.exec(rawArgs)
    if (m?.[1]) return m[1].trim()
  }
  return ''
}

/** 工具名 → 中文能力标签（画布气泡 / 聊天工具行 / 列表卡片共用）
 *  优先用后端 meta.label（已带 emoji），没有时按工具名关键词推断 emoji + 原名。
 *  当 name 是 skill.invoke 且传了 args 时，显示具体被调用的技能名称。 */
export function toolLabel(name: string | undefined, args?: any): string {
  if (!name) return ''
  const clean = _sanitizeToolName(name)

  // skill.invoke → 显示具体技能名
  if (clean === 'skill.invoke') {
    const sn = _extractSkillName(args)
    if (sn) {
      try {
        const s = useSettingsStore()
        const sm = s.skillsMeta?.[sn]
        if (!sm) {
          // 尝试别名
          const alt = sn.includes('_') ? sn.replace(/_/g, '-') : sn.replace(/-/g, '_')
          if (alt && s.skillsMeta?.[alt]) return '🔧 ' + s.skillsMeta[alt].label
        }
        if (sm?.label) return '🔧 ' + sm.label
      } catch { /* store 未就绪 → 跳过 */ }
      // 没 meta → 直接显示 skill_name
      return '🔧 ' + sn
    }
    // 还没拿到 skill_name（如参数还在流式中）→ 先显示通用标签
    return '🔧 调用技能'
  }

  if (SPECIAL_TOOL_LABELS[clean]) return SPECIAL_TOOL_LABELS[clean]
  const meta = _resolveToolMeta(clean)
  if (meta?.label) {
    // 后端 label 已经带 emoji → 直接用
    if (/[\p{Emoji_Presentation}\p{Emoji}]/u.test(meta.label)) return meta.label
    // 后端 label 没带 emoji → 前端补一个
    return _inferEmoji(clean) + meta.label
  }
  // 完全没 meta → emoji + 原名（去前缀）
  const bare = clean.replace(/^(file|cmd|cluster|browser|http|tool|plugin|skill)\./i, '')
  return _inferEmoji(clean) + (bare || clean)
}

/** 工具名是否属于"文件操作"类工具（有 path 参数，需在工具行显示文件名） */
const FILE_TOOL_NAMES = new Set([
  'file.write', 'file.read', 'file.patch', 'file.replace', 'file.delete',
  'file.structure', 'file.search', 'file.move', 'file.copy',
])

/** 真正会改动磁盘文件的工具：执行完成后广播事件，让打开的工作区面板立即刷新 */
const MUTATING_FILE_TOOLS = new Set([
  'file.write', 'file.patch', 'file.replace', 'file.delete', 'file.move', 'file.copy',
])

/** 从原始（可能不完整的）JSON 字符串片段里正则提取文件路径值。
 *  用于参数流式阶段：JSON 还没闭合（如 '{"path": "/foo/ba'），
 *  正则允许引号未闭合，能拿到已流出的部分路径，让工具名旁立即显示文件名。
 *  捕获到的内容用 JSON.parse 解码（补闭合引号），正确处理 `\\` 等转义（Windows 路径 C:\Users）。 */
const _RAW_PATH_RE = /"(?:path|file_path|file)"\s*:\s*"((?:[^"\\]|\\.)*)"?/
function _extractPathFromRaw(rawArgs: string): string {
  if (!rawArgs) return ''
  const m = _RAW_PATH_RE.exec(rawArgs)
  if (!m || !m[1]) return ''
  const val = m[1]
  try { return JSON.parse('"' + val + '"').trim() }
  catch { return val.trim() }
}

/** 从工具参数中提取文件相关路径，用于在工具行名后显示"（文件名）"
 *  - file.* 类：取 path / file_path 参数
 *  - 其他工具：取 path / file / file_path 参数（如 browser_control screenshot 也有 file 字段）
 *  - 流式未完成阶段：从 args._raw（原始 JSON 片段）正则提取已流出的路径
 */
export function extractToolFileName(toolName: string | undefined, args: any): string {
  if (!toolName) return ''
  const clean = _sanitizeToolName(toolName)
  if (!clean) return ''
  if (args && typeof args === 'object') {
    // 文件操作类工具 → 优先 path
    if (FILE_TOOL_NAMES.has(clean)) {
      const p = args.path || args.file_path || args.file
      if (typeof p === 'string' && p.trim()) return p.trim()
    }
    // 通用兜底：很多工具返回 file 字段（如 screenshot），或者有 path 参数
    const f = args.file || args.file_path || args.path
    if (typeof f === 'string' && f.trim()) return f.trim()
    // 流式未完成阶段：JSON 还没闭合，从原始片段正则提取已流出的路径
    if (typeof args._raw === 'string' && args._raw) {
      const p = _extractPathFromRaw(args._raw)
      if (p) return p
    }
  }
  return ''
}

/** 循环窗口：按固定窗口大小 W 把长字符串切成"一行一行覆盖"的效果。
 *  - len ≤ W → 完整显示（第一行正在填）
 *  - len % W === 0 → 刚好填满一行，显示最近完整一行 slice(-W)
 *  - 其他 → 显示当前这轮（从刚清空后的位置继续填）
 *  应用场景：深度思考流式单行显示，填满即清空、重新从头填。 */
export function cyclicWindow(text: string, W = 70): string {
  const s = (text || '').replace(/\s+/g, ' ').trim()
  if (!s) return ''
  const len = s.length
  if (len <= W) return s
  const mod = len % W
  if (mod === 0) return s.slice(-W)          // 刚好整除 → 显示最后完整一行
  const completeRows = Math.floor(len / W)
  return s.slice(completeRows * W)             // 显示当前这轮（正在填）
}

/** 消息 → 工具归属分组：assistant 索引 → 其后连续的工具卡片（同 agent_id）。
 *  分几轮调用工具时，轮与轮之间只起"锚点/占位"作用的空 assistant 视为透明，
 *  跨过它把前后的工具并成一组 → 聊天区只渲染一条工具行，智能体输出正文时界面更整洁。
 *
 *  「透明」判定（key）：无正文即视为占位——只要 content 为空就是锚点，即使 reasoning 有值
 *  也算透明。原因：assistant 每次被唤起都会带 reasoning（思考流水），LLM 在多次工具调用之间
 *  会持续注入 reasoning 而不更新正文，此时若把 reasoning 也当作"有内容"就不透明，
 *  会把一整轮内连续的 tool_call 拆成多条工具行（视觉上就是"多个工具行没被折叠到一起"）。
 *  只有真正产出了正文（content 非空）才算这一轮的"答复"，才算天然轮次分隔。
 *  已认领的工具不会被后面的 assistant 重复分组（消费到哪条消息就从下一条继续）。 */
export function buildToolGroups(msgs: any[]): Map<number, any[]> {
  const map = new Map<number, any[]>()
  const isPlaceholderAssistant = (n: any) =>
    !!n && n.role === 'assistant' && !(n.content || '').trim()
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
      } else if (isPlaceholderAssistant(n) && n.agent_id === m.agent_id) {
        j++   // 轮次间的空占位 assistant（可能带 reasoning）→ 透明，继续向后合并
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
  streamingArgs?: string   // 参数流式中：累计到当前的原始 JSON 字符串片段
}

export const useConvStore = defineStore('conv', {
  state: () => ({
    list: [] as any[],
    current: null as number | null,
    // —— 流式状态：按会话隔离（同一智能体/画布在不同会话中互相独立、可同时运行）——
    // 下划线开头的字段是各会话的流式状态，通过下方 getter 以「当前会话」为视角暴露给
    // 消费者（聊天区/画布只看当前会话）。streamingConvIds 记录正在流式的会话集合。
    streamingConvIds: new Set<number>(),
    _ctrls: new Map<number, AbortController>(),                 // 会话 → 独立的 AbortController
    _speakingAgent: {} as Record<number, number | null>,        // 会话 → 当前发言智能体
    // 并行模式下可以同时有多个发言智能体（一个发送方同时唤起 N 个接收方）。
    // speakingAgents 是集合，画布节点靠它做 "speaking" 高亮；
    // speakingAgent 保留为"最近一个"，供聊天区等只关心单一发言者的逻辑兼容。
    _speakingAgents: {} as Record<number, Set<number>>,         // 会话 → 并行发言集合
    _relayPhase: new Set<number>(),                             // 已进入接力段的会话集合
    _handoffActive: new Set<number>(),                          // 正在 cluster.handoff / A2A 接力的会话集合
    _callEdges: {} as Record<number, { from: number; to: number; tcId: string }[]>, // 会话 → call 进行中的连线流动
    // 能力调用流式状态：会话 → (agent_id → { name, phase })
    //   phase='args' → 名称已确定、参数还在流（TS: 行）
    //   phase='exec' → 参数收齐（C: 行），工具执行中
    // R: 收到该 agent 最后一个结果后移除该条（见 toolPending）
    _toolCalls: {} as Record<number, Record<number, ToolCallPhase>>,
    // 会话 → 本批未返回结果的工具数（agent_id → 数量，C: +1，R: -1）
    // 一次性调用多个工具时，第一个 R: 不能立刻清掉 toolCalls，
    // 否则画布节点的"执行中"状态提前消失 → 渲染成空白气泡
    _toolPending: {} as Record<number, Record<number, number>>,
    selectedAgentId: null as number | null, // 节点💬直接对话选中的智能体
    focusChatAt: 0,                          // 触发对话区聚焦输入框
    running: false,                          // 画布运行中（等待/匹配触发）
    runCanvasId: null as number | null,
    _timer: null as ReturnType<typeof setInterval> | null,
    _fired: {} as Record<string, string>,    // cron 防重 {agentId: 'YYYY-MM-DDTHH:MM'}
    // —— 流式保存节流相关（2026-09 加入）——
    _flushTimers: {} as Record<number, ReturnType<typeof setTimeout>>, // 会话 → 节流 flush 定时器
    _lastFlushAt: 0,           // 上次 flush 时间戳（ms）
    _unloadBound: false,       // 是否已绑定 beforeunload
  }),
  getters: {
    /** 当前会话是否正在流式（发送/停止按钮据此切换：仅当前会话在流式才显示"停止"，
     *  新建会话不在集合中 → 返回 false → 按钮显示"发送"） */
    streaming(state): boolean {
      return state.current != null && state.streamingConvIds.has(state.current)
    },
    /** 当前会话的发言智能体（最近一个） */
    speakingAgent(state): number | null {
      return state.current != null ? (state._speakingAgent[state.current] ?? null) : null
    },
    /** 当前会话的并行发言集合 */
    speakingAgents(state): Set<number> {
      return state.current != null ? (state._speakingAgents[state.current] ?? new Set()) : new Set()
    },
    /** 当前会话是否正在 cluster.handoff / A2A 接力（连线流动） */
    handoffActive(state): boolean {
      return state.current != null && state._handoffActive.has(state.current)
    },
    /** 当前会话的 call 进行中连线流动 */
    callEdges(state): { from: number; to: number; tcId: string }[] {
      return state.current != null ? (state._callEdges[state.current] ?? []) : []
    },
    /** 当前会话的能力调用流式状态（agent_id → { name, phase }） */
    toolCalls(state): Record<number, ToolCallPhase> {
      return state.current != null ? (state._toolCalls[state.current] ?? {}) : {}
    },
    /** 当前会话的本批未返回工具数 */
    toolPending(state): Record<number, number> {
      return state.current != null ? (state._toolPending[state.current] ?? {}) : {}
    },
    /** 当前发言智能体正在调用的能力（用于气泡顶部状态条） */
    speakingToolCall(state): ToolCallPhase | null {
      const cur = state.current
      if (cur == null) return null
      const sa = state._speakingAgent[cur] ?? null
      if (sa == null) return null
      return (state._toolCalls[cur] ?? {})[sa] || null
    },
  },
  actions: {
    async fetch() {
      const list: any[] = await api.convs.list()
      // 清洗历史遗留的 streaming 标记：fetch 时不可能存在进行中的流，
      // 防止之前异常中断被持久化的 streaming:true 在重开后渲染成"空泡泡+永久闪烁光标"
      for (const c of list) {
        for (const m of c?.messages || []) {
          if (m && typeof m === 'object') {
            if (m.streaming) m.streaming = false
            // 后端持久化字段 images → 前端统一 image_refs
            if (!m.image_refs && Array.isArray(m.images) && m.images.length) {
              m.image_refs = m.images
            }
          }
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

    /** 删除单条消息（仅删除该条，保留其余消息）。
     *  若删除的是带 tool_calls 的 assistant 消息，后端会连带删除紧随的 tool 结果，
     *  避免留下孤立 tool 消息让模型"记得"刚才做过的事。
     *  前端可见消息可能经过 relay 过滤，需把可见索引映射回原始 messages 索引。 */
    async deleteMessage(visibleIdx: number): Promise<{ ok: boolean; msg?: string; removed?: number }> {
      const c = this.currentData()
      if (!c) return { ok: false, msg: '无当前会话' }
      const visibleMsgs = (c.messages || []).filter((m: any) => !m.relay)
      if (visibleIdx < 0 || visibleIdx >= visibleMsgs.length) return { ok: false, msg: '索引越界' }
      const target = visibleMsgs[visibleIdx]
      const rawIdx = (c.messages || []).indexOf(target)
      if (rawIdx < 0) return { ok: false, msg: '找不到原始消息' }
      try {
        const r = await api.convs.deleteMessage(c.id, rawIdx)
        await this.fetch()
        if (this.current != null) {
          const found = this.list.find(x => x.id === this.current)
          if (found) this.current = found.id
        }
        return { ok: true, removed: (r as any)?.removed ?? 1 }
      } catch (e: any) {
        return { ok: false, msg: e?.message || String(e) }
      }
    },

    /** 撤销：删除该用户消息及其后的所有对话，并还原之后的所有文件操作。
     *  仅对用户消息（role === 'user'）有效。 */
    async undoFromUserMessage(visibleIdx: number): Promise<{ ok: boolean; msg?: string; restoredOps?: number }> {
      const c = this.currentData()
      if (!c) return { ok: false, msg: '无当前会话' }
      const visibleMsgs = (c.messages || []).filter((m: any) => !m.relay)
      if (visibleIdx < 0 || visibleIdx >= visibleMsgs.length) return { ok: false, msg: '索引越界' }
      const target = visibleMsgs[visibleIdx]
      if (target.role !== 'user') return { ok: false, msg: '仅可对用户消息执行撤销' }
      const rawIdx = (c.messages || []).indexOf(target)
      if (rawIdx < 0) return { ok: false, msg: '找不到原始消息' }
      try {
        const r = await api.convs.undoFrom(c.id, rawIdx)
        await this.fetch()
        if (this.current != null) {
          const found = this.list.find(x => x.id === this.current)
          if (found) this.current = found.id
        }
        return { ok: true, restoredOps: r?.restored_ops ?? 0 }
      } catch (e: any) {
        return { ok: false, msg: e?.message || String(e) }
      }
    },

    // ========== 流式持久化（2026-09 加入） ==========
    /** 强制把指定会话（含 streaming:true 的 assistant）写回后端。同步/异步都能跑。
     *  未传 id 时回退到当前会话。流式状态按会话隔离后，send() 必须传入对应会话 id，
     *  否则用户中途切换会话后会写错对象。 */
    async flushCurrentNow(useSyncXHR = false, id: number | null = null): Promise<void> {
      const cid = id ?? this.current
      if (cid == null) return
      const c = this.list.find(x => x.id === cid)
      if (!c) return
      const payload = {
        messages: (c.messages || []).map((m: any) => {
          const o: any = { ...m }
          return o
        }),
      }
      try {
        if (useSyncXHR) {
          // beforeunload 里 fetch 会被浏览器取消，用同步 XHR 兜底（仅 Chrome/Edge 支持）
          const xhr = new XMLHttpRequest()
          xhr.open('PUT', `/api/conversations/${cid}`, false) // false = 同步
          xhr.setRequestHeader('Content-Type', 'application/json')
          try { xhr.send(JSON.stringify(payload)) } catch { /* ignore */ }
        } else {
          await api.convs.update(cid, payload)
        }
        this._lastFlushAt = Date.now()
      } catch {
        // 静默：流式过程中后端短暂不可达不应该炸掉整个流
      }
    },
    /** 节流 flush：调用后 delayMs 内只会真的发一次，避免每 chunk 都打一次 PUT。
     *  未传 id 时回退到当前会话。按会话维护独立的节流定时器，互不干扰。 */
    scheduleFlushCurrent(delayMs = 1200, id: number | null = null) {
      const cid = id ?? this.current
      if (cid == null) return
      const prev = this._flushTimers[cid]
      if (prev) clearTimeout(prev)
      this._flushTimers[cid] = setTimeout(() => {
        delete this._flushTimers[cid]
        void this.flushCurrentNow(false, cid)
      }, delayMs)
    },
    /** 取消挂起的节流 flush（流式结束、手动 fetch 等场景） */
    cancelScheduledFlush(id: number | null = null) {
      const cid = id ?? this.current
      if (cid == null) return
      const t = this._flushTimers[cid]
      if (t) { clearTimeout(t); delete this._flushTimers[cid] }
    },
    /** 绑定 window.beforeunload：关页/关标签时用同步 XHR 强制保存所有正在流式的会话。
     *  只绑一次，幂等。 */
    bindBeforeUnload() {
      if (this._unloadBound || typeof window === 'undefined') return
      this._unloadBound = true
      const handler = () => {
        // 关页时尽力保存所有正在流式的会话 + 当前会话（流式按会话隔离，可能不止一个）
        const ids = new Set<number>(this.streamingConvIds)
        if (this.current != null) ids.add(this.current)
        for (const cid of ids) {
          try { this.flushCurrentNow(true, cid) } catch { /* ignore */ }
        }
      }
      window.addEventListener('beforeunload', handler)
      // pagehide 也触发一次（iOS Safari 更倾向 pagehide）
      window.addEventListener('pagehide', handler)
    },

    async send(agentId: number, content: string) {
      if (this.current == null) await this.create()
      const c = this.currentData()
      const cid = this.current!  // 捕获本次发送的会话 id：整个流程（含 finally）都按此 id 操作，
      // 用户中途切换会话不会影响本流的收尾与状态复位

      // 文字触发：运行中且命中某智能体的 message 触发 → 替换目标
      if (this.running) {
        const hit = this._matchTextTrigger(content)
        if (hit) agentId = hit
      }

      // 链首不是接力段：复位本会话的接力/工具状态
      this._relayPhase.delete(cid)
      this._handoffActive.delete(cid)
      this._callEdges[cid] = []
      this._toolCalls[cid] = {}
      this._toolPending[cid] = {}

      // 绑定一次 beforeunload（幂等），关页/关标签会同步 XHR 强制保存
      this.bindBeforeUnload()

      // 构建当前轮消息数组（混合：用户 + 工具卡片 + 流式 assistant）
      const msgs: RichMsg[] = c.messages || []
      msgs.push({ role: 'user', content, ts: new Date().toISOString() })
      msgs.push({ role: 'assistant', content: '', streaming: true, ts: new Date().toISOString(), startedAt: Date.now(), agent_id: agentId })
      c.messages = msgs
      this.streamingConvIds.add(cid)
      this._speakingAgent[cid] = agentId
      this._speakingAgents[cid] = new Set([agentId])
      const ctrl = new AbortController()
      this._ctrls.set(cid, ctrl)

      // 先立刻落库一次（用户消息 + 空的 assistant 占位），中途 crash 也能保住用户输入
      await this.flushCurrentNow(false, cid)

      try {
        // 逐行解析新协议（signal 支持「停止」中止 fetch → 后端断连取消接力链）
        const r = await fetch('/api/conversations/' + cid + '/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: agentId, content }),
          signal: ctrl.signal,
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
            if (tickCount % 6 === 0) this.scheduleFlushCurrent(1200, cid)
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
        // 仅复位本会话的流式状态，不影响其他可能正在并行的会话
        this.streamingConvIds.delete(cid)
        this._ctrls.delete(cid)
        this._speakingAgent[cid] = null
        this._speakingAgents[cid] = new Set()
        this._relayPhase.delete(cid)
        this._handoffActive.delete(cid)
        this._callEdges[cid] = []
        this._toolCalls[cid] = {}
        this._toolPending[cid] = {}
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
        this.cancelScheduledFlush(cid)
        // 关键：先把最终状态（包含已停止标记）立刻写回后端，再 fetch 刷新
        await this.flushCurrentNow(false, cid)
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
      // 中止当前会话的流式（流式按会话隔离，停止运行只影响当前会话）
      const cid = this.current
      if (cid != null) {
        const ctrl = this._ctrls.get(cid)
        if (ctrl) { ctrl.abort(); this._ctrls.delete(cid) }
      }
    },
    /** 停止当前对话流：仅触发 abort，让 send() 的 catch/finally 走完整的收尾路径
     *  （写 "[已停止]" 标记 + flush + fetch + 复位 streaming 状态）。
     *  切勿在此处删除 _ctrls 或重置 streaming，否则 fetch 收尾流程会失效。 */
    stopSend() {
      const cid = this.current
      if (cid != null) {
        const ctrl = this._ctrls.get(cid)
        if (ctrl) ctrl.abort()
      }
    },
    async _cronTick() {
      if (!this.running || this.runCanvasId == null) return
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
        // 该画布的对话正在流式 → 跳过本轮（流式状态已按会话隔离，不阻塞其他会话）
        if (this.streamingConvIds.has(conv.id)) return
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

    /** 被消息目标开始输出（当前发言者是某条 call 连线的 to）→ 结束该条连线流动
     *  按 cid 操作对应会话的状态，避免并发流互相干扰。 */
    _callTargetOutputted(cid: number) {
      const edges = this._callEdges[cid]
      if (!edges || !edges.length) return
      const sp = this._speakingAgent[cid] ?? null
      if (sp == null) return
      const left = edges.filter(d => d.to !== sp)
      if (left.length !== edges.length) this._callEdges[cid] = left
    },

    /** 记录发言者。串行=替换（同时只有一个），并行=累加（多个接收方同时高亮）。
     *  并行模式下若只保留一个，画布上永远只亮最后一个接收方，
     *  表现成"一个发送方同时唤起 N 个智能体、却只有一个被唤醒"。
     *  按 cid 操作对应会话的状态，避免并发流互相干扰。 */
    _setSpeaker(cid: number, agentId: number) {
      this._speakingAgent[cid] = agentId
      const c = this.list.find(x => x.id === cid)
      const canvas = c?.canvas_id != null
        ? useCanvasStore().list.find(x => x.id === c.canvas_id)
        : null
      if (canvas?.execution_mode === 'parallel') {
        const next = new Set(this._speakingAgents[cid] ?? new Set())
        next.add(agentId)
        this._speakingAgents[cid] = next
      } else {
        this._speakingAgents[cid] = new Set([agentId])
      }
    },

    _handleLine(c: any, line: string) {
      const cid = c.id
      const msgs: RichMsg[] = c.messages || []
      const lastAssistant = msgs[msgs.length - 1]
      const curAgent = this._speakingAgent[cid] ?? null   // 当前发言者（工具调用的发起者）
      const relayPhase = this._relayPhase.has(cid)         // 本会话是否已进入接力段

      if (line === 'DONE') {
        this._handoffActive.delete(cid)
        this._callEdges[cid] = []
        this._toolCalls[cid] = {}
        this._toolPending[cid] = {}
        this._speakingAgents[cid] = new Set()
        return
      }

      if (line === 'RESET') {
        // 快通道检测到 <continue> → 后端清空本轮已流出的正文/思考，慢通道重新生成
        if (lastAssistant && lastAssistant.role === 'assistant' && lastAssistant.streaming) {
          lastAssistant.content = ''
          lastAssistant.reasoning = ''
        }
        return
      }

      if (line.startsWith('E:')) {
        // 后端错误行（LLM 调用失败/运行时异常/上游 HTTP 错误）→ 必须呈现给用户，
        // 否则表现为"无缘无故停止运行"
        // ⚠️ 后端 _esc 转义了 err_msg 内的 \n/\r/\\（防止多行异常详情破坏行协议），这里用 _unesc 还原
        const text = _unesc(line.slice(2))
        if (lastAssistant && lastAssistant.role === 'assistant' && lastAssistant.streaming) {
          lastAssistant.content = ((lastAssistant.content || '') + (lastAssistant.content ? '\n\n' : '') + text)
        } else {
          msgs.push({ role: 'assistant', content: text, streaming: false, ts: new Date().toISOString(), relay: relayPhase, agent_id: curAgent ?? undefined })
        }
        return
      }

      if (line.startsWith('RN:')) {
        // 深度思考流式增量 → 实时累加到当前流式 assistant 的 reasoning 字段
        // ⚠️ 后端 _esc 转义了 payload 内的 \n/\r/\\，这里用 _unesc 还原
        const text = stripContinueTag(_unesc(line.slice(3)))
        console.log('[RN] 收到思考增量 len=' + text.length + ', 预览=' + text.slice(0, 40).replace(/\n/g, '\\n'))
        if (lastAssistant && lastAssistant.role === 'assistant' && lastAssistant.streaming) {
          lastAssistant.reasoning = (lastAssistant.reasoning || '') + text
          console.log('[RN] 已累加到 lastAssistant，累计总长=' + (lastAssistant.reasoning || '').length)
        } else {
          // 思考先于正文到达（快通道常见）→ 先开一条流式 assistant 承接思考
          msgs.push({ role: 'assistant', content: '', reasoning: text, streaming: true, ts: new Date().toISOString(), startedAt: Date.now(), relay: relayPhase, agent_id: curAgent ?? undefined })
          console.log('[RN] 懒创建新的 assistant 来承接思考')
        }
      } else if (line.startsWith('T:')) {
        // 对方开始输出文字 → 结束指向它的 call 连线流动
        this._callTargetOutputted(cid)
        // 普通文字 → 追加到当前流式 assistant
        // ⚠️ 后端 _esc 转义了 payload 内的 \n/\r/\\，这里用 _unesc 还原
        const text = stripContinueTag(_unesc(line.slice(2)))
        if (lastAssistant && lastAssistant.role === 'assistant' && lastAssistant.streaming) {
          lastAssistant.content = (lastAssistant.content || '') + text
        } else {
          // 懒创建：正文真正到达时才开新的 assistant（不在 R: 后预建空泡泡，
          // 一次性多工具调用会产生 N-1 条空 assistant → 空消息泡泡）
          msgs.push({ role: 'assistant', content: text, streaming: true, ts: new Date().toISOString(), startedAt: Date.now(), relay: relayPhase, agent_id: curAgent ?? undefined })
        }
      } else if (line.startsWith('TS:')) {
        // 能力调用开始（名称已确定，参数仍在流式）→ 立即显示"正在调用: xxx"
        const name = line.slice(3).trim()
        if (name && curAgent != null) {
          this._toolCalls[cid] = { ...(this._toolCalls[cid] ?? {}), [curAgent]: { name, phase: 'args', streamingArgs: '' } }
        }
      } else if (line.startsWith('TD:')) {
        // 参数增量（accumulated 是累计到当前的原始 JSON 字符串）→ 实时更新
        const accumulated = _unesc(line.slice(3))
        if (curAgent != null && (this._toolCalls[cid] ?? {})[curAgent]) {
          const tc = (this._toolCalls[cid] ?? {})[curAgent]
          this._toolCalls[cid] = { ...(this._toolCalls[cid] ?? {}), [curAgent]: { ...tc, streamingArgs: accumulated } }
        } else {
          // 诊断日志：TD 到达但无法写入（未收到对应 TS 行 / speakingAgent 为空）
          console.warn('[TD] 无法写入 streamingArgs', { curAgent, hasToolCall: curAgent != null && !!(this._toolCalls[cid] ?? {})[curAgent], len: accumulated.length, preview: accumulated.slice(0, 60) })
        }
      } else if (line.startsWith('C:')) {
        // tool_call: id|name|{json args}
        const rest = line.slice(2)
        const sep1 = rest.indexOf('|'); const sep2 = rest.indexOf('|', sep1 + 1)
        const tcId = rest.slice(0, sep1), tcName = rest.slice(sep1 + 1, sep2), argsJson = rest.slice(sep2 + 1)
        let args: any = {}; try { args = JSON.parse(argsJson) } catch { args = { _raw: argsJson } }
        // 参数收齐 → 阶段从 args 切到 exec（工具开始执行），同时清空流式参数
        if (curAgent != null && tcName) {
          this._toolCalls[cid] = { ...(this._toolCalls[cid] ?? {}), [curAgent]: { name: tcName, phase: 'exec' } }
          this._toolPending[cid] = { ...(this._toolPending[cid] ?? {}), [curAgent]: ((this._toolPending[cid] ?? {})[curAgent] || 0) + 1 }
        }
        // 沟通工具 cluster.handoff / cluster.send_message → 连线流动
        if (tcName === 'cluster.handoff' || tcName === 'cluster.send_message') this._handoffActive.add(cid)
        // 被消息目标以工具调用开场 → 结束指向它的流动
        this._callTargetOutputted(cid)
        // call 调用 → 仅「调用方 → 对方」一条连线开始流动
        if (tcName === 'cluster.send_message') {
          const toId = resolveAgentId(args?.to_agent)
          if (toId != null && curAgent != null && toId !== curAgent) {
            if (!this._callEdges[cid]) this._callEdges[cid] = []
            this._callEdges[cid].push({ from: curAgent, to: toId, tcId })
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
          msgs.push({ role: 'assistant', content: '', ts: new Date().toISOString(), startedAt: Date.now(), relay: relayPhase, agent_id: curAgent ?? undefined })
        }
        msgs.push({ role: 'tool_call', content: '', name: tcName, tool_call_id: tcId, args, ts: new Date().toISOString(), relay: relayPhase, agent_id: curAgent ?? undefined })
      } else if (line.startsWith('R:')) {
        // tool_result: id|{json result}
        const rest = line.slice(2); const sep = rest.indexOf('|')
        const tcId = rest.slice(0, sep), resultJson = rest.slice(sep + 1)
        let result: any = {}; try { result = JSON.parse(resultJson) } catch { result = { _raw: resultJson } }
        // tool_result 的 agent_id = 对应 tool_call 的发起者
        const tc = msgs.find((m: RichMsg) => m.role === 'tool_call' && m.tool_call_id === tcId)
        const tcAgent = tc?.agent_id ?? curAgent ?? undefined
        msgs.push({ role: 'tool_result', content: '', tool_call_id: tcId, result, ts: new Date().toISOString(), relay: relayPhase, agent_id: tcAgent })
        // 文件被改动的工具执行完 → 广播事件，打开着的工作区面板立即刷新（不用等轮询间隔）
        if (tc?.name && MUTATING_FILE_TOOLS.has(_sanitizeToolName(tc.name))) {
          try { window.dispatchEvent(new CustomEvent('agentcluster:workspace-changed')) } catch { /* 非浏览器环境忽略 */ }
        }
        // 工具执行完成 → 只有本批所有工具都返回结果后才移除能力调用流式状态
        // （一次性多个工具时，第一个 R: 就清掉会让画布/聊天的"执行中"提前消失 → 空白气泡）
        let remain = -1
        if (tcAgent != null) {
          const leftP = { ...(this._toolPending[cid] ?? {}) }
          remain = (leftP[tcAgent] || 0) - 1
          if (remain > 0) leftP[tcAgent] = remain
          else delete leftP[tcAgent]
          this._toolPending[cid] = leftP
          if (remain <= 0 && (this._toolCalls[cid] ?? {})[tcAgent]) {
            const left = { ...(this._toolCalls[cid] ?? {}) }
            delete left[tcAgent]
            this._toolCalls[cid] = left
          }
        }
        // 集群编排改动画布/智能体 → 立即拉取，避免画布停留在旧内容
        if (tc?.name && CLUSTER_REFETCH.has(tc.name)) {
          void useCanvasStore().fetch()
          void useAgentStore().fetch()
        }
        // call 调用失败（未连线/找不到目标等）→ 立即结束该条连线流动
        if (tc?.name === 'cluster.send_message' && result?.error) {
          this._callEdges[cid] = (this._callEdges[cid] || []).filter(d => d.tcId !== tcId)
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
            msgs.push({ role: 'assistant', content: '', streaming: true, ts: new Date().toISOString(), startedAt: Date.now(), relay: relayPhase, agent_id: curAgent ?? undefined })
          }
        }
      } else if (line.startsWith('I:')) {
        // 图片引用（紧接 R: 行之后）：id|{json refs}
        const rest = line.slice(2); const sep = rest.indexOf('|')
        const tcId = rest.slice(0, sep), refsJson = rest.slice(sep + 1)
        let refs: any[] = []
        try { refs = JSON.parse(refsJson) } catch { refs = [] }
        if (!Array.isArray(refs)) refs = []
        // 找到刚刚的 tool_result 消息（按 tcId 匹配，取最后一条），把图片引用挂上去
        let target: RichMsg | undefined
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i]
          if (m.role === 'tool_result' && m.tool_call_id === tcId) { target = m; break }
        }
        if (target) target.image_refs = refs
      } else if (line.startsWith('IMAGE:')) {
        // 历史协议兼容：工具生成的图片本地路径 IMAGE:<file_path>
        // 转换为新格式 image_refs={type:'file', src:<path>} 挂到最近的 tool_result
        const imgPath = line.slice(6).trim()
        if (imgPath) {
          let targetIdx = -1
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === 'tool_result') { targetIdx = i; break }
          }
          if (targetIdx >= 0) {
            const t = msgs[targetIdx] as RichMsg
            if (!t.image_refs) t.image_refs = []
            if (!t.image_refs.find(r => r.src === imgPath)) {
              t.image_refs.push({ type: 'file', src: imgPath })
            }
          }
        }
      } else if (line.startsWith('F:')) {
        // 最终完整文本（通常前端不需要，已经有流式拼接了）
        this._callTargetOutputted(cid)
      } else if (line.startsWith('SPEAK:')) {
        // 切换发言者：结束当前流式消息（空泡泡直接丢弃），后续进入接力段（聊天区过滤）
        const id = parseInt(line.slice(6), 10)
        _closeTailAssistant(msgs)
        if (!Number.isNaN(id)) this._setSpeaker(cid, id)
        this._relayPhase.add(cid)
        this._handoffActive.add(cid)
      } else if (line.startsWith('A2A:')) {
        // A2A 消息信封：关闭当前流式消息 → 插入信封卡片（正文由后续 T: 懒创建）
        let env: any = null
        try { env = JSON.parse(line.slice(4)) } catch { env = { _raw: line.slice(4) } }
        this._handoffActive.add(cid)
        _closeTailAssistant(msgs)
        // 信封带双 agent_id：from.agent_id=发送方（画布源节点也应显示这条"我发出的"消息）
        //                 to.agent_id=接收方（画布目标节点应显示这条"我收到的"消息）
        msgs.push({
          role: 'a2a', envelope: env, ts: new Date().toISOString(),
          agent_id: env?.to?.agent_id ?? curAgent ?? undefined,   // 主 agent_id 存接收方，给接收方节点显示
          from_agent_id: env?.from?.agent_id ?? undefined,       // 发送方，给源节点也显示"我发的"
        } as any)
      }
      c.messages = msgs
    }
  }
})
