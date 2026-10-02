<template>
  <!-- 钉住时嵌入画布视口 DOM（随 transform 自动跟随平移/缩放）；未钉住时浮动（body 下 fixed） -->
  <Teleport :to="teleportTarget">
    <!-- 工作区面板（可拖动） -->
    <div class="ws-panel glass" :style="panelStyle" :class="{ embedded: pinned }" ref="panelEl"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="ws-head" @mousedown="startDrag">
        <span class="ws-ico">🗂</span>
        <div class="ws-titles">
          <div class="ws-title">工作区</div>
          <div class="ws-path" :title="workdir || ''">{{ workdir || '未设置工作区文件夹' }}</div>
        </div>
        <div class="ws-head-actions">
          <button class="ws-btn pin" :class="{ active: pinned }"
                  @click.stop="onPinToggle" :title="pinned ? '取消钉住，恢复浮动' : '钉住到画布（随画布移动/缩放）'">📌</button>
          <button class="ws-btn" @click.stop="refresh" :disabled="loading" title="刷新">⟳</button>
          <button class="ws-btn trash" :class="{ active: trashOpen }" @click.stop="toggleTrash"
                  :title="trashOpen ? '收起回收站' : '回收站（被删除的文件）'">🗑</button>
          <button class="ws-btn" @click.stop="$emit('close')" title="关闭">✕</button>
        </div>
      </div>

      <div class="ws-body" @contextmenu.prevent="showRootCtx">
        <div v-if="!workdir" class="ws-empty">
          <div>当前画布未设置工作区文件夹</div>
          <button class="ws-btn purple" @click.stop="$emit('setup-workdir')">去设置</button>
        </div>
        <div v-else-if="loading" class="ws-empty">加载中…</div>
        <div v-else-if="!rows.length" class="ws-empty">{{ errorMsg || '工作区为空' }}</div>
        <div v-else class="ws-tree">
          <div v-for="row in rows" :key="row.path" class="ws-row" :class="{ dir: row.is_dir }"
               :style="{ paddingLeft: 8 + row.depth * 16 + 'px' }"
               @click="onRowClick(row)"
               @dblclick="onRowDblClick(row)"
               @contextmenu.prevent.stop="showRowCtx($event, row)">
            <span class="ws-row-ico">{{ row.is_dir ? (expanded.has(row.path) ? '📂' : '📁') : iconFor(row.name) }}</span>
            <template v-if="renameTarget && renameTarget.path === row.path">
              <input class="ws-rename-input" :value="renameTarget.value" autofocus
                     @input="renameTarget.value = ($event.target as HTMLInputElement).value"
                     @keydown.enter.prevent="commitRename" @keydown.esc="cancelRename"
                     @blur="commitRename" @click.stop
                     ref="r => r && (r as HTMLInputElement).focus()" />
            </template>
            <template v-else>
              <span class="ws-row-name" :title="row.rel || row.name">{{ row.name }}</span>
              <span v-if="row.truncated" class="ws-trunc" title="子项过多，仅显示前 300 项">…</span>
              <template v-if="!compact">
                <span class="ws-row-size">{{ row.is_dir ? '' : formatSize(row.size) }}</span>
                <span class="ws-row-time">{{ formatEpoch(row.mtime) }}</span>
                <span class="ws-row-people" :title="peopleTitle(row)">
                  <template v-if="row.participants && row.participants.length">
                    <span v-for="(p, i) in row.participants.slice(0, 2)" :key="i" class="chip">{{ p.name }}</span>
                    <span v-if="row.participants.length > 2" class="chip more">+{{ row.participants.length - 2 }}</span>
                  </template>
                  <span v-else class="chip none">—</span>
                </span>
              </template>
            </template>
          </div>
        </div>
      </div>

      <div class="ws-foot">
        <span class="ws-count">{{ rows.length }} 项</span>
        <span v-if="copiedPath" class="ws-copy-hint">📋 已复制：{{ copiedName }}</span>
        <span class="ws-legend" :class="{ on: !compact }" :title="compact ? '点击显示 大小·修改时间·参与者' : '点击隐藏 大小·修改时间·参与者（文件名更多空间）'"
              @click="compact = !compact">大小·修改时间·参与者</span>
      </div>
    </div>

    <!-- 回收站：按项目树层级绘制被删文件卡片 -->
    <div v-if="trashOpen" class="ws-trash glass" :style="trashStyle"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="wt-head" @mousedown="startTrashDrag">
        <span class="wt-ico">🗑</span>
        <span class="wt-title">回收站 · {{ trashItems.length }}</span>
        <div class="wt-actions">
          <button class="ws-btn" @click.stop="selectAll" :disabled="!trashItems.length">全选</button>
          <button class="ws-btn" @click.stop="clearSel" :disabled="!selected.size">清空选择</button>
          <button class="ws-btn ok" @click.stop="restoreSelected" :disabled="!selected.size || busy">↩ 还原</button>
          <button class="ws-btn danger" @click.stop="purgeSelected" :disabled="!selected.size || busy">🗑 彻底删除</button>
          <button class="ws-btn" @click.stop="trashOpen = false" title="收起回收站">✕</button>
        </div>
      </div>
      <div class="wt-body">
        <div v-if="busy" class="wt-empty">处理中…</div>
        <div v-else-if="!trashItems.length" class="wt-empty">回收站为空</div>
        <div v-else class="wt-grid">
          <div v-for="it in trashItems" :key="keyOf(it)"
               class="wt-card" :class="{ selected: selected.has(keyOf(it)), done: it.purged, disabled: !it.restorable && !it.purged }"
               :style="{ marginLeft: it.depth * 18 + 'px' }"
               @click="toggleSel(it)">
            <div class="wt-card-top">
              <span class="wt-card-ico">{{ it.is_dir ? '📁' : iconFor(it.name) }}</span>
              <span class="wt-card-name" :title="it.rel">{{ it.name }}</span>
              <span class="wt-check">{{ selected.has(keyOf(it)) ? '☑' : '☐' }}</span>
            </div>
            <div class="wt-card-meta"><span class="k">大小</span>{{ formatSize(it.size) }}</div>
            <div class="wt-card-meta"><span class="k">删除</span>{{ formatTs(it.deleted_at) }}</div>
            <div class="wt-card-meta"><span class="k">删除者</span>{{ it.deleter }}</div>
            <div v-if="it.purged" class="wt-card-tag">已彻底删除</div>
            <div v-else-if="it.restored" class="wt-card-tag ok">已还原</div>
            <div v-else-if="!it.restorable" class="wt-card-tag warn">不可还原（目标已存在）</div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- 右键菜单（始终 fixed，避免被画布 transform 影响） -->
  <Teleport to="body">
    <div v-if="ctxMenu" class="ws-ctx glass" :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }" @click.stop @contextmenu.stop.prevent>
      <template v-if="ctxMenu.row">
        <div class="ws-ctx-title">{{ ctxMenu.row.is_dir ? '📁' : '📄' }} {{ ctxMenu.row.name }}</div>
        <template v-if="!ctxMenu.row.is_dir">
          <div class="ws-ctx-item" @click="actEdit">✏️ 编辑</div>
          <div class="ws-ctx-item" @click="actCopy">📋 复制</div>
        </template>
        <template v-else>
          <div class="ws-ctx-item" :class="{ disabled: !copiedPath }" @click="actPaste">📥 粘贴{{ copiedPath ? '' : '（先复制文件）' }}</div>
        </template>
        <div class="ws-ctx-item" @click="actRename">✏️ 重命名</div>
        <div class="ws-ctx-divider" />
        <div class="ws-ctx-item danger" @click="actDelete">🗑 删除（进入回收站）</div>
      </template>
      <template v-else>
        <div class="ws-ctx-item" :class="{ disabled: !copiedPath }" @click="actPasteRoot">📥 粘贴到工作区根目录</div>
      </template>
    </div>
  </Teleport>

  <!-- 文件编辑器（双击文件打开） -->
  <Teleport to="body">
    <div v-if="editor" class="ws-modal-bg" @mousedown.self="closeEditor">
      <div class="ws-modal-card glass" @contextmenu.stop.prevent>
        <div class="ws-modal-head">
          <span class="ws-modal-title">📄 {{ editor.name }}</span>
          <span class="ws-modal-meta">{{ formatSize(editor.size) }} · {{ editor.ext || 'txt' }}</span>
          <span v-if="dirty" class="ws-modal-dirty">● 未保存</span>
          <button class="ws-btn" @click.stop="closeEditor" title="关闭">✕</button>
        </div>
        <textarea class="ws-editor-text" v-model="editContent" spellcheck="false"
                  :placeholder="editor.ext ? '' : '（不支持在线编辑的二进制文件，已只读）'"
                  :readonly="!editor.ext" @keydown.ctrl.enter.prevent="saveEditor" @keydown.esc="closeEditor"></textarea>
        <div class="ws-modal-foot">
          <span class="ws-modal-hint">Ctrl+Enter 保存 · Esc 关闭</span>
          <button class="ws-btn ok" @click="saveEditor" :disabled="saving || !editor.ext">💾 保存</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { api } from '@/api'

