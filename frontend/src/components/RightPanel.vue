
<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, onActivated, onDeactivated, watch, nextTick } from 'vue'
import { useConvStore, useAgentStore, useCanvasStore, useModelStore, useSettingsStore, useLayoutStore } from '@/stores'
import { convDisplayName, convTimeLabel, sortConvsByActivity, buildToolGroups, toolLabel, extractToolFileName, cyclicWindow, stripContinueTag } from '@/stores/conversation'
import { renderChatMarkdown } from '@/utils/markdown'
import { getAvatarGradient, getAvatarInitial } from '@/utils/avatar'
import { api } from '@/api'
import FileViewer, { type ViewerFile } from './FileViewer.vue'

/** 去掉消息首尾纯空行（保留中间换行）—— 用于修复气泡顶部空白；顺带剥离 <continue> 续跑标记 */
function trimMsgContent(s: string | undefined): string {
  if (!s) return ''
  return stripContinueTag(s).replace(/^\s*\n+/, '').replace(/\n+\s*$/, '')
}
/** 消息泡泡 Markdown 渲染（用户/助手统一走这里，含流式光标 + 聊天附件后处理） */
function msgBubbleHtml(m: any): string {
  const html = renderChatMarkdown(trimMsgContent(m.content))
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

// —— 思考用时计时器：流式期间每秒 tick 驱动实时用时；停止后用缓存 end 算最终用时 ——
const nowTick = ref(Date.now())
let _elapsedTimer: ReturnType<typeof setInterval> | null = null
watch(() => convs.streaming, (v) => {
  if (v) {
    if (!_elapsedTimer) _elapsedTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
  } else {
    if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null }
    nowTick.value = Date.now()
  }
}, { immediate: true })
onBeforeUnmount(() => { if (_elapsedTimer) clearInterval(_elapsedTimer) })
function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000)
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  return m + 'm' + (s % 60) + 's'
}
/** 计算本轮智能体运行用时：
 *  - 流式中：nowTick - 最早 startedAt（实时计时）
 *  - 已结束：最大 endedAt - 最早 startedAt（从消息本身取结束时间，组件重挂载也不会漂移）
 *  - 两者都没有足够数据时返回空串 */
function turnElapsedLabel(turn: any): string {
  let start = Infinity
  let end = -Infinity
  for (const ch of turn?.children || []) {
    if (ch.kind !== 'assistant') continue
    const msg: any = ch.msg
    const s = msg?.startedAt
    const e = msg?.endedAt
    if (typeof s === 'number' && s < start) start = s
    if (typeof e === 'number' && e > end) end = e
  }
  if (start === Infinity) return ''
  const live = isTurnActive(turn.start)
  let elapsed: number
  if (live) {
    elapsed = nowTick.value - start
  } else if (end > 0) {
    elapsed = end - start
  } else {
    return ''  // 结束了但没有 endedAt（极老的历史消息或 edge case），不显示错误的 Date.now() 推算
  }
  if (elapsed < 1000) return ''
  return formatElapsed(elapsed)
}

const input = ref('')
const currentAgentId = ref<number | null>(null)
const msgBox = ref<HTMLElement | null>(null)
const convListEl = ref<HTMLElement | null>(null)
const taRef = ref<HTMLTextAreaElement | null>(null)
// 语音输入（同声传译）
const isRecording = ref(false)
let mediaStream: MediaStream | null = null
let mediaRecorder: MediaRecorder | null = null
let audioChunks: Blob[] = []
let recordTimer: number | null = null
// 工作区拖拽到输入框：填入文件/文件夹绝对路径
const chatDropHover = ref(false)
let chatDragDepth = 0

// —— 附件（从电脑拖入的文件）——
interface ChatAttachment {
  id: string
  name: string
  path: string
  size: number
  content_type: string
  ext: string
  /** 图片缩略图的 objectURL（仅图片类型有） */
  previewUrl?: string
  /** 上传中/失败状态 */
  status: 'uploading' | 'ready' | 'error'
  errorMsg?: string
}
const attachments = ref<ChatAttachment[]>([])

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'])
function isImageFile(name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  return IMAGE_EXTS.has(ext)
}
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}
function fileIconOf(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  const map: Record<string, string> = {
    pdf: '📄', doc: '📝', docx: '📝', txt: '📃', md: '📝',
    xls: '📊', xlsx: '📊', csv: '📊', json: '📋', yaml: '📋', yml: '📋',
    py: '🐍', js: '📜', ts: '📜', vue: '📜', html: '🌐', css: '🎨',
    zip: '🗜', rar: '🗜', '7z': '🗜',
    mp3: '🎵', wav: '🎵', mp4: '🎬', mov: '🎬',
  }
  return map[ext] || '📎'
}

/**
 * 从 DragEvent 的 dataTransfer.items 里用 webkitGetAsEntry 递归收集所有 File 对象。
 * Chrome / Edge / Safari 支持；Firefox 回退用 dataTransfer.files。
 *
 * 关键点：
 *  - 拖**文件夹**时 dt.files 可能返回占位符（0 size / 目录名本身），必须用 webkitGetAsEntry 才能拿到里面的文件
 *  - 拖**多个文件**时 dt.files 已经是完整的 FileList，但 webkitGetAsEntry 也能逐个取
 *  - 过滤系统垃圾文件：.DS_Store / Thumbs.db / desktop.ini / 隐藏文件
 *  - 返回 { files, relativePaths }：relativePaths[i] 是 files[i] 相对于最顶层根的路径（用 / 分隔）
 */
async function _extractFilesFromDataTransfer(e: DragEvent): Promise<{ files: File[]; relativePaths: string[] }> {
  const dt = e.dataTransfer
  if (!dt) return { files: [], relativePaths: [] }

  const SKIP_NAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini', '$RECYCLE.BIN', 'System Volume Information'])

  // 递归采集函数
  async function collect(entry: any, basePath: string, acc: { files: File[]; paths: string[] }) {
    // 跳过以 . 开头的隐藏文件/目录 + 系统垃圾名
    const name = entry.name || ''
    if (!name || SKIP_NAMES.has(name) || name.startsWith('.')) return

    if (entry.isFile) {
      const file: File = await new Promise((resolve, reject) => {
        try { entry.file(resolve, reject) } catch (err) { reject(err) }
      })
      // 过滤零字节占位符（Chrome 对某些拖入的非文件对象会返回空 file）
      if (!file || file.size === 0) return
      acc.files.push(file)
      acc.paths.push(basePath ? `${basePath}/${name}` : name)
    } else if (entry.isDirectory) {
      const reader = entry.createReader()
      // readEntries 需要循环直到返回空数组（Chrome 单次最多返回 100 条）
      const readBatch = (): Promise<any[]> =>
        new Promise((resolve, reject) => {
          try { reader.readEntries(resolve, reject) } catch (err) { reject(err) }
        })
      let batch: any[]
      while ((batch = await readBatch()).length > 0) {
        for (const child of batch) {
          await collect(child, basePath ? `${basePath}/${name}` : name, acc)
        }
      }
    }
  }

  const acc: { files: File[]; paths: string[] } = { files: [], paths: [] }

  // 优先：dataTransfer.items（含目录信息）
  if (dt.items && dt.items.length > 0) {
    // 判断是否有 webkitGetAsEntry（Chrome/Edge/Safari）
    const hasEntryApi = Array.from(dt.items).some(it => (it as any).webkitGetAsEntry)
    if (hasEntryApi) {
      for (const item of Array.from(dt.items)) {
        const entry = (item as any).webkitGetAsEntry?.()
        if (!entry) continue
        await collect(entry, '', acc)
      }
      if (acc.files.length > 0) return { files: acc.files, relativePaths: acc.paths }
    }
  }

  // 回退：dataTransfer.files（Firefox / 单文件拖入时 items 可能为空但 files 有值）
  if (dt.files && dt.files.length > 0) {
    for (const f of Array.from(dt.files)) {
      if (!f || f.size === 0) continue
      const name = f.name || ''
      if (!SKIP_NAMES.has(name) && !name.startsWith('.')) {
        acc.files.push(f)
        acc.paths.push(name)
      }
    }
  }
  return { files: acc.files, relativePaths: acc.paths }
}

/** 上传一组 File 对象到后端，返回附件信息数组 */
async function uploadFiles(files: FileList | File[], relativePaths?: string[]): Promise<ChatAttachment[]> {
  const list = Array.from(files)
  const relPaths = (relativePaths && relativePaths.length === list.length) ? relativePaths : list.map(f => f.name)

  // 为每个文件先生成临时占位（带本地预览），再并行上传
  // 用 name+size+lastModified 三元组作为上传后匹配键（比 name+size 更稳）
  const atts: ChatAttachment[] = list.map((f, i) => ({
    id: crypto.randomUUID(),
    name: relPaths[i] || f.name,  // 有相对路径就完整显示（例如 myfolder/report.docx）
    path: '',
    size: f.size,
    content_type: f.type || '',
    ext: f.name.split('.').pop()?.toLowerCase() || '',
    previewUrl: isImageFile(f.name) ? URL.createObjectURL(f) : undefined,
    status: 'uploading',
    // 内部匹配用的原始签名（不进 template，只在运行时用来匹配后端返回）
    _sign: `${f.name}::${f.size}::${f.lastModified}::${i}`,
  } as ChatAttachment & { _sign: string }))
  attachments.value.push(...atts)

  // 执行上传（一次 POST 全量）
  const form = new FormData()
  list.forEach((f, i) => {
    // 把原始文件名保留（后端返回的 name 字段），相对路径通过单独 JSON 字段传
    form.append('files', f, f.name)
  })
  form.append('relative_paths', JSON.stringify(relPaths))
  try {
    const r = await fetch('/api/fs/upload', { method: 'POST', body: form })
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: '上传失败' }))
      throw new Error(err.detail || `HTTP ${r.status}`)
    }
    const data = await r.json()
    const uploaded: Array<{ name: string; path: string; size: number; content_type: string; ext: string; relative_path?: string; upload_index?: number }> = data.files || []
    // 匹配：优先按 upload_index；否则按 name+size+lastModified
    uploaded.forEach(u => {
      const idx = u.upload_index
      let match = idx != null ? atts[idx] : undefined
      if (!match) {
        // 退化匹配：name + size（如果有多个同签名的，只 match 第一个）
        match = atts.find(a => a.name === u.name && a.size === u.size)
      }
      if (match) {
        match.path = u.path
        match.status = 'ready'
        // 如果后端返回了 relative_path，把附件卡片的 name 显示成带路径的
        if (u.relative_path) match.name = u.relative_path
      }
    })
    atts.forEach(a => {
      if (a.status === 'uploading') { a.status = 'error'; a.errorMsg = '上传失败' }
    })
  } catch (e: any) {
    atts.forEach(a => { a.status = 'error'; a.errorMsg = e?.message || String(e) })
  }
  return atts
}

function removeAttachment(id: string) {
  const idx = attachments.value.findIndex(a => a.id === id)
  if (idx < 0) return
  const att = attachments.value[idx]
  if (att.previewUrl) URL.revokeObjectURL(att.previewUrl)
  attachments.value.splice(idx, 1)
}

/** 把附件列表格式化为文本，合并到发送内容里（markdown + 自定义链接格式供后处理） */
function attachmentsToText(): string {
  if (!attachments.value.length) return ''
  const lines: string[] = []
  for (const a of attachments.value) {
    if (a.status !== 'ready' || !a.path) continue
    // 所有 path/name 参数做 URL 编码（Windows 路径里的 \ / : 中文等安全）
    const enc = encodeURIComponent
    if (isImageFile(a.name)) {
      // 图片：markdown 图片语法 → 后处理成带点击事件的缩略图
      lines.push(`![${a.name}](/api/fs/image?path=${enc(a.path)})`)
    } else {
      // 其他文件：markdown 链接 → 后处理替换为可点击的文件卡片
      // href 用 /file-viewer?... 前缀，data 层解码
      lines.push(`[📎 ${a.name} · ${formatFileSize(a.size)}](/file-viewer?path=${enc(a.path)}&name=${enc(a.name)}&size=${a.size})`)
    }
  }
  return lines.join('\n\n')
}

