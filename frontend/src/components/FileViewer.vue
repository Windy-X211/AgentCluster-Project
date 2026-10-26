<template>
  <Teleport to="body">
    <div v-if="file" class="fv glass" :style="floatStyle"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="fv-head" @mousedown="startDrag">
        <span class="fv-ico">{{ icon }}</span>
        <span class="fv-title" :title="file.path">{{ file.name }}</span>
        <span class="fv-meta">{{ sizeText }}<template v-if="ext"> · {{ ext }}</template></span>
        <button v-if="editable" class="fv-btn" :disabled="loading || !!err"
                @click.stop="toggleMode"
                :title="mode === 'edit' ? '切换到预览（未保存内容保留）' : '在当前窗口内编辑（编辑与预览同一窗口）'">
          {{ mode === 'edit' ? '👁 预览' : '✏️ 编辑' }}
        </button>
        <button class="fv-btn" @click.stop="openSystem" title="用系统默认程序打开">↗️ 打开</button>
      </div>
      <!-- 关闭按钮固定在窗口右上角（不随标题栏内容流动） -->
      <button class="fv-close" @click.stop="requestClose" title="关闭（Esc）">✕</button>

      <div ref="bodyEl" class="fv-body" :class="{ editing: mode === 'edit' }">
        <!-- pptx 渲染挂载容器：v-show 保留 DOM，供 PptxViewer.open 使用 -->
        <div v-if="kind === 'pptx'" v-show="!pptxFallback && !err" ref="pptxEl" class="fv-pptx"></div>

        <div v-if="loading || pptxRendering" class="fv-hint">{{ loadingText }}</div>

        <textarea v-else-if="mode === 'edit' && editable" ref="editEl" class="fv-edit"
                  v-model="text" spellcheck="false" @input="dirty = true"
                  @keydown.ctrl.enter.prevent="save"></textarea>

        <template v-else-if="kind === 'image'">
          <div v-if="src && !imgErr" class="fv-img-wrap" :class="{ full: imgFull }" @click="imgFull = !imgFull">
            <img class="fv-img" :class="{ full: imgFull }" :src="src" @error="imgErr = true" />
          </div>
          <div v-else class="fv-hint err">
            <div>图片加载失败（可能超出 50MB 预览限制）</div>
            <button class="fv-btn big" @click="openSystem">↗️ 用系统默认程序打开</button>
          </div>
        </template>

        <iframe v-else-if="kind === 'pdf' && src && !err" class="fv-frame" :src="src" title="PDF 预览"></iframe>

        <template v-else-if="err">
          <div class="fv-hint err">{{ err }}</div>
          <div class="fv-fail">
            <button class="fv-btn big" @click="openSystem">↗️ 用系统默认程序打开</button>
          </div>
        </template>

        <template v-else-if="kind === 'office' && pdfUrl">
          <iframe class="fv-frame" :src="pdfUrl" title="文档预览"></iframe>
        </template>

        <template v-else-if="kind === 'office' && officeFallback">
          <div class="fv-notice">⚠ PDF 转换不可用，以下为文本提取内容</div>
          <div class="fv-doc">
            <div v-for="(l, i) in docLines" :key="i" class="fv-line">{{ l }}</div>
          </div>
        </template>

        <template v-else-if="kind === 'md'">
          <div class="fv-md" v-html="mdHtml"></div>
        </template>

        <pre v-else-if="kind === 'text'" class="fv-text">{{ text }}</pre>

        <template v-else-if="kind === 'sheet'">
          <div v-if="sheets.length > 1" class="fv-tabs">
            <button v-for="(s, i) in sheets" :key="i" class="fv-tab"
                    :class="{ on: i === sheetIdx }" @click.stop="sheetIdx = i">{{ s.name }}</button>
          </div>
          <div class="fv-table-wrap">
            <table v-if="curSheet && curSheet.rows.length" class="fv-table">
              <thead>
                <tr><th v-for="(c, ci) in curSheet.rows[0]" :key="ci">{{ c }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in curSheet.rows.slice(1)" :key="ri">
                  <td v-for="(c, ci) in row" :key="ci">{{ c }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="fv-hint">（空工作表）</div>
          </div>
        </template>

        <template v-else-if="kind === 'pptx' && pptxFallback">
          <div class="fv-notice">⚠ 图形渲染失败，以下为文本提取内容</div>
          <div class="fv-doc">
            <div v-for="s in slides" :key="s.idx" class="fv-slide">
              <div class="fv-slide-no">🎞 幻灯片 {{ s.idx }}</div>
              <div v-for="(l, i) in s.lines" :key="i" class="fv-line">{{ l }}</div>
            </div>
          </div>
        </template>

        <div v-else-if="kind !== 'pptx'" class="fv-hint err">
          <div>此格式暂不支持内置预览{{ ext ? '：.' + ext : '' }}</div>
          <button class="fv-btn big" @click="openSystem">↗️ 用系统默认程序打开</button>
        </div>
      </div>

      <div class="fv-foot">
        <span class="fv-foot-hint">{{ footHint }}</span>
        <span class="fv-foot-right">
          <span v-if="kind === 'sheet' && curSheet" class="fv-foot-hint">
            {{ curSheet.name }} · 共 {{ curSheet.total }} 行<template v-if="curSheet.total > MAX_ROWS">（仅显示前 {{ MAX_ROWS }} 行）</template>
          </span>
          <span v-else-if="kind === 'pptx' && pptxCount && !pptxFallback" class="fv-foot-hint">共 {{ pptxCount }} 页</span>
          <span v-else-if="kind === 'image' && !imgErr && !loading" class="fv-foot-hint">
            {{ imgFull ? '点击图片还原为适应窗口' : '点击图片按 100% 查看' }}
          </span>
          <button v-if="mode === 'edit' && editable" class="fv-btn ok"
                  :disabled="saving || !!err" @click="save" title="Ctrl+Enter 保存">
            {{ dirty ? '💾 保存 ●' : '💾 保存' }}
          </button>
        </span>
      </div>

      <!-- 缩放手把（四边 + 四角），吸附在窗口内缘 -->
      <div class="fv-rs n" @mousedown.stop.prevent="startResize($event, 'n')"></div>
      <div class="fv-rs s" @mousedown.stop.prevent="startResize($event, 's')"></div>
      <div class="fv-rs e" @mousedown.stop.prevent="startResize($event, 'e')"></div>
      <div class="fv-rs w" @mousedown.stop.prevent="startResize($event, 'w')"></div>
      <div class="fv-rs ne" @mousedown.stop.prevent="startResize($event, 'ne')"></div>
      <div class="fv-rs nw" @mousedown.stop.prevent="startResize($event, 'nw')"></div>
      <div class="fv-rs se" @mousedown.stop.prevent="startResize($event, 'se')"></div>
      <div class="fv-rs sw" @mousedown.stop.prevent="startResize($event, 'sw')"></div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { api } from '@/api'
import { renderMarkdown } from '@/utils/markdown'
import { unzipSync, strFromU8 } from 'fflate'
import { API_BASE } from '@/utils/env'

export interface ViewerFile {
  path: string
  name: string
  size: number
  mtime?: number
  /** 打开时的目标模式：右键"编辑"直接进编辑态 */
  mode?: 'preview' | 'edit'
}

const props = defineProps<{
  canvasId: number | null
  file: ViewerFile | null
}>()
const emit = defineEmits<{
  (e: 'close'): void
  /** 文本已保存（工作区面板据此刷新文件树） */
  (e: 'saved'): void
}>()

// —— 分类：与工作区右键/双击分流保持一致（程序类文件不会进查看器） ——
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif', 'apng'])
const SHEET_EXTS = new Set(['xlsx', 'xls', 'xlsb', 'csv'])
// Office 文档：doc/docx/ppt 走后端 COM 转 PDF（docx 转换失败时回退文本提取）
const OFFICE_EXTS = new Set(['doc', 'docx', 'ppt'])
// 无内置预览能力的常见二进制类型（直接给"系统打开"，不发请求）
const UNSUPPORTED_EXTS = new Set([
  'zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz', 'zst',
  'mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'mp3', 'wav', 'flac', 'ogg', 'wma', 'm4a',
  'apk', 'dll', 'so', 'dat', 'db', 'sqlite', 'iso', 'img', 'dmg', 'deb', 'rpm', 'msi',
])

const MAX_ROWS = 300
const MAX_COLS = 50

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i > 0 ? name.slice(i + 1).toLowerCase() : ''
}

type Kind = 'image' | 'pdf' | 'md' | 'text' | 'sheet' | 'office' | 'pptx' | 'unsupported'

function kindOf(name: string): Kind {
  const e = extOf(name)
  if (IMAGE_EXTS.has(e)) return 'image'
  if (e === 'pdf') return 'pdf'
  if (e === 'md') return 'md'
  if (e === 'pptx') return 'pptx'
  if (OFFICE_EXTS.has(e)) return 'office'
  if (SHEET_EXTS.has(e)) return 'sheet'
  if (UNSUPPORTED_EXTS.has(e)) return 'unsupported'
  return 'text'
}

const kind = computed<Kind>(() => (props.file ? kindOf(props.file.name) : 'text'))
const ext = computed(() => extOf(props.file?.name || ''))
const editable = computed(() => kind.value === 'md' || kind.value === 'text')
const sizeText = computed(() => formatSize(props.file?.size || 0))
const icon = computed(() => {
  const e = ext.value
  if (IMAGE_EXTS.has(e)) return '🖼'
  if (e === 'pdf') return '📕'
  if (e === 'md') return '📝'
  if (SHEET_EXTS.has(e)) return '📊'
  if (e === 'docx' || e === 'doc') return '📘'
  if (e === 'pptx' || e === 'ppt') return '📙'
  return '📄'
})

function formatSize(n: number): string {
  n = n || 0
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

// —— 状态 ——
const mode = ref<'preview' | 'edit'>('preview')
const loading = ref(false)
const loadingText = ref('加载中…')
const pptxRendering = ref(false)
const err = ref('')
const src = ref('')            // 图片 / PDF 的原始地址
const text = ref('')           // 文本内容（预览展示 + 编辑器共用）
const dirty = ref(false)
const saving = ref(false)
const sheets = ref<{ name: string; rows: string[][]; total: number }[]>([])
const sheetIdx = ref(0)
const docLines = ref<string[]>([])     // docx 文本回退
const slides = ref<{ idx: number; lines: string[] }[]>([])   // pptx 文本回退
const imgErr = ref(false)
const imgFull = ref(false)
const pdfUrl = ref('')         // Office 转 PDF 的 blob URL
const officeFallback = ref(false)
const pptxFallback = ref(false)
const pptxCount = ref(0)
const curSheet = computed(() => sheets.value[sheetIdx.value] || null)
let loadGen = 0

const editEl = ref<HTMLTextAreaElement | null>(null)
const bodyEl = ref<HTMLElement | null>(null)
const pptxEl = ref<HTMLElement | null>(null)
let pptxViewer: any = null

function revokePdf() {
  if (pdfUrl.value) {
    URL.revokeObjectURL(pdfUrl.value)
    pdfUrl.value = ''
  }
}

function destroyPptx() {
  try { pptxViewer?.destroy?.() } catch { /* 已销毁/未初始化 */ }
  pptxViewer = null
}

function reset() {
  destroyPptx()
  revokePdf()
  loading.value = false
  loadingText.value = '加载中…'
  pptxRendering.value = false
  err.value = ''
  src.value = ''
  text.value = ''
  dirty.value = false
  saving.value = false
  sheets.value = []
  sheetIdx.value = 0
  docLines.value = []
  slides.value = []
  imgErr.value = false
  imgFull.value = false
  officeFallback.value = false
  pptxFallback.value = false
  pptxCount.value = 0
}

function cleanupAll() {
  loadGen++          // 使在途加载作废
  reset()
}

// —— Markdown：渲染（含相对路径图片重写） ——
const mdHtml = computed(() => {
  if (kind.value !== 'md' || mode.value !== 'preview' || !text.value) return ''
  const cid = props.canvasId
  const f = props.file
  if (cid == null || !f) return ''
  try {
    return rewriteRelImages(renderMarkdown(text.value), f.path, cid)
  } catch {
    return ''
  }
})

function joinRel(dir: string, rel: string): string {
  const stack = dir.split(/[\\/]/)
  for (const part of rel.split(/[\\/]/)) {
    if (!part || part === '.') continue
    if (part === '..') { if (stack.length > 1) stack.pop() }
    else stack.push(part)
  }
  return stack.join('\\')
}

function rewriteRelImages(html: string, mdPath: string, cid: number): string {
  const dir = mdPath.replace(/[\\/][^\\/]*$/, '')
  return html.replace(/<img\b([^>]*?)src="([^"]*)"([^>]*)>/gi, (m, pre, s, post) => {
    if (!s || /^(https?:|data:|blob:|\/\/|#|\/)/i.test(s)) return m
    const abs = joinRel(dir, s)
    return `<img${pre}src="${api.canvases.fileRawUrl(cid, abs)}"${post}>`
  })
}

// —— 加载 ——
function extError(r: any, statusText: string): string {
  return r?.detail || r?.error || statusText
}

/** 读文本：保留后端 detail 错误信息；canvasId=null 时走根级 /api/fs/read 等价路径 */
async function readText(cid: number | null, path: string): Promise<string> {
  const r = cid != null
    ? await fetch(`${API_BASE}/api/canvases/${cid}/file?path=` + encodeURIComponent(path))
    : await fetch(`${API_BASE}/api/fs/read?path=` + encodeURIComponent(path))
  const d = await r.json().catch(() => null)
  if (!r.ok || !d?.ok) throw new Error(extError(d, `${r.status} ${r.statusText}`))
  return d.content || ''
}

async function renderPptx(buf: ArrayBuffer) {
  destroyPptx()
  const el = pptxEl.value
  if (!el) throw new Error('幻灯片预览容器未就绪')
  const mod: any = await import('@aiden0z/pptx-renderer')
  pptxViewer = await mod.PptxViewer.open(buf, el, {
    zipLimits: mod.RECOMMENDED_ZIP_LIMITS,
    fitMode: 'contain',
    lazySlides: true,
    lazyMedia: true,
    listOptions: { windowed: true, initialSlides: 4, batchSize: 4 },
  })
  pptxCount.value = pptxViewer?.slideCount || 0
}

async function load() {
  const f = props.file
  const cid = props.canvasId
  if (!f) return
  const g = ++loadGen
  reset()
  const k = kind.value
  if (k === 'image' || k === 'pdf') {
    src.value = cid != null
      ? api.canvases.fileRawUrl(cid, f.path, f.mtime)
      : api.fs.rawUrl(f.path, f.mtime)
    return
  }
  if (k === 'unsupported') return

  loading.value = true
  loadingText.value = k === 'office' ? '正在调用 Office 转换为 PDF…（首次约几秒）' : '加载中…'
  try {
    if (k === 'md' || k === 'text') {
      text.value = await readText(cid, f.path)
      if (g !== loadGen) return
      if (mode.value === 'edit') await nextTick(() => editEl.value?.focus())
    } else if (k === 'sheet') {
      const buf = cid != null ? await api.canvases.fileRaw(cid, f.path) : await api.fs.raw(f.path)
      if (g !== loadGen) return
      const XLSX = await import('xlsx')
      const wb = XLSX.read(new Uint8Array(buf), { type: 'array' })
      sheets.value = wb.SheetNames.map((n) => {
        const all = XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: false, defval: '' }) as unknown as string[][]
        return {
          name: n || 'Sheet1',
          rows: all.slice(0, MAX_ROWS).map((r) => r.slice(0, MAX_COLS).map((c) => String(c ?? ''))),
          total: all.length,
        }
      })
      sheetIdx.value = 0
    } else if (k === 'office') {
      try {
        pdfUrl.value = cid != null
          ? await api.canvases.fileOfficePdf(cid, f.path)
          : await api.fs.officePdf(f.path)
        if (g !== loadGen) revokePdf()
      } catch (e: any) {
        if (g !== loadGen) return
        if (ext.value === 'docx') {
          // Word COM 不可用 → 回退纯文本提取
          const buf = cid != null ? await api.canvases.fileRaw(cid, f.path) : await api.fs.raw(f.path)
          if (g !== loadGen) return
          docLines.value = extractDocx(buf)
          officeFallback.value = true
        } else {
          throw e
        }
      }
    } else if (k === 'pptx') {
      const buf = cid != null ? await api.canvases.fileRaw(cid, f.path) : await api.fs.raw(f.path)
      if (g !== loadGen) return
      loading.value = false
      pptxRendering.value = true
      loadingText.value = '正在渲染幻灯片…'
      try {
        await nextTick()
        if (g !== loadGen) return
        await renderPptx(buf)
      } catch (e: any) {
        if (g !== loadGen) return
        try {
          slides.value = extractPptx(buf)
          pptxFallback.value = true
        } catch {
          err.value = '幻灯片渲染失败：' + (e?.message || e)
        }
      } finally {
        if (g === loadGen) pptxRendering.value = false
      }
      return
    }
  } catch (e: any) {
    if (g === loadGen) err.value = e?.message || String(e)
  } finally {
    if (g === loadGen) loading.value = false
  }
}