// —— 全局单例：坐标契约全部由宿主 MainView 下发，禁止自行推断画布位置 ——
type Bbox = { x: number; y: number; w: number; h: number; hasNodes: boolean }
type EdgeState = { xMode: number; xOff: number; yMode: number; yOff: number }

// 钉住时 viewport DOM 引用（Teleport 目标）；解锁时 'body'
const props = defineProps<{
  visible: boolean
  canvasId: number | null
  workdir: string | null
  pinned: boolean
  // 钉住位置：最近相邻边对 + 位移（画布坐标，跨画布统一）
  edge: EdgeState
  scale: number
  bbox: Bbox | null
  // viewport DOM 引用（钉住模式 Teleport 目标，由 MainView 从 CanvasBoard 暴露的 ref 下发）
  viewportEl: HTMLElement | null
  // 解锁：浮动面板的屏幕坐标
  floatX: number
  floatY: number
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'setup-workdir'): void
  (e: 'set-pinned', pinned: boolean): void
  // 钉住拖动：Overlay 计算好"最近相邻边对 + 位移"后直接下发（set，非累加）
  (e: 'set-edge', edge: EdgeState): void
  (e: 'update-float-pos', x: number, y: number): void
}>()

const PANEL_W = 360

// 面板实际高度（画布坐标，取决于内容）。用于跨画布重建 y 方向的边对位置（仅 yMode 2/3 需要）
const panelEl = ref<HTMLElement | null>(null)
const panelH = ref(420)
let sizeObserver: ResizeObserver | null = null
watch(panelEl, (el) => {
  if (!el) return
  if (!sizeObserver) sizeObserver = new ResizeObserver(() => { panelH.value = el.offsetHeight || 420 })
  sizeObserver.observe(el)
  panelH.value = el.offsetHeight || 420
}, { flush: 'post' })
onBeforeUnmount(() => sizeObserver?.disconnect())