/** 从 dataTransfer 取路径：优先工作区自定义类型，其次 file:// 列表（资源管理器拖入），最后纯文本 */
function dropPathFromEvent(e: DragEvent): string {
  const dt = e.dataTransfer
  if (!dt) return ''
  const own = dt.getData('application/x-agentcluster-path')
  if (own) return own
  const uri = dt.getData('text/uri-list')
  if (uri) {
    const line = uri.split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#'))[0] || ''
    if (line.toLowerCase().startsWith('file://')) {
      try {
        // file:///F:/a/b.txt → /F:/a/b.txt → 去掉开头斜杠（Windows 盘符路径）
        return decodeURIComponent(new URL(line).pathname).replace(/^\//, '')
      } catch { /* 解析失败走下面 */ }
    }
  }
  const plain = (dt.getData('text/plain') || '').trim()
  // 纯文本只有像单行路径时才采用，避免把整段聊天文本塞进输入框
  if (plain && !plain.includes('\n') && plain.length < 512 && /[\\/]/.test(plain)) return plain
  return ''
}
/** 把文本插入到输入框光标处（前后自动补空格），并把光标移到插入内容之后 */
function insertAtCursor(text: string) {
  const ta = taRef.value
  if (!ta) {
    input.value = input.value ? input.value.replace(/\s*$/, ' ') + text : text
    return
  }
  const s = ta.selectionStart ?? input.value.length
  const en = ta.selectionEnd ?? s
  const before = input.value.slice(0, s)
  const after = input.value.slice(en)
  const padB = before && !/\s$/.test(before) ? ' ' : ''
  const padA = after && !/^\s/.test(after) ? ' ' : ''
  input.value = before + padB + text + padA + after
  nextTick(() => {
    ta.focus()
    const pos = (before + padB + text).length
    ta.setSelectionRange(pos, pos)
  })
}
function onChatDragEnter() {
  chatDragDepth++
  chatDropHover.value = true
}
function onChatDragLeave() {
  chatDragDepth = Math.max(0, chatDragDepth - 1)
  if (!chatDragDepth) chatDropHover.value = false
}
/** 从 DragEvent 里快速提取本地路径：优先 text/uri-list，其次 text/plain（单行路径） */
function _extractDropPaths(e: DragEvent): string[] {
  const dt = e.dataTransfer
  if (!dt) return []
  const out: string[] = []

  // 1) text/uri-list：Chrome/Edge 一定会给（file:///C:/xxx/yyy.docx 格式）
  const uriList = dt.getData('text/uri-list')
  if (uriList) {
    for (const line of uriList.split('\n')) {
      const s = line.trim()
      if (!s || s.startsWith('#')) continue
      if (!s.toLowerCase().startsWith('file://')) continue
      try {
        // file:///F:/path/to/file → /F:/path/to/file → F:/path/to/file
        let decoded = decodeURIComponent(new URL(s).pathname)
        decoded = decoded.replace(/^\//, '')
        if (decoded) out.push(decoded)
      } catch { /* ignore */ }
    }
  }

  // 2) text/plain：单行 + 含路径分隔符（避免把聊天文本塞进输入框）
  if (out.length === 0) {
    const plain = (dt.getData('text/plain') || '').trim()
    if (plain && !plain.includes('\n') && plain.length < 512 && /[\\/]/.test(plain)) {
      out.push(plain)
    }
  }
  return out
}

/** 判断路径指向文件还是目录（后缀启发式：有扩展名大概率文件） */
function _looksLikeFile(p: string): boolean {
  const base = p.replace(/\\/g, '/').split('/').pop() || ''
  return /\.[a-zA-Z0-9]{1,5}$/.test(base)
}

/**
 * 把一组候选路径发给后端做"路径直通验证"（毫秒级，不读任何文件内容）
 * 返回 [{ path, ok, name, size, ext, skipped?, skip_reason? }]
 * 其中 ok=true 表示可以直接用（via="path"），ok=false 表示后端拒绝（通常是不在 ROOT 内）
 */
async function _verifyPaths(paths: string[]): Promise<any[]> {
  if (!paths.length) return []
  const form = new FormData()
  form.append('absolute_paths', JSON.stringify(paths))
  try {
    const r = await fetch('/api/fs/upload', { method: 'POST', body: form })
    if (!r.ok) return []
    const data = await r.json()
    return data.files || []
  } catch {
    return []
  }
}

async function onChatDrop(e: DragEvent) {
  chatDragDepth = 0
  chatDropHover.value = false
  e.preventDefault()
  e.stopPropagation()

  // —— 优先级 1：本地路径直通（毫秒级，零上传）——
  //    从 text/uri-list 拿路径是最快的，Chrome/Edge 100% 有；
  //    后端 _safe_resolve 只放 ROOT 内的路径通过，ROOT 外的会返回 skipped=true
  const dropPaths = _extractDropPaths(e)
  if (dropPaths.length > 0) {
    // 分类：文件 → 走附件；文件夹 → 直接塞路径，让智能体自己遍历
    const filePaths = dropPaths.filter(_looksLikeFile)
    const dirPaths  = dropPaths.filter(p => !_looksLikeFile(p))

    // 文件夹路径 → 直接 insertAtCursor（零上传）
    for (const d of dirPaths) insertAtCursor(d)

    if (filePaths.length > 0) {
      // 发给后端验证：ROOT 内的秒过，ROOT 外的返回 skipped
      const verified = await _verifyPaths(filePaths)
      const useable  = verified.filter(v => !v.skipped)
      const rejected = verified.filter(v => v.skipped)

      // ROOT 内 → 直接当附件用（零上传！只是把路径塞进卡片）
      for (const v of useable) {
        attachments.value.push({
          id: crypto.randomUUID(),
          name: v.name,
          path: v.path,
          size: v.size,
          content_type: v.content_type || '',
          ext: v.ext || '',
          previewUrl: isImageFile(v.name) ? api.fs.rawUrl(v.path, v.size) : undefined,
          status: 'ready',
        })
      }

      // ROOT 外 → 才走 webkitGetAsEntry + 上传（兜底）
      if (rejected.length > 0) {
        const names = new Set(rejected.map(v => v.name))
        const { files, relativePaths } = await _extractFilesFromDataTransfer(e)
        // 只上传被拒绝的那些（按名字+大小近似匹配）
        const toUpload = files.filter((f, i) => {
          if (names.has(f.name)) return true
          // 再用路径前缀匹配一次（避免同名字漏掉）
          return rejected.some(r => r.path.endsWith('/' + f.name) || r.path.endsWith('\\' + f.name))
        })
        if (toUpload.length > 0) {
          const toPaths = relativePaths.filter((_, i) => toUpload.includes(files[i]))
          await uploadFiles(toUpload, toPaths)
        }
      }
      return
    }
    if (dirPaths.length > 0) return   // 只有目录，已经塞完
  }

  // —— 优先级 2：webkitGetAsEntry 递归（拖文件夹进 ROOT 外，或浏览器内部拖入）——
  const { files, relativePaths } = await _extractFilesFromDataTransfer(e)
  if (files.length > 0) {
    await uploadFiles(files, relativePaths)
    return
  }

  // —— 优先级 3：工作区拖拽路径 ——
  const path = dropPathFromEvent(e)
  if (path) insertAtCursor(path)
}
const renameId = ref<number | null>(null)
const renameVal = ref('')
const convsCollapsed = ref(true) // 对话记录列表默认折叠（展开时浮层覆盖画布）
const canvasesCollapsed = ref(true) // 画布列表默认折叠
const convDesc = ref(true) // 对话记录排序：true = 新在上，false = 新在下

// 图片浮窗：滚轮缩放（以鼠标为锚） + 鼠标拖拽平移 + 8 方向 resize
// —— 解耦架构：窗口尺寸 ≠ 图片缩放 ≠ 图片平移 ——
//   · lightboxW/H = 浮窗尺寸（resize 改这个）
//   · lbScale     = 图片额外缩放（滚轮/键盘改）
//   · lbPanX/Y    = 图片平移（鼠标拖拽）
//   · img { object-fit: contain; transform: translate(x,y) scale(s) }
const lightboxSrc = ref<string | null>(null)
const lightboxOpen = ref(false)
const lightboxX = ref(0)
const lightboxY = ref(0)
const lightboxW = ref(320)
const lightboxH = ref(240)
const lbOrigW = ref(600)
const lbOrigH = ref(400)
const lbScale = ref(1)
const lbPanX = ref(0)
const lbPanY = ref(0)
const lbBodyRef = ref<HTMLElement | null>(null)

const LB_TITLEBAR_H = 32
const LB_MIN_W = 160
const LB_MIN_H = 120
const LB_MAX_SCALE = 8
const LB_MIN_SCALE = 0.1

// 聊天附件预览器（文件卡片点击后打开内置 FileViewer）
const chatViewerFile = ref<ViewerFile | null>(null)

/** 图片 load：只保存原始尺寸，**不再改窗口大小**（图片 object-fit: contain 自动适配）。
 *  窗口尺寸由 openLightbox 一次到位按画布区最大值算好，避免闪变。 */
function onLightboxImgLoad(e: Event) {
  const img = e.target as HTMLImageElement
  if (!img) return
  lbOrigW.value = img.naturalWidth || img.width || 600
  lbOrigH.value = img.naturalHeight || img.height || 400
  // 图片已经 object-fit: contain 填满窗口了，这里只重置缩放/平移
  lbScale.value = 1; lbPanX.value = 0; lbPanY.value = 0
}
function clampLightboxPos() {
  const pad = 8
  const maxX = Math.max(pad, window.innerWidth - lightboxW.value - pad)
  const maxY = Math.max(pad, window.innerHeight - lightboxH.value - pad)
  lightboxX.value = Math.min(Math.max(pad, lightboxX.value), maxX)
  lightboxY.value = Math.min(Math.max(pad, lightboxY.value), maxY)
}
/** 八方向 resize 专用 clamp：保持"没被拖动的那条边"固定 */
function clampResizeEdges(d: string, newX: number, newY: number, newW: number, newH: number) {
  const pad = 8
  let nx = newX, ny = newY, nw = newW, nh = newH
  if (d.includes('e')) nw = Math.min(nw, window.innerWidth - pad - nx)
  if (d.includes('s')) nh = Math.min(nh, window.innerHeight - pad - ny)
  if (d.includes('w')) {
    const rightEdge = nx + nw // 固定右边界
    nw = Math.min(nw, rightEdge - pad)
    nx = rightEdge - nw
  }
  if (d.includes('n')) {
    const bottomEdge = ny + nh // 固定下边界
    nh = Math.min(nh, bottomEdge - pad)
    ny = bottomEdge - nh
  }
  // 整体再保底（极端尺寸）
  nw = Math.max(LB_MIN_W, nw); nh = Math.max(LB_MIN_H, nh)
  nx = Math.max(pad, nx); ny = Math.max(pad, ny)
  return { x: nx, y: ny, w: nw, h: nh }
}
/** 获取画布区（center 列）的 boundingRect；找不到时降级为整个视口内缩 16px */
function getCanvasRect(): { x: number; y: number; w: number; h: number } {
  const center = document.querySelector('.app-root > .center') as HTMLElement | null
  if (center) {
    const r = center.getBoundingClientRect()
    const topOffset = 52
    return { x: r.left + 8, y: r.top + topOffset, w: Math.max(200, r.width - 16), h: Math.max(120, r.height - topOffset - 8) }
  }
  return { x: 16, y: 60, w: window.innerWidth - 32, h: window.innerHeight - 80 }
}
/** 打开浮窗：一次就位——按画布区最大可用尺寸开窗口并居中，避免二次 resize 闪变 */
function openLightbox(src: string, _clickEvt?: MouseEvent) {
  if (!src) return
  const c = getCanvasRect()
  // 窗口一次到位：画布区 80% 宽、80% 高，保留画布区完整的可视 margin
  const pad = 16
  const maxBodyW = Math.max(200, c.w - pad * 2)
  const maxBodyH = Math.max(120, c.h - pad * 2 - LB_TITLEBAR_H)
  lightboxW.value = Math.max(LB_MIN_W, Math.round(maxBodyW) + pad)   // +pad 给 body 的 padding 8×2
  lightboxH.value = Math.max(LB_MIN_H, Math.round(maxBodyH) + pad + LB_TITLEBAR_H)
  lbScale.value = 1; lbPanX.value = 0; lbPanY.value = 0
  lightboxSrc.value = src
  lightboxOpen.value = true
  lightboxX.value = Math.round(c.x + Math.max(0, (c.w - lightboxW.value) / 2))
  lightboxY.value = Math.round(c.y + Math.max(0, (c.h - lightboxH.value) / 2))
  requestAnimationFrame(() => clampLightboxPos())
}
function closeLightbox() {
  lightboxOpen.value = false
  setTimeout(() => { lightboxSrc.value = null }, 200)
}
function onLightboxKey(e: KeyboardEvent) {
  if (!lightboxOpen.value) return
  if (e.key === 'Escape') { e.stopImmediatePropagation(); closeLightbox() }
  else if (e.key === '+' || e.key === '=') lbZoomAtWithAnchor(lbScale.value * 1.2, null)
  else if (e.key === '-') lbZoomAtWithAnchor(lbScale.value / 1.2, null)
  else if (e.key === '0') { lbScale.value = 1; lbPanX.value = 0; lbPanY.value = 0 }
}
/** 设置缩放，锚点 = body 内 (ax, ay)；null 表示 body 中心。
 *  保证鼠标指向的那个像素在屏幕上位置不变。
 *  transform: translate(pan) scale(s) 按 CSS 右→左顺序 = scale 后 translate；
 *  视觉位置 = center + (p-center)*s + pan。
 *  推导得：pan_new = (delta)(1 - s'/s) + pan_old*(s'/s)，其中 delta = anchor - center */
function lbZoomAtWithAnchor(s: number, anchor: { x: number; y: number } | null) {
  const newScale = Math.max(LB_MIN_SCALE, Math.min(LB_MAX_SCALE, s))
  if (newScale === lbScale.value) return
  const body = lbBodyRef.value
  const bw = body?.clientWidth ?? 320
  const bh = body?.clientHeight ?? 240
  const cx = bw / 2; const cy = bh / 2
  const ax = anchor?.x ?? cx; const ay = anchor?.y ?? cy
  const dx = ax - cx; const dy = ay - cy
  const ratio = newScale / lbScale.value
  lbPanX.value = dx * (1 - ratio) + lbPanX.value * ratio
  lbPanY.value = dy * (1 - ratio) + lbPanY.value * ratio
  lbScale.value = newScale
}
/** 双击 body：让图片按窗口尺寸自动填充（scale=1，pan=0） */
function onLbBodyDblClick() {
  lbScale.value = 1
  lbPanX.value = 0
  lbPanY.value = 0
}

// —— 滚轮缩放（以鼠标位置为锚，鼠标不动） ——
function onLbWheel(e: WheelEvent) {
  e.preventDefault()
  const body = lbBodyRef.value
  if (!body) { lbZoomAtWithAnchor(lbScale.value * (e.deltaY > 0 ? 0.85 : 1.18), null); return }
  const r = body.getBoundingClientRect()
  const ax = e.clientX - r.left
  const ay = e.clientY - r.top
  lbZoomAtWithAnchor(lbScale.value * (e.deltaY > 0 ? 0.85 : 1.18), { x: ax, y: ay })
}

// —— body 内鼠标拖拽平移图片 ——
let lbPanning = false
let lbPanStartX = 0
let lbPanStartY = 0
let lbPanStartPanX = 0
let lbPanStartPanY = 0
function onLbPanStart(e: MouseEvent) {
  // 只在左键、且事件 target 不是 body 里的滚动条时触发
  if (e.button !== 0) return
  lbPanning = true
  lbPanStartX = e.clientX; lbPanStartY = e.clientY
  lbPanStartPanX = lbPanX.value; lbPanStartPanY = lbPanY.value
  e.preventDefault()
  window.addEventListener('mousemove', onLbPanMove)
  window.addEventListener('mouseup', onLbPanEnd)
}
function onLbPanMove(e: MouseEvent) {
  if (!lbPanning) return
  let px = lbPanStartPanX + (e.clientX - lbPanStartX)
  let py = lbPanStartPanY + (e.clientY - lbPanStartY)
  // 限制 pan：图片至少要留 30% 在 body 里，防止拖到完全看不见
  const body = lbBodyRef.value
  if (body) {
    const bw = body.clientWidth
    const bh = body.clientHeight
    const halfImgW = (lbOrigW.value * lbScale.value) / 2 * 0.7
    const halfImgH = (lbOrigH.value * lbScale.value) / 2 * 0.7
    px = Math.max(-bw * 0.7, Math.min(bw * 0.7, px))
    py = Math.max(-bh * 0.7, Math.min(bh * 0.7, py))
    // 额外：按图片实际尺寸算更精确的 clamp
    const maxPanX = Math.max(0, lbOrigW.value * lbScale.value / 2 - bw * 0.3)
    const maxPanY = Math.max(0, lbOrigH.value * lbScale.value / 2 - bh * 0.3)
    px = Math.max(-maxPanX, Math.min(maxPanX, px))
    py = Math.max(-maxPanY, Math.min(maxPanY, py))
  }
  lbPanX.value = px
  lbPanY.value = py
}
function onLbPanEnd() {
  lbPanning = false
  window.removeEventListener('mousemove', onLbPanMove)
  window.removeEventListener('mouseup', onLbPanEnd)
}

// —— 标题栏拖拽移动 ——
let lbDragging = false
let lbDragStartX = 0
let lbDragStartY = 0
let lbWinStartX = 0
let lbWinStartY = 0
function onLbDragStart(e: MouseEvent) {
  lbDragging = true
  lbDragStartX = e.clientX; lbDragStartY = e.clientY
  lbWinStartX = lightboxX.value; lbWinStartY = lightboxY.value
  e.preventDefault(); e.stopPropagation()
  window.addEventListener('mousemove', onLbDragMove)
  window.addEventListener('mouseup', onLbDragEnd)
}
function onLbDragMove(e: MouseEvent) {
  if (!lbDragging) return
  lightboxX.value = lbWinStartX + (e.clientX - lbDragStartX)
  lightboxY.value = lbWinStartY + (e.clientY - lbDragStartY)
  clampLightboxPos()
}
function onLbDragEnd() {
  lbDragging = false
  window.removeEventListener('mousemove', onLbDragMove)
  window.removeEventListener('mouseup', onLbDragEnd)
}

// —— 八方向 resize ——
type LbResizeDir = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
let lbResizing = false
let lbResizeDir: LbResizeDir = 'se'
let lbResizeStartX = 0
let lbResizeStartY = 0
let lbResizeStartW = 0
let lbResizeStartH = 0
let lbResizeStartWinX = 0
let lbResizeStartWinY = 0
function onLbResizeStart(e: MouseEvent, dir: LbResizeDir) {
  lbResizing = true
  lbResizeDir = dir
  lbResizeStartX = e.clientX; lbResizeStartY = e.clientY
  lbResizeStartW = lightboxW.value; lbResizeStartH = lightboxH.value
  lbResizeStartWinX = lightboxX.value; lbResizeStartWinY = lightboxY.value
  e.preventDefault(); e.stopPropagation()
  window.addEventListener('mousemove', onLbResizeMove)
  window.addEventListener('mouseup', onLbResizeEnd)
}
function onLbResizeMove(e: MouseEvent) {
  if (!lbResizing) return
  const dx = e.clientX - lbResizeStartX
  const dy = e.clientY - lbResizeStartY
  const d = lbResizeDir
  let nx = lbResizeStartWinX
  let ny = lbResizeStartWinY
  let nw = lbResizeStartW
  let nh = lbResizeStartH
  // 东西方向：w 边向左拖 → x 减 + w 加；e 边只有 w 加
  if (d.includes('w')) {
    const newW = Math.max(LB_MIN_W, lbResizeStartW - dx)
    nx = lbResizeStartWinX + (lbResizeStartW - newW)
    nw = newW
  } else if (d.includes('e')) {
    nw = Math.max(LB_MIN_W, lbResizeStartW + dx)
  }
  // 南北方向：n 边向上拖 → y 减 + h 加；s 边只有 h 加
  if (d.includes('n')) {
    const newH = Math.max(LB_MIN_H, lbResizeStartH - dy)
    ny = lbResizeStartWinY + (lbResizeStartH - newH)
    nh = newH
  } else if (d.includes('s')) {
    nh = Math.max(LB_MIN_H, lbResizeStartH + dy)
  }
  // 专用 clamp：保持 opposite edge 固定
  const c = clampResizeEdges(d, nx, ny, nw, nh)
  lightboxX.value = c.x; lightboxY.value = c.y
  lightboxW.value = c.w; lightboxH.value = c.h
}
function onLbResizeEnd() {
  lbResizing = false
  window.removeEventListener('mousemove', onLbResizeMove)
  window.removeEventListener('mouseup', onLbResizeEnd)
}

/** img transform 计算属性 */
const lbImgTransform = computed(() =>
  `translate(${lbPanX.value}px, ${lbPanY.value}px) scale(${lbScale.value})`
)

// 对话列表浮层的 right 偏移量跟随右侧面板宽度变化
const convFloatStyle = computed(() => ({ right: `${layout.rightWidth + 10}px` }))

// 对话列表右键菜单
const selMode = ref(false)
const selectedIds = ref<number[]>([])
const ctxMenu = ref({ show: false, x: 0, y: 0, id: 0 as number | null })
// 画布列表右键菜单
const cvCtxMenu = ref({ show: false, x: 0, y: 0, id: 0 as number | null })

// ============ 智能体分组下拉 ============
const agentDdOpen = ref(false)
const agentTriggerEl = ref<HTMLElement | null>(null)
const ddPopEl = ref<HTMLElement | null>(null)
const chatPaneEl = ref<HTMLElement | null>(null)
const ddPosStyle = ref<Record<string, string>>({})
/** 按分组分列：第 0 列「全部」= 无组智能体；其余每列一个自定义组 */
const agentGroupsByColumn = computed(() => {
  const out: { id: 'all' | number; name: string; agents: any[] }[] = [
    { id: 'all', name: '全部', agents: agents.list.filter(a => a.group_id == null) }
  ]
  for (const g of agents.groups) {
    out.push({ id: g.id, name: g.name, agents: agents.list.filter(a => a.group_id === g.id) })
  }
  return out
})
function currentAgentName(): string {
  if (currentAgentId.value == null) return '选择智能体'
  return agents.list.find(a => a.id === currentAgentId.value)?.name || '选择智能体'
}
function toggleAgentDd() { agentDdOpen.value = !agentDdOpen.value }
function updateDdPos() {
  const pop = ddPopEl.value
  const pane = chatPaneEl.value
  const input = pane?.querySelector('.chat-input') as HTMLElement | null
  if (!pop || !pane) return
  const paneR = pane.getBoundingClientRect()
  const popH = pop.offsetHeight || 300
  const inputTop = input ? input.getBoundingClientRect().top : paneR.bottom
  const popBottom = inputTop - 6
  let top = popBottom - popH
  // 上方不够时翻到下方
  const spaceAbove = inputTop - 8
  if (top < 8 && popH > spaceAbove) {
    top = inputTop + 6
  }
  ddPosStyle.value = {
    position: 'fixed',
    left: paneR.left + 'px',
    width: paneR.width + 'px',
    top: top + 'px',
  }
}
function onAgentDdOutside(e: MouseEvent) {
  if (!agentDdOpen.value) return
  const tgt = e.target as Node
  if (agentTriggerEl.value && agentTriggerEl.value.contains(tgt)) return
  if (ddPopEl.value && ddPopEl.value.contains(tgt)) return
  agentDdOpen.value = false
}
function selectAgent(id: number) {
  currentAgentId.value = id
  agentDdOpen.value = false
}

// —— 分组列横向滑杆：自定义底部滑块，支持拖拽 / 点击轨道翻页 ——
const ddScrollEl = ref<HTMLElement | null>(null)
const ddScrollLeft = ref(0)
const ddScrollW = ref(0)
const ddClientW = ref(0)

const ddNeedsSlider = computed(() => ddScrollW.value > ddClientW.value + 1)
const ddThumbW = computed(() => {
  if (!ddNeedsSlider.value || ddClientW.value === 0) return 0
  return Math.max(28, Math.round(ddClientW.value * ddClientW.value / ddScrollW.value))
})
const ddMaxScroll = computed(() => Math.max(0, ddScrollW.value - ddClientW.value))
const ddMaxThumbLeft = computed(() => Math.max(0, ddClientW.value - ddThumbW.value))
const ddThumbLeft = computed(() => {
  if (ddMaxScroll.value <= 0) return 0
  return Math.round((ddScrollLeft.value / ddMaxScroll.value) * ddMaxThumbLeft.value)
})
const ddThumbStyle = computed(() => ({
  left: ddThumbLeft.value + 'px',
  width: ddThumbW.value + 'px',
}))

function syncDdMetrics() {
  const el = ddScrollEl.value
  if (!el) return
  ddScrollW.value = el.scrollWidth
  ddClientW.value = el.clientWidth
  ddScrollLeft.value = el.scrollLeft
}
function onDdScroll() {
  const el = ddScrollEl.value
  if (!el) return
  ddScrollLeft.value = el.scrollLeft
}
function onDdWheel(e: WheelEvent) {
  // 鼠标滚轮在列区内转为横向滚动（默认竖向会触发页面滚动）
  const el = ddScrollEl.value
  if (!el) return
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
    e.preventDefault()
    el.scrollLeft += e.deltaY
    ddScrollLeft.value = el.scrollLeft
  }
}

let ddDragging = false
let ddDragStartX = 0
let ddDragStartLeft = 0
function onThumbDown(e: MouseEvent) {
  if (!ddNeedsSlider.value) return
  e.preventDefault()
  e.stopPropagation()
  ddDragging = true
  ddDragStartX = e.clientX
  ddDragStartLeft = ddThumbLeft.value
  window.addEventListener('mousemove', onThumbMove)
  window.addEventListener('mouseup', onThumbUp)
}
function onThumbMove(e: MouseEvent) {
  if (!ddDragging) return
  const el = ddScrollEl.value
  if (!el) return
  const dx = e.clientX - ddDragStartX
  const newLeft = Math.max(0, Math.min(ddMaxThumbLeft.value, ddDragStartLeft + dx))
  const newScroll = ddMaxScroll.value > 0
    ? (newLeft / ddMaxThumbLeft.value) * ddMaxScroll.value
    : 0
  el.scrollLeft = newScroll
  ddScrollLeft.value = newScroll
}
function onThumbUp() {
  ddDragging = false
  window.removeEventListener('mousemove', onThumbMove)
  window.removeEventListener('mouseup', onThumbUp)
}
function onTrackDown(e: MouseEvent) {
  if ((e.target as HTMLElement)?.classList.contains('dd-thumb')) return
  const el = ddScrollEl.value
  if (!el) return
  const trackRect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  const clickX = e.clientX - trackRect.left
  const thumbCenter = ddThumbLeft.value + ddThumbW.value / 2
  const page = ddClientW.value * 0.8
  if (clickX < thumbCenter) el.scrollLeft = Math.max(0, ddScrollLeft.value - page)
  else el.scrollLeft = Math.min(ddMaxScroll.value, ddScrollLeft.value + page)
  ddScrollLeft.value = el.scrollLeft
}

// 弹层打开时测量；列内容变化时也需要重新测量（用 watch）
watch(agentDdOpen, async (open) => {
  if (open) {
    await nextTick()
    updateDdPos()
    syncDdMetrics()
  } else {
    if (ddScrollEl.value) ddScrollEl.value.scrollLeft = 0
    ddScrollLeft.value = 0
  }
})
watch([() => agents.list, () => agents.groups, agentGroupsByColumn], async () => {
  if (agentDdOpen.value) {
    await nextTick()
    syncDdMetrics()
  }
})
function onWinResizeDd() {
  if (agentDdOpen.value) { updateDdPos(); syncDdMetrics() }
  modelDdOpen.value = false
  if (lightboxOpen.value) clampLightboxPos()
}
onMounted(() => {
  document.addEventListener('mousedown', onAgentDdOutside)
  document.addEventListener('mousedown', onModelDdOutside)
  window.addEventListener('resize', onWinResizeDd)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onAgentDdOutside)
  document.removeEventListener('mousedown', onModelDdOutside)
  window.removeEventListener('resize', onWinResizeDd)
  window.removeEventListener('mousemove', onThumbMove)
  window.removeEventListener('mouseup', onThumbUp)
})
// ============ 智能体分组下拉 end ============

// ============ 模型快速切换下拉（接口 + 具体模型，修改当前选中智能体） ============
const modelDdOpen = ref(false)
const modelTriggerEl = ref<HTMLElement | null>(null)
const modelPopEl = ref<HTMLElement | null>(null)
const modelPosStyle = ref<Record<string, string>>({})

const currentAgent = computed(() => agents.list.find((a: any) => a.id === currentAgentId.value) || null)

/** 当前选中智能体的模型标签：接口名 · 具体模型名；未指定接口时用「默认」 */
function currentModelLabel(): string {
  const a = currentAgent.value
  if (!a) return '选择模型'
  const mi = models.list.find((m: any) => m.id === a.interface_id)
  const modelName = a.model || mi?.default_model || ''
  if (!modelName) return mi?.name || '默认'
  return `${mi?.name || '?'} · ${modelName}`
}

/** 接口下可选模型列表：default_model 兜底置顶 */
function ifaceModelList(mi: any): string[] {
  const list = [...(mi?.models || [])]
  if (mi?.default_model && !list.includes(mi.default_model)) list.unshift(mi.default_model)
  return list
}

/** 该接口/模型是否是当前智能体正在使用的 */
function isModelActive(ifaceId: number, modelName: string): boolean {
  const a = currentAgent.value
  if (!a || a.interface_id !== ifaceId) return false
  if (a.model === modelName) return true
  if (!a.model) {
    const mi = models.list.find((m: any) => m.id === ifaceId)
    return mi?.default_model === modelName
  }
  return false
}

async function selectModel(ifaceId: number, modelName: string) {
  const a = currentAgent.value
  modelDdOpen.value = false
  if (!a) return
  await agents.update(a.id, { ...a, interface_id: ifaceId, model: modelName })
}