watch(
  () => props.file,
  (f) => {
    if (!f) { cleanupAll(); return }
    mode.value = f.mode === 'edit' && (kindOf(f.name) === 'md' || kindOf(f.name) === 'text') ? 'edit' : 'preview'
    load()
  },
)

// —— 编辑 / 保存 ——
/** 当前可视区的滚动进度（0~1）：预览滚 .fv-body，编辑滚 textarea，二者视口高度不同，故按比例映射 */
function scrollRatio(): number {
  const el = mode.value === 'edit' ? editEl.value : bodyEl.value
  if (!el) return 0
  const max = el.scrollHeight - el.clientHeight
  return max > 0 ? el.scrollTop / max : 0
}

function applyScrollRatio(r: number) {
  const el = mode.value === 'edit' ? editEl.value : bodyEl.value
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  if (max > 0) el.scrollTop = Math.min(1, Math.max(0, r)) * max
}

function toggleMode() {
  if (!editable.value || err.value || loading.value) return
  const ratio = scrollRatio()
  mode.value = mode.value === 'edit' ? 'preview' : 'edit'
  nextTick(() => {
    if (mode.value === 'edit') editEl.value?.focus()
    applyScrollRatio(ratio)
  })
}

async function save() {
  const f = props.file
  const cid = props.canvasId
  if (!f || cid == null || !editable.value || err.value || saving.value) return
  saving.value = true
  try {
    await api.canvases.fileWrite(cid, f.path, text.value)
    dirty.value = false
    emit('saved')
  } catch (e: any) {
    alert('保存失败：' + (e?.message || e))
  } finally {
    saving.value = false
  }
}