// —— Teleport 目标：钉住时嵌入 canvas viewport DOM（让 viewport 的 transform 自动带着走）；解锁时 body ——
const teleportTarget = computed(() => {
  if (props.pinned && props.viewportEl) return props.viewportEl
  return 'body' as const
})

function cid(): number {
  if (props.canvasId == null) throw new Error('无激活画布')
  return props.canvasId
}

// —— 边对模型：根据"最近相邻边对 + 位移"从节点总包围盒反推面板画布坐标 ——
// xMode: 0=a左↔b左  1=a左↔b右  2=a右↔b左  3=a右↔b右
// yMode: 0=a上↔b上  1=a上↔b下  2=a下↔b上  3=a下↔b下
function edgeToPx(e: EdgeState, b: Bbox, ph: number): { x: number; y: number } {
  const bLeft = b.x, bRight = b.x + b.w
  const bTop = b.y, bBottom = b.y + b.h
  const x = e.xMode === 0 ? bLeft + e.xOff
    : e.xMode === 1 ? bRight + e.xOff
      : e.xMode === 2 ? bLeft + e.xOff - PANEL_W
        : bRight + e.xOff - PANEL_W
  const y = e.yMode === 0 ? bTop + e.yOff
    : e.yMode === 1 ? bBottom + e.yOff
      : e.yMode === 2 ? bTop + e.yOff - ph
        : bBottom + e.yOff - ph
  return { x, y }
}

// 由面板当前画布位置反推"最近相邻边对 + 位移"（a 的边与 b 的边取距离最近的一对）
function pxToEdge(px: number, py: number, b: Bbox, ph: number): EdgeState {
  const bLeft = b.x, bRight = b.x + b.w
  const bTop = b.y, bBottom = b.y + b.h
  const aLeft = px, aRight = px + PANEL_W
  const aTop = py, aBottom = py + ph
  const xCands = [
    { mode: 0, d: Math.abs(aLeft - bLeft), off: aLeft - bLeft },
    { mode: 1, d: Math.abs(aLeft - bRight), off: aLeft - bRight },
    { mode: 2, d: Math.abs(aRight - bLeft), off: aRight - bLeft },
    { mode: 3, d: Math.abs(aRight - bRight), off: aRight - bRight },
  ]
  const yCands = [
    { mode: 0, d: Math.abs(aTop - bTop), off: aTop - bTop },
    { mode: 1, d: Math.abs(aTop - bBottom), off: aTop - bBottom },
    { mode: 2, d: Math.abs(aBottom - bTop), off: aBottom - bTop },
    { mode: 3, d: Math.abs(aBottom - bBottom), off: aBottom - bBottom },
  ]
  const xc = xCands.reduce((m, c) => (c.d < m.d ? c : m))
  const yc = yCands.reduce((m, c) => (c.d < m.d ? c : m))
  return { xMode: xc.mode, xOff: xc.off, yMode: yc.mode, yOff: yc.off }
}