function toggleModelDd() { modelDdOpen.value = !modelDdOpen.value }

function updateModelDdPos() {
  const pop = modelPopEl.value
  const trigger = modelTriggerEl.value
  if (!pop || !trigger) return
  const tr = trigger.getBoundingClientRect()
  const popH = pop.offsetHeight || 320
  let top = tr.top - popH - 6
  if (top < 8) top = tr.bottom + 6
  modelPosStyle.value = {
    position: 'fixed',
    left: tr.left + 'px',
    width: '300px',
    maxWidth: (window.innerWidth - tr.left - 8) + 'px',
    top: top + 'px',
  }
}

function onModelDdOutside(e: MouseEvent) {
  if (!modelDdOpen.value) return
  const tgt = e.target as Node
  if (modelTriggerEl.value && modelTriggerEl.value.contains(tgt)) return
  if (modelPopEl.value && modelPopEl.value.contains(tgt)) return
  modelDdOpen.value = false
}

watch(modelDdOpen, async (open) => {
  if (open) { await nextTick(); updateModelDdPos() }
})
// ============ 模型快速切换下拉 end ============

const conv = computed(() => convs.currentData())
const canvas = computed(() => canvases.current)
// 运行中对话所属的画布 id（用于画布列表显示"运行中"标志）
const runningCanvasId = computed<number | null>(() => {
  if (convs.running) return convs.runCanvasId ?? null
  if (convs.streaming) return convs.currentData()?.canvas_id ?? null
  return null
})
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

/** 渲染项：以"用户消息"为分界线，把每两条用户消息之间的全部智能体输出
 *  （assistant 消息 + a2a 信封组，按原顺序）整合成一个 turn：
 *  - { type:'user', msg, idx }
 *  - { type:'turn', start, end, children:[ {kind:'assistant',msg,idx} | {kind:'a2a',key,items,latest} ] }
 *  turn 内的全部内容收进同一个固定大小"过程框"流式展示（详见过程窗口逻辑）。 */
const renderItems = computed(() => {
  const msgs = visibleMsgs.value
  const out: any[] = []
  let i = 0
  while (i < msgs.length) {
    const m = msgs[i] as any
    if (m.role === 'user') { out.push({ type: 'user', msg: m, idx: i }); i++; continue }
    // 一轮智能体输出：从 user 消息之后开始，直到下一条 user 消息为止
    const start = i
    const children: any[] = []
    while (i < msgs.length && (msgs[i] as any).role !== 'user') {
      const m2 = msgs[i] as any
      if (m2.role === 'tool_call' || m2.role === 'tool_result') { i++; continue }
      if (m2.role === 'a2a') {
        // 连续的 a2a 信封合并成一组（保留原两级折叠样式）
        const grp: any = { kind: 'a2a', key: 'a2ag-' + i, items: [], latest: m2.envelope }
        while (i < msgs.length && (msgs[i] as any).role === 'a2a') {
          grp.items.push({ msg: msgs[i] as any, idx: i })
          grp.latest = (msgs[i] as any).envelope
          i++
        }
        children.push(grp)
      } else {
        children.push({ kind: 'assistant', msg: m2, idx: i })
        i++
      }
    }
    out.push({ type: 'turn', start, end: i - 1, children })
  }
  return out
})

// ③ 连续工具调用分组折叠 + A2A 信封折叠
const expandedA2a = ref(new Set<number>())
const expandedA2aGroup = ref(new Set<string>())  // 连续信封组的「一级展开」状态
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
        // 虚拟 pill 的 args 实时反映 streamingArgs：流式参数增长时直接显示出来，
        // 不再只显示"参数流式中…"文字提示。streamingArgs 为空时 args={} 走提示兜底。
        const sa = speakingTool.value?.streamingArgs || ''
        let vArgs: any = {}
        if (sa) { try { vArgs = JSON.parse(sa) } catch { vArgs = { _raw: sa } } }
        return [{ role: 'tool_call', name: speakingTool.value.name, tool_call_id: '_virtual_running', args: vArgs, _running: speakingTool.value.phase || 'args' }, ...archived]
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
/** 提取 assistant 消息下 tool_result 的所有图片引用（data_url / file 路径），供对话区缩略图渲染。
 *  覆盖：后端 I: 行 image_refs、历史持久化 images 字段、以及兜底 legacy 的 image_data_url 字段。 */
function imageRefsOfAssistant(idx: number): Array<{ type: 'data_url' | 'file'; src: string }> {
  const out: Array<{ type: 'data_url' | 'file'; src: string }> = []
  const seen = new Set<string>()
  const add = (r: { type: 'data_url' | 'file'; src: string }) => {
    if (r.src && !seen.has(r.src)) { seen.add(r.src); out.push(r) }
  }
  const tools = toolsOfAssistant(idx)
  for (const t of tools) {
    if (t.role !== 'tool_result') continue
    // 1) 新通道：后端 I: 行 / 持久化 images 字段 → image_refs
    const refs = t.image_refs || t.images
    if (Array.isArray(refs)) {
      for (const r of refs) {
        if (r?.type === 'data_url' || r?.type === 'file') add(r)
      }
    }
    // 2) 兜底 legacy：顶层 image_data_url（browser_control share 模式）
    if (typeof t.image_data_url === 'string') {
      add({ type: 'data_url', src: t.image_data_url })
    }
    // 3) 兜底 legacy：result JSON 里的 image_data_url（旧版可能直接塞在 result 里）
    if (typeof t.result?.image_data_url === 'string') {
      add({ type: 'data_url', src: t.result.image_data_url })
    }
  }
  return out
}
/** 把图片引用解析为可用于 <img src=> 的 URL：
 *  data_url → 原样返回；file 路径 → 走后端 /api/fs/image 接口按需加载 */
function resolveImgSrc(ref: { type: 'data_url' | 'file'; src: string }): string {
  if (ref?.type === 'file' && ref.src) {
    return `/api/fs/image?path=${encodeURIComponent(ref.src)}`
  }
  return ref?.src || ''
}
/** @deprecated 用 imageRefsOfAssistant 替代。保留引用以兼容潜在的旧调用点。 */
const shareScreenshotsOfAssistant = imageRefsOfAssistant
function toolBarIsOpen(idx: number): boolean {
  return toolBarOpen.value.has(idx)
}
function toggleToolBar(idx: number) {
  const s = new Set(toolBarOpen.value)
  if (s.has(idx)) {
    s.delete(idx)
  } else {
    s.add(idx)
    // 展开工具行时，自动展开当前正在执行的工具详情（流式参数立即可见）
    const tools = toolsOfAssistant(idx)
    const running = tools.find((t: any) => t.role === 'tool_call' && toolIsRunning(t))
    if (running) {
      const m = new Map(toolDetailOpen.value)
      m.set(idx, running.tool_call_id)
      toolDetailOpen.value = m
    }
  }
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

// ============ 过程窗口（流式期间展开固定大小，停止后折叠为 "(>过程)"） ============
// 设计：以"用户消息"为分界线，每两条用户消息之间的全部智能体输出（多条 assistant 消息、
// 深度思考 / 流式正文 / 工具行 / 截图 / a2a 信封）按原顺序整合进【同一个】固定大小"过程框"
// 流式展示；停止输出时，把本轮最后一条带正文的 assistant "提出来"作为正常气泡渲染，
// 过程框自动折叠为紧贴智能体名称后的 "(>过程)" 文字（无边框、字号字体差异化），点击重新展开。
const processOpen = ref(new Set<number>())   // 键 = turn.start
/** 过程窗口 DOM 引用：用于流式时自动滚到底部（键 = turn.start） */
const processBoxes = new Map<number, HTMLElement>()
/** 把某个过程框贴到内容底部（仅流式中、且用户未主动上滑时）。 */
function stickProcessBox(el: HTMLElement | null | undefined) {
  if (!el || !convs.streaming) return
  const now = Date.now()
  const last = (el as any).__lastUserScroll || 0
  if (now - last > 5000) (el as any).__stickBottom = true
  if ((el as any).__stickBottom === false) return
  ;(el as any).__autoScrolling = true
  el.scrollTop = el.scrollHeight
  requestAnimationFrame(() => { (el as any).__autoScrolling = false })
}
/** 过程框内容监听：思考流 / 正文 / 工具行 / 图片等任何 DOM 变动后重新贴底，
 *  避免"跟随时底部自己开一条空隙"（只靠 scrollKey 单次 rAF 补底不够）。 */
function ensureProcessWatcher(el: HTMLElement) {
  if (!(el as any).__procObserved) {
    ;(el as any).__procObserved = true
    // 一帧内的多次变动合并成一次贴底（避免每次 mutation 都强制回流）
    const mo = new MutationObserver(() => {
      if ((el as any).__procRaf) return
      ;(el as any).__procRaf = requestAnimationFrame(() => {
        ;(el as any).__procRaf = 0
        stickProcessBox(el)
      })
    })
    mo.observe(el, { childList: true, subtree: true, characterData: true })
    ;(el as any).__procMo = mo
  }
  el.querySelectorAll('img').forEach(img => {
    if ((img as any).__procStick) return
    ;(img as any).__procStick = true
    img.addEventListener('load', () => stickProcessBox(el), { once: true })
  })
}
function onProcessBox(turnStart: number, el: HTMLElement | null) {
  const prev = processBoxes.get(turnStart)
  if (prev && prev !== el) (prev as any).__procMo?.disconnect?.()
  if (el) {
    processBoxes.set(turnStart, el)
    // 内联 :ref 在每次重渲染都会重新触发；仅初次挂载时默认跟随底部，
    // 不覆盖用户手动上滑后设置的 __stickBottom=false
    if (typeof (el as any).__stickBottom !== 'boolean') (el as any).__stickBottom = true
    ensureProcessWatcher(el)
  } else processBoxes.delete(turnStart)
}
/** 用户在过程框内滑动即脱离自动跟随；记录最近一次滑动时间，5s 无滑动后恢复跟随。
 *  注意：我们自己写的 scrollTop 也会触发 scroll 事件，那种不算"用户滑动"，必须忽略，
 *  否则每次自动滚底都会把自己标记成脱离，导致永远贴不住底部。
 *  判定用"距底部距离"而不是纯时序标记：程序滚底后 dist≈0 一定不算用户滑动；
 *  用户手动滚回底部则自动恢复跟随。 */
function onProcessScroll(turnStart: number, e: Event) {
  const el = e.target as HTMLElement
  if (!el) return
  if ((el as any).__autoScrolling) return
  const dist = el.scrollHeight - el.scrollTop - el.clientHeight
  if (dist <= 4) {   // 仍在底部 → 保持（或恢复）跟随
    ;(el as any).__stickBottom = true
    return
  }
  ;(el as any).__stickBottom = false
  ;(el as any).__lastUserScroll = Date.now()
}
/** 所有"轮"的边界（每两条用户消息之间的非用户消息区间）—— 用于按轮归属判断 */
const turns = computed(() => {
  const msgs = visibleMsgs.value
  const list: { start: number, end: number }[] = []
  let i = 0
  while (i < msgs.length) {
    if ((msgs[i] as any).role === 'user') { i++; continue }
    const start = i
    while (i < msgs.length && (msgs[i] as any).role !== 'user') i++
    list.push({ start, end: i - 1 })
  }
  return list
})
function turnOf(idx: number): { start: number, end: number } | null {
  for (const t of turns.value) {
    if (idx >= t.start && idx <= t.end) return t
  }
  return null
}
/** 本轮（最近一条 user 消息之后）的起始索引 */
function turnStartIdx(): number {
  const msgs = visibleMsgs.value
  for (let i = msgs.length - 1; i >= 0; i--) {
    if ((msgs[i] as any)?.role === 'user') return i + 1
  }
  return 0
}
/** 该 turn 是否仍在流式（用于强制过程框展开，避免工具行/思考中途消失） */
function isTurnActive(turnStart: number): boolean {
  if (!convs.streaming) return false
  return turnStart === turnStartIdx()
}
/** 该 assistant 是否被"提出来"作为最终正文气泡：仅在其所属轮彻底停止后、
 *  且是"该轮最后一条带正文的 assistant"时成立。流式未结束一律留在过程框里 */
function isPromotedMessage(idx: number): boolean {
  const msgs = visibleMsgs.value
  const m = msgs[idx] as any
  if (!m || m.role !== 'assistant') return false
  const t = turnOf(idx)
  if (!t) return false
  if (isTurnActive(t.start)) return false
  if (isStreamingMsg(m)) return false
  if (!trimMsgContent(m.content)) return false
  for (let i = t.end; i >= t.start; i--) {
    const x = msgs[i] as any
    if (x?.role === 'assistant' && trimMsgContent(x.content)) return i === idx
  }
  return false
}
/** 该轮是否含有"过程"内容（任一子块的思考 / 工具 / 截图 / 非末条正文 / a2a 信封） */
function turnHasContent(turn: any): boolean {
  if (!turn) return false
  for (const ch of turn.children) {
    if (ch.kind === 'a2a') return true
    const m = ch.msg as any
    if ((m.reasoning || '').trim()) return true
    if (hasTools(ch.idx)) return true
    if (shareScreenshotsOfAssistant(ch.idx).length) return true
    if (trimMsgContent(m.content) && !isPromotedMessage(ch.idx)) return true
  }
  return false
}
/** 本轮"提出来"的 assistant 索引（停止后最后一条带正文的 assistant）；流式中或无正文返回 -1 */
function turnPromotedIdx(turn: any): number {
  if (!turn || isTurnActive(turn.start)) return -1
  for (let j = turn.children.length - 1; j >= 0; j--) {
    const ch = turn.children[j]
    if (ch.kind === 'assistant' && trimMsgContent(ch.msg.content)) return ch.idx
  }
  return -1
}
function turnPromotedMsg(turn: any): any {
  const idx = turnPromotedIdx(turn)
  return idx >= 0 ? visibleMsgs.value[idx] : null
}
/** 本轮最后一条 assistant 子块索引（用于右键菜单定位）；无则 turn.start */
function turnLastAssistantIdx(turn: any): number {
  if (!turn) return 0
  for (let j = turn.children.length - 1; j >= 0; j--) {
    const ch = turn.children[j]
    if (ch.kind === 'assistant') return ch.idx
  }
  return turn.start
}
/** 轮首智能体名（取第一个 assistant 子块） */
function turnName(turn: any): string {
  const ch = turn?.children?.find((c: any) => c.kind === 'assistant')
  return ch ? (agentNameOf(ch.msg.agent_id) || '') : ''
}
function turnAvatar(turn: any): string {
  return (turnName(turn) || '智能体').slice(0, 1).toUpperCase()
}

function turnAvatarGradient(turn: any): string {
  const mode = settings.data.avatar_mode || 'differentiated'
  if (mode === 'unified') {
    return 'linear-gradient(135deg, var(--accent), var(--accent-2))'
  }
  const ch = turn?.children?.find((c: any) => c.kind === 'assistant')
  const agentId = ch?.msg?.agent_id || turnName(turn) || '智能体'
  return getAvatarGradient(agentId)
}

function userAvatarGradient(): string {
  const mode = settings.data.avatar_mode || 'differentiated'
  if (mode === 'unified') {
    return 'linear-gradient(135deg, var(--accent), var(--accent-2))'
  }
  return 'linear-gradient(135deg, #f97316, #ec4899)'
}
/** 子块智能体名标签：仅当与"上一个 assistant 子块"的 agent 不同时返回名字（标注接力切换） */
function childAgentLabel(child: any, turn: any): string {
  if (child.kind !== 'assistant') return ''
  let prevAgent: any = null
  for (const c of turn.children) {
    if (c === child) {
      if (prevAgent == null) return ''
      const cur = agentNameOf(child.msg.agent_id)
      const pre = agentNameOf(prevAgent)
      return cur && cur !== pre ? cur : ''
    }
    if (c.kind === 'assistant') prevAgent = c.msg.agent_id
  }
  return ''
}
/** 该轮是否完全无内容可渲染（用于 .empty 隐藏整块） */
function turnEmpty(turn: any): boolean {
  if (isTurnActive(turn.start)) return false
  if (turnPromotedIdx(turn) >= 0) return false
  return !turnHasContent(turn)
}
/** 过程框是否处于"展开"状态：本轮流式中强制展开；停止后由 processOpen 决定 */
function isProcessOpen(turnStart: number): boolean {
  if (isTurnActive(turnStart)) return true
  return processOpen.value.has(turnStart)
}
/** 是否渲染过程框：本轮流式中（承载流式光标 / 工具行）或停止后用户主动展开且有过程内容 */
function showProcessWindow(turnStart: number, turn: any): boolean {
  if (isTurnActive(turnStart)) {
    return turn.children.some((ch: any) => ch.kind === 'assistant' && isStreamingMsg(ch.msg)) || turnHasContent(turn)
  }
  return turnHasContent(turn) && processOpen.value.has(turnStart)
}
/** 是否显示 "(>过程)" 折叠钮：仅停止后且有过程内容时（流式中过程框始终展开，无需折叠钮） */
function showProcToggle(turnStart: number, turn: any): boolean {
  if (isTurnActive(turnStart)) return false
  return turnHasContent(turn)
}
function toggleProcess(turnStart: number) {
  const s = new Set(processOpen.value)
  if (s.has(turnStart)) s.delete(turnStart); else s.add(turnStart)
  processOpen.value = s
}
// —— 过程框内的"思考过程"：默认单行（流式中循环覆盖、停止后截断首行），点击展开全文 ——
const thinkOpen = ref(new Set<number>())   // 键 = assistant 子块 idx
function thinkIsOpen(idx: number): boolean {
  return thinkOpen.value.has(idx)
}
function toggleThink(idx: number) {
  const s = new Set(thinkOpen.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  thinkOpen.value = s
}
function scrollProcessBoxesToBottom() {
  if (!convs.streaming) return
  for (const [turnStart, el] of processBoxes) {
    const turn = renderItems.value.find((it: any) => it.type === 'turn' && it.start === turnStart)
    if (!turn) continue
    if (!isTurnActive(turnStart)) continue
    stickProcessBox(el)
  }
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
  const map = new Map<number, { name: string; running: boolean; total: number; index: number; fileName: string } | null>()
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
    const fileName = extractToolFileName(picked.name, picked.args || {})
    map.set(i, {
      name: toolLabel(picked.name, picked.args || {}),
      running: runningIdx >= 0,
      total: calls.length,
      index: pickedIdx,
      fileName,
    })
  }
  return map
})
function latestToolInfo(idx: number) {
  return latestToolInfoMap.value.get(idx) || null
}

function prettyArgs(args: any): string { try { return JSON.stringify(args, null, 2) } catch { return String(args) } }
function prettyResult(result: any): string {
  // share 截图结果里塞着完整 base64 data URL（用于缩略图渲染 + 落库），
  // 展开详情时把这几个字段替换成占位提示，避免详情面板出现几万字节的 base64 墙。
  let v = result
  if (v && typeof v === 'object' && (v.purpose === 'share' || v.image_data_url)) {
    v = { ...v }
    if (typeof v.image_data_url === 'string') v.image_data_url = '[base64 data URL，已用于缩略图渲染]'
    if (typeof v.data === 'string' && v.data.length > 4000) v.data = `[base64 ${v.data.length} chars，已用于缩略图渲染]`
  }
  try { return JSON.stringify(v, null, 2) } catch { return String(result) }
}
function toggleA2a(idx: number) {
  const s = new Set(expandedA2a.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  expandedA2a.value = s
}

function toggleA2aGroup(key: string) {
  const s = new Set(expandedA2aGroup.value)
  if (s.has(key)) s.delete(key); else s.add(key)
  expandedA2aGroup.value = s
}
function isA2aGroupOpen(key: string): boolean {
  return expandedA2aGroup.value.has(key)
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

const msgCtxMenu = ref<{ x: number; y: number; msgIdx: number; role: string } | null>(null)
const msgMenuEl = ref<HTMLElement | null>(null)

function onMsgContextMenu(e: MouseEvent, idx: number) {
  e.preventDefault()
  const menuW = 260
  const menuH = 60
  const pad = 8
  const msgs = visibleMsgs.value
  const m = msgs[idx]
  const role = m?.role || ''
  const x = Math.max(pad, Math.min(e.clientX, window.innerWidth - menuW - pad))
  const y = Math.max(pad, Math.min(e.clientY, window.innerHeight - menuH - pad))
  msgCtxMenu.value = { x, y, msgIdx: idx, role }
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

// —— 用户头像右键菜单：设置/更换头像照片 ——
const avatarCtxMenu = ref<{ x: number; y: number } | null>(null)
const avatarMenuEl = ref<HTMLElement | null>(null)
const userAvatarPhoto = computed(() => (settings.data.user_avatar as string) || '')

function onAvatarContextMenu(e: MouseEvent) {
  e.preventDefault()
  const menuW = 240
  const menuH = userAvatarPhoto.value ? 88 : 44
  const pad = 8
  const x = Math.max(pad, Math.min(e.clientX, window.innerWidth - menuW - pad))
  const y = Math.max(pad, Math.min(e.clientY, window.innerHeight - menuH - pad))
  avatarCtxMenu.value = { x, y }
  nextTick(() => {
    const el = avatarMenuEl.value
    const v = avatarCtxMenu.value
    if (!el || !v) return
    const r = el.getBoundingClientRect()
    let nx = v.x
    let ny = v.y
    if (r.right > window.innerWidth - pad) nx = Math.max(pad, window.innerWidth - r.width - pad)
    if (r.bottom > window.innerHeight - pad) ny = Math.max(pad, window.innerHeight - r.height - pad)
    if (nx !== v.x || ny !== v.y) avatarCtxMenu.value = { ...v, x: nx, y: ny }
  })
}
function closeAvatarCtx() { avatarCtxMenu.value = null }

const avatarInput = ref<HTMLInputElement | null>(null)

function triggerAvatarPhotoUpload() {
  closeAvatarCtx()
  if (!avatarInput.value) {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.style.display = 'none'
    input.addEventListener('change', handleAvatarPhotoUpload)
    document.body.appendChild(input)
    avatarInput.value = input
  }
  avatarInput.value.click()
}

async function handleAvatarPhotoUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (avatarInput.value) {
    avatarInput.value.remove()
    avatarInput.value = null
  }
  if (!file) return
  try {
    const dataUrl = await shrinkToDataUrl(file, 256)
    await settings.update({ user_avatar: dataUrl })
  } catch {
    alert('头像照片读取失败，请换一张图片重试')
  }
}

async function resetAvatarPhoto() {
  closeAvatarCtx()
  await settings.update({ user_avatar: '' })
}

/** 图片等比缩小到 max 边长内，转成 JPEG dataURL（避免原图过大撑爆配置存储） */
function shrinkToDataUrl(file: File, max = 256): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      try {
        const iw = img.width || 1
        const ih = img.height || 1
        const scale = Math.min(1, max / Math.max(iw, ih))
        const w = Math.max(1, Math.round(iw * scale))
        const h = Math.max(1, Math.round(ih * scale))
        const c = document.createElement('canvas')
        c.width = w
        c.height = h
        const ctx = c.getContext('2d')
        if (!ctx) return reject(new Error('canvas unsupported'))
        ctx.drawImage(img, 0, 0, w, h)
        resolve(c.toDataURL('image/jpeg', 0.85))
      } catch (err) { reject(err) }
    }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('invalid image')) }
    img.src = url
  })
}