function requestClose() {
  if (dirty.value && !window.confirm('有未保存的修改，放弃并关闭？')) return
  emit('close')
}

async function openSystem() {
  const f = props.file
  const cid = props.canvasId
  if (!f) return
  try {
    if (cid != null) {
      await api.canvases.fileRun(cid, f.path)
    } else {
      await api.fs.run(f.path)
    }
  } catch (e: any) {
    alert('打开失败：' + (e?.message || e))
  }
}

const footHint = computed(() => {
  if (mode.value === 'edit') return 'Ctrl+Enter 保存 · Esc 关闭 · 编辑与预览在同一窗口切换'
  return '拖动标题栏移动 · 拖动窗口边缘调整大小 · Esc 关闭'
})

// —— OOXML（docx / pptx）纯文本提取：fflate 解压 + 轻量标签处理（回退路径） ——
function decodeEntities(s: string): string {
  return s.replace(/&(#x?[0-9a-fA-F]+|amp|lt|gt|quot|apos);/g, (m, t) => {
    switch (t) {
      case 'amp': return '&'
      case 'lt': return '<'
      case 'gt': return '>'
      case 'quot': return '"'
      case 'apos': return "'"
      default: {
        const code = t[1] === 'x' || t[1] === 'X' ? parseInt(t.slice(2), 16) : parseInt(t.slice(1), 10)
        return Number.isFinite(code) && code > 0 ? String.fromCodePoint(code) : m
      }
    }
  })
}