// —— 位置：两套坐标系 ——
const panelStyle = computed(() => {
  if (!props.pinned) {
    // 解锁浮动：body 下 fixed 屏幕像素
    return {
      position: 'fixed' as const,
      left: Math.max(8, Math.min(props.floatX, window.innerWidth - PANEL_W - 8)) + 'px',
      top: Math.max(8, Math.min(props.floatY, window.innerHeight - 420)) + 'px',
    }
  }
  // 钉住嵌入：viewport 内 absolute 画布坐标（由边对 + 位移从包围盒反推）
  const b = props.bbox
  if (!b?.hasNodes) {
    return { position: 'absolute' as const, left: '12px', top: '12px' }
  }
  const { x, y } = edgeToPx(props.edge, b, panelH.value)
  return { position: 'absolute' as const, left: x + 'px', top: y + 'px' }
})

// 回收站面板：跟随工作区面板右侧
const trashStyle = computed(() => {
  const p = trashPos.value
  if (p) return { left: p.x + 'px', top: p.y + 'px' }
  // 默认紧挨着面板右侧
  const left = (parseFloat(panelStyle.value.left) + PANEL_W + 12)
  const top = parseFloat(panelStyle.value.top)
  return { left: Math.max(8, left) + 'px', top: Math.max(8, top) + 'px' }
})

// —— 拖动 ——
const trashPos = ref<{ x: number; y: number } | null>(null)
const dragStart = ref<{ clientX: number; clientY: number; base: { x: number; y: number }; trash?: boolean; kind: 'float' | 'pin' } | null>(null)

function startDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  e.preventDefault()
  if (props.pinned) {
    // 钉住：记录面板当前的画布坐标（由边对 + 位移反推），拖动时重新计算"最近相邻边对 + 位移"直接下发
    const b = props.bbox
    const base = b?.hasNodes ? edgeToPx(props.edge, b, panelH.value) : { x: 12, y: 12 }
    dragStart.value = { clientX: e.clientX, clientY: e.clientY, base, kind: 'pin' }
  } else {
    // 解锁：浮动面板的屏幕坐标
    dragStart.value = {
      clientX: e.clientX, clientY: e.clientY,
      base: { x: props.floatX, y: props.floatY }, kind: 'float',
    }
  }
}
function startTrashDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  const p = trashPos.value || {
    x: parseFloat(panelStyle.value.left) + PANEL_W + 12,
    y: parseFloat(panelStyle.value.top),
  }
  dragStart.value = { clientX: e.clientX, clientY: e.clientY, base: p, trash: true, kind: 'float' }
  e.preventDefault()
}
function onMove(e: MouseEvent) {
  if (!dragStart.value) return
  const dx = e.clientX - dragStart.value.clientX
  const dy = e.clientY - dragStart.value.clientY
  if (dragStart.value.trash) {
    trashPos.value = { x: dragStart.value.base.x + dx, y: dragStart.value.base.y + dy }
    return
  }
  if (dragStart.value.kind === 'float') {
    emit('update-float-pos', dragStart.value.base.x + dx, dragStart.value.base.y + dy)
  } else if (dragStart.value.kind === 'pin') {
    // 钉住：屏幕像素差 ÷ scale 转画布差，得到新面板左上角，再反推"最近相邻边对 + 位移"直接 set
    const s = props.scale || 1
    const b = props.bbox
    if (!b?.hasNodes) return
    const px = dragStart.value.base.x + dx / s
    const py = dragStart.value.base.y + dy / s
    emit('set-edge', pxToEdge(px, py, b, panelH.value))
  }
}
function onUp() { dragStart.value = null }

// window 事件
function startListening() {
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}
function stopListening() {
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
}
watch(() => props.visible, (v) => {
  if (v) startListening()
  else { stopListening(); trashPos.value = null }
}, { immediate: true })