// —— 删除单条消息：截断到这条之前 ——
const msgOpStatus = ref('')
// —— 复制消息文本到剪贴板（用户/智能体消息通用） ——
async function copyMessageFromCtx() {
  const menu = msgCtxMenu.value
  closeMsgCtx()
  if (!menu) return
  const m = visibleMsgs.value[menu.msgIdx]
  const text = (m?.content || '').replace(/\s+$/, '')
  if (!text) {
    msgOpStatus.value = '（该消息无文本可复制）'
    setTimeout(() => { msgOpStatus.value = '' }, 2000)
    return
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    msgOpStatus.value = '✅ 已复制到剪贴板'
  } catch {
    msgOpStatus.value = '❌ 复制失败'
  }
  setTimeout(() => { msgOpStatus.value = '' }, 2000)
}
async function deleteMessageFromCtx() {
  const menu = msgCtxMenu.value
  closeMsgCtx()
  if (!menu) return
  const c = conv.value
  if (!c) return
  const m = visibleMsgs.value[menu.msgIdx]
  if (!m) return
  const preview = (m.content || '').replace(/\s+/g, ' ').trim().slice(0, 30)
  if (!confirm(`删除这条消息？\n\n「${preview || '(空)'}」\n\n仅删除该条消息本身，其余对话保留。`)) return
  msgOpStatus.value = '正在删除…'
  const r = await convs.deleteMessage(menu.msgIdx)
  if (r.ok) {
    msgOpStatus.value = r.removed && r.removed > 1
      ? `✅ 已删除（含 ${r.removed} 条工具结果，智能体不再记得此轮）`
      : '✅ 已删除'
  } else {
    msgOpStatus.value = '❌ 删除失败: ' + (r.msg || '未知错误')
  }
  setTimeout(() => { msgOpStatus.value = '' }, 3000)
}

// —— 撤销：删除该用户消息及之后的所有对话，并还原之后的所有文件操作 ——
async function undoFromCtx() {
  const menu = msgCtxMenu.value
  closeMsgCtx()
  if (!menu) return
  const c = conv.value
  if (!c) return
  const m = visibleMsgs.value[menu.msgIdx]
  if (!m) return
  const preview = (m.content || '').replace(/\s+/g, ' ').trim().slice(0, 30)
  if (!confirm(`撤销这条用户消息？\n\n「${preview || '(空)'}」\n\n该消息及其后的所有对话都会被删除，之后智能体修改的文件会被还原。`)) return
  msgOpStatus.value = '正在撤销…'
  const r = await convs.undoFromUserMessage(menu.msgIdx)
  if (r.ok) {
    msgOpStatus.value = `✅ 已撤销${r.restoredOps ? `（还原 ${r.restoredOps} 项文件操作）` : ''}`
  } else {
    msgOpStatus.value = '❌ 撤销失败: ' + (r.msg || '未知错误')
  }
  setTimeout(() => { msgOpStatus.value = '' }, 4000)
}

// —— 编辑用户消息（仅最新一条）：撤销本轮对话并重新发送修改内容 ——
const editDlg = ref<{ open: boolean; content: string; agentId: number | null; msgIdx: number }>(
  { open: false, content: '', agentId: null, msgIdx: -1 }
)

/** 该用户消息是否是最新一条用户消息（其后不再有 user 消息） */
function isLastUserMsg(idx: number): boolean {
  const msgs = visibleMsgs.value
  if (!msgs[idx] || msgs[idx].role !== 'user') return false
  for (let i = idx + 1; i < msgs.length; i++) {
    if (msgs[i].role === 'user') return false
  }
  return true
}

/** 该用户消息之后第一个 assistant 的 agent_id（作为重新发送的目标智能体） */
function agentIdAfterUserMsg(rawIdx: number): number | null {
  const msgs = conv.value?.messages || []
  for (let i = rawIdx + 1; i < msgs.length; i++) {
    const x = msgs[i]
    if (x.role === 'assistant' && x.agent_id != null) return x.agent_id
  }
  return null
}

function editMessageFromCtx() {
  const menu = msgCtxMenu.value
  if (!menu) return
  const c = conv.value
  const m = visibleMsgs.value[menu.msgIdx]
  if (!c || !m || m.role !== 'user') return
  let rawIdx = (c.messages || []).indexOf(m)
  if (rawIdx < 0) rawIdx = menu.msgIdx
  const agentId = agentIdAfterUserMsg(rawIdx) ?? currentAgentId.value ?? agents.list[0]?.id ?? null
  closeMsgCtx()
  editDlg.value = { open: true, content: m.content || '', agentId, msgIdx: menu.msgIdx }
}

async function confirmEditMessage() {
  const dlg = editDlg.value
  const text = (dlg.content || '').trim()
  if (!text) {
    msgOpStatus.value = '❌ 内容不能为空'
    setTimeout(() => { msgOpStatus.value = '' }, 2000)
    return
  }
  if (dlg.agentId == null) {
    editDlg.value = { ...dlg, open: false }
    msgOpStatus.value = '❌ 找不到目标智能体'
    setTimeout(() => { msgOpStatus.value = '' }, 2000)
    return
  }
  editDlg.value = { ...dlg, open: false }
  msgOpStatus.value = '正在撤销并重新发送…'
  const r = await convs.undoFromUserMessage(dlg.msgIdx)
  if (!r.ok) {
    msgOpStatus.value = '❌ 撤销失败: ' + (r.msg || '未知错误')
    setTimeout(() => { msgOpStatus.value = '' }, 3000)
    return
  }
  stickBottom.value = true
  await convs.send(dlg.agentId, text)
  setTimeout(() => { stickBottom.value = true; scrollToBottom() }, 50)
}

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

// ③ 流式自动滚动：streaming + 最后消息内容长度 + 消息数 + 工具参数流式长度
//   用户停留在底部附近时自动贴底；用户上滑即脱离，不再抢滚动焦点（避免"锁底"）
const BOTTOM_THRESHOLD = 48
const stickBottom = ref(true)

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD
}
function onMessagesScroll() {
  const el = msgBox.value
  if (!el) return
  stickBottom.value = isNearBottom(el)
  layout.chatMsgScrollTop = el.scrollTop
  console.log('[DBG scroll] saved chatMsgScrollTop=', el.scrollTop)
}
function onConvListScroll() {
  const el = convListEl.value
  if (el) layout.convListScrollTop = el.scrollTop
}
function scrollToBottom() {
  const el = msgBox.value
  if (el) el.scrollTop = el.scrollHeight
}
function followBottom() {
  if (stickBottom.value) scrollToBottom()
}

// ④ 本轮输出完毕：把"提出来"的最终正文气泡滚到可视区顶部第一行，便于用户从头阅读
function scrollToTurnBubbleTop() {
  const el = msgBox.value
  if (!el) return
  const turns = el.querySelectorAll<HTMLElement>('.msg.assistant.turn')
  const target = turns[turns.length - 1]?.querySelector<HTMLElement>('.promoted-bubble')
  if (!target) return
  const delta = target.getBoundingClientRect().top - el.getBoundingClientRect().top
  el.scrollTop = Math.max(0, el.scrollTop + delta - 8)
}

// 工具参数流式输出：贴近底部自动跟随，用户上滑即脱离
function onStreamingArgsScroll(e: Event) {
  const el = e.target as HTMLElement
  if (!el || !el.classList.contains('tr-streaming-args')) return
  ;(el as any).__stickBottom = isNearBottom(el)
}
function scrollStreamingArgsToBottom() {
  const root = msgBox.value
  if (!root) return
  root.querySelectorAll<HTMLElement>('.tr-streaming-args').forEach(el => {
    if ((el as any).__stickBottom === false) return
    el.scrollTop = el.scrollHeight
  })
}

const scrollKey = computed(() => {
  const msgs = conv.value?.messages || []
  const last = msgs[msgs.length - 1]
  return `${convs.streaming ? 1 : 0}:${msgs.length}:${(last?.content || '').length}:${speakingTool.value?.streamingArgs?.length || 0}`
})
watch(scrollKey, async () => {
  await nextTick()
  followBottom()
  requestAnimationFrame(scrollStreamingArgsToBottom)
  requestAnimationFrame(scrollProcessBoxesToBottom)
})

// 流式结束（本轮收尾）时：自动折叠本轮的过程框 → 让 "(>过程)" 折叠态生效。
// 只收敛「本轮」（turnStartIdx），不影响用户此前手动展开的历史轮次过程框。
// 折叠后把最终正文气泡滚到第一行；用户上滑过则不打扰。
let _wasStreamingForProc = false
watch(() => convs.streaming, async (v) => {
  if (_wasStreamingForProc && !v) {
    const s = new Set(processOpen.value)
    s.delete(turnStartIdx())
    processOpen.value = s
    const stick = stickBottom.value
    await nextTick()
    await new Promise((r) => requestAnimationFrame(() => r(null)))
    if (stick) {
      scrollToTurnBubbleTop()
      stickBottom.value = false
    }
  }
  _wasStreamingForProc = v
})

// 切换对话记录时：滚动到底部，并默认选中当前会话最新一条回复的智能体
watch(() => convs.current, async (id) => {
  if (id == null) return
  await nextTick()
  // 切换对话记录：回到最新一条，并恢复底部跟随
  stickBottom.value = true
  scrollToBottom()
  const data = convs.currentData()
  const msgs = data?.messages || []
  const lastAssistant = [...msgs].reverse().find(
    (m: any) => m.role === 'assistant' && m.agent_id != null
  )
  if (lastAssistant && lastAssistant.agent_id != null) {
    currentAgentId.value = lastAssistant.agent_id
    convs.selectedAgentId = lastAssistant.agent_id
  }
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
  // 合并消息文本 + 附件
  const msgText = input.value.trim()
  const attText = attachmentsToText()
  if (!msgText && !attText) return
  const fullText = [msgText, attText].filter(Boolean).join('\n\n')
  input.value = ''  // ← 立刻清空，不等流式结束
  // 清理附件（已合并到文本里）
  for (const a of attachments.value) { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl) }
  attachments.value = []

  const agentId = currentAgentId.value ?? agents.list[0]?.id; if (!agentId) return alert('请先新建智能体')
  stickBottom.value = true   // 自己发的消息必须可见
  await convs.send(agentId, fullText)
  setTimeout(() => { stickBottom.value = true; scrollToBottom() }, 50)
}
function sendStop() {
  convs.stopSend()
}

async function toggleVoiceInput() {
  if (isRecording.value) { stopVoiceInput() } else { await startVoiceInput() }
}

async function startVoiceInput() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    isRecording.value = true
    startRecordingChunk()
  } catch {
    alert('无法访问麦克风，请检查浏览器权限设置')
  }
}

function startRecordingChunk() {
  if (!mediaStream) return
  audioChunks = []
  mediaRecorder = new MediaRecorder(mediaStream)
  mediaRecorder.ondataavailable = (e: BlobEvent) => { if (e.data.size > 0) audioChunks.push(e.data) }
  mediaRecorder.onstop = () => {
    if (audioChunks.length > 0) {
      const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || 'audio/webm' })
      transcribeChunk(blob)
    }
    if (isRecording.value) startRecordingChunk()
  }
  mediaRecorder.start()
  recordTimer = window.setTimeout(() => {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop()
  }, 2000)
}

function stopVoiceInput() {
  isRecording.value = false
  if (recordTimer) { clearTimeout(recordTimer); recordTimer = null }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop()
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null }
}

async function transcribeChunk(webmBlob: Blob) {
  if (webmBlob.size < 2000) return
  try {
    const arrayBuffer = await webmBlob.arrayBuffer()
    const audioContext = new AudioContext()
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer)
    const wavBlob = audioBufferToWav(audioBuffer)
    audioContext.close()
    const wavBuf = await wavBlob.arrayBuffer()
    const bytes = new Uint8Array(wavBuf)
    let binary = ''
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192))
    const base64 = btoa(binary)
    const r = await fetch('/api/asr/transcribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio: base64, language: 'auto' })
    })
    const data = await r.json()
    if (data.text) {
      input.value = input.value ? input.value + data.text : data.text
    }
  } catch {
    // 静默失败，不打断录音
  }
}

function audioBufferToWav(buffer: AudioBuffer): Blob {
  const numChannels = buffer.numberOfChannels
  const sampleRate = buffer.sampleRate
  const numSamples = buffer.length
  const blockAlign = numChannels * 2
  const dataSize = numSamples * blockAlign
  const ab = new ArrayBuffer(44 + dataSize)
  const view = new DataView(ab)
  const ws = (off: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)) }
  ws(0, 'RIFF'); view.setUint32(4, 36 + dataSize, true); ws(8, 'WAVE'); ws(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, numChannels, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * blockAlign, true)
  view.setUint16(32, blockAlign, true); view.setUint16(34, 16, true); ws(36, 'data'); view.setUint32(40, dataSize, true)
  const channels: Float32Array[] = []
  for (let i = 0; i < numChannels; i++) channels.push(buffer.getChannelData(i))
  let off = 44
  for (let i = 0; i < numSamples; i++) {
    for (let ch = 0; ch < numChannels; ch++) {
      let s = Math.max(-1, Math.min(1, channels[ch][i]))
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true); off += 2
    }
  }
  return new Blob([ab], { type: 'audio/wav' })
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
// 工具行标签直接用 conversation.ts 的统一 toolLabel（已内置伪协议清洗 + settings meta 查询）
// 不再维护本地硬编码映射，避免和画布/对话区行为分叉
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

/**
 * 格式化文件路径for显示：如果空间够完整显示，如果空间不够则从两端显示（保留文件名）
 * @param path 文件路径
 * @param type 'summary' 用于摘要行，'detail' 用于详情行
 * @returns 格式化后的文件路径
 */