function unzip(buf: ArrayBuffer): Record<string, Uint8Array> {
  try {
    return unzipSync(new Uint8Array(buf))
  } catch {
    throw new Error('无法解析压缩结构（文件可能已损坏）')
  }
}

function xmlToLines(xml: string): string[] {
  return xml
    .replace(/<a:br\s*\/>/g, '\n')
    .replace(/<w:tab[^>]*\/>/g, '\t')
    .replace(/<w:br[^>]*\/>/g, '\n')
    .replace(/<\/w:p>|<\/a:p>/g, '\n')
    .replace(/<\/w:tc>/g, '\t')
    .replace(/<\/w:tr>/g, '\n')
    .replace(/<[^>]+>/g, '')
    .split('\n')
    .map((l) => decodeEntities(l).trim())
}

function extractDocx(buf: ArrayBuffer): string[] {
  const files = unzip(buf)
  const entry = files['word/document.xml']
  if (!entry) throw new Error('不是有效的 docx 文件（缺少 word/document.xml）')
  const lines = xmlToLines(strFromU8(entry))
  return lines.length ? lines : ['（文档为空）']
}

function extractPptx(buf: ArrayBuffer): { idx: number; lines: string[] }[] {
  const files = unzip(buf)
  const names = Object.keys(files)
    .filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))
    .sort((a, b) => (parseInt(a.match(/slide(\d+)/)![1], 10) - parseInt(b.match(/slide(\d+)/)![1], 10)))
  if (!names.length) throw new Error('不是有效的 pptx 文件（缺少幻灯片）')
  return names.map((n, i) => ({
    idx: i + 1,
    lines: xmlToLines(strFromU8(files[n])).filter((l, j, arr) => l || (j > 0 && arr[j - 1])),
  }))
}