// —— 钉住切换 ——
function onPinToggle() {
  emit('set-pinned', !props.pinned)
  trashPos.value = null
}

// —— 项目树（单例：cid() 变化时刷新，DOM 不销毁） ——
const tree = ref<any[]>([])
const loading = ref(false)
const errorMsg = ref('')
const expanded = ref<Set<string>>(new Set())
const compact = ref(false)
const autoExpanded = ref(false)

// 避免 canvasId 连续变化时的竞态
let refreshSeq = 0

const rows = computed(() => {
  const out: any[] = []
  const walk = (nodes: any[], depth: number) => {
    for (const n of nodes) {
      out.push({ ...n, depth })
      if (n.is_dir && expanded.value.has(n.path)) walk(n.children || [], depth + 1)
    }
  }
  walk(tree.value, 0)
  return out
})

function toggleDir(p: string) {
  const s = new Set(expanded.value)
  s.has(p) ? s.delete(p) : s.add(p)
  expanded.value = s
}

function ensureExpanded() {
  if (autoExpanded.value) return
  autoExpanded.value = true
  const s = new Set<string>()
  for (const n of tree.value) if (n.is_dir) s.add(n.path)
  expanded.value = s
}

async function refresh() {
  if (props.canvasId == null || !props.workdir) { tree.value = []; return }
  const seq = ++refreshSeq
  loading.value = true
  errorMsg.value = ''
  try {
    const r: any = await api.canvases.workspace(cid())
    if (seq !== refreshSeq) return  // 已切到新画布,结果丢弃
    if (r?.ok) { tree.value = r.tree || []; ensureExpanded() }
    else { tree.value = []; errorMsg.value = r?.error || '工作区不可用' }
  } catch (e: any) {
    if (seq !== refreshSeq) return
    tree.value = []
    errorMsg.value = '加载失败：' + (e?.message || e)
  } finally {
    if (seq === refreshSeq) loading.value = false
  }
}

// 单例模式：canvasId 变化时自动刷新（DOM 不销毁）
watch(() => [props.canvasId, props.workdir], ([id, wd]) => {
  if (id != null && wd) refresh()
  else tree.value = []
}, { immediate: true })

function onRowClick(row: any) { if (row.is_dir) toggleDir(row.path) }
function onRowDblClick(row: any) {
  if (row.is_dir) { toggleDir(row.path); return }
  openEditor(row)
}

// —— 右键菜单 + 文件操作 ——
const ctxMenu = ref<{ x: number; y: number; row?: any } | null>(null)
const copiedPath = ref<string | null>(null)
const copiedName = ref('')
const renameTarget = ref<{ path: string; value: string } | null>(null)
const busy = ref(false)

function showRowCtx(e: MouseEvent, row: any) {
  ctxMenu.value = { x: e.clientX, y: e.clientY, row }
}
function showRootCtx(e: MouseEvent) {
  if (!props.workdir || !copiedPath.value) return
  ctxMenu.value = { x: e.clientX, y: e.clientY }
}
function closeCtx() { ctxMenu.value = null }

function actEdit() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (row && !row.is_dir) openEditor(row)
}
function actCopy() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row) return
  copiedPath.value = row.path
  copiedName.value = row.name
  // 同时把内容拷进系统剪贴板，方便外部使用
  if (!row.is_dir) {
    api.canvases.fileRead(cid(), row.path).then(r => {
      if (r?.ok && r.content) navigator.clipboard?.writeText(r.content).catch(() => {})
    }).catch(() => {})
  }
}
function actPaste() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row || !row.is_dir || !copiedPath.value) return
  doPaste(row.path)
}
function actPasteRoot() {
  closeCtx()
  if (!copiedPath.value) return
  doPaste('')
}
async function doPaste(destDir: string) {
  if (!copiedPath.value) return
  busy.value = true
  try {
    const base = props.workdir || ''
    const dest = destDir ? destDir : base
    await api.canvases.fileCopy(cid(), copiedPath.value, dest)
    await refresh()
  } catch (e: any) {
    alert('粘贴失败：' + (e?.message || e))
  } finally { busy.value = false }
}
function actRename() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row) return
  renameTarget.value = { path: row.path, value: row.name }
  nextTick(() => { })
}
async function commitRename() {
  if (!renameTarget.value) return
  const t = renameTarget.value
  renameTarget.value = null
  const name = t.value.trim()
  if (!name || name === t.path.split(/[\\/]/).pop()) return
  busy.value = true
  try {
    await api.canvases.fileRename(cid(), t.path, name)
    await refresh()
  } catch (e: any) {
    alert('重命名失败：' + (e?.message || e))
  } finally { busy.value = false }
}
function cancelRename() { renameTarget.value = null }
async function actDelete() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row) return
  const label = row.is_dir ? `整个文件夹「${row.name}」` : `文件「${row.name}」`
  if (!window.confirm(`删除${label}？（会进入回收站，可还原）`)) return
  busy.value = true
  try {
    await api.canvases.fileDelete(cid(), row.path)
    await refresh()
    if (trashOpen.value) await loadTrash()
  } catch (e: any) {
    alert('删除失败：' + (e?.message || e))
  } finally { busy.value = false }
}