function formatFilePath(path: string, type: 'summary' | 'detail'): string {
  if (!path) return '';
  
  // 根据CSS中的max-width和font-size估算最大字符数
  let maxChars: number;
  if (type === 'summary') {
    // .tr-filename: max-width: 140px; font-size: calc(10px * var(--font-scale))
    maxChars = Math.floor(140 / 10); // 14
  } else {
    // .tr-filename-mini: max-width: 100px; font-size: calc(9.5px * var(--font-scale))
    maxChars = Math.floor(100 / 9.5); // 10
  }
  
  // 如果路径短于等于最大字符数，直接返回
  if (path.length <= maxChars) {
    return path;
  }
  
  // 提取文件名（路径的最后一部分）
  const lastSlashIndex = Math.max(
    path.lastIndexOf('\\'),
    path.lastIndexOf('/')
  );
  
  let fileName = '';
  let pathPrefix = '';
  
  if (lastSlashIndex !== -1) {
    fileName = path.substring(lastSlashIndex + 1);
    pathPrefix = path.substring(0, lastSlashIndex + 1); // 包含斜杠
  } else {
    // 没有斜杠，整个路径就是文件名
    fileName = path;
    pathPrefix = '';
  }
  
  // 如果文件名本身就超过了最大字符数，那就只返回文件名的末尾部分
  if (fileName.length >= maxChars) {
    // 如果即使是文件名也太长，就截断文件名的末尾
    return fileName.substring(0, maxChars - 3) + '...';
  }
  
  // 计算可以用于路径前缀的字符数
  // 需要留出3个字符给省略号，以及文件名本身
  const availableForPrefix = maxChars - 3 - fileName.length;
  
  if (availableForPrefix <= 0) {
    // 空间不够显示路径前缀，只显示省略号+文件名
    return '...' + fileName;
  }
  
  // 取路径前缀的开头availableForPrefix个字符
  const prefixPart = pathPrefix.substring(0, Math.min(pathPrefix.length, availableForPrefix));
  
  return prefixPart + '...' + fileName;
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
function onDocClick() { closeCtx(); closeCvCtx(); closeMsgCtx(); closeAvatarCtx(); convsCollapsed.value = true }
function onDocKey(e: KeyboardEvent) {
    if (e.key === 'Escape') { closeCtx(); closeCvCtx(); closeMsgCtx(); closeAvatarCtx(); convsCollapsed.value = true; if (selMode.value) exitSel(); modelDdOpen.value = false }
}

/** 聊天消息内附件点击事件委托（冒泡到 document 处理，支持 v-html 渲染的附件卡片） */
function onChatAttachClick(e: MouseEvent) {
  const t = e.target as HTMLElement
  if (!t) return
  // 从目标向上找 chat-attach 或 chat-img-wrap 祖先
  const card = t.closest('.chat-attach, .chat-img-wrap') as HTMLElement | null
  if (!card) return
  e.stopPropagation()
  // 文件卡片 → 打开 FileViewer
  const path = (card.getAttribute('data-path') || '').trim()
  const name = (card.getAttribute('data-name') || '').trim()
  if (card.classList.contains('chat-attach') && path && name) {
    const sizeStr = card.getAttribute('data-size') || ''
    chatViewerFile.value = {
      path, name,
      size: sizeStr ? parseInt(sizeStr, 10) || 0 : 0,
    }
    return
  }
  // 图片缩略图 → 打开已有 lightbox
  if (card.classList.contains('chat-img-wrap')) {
    const src = (card.getAttribute('data-src') || '').trim()
    if (src) openLightbox(src, e)
  }
}

onMounted(async () => {
  // 父级 Home 的 fetch 可能还没返回，这里先确保拉到列表，避免误判为空而重复新建
  if (!convs.list.length) await convs.fetch()
  if (!convs.current && convs.list.length) convs.current = convs.list[0].id
  if (!currentAgentId.value && agents.list.length) currentAgentId.value = agents.list[0].id
  if (!convs.list.length) await newConv('independent')
})
// 从设置/能力管理等页面返回首页时，浏览器/KeepAlive 会把脱离文档流的可滚动元素
// scrollTop 归零；这里保存 / 还原消息区与会话列表的滚动位置。
// 滚动位置存在 layout store（仅内存），既能覆盖 KeepAlive 命中的情况，
// 也能覆盖 KeepAlive 未命中（组件被重建）的情况。
// 注意：onDeactivated 触发时 DOM 已被移出文档流，scrollTop 可能读不到，
// 所以主保存点在滚动事件里（onMessagesScroll / onConvListScroll），这里只做兜底。
function restoreScrollPositions() {
  const mEl = msgBox.value
  const before = mEl?.scrollTop
  if (mEl) mEl.scrollTop = layout.chatMsgScrollTop
  console.log('[DBG restore] saved=', layout.chatMsgScrollTop, 'before=', before, 'after=', mEl?.scrollTop, 'sh=', mEl?.scrollHeight)
  const cEl = convListEl.value
  if (cEl) cEl.scrollTop = layout.convListScrollTop
}
onMounted(() => {
  console.log('[DBG] onMounted')
  nextTick(() => { restoreScrollPositions(); requestAnimationFrame(restoreScrollPositions) })
})
onActivated(() => {
  console.log('[DBG] onActivated, store.chatMsgScrollTop=', layout.chatMsgScrollTop)
  document.addEventListener('click', onDocClick)
  document.addEventListener('click', onChatAttachClick, true)   // capture 阶段先触发，避免冒泡被吞
  document.addEventListener('keydown', onDocKey)
  window.addEventListener('keydown', onLightboxKey)
  nextTick(() => {
    restoreScrollPositions()
    requestAnimationFrame(restoreScrollPositions)
    setTimeout(restoreScrollPositions, 0)
  })
})
onDeactivated(() => {
  const mEl = msgBox.value
  console.log('[DBG] onDeactivated, isConnected=', mEl?.isConnected, 'el.scrollTop=', mEl?.scrollTop, 'store=', layout.chatMsgScrollTop)
  if (mEl && mEl.isConnected) layout.chatMsgScrollTop = mEl.scrollTop
  const cEl = convListEl.value
  if (cEl && cEl.isConnected) layout.convListScrollTop = cEl.scrollTop
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('click', onChatAttachClick, true)
  document.removeEventListener('keydown', onDocKey)
  window.removeEventListener('keydown', onLightboxKey)
  // 浮窗拖拽 / resize / pan 期间被切走也要清干净
  window.removeEventListener('mousemove', onLbDragMove)
  window.removeEventListener('mouseup', onLbDragEnd)
  window.removeEventListener('mousemove', onLbResizeMove)
  window.removeEventListener('mouseup', onLbResizeEnd)
  window.removeEventListener('mousemove', onLbPanMove)
  window.removeEventListener('mouseup', onLbPanEnd)
})
onBeforeUnmount(() => {
  // 清理附件预览 URL
  for (const a of attachments.value) { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl) }
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
            <span v-if="runningCanvasId === c.id" class="cv-running" title="正在运行">●</span>
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
    <div class="chat-pane" ref="chatPaneEl">
      <div class="chat-header">
        <div class="section-title conv-title">
          <button class="collapse-btn" type="button"
                  @click.stop="convsCollapsed = !convsCollapsed"
                  :title="convsCollapsed ? '展开对话记录（覆盖在画布上）' : '折叠对话记录'">
            {{ convsCollapsed ? '▸' : '▾' }}
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
          <div class="conv-list-wrap" ref="convListEl" @scroll.passive="onConvListScroll">
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
          <!-- 复制（所有消息通用） -->
          <div class="msg-ctx-item" @click.stop="copyMessageFromCtx">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></span>
            <span>复制</span>
          </div>
          <!-- 修改（仅用户最新一条消息） -->
          <div v-if="msgCtxMenu.role === 'user' && isLastUserMsg(msgCtxMenu.msgIdx)" class="msg-ctx-item" @click.stop="editMessageFromCtx">
            <span class="msg-ctx-icon">✏️</span>
            <span>修改此消息并重新发送</span>
          </div>
          <!-- 删除消息（所有消息通用） -->
          <div class="msg-ctx-item" @click.stop="deleteMessageFromCtx">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></span>
            <span>删除此消息</span>
          </div>
          <!-- 撤销（仅用户消息） -->
          <div v-if="msgCtxMenu.role === 'user'" class="msg-ctx-item" @click.stop="undoFromCtx">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 5 5v3"/></svg></span>
            <span>撤销此消息及之后对话（并还原文件）</span>
          </div>
          <!-- 查看文件操作历史（仅用户消息） -->
          <div v-if="msgCtxMenu.role === 'user'" class="msg-ctx-item" @click.stop="openFileOpsFromMsg">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M3 7v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2H10l-2-2H5a2 2 0 0 0-2 2z" fill="#fff" stroke="#2E7D8A" stroke-width="1.8" stroke-linejoin="round"/><path d="M3 7a2 2 0 0 1 2-2h3l2 2H3z" fill="#8FC1C1"/></svg></span>
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
      <!-- 编辑用户消息对话框（修改后撤销本轮并重新发送） -->
      <Teleport to="body">
        <div v-if="editDlg.open" class="modal-backdrop" @click.self="editDlg.open = false">
          <div class="modal edit-msg-modal" @click.stop>
            <div class="modal-header">
              <span class="modal-title">✏️ 修改消息并重新发送</span>
              <button class="btn sm ghost" @click="editDlg.open = false">✕</button>
            </div>
            <div class="modal-sub">
              保存后将撤销这条用户消息及其后智能体的回复（含还原文件操作），并把新内容重新发送给智能体。
            </div>
            <div class="modal-body">
              <textarea class="input input-ta edit-msg-ta" v-model="editDlg.content" placeholder="输入修改后的内容..."
                        @keydown.ctrl.enter.prevent="confirmEditMessage" @keydown.meta.enter.prevent="confirmEditMessage"></textarea>
            </div>
            <div class="edit-msg-actions">
              <button class="btn ghost" @click="editDlg.open = false">取消</button>
              <button class="btn" @click="confirmEditMessage">修改并重新发送</button>
            </div>
          </div>
        </div>
      </Teleport>
      <!-- 用户头像右键菜单（更换照片） -->
      <Teleport to="body">
        <div v-if="avatarCtxMenu"
             ref="avatarMenuEl"
             class="msg-ctx-menu"
             :style="{ left: avatarCtxMenu.x + 'px', top: avatarCtxMenu.y + 'px' }"
             @click.stop @mousedown.stop>
          <div class="msg-ctx-item" @click.stop="triggerAvatarPhotoUpload">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="8" r="3"/><path d="M6 20a6 6 0 0 1 12 0"/></svg></span>
            <span>{{ userAvatarPhoto ? '更换头像照片' : '设置头像照片' }}</span>
          </div>
          <div v-if="userAvatarPhoto" class="msg-ctx-item" @click.stop="resetAvatarPhoto">
            <span class="msg-ctx-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 5 5v3"/></svg></span>
            <span>恢复默认头像</span>
          </div>
        </div>
      </Teleport>
      <!-- 消息操作状态提示（删除/撤销的即时反馈） -->
      <Teleport to="body">
        <div v-if="msgOpStatus" class="msg-op-status">{{ msgOpStatus }}</div>
      </Teleport>
      <div class="chat-box" ref="msgBox" @scroll.passive="onMessagesScroll">
        <div v-if="!conv" class="no-conv">请新建对话</div>
        <!-- ============ 消息列表 ============
             tool_call/tool_result 不单独出块，归属到紧接其前的 assistant；
             连续 a2a 信封合并成一组，默认折叠为一条摘要（两级展开） -->
        <template v-for="(it, ri) in renderItems" :key="ri">
          <!-- ===== 用户消息（靠右，作为每轮智能体输出的分界） ===== -->
          <div v-if="it.type === 'user'" class="msg user" @contextmenu.prevent.stop="onMsgContextMenu($event, it.idx)">
            <div class="avatar user-avatar" :style="{ background: userAvatarGradient() }"
                 @contextmenu.prevent.stop="onAvatarContextMenu($event)">
              <img v-if="userAvatarPhoto" class="avatar-photo" :src="userAvatarPhoto" alt="U" />
              <template v-else>U</template>
            </div>
            <div class="bubble markdown user-bubble" v-html="msgBubbleHtml(it.msg)"></div>

          </div>

          <!-- ===== 一轮智能体输出（靠左）=====
               以"用户消息"为分界：本轮（两条用户消息之间）的全部智能体连续输出
               （多条 assistant 的思考/正文/工具行/截图 + a2a 信封）按原顺序整合进
               【同一个】固定大小"过程框"流式展示；停止时把末条带正文的 assistant
               提出作为正常气泡，过程框折叠为名称后的 "(>过程)" -->
          <div v-else-if="it.type === 'turn'"
               class="msg assistant turn"
               :class="{ empty: turnEmpty(it), streaming: isTurnActive(it.start) }"
               @contextmenu.prevent.stop="onMsgContextMenu($event, turnLastAssistantIdx(it))">
            <div class="col-wrap">
              <!-- 头像 + 名字行 + 过程折叠钮（每轮只出现一次，无边框、字号字体差异化） -->
              <div class="head-row">
                <div class="avatar" :style="{ background: turnAvatarGradient(it) }">{{ turnAvatar(it) }}</div>
                <div v-if="turnName(it)" class="who-assistant">{{ turnName(it) }}</div>
                <span v-if="turnElapsedLabel(it)" class="turn-elapsed" :class="{ active: isTurnActive(it.start) }">{{ turnElapsedLabel(it) }}</span>
                <span v-if="showProcToggle(it.start, it)"
                      class="proc-toggle"
                      :class="{ open: isProcessOpen(it.start) }"
                      @click.stop="toggleProcess(it.start)">{{ isProcessOpen(it.start) ? '(▾过程)' : '(>过程)' }}</span>
              </div>

              <!-- 过程框：固定大小 + 内部滚动，整合本轮全部智能体输出（顺序保持）；
                   展开时位于折叠钮下方、最终正文气泡上方 -->
              <div v-if="showProcessWindow(it.start, it)" class="process-window">
                <div class="process-body"
                     :ref="(el) => onProcessBox(it.start, el as HTMLElement | null)"
                     @scroll.passive="onProcessScroll(it.start, $event)">
                  <template v-for="ch in it.children" :key="ch.kind === 'a2a' ? ch.key : ('a-' + ch.idx)">

                    <!-- a2a 信封组：保留原两级折叠样式，作为本轮过程的一部分按顺序展示 -->
                    <div v-if="ch.kind === 'a2a'" class="a2a-group-wrap">
                      <!-- 多条时的组摘要头 -->
                      <div v-if="ch.items.length > 1" class="tool-card a2a a2a-group" :class="{ open: isA2aGroupOpen(ch.key) }">
                        <div class="tc-header a2a-toggle" @click="toggleA2aGroup(ch.key)">
                          <span class="tc-ico">🤝</span>
                          <span class="tc-name">连续信封 ×{{ ch.items.length }}</span>
                          <span class="a2a-route">{{ ch.latest?.from?.name }} → {{ ch.latest?.to?.name }}</span>
                          <span class="tg-chev">{{ isA2aGroupOpen(ch.key) ? '▾' : '▸' }}</span>
                        </div>
                      </div>
                      <!-- 信封卡片列表：单条直接显示；多条需一级展开 -->
                      <div v-if="ch.items.length === 1 || isA2aGroupOpen(ch.key)" class="a2a-group-body" :class="{ single: ch.items.length === 1 }">
                        <div v-for="sub in ch.items" :key="sub.idx" class="tool-card a2a">
                          <div class="tc-header a2a-toggle" @click="toggleA2a(sub.idx)">
                            <span class="tc-ico">{{ ch.items.length > 1 ? '✉️' : '🤝' }}</span>
                            <span class="tc-name">{{ kindLabel(sub.msg.envelope?.kind) }}</span>
                            <span class="a2a-route">{{ sub.msg.envelope?.from?.name }} → {{ sub.msg.envelope?.to?.name }}</span>
                            <span class="tc-id">{{ sub.msg.envelope?.task?.state }}</span>
                            <span class="tg-chev">{{ expandedA2a.has(sub.idx) ? '▾' : '▸' }}</span>
                          </div>
                          <div v-if="expandedA2a.has(sub.idx)" class="a2a-body">
                            <div class="a2a-meta">
                              <span class="a2a-chip">{{ sub.msg.envelope?.task_id }}</span>
                              <span class="a2a-chip dim">{{ sub.msg.envelope?.message_id }}</span>
                            </div>
                            <template v-for="(p, pi) in sub.msg.envelope?.parts || []" :key="pi">
                              <div v-if="p.type === 'text'" class="a2a-text">{{ p.text }}</div>
                              <details v-else-if="p.type === 'artifact'" class="tc-body" open>
                                <summary>📎 {{ p.name }}（{{ p.mime_type }}）</summary>
                                <pre>{{ p.content }}</pre>
                              </details>
                            </template>
                          </div>
                        </div>
                      </div>
                    </div>

                    <!-- assistant 子块（思考 / 正文 / 截图 / 工具行，原样式与顺序不变） -->
                    <template v-else>
                      <!-- 接力切换发言者时，在窗口内标注当前智能体名 -->
                      <div v-if="childAgentLabel(ch, it)" class="child-agent-label">{{ childAgentLabel(ch, it) }}</div>

                      <!-- 思考过程：流式中保留单行循环覆盖；停止后折叠态只显示"思考"二字，点击展开全文 -->
                      <div v-if="(ch.msg.reasoning || '').trim()" class="think-stream clickable"
                           :class="{ open: thinkIsOpen(ch.idx) }"
                           @click.stop="toggleThink(ch.idx)"
                           :title="thinkIsOpen(ch.idx) ? '点击收起思考过程' : '点击展开思考过程'">
                        <span class="think-icon">🧠</span>
                        <span class="think-text">{{ isStreamingMsg(ch.msg) ? cyclicWindow(stripContinueTag(ch.msg.reasoning), 70) : '思考' }}</span>
                        <span v-if="isStreamingMsg(ch.msg)" class="think-cursor">▌</span>
                        <span class="think-chev">{{ thinkIsOpen(ch.idx) ? '▾' : '▸' }}</span>
                      </div>
                      <div v-if="thinkIsOpen(ch.idx) && (ch.msg.reasoning || '').trim()" class="think-full">
                        <pre class="think-full-text">{{ stripContinueTag(ch.msg.reasoning) }}</pre>
                      </div>

                      <!-- 流式正文（仅流式中，带光标）—— 始终在过程框内流式显示 -->
                      <div v-if="isStreamingMsg(ch.msg) && trimMsgContent(ch.msg.content)" class="bubble markdown" v-html="msgBubbleHtml(ch.msg)"></div>
                      <div v-else-if="isStreamingMsg(ch.msg) && !hasTools(ch.idx) && !(ch.msg.reasoning || '').trim()" class="bubble streaming-placeholder">
                        <span class="stream-cursor">▌</span>
                      </div>
                      <!-- 非末条正文（停止后留在过程框里，未被提出） -->
                      <div v-else-if="!isStreamingMsg(ch.msg) && !isPromotedMessage(ch.idx) && trimMsgContent(ch.msg.content)" class="bubble markdown" v-html="msgBubbleHtml(ch.msg)"></div>

                      <!-- 工具返回图片缩略图：覆盖本地生图（sd_webui）、浏览器截图（browser_control）、
                           桌面截图（desktop_control）、网络下载图片等所有能返回图片的插件/技能。
                           data_url 直接嵌 <img>，file 路径走 /api/fs/image 按需加载。
                           点击放大查看（复用灯箱） -->
                      <div v-if="imageRefsOfAssistant(ch.idx).length" class="share-shots">
                        <div v-for="(ref, si) in imageRefsOfAssistant(ch.idx)" :key="si" class="share-shot">
                          <img :src="resolveImgSrc(ref)" :alt="'tool result image ' + (si + 1)" loading="lazy"
                               class="share-shot-img"
                               @click.stop="(e: MouseEvent) => openLightbox(resolveImgSrc(ref), e)" />
                        </div>
                      </div>

                      <!-- 工具调用行：只在有真实 tool_call 时出现，汇总 pill + 可展开历史清单（保持原有样式与顺序） -->
                      <div v-if="hasTools(ch.idx)" class="tool-row">
                        <div class="tr-summary"
                             :class="{ open: toolBarIsOpen(ch.idx), running: latestToolInfo(ch.idx)?.running }"
                             @click="toggleToolBar(ch.idx)">
                          <span class="tr-icon" :class="{ spinning: latestToolInfo(ch.idx)?.running }">{{ latestToolInfo(ch.idx)?.running ? '⟳' : '✓' }}</span>
                          <span class="tr-summary-name">{{ latestToolInfo(ch.idx)?.name }}<template v-if="latestToolInfo(ch.idx)?.fileName"> <span class="tr-filename">（{{ formatFilePath(latestToolInfo(ch.idx)?.fileName || '', 'summary') }}）</span></template></span>
                          <span class="tr-count" v-if="latestToolInfo(ch.idx)?.total && latestToolInfo(ch.idx)!.total! > 1">共 {{ latestToolInfo(ch.idx)?.total }} 个工具</span>
                          <span class="tr-chev">{{ toolBarIsOpen(ch.idx) ? '▾' : '▸' }}</span>
                        </div>
                        <div v-if="toolBarIsOpen(ch.idx)" class="tr-items">
                          <div v-for="(t, toolIndex) in toolsOfAssistant(ch.idx).filter(t => t.role === 'tool_call')" :key="t.tool_call_id || ('tc-' + ch.idx + '-' + t.name)" class="tr-item">
                            <div class="tr-pill"
                                 :class="{ running: toolIsRunning(t), expanded: toolDetailIsOpen(ch.idx, t.tool_call_id) }"
                                 @click="toggleToolDetail(ch.idx, t.tool_call_id)">
                              <span class="tr-icon" :class="{ spinning: toolIsRunning(t) }">{{ toolIsRunning(t) ? '⟳' : '✓' }}</span>
                              <span class="tr-name">
                                <template v-if="latestToolInfo(ch.idx)?.total && (latestToolInfo(ch.idx)?.total ?? 0) > 1">
                                  {{ toolIndex + 1 }}. 
                                </template>
                                {{ toolLabel(t.name, t.args) }}<template v-if="extractToolFileName(t.name, t.args)"> <span class="tr-filename-mini">（{{ formatFilePath(extractToolFileName(t.name, t.args), 'detail') }}）</span></template>
                              </span>
                              <span class="tr-toggle">{{ toolDetailIsOpen(ch.idx, t.tool_call_id) ? '▾' : '▸' }}</span>
                            </div>
                            <div v-if="toolDetailIsOpen(ch.idx, t.tool_call_id)" class="tr-detail">
                              <div class="tr-block">
                                <div class="tr-block-title">参数</div>
                                <!-- 虚拟 pill（参数流式中）：直接流式打印累计的原始 JSON 片段（打字机效果） -->
                                <pre v-if="t.tool_call_id === '_virtual_running' && speakingTool?.streamingArgs" class="tr-streaming-args" @scroll.passive="onStreamingArgsScroll">{{ speakingTool.streamingArgs }}</pre>
                                <div v-else-if="t.tool_call_id === '_virtual_running'" class="tr-arg-streaming"><span class="ti-spinner"></span>参数流式中…</div>
                                <pre v-else-if="prettyArgs(t.args) && prettyArgs(t.args) !== '{}'">{{ prettyArgs(t.args) }}</pre>
                                <pre v-else>{{ prettyArgs(t.args) || '（无参数）' }}</pre>
                              </div>
                              <template v-for="res in toolsOfAssistant(ch.idx).filter(x => x.role === 'tool_result' && x.tool_call_id === t.tool_call_id)" :key="res.tool_call_id + '-r'">
                                <div class="tr-block"><div class="tr-block-title">📥 结果</div><pre>{{ prettyResult(res.result) }}</pre></div>
                              </template>
                              <div v-if="toolIsRunning(t)" class="tr-block running">
                                <span class="ti-spinner"></span><span>执行中…{{ speakingTool?.phase === 'args' ? '（参数流式中）' : '' }}</span>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </template>

                  </template>
                </div>
              </div>

              <!-- 提出来：本轮末条带正文的 assistant（停止后）作为正常气泡原样渲染 -->
              <div v-if="turnPromotedMsg(it)" class="bubble markdown promoted-bubble" v-html="msgBubbleHtml(turnPromotedMsg(it))"></div>
            </div>
          </div>
        </template>
      </div>

      <!-- 图片浮窗：8 方向边界改大小，滚轮只缩放图片不动窗口 -->
      <Teleport to="body">
        <div v-if="lightboxOpen && lightboxSrc"
             class="lb-float"
             :style="{ left: lightboxX + 'px', top: lightboxY + 'px', width: lightboxW + 'px', height: lightboxH + 'px' }">
          <div class="lb-titlebar" @mousedown="onLbDragStart">
            <span class="lb-titlebar-icon">🖼</span>
            <span class="lb-titlebar-text">图片预览 <span class="lb-zoom-pct">{{ Math.round(lbScale * 100) }}%</span></span>
            <button class="lb-close" type="button" @mousedown.stop @click.stop="closeLightbox" title="关闭 (Esc)">✕</button>
          </div>
          <div class="lb-body" ref="lbBodyRef"
               @wheel="onLbWheel" @mousedown="onLbPanStart" @dblclick="onLbBodyDblClick"
               title="滚轮缩放（鼠标为锚）；按住左键拖拽平移图片；双击自适应窗口">
            <img :src="lightboxSrc" alt="image preview" class="lb-img"
                 :style="{ transform: lbImgTransform }"
                 @load="onLightboxImgLoad" draggable="false" />
          </div>
          <!-- 八方向 resize handles -->
          <div class="lb-edge lb-n"  @mousedown.stop="onLbResizeStart($event, 'n')"  title="上"></div>
          <div class="lb-edge lb-s"  @mousedown.stop="onLbResizeStart($event, 's')"  title="下"></div>
          <div class="lb-edge lb-e"  @mousedown.stop="onLbResizeStart($event, 'e')"  title="右"></div>
          <div class="lb-edge lb-w"  @mousedown.stop="onLbResizeStart($event, 'w')"  title="左"></div>
          <div class="lb-corner lb-nw" @mousedown.stop="onLbResizeStart($event, 'nw')" title="左上"></div>
          <div class="lb-corner lb-ne" @mousedown.stop="onLbResizeStart($event, 'ne')" title="右上"></div>
          <div class="lb-corner lb-sw" @mousedown.stop="onLbResizeStart($event, 'sw')" title="左下"></div>
          <div class="lb-corner lb-se" @mousedown.stop="onLbResizeStart($event, 'se')" title="右下"></div>
        </div>
      </Teleport>

      <!-- 聊天附件文件预览器（复用已有 FileViewer 组件） -->
      <FileViewer :canvas-id="null" :file="chatViewerFile" @close="chatViewerFile = null" />

      <div class="chat-input" :class="{ 'drop-hover': chatDropHover }"
           @dragenter.prevent="onChatDragEnter" @dragover.prevent
           @dragleave.prevent="onChatDragLeave" @drop.prevent="onChatDrop">
        <div class="row1">
          <div class="agent-dd">
            <button type="button" class="agent-dd-trigger" ref="agentTriggerEl" @click.stop="toggleAgentDd"
                    :title="currentAgentName()">
              <span class="dd-icon">🧠</span>
              <span class="dd-name">{{ currentAgentName() }}</span>
              <span class="dd-chev" :class="{ open: agentDdOpen }">▾</span>
            </button>
            <Teleport to="body">
              <div v-if="agentDdOpen" class="agent-dd-pop" ref="ddPopEl" :style="ddPosStyle" @click.stop>
                <div class="dd-cols-wrap" ref="ddScrollEl"
                     @scroll.passive="onDdScroll"
                     @wheel="onDdWheel">
                  <div class="dd-col" v-for="col in agentGroupsByColumn" :key="col.id">
                    <div class="dd-col-head">
                      <span class="dd-col-name">{{ col.name }}</span>
                      <span class="dd-col-count">{{ col.agents.length }}</span>
                    </div>
                    <div class="dd-col-list">
                      <div v-for="a in col.agents" :key="a.id"
                           class="dd-item"
                           :class="{ active: currentAgentId === a.id }"
                           @click="selectAgent(a.id)"
                           :title="a.name">
                        <span class="dd-item-dot"></span>
                        <span class="dd-item-name">{{ a.name }}</span>
                      </div>
                      <div v-if="!col.agents.length" class="dd-empty">—</div>
                    </div>
                  </div>
                </div>
                <div v-if="ddNeedsSlider" class="dd-slider">
                  <div class="dd-track" @mousedown="onTrackDown">
                    <div class="dd-thumb" :style="ddThumbStyle"
                         @mousedown.stop="onThumbDown"></div>
                  </div>
                </div>
              </div>
            </Teleport>
          </div>
          <!-- 模型快速切换下拉（接口 + 具体模型，作用于当前选中智能体） -->
          <div class="model-dd">
            <button type="button" class="agent-dd-trigger model-dd-trigger" ref="modelTriggerEl"
                    :disabled="!currentAgent"
                    :title="currentAgent ? '切换当前智能体使用的模型' : '请先选择智能体'"
                    @click.stop="toggleModelDd">
              <span class="dd-icon">🔌</span>
              <span class="dd-name">{{ currentModelLabel() }}</span>
              <span class="dd-chev" :class="{ open: modelDdOpen }">▾</span>
            </button>
            <Teleport to="body">
              <div v-if="modelDdOpen" class="agent-dd-pop model-dd-pop" ref="modelPopEl" :style="modelPosStyle" @click.stop>
                <div v-if="!models.list.length" class="dd-empty">暂无模型接口，请到「设置」页配置</div>
                <div v-else class="model-iface-list">
                  <template v-for="mi in models.list" :key="mi.id">
                    <div class="model-iface-head">
                      <span class="model-iface-name" :title="mi.name">{{ mi.name }}</span>
                      <span class="dd-col-count">{{ ifaceModelList(mi).length }}</span>
                    </div>
                    <div v-for="mid in ifaceModelList(mi)" :key="mid"
                         class="dd-item model-item" :class="{ active: isModelActive(mi.id, mid) }"
                         @click="selectModel(mi.id, mid)">
                      <span class="dd-item-dot"></span>
                      <span class="dd-item-name">{{ mid }}</span>
                      <span v-if="mi.default_model === mid" class="model-default-tag">默认</span>
                    </div>
                  </template>
                </div>
              </div>
            </Teleport>
          </div>
          <span class="cur-title">{{ conv ? convDisplayName(conv) : '— 未选择 —' }}</span>
        </div>
        <!-- 附件预览区：从电脑拖入的文件（上传中/已就绪/失败） -->
        <div v-if="attachments.length" class="attachments-bar">
          <div v-for="a in attachments" :key="a.id"
               class="att-card" :class="{ error: a.status === 'error', uploading: a.status === 'uploading' }">
            <button type="button" class="att-remove" @click="removeAttachment(a.id)" :title="'移除 ' + a.name">✕</button>
            <!-- 图片：缩略图预览 -->
            <template v-if="isImageFile(a.name)">
              <div class="att-thumb">
                <img v-if="a.previewUrl" :src="a.previewUrl" :alt="a.name" />
                <span v-if="a.status === 'uploading'" class="att-spinner"></span>
                <span v-if="a.status === 'error'" class="att-err-ico">⚠️</span>
              </div>
              <div class="att-info">
                <div class="att-name" :title="a.name">{{ a.name }}</div>
                <div class="att-meta">
                  <span>{{ formatFileSize(a.size) }}</span>
                  <span v-if="a.status === 'uploading'" class="att-loading">上传中…</span>
                  <span v-if="a.status === 'error'" class="att-err-msg">{{ a.errorMsg || '上传失败' }}</span>
                </div>
              </div>
            </template>
            <!-- 非图片：文件卡片（带图标） -->
            <template v-else>
              <div class="att-file-icon">{{ fileIconOf(a.name) }}</div>
              <div class="att-info">
                <div class="att-name" :title="a.name">{{ a.name }}</div>
                <div class="att-meta">
                  <span>{{ formatFileSize(a.size) }}</span>
                  <span v-if="a.status === 'uploading'" class="att-loading">上传中…</span>
                  <span v-if="a.status === 'error'" class="att-err-msg">{{ a.errorMsg || '上传失败' }}</span>
                </div>
              </div>
            </template>
          </div>
        </div>
        <div class="row2">
          <textarea ref="taRef" class="input input-ta" v-model="input" placeholder="输入消息或指令..." @keydown.enter.exact.prevent="send" @focus="canvasesCollapsed = true"></textarea>
          <button v-if="settings.asrAvailable && settings.asrEnabled" class="btn voice-btn" :class="{ recording: isRecording }" @click="toggleVoiceInput" :title="isRecording ? '停止同声传译' : '同声传译'" aria-label="语音输入">
            <span class="voice-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18" aria-hidden="true">
                <rect x="9" y="3" width="6" height="10" rx="3" />
                <path d="M6 11 a6 6 0 0 0 12 0" />
                <line x1="12" y1="17" x2="12" y2="20" />
                <line x1="8" y1="20" x2="16" y2="20" />
              </svg>
            </span>
          </button>
          <button v-if="!convs.streaming" class="btn send" @click="send" aria-label="发送消息">发送</button>
          <button v-else class="btn send stop-btn" @click="sendStop" aria-label="停止输出">
            <span class="stop-icon" aria-hidden="true"></span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.right-panel { display: flex; flex-direction: column; overflow: hidden; }
.canvases-pane { padding: 4px 0; }
/* 画布标题与对话标题使用完全相同的 padding，确保折叠按钮左右对齐 */
.canvases-pane > .section-title { padding: 6px 8px 4px; gap: 6px; }
.cv-list { padding: 2px 4px; max-height: 180px; overflow-y: auto; }
.cv-item { padding: 6px 10px; border-radius: 8px; font-size: calc(12px * var(--font-scale)); cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 6px; position: relative; }
.cv-item:hover { background: rgba(var(--surface-rgb), 0.05); }
.cv-item.active { background: rgba(var(--accent-rgb), 0.15); border: 1px solid rgba(var(--accent-rgb), 0.25); }
.cv-item[draggable="true"] { cursor: grab; }
.cv-item[draggable="true"]:active { cursor: grabbing; }
.cv-item.dragging { opacity: .4; }
/* 拖拽落点指示线：上/下各一条高亮边 */
.cv-item.drop-before { box-shadow: 0 -2px 0 0 var(--accent), 0 1px 0 0 rgba(var(--accent-rgb), 0.25); }
.cv-item.drop-after { box-shadow: 0 2px 0 0 var(--accent), 0 -1px 0 0 rgba(var(--accent-rgb), 0.25); }
.cv-item .cv-label { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.cv-item .cv-label:hover::after { content: ' ✎'; opacity: .35; font-size: calc(11px * var(--font-scale)); }
.cv-item .cv-count { font-size: calc(11px * var(--font-scale)); flex-shrink: 0; font-family: Consolas, monospace; padding: 1px 6px; border-radius: 999px; background: rgba(var(--accent2-rgb), 0.15); color: var(--purple-ink); border: 1px solid rgba(var(--accent2-rgb), 0.3); line-height: 1.5; font-weight: 600; }
.cv-item .cv-running { flex-shrink: 0; font-size: calc(10px * var(--font-scale)); color: #4ade80; line-height: 1; animation: cvPulse 1.2s ease-in-out infinite; }
@keyframes cvPulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .35; transform: scale(.8); } }
.cv-item .del { opacity: 0; font-size: calc(14px * var(--font-scale)); flex-shrink: 0; color: rgba(var(--text-rgb), 0.7); }
.cv-item:hover .del { opacity: .6; }
.cv-item .del:hover { opacity: 1; color: #ff9090; }
.cv-rename { width: 100%; font-size: calc(12px * var(--font-scale)); padding: 2px 6px; border-radius: 4px; background: rgba(var(--shadow-rgb), 0.3); border: 1px solid rgba(var(--accent-rgb), 0.4); color: rgb(var(--text-rgb)); }
.divider { height: 1px; background: rgba(var(--surface-rgb), 0.08); margin: 4px 10px; }

.chat-pane { flex: 1; display: flex; flex-direction: column; min-height: 0; position: relative; }
.chat-header { flex-shrink: 0; display: flex; flex-direction: column; min-height: 0; border-bottom: 1px solid rgba(var(--surface-rgb), 0.06); }
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
  border: 1px solid rgba(var(--surface-rgb), 0.1);
  border-right: none;
  border-radius: 10px 0 0 10px;
  box-shadow: -8px 0 24px rgba(var(--shadow-rgb), 0.4);
}
.conv-float-head { flex-shrink: 0; display: flex; justify-content: flex-start; align-items: center; gap: 6px; padding: 8px 8px 4px; min-width: 0; }
.conv-title { justify-content: flex-start !important; gap: 6px; padding: 6px 8px 4px; flex-wrap: nowrap; min-width: 0; flex-shrink: 0; }
.conv-title-text { flex-shrink: 0; }
.flex-spacer { flex: 1; min-width: 0; }
.collapse-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 6px; color: rgba(var(--text-rgb), 0.7); font-size: calc(11px * var(--font-scale)); cursor: pointer; line-height: 1; }
.collapse-btn:hover { background: rgba(var(--surface-rgb), 0.08); color: rgb(var(--text-rgb)); }
/* 排序方向切换按钮：↓ 新在上（倒序）／↑ 旧在上（顺序） */
.sort-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 6px; color: var(--accent); font-size: calc(12px * var(--font-scale)); cursor: pointer; line-height: 1; }
.sort-btn:hover { background: rgba(var(--accent-rgb), 0.18); color: #dce8ff; }
.conv-count {
  font-size: calc(11px * var(--font-scale));
  font-family: Consolas, monospace;
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(var(--accent-rgb), 0.15);
  color: var(--accent-ink);
  border: 1px solid rgba(var(--accent-rgb), 0.3);
  line-height: 1.5;
  font-weight: 600;
}
.new-conv-bar { display: flex; gap: 4px; flex-shrink: 0; padding: 0; margin-left: auto; }
.btn.purple { background: rgba(var(--accent2-rgb), 0.15); border-color: rgba(var(--accent2-rgb), 0.35); color: var(--purple-ink); }
.btn.purple:hover { background: rgba(var(--accent2-rgb), 0.25); }
.btn.danger { background: rgba(255,80,80,0.18); border-color: rgba(255,80,80,0.4); color: var(--red-ink); }
.btn.danger:hover { background: rgba(255,80,80,0.3); }
.btn.danger:disabled { opacity: .4; cursor: not-allowed; }
.sel-bar { flex-shrink: 0; display: flex; align-items: center; gap: 6px; padding: 4px 8px; background: rgba(255,80,80,0.08); border-top: 1px dashed rgba(255,80,80,0.25); border-bottom: 1px dashed rgba(255,80,80,0.25); }
.sel-count { font-size: calc(11px * var(--font-scale)); color: var(--red-ink); flex-shrink: 0; }
.conv-list-wrap { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 4px 6px 6px; }
.conv-item { padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: calc(12px * var(--font-scale)); display: flex; align-items: center; gap: 6px; margin-bottom: 2px; position: relative; }
/* 对话序号：纯文本小字，低调不抢行内其它标签的视觉 */
.conv-item .seq { flex-shrink: 0; min-width: 18px; text-align: right; color: rgba(var(--text-rgb), 0.45); font-family: Consolas, monospace; font-size: calc(11.5px * var(--font-scale)); }
.conv-item.active .seq { color: #8ab8ff; }
.conv-item:hover .seq { color: rgba(var(--text-rgb), 0.75); }
.conv-item:hover { background: rgba(var(--surface-rgb), 0.05); }
.conv-item.active { background: rgba(var(--accent-rgb), 0.15); }
.conv-item.checked { background: rgba(255,80,80,0.12); border: 1px solid rgba(255,80,80,0.35); }
.conv-item .chk { flex-shrink: 0; font-size: calc(13px * var(--font-scale)); opacity: .85; }
.conv-item .conv-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.conv-item .conv-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-item .conv-time { font-size: calc(10px * var(--font-scale)); opacity: .45; font-family: Consolas, monospace; line-height: 1.2; }
.conv-item .del { opacity: 0; font-size: calc(14px * var(--font-scale)); flex-shrink: 0; }
.conv-item:hover .del { opacity: .6; }
.mode-tag { font-size: calc(10px * var(--font-scale)); padding: 1px 5px; border-radius: 4px; background: rgba(var(--surface-rgb), 0.1); flex-shrink: 0; }
.mode-tag.linked { background: rgba(var(--accent2-rgb), 0.2); color: var(--purple-ink); }

.chat-box { flex: 1; overflow-y: auto; padding: 10px 12px 24px; display: flex; flex-direction: column; gap: 10px; }
.no-conv { text-align: center; opacity: .5; margin: 30px auto; font-size: calc(13px * var(--font-scale)); }

/* ============ 消息容器 ============ */
.msg { display: flex; gap: 6px; max-width: 90%; }
/* 用户消息靠右（row-reverse：头像在右，泡泡在左） */
.msg.user { align-self: flex-end; flex-direction: row-reverse; }
.msg.user .bubble {
  background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.35), rgba(var(--accent2-rgb), 0.3));
  border-color: rgba(var(--accent-rgb), 0.35);
  border-top-right-radius: 4px;
}
/* 助手消息靠左，纵向堆叠（头像行 + 泡泡 + 工具行） */
.msg.assistant { align-self: flex-start; flex-direction: column; }
.msg.assistant .bubble {
  background: rgba(var(--surface-rgb), 0.06);
  border-top-left-radius: 4px;
}
.msg.assistant.streaming .bubble { border-color: rgba(var(--accent-rgb), 0.4); box-shadow: 0 0 0 1px rgba(var(--accent-rgb), 0.1); }
/* 思考过程：单行（流式打字机 / 停止后截断首行），点击展开全文 */
.think-stream {
  display: flex; align-items: center; gap: 6px;
  padding: 3px 9px; border-radius: 7px;
  background: rgba(var(--accent2-rgb), 0.08);
  border: 1px solid rgba(var(--accent2-rgb), 0.22);
  font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.65);
  max-width: 100%; min-width: 0;
}
.think-stream.clickable { cursor: pointer; transition: background .15s, border-color .15s; }
.think-stream.clickable:hover { background: rgba(var(--accent2-rgb), 0.15); border-color: rgba(var(--accent2-rgb), 0.38); }
.think-stream.open { border-color: rgba(var(--accent2-rgb), 0.42); }
.think-icon { flex-shrink: 0; font-size: calc(11px * var(--font-scale)); }
.think-text {
  flex: 1; min-width: 0;
  overflow: hidden; white-space: nowrap;
  font-style: italic; letter-spacing: 0.2px;
}
.think-cursor {
  flex-shrink: 0; color: var(--accent-2); font-weight: 600;
  animation: cursorBlink 1s steps(2) infinite;
}
.think-chev { flex-shrink: 0; font-size: calc(10px * var(--font-scale)); color: rgba(var(--accent2-rgb), 0.75); }
/* 点击展开后的思考全文：自然高度，由过程框内部滚动条统一处理滚动 */
.think-full {
  margin-top: -3px;
  padding: 6px 9px;
  background: rgba(var(--accent2-rgb), 0.05);
  border: 1px solid rgba(var(--accent2-rgb), 0.16);
  border-radius: 7px;
}
.think-full-text {
  margin: 0;
  white-space: pre-wrap; word-break: break-word;
  font-size: calc(10.5px * var(--font-scale)); line-height: 1.5;
  color: rgba(var(--text-rgb), 0.72);
  font-family: Consolas, monospace;
}