// —— 浮窗：位置 + 鼠标拉伸 ——
const MIN_W = 480
const MIN_H = 320
const rect = ref({ x: 140, y: 64, w: 880, h: 620 })
const vp = ref({ w: window.innerWidth, h: window.innerHeight })

const floatStyle = computed(() => {
  const w = Math.min(rect.value.w, Math.max(MIN_W, vp.value.w - 8))
  const h = Math.min(rect.value.h, Math.max(MIN_H, vp.value.h - 8))
  const x = Math.max(0, Math.min(rect.value.x, vp.value.w - w))
  const y = Math.max(0, Math.min(rect.value.y, Math.max(0, vp.value.h - 60)))
  return { position: 'fixed' as const, left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px' }
})

type ResizeEdge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
const resizing = ref<{ edge: ResizeEdge; sx: number; sy: number; r: { x: number; y: number; w: number; h: number } } | null>(null)

function startResize(e: MouseEvent, edge: ResizeEdge) {
  resizing.value = { edge, sx: e.clientX, sy: e.clientY, r: { ...rect.value } }
}

function clampRect(x: number, y: number, w: number, h: number) {
  w = Math.max(MIN_W, Math.min(w, vp.value.w))
  h = Math.max(MIN_H, Math.min(h, vp.value.h))
  x = Math.max(0, Math.min(x, vp.value.w - w))
  y = Math.max(0, Math.min(y, vp.value.h - 60))
  return { x, y, w, h }
}

function onResizeMove(e: MouseEvent) {
  const st = resizing.value
  if (!st) return
  const s = st.r
  const dx = e.clientX - st.sx
  const dy = e.clientY - st.sy
  let { x, y, w, h } = s
  if (st.edge.includes('e')) w = s.w + dx
  if (st.edge.includes('s')) h = s.h + dy
  if (st.edge.includes('w')) w = s.w - dx
  if (st.edge.includes('n')) h = s.h - dy
  w = Math.max(MIN_W, w)
  h = Math.max(MIN_H, h)
  if (st.edge.includes('w')) x = s.x + (s.w - w)
  if (st.edge.includes('n')) y = s.y + (s.h - h)
  rect.value = clampRect(x, y, w, h)
}

// —— 标题栏拖动 ——
const dragStart = ref<{ clientX: number; clientY: number; base: { x: number; y: number } } | null>(null)
function startDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  dragStart.value = { clientX: e.clientX, clientY: e.clientY, base: { x: rect.value.x, y: rect.value.y } }
  e.preventDefault()
}
function onDragMove(e: MouseEvent) {
  const s = dragStart.value
  if (!s) return
  rect.value = clampRect(
    s.base.x + (e.clientX - s.clientX),
    s.base.y + (e.clientY - s.clientY),
    rect.value.w, rect.value.h,
  )
}
function onMove(e: MouseEvent) {
  onDragMove(e)
  onResizeMove(e)
}
function onUp() {
  dragStart.value = null
  resizing.value = null
}
function onVpResize() {
  vp.value = { w: window.innerWidth, h: window.innerHeight }
}
function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !props.file) return
  e.stopImmediatePropagation()
  requestClose()
}
onMounted(() => {
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', onVpResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', onVpResize)
  cleanupAll()
})
</script>