// —— 文件编辑器 ——
const editor = ref<{ path: string; name: string; ext: string; size: number } | null>(null)
const editContent = ref('')
const dirty = ref(false)
const saving = ref(false)

async function openEditor(row: any) {
  busy.value = true
  try {
    const r: any = await api.canvases.fileRead(cid(), row.path)
    if (r?.ok) {
      editor.value = { path: r.path, name: r.name, ext: r.ext || '', size: r.size }
      editContent.value = r.content
      dirty.value = false
    } else {
      alert((r && r.error) || '读取文件失败')
    }
  } catch (e: any) {
    alert('读取文件失败：' + (e?.message || e))
  } finally { busy.value = false }
}
async function saveEditor() {
  if (!editor.value || !editor.value.ext || saving.value) return
  saving.value = true
  try {
    await api.canvases.fileWrite(cid(), editor.value.path, editContent.value)
    dirty.value = false
    await refresh()
  } catch (e: any) {
    alert('保存失败：' + (e?.message || e))
  } finally { saving.value = false }
}
function closeEditor() {
  if (dirty.value && !window.confirm('有未保存的修改，放弃并关闭？')) return
  editor.value = null
}

// —— 回收站 ——
const trashOpen = ref(false)
const trashItems = ref<any[]>([])
const selected = ref<Set<string>>(new Set())

function keyOf(it: any) { return it.conv_id + ':' + it.op_idx }

function toggleTrash() {
  trashOpen.value = !trashOpen.value
  if (trashOpen.value) { trashPos.value = null; loadTrash() }
}

async function loadTrash() {
  busy.value = true
  try {
    const r: any = await api.canvases.trash(cid())
    trashItems.value = r?.items || []
    selected.value = new Set()
  } catch (e: any) {
    alert('加载回收站失败：' + (e?.message || e))
  } finally { busy.value = false }
}

function toggleSel(it: any) {
  const s = new Set(selected.value)
  const k = keyOf(it)
  s.has(k) ? s.delete(k) : s.add(k)
  selected.value = s
}
function selectAll() { selected.value = new Set(trashItems.value.map(keyOf)) }
function clearSel() { selected.value = new Set() }

function selectedRestorable() {
  return trashItems.value.filter(it => selected.value.has(keyOf(it)) && !it.purged)
}

function payloadOf(items: any[]) {
  return items.map(it => ({ conv_id: it.conv_id, op_idx: it.op_idx }))
}

async function restoreSelected() {
  const items = selectedRestorable()
  if (!items.length) return
  busy.value = true
  try {
    const r: any = await api.canvases.restoreTrash(cid(), payloadOf(items))
    await loadTrash()
    await refresh()
    if (r && r.succeeded < r.count) {
      alert(`部分还原失败：成功 ${r.succeeded}/${r.count}（目标已存在或无快照）`)
    }
  } catch (e: any) {
    alert('还原失败：' + (e?.message || e))
  } finally { busy.value = false }
}

async function purgeSelected() {
  const items = selectedRestorable()
  if (!items.length) return
  if (!window.confirm(`彻底删除 ${items.length} 项？将移除其快照备份，之后无法再还原。`)) return
  busy.value = true
  try {
    await api.canvases.purgeTrash(cid(), payloadOf(items))
    await loadTrash()
  } catch (e: any) {
    alert('彻底删除失败：' + (e?.message || e))
  } finally { busy.value = false }
}

// —— 展示辅助 ——
function pad2(x: number) { return String(x).padStart(2, '0') }