/* ============ 过程窗口：流式中展开的固定大小框，停止后折叠为 "(>过程)" 文字 ============ */
/* 折叠钮：紧贴智能体名称，无边框、纯文字，字体/字号与名称差异化 */
.proc-toggle {
  margin-left: 3px;
  padding: 0;
  border: none;
  background: none;
  font-family: Consolas, "Courier New", monospace;
  font-size: calc(9.5px * var(--font-scale));
  line-height: 1;
  letter-spacing: 0.3px;
  color: rgba(var(--text-rgb), 0.42);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  transition: color .15s, transform .15s;
}
.proc-toggle:hover { color: var(--accent-ink); transform: translateY(-1px); }
.proc-toggle.open { color: var(--purple-ink); }

/* 过程框内：接力切换发言者时标注当前智能体名（与轮首不同时显示） */
.child-agent-label {
  align-self: flex-start;
  margin: 4px 2px 0;
  font-size: calc(10px * var(--font-scale));
  line-height: 1;
  font-weight: 500;
  letter-spacing: 0.3px;
  color: rgba(var(--text-rgb), 0.5);
}

/* 过程框本体：固定高度 + 内部滚动，外观像一个小窗口 */
.process-window {
  margin-top: 5px;
  height: 260px;
  width: 100%;
  background: rgba(var(--surface-rgb), 0.028);
  border: 1px solid rgba(var(--surface-rgb), 0.08);
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.process-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 9px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  scrollbar-width: thin;
  scrollbar-color: rgba(var(--accent-rgb), 0.28) transparent;
  /* 禁用浏览器滚动锚定：上方内容（工具行展开/图片加载）变化时锚定会把视口
     从底部"顶开"，表现为跟随时底部莫名出现空隙 */
  overflow-anchor: none;
}
.process-body::-webkit-scrollbar { width: 5px; }
.process-body::-webkit-scrollbar-thumb { background: rgba(var(--accent-rgb), 0.28); border-radius: 999px; }
.process-body::-webkit-scrollbar-track { background: transparent; }