<style scoped>
.fv {
  display: flex; flex-direction: column;
  overflow: hidden; z-index: 9994;
  box-shadow: 0 16px 52px rgba(var(--shadow-rgb), 0.58);
  border-radius: 10px;
}
.fv-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 44px 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
  flex-shrink: 0;
}
.fv-head:active { cursor: grabbing; }
.fv-ico { flex-shrink: 0; }
.fv-title { font-size: calc(13.5px * var(--font-scale)); font-weight: 600; flex-shrink: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fv-meta { font-size: calc(11px * var(--font-scale)); opacity: .55; font-family: Consolas, monospace; white-space: nowrap; flex-shrink: 0; }
.fv-btn {
  background: rgba(var(--surface-rgb), 0.05);
  border: 1px solid rgba(var(--surface-rgb), 0.12);
  color: rgb(var(--text-rgb)); border-radius: 7px; cursor: pointer;
  font-size: calc(11px * var(--font-scale)); padding: 3px 8px; line-height: 1.6;
  transition: all .15s; flex-shrink: 0; white-space: nowrap;
}
.fv-btn:hover { background: rgba(var(--accent-rgb), 0.18); }
.fv-btn:disabled { opacity: .4; cursor: not-allowed; }
.fv-btn.big { font-size: calc(13px * var(--font-scale)); padding: 8px 18px; }
.fv-btn.ok { background: rgba(100, 220, 160, 0.15); border-color: rgba(100, 220, 160, 0.4); color: #7dd88f; }
.fv-btn.ok:hover { background: rgba(100, 220, 160, 0.28); }

/* 关闭按钮：固定右上角，不随标题栏内容流动 */
.fv-close {
  position: absolute; top: 6px; right: 8px; z-index: 8;
  width: 26px; height: 26px; padding: 0;
  background: rgba(var(--surface-rgb), 0.06);
  border: 1px solid rgba(var(--surface-rgb), 0.14);
  color: rgb(var(--text-rgb)); border-radius: 7px; cursor: pointer;
  font-size: calc(13px * var(--font-scale)); line-height: 1; transition: all .15s;
}
.fv-close:hover { background: rgba(255, 80, 80, 0.28); border-color: rgba(255, 80, 80, 0.5); }

.fv-body { flex: 1; min-height: 0; overflow: auto; overflow-anchor: none; padding: 10px 12px; position: relative; }
.fv-body.editing { padding: 8px 10px; overflow: hidden; display: flex; }
.fv-hint { padding: 30px 12px; text-align: center; font-size: calc(12.5px * var(--font-scale)); opacity: .65; }
.fv-hint.err { color: #ffb0b0; opacity: 1; display: flex; flex-direction: column; align-items: center; gap: 12px; }
.fv-fail { display: flex; justify-content: center; padding-bottom: 20px; }
.fv-notice {
  font-size: calc(11.5px * var(--font-scale)); color: #ffd18a; opacity: .9;
  border: 1px dashed rgba(255, 209, 138, 0.4);
  background: rgba(255, 209, 138, 0.08);
  border-radius: 7px; padding: 6px 10px; margin-bottom: 10px;
}

/* 编辑器（与预览同一窗口内切换） */
.fv-edit {
  flex: 1; width: 100%; min-height: 0;
  background: rgba(var(--shadow-rgb), 0.35);
  border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 8px;
  color: rgb(var(--text-rgb)); font-size: calc(12.5px * var(--font-scale));
  font-family: Consolas, "Courier New", monospace;
  padding: 10px; outline: none; resize: none; line-height: 1.55;
}
.fv-edit:focus { border-color: rgba(var(--accent-rgb), 0.6); }

/* 图片 */
.fv-img-wrap { display: flex; align-items: center; justify-content: center; min-height: 100%; cursor: zoom-in; }
.fv-img-wrap.full { cursor: zoom-out; }
.fv-img { max-width: 100%; max-height: 100%; border-radius: 6px; display: block; }
.fv-img.full { max-width: none; max-height: none; }

/* PDF / Office 转 PDF 的 iframe */
.fv-frame { width: 100%; height: 100%; min-height: 420px; border: none; background: rgba(var(--shadow-rgb), 0.35); border-radius: 8px; }

/* pptx 渲染容器 */
.fv-pptx { width: 100%; min-height: 200px; }
.fv-pptx :deep(.pptx-viewer), .fv-pptx > * { max-width: 100%; }

/* 文本 */
.fv-text {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, "Courier New", monospace;
  line-height: 1.6; color: rgb(var(--text-rgb));
}

/* 表格 */
.fv-tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; position: sticky; top: -10px; }
.fv-tab {
  background: rgba(var(--surface-rgb), 0.05); border: 1px solid rgba(var(--surface-rgb), 0.12);
  color: rgb(var(--text-rgb)); border-radius: 6px 6px 0 0; cursor: pointer;
  font-size: calc(11.5px * var(--font-scale)); padding: 4px 12px; transition: all .15s;
}
.fv-tab:hover { background: rgba(var(--accent-rgb), 0.15); }
.fv-tab.on { background: rgba(var(--accent-rgb), 0.22); border-color: rgba(var(--accent-rgb), 0.5); }
.fv-table-wrap { overflow: auto; overflow-anchor: none; }
.fv-table { border-collapse: collapse; font-size: calc(12px * var(--font-scale)); min-width: 100%; }
.fv-table th, .fv-table td {
  border: 1px solid rgba(var(--surface-rgb), 0.14);
  padding: 4px 9px; text-align: left; white-space: nowrap;
  max-width: 320px; overflow: hidden; text-overflow: ellipsis;
}
.fv-table th { background: rgba(var(--accent-rgb), 0.16); position: sticky; top: 0; font-weight: 600; }
.fv-table tbody tr:nth-child(even) { background: rgba(var(--surface-rgb), 0.03); }

/* docx 回退 / pptx 回退 文本 */
.fv-doc { font-size: calc(13px * var(--font-scale)); line-height: 1.7; }
.fv-line { min-height: 1.4em; white-space: pre-wrap; word-break: break-word; }
.fv-slide { margin-bottom: 18px; padding-bottom: 10px; border-bottom: 1px dashed rgba(var(--surface-rgb), 0.14); }
.fv-slide:last-child { border-bottom: none; }
.fv-slide-no { font-size: calc(11px * var(--font-scale)); font-weight: 600; color: #ffd18a; opacity: .85; margin-bottom: 6px; letter-spacing: .5px; }

.fv-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 6px 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.08);
  font-size: calc(10.5px * var(--font-scale)); opacity: .7; flex-shrink: 0;
}
.fv-foot-hint { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: .75; }
.fv-foot-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

/* 缩放手把：吸附窗口内缘（.fv overflow:hidden，故不用负坐标） */
.fv-rs { position: absolute; z-index: 7; }
.fv-rs.n { top: 0; left: 10px; right: 10px; height: 6px; cursor: ns-resize; }
.fv-rs.s { bottom: 0; left: 10px; right: 10px; height: 6px; cursor: ns-resize; }
.fv-rs.e { right: 0; top: 10px; bottom: 10px; width: 6px; cursor: ew-resize; }
.fv-rs.w { left: 0; top: 10px; bottom: 10px; width: 6px; cursor: ew-resize; }
.fv-rs.ne { top: 0; right: 0; width: 14px; height: 14px; cursor: nesw-resize; }
.fv-rs.nw { top: 0; left: 0; width: 14px; height: 14px; cursor: nwse-resize; }
.fv-rs.se { bottom: 0; right: 0; width: 14px; height: 14px; cursor: nwse-resize; }
.fv-rs.sw { bottom: 0; left: 0; width: 14px; height: 14px; cursor: nesw-resize; }
</style>

<!-- markdown v-html 内容样式：scoped 样式命中不了 v-html 节点，故单独全局块（统一 .fv-md 前缀） -->
<style>
.fv-md { font-size: calc(13.5px * var(--font-scale)); line-height: 1.7; word-break: break-word; }
.fv-md h1, .fv-md h2, .fv-md h3, .fv-md h4, .fv-md h5, .fv-md h6 {
  margin: 16px 0 8px; line-height: 1.35; font-weight: 650;
}
.fv-md h1 { font-size: calc(20px * var(--font-scale)); border-bottom: 1px solid rgba(var(--surface-rgb), 0.12); padding-bottom: 5px; }
.fv-md h2 { font-size: calc(17px * var(--font-scale)); border-bottom: 1px solid rgba(var(--surface-rgb), 0.1); padding-bottom: 4px; }
.fv-md h3 { font-size: calc(15.5px * var(--font-scale)); }
.fv-md h4 { font-size: calc(14px * var(--font-scale)); }
.fv-md h5, .fv-md h6 { font-size: calc(13px * var(--font-scale)); opacity: .9; }
.fv-md p { margin: 8px 0; }
.fv-md ul, .fv-md ol { margin: 8px 0; padding-left: 24px; }
.fv-md li { margin: 3px 0; }
.fv-md a { color: #7db3ff; text-decoration: underline; text-underline-offset: 2px; }
.fv-md img { max-width: 100%; border-radius: 6px; }
.fv-md hr { border: none; border-top: 1px solid rgba(var(--surface-rgb), 0.15); margin: 16px 0; }
.fv-md blockquote {
  border-left: 3px solid rgba(var(--accent-rgb), 0.55);
  margin: 10px 0; padding: 4px 12px; opacity: .85;
  background: rgba(var(--accent-rgb), 0.07); border-radius: 0 6px 6px 0;
}
.fv-md code {
  font-family: Consolas, "Courier New", monospace;
  background: rgba(var(--surface-rgb), 0.09);
  border: 1px solid rgba(var(--surface-rgb), 0.1);
  padding: 1px 5px; border-radius: 4px; font-size: .9em;
}
.fv-md pre {
  background: rgba(var(--shadow-rgb), 0.45);
  border: 1px solid rgba(var(--surface-rgb), 0.12);
  border-radius: 8px; padding: 10px 12px; overflow: auto; margin: 10px 0;
}
.fv-md pre code { background: none; border: none; padding: 0; font-size: calc(12.5px * var(--font-scale)); line-height: 1.55; }
.fv-md table { border-collapse: collapse; margin: 10px 0; display: block; overflow-x: auto; }
.fv-md th, .fv-md td { border: 1px solid rgba(var(--surface-rgb), 0.18); padding: 5px 10px; font-size: calc(12.5px * var(--font-scale)); }
.fv-md th { background: rgba(var(--surface-rgb), 0.07); font-weight: 600; }
.fv-md tbody tr:nth-child(even) { background: rgba(var(--surface-rgb), 0.03); }
.fv-md input[type="checkbox"] { margin-right: 6px; }
</style>