function formatSize(n: number) {
  n = n || 0
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

function formatEpoch(epoch: number) {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  return `${d.getMonth() + 1}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function formatTs(ts: string) {
  if (!ts) return ''
  const iso = ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ts
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function iconFor(name: string) {
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (['json', 'yaml', 'yml'].includes(ext)) return '🗂'
  if (ext === 'py') return '🐍'
  if (ext === 'md') return '📝'
  if (['txt', 'log'].includes(ext)) return '📄'
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) return '🖼'
  return '📎'
}

function peopleTitle(row: any) {
  if (!row.participants || !row.participants.length) return '暂无智能体操作记录'
  return row.participants.map((p: any) => p.name).join('、')
}
</script>

<style scoped>
.ws-panel {
  position: fixed;
  width: 360px;
  max-height: min(70vh, 560px);
  display: flex;
  flex-direction: column;
  z-index: 9990;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
  overflow: hidden;
}
.ws-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; cursor: grab;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  user-select: none;
}
.ws-head:active { cursor: grabbing; }
.ws-ico { font-size: 16px; }
.ws-titles { flex: 1; min-width: 0; }
.ws-title { font-size: 13px; font-weight: 600; }
.ws-path { font-size: 10.5px; opacity: .55; font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-head-actions { display: flex; gap: 4px; }
.ws-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: #e8ecf4; border-radius: 7px; cursor: pointer;
  font-size: 11px; padding: 3px 8px; line-height: 1.6; transition: all .15s;
}
.ws-btn:hover { background: rgba(110, 168, 255, 0.18); }
.ws-btn:disabled { opacity: .4; cursor: not-allowed; }
.ws-btn.pin.active { background: rgba(255, 180, 90, 0.22); border-color: rgba(255, 180, 90, 0.5); }
.ws-btn.trash.active { background: rgba(255, 120, 120, 0.2); border-color: rgba(255, 120, 120, 0.5); }
.ws-btn.purple { background: rgba(180, 114, 255, 0.18); border-color: rgba(180, 114, 255, 0.4); color: #d4a8ff; }
.ws-btn.ok { background: rgba(100, 220, 160, 0.15); border-color: rgba(100, 220, 160, 0.4); color: #80e4b8; }
.ws-btn.danger { background: rgba(255, 80, 80, 0.18); border-color: rgba(255, 80, 80, 0.42); color: #ffb0b0; }

.ws-body { flex: 1; overflow-y: auto; padding: 6px 6px 8px; min-height: 60px; }
.ws-empty { padding: 22px 12px; text-align: center; font-size: 12px; opacity: .6; display: flex; flex-direction: column; align-items: center; gap: 10px; }

.ws-tree { display: flex; flex-direction: column; }
.ws-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 8px; border-radius: 7px; font-size: 12px;
  cursor: default; transition: background .12s;
}
.ws-row.dir { cursor: pointer; }
.ws-row:hover { background: rgba(255, 255, 255, 0.05); }
.ws-row-ico { flex-shrink: 0; width: 16px; text-align: center; }
.ws-row-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-row.dir .ws-row-name { font-weight: 500; }
.ws-trunc { flex-shrink: 0; font-size: 10px; color: #ffd18a; padding: 0 2px; }
.ws-row-size { flex-shrink: 0; width: 58px; text-align: right; font-family: Consolas, monospace; font-size: 10.5px; opacity: .7; }
.ws-row-time { flex-shrink: 0; width: 76px; text-align: right; font-family: Consolas, monospace; font-size: 10.5px; opacity: .5; }
.ws-row-people { flex-shrink: 0; display: flex; gap: 4px; }
.chip { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: rgba(110, 168, 255, 0.18); color: #a8c8ff; border: 1px solid rgba(110, 168, 255, 0.3); white-space: nowrap; }
.chip.more { background: rgba(180, 114, 255, 0.18); color: #d4a8ff; border-color: rgba(180, 114, 255, 0.3); }
.chip.none { background: transparent; color: rgba(232, 236, 244, 0.3); border-color: transparent; }

.ws-rename-input {
  flex: 1; min-width: 0; background: rgba(0, 0, 0, 0.35);
  border: 1px solid rgba(110, 168, 255, 0.6); border-radius: 6px;
  color: #e8ecf4; font-size: 12px; padding: 2px 6px; outline: none;
}

.ws-foot { display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-top: 1px solid rgba(255, 255, 255, 0.07); font-size: 10.5px; opacity: .5; }
.ws-count { flex-shrink: 0; }
.ws-copy-hint { flex: 1; min-width: 0; font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #ffd18a; }
.ws-legend { flex-shrink: 0; cursor: pointer; padding: 2px 6px; border-radius: 6px; transition: all .15s; white-space: nowrap; }
.ws-legend:hover { background: rgba(255, 255, 255, 0.08); }
.ws-legend.on { color: #a8c8ff; }

/* —— 右键菜单 —— */
.ws-ctx { position: fixed; min-width: 210px; padding: 6px; z-index: 9999; box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5); }
.ws-ctx-title { font-size: 11px; padding: 6px 10px; color: rgba(232, 236, 244, 0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(255, 255, 255, 0.08); margin-bottom: 4px; }
.ws-ctx-item { padding: 8px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; transition: background .15s; }
.ws-ctx-item:hover { background: rgba(110, 168, 255, 0.18); }
.ws-ctx-item.danger:hover { background: rgba(255, 80, 80, 0.18); color: #ff9090; }
.ws-ctx-item.disabled { opacity: .4; cursor: default; }
.ws-ctx-item.disabled:hover { background: transparent; }
.ws-ctx-divider { height: 1px; background: rgba(255, 255, 255, 0.08); margin: 4px 6px; }

/* —— 文件编辑器 —— */
.ws-modal-bg { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 9995; }
.ws-modal-card { width: 640px; max-width: 92vw; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.ws-modal-head { display: flex; align-items: center; gap: 10px; }
.ws-modal-title { font-size: 14px; font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-modal-meta { font-size: 11px; opacity: .55; font-family: Consolas, monospace; white-space: nowrap; }
.ws-modal-dirty { font-size: 11px; color: #ffd18a; }
.ws-editor-text {
  width: 100%; height: 420px; background: rgba(0, 0, 0, 0.35);
  border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 8px;
  color: #e8ecf4; font-size: 12.5px; font-family: Consolas, "Courier New", monospace;
  padding: 10px; outline: none; resize: none; line-height: 1.55;
}
.ws-editor-text:focus { border-color: rgba(110, 168, 255, 0.6); }
.ws-editor-text[readonly] { opacity: .7; }
.ws-modal-foot { display: flex; justify-content: space-between; align-items: center; }
.ws-modal-hint { font-size: 10.5px; opacity: .5; }

/* —— 回收站浮层 —— */
.ws-trash {
  position: fixed; width: 380px; max-height: min(70vh, 560px);
  display: flex; flex-direction: column; z-index: 9991;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
  border-color: rgba(255, 120, 120, 0.28);
  overflow: hidden;
}
.wt-head {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 90, 90, 0.06);
}
.wt-head:active { cursor: grabbing; }
.wt-ico { font-size: 14px; }
.wt-title { font-size: 12.5px; font-weight: 600; flex: 1; }
.wt-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.wt-body { flex: 1; overflow-y: auto; padding: 10px; }
.wt-empty { padding: 24px 12px; text-align: center; font-size: 12px; opacity: .6; }
.wt-grid { display: flex; flex-direction: column; gap: 8px; }
.wt-card {
  border: 1px solid rgba(255, 120, 120, 0.32);
  background: rgba(30, 18, 24, 0.55);
  border-radius: 10px; padding: 8px 10px; cursor: pointer;
  transition: all .15s; position: relative;
}
.wt-card:hover { border-color: rgba(255, 150, 150, 0.6); box-shadow: 0 0 14px rgba(255, 90, 90, 0.18); }
.wt-card.selected { border-color: #ff8a8a; box-shadow: 0 0 0 2px rgba(255, 120, 120, 0.35), 0 0 16px rgba(255, 90, 90, 0.28); background: rgba(60, 26, 30, 0.6); }
.wt-card.done { opacity: .5; }
.wt-card.disabled { opacity: .75; }
.wt-card-top { display: flex; align-items: center; gap: 6px; margin-bottom: 5px; }
.wt-card-ico { flex-shrink: 0; }
.wt-card-name { flex: 1; min-width: 0; font-size: 12px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wt-check { flex-shrink: 0; font-size: 13px; opacity: .8; }
.wt-card-meta { font-size: 10.5px; opacity: .72; display: flex; gap: 6px; line-height: 1.6; }
.wt-card-meta .k { flex-shrink: 0; width: 34px; opacity: .6; }
.wt-card-tag { margin-top: 5px; font-size: 10px; color: #ffb0b0; }
.wt-card-tag.ok { color: #80e4b8; }
.wt-card-tag.warn { color: #ffd18a; }
</style>