.col-wrap { display: flex; flex-direction: column; gap: 3px; }
.head-row { display: flex; align-items: center; gap: 6px; }
.avatar {
  position: relative; overflow: hidden;
  width: 24px; height: 24px; border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  font-size: calc(11px * var(--font-scale)); font-weight: 600; flex-shrink: 0; color: #fff;
  text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.3);
  box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45),
              inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32),
              inset 0 0 0 1px rgba(var(--surface-rgb), 0.08),
              0 3px 8px rgba(var(--shadow-rgb), 0.4);
}
.avatar::before {
  content: '';
  position: absolute; inset: 0;
  background:
    linear-gradient(165deg, rgba(var(--surface-rgb), 0.45) 0%, rgba(var(--surface-rgb), 0.10) 32%, rgba(var(--surface-rgb), 0) 52%),
    linear-gradient(0deg, rgba(var(--shadow-rgb), 0.24) 0%, rgba(var(--shadow-rgb), 0) 42%);
  pointer-events: none;
}
.avatar.user-avatar {
  width: 24px; height: 24px; border-radius: 7px;
  background: linear-gradient(135deg, #f97316, #ec4899);
  overflow: hidden;
}
.avatar .avatar-photo {
  width: 100%; height: 100%;
  object-fit: cover; display: block; border-radius: inherit;
}
.who-assistant { font-size: calc(11.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.75); opacity: .9; line-height: 1; }
.turn-elapsed { font-size: calc(10px * var(--font-scale)); color: rgba(var(--text-rgb), 0.4); font-family: Consolas, 'Courier New', monospace; line-height: 1.2; margin-left: 4px; align-self: center; white-space: nowrap; }
.turn-elapsed.active { color: var(--accent); }

/* ============ 消息泡泡（基础） ============ */
.bubble {
  padding: 9px 13px;
  border-radius: 10px;
  font-size: calc(12px * var(--font-scale));
  line-height: 1.6;
  white-space: pre-wrap;        /* 默认保留换行 */
  border: 1px solid rgba(var(--surface-rgb), 0.08);
  position: relative;
  transition: border-color .15s, box-shadow .15s;
}
/* 当 bubble 带 .markdown class 时，由全局样式接管排版 */
.bubble.markdown { white-space: normal; }

/* 用户消息泡泡额外样式 */
.bubble.user-bubble { color: rgba(var(--text-rgb), 0.95); }
.bubble.user-bubble.markdown a { color: #ffd088; }
.bubble.user-bubble.markdown code { background: rgba(var(--surface-rgb), 0.12); color: var(--code-ink); }
.bubble.user-bubble.markdown blockquote { background: rgba(var(--surface-rgb), 0.08); border-left-color: rgba(255,208,136,0.5); }

/* 流式占位泡泡（只有光标闪烁） */
.bubble.streaming-placeholder {
  background: rgba(var(--surface-rgb), 0.04);
  border-style: dashed;
  border-color: rgba(var(--accent-rgb), 0.3);
  padding: 6px 12px;
  min-height: 14px;
  line-height: 1;
}
.bubble.streaming-placeholder .stream-cursor { color: var(--accent); }

/* 空输出提示 */
.bubble.empty-bubble {
  background: transparent;
  border-style: dashed;
  border-color: rgba(var(--surface-rgb), 0.12);
  color: rgba(var(--text-rgb), 0.4);
  font-style: italic;
  font-size: calc(11px * var(--font-scale));
  padding: 6px 12px;
}

.chat-input { padding: 8px 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); display: flex; flex-direction: column; gap: 6px; transition: background .15s, box-shadow .15s; }
/* 拖拽文件悬停时的高亮提示 */
.chat-input.drop-hover { background: rgba(var(--accent-rgb), 0.1); box-shadow: inset 0 0 0 1px rgba(var(--accent-rgb), 0.45); }

/* ============ 附件预览区 ============ */
.attachments-bar {
  display: flex; flex-wrap: wrap; gap: 6px;
  max-height: 120px; overflow-y: auto; padding: 2px 0;
}
.att-card {
  position: relative;
  display: flex; align-items: center; gap: 8px;
  padding: 6px 28px 6px 6px;
  background: rgba(var(--surface-rgb), 0.08);
  border: 1px solid rgba(var(--accent-rgb), 0.25);
  border-radius: 8px;
  min-width: 180px; max-width: 260px;
}
.att-card.uploading { border-color: rgba(251, 191, 36, 0.5); background: rgba(251, 191, 36, 0.06); }
.att-card.error { border-color: rgba(239, 68, 68, 0.6); background: rgba(239, 68, 68, 0.08); }
.att-remove {
  position: absolute; top: 2px; right: 2px;
  width: 18px; height: 18px;
  border: none; border-radius: 50%;
  background: rgba(var(--shadow-rgb), 0.4);
  color: rgba(var(--text-rgb), 0.7);
  font-size: 11px; line-height: 1; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.att-remove:hover { background: rgba(239, 68, 68, 0.7); color: #fff; }
.att-thumb {
  flex-shrink: 0; width: 44px; height: 44px;
  border-radius: 6px; overflow: hidden;
  background: rgba(var(--shadow-rgb), 0.25);
  display: flex; align-items: center; justify-content: center;
  position: relative;
}
.att-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.att-file-icon {
  flex-shrink: 0; width: 44px; height: 44px;
  border-radius: 6px;
  background: rgba(var(--accent-rgb), 0.15);
  display: flex; align-items: center; justify-content: center;
  font-size: 22px;
}
.att-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.att-name {
  font-size: calc(11px * var(--font-scale));
  color: rgb(var(--text-rgb));
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.att-meta {
  font-size: calc(10px * var(--font-scale));
  opacity: .6;
  display: flex; gap: 6px; align-items: center;
}
.att-meta .att-loading { color: #fbbf24; }
.att-meta .att-err-msg { color: #ef4444; }
.att-spinner {
  position: absolute; inset: 0;
  background: rgba(0,0,0,0.45);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 10px;
  animation: attPulse 1.2s ease-in-out infinite;
}
.att-err-ico {
  position: absolute; inset: 0;
  background: rgba(239, 68, 68, 0.4);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
}
@keyframes attPulse { 0%, 100% { opacity: .4; } 50% { opacity: 1; } }

/* ============ 消息气泡里的附件（文件卡片 + 图片缩略图） ============ */
.chat-attach {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 10px; margin: 4px 2px;
  background: rgba(var(--accent-rgb), 0.08);
  border: 1px solid rgba(var(--accent-rgb), 0.3);
  border-radius: 8px;
  cursor: pointer;
  user-select: none;
  transition: background .12s, border-color .12s, transform .08s;
  vertical-align: middle;
}
.chat-attach:hover {
  background: rgba(var(--accent-rgb), 0.15);
  border-color: rgba(var(--accent-rgb), 0.55);
  transform: translateY(-1px);
}
.chat-attach:active { transform: translateY(0); }
.chat-attach .chat-att-icon {
  flex-shrink: 0; font-size: 20px; line-height: 1;
  width: 32px; height: 32px; border-radius: 6px;
  background: rgba(var(--accent-rgb), 0.12);
  display: flex; align-items: center; justify-content: center;
}
.chat-attach .chat-att-info {
  display: flex; flex-direction: column; gap: 1px; min-width: 0;
}
.chat-attach .chat-att-name {
  font-size: calc(12px * var(--font-scale));
  max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: rgb(var(--text-rgb));
}
.chat-attach .chat-att-size {
  font-size: calc(10px * var(--font-scale)); opacity: .6;
}

.chat-img-wrap {
  display: inline-block; cursor: zoom-in; margin: 4px 2px;
  border-radius: 8px; overflow: hidden;
  background: rgba(var(--shadow-rgb), 0.1);
  vertical-align: middle;
  user-select: none;
  transition: transform .1s, box-shadow .1s;
  line-height: 0;
}
.chat-img-wrap:hover {
  transform: scale(1.02);
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
.chat-img-wrap .chat-att-img {
  max-width: 120px; max-height: 90px; object-fit: cover;
  display: block;
  border-radius: 8px;
}
@media (max-width: 700px) {
  .chat-img-wrap .chat-att-img { max-width: 80px; max-height: 64px; }
}

.row1 { display: flex; gap: 6px; align-items: center; }
.cur-title { font-size: calc(11px * var(--font-scale)); opacity: .6; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ============ 智能体分组下拉（一组一列） ============ */
.agent-dd { position: relative; flex-shrink: 0; display: inline-flex; align-items: center; }
.agent-dd-trigger {
  display: inline-flex; align-items: center; gap: 5px;
  height: 28px; padding: 0 10px;
  font-size: calc(12px * var(--font-scale)); font-family: inherit;
  color: rgba(var(--text-rgb), 0.92);
  background: rgba(var(--accent-rgb), 0.10);
  border: 1px solid rgba(var(--accent-rgb), 0.28);
  border-radius: 6px;
  cursor: pointer;
  max-width: 250px;
  min-width: 0;
  transition: background .15s, border-color .15s;
}
.agent-dd-trigger:hover { background: rgba(var(--accent-rgb), 0.18); border-color: rgba(var(--accent-rgb), 0.45); }
.agent-dd-trigger .dd-icon { font-size: calc(12px * var(--font-scale)); flex-shrink: 0; }
.agent-dd-trigger .dd-name {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 200px;
}
.agent-dd-trigger .dd-chev {
  font-size: calc(10px * var(--font-scale)); color: rgba(var(--text-rgb), 0.5);
  flex-shrink: 0; transition: transform .15s;
}
.agent-dd-trigger .dd-chev.open { transform: rotate(180deg); color: var(--accent-ink); }

.agent-dd-pop {
  position: fixed;
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: min(420px, calc(100vh - 180px));
  background: rgba(var(--node-bg-rgb), 0.98);
  border: 1px solid rgba(var(--surface-rgb), 0.14);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.55);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  padding: 6px;
  animation: ddFadeIn .12s ease-out;
}
@keyframes ddFadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

/* 横向滚动列容器：隐藏原生滚动条，由底部滑杆接管 */
.dd-cols-wrap {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  overflow-y: hidden;
  flex: 1;
  min-height: 0;
  /* 关键：允许在交叉轴上收缩到比列内容更窄，从而触发 overflow-x */
  min-width: 0;
  padding: 2px 2px 4px;
  scrollbar-width: none;        /* Firefox */
  -ms-overflow-style: none;     /* IE10+ */
}
.dd-cols-wrap::-webkit-scrollbar { display: none; }

.dd-col {
  flex: 1 1 192px;
  max-width: none;
  min-width: 160px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: rgba(var(--surface-rgb), 0.06);
  border: 1px solid rgba(var(--surface-rgb), 0.08);
  border-radius: 8px;
  overflow: hidden;
}
.dd-col-head {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  padding: 5px 8px 4px;
  background: rgba(var(--accent-rgb), 0.10);
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.06);
  flex-shrink: 0;
}
.dd-col-name {
  font-size: calc(11.5px * var(--font-scale)); font-weight: 600; color: var(--accent-ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.dd-col-count {
  font-size: calc(10px * var(--font-scale)); font-family: Consolas, monospace;
  background: rgba(var(--surface-rgb), 0.08); color: rgba(var(--text-rgb), 0.7);
  padding: 1px 6px; border-radius: 999px; line-height: 1.4;
  flex-shrink: 0;
}
.dd-col-list {
  overflow-y: auto;
  padding: 3px;
  max-height: 340px;
  display: flex; flex-direction: column; gap: 2px;
}
.dd-item {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 7px; border-radius: 5px;
  font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.88);
  cursor: pointer;
  transition: background .12s;
}
.dd-item:hover { background: rgba(var(--accent-rgb), 0.18); color: rgb(var(--text-rgb)); }
.dd-item.active {
  background: rgba(var(--accent-rgb), 0.28);
  color: rgb(var(--text-rgb));
  box-shadow: inset 0 0 0 1px rgba(var(--accent-rgb), 0.4);
}
.dd-item-dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  flex-shrink: 0;
}
.dd-item-name {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1; min-width: 0;
}
.dd-empty {
  padding: 8px; font-size: calc(11px * var(--font-scale)); opacity: .35;
  text-align: center;
}

/* ============ 底部横向滑杆 ============ */
.dd-slider {
  flex-shrink: 0;
  padding: 4px 6px 2px;
  margin-top: 2px;
  border-top: 1px solid rgba(var(--surface-rgb), 0.06);
}
.dd-track {
  position: relative;
  height: 8px;
  background: rgba(var(--surface-rgb), 0.08);
  border: 1px solid rgba(var(--surface-rgb), 0.06);
  border-radius: 999px;
  cursor: pointer;
  user-select: none;
}
.dd-track:hover { background: rgba(var(--surface-rgb), 0.12); }
.dd-thumb {
  position: absolute;
  top: 0; left: 0;
  height: 8px;
  min-width: 28px;
  border-radius: 999px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: 0 1px 4px rgba(var(--shadow-rgb), 0.4);
  cursor: grab;
  transition: filter .15s;
}
.dd-thumb:hover { filter: brightness(1.12); }
.dd-thumb:active { cursor: grabbing; filter: brightness(1.2); }
/* ============ 模型快速切换下拉 ============ */
.model-dd { position: relative; flex-shrink: 0; display: inline-flex; align-items: center; }
.model-dd-trigger { color: rgba(var(--text-rgb), 0.92); background: rgba(var(--accent2-rgb), 0.10); border-color: rgba(var(--accent2-rgb), 0.28); max-width: 200px; }
.model-dd-trigger:hover { background: rgba(var(--accent2-rgb), 0.18); border-color: rgba(var(--accent2-rgb), 0.45); }
.model-dd-trigger:disabled { opacity: .5; cursor: not-allowed; }
.model-dd-trigger:disabled:hover { background: rgba(var(--accent2-rgb), 0.10); border-color: rgba(var(--accent2-rgb), 0.28); }
.model-dd-pop { max-height: min(420px, calc(100vh - 140px)); }
.model-iface-list { overflow-y: auto; max-height: min(400px, calc(100vh - 160px)); display: flex; flex-direction: column; gap: 2px; }
.model-iface-head {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  padding: 6px 8px 4px; margin-top: 2px;
  border-top: 1px solid rgba(var(--surface-rgb), 0.06);
}
.model-iface-head:first-child { margin-top: 0; border-top: none; }
.model-iface-name {
  font-size: calc(11.5px * var(--font-scale)); font-weight: 600; color: var(--purple-ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.model-item { padding-left: 14px; }
.model-item.active { background: rgba(var(--accent2-rgb), 0.24); box-shadow: inset 0 0 0 1px rgba(var(--accent2-rgb), 0.4); }
.model-default-tag {
  flex-shrink: 0; font-size: calc(10px * var(--font-scale)); color: var(--purple-ink);
  background: rgba(var(--accent2-rgb), 0.18); border: 1px solid rgba(var(--accent2-rgb), 0.3);
  padding: 0 5px; border-radius: 999px; line-height: 1.5;
}

.row2 { display: flex; gap: 6px; align-items: flex-end; }
.voice-btn { flex-shrink: 0; width: 40px; height: 40px; padding: 0; min-width: 40px; display: inline-flex; align-items: center; justify-content: center; font-size: 18px; border-radius: 8px; background: rgba(var(--surface-rgb), 0.08); border: 1px solid rgba(var(--surface-rgb), 0.12); color: var(--text); transition: all .2s; }
.voice-btn:hover:not(:disabled) { background: rgba(var(--accent-rgb), 0.15); border-color: rgba(var(--accent-rgb), 0.3); }
.voice-btn:disabled { opacity: .5; cursor: wait; }
.voice-btn.recording { background: rgba(255,80,80,0.2); border-color: rgba(255,80,80,0.5); animation: voicePulse 1.2s ease-in-out infinite; }
.voice-btn.recording .voice-icon { animation: voiceIconBounce .6s ease-in-out infinite; }
.voice-loading { width: 16px; height: 16px; border: 2px solid rgba(var(--accent-rgb), 0.3); border-top-color: var(--accent-ink); border-radius: 50%; animation: voiceSpin .8s linear infinite; }
@keyframes voicePulse { 0%,100% { box-shadow: 0 0 0 0 rgba(255,80,80,0.4); } 50% { box-shadow: 0 0 0 6px rgba(255,80,80,0); } }
@keyframes voiceIconBounce { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }
@keyframes voiceSpin { to { transform: rotate(360deg); } }
.sm { padding: 3px 8px; font-size: calc(11px * var(--font-scale)); }
.input-ta { min-height: 44px; max-height: 100px; resize: vertical; font-family: inherit; }
.msg.assistant.empty { display: none; }
.tool-card { max-width: 95%; margin: 4px 0; align-self: flex-start; border-radius: 10px; padding: 8px 12px; border: 1px solid rgba(var(--surface-rgb), 0.1); font-size: calc(12px * var(--font-scale)); }
/* 连续 a2a 信封组：一级展开后逐条列出（嵌套缩进 + 左侧竖线提示层级） */
.a2a-group-wrap { display: flex; flex-direction: column; gap: 4px; margin: 4px 0; min-width: 0; }
.a2a-group-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.a2a-group-body:not(.single) { margin-left: 4px; padding-left: 10px; border-left: 2px solid rgba(var(--accent2-rgb), 0.35); }
.a2a-group.open { border-color: rgba(var(--accent2-rgb), 0.65); }
.tc-card { background: rgba(var(--accent-rgb), 0.1); border-color: rgba(var(--accent-rgb), 0.25); }
.tr-card { background: rgba(100,220,160,0.08); border-color: rgba(100,220,160,0.25); align-self: flex-start; }
.a2a-card { background: rgba(var(--accent2-rgb), 0.12); border-color: rgba(var(--accent2-rgb), 0.4); }
.a2a-card .tc-title { color: var(--purple-ink); }
.a2a-route { font-weight: 600; color: var(--purple-ink); text-transform: none; letter-spacing: 0; font-size: calc(12px * var(--font-scale)); }
.a2a-state { margin-left: auto; font-family: Consolas, monospace; font-size: calc(10px * var(--font-scale)); background: rgba(var(--accent2-rgb), 0.2); color: var(--purple-ink); padding: 1px 6px; border-radius: 3px; text-transform: none; }
.a2a-text { font-size: calc(12px * var(--font-scale)); line-height: 1.55; white-space: pre-wrap; margin-top: 6px; max-height: 180px; overflow-y: auto; }
.a2a-art summary { cursor: pointer; font-size: calc(11px * var(--font-scale)); opacity: .7; margin-top: 6px; user-select: none; }
.a2a-art summary::-webkit-details-marker { display: none; }
.tc-head { display: flex; gap: 6px; align-items: center; font-size: calc(11px * var(--font-scale)); text-transform: uppercase; letter-spacing: 0.5px; color: rgba(var(--text-rgb), 0.6); }
.tc-icon { font-size: calc(14px * var(--font-scale)); }
.tc-title { font-weight: 600; }
.tc-name { font-family: Consolas, monospace; font-size: calc(12px * var(--font-scale)); background: rgba(var(--shadow-rgb), 0.3); padding: 2px 8px; border-radius: 4px; color: var(--accent-ink); text-transform: none; letter-spacing: 0; margin-left: 4px; }
.tc-args { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.arg-chip { background: rgba(var(--shadow-rgb), 0.25); border: 1px solid rgba(var(--surface-rgb), 0.08); padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale)); display: inline-flex; gap: 2px; }
.ak { color: var(--purple-ink); }
.av { color: rgb(var(--text-rgb)); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-error { color: #ff9090; background: rgba(255,80,80,0.15); padding: 1px 6px; border-radius: 3px; font-size: calc(10px * var(--font-scale)); }
.tr-body { font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale)); background: rgba(var(--shadow-rgb), 0.2); padding: 6px 10px; border-radius: 6px; margin: 6px 0 0; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; color: var(--green-ink); }

/* —— 助手消息下的工具行 —— */
.tool-row { margin-top: 5px; display: flex; flex-direction: column; gap: 3px; }
.tr-summary { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 9px; background: rgba(var(--accent-rgb), 0.08); border: 1px solid rgba(var(--accent-rgb), 0.22); cursor: pointer; font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.72); user-select: none; width: fit-content; max-width: 100%; transition: background .15s; }
.tr-summary:hover { background: rgba(var(--accent-rgb), 0.18); }
.tr-summary:hover .tr-chev { color: var(--accent-ink); }
.tr-summary.open { background: rgba(var(--accent-rgb), 0.22); border-color: rgba(var(--accent-rgb), 0.45); color: rgba(var(--text-rgb), 0.95); }
.tr-summary.open .tr-chev { color: var(--accent-ink); }
.tr-summary.running { border-color: rgba(var(--accent-rgb), 0.55); background: rgba(var(--accent-rgb), 0.12); color: var(--accent-ink); }
.tr-summary.running .tr-icon { color: var(--accent-ink); }
.tr-summary-name { font-weight: 500; color: rgba(var(--text-rgb), 0.92); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
.tr-status { font-size: calc(10px * var(--font-scale)); opacity: .6; flex-shrink: 0; }
.tr-items { display: flex; flex-direction: column; gap: 3px; padding-left: 2px; }
.tr-ico { font-size: calc(11px * var(--font-scale)); }
.tr-count { font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.65); margin-left: auto; padding-right: 4px; }
.tr-label { opacity: .75; }
.tr-list { display: inline-flex; gap: 2px; flex-wrap: wrap; max-width: 220px; overflow: hidden; }
.tr-chip { font-size: calc(10px * var(--font-scale)); padding: 1px 5px; border-radius: 4px; background: rgba(var(--shadow-rgb), 0.28); color: rgba(var(--text-rgb), 0.7); white-space: nowrap; }
.tr-chip.more { background: rgba(var(--accent-rgb), 0.2); color: var(--accent-ink); }
.tr-chev { color: rgba(var(--text-rgb), 0.4); font-size: calc(10px * var(--font-scale)); margin-left: 3px; transition: color .15s; }
.tr-item { display: flex; flex-direction: column; gap: 2px; }
.tr-pill { display: flex; align-items: center; gap: 5px; padding: 2px 7px; border-radius: 7px; background: rgba(var(--shadow-rgb), 0.18); border: 1px solid rgba(var(--surface-rgb), 0.08); cursor: pointer; font-size: calc(10.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.85); user-select: none; }
.tr-pill:hover { background: rgba(var(--shadow-rgb), 0.28); border-color: rgba(var(--accent-rgb), 0.3); }
.tr-pill.running { border-color: rgba(var(--accent-rgb), 0.55); background: rgba(var(--accent-rgb), 0.1); color: var(--accent-ink); }
.tr-pill.expanded { border-color: rgba(var(--accent-rgb), 0.4); }
.tr-icon { width: 11px; text-align: center; font-size: calc(10px * var(--font-scale)); transition: transform .3s; }
.tr-icon.spinning { display: inline-block; animation: tiSpin .8s linear infinite; }
@keyframes tiSpin { to { transform: rotate(360deg); } }
.tr-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tr-toggle { color: rgba(var(--text-rgb), 0.4); font-size: calc(10px * var(--font-scale)); flex-shrink: 0; }
.tr-detail { padding-left: 12px; border-left: 2px solid rgba(var(--accent-rgb), 0.2); margin-top: 2px; display: flex; flex-direction: column; gap: 3px; }
.tr-block { padding: 3px 7px; border-radius: 5px; background: rgba(var(--shadow-rgb), 0.25); border: 1px solid rgba(var(--surface-rgb), 0.07); font-size: calc(10.5px * var(--font-scale)); }
.tr-block.running { display: inline-flex; align-items: center; gap: 5px; background: rgba(var(--accent-rgb), 0.1); border-color: rgba(var(--accent-rgb), 0.3); color: var(--accent-ink); padding: 2px 7px; width: fit-content; }
.tr-arg-streaming { display: inline-flex; align-items: center; gap: 5px; color: var(--accent-ink); font-size: calc(10px * var(--font-scale)); }
.tr-arg-streaming-wrap { display: flex; flex-direction: column; gap: 2px; }
.tr-streaming-args { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: calc(10px * var(--font-scale)); max-height: 180px; overflow-y: auto; color: rgb(var(--text-rgb)); opacity: .9; padding: 0 0 10px; }
.tr-filename { color: var(--green-ink); font-size: calc(10px * var(--font-scale)); font-family: Consolas, monospace; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; }
.tr-filename-mini { color: var(--green-ink); font-size: calc(9.5px * var(--font-scale)); font-family: Consolas, monospace; max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; }
.tr-block-title { font-size: calc(9.5px * var(--font-scale)); opacity: .65; margin-bottom: 1px; font-family: Consolas, monospace; }
.tr-block pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: calc(10px * var(--font-scale)); max-height: 180px; overflow-y: auto; }
.ti-spinner { width: 9px; height: 9px; border-radius: 50%; border: 1.5px solid rgba(var(--accent-rgb), 0.25); border-top-color: var(--accent); animation: tiSpin .8s linear infinite; flex-shrink: 0; display: inline-block; }

/* Share 截图缩略图：purpose=share 的截图直接挂在对话流里，点击可放大 */
.share-shots { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.share-shot { position: relative; border-radius: 6px; overflow: hidden; border: 1px solid rgba(var(--accent-rgb), 0.35); background: rgba(var(--shadow-rgb), 0.35); cursor: zoom-in; transition: transform .15s ease, box-shadow .15s ease; }
.share-shot:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(var(--shadow-rgb), 0.35); border-color: rgba(var(--accent-rgb), 0.6); }
.share-shot-img { display: block; max-width: 260px; max-height: 160px; width: auto; height: auto; object-fit: contain; }
.a2a-toggle { cursor: pointer; user-select: none; }
.a2a-toggle:hover { opacity: 0.85; }
.send { flex-shrink: 0; padding: 0 18px; height: 40px; min-width: 64px; line-height: 1.2; display: inline-flex; align-items: center; justify-content: center; }
/* 流式中：发送按钮变形为「停止」——虚线环旋转 + 中心方块脉冲，
   点击后 abort 后端 fetch，send() 的 catch/finally 会补 "[已停止]" 并复位状态 */
.send.stop-btn {
  padding: 0; width: 52px; min-width: 52px;
  background: linear-gradient(135deg, rgba(255,90,110,0.55), rgba(255,60,80,0.55));
  border: 1px solid rgba(255,140,150,0.7);
  color: rgb(var(--text-rgb));
  animation: stopBtnPulse 1.4s ease-in-out infinite;
}
.send.stop-btn:hover {
  transform: translateY(-1px) scale(1.03);
  box-shadow: 0 4px 18px rgba(255,80,100,0.45);
}
.send.stop-btn .stop-icon {
  position: relative; width: 14px; height: 14px; border-radius: 3px;
  background: #fff;
  box-shadow: 0 0 8px rgba(var(--surface-rgb), 0.55);
  animation: stopIconPulse 1.4s ease-in-out infinite;
}
.send.stop-btn .stop-icon::before,
.send.stop-btn .stop-icon::after {
  content: ''; position: absolute; inset: -6px; border-radius: 50%;
  border: 1.5px dashed rgba(var(--surface-rgb), 0.65);
  animation: stopRingSpin 3s linear infinite;
}
.send.stop-btn .stop-icon::after {
  inset: -10px; border-style: dotted;
  border-color: rgba(var(--surface-rgb), 0.35);
  animation: stopRingSpin 6s linear infinite reverse;
}
@keyframes stopBtnPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(255,80,100,0.5); }
  50%       { box-shadow: 0 0 0 6px rgba(255,80,100,0); }
}
@keyframes stopIconPulse {
  0%, 100% { transform: scale(1);   opacity: 1;   }
  50%      { transform: scale(0.75); opacity: 0.85; }
}
@keyframes stopRingSpin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}
.empty { text-align: center; padding: 16px; font-size: calc(12px * var(--font-scale)); opacity: .5; }

.hint-right { font-size: calc(10px * var(--font-scale)); opacity: .35; align-self: flex-end; margin-left: 8px; user-select: none; pointer-events: none; }


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
.bubble.markdown h1 { font-size: calc(15px * var(--font-scale)); border-bottom: 1px solid rgba(var(--surface-rgb), 0.1); padding-bottom: 3px; }
.bubble.markdown h2 { font-size: calc(14px * var(--font-scale)); border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); padding-bottom: 2px; }
.bubble.markdown h3 { font-size: calc(13px * var(--font-scale)); }
.bubble.markdown h4, .bubble.markdown h5, .bubble.markdown h6 { font-size: calc(12px * var(--font-scale)); opacity: .9; }
.bubble.markdown h1:first-child, .bubble.markdown h2:first-child, .bubble.markdown h3:first-child { margin-top: 0; }

/* —— 列表 —— */
.bubble.markdown ul, .bubble.markdown ol { margin: 4px 0 6px; padding-left: 20px; }
.bubble.markdown li { margin: 2px 0; line-height: 1.55; }
.bubble.markdown li > ul, .bubble.markdown li > ol { margin: 2px 0; }

/* —— 行内代码 —— */
.bubble.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: calc(11.5px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.12); padding: 1px 5px; border-radius: 4px; color: var(--code-ink);
  word-break: break-word;
}

/* —— 代码块 —— */
.bubble.markdown pre {
  background: rgba(var(--surface-rgb), 0.06); padding: 10px 12px; border-radius: 6px;
  overflow-x: auto; margin: 6px 0; border: 1px solid rgba(var(--surface-rgb), 0.1);
  max-height: 400px;
}
.bubble.markdown pre code { background: none; padding: 0; color: rgb(var(--text-rgb)); font-size: calc(11.5px * var(--font-scale)); white-space: pre; word-break: normal; }

/* —— 引用块 —— */
.bubble.markdown blockquote {
  margin: 6px 0; padding: 4px 12px; border-left: 3px solid rgba(var(--accent-rgb), 0.5);
  background: rgba(var(--accent-rgb), 0.08); color: rgba(var(--text-rgb), 0.85); border-radius: 0 6px 6px 0;
}
.bubble.markdown blockquote p { margin: 0; }

/* —— 链接（核心：默认 target=_blank 由 JS 注入，这里管视觉）—— */
.bubble.markdown a {
  color: var(--accent-ink);
  text-decoration: underline;
  text-underline-offset: 2px;
  transition: color .15s, background-color .15s;
  word-break: break-all;
  padding: 0 1px;
  border-radius: 3px;
}
.bubble.markdown a:hover {
  color: var(--purple-ink);
  background: rgba(var(--accent-rgb), 0.12);
}
.bubble.markdown a:active { color: #ffb088; }
/* 外链提示小箭头（通过 CSS ::after，避免污染 HTML） */
.bubble.markdown a[href^="http"]::after {
  content: '↗';
  display: inline-block;
  margin-left: 1px;
  font-size: calc(9px * var(--font-scale));
  opacity: .6;
  vertical-align: super;
  text-decoration: none;
}

/* —— 分割线 —— */
.bubble.markdown hr { border: none; border-top: 1px solid rgba(var(--surface-rgb), 0.12); margin: 10px 0; }

/* —— 加粗 / 斜体 / 删除线 —— */
.bubble.markdown strong { font-weight: 700; color: inherit; }
.bubble.markdown em { font-style: italic; }
.bubble.markdown del, .bubble.markdown s { text-decoration: line-through; opacity: .55; }

/* —— 表格 —— */
.bubble.markdown table {
  border-collapse: collapse; border: 1px solid rgba(var(--surface-rgb), 0.3);
  background: rgba(var(--surface-rgb), 0.08); margin: 8px 0; font-size: calc(11.5px * var(--font-scale));
  width: max-content; max-width: 100%; border-radius: 6px; overflow: hidden;
}
.bubble.markdown th, .bubble.markdown td {
  border: 1px solid rgba(var(--surface-rgb), 0.28); padding: 5px 9px;
  text-align: left; vertical-align: top; max-width: 350px;
}
.bubble.markdown th {
  background: rgba(var(--surface-rgb), 0.12); font-weight: 600;
  border-bottom: 2px solid rgba(var(--surface-rgb), 0.4); white-space: nowrap;
}
.bubble.markdown tbody tr:nth-child(even) td { background: rgba(var(--surface-rgb), 0.04); }

/* —— 图片 —— */
.bubble.markdown img { max-width: 100%; border-radius: 6px; margin: 4px 0; border: 1px solid rgba(var(--surface-rgb), 0.1); display: inline-block; }

/* —— GFM task list —— */
.bubble.markdown li input[type="checkbox"] { margin-right: 5px; vertical-align: middle; accent-color: var(--accent); }
.bubble.markdown li input[type="checkbox"] + * { vertical-align: middle; }

/* —— details/summary —— */
.bubble.markdown details { margin: 4px 0; border: 1px solid rgba(var(--surface-rgb), 0.1); border-radius: 6px; overflow: hidden; }
.bubble.markdown summary {
  cursor: pointer; padding: 6px 10px; background: rgba(var(--shadow-rgb), 0.2);
  font-size: calc(11.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.8); user-select: none;
  list-style: none; outline: none;
}
.bubble.markdown summary::-webkit-details-marker { display: none; }
.bubble.markdown summary::before { content: '▶'; display: inline-block; margin-right: 6px; font-size: calc(9px * var(--font-scale)); transition: transform .15s; }
.bubble.markdown details[open] > summary::before { content: '▼'; }
.bubble.markdown details > *:not(summary) { padding: 6px 10px; }

/* —— kbd —— */
.bubble.markdown kbd {
  font-family: Consolas, "Courier New", monospace; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.08); border: 1px solid rgba(var(--surface-rgb), 0.25);
  border-radius: 4px; padding: 1px 5px; color: rgb(var(--text-rgb));
}

/* —— 流式输出光标 —— */
.bubble.markdown .stream-cursor,
.stream-cursor {
  display: inline-block;
  margin-left: 2px;
  color: var(--accent);
  font-weight: 700;
  animation: cursorBlink 1s steps(2) infinite;
}
@keyframes cursorBlink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

.ctx-menu {
  position: fixed; z-index: 9999; min-width: 120px;
  background: rgba(var(--node-bg-rgb), 0.96); border: 1px solid rgba(var(--surface-rgb), 0.14);
  border-radius: 8px; padding: 4px; box-shadow: 0 8px 24px rgba(var(--shadow-rgb), 0.45);
  display: flex; flex-direction: column; gap: 2px;
}
.ctx-menu button {
  appearance: none; border: 0; background: transparent; color: rgb(var(--text-rgb));
  font-size: calc(12px * var(--font-scale)); text-align: left; padding: 6px 10px; border-radius: 6px; cursor: pointer;
}
.ctx-menu button:hover { background: rgba(var(--accent-rgb), 0.2); }

/* —— 消息右键菜单 —— */
.msg-ctx-menu {
  position: fixed;
  min-width: 240px;
  max-width: 320px;
  padding: 6px;
  z-index: 9999;
  background: rgba(var(--node-bg-rgb), 0.98);
  border: 1px solid rgba(var(--surface-rgb), 0.14);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.55);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}
.msg-ctx-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-radius: 6px; font-size: calc(13px * var(--font-scale));
  cursor: pointer; transition: background .15s;
}
.msg-ctx-item:hover { background: rgba(var(--accent-rgb), 0.2); }
.msg-ctx-icon { font-size: calc(14px * var(--font-scale)); }

/* 消息操作状态提示（右上角浮动） */
.msg-op-status {
  position: fixed;
  top: 14px;
  right: 14px;
  z-index: 2000;
  padding: 8px 14px;
  border-radius: 8px;
  background: rgba(var(--node-bg-rgb), 0.95);
  border: 1px solid rgba(var(--accent-rgb), 0.35);
  color: rgb(var(--text-rgb));
  font-size: calc(12px * var(--font-scale));
  box-shadow: 0 8px 24px rgba(var(--shadow-rgb), 0.5);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  animation: toastFadeIn .2s ease-out;
}
@keyframes toastFadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

/* —— 文件操作历史还原对话框 —— */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(var(--shadow-rgb), 0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1500;
}
.modal {
  background: rgba(var(--node-bg-rgb), 0.98);
  border: 1px solid rgba(var(--surface-rgb), 0.14);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(var(--shadow-rgb), 0.6);
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
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
}
.modal-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; flex: 1; }
.modal-sub {
  padding: 8px 16px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.6);
  background: rgba(var(--shadow-rgb), 0.15);
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.06);
  line-height: 1.5;
}
.modal-body { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }

/* —— 编辑用户消息对话框 —— */
.modal.edit-msg-modal {
  width: 560px;
  max-width: 94vw;
  max-height: 86vh;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.edit-msg-ta { min-height: 120px; resize: vertical; font-family: inherit; }
.edit-msg-actions {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 16px; border-top: 1px solid rgba(var(--surface-rgb), 0.08);
}

/* 工具栏 */
.ops-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 6px 0;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.06);
  margin-bottom: 4px;
}
.cb {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(12px * var(--font-scale)); cursor: pointer; user-select: none;
}
.cb input[type="checkbox"] { cursor: pointer; }

/* 操作列表 */
.ops-list { display: flex; flex-direction: column; gap: 4px; }
.op-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 8px;
  background: rgba(var(--shadow-rgb), 0.18);
  border: 1px solid rgba(var(--surface-rgb), 0.06);
  font-size: calc(12px * var(--font-scale));
  transition: background .15s;
}
.op-row:hover { background: rgba(var(--shadow-rgb), 0.28); }
.op-row.restored { opacity: .55; }
.op-row .op-cb { flex-shrink: 0; }
.op-ico { font-size: calc(14px * var(--font-scale)); flex-shrink: 0; }
.op-type { font-weight: 600; color: var(--accent-ink); flex-shrink: 0; }
.op-agent {
  font-size: calc(11px * var(--font-scale)); padding: 1px 6px; border-radius: 4px;
  background: rgba(var(--accent2-rgb), 0.15); color: var(--purple-ink);
  flex-shrink: 0; max-width: 120px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.op-path {
  flex: 1; min-width: 0;
  font-family: Consolas, monospace; font-size: calc(11.5px * var(--font-scale));
  color: rgba(var(--text-rgb), 0.85);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.op-time {
  font-family: Consolas, monospace; font-size: calc(10.5px * var(--font-scale));
  color: rgba(var(--text-rgb), 0.45); flex-shrink: 0;
}
.op-restore { flex-shrink: 0; }
.op-restored-tag {
  font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 4px;
  background: rgba(100,220,160,0.15); color: var(--green-ink);
  flex-shrink: 0;
}

/* 空/加载状态 */
.ops-loading, .ops-empty {
  padding: 24px; text-align: center; font-size: calc(13px * var(--font-scale)); opacity: .7;
}
.restore-result {
  padding: 8px 12px; border-radius: 8px; font-size: calc(12px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.12);
  border: 1px solid rgba(var(--accent-rgb), 0.3);
  color: rgba(var(--text-rgb), 0.9);
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
.btn.sm.ghost { padding: 3px 8px; font-size: calc(11px * var(--font-scale)); background: transparent; }
.btn.sm.ghost:hover { background: rgba(var(--surface-rgb), 0.08); }

/* 图片浮窗：窗口尺寸 ≠ 图片缩放；8 方向边界改大小 */
.lb-float {
  position: fixed; z-index: 10000;
  background: rgba(var(--node-bg-rgb), 0.98);   /* 主题化弹层底 */
  border: 1px solid rgba(var(--surface-rgb), 0.28);
  border-radius: 10px;
  box-shadow: 0 12px 36px rgba(var(--shadow-rgb), 0.55), 0 2px 8px rgba(var(--shadow-rgb), 0.3);
  user-select: none;
  animation: lbFloatIn .16s ease-out;
  cursor: default;
  display: flex;
  flex-direction: column;
  overflow: hidden; /* 让 body 的 overflow:hidden 能生效，同时圆角能裁剪内容 */
  color: var(--text);  /* 主题化前景色：保证与背景对比度 */
}
.lb-titlebar {
  height: 32px; flex-shrink: 0;
  display: flex; align-items: center; gap: 6px;
  padding: 0 8px 0 10px;
  background: linear-gradient(180deg, rgba(var(--node-bg-rgb), 0.95), rgba(var(--node-bg-rgb), 0.8));
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.18);
  cursor: grab;
  font-size: calc(12px * var(--font-scale));
  line-height: 1;
  border-radius: 10px 10px 0 0;
}
.lb-titlebar:active { cursor: grabbing; }
.lb-titlebar-icon { font-size: 13px; opacity: 0.85; }
.lb-titlebar-text { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: 0.9; display: inline-flex; align-items: center; gap: 6px; }
.lb-zoom-pct {
  font-variant-numeric: tabular-nums;
  font-size: calc(11px * var(--font-scale));
  padding: 1px 6px;
  background: rgba(var(--accent-rgb), 0.18);
  color: rgb(var(--accent));
  border-radius: 999px;
  border: 1px solid rgba(var(--accent-rgb), 0.3);
}
.lb-close {
  width: 24px; height: 24px; border-radius: 6px;
  background: transparent; border: 1px solid transparent;
  color: rgb(var(--text-rgb));
  cursor: pointer; font-size: calc(12px * var(--font-scale)); line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background .12s, border-color .12s, color .12s;
}
.lb-close:hover { background: rgba(255,80,80,0.22); border-color: rgba(255,80,80,0.48); color: #ff9090; }
.lb-body {
  flex: 1;
  padding: 8px;
  background: rgba(var(--node-bg-rgb), 0.95); /* 与浮窗背景一致的主题色 */
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; /* 阻止原生滚动 —— 放大图用鼠标拖拽平移看（自定义 pan） */
  border-radius: 0 0 10px 10px;
  min-height: 0; /* 让 flex 子元素能正确收缩 */
  cursor: grab;
}
.lb-body:active { cursor: grabbing; }
.lb-img {
  width: 100%; height: 100%;
  object-fit: contain;  /* 窗口多大，图片按比例填满多大 */
  border-radius: 6px;
  display: block;
  pointer-events: none;   /* wheel / mousedown 事件冒泡到 .lb-body */
  transform-origin: center center;  /* 缩放以 body 中心为原点（pan 叠加在 translate 上） */
  transition: none;      /* 滚轮缩放要即时响应 */
}
/* —— 8 方向 resize handles —— */
/* 边条：粗 6px，hover 时变色 + 加粗到 8px 以便抓取 */
.lb-edge {
  position: absolute;
  z-index: 3;
  background: transparent;
  transition: background .12s;
}
.lb-edge:hover { background: rgba(var(--accent-rgb), 0.35); }
.lb-n { top: 0; left: 6px; right: 6px; height: 6px; cursor: ns-resize; }
.lb-s { bottom: 0; left: 6px; right: 6px; height: 6px; cursor: ns-resize; }
.lb-e { right: 0; top: 6px; bottom: 6px; width: 6px; cursor: ew-resize; }
.lb-w { left: 0; top: 6px; bottom: 6px; width: 6px; cursor: ew-resize; }
/* 角：12×12 点击热区，比视觉 6px 大，更易抓取 */
.lb-corner {
  position: absolute;
  z-index: 4;
  width: 12px; height: 12px;
  background: transparent;
}
.lb-corner::before {
  content: '';
  position: absolute; inset: 2px;
  background: rgba(var(--accent-rgb), 0);
  border-radius: 2px;
  transition: background .12s;
}
.lb-corner:hover::before { background: rgba(var(--accent-rgb), 0.55); }
.lb-nw { top: 0; left: 0; cursor: nwse-resize; }
.lb-ne { top: 0; right: 0; cursor: nesw-resize; }
.lb-sw { bottom: 0; left: 0; cursor: nesw-resize; }
.lb-se { bottom: 0; right: 0; cursor: nwse-resize; }
@keyframes lbFloatIn {
  from { opacity: 0; transform: scale(0.94); }
  to { opacity: 1; transform: scale(1); }
}
</style>
