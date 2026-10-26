<template>
  <!-- 钉住时嵌入画布视口 DOM（随 transform 自动跟随平移/缩放）；未钉住时浮动（body 下 fixed） -->
  <Teleport :to="teleportTarget">
    <!-- 工作区面板（可拖动） -->
    <div class="ws-panel glass" :style="panelStyle" :class="{ embedded: pinned }" ref="panelEl"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="ws-head" @mousedown="startDrag" @wheel.stop.prevent="onHeadWheel">
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
          <div v-for="row in rows" :key="row.path" class="ws-row"
               :class="{ dir: row.is_dir, changed: flashPaths.has(row.path), dragging: dragPath === row.path }"
               :style="{ paddingLeft: 8 + row.depth * 16 + 'px' }"
               draggable="true"
               @dragstart="onRowDragStart($event, row)"
               @dragend="onRowDragEnd"
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
        <span v-else-if="copyPathHint" class="ws-copy-hint" :title="copyPathHint">🔗 地址已复制：{{ copyPathHint }}</span>
        <span class="ws-legend" :class="{ on: !compact }" :title="compact ? '点击显示 大小·修改时间·参与者' : '点击隐藏 大小·修改时间·参与者（文件名更多空间）'"
              @click="compact = !compact">大小·修改时间·参与者</span>
      </div>
    </div>

    <!-- 回收站：按项目树层级绘制被删文件卡片 -->
    <div v-if="trashOpen" class="ws-trash glass" :class="{ embedded: pinned }" :style="trashStyle"
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
    <div v-if="ctxMenu" ref="ctxEl" class="ws-ctx glass"
         :style="{ left: ctxPos.x + 'px', top: ctxPos.y + 'px' }"
         @click.stop @contextmenu.stop.prevent @mousedown.stop>
      <template v-if="ctxMenu.row">
        <div class="ws-ctx-title">{{ ctxMenu.row.is_dir ? '📁' : '📄' }} {{ ctxMenu.row.name }}</div>
        <template v-if="!ctxMenu.row.is_dir">
          <div v-if="canPreview(ctxMenu.row.name)" class="ws-ctx-item" @click="actPreview"
               title="用内置简易查看器预览（双击文件同样打开）">👁 预览</div>
          <div class="ws-ctx-item" @click="actRun"
               :title="rowIsPy(ctxMenu.row) ? '双击 .py 文件同样打开控制台' : '用系统默认程序打开（双击非程序文件则用内置查看器预览）'">
            {{ rowIsPy(ctxMenu.row) ? '🖥 控制台运行' : '↗️ 默认程序打开' }}
          </div>
          <div class="ws-ctx-item" @click="actEdit">✏️ 编辑</div>
          <div class="ws-ctx-item" @click="actCopy">📋 复制</div>
        </template>
        <template v-else>
          <div class="ws-ctx-item" :class="{ disabled: !copiedPath }" @click="actPaste">📥 粘贴{{ copiedPath ? '' : '（先复制文件）' }}</div>
        </template>
        <div class="ws-ctx-item" @click="actRename">✏️ 重命名</div>
        <div class="ws-ctx-item" @click="actCopyPath" title="复制绝对路径到系统剪贴板">🔗 复制文件地址</div>
        <div class="ws-ctx-divider" />
        <div class="ws-ctx-item danger" @click="actDelete">🗑 删除（进入回收站）</div>
      </template>
      <template v-else>
        <div class="ws-ctx-item" :class="{ disabled: !copiedPath }" @click="actPasteRoot">📥 粘贴到工作区根目录</div>
      </template>
    </div>
  </Teleport>

  <!-- 文件编辑器浮窗（可拖动，不遮挡其他操作） -->
  <Teleport to="body">
    <div v-if="editor" class="ws-edit-float glass" :style="editFloatStyle"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="ws-edit-float-head" @mousedown="startEditDrag">
        <span class="ws-modal-title">📄 {{ editor.name }}</span>
        <span class="ws-modal-meta">{{ formatSize(editor.size) }} · {{ editor.ext || 'txt' }}</span>
        <span v-if="dirty" class="ws-modal-dirty">● 未保存</span>
        <button class="ws-btn" @click.stop="closeEditor" title="关闭">✕</button>
      </div>
      <textarea class="ws-editor-text" v-model="editContent" spellcheck="false"
                :placeholder="editor.ext ? '' : '（不支持在线编辑的二进制文件，已只读）'"
                :readonly="!editor.ext" @keydown.ctrl.enter.prevent="saveEditor" @keydown.esc="closeEditor"></textarea>
      <div class="ws-modal-foot">
        <span class="ws-modal-hint">拖动标题栏移动 · Ctrl+Enter 保存 · Esc 关闭</span>
        <button class="ws-btn ok" @click="saveEditor" :disabled="saving || !editor.ext">💾 保存</button>
      </div>
    </div>
  </Teleport>

  <!-- Python 控制台浮窗（可拖动：运行 / 停止 / 输入 / 实时输出） -->
  <Teleport to="body">
    <div v-if="pyConsole" class="ws-console-float glass" :style="consoleFloatStyle"
         @contextmenu.stop.prevent @mousedown.stop @wheel.stop>
      <div class="ws-console-head" @mousedown="startConsoleDrag">
        <span class="ws-modal-title">🐍 {{ pyConsole.name }}</span>
        <span class="ws-modal-meta" :class="pyConsole.exitCode && pyConsole.exitCode !== 0 ? 'warn' : 'ok'">
          <template v-if="pyConsole.starting">⏳ 启动中…</template>
          <template v-else-if="pyConsole.running">● 运行中{{ pyConsole.pid ? ' · PID ' + pyConsole.pid : '' }}</template>
          <template v-else-if="pyConsole.exitCode === 0">✓ 已退出（正常）</template>
          <template v-else>⚠ 已退出（退出码 {{ pyConsole.exitCode ?? '?' }}）</template>
        </span>
        <button class="ws-btn ok" @click.stop="startConsole"
                :disabled="pyConsole.starting || pyConsole.running" title="运行 / 重新运行">▶️ 运行</button>
        <button class="ws-btn danger" @click.stop="stopConsole"
                :disabled="!pyConsole.running" title="停止（含子进程）">⏹ 停止</button>
        <button class="ws-btn" @click.stop="closeConsole" title="关闭">✕</button>
      </div>
      <pre class="ws-console-output" ref="consoleOutEl">{{ pyConsole.output || '（等待程序输出…）' }}</pre>
      <div class="ws-console-input">
        <input v-model="consoleInput" @keydown.enter.prevent="sendConsoleInput"
               :disabled="!pyConsole.running" spellcheck="false"
               placeholder="输入内容，回车发送到程序 stdin" />
        <button class="ws-btn ok" @click="sendConsoleInput"
                :disabled="!pyConsole.running || !consoleInput">发送</button>
      </div>
      <div class="ws-modal-foot">
        <span class="ws-modal-hint">拖动标题栏移动 · 回车发送输入 · Esc 关闭</span>
        <button class="ws-btn ok" @click="clearConsoleOutput">🧹 清空输出</button>
      </div>
    </div>
  </Teleport>

  <!-- 内置简易查看器（双击非程序文件 / 右键"预览"） -->
  <FileViewer :canvas-id="canvasId" :file="viewerFile"
              @close="viewerFile = null" @edit="onViewerEdit" />
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { api } from '@/api'
import FileViewer, { type ViewerFile } from './FileViewer.vue'

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
/**
 * 浮动模式下的视口钳制：用面板实际高度（而非写死的 420）计算最大 top，
 * 保证面板既能完整留在视口内，又能一直拖到界面底部。
 * 展示与拖动起点共用此结果，避免"存的值越界 → 拖动时面板跳一下"。
 */
function clampFloat(x: number, y: number) {
  const h = panelH.value || panelEl.value?.offsetHeight || 420
  return {
    x: Math.max(8, Math.min(x, Math.max(8, window.innerWidth - PANEL_W - 8))),
    y: Math.max(8, Math.min(y, Math.max(8, window.innerHeight - h - 8))),
  }
}

const panelStyle = computed(() => {
  if (!props.pinned) {
    // 解锁浮动：body 下 fixed 屏幕像素
    const p = clampFloat(props.floatX, props.floatY)
    return {
      position: 'fixed' as const,
      left: p.x + 'px',
      top: p.y + 'px',
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

// 回收站面板：紧跟工作区面板右侧，坐标系与面板一致（钉住=画布坐标 absolute，浮动=屏幕像素 fixed）
const trashStyle = computed(() => {
  const pos = props.pinned ? 'absolute' as const : 'fixed' as const
  const p = trashPos.value
  if (p) return { position: pos, left: p.x + 'px', top: p.y + 'px' }
  // 默认紧挨面板右侧（panelStyle 的 left/top 与面板同一坐标系）
  const left = (parseFloat(panelStyle.value.left) + PANEL_W + 12)
  const top = parseFloat(panelStyle.value.top)
  return { position: pos, left: Math.max(8, left) + 'px', top: Math.max(8, top) + 'px' }
})

// —— 拖动 ——
const trashPos = ref<{ x: number; y: number } | null>(null)
const dragStart = ref<{ clientX: number; clientY: number; base: { x: number; y: number }; trash?: boolean; kind: 'float' | 'pin' } | null>(null)

/**
 * 工作区顶部（标题栏）滚轮：钉住嵌入画布时 → 缩放画布；
 * 浮动时保持原行为（不滚动页面）。
 * 派发一个从 viewport 向上冒泡的合成 wheel 事件，复用 CanvasBoard 的 onWheel 缩放逻辑，
 * 锚点即鼠标位置，与画布其他区域的滚轮缩放行为一致。
 */
function onHeadWheel(e: WheelEvent) {
  if (!props.pinned || !props.viewportEl) return
  props.viewportEl.dispatchEvent(new WheelEvent('wheel', {
    clientX: e.clientX,
    clientY: e.clientY,
    deltaX: e.deltaX,
    deltaY: e.deltaY,
    deltaMode: e.deltaMode,
    bubbles: true,
    cancelable: true,
  }))
}

function startDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  e.preventDefault()
  // 拖动面板时让回收站重新贴合面板右侧（清掉之前的独立拖拽偏移）
  trashPos.value = null
  if (props.pinned) {
    // 钉住：记录面板当前的画布坐标（由边对 + 位移反推），拖动时重新计算"最近相邻边对 + 位移"直接下发
    const b = props.bbox
    const base = b?.hasNodes ? edgeToPx(props.edge, b, panelH.value) : { x: 12, y: 12 }
    dragStart.value = { clientX: e.clientX, clientY: e.clientY, base, kind: 'pin' }
  } else {
    // 解锁：浮动面板的屏幕坐标（取实际展示的钳制位置作为起点，避免跳变）
    dragStart.value = {
      clientX: e.clientX, clientY: e.clientY,
      base: clampFloat(props.floatX, props.floatY), kind: 'float',
    }
  }
}
function startTrashDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  // base 用当前面板右侧的位置（坐标系与面板一致），保证拖动前后坐标系匹配
  const base = trashPos.value || {
    x: parseFloat(panelStyle.value.left) + PANEL_W + 12,
    y: parseFloat(panelStyle.value.top),
  }
  dragStart.value = { clientX: e.clientX, clientY: e.clientY, base, trash: true, kind: props.pinned ? 'pin' : 'float' }
  e.preventDefault()
}
function onMove(e: MouseEvent) {
  if (!dragStart.value) return
  const dx = e.clientX - dragStart.value.clientX
  const dy = e.clientY - dragStart.value.clientY
  if (dragStart.value.trash) {
    // 钉住：屏幕差 ÷ scale → 画布差；浮动：直接用屏幕差
    const s = props.pinned ? (props.scale || 1) : 1
    trashPos.value = { x: dragStart.value.base.x + dx / s, y: dragStart.value.base.y + dy / s }
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
  trashPos.value = null   // 切换坐标系，让回收站重新贴合面板右侧
}

// —— 项目树（单例：cid() 变化时刷新，DOM 不销毁） ——
const tree = ref<any[]>([])
const loading = ref(false)
const errorMsg = ref('')
const expanded = ref<Set<string>>(new Set())   // 默认折叠：清空初始状态，只在用户点击时展开
const compact = ref(false)

// 避免 canvasId 连续变化时的竞态
let refreshSeq = 0
// 后端树版本号：轮询时带上，未变化后端只回 {changed:false}（省流量、秒级实时）
const wsRev = ref(-1)
// 刚发生变动的行路径（短暂高亮，提示"只有这些变了"）
const flashPaths = ref<Set<string>>(new Set())
let flashTimer: number | null = null

function nodeSig(n: any): string {
  if (!n) return ''
  const ppl = (n.participants || []).map((p: any) => `${p.id}:${p.name}:${p.last_op || ''}:${p.last_ts || ''}`).join(',')
  return [n.name, n.rel, n.is_dir ? 1 : 0, n.size, n.mtime, n.truncated ? 1 : 0,
    n.created_by || '', n.modified_by || '', n.last_ts || '', ppl].join('|')
}

/** 对比新旧树，收集真正变动（新增/修改/删除涉及）的路径，用于高亮提示。 */
function diffChangedPaths(oldList: any[], newList: any[], out: string[]) {
  const oldMap = new Map<string, any>((oldList || []).map((n: any) => [n.path, n]))
  for (const n of newList || []) {
    const o = oldMap.get(n.path)
    if (!o || nodeSig(o) !== nodeSig(n)) out.push(n.path)
    if (n.children?.length) diffChangedPaths(o?.children || [], n.children, out)
  }
}

function flashChanged(paths: string[]) {
  if (!paths.length) return
  const s = new Set(flashPaths.value)
  for (const p of paths) s.add(p)
  flashPaths.value = s
  if (flashTimer != null) clearTimeout(flashTimer)
  flashTimer = window.setTimeout(() => { flashPaths.value = new Set(); flashTimer = null }, 1100)
}

/** 应用新树：保留展开状态，仅变动的行做高亮；未变的行 DOM 不动（界面不闪）。 */
function applyTree(newTree: any[]) {
  const changed: string[] = []
  diffChangedPaths(tree.value, newTree, changed)
  tree.value = newTree || []
  flashChanged(changed)
}

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

async function refresh() {
  if (props.canvasId == null || !props.workdir) { tree.value = []; wsRev.value = -1; return }
  const seq = ++refreshSeq
  // 已有树时不进入 loading：避免整树被"加载中…"替换造成的闪烁
  const firstLoad = !tree.value.length
  if (firstLoad) loading.value = true
  errorMsg.value = ''
  try {
    const r: any = await api.canvases.workspace(cid(), -1)
    if (seq !== refreshSeq) return  // 已切到新画布,结果丢弃
    if (r?.ok) {
      wsRev.value = typeof r.rev === 'number' ? r.rev : -1
      applyTree(r.tree || [])
    } else { tree.value = []; errorMsg.value = r?.error || '工作区不可用' }
  } catch (e: any) {
    if (seq !== refreshSeq) return
    if (firstLoad) { tree.value = []; errorMsg.value = '加载失败：' + (e?.message || e) }
  } finally {
    if (seq === refreshSeq) loading.value = false
  }
}

// —— 实时更新：面板打开期间每 1.2s 轮询一次差量接口；有变动才下发全量树并合并 ——
const POLL_MS = 1200
let pollTimer: number | null = null
let polling = false
let nudgeQueued = false
async function pollWorkspace() {
  if (polling || document.hidden) return
  const id = props.canvasId
  if (id == null || !props.workdir) return
  const seq = refreshSeq
  polling = true
  try {
    const r: any = await api.canvases.workspace(id, wsRev.value)
    if (props.canvasId !== id || seq !== refreshSeq) return
    if (r?.ok && r.changed) {
      wsRev.value = typeof r.rev === 'number' ? r.rev : -1
      applyTree(r.tree || [])
    } else if (r?.ok && typeof r.rev === 'number') {
      wsRev.value = r.rev
    }
  } catch { /* 网络抖动忽略，下一轮重试 */ } finally {
    polling = false
    if (nudgeQueued) { nudgeQueued = false; pollWorkspace() }
  }
}
// 智能体刚写完文件（conversation store 广播）→ 立刻刷一次，不等轮询间隔
function onWorkspaceChangedNudge() {
  if (polling) nudgeQueued = true
  else pollWorkspace()
}
onMounted(() => {
  if (pollTimer == null) pollTimer = window.setInterval(pollWorkspace, POLL_MS)
  window.addEventListener('agentcluster:workspace-changed', onWorkspaceChangedNudge)
})
onBeforeUnmount(() => {
  if (pollTimer != null) { clearInterval(pollTimer); pollTimer = null }
  if (flashTimer != null) { clearTimeout(flashTimer); flashTimer = null }
  if (copyPathTimer != null) { clearTimeout(copyPathTimer); copyPathTimer = null }
  window.removeEventListener('agentcluster:workspace-changed', onWorkspaceChangedNudge)
})

// 单例模式：canvasId / workdir 变化时清空展开状态（默认折叠），并刷新
watch(() => [props.canvasId, props.workdir], ([id, wd]) => {
  expanded.value = new Set()   // 切换工作区时回到默认折叠
  wsRev.value = -1
  if (id != null && wd) refresh()
  else tree.value = []
}, { immediate: true })

function onRowClick(row: any) { if (row.is_dir) toggleDir(row.path) }
function onRowDblClick(row: any) {
  if (row.is_dir) { toggleDir(row.path); return }
  // 非程序文件 → 内置简易查看器；程序文件（py/js/脚本/可执行…）→ 运行/系统打开
  if (canPreview(row.name)) { openViewer(row); return }
  runFile(row)
}

// —— 右键菜单 + 文件操作 ——
const ctxMenu = ref<{ x: number; y: number; row?: any } | null>(null)
const ctxEl = ref<HTMLElement | null>(null)
// 实际渲染坐标（会按视口钳制，避免超出界面）
const ctxPos = ref({ x: 0, y: 0 })
const MARGIN = 4
const copiedPath = ref<string | null>(null)
const copiedName = ref('')
const copiedFromWd = ref<string | ''>('')
const copiedSrcAbsolute = ref<boolean>(false)
const renameTarget = ref<{ path: string; value: string } | null>(null)
const busy = ref(false)
// 复制文件地址：底栏短暂提示 + 正在拖拽的行
const copyPathHint = ref('')
let copyPathTimer: number | null = null
const dragPath = ref('')

function clampCtx() {
  const el = ctxEl.value
  if (!el || !ctxMenu.value) return
  const w = el.offsetWidth || 220
  const h = el.offsetHeight || 200
  const vw = window.innerWidth || document.documentElement.clientWidth
  const vh = window.innerHeight || document.documentElement.clientHeight
  const x = Math.max(MARGIN, Math.min(ctxMenu.value.x, vw - w - MARGIN))
  const y = Math.max(MARGIN, Math.min(ctxMenu.value.y, vh - h - MARGIN))
  ctxPos.value = { x, y }
}

function showRowCtx(e: MouseEvent, row: any) {
  ctxPos.value = { x: e.clientX, y: e.clientY }
  ctxMenu.value = { x: e.clientX, y: e.clientY, row }
  nextTick(clampCtx)
}
function showRootCtx(e: MouseEvent) {
  if (!props.workdir || !copiedPath.value) return
  ctxPos.value = { x: e.clientX, y: e.clientY }
  ctxMenu.value = { x: e.clientX, y: e.clientY }
  nextTick(clampCtx)
}
function closeCtx() { ctxMenu.value = null }

// 点击其他区域 / 右键 / 失焦 / 滚轮 / Esc → 关闭右键菜单
function onCtxMouseDown(e: MouseEvent) {
  if (!ctxMenu.value) return
  // 点中菜单本体（含子项）则不关——事件会从菜单冒泡到 window
  if ((e.target as HTMLElement)?.closest?.('.ws-ctx')) return
  ctxMenu.value = null
}
function onCtxContextMenu() { ctxMenu.value = null }
function onCtxBlur() { if (ctxMenu.value) ctxMenu.value = null }
function onCtxKeydown(e: KeyboardEvent) { if (e.key === 'Escape' && ctxMenu.value) ctxMenu.value = null }
onMounted(() => {
  window.addEventListener('mousedown', onCtxMouseDown)
  window.addEventListener('contextmenu', onCtxContextMenu)
  window.addEventListener('blur', onCtxBlur)
  window.addEventListener('keydown', onCtxKeydown)
  window.addEventListener('wheel', onCtxBlur, { passive: true })
})
onBeforeUnmount(() => {
  window.removeEventListener('mousedown', onCtxMouseDown)
  window.removeEventListener('contextmenu', onCtxContextMenu)
  window.removeEventListener('blur', onCtxBlur)
  window.removeEventListener('keydown', onCtxKeydown)
  window.removeEventListener('wheel', onCtxBlur)
})

function actEdit() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (row && !row.is_dir) openEditor(row)
}
function actRun() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (row && !row.is_dir) runFile(row)
}
function actPreview() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (row && !row.is_dir) openViewer(row)
}
function actCopy() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row) return
  // row.path 是绝对路径；同时记录来源 workdir，用于跨工作区粘贴时区分同源/跨源
  copiedPath.value = row.path
  copiedName.value = row.name
  copiedFromWd.value = props.workdir || ''
  // 同时把内容拷进系统剪贴板，方便外部使用
  if (!row.is_dir) {
    api.canvases.fileRead(cid(), row.path).then(r => {
      if (r?.ok && r.content) navigator.clipboard?.writeText(r.content).catch(() => {})
    }).catch(() => {})
  }
}
/** 复制文本到系统剪贴板（clipboard API 不可用时兜底 execCommand） */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* 无权限/非安全上下文 → 走兜底 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch { return false }
}
/** 右键菜单：复制文件/文件夹的绝对地址到系统剪贴板 */
async function actCopyPath() {
  const row = ctxMenu.value?.row
  closeCtx()
  if (!row) return
  if (!(await copyText(row.path))) { alert('复制文件地址失败'); return }
  copyPathHint.value = row.path
  if (copyPathTimer != null) clearTimeout(copyPathTimer)
  copyPathTimer = window.setTimeout(() => { copyPathHint.value = ''; copyPathTimer = null }, 2500)
}
/** 拖拽：把绝对路径写进 dataTransfer，供右侧对话输入框读取 */
function onRowDragStart(e: DragEvent, row: any) {
  dragPath.value = row.path
  const dt = e.dataTransfer
  if (!dt) return
  dt.effectAllowed = 'copy'
  dt.setData('application/x-agentcluster-path', row.path)
  dt.setData('text/plain', row.path)   // 兜底：其他程序/组件读 text/plain 也能拿到
}
function onRowDragEnd() { dragPath.value = '' }
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
function sameWorkdir(a: string, b: string) {
  if (!a || !b) return false
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  return norm(a) === norm(b)
}
function toRelative(absPath: string, wd: string): string | null {
  // 把 absPath 转为相对 wd 的路径；不在其下返回 null
  if (!wd) return null
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '')
  const a = norm(absPath).toLowerCase()
  const w = norm(wd).toLowerCase()
  if (a === w) return ''
  if (!a.startsWith(w + '/')) return null
  return a.slice(w.length + 1)
}
async function doPaste(destDir: string) {
  if (!copiedPath.value) return
  busy.value = true
  try {
    const base = props.workdir || ''
    const dest = destDir ? destDir : base
    // 同源粘贴：把 src 转成相对路径（后端 _resolve_under 才能接受）；
    // 跨源粘贴：保留绝对路径（后端检测到绝对路径会跳过 _resolve_under 直接复制）
    const src = sameWorkdir(copiedFromWd.value, base)
      ? (toRelative(copiedPath.value, base) ?? copiedPath.value)
      : copiedPath.value
    await api.canvases.fileCopy(cid(), src, dest)
    await refresh()
  } catch (e: any) {
    let msg = '粘贴失败：' + (e?.message || e)
    if (!sameWorkdir(copiedFromWd.value, props.workdir || '')) {
      msg += `\n\n（从工作区「${copiedFromWd.value}」复制，粘贴到「${props.workdir}」）`
    }
    alert(msg)
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
    closeViewerIfMatches(t.path)
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
    closeViewerIfMatches(row.path)
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

// 编辑浮窗位置（屏幕像素，跨编辑保留）
const editPos = ref({ x: 120, y: 80 })
const EDIT_W = 640
const EDIT_H = 500
const editDragStart = ref<{ clientX: number; clientY: number; base: { x: number; y: number } } | null>(null)
const editFloatStyle = computed(() => {
  const p = editPos.value
  const x = Math.max(0, Math.min(p.x, window.innerWidth - EDIT_W))
  const y = Math.max(0, Math.min(p.y, window.innerHeight - 120))
  return { position: 'fixed' as const, left: x + 'px', top: y + 'px', width: EDIT_W + 'px', height: EDIT_H + 'px' }
})
function startEditDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  editDragStart.value = { clientX: e.clientX, clientY: e.clientY, base: { x: editPos.value.x, y: editPos.value.y } }
  e.preventDefault()
}
function onEditDragMove(e: MouseEvent) {
  const s = editDragStart.value
  if (!s) return
  editPos.value = { x: s.base.x + (e.clientX - s.clientX), y: s.base.y + (e.clientY - s.clientY) }
}
function onEditDragUp() { editDragStart.value = null }
function onEditKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && editor.value) closeEditor()
}
onMounted(() => {
  window.addEventListener('mousemove', onEditDragMove)
  window.addEventListener('mouseup', onEditDragUp)
  window.addEventListener('keydown', onEditKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onEditDragMove)
  window.removeEventListener('mouseup', onEditDragUp)
  window.removeEventListener('keydown', onEditKeydown)
})

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

// —— 双击文件：非程序文件 → 内置查看器；.py 开控制台；其余程序文件 → 系统打开 ——
function isPyFile(name: string) {
  return (name.split('.').pop() || '').toLowerCase() === 'py'
}
function rowIsPy(row: any) { return !!row && isPyFile(row.name || '') }

// "运行程序"类扩展名（与后端 workspace.RUN_EXTS 对齐）：双击/右键仍走运行或系统打开
const PROGRAM_EXTS = new Set([
  'py', 'pyw', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'sh', 'bash', 'bat', 'cmd',
  'ps1', 'psm1', 'html', 'htm', 'exe', 'msi', 'jar', 'vbs', 'wsf',
])

/** 是否为可用内置查看器预览的"非运行程序"文件 */
function canPreview(name: string) {
  const ext = (name.split('.').pop() || '').toLowerCase()
  return !PROGRAM_EXTS.has(ext)
}

// —— 内置查看器 ——
const viewerFile = ref<ViewerFile | null>(null)
function openViewer(row: any) {
  viewerFile.value = { path: row.path, name: row.name, size: row.size || 0, mtime: row.mtime }
}
function onViewerEdit(f: ViewerFile) {
  openEditor({ path: f.path, name: f.name })
}
/** 文件被删除/重命名后关闭指向它的查看器，避免显示失效内容 */
function closeViewerIfMatches(path: string) {
  if (viewerFile.value?.path === path) viewerFile.value = null
}

async function runFile(row: any) {
  if (row.is_dir) return
  if (isPyFile(row.name)) { await openPyConsole(row); return }
  try {
    await api.canvases.fileRun(cid(), row.path)
  } catch (e: any) {
    alert('打开失败：' + (e?.message || e))
  }
}

// —— Python 控制台 ——
// pyConsole: null 无 / { name, path, sessionId, pid, running, exitCode, starting, output }
const pyConsole = ref<{
  name: string; path: string; sessionId: string | null; pid: number | null;
  running: boolean; exitCode: number | null; starting: boolean; output: string
} | null>(null)
const consoleInput = ref('')
const consoleOutEl = ref<HTMLElement | null>(null)
let consoleAbort: AbortController | null = null
let consoleGen = 0   // 会话代际：防止「重新运行 / 关闭」后旧流事件污染新会话

// 浮窗位置（屏幕像素，跨会话保留）
const consolePos = ref({ x: 90, y: 90 })
const CONSOLE_W = 620
const CONSOLE_H = 470
const consoleDragStart = ref<{ clientX: number; clientY: number; base: { x: number; y: number } } | null>(null)
const consoleFloatStyle = computed<Record<string, string>>(() => {
  const p = consolePos.value
  const x = Math.max(0, Math.min(p.x, window.innerWidth - CONSOLE_W))
  const y = Math.max(0, Math.min(p.y, window.innerHeight - 120))
  return { position: 'fixed', left: x + 'px', top: y + 'px', width: CONSOLE_W + 'px', height: CONSOLE_H + 'px' }
})

function startConsoleDrag(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) return
  consoleDragStart.value = { clientX: e.clientX, clientY: e.clientY, base: { x: consolePos.value.x, y: consolePos.value.y } }
  e.preventDefault()
}
function onConsoleDragMove(e: MouseEvent) {
  const s = consoleDragStart.value
  if (!s) return
  consolePos.value = { x: s.base.x + (e.clientX - s.clientX), y: s.base.y + (e.clientY - s.clientY) }
}
function onConsoleDragUp() { consoleDragStart.value = null }
function onConsoleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && pyConsole.value) closeConsole()
}
onMounted(() => {
  window.addEventListener('mousemove', onConsoleDragMove)
  window.addEventListener('mouseup', onConsoleDragUp)
  window.addEventListener('keydown', onConsoleKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('mousemove', onConsoleDragMove)
  window.removeEventListener('mouseup', onConsoleDragUp)
  window.removeEventListener('keydown', onConsoleKeydown)
  consoleGen++
  consoleAbort?.abort()
  if (pyConsole.value?.sessionId) api.canvases.filePyClose(cid(), pyConsole.value.sessionId).catch(() => { })
})

function scrollConsole() {
  const el = consoleOutEl.value
  if (el) el.scrollTop = el.scrollHeight
}

// 断开当前 SSE 流并清理后端会话（防止重新运行/切换文件时旧进程泄漏）
async function dropSession(sid: string | null | undefined) {
  consoleAbort?.abort()
  consoleAbort = null
  if (!sid) return
  try { await api.canvases.filePyClose(cid(), sid) } catch { }
}

async function openPyConsole(row: any) {
  const prevSid = pyConsole.value?.sessionId
  if (prevSid && pyConsole.value) pyConsole.value.sessionId = null
  await dropSession(prevSid)
  pyConsole.value = {
    name: row.name, path: row.path, sessionId: null, pid: null,
    running: false, exitCode: null, starting: false, output: '',
  }
  await startConsole()
}

async function startConsole() {
  const pc = pyConsole.value
  if (!pc || pc.starting) return
  const gen = ++consoleGen
  pc.starting = true
  consoleInput.value = ''
  pc.running = false
  pc.exitCode = null
  pc.pid = null
  pc.output = ''
  // 重新运行同一文件：先清理上一次会话
  const prevSid = pc.sessionId
  if (prevSid) pc.sessionId = null
  await dropSession(prevSid)
  if (gen !== consoleGen) return   // 清理期间被更新的运行/关闭取代
  consoleAbort = new AbortController()   // 保证 streamConsole 有可 abort 的 signal
  try {
    const r: any = await api.canvases.fileRun(cid(), pc.path)
    if (gen !== consoleGen) return
    if (r?.session_id) {
      pc.sessionId = r.session_id
      pc.pid = r.pid || null
      pc.starting = false
      pc.running = true
      streamConsole(r.session_id, gen)
    } else {
      pc.starting = false
      pc.output = '❌ 启动失败：' + (r?.error || '未知错误')
    }
  } catch (e: any) {
    if (gen !== consoleGen) return
    pc.starting = false
    pc.output = '❌ 启动失败：' + (e?.message || e)
  }
}

async function streamConsole(sid: string, gen: number) {
  try {
    await api.canvases.filePyStream(cid(), sid, (ev) => {
      if (gen !== consoleGen) return
      const pc = pyConsole.value
      if (!pc || pc.sessionId !== sid) return
      if (ev.type === 'out') {
        pc.output += ev.text || ''
        nextTick(scrollConsole)
      } else if (ev.type === 'exit') {
        pc.running = false
        pc.exitCode = (typeof ev.exit_code === 'number') ? ev.exit_code : null
      } else if (ev.type === 'closed') {
        pc.running = false
        if (!pc.output) pc.output = ev.error || '控制台会话已结束'
      }
    }, consoleAbort?.signal)
  } catch (e: any) {
    if (gen !== consoleGen) return
    const pc = pyConsole.value
    if (!pc || pc.sessionId !== sid) return
    if (pc.running) {
      pc.running = false
      pc.output += '\n\n[与后端的输出连接中断：' + (e?.message || e) + ']'
    }
  }
}

async function sendConsoleInput() {
  const pc = pyConsole.value
  if (!pc || !pc.running || !pc.sessionId) return
  const text = consoleInput.value
  if (!text) return
  consoleInput.value = ''
  pc.output += '>>> ' + text + '\n'
  nextTick(scrollConsole)
  try {
    await api.canvases.filePyInput(cid(), pc.sessionId, text + '\n')
  } catch (e: any) {
    pc.output += '\n[输入失败：' + (e?.message || e) + ']\n'
  }
}

async function stopConsole() {
  const pc = pyConsole.value
  if (!pc || !pc.running || !pc.sessionId) return
  try {
    const r: any = await api.canvases.filePyStop(cid(), pc.sessionId)
    pc.running = false
    if (typeof r?.exit_code === 'number') pc.exitCode = r.exit_code
  } catch (e: any) {
    pc.output += '\n\n[停止失败：' + (e?.message || e) + ']'
  }
}

async function closeConsole() {
  const pc = pyConsole.value
  if (!pc) return
  consoleGen++
  consoleAbort?.abort()
  consoleAbort = null
  if (pc.sessionId) {
    try { await api.canvases.filePyClose(cid(), pc.sessionId) } catch { }
  }
  pyConsole.value = null
}

function clearConsoleOutput() {
  if (pyConsole.value) pyConsole.value.output = ''
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
  if (['xlsx', 'xls', 'csv'].includes(ext)) return '📊'
  if (['doc', 'docx'].includes(ext)) return '📘'
  if (['ppt', 'pptx'].includes(ext)) return '📙'
  if (ext === 'pdf') return '📕'
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
  box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.5);
  overflow: hidden;
}
/* 钉住嵌入 viewport（CSS zoom 布局级缩放）时禁用 backdrop-filter：
   zoom 已按真实字号光栅化文字，无旧 transform: scale 的纹理缓存发糊问题，
   禁用仅为避免钉住态与浮动态的视觉差异 */
.ws-panel.embedded, .ws-trash.embedded {
  backdrop-filter: none; -webkit-backdrop-filter: none;
  background: rgba(var(--node-bg-rgb), 0.97);
}
.ws-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; cursor: grab;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
  user-select: none;
}
.ws-head:active { cursor: grabbing; }
.ws-ico { font-size: calc(16px * var(--font-scale)); }
.ws-titles { flex: 1; min-width: 0; }
.ws-title { font-size: calc(13px * var(--font-scale)); font-weight: 600; }
.ws-path { font-size: calc(10.5px * var(--font-scale)); opacity: .55; font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-head-actions { display: flex; gap: 4px; }
.ws-btn {
  background: rgba(var(--surface-rgb), 0.05);
  border: 1px solid rgba(var(--surface-rgb), 0.12);
  color: rgb(var(--text-rgb)); border-radius: 7px; cursor: pointer;
  font-size: calc(11px * var(--font-scale)); padding: 3px 8px; line-height: 1.6; transition: all .15s;
}
.ws-btn:hover { background: rgba(var(--accent-rgb), 0.18); }
.ws-btn:disabled { opacity: .4; cursor: not-allowed; }
.ws-btn.pin.active { background: rgba(255, 180, 90, 0.22); border-color: rgba(255, 180, 90, 0.5); }
.ws-btn.trash.active { background: rgba(255, 120, 120, 0.2); border-color: rgba(255, 120, 120, 0.5); }
.ws-btn.purple { background: rgba(var(--accent2-rgb), 0.18); border-color: rgba(var(--accent2-rgb), 0.4); color: var(--purple-ink); }
.ws-btn.ok { background: rgba(100, 220, 160, 0.15); border-color: rgba(100, 220, 160, 0.4); color: var(--green-ink); }
.ws-btn.danger { background: rgba(255, 80, 80, 0.18); border-color: rgba(255, 80, 80, 0.42); color: var(--red-ink); }

.ws-body { flex: 1; overflow-y: auto; overflow-anchor: none; padding: 6px 6px 8px; min-height: 60px; }
.ws-empty { padding: 22px 12px; text-align: center; font-size: calc(12px * var(--font-scale)); opacity: .6; display: flex; flex-direction: column; align-items: center; gap: 10px; }

.ws-tree { display: flex; flex-direction: column; }
.ws-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 8px; border-radius: 7px; font-size: calc(12px * var(--font-scale));
  cursor: default; transition: background .12s;
}
.ws-row.dir { cursor: pointer; }
.ws-row.dragging { opacity: .45; }
.ws-row:hover { background: rgba(var(--surface-rgb), 0.05); }
/* 刚变动的行：短促高亮后淡出，让用户一眼看到"只更新了这些" */
.ws-row.changed { animation: ws-row-flash 1.1s ease-out; }
@keyframes ws-row-flash {
  0% { background: rgba(var(--accent-rgb), 0.3); }
  60% { background: rgba(var(--accent-rgb), 0.12); }
  100% { background: transparent; }
}
.ws-row-ico { flex-shrink: 0; width: 16px; text-align: center; }
.ws-row-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-row.dir .ws-row-name { font-weight: 500; }
.ws-trunc { flex-shrink: 0; font-size: calc(10px * var(--font-scale)); color: #ffd18a; padding: 0 2px; }
.ws-row-size { flex-shrink: 0; width: 58px; text-align: right; font-family: Consolas, monospace; font-size: calc(10.5px * var(--font-scale)); opacity: .7; }
.ws-row-time { flex-shrink: 0; width: 76px; text-align: right; font-family: Consolas, monospace; font-size: calc(10.5px * var(--font-scale)); opacity: .5; }
.ws-row-people { flex-shrink: 0; display: flex; gap: 4px; }
.chip { font-size: calc(10px * var(--font-scale)); padding: 1px 6px; border-radius: 999px; background: rgba(var(--accent-rgb), 0.18); color: var(--accent-ink); border: 1px solid rgba(var(--accent-rgb), 0.3); white-space: nowrap; }
.chip.more { background: rgba(var(--accent2-rgb), 0.18); color: var(--purple-ink); border-color: rgba(var(--accent2-rgb), 0.3); }
.chip.none { background: transparent; color: rgba(var(--text-rgb), 0.3); border-color: transparent; }

.ws-rename-input {
  flex: 1; min-width: 0; background: rgba(var(--shadow-rgb), 0.35);
  border: 1px solid rgba(var(--accent-rgb), 0.6); border-radius: 6px;
  color: rgb(var(--text-rgb)); font-size: calc(12px * var(--font-scale)); padding: 2px 6px; outline: none;
}

.ws-foot { display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.07); font-size: calc(10.5px * var(--font-scale)); opacity: .5; }
.ws-count { flex-shrink: 0; }
.ws-copy-hint { flex: 1; min-width: 0; font-family: Consolas, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #ffd18a; }
.ws-legend { flex-shrink: 0; margin-left: auto; cursor: pointer; padding: 2px 6px; border-radius: 6px; transition: all .15s; white-space: nowrap; }
.ws-legend:hover { background: rgba(var(--surface-rgb), 0.08); }
.ws-legend.on { color: var(--accent-ink); }

/* —— 右键菜单 —— */
.ws-ctx { position: fixed; min-width: 210px; padding: 6px; z-index: 9999; background: rgba(var(--node-bg-rgb), 0.98); box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.5); }
.ws-ctx-title { font-size: calc(11px * var(--font-scale)); padding: 6px 10px; color: rgba(var(--text-rgb), 0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); margin-bottom: 4px; }
.ws-ctx-item { padding: 8px 12px; border-radius: 6px; font-size: calc(13px * var(--font-scale)); cursor: pointer; transition: background .15s; }
.ws-ctx-item:hover { background: rgba(var(--accent-rgb), 0.18); }
.ws-ctx-item.danger:hover { background: rgba(255, 80, 80, 0.18); color: #ff9090; }
.ws-ctx-item.disabled { opacity: .4; cursor: default; }
.ws-ctx-item.disabled:hover { background: transparent; }
.ws-ctx-divider { height: 1px; background: rgba(var(--surface-rgb), 0.08); margin: 4px 6px; }

/* —— 文件编辑器 —— */
.ws-modal-bg { position: fixed; inset: 0; background: rgba(var(--shadow-rgb), 0.55); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 9995; }
.ws-modal-card { width: 640px; max-width: 92vw; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.ws-modal-head { display: flex; align-items: center; gap: 10px; }
.ws-modal-title { font-size: calc(14px * var(--font-scale)); font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-modal-meta { font-size: calc(11px * var(--font-scale)); opacity: .55; font-family: Consolas, monospace; white-space: nowrap; }
.ws-modal-meta.ok { opacity: 1; color: #7dd88f; }
.ws-modal-meta.warn { opacity: 1; color: #ffd18a; }
.ws-modal-dirty { font-size: calc(11px * var(--font-scale)); color: #ffd18a; }
/* —— 文件编辑器浮窗 —— */
.ws-edit-float {
  display: flex; flex-direction: column; gap: 8px;
  padding: 0; overflow: hidden; z-index: 9993;
  box-shadow: 0 14px 44px rgba(var(--shadow-rgb), 0.55);
  border-radius: 10px;
}
.ws-edit-float-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
  flex-shrink: 0;
}
.ws-edit-float-head:active { cursor: grabbing; }
.ws-edit-float .ws-modal-foot { padding: 6px 10px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); flex-shrink: 0; }
.ws-editor-text {
  flex: 1; min-height: 0; margin: 0 10px; background: rgba(var(--shadow-rgb), 0.35);
  border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 8px;
  color: rgb(var(--text-rgb)); font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, "Courier New", monospace;
  padding: 10px; outline: none; resize: none; line-height: 1.55;
}
.ws-editor-text:focus { border-color: rgba(var(--accent-rgb), 0.6); }
.ws-editor-text[readonly] { opacity: .7; }
/* —— Python 控制台浮窗 —— */
.ws-console-float {
  display: flex; flex-direction: column; gap: 8px;
  padding: 0; overflow: hidden; z-index: 9992;
  box-shadow: 0 14px 44px rgba(var(--shadow-rgb), 0.55);
  border-radius: 10px;
}
.ws-console-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
  flex-shrink: 0;
}
.ws-console-head:active { cursor: grabbing; }
.ws-console-float .ws-modal-foot { padding: 6px 10px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); flex-shrink: 0; }
.ws-console-output {
  flex: 1; min-height: 0; margin: 0 10px; overflow: auto; overflow-anchor: none;
  white-space: pre-wrap; word-break: break-word;
  background: rgba(var(--shadow-rgb), 0.45); border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 8px;
  color: #bfe6c6; font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, "Courier New", monospace;
  padding: 10px; line-height: 1.55;
}
.ws-console-input {
  display: flex; gap: 6px; margin: 0 10px; flex-shrink: 0;
}
.ws-console-input input {
  flex: 1; min-width: 0; background: rgba(var(--shadow-rgb), 0.45);
  border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 8px;
  color: rgb(var(--text-rgb)); font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, "Courier New", monospace;
  padding: 7px 10px; outline: none;
}
.ws-console-input input:focus { border-color: rgba(var(--accent-rgb), 0.6); }
.ws-console-input input:disabled { opacity: .5; }
.ws-editor-text {
  width: 100%; height: 420px; background: rgba(var(--shadow-rgb), 0.35);
  border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 8px;
  color: rgb(var(--text-rgb)); font-size: calc(12.5px * var(--font-scale)); font-family: Consolas, "Courier New", monospace;
  padding: 10px; outline: none; resize: none; line-height: 1.55;
}
.ws-editor-text:focus { border-color: rgba(var(--accent-rgb), 0.6); }
.ws-editor-text[readonly] { opacity: .7; }
.ws-modal-foot { display: flex; justify-content: space-between; align-items: center; }
.ws-modal-hint { font-size: calc(10.5px * var(--font-scale)); opacity: .5; }

/* —— 回收站浮层（position 由 trashStyle 内联控制：钉住=absolute 画布坐标，浮动=fixed 屏幕像素） —— */
.ws-trash {
  width: 380px; max-height: min(70vh, 560px);
  display: flex; flex-direction: column; z-index: 9991;
  box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.55);
  border-color: rgba(255, 120, 120, 0.28);
  overflow: hidden;
}
.wt-head {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 8px 10px; cursor: grab; user-select: none;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
  background: rgba(255, 90, 90, 0.06);
}
.wt-head:active { cursor: grabbing; }
.wt-ico { font-size: calc(14px * var(--font-scale)); }
.wt-title { font-size: calc(12.5px * var(--font-scale)); font-weight: 600; flex: 1; }
.wt-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.wt-body { flex: 1; overflow-y: auto; overflow-anchor: none; padding: 10px; }
.wt-empty { padding: 24px 12px; text-align: center; font-size: calc(12px * var(--font-scale)); opacity: .6; }
.wt-grid { display: flex; flex-direction: column; gap: 8px; }
.wt-card {
  border: 1px solid rgba(255, 120, 120, 0.32);
  background: rgba(var(--node-bg-rgb), 0.55);
  border-radius: 10px; padding: 8px 10px; cursor: pointer;
  transition: all .15s; position: relative;
}
.wt-card:hover { border-color: rgba(255, 150, 150, 0.6); box-shadow: 0 0 14px rgba(255, 90, 90, 0.18); }
.wt-card.selected { border-color: #ff8a8a; box-shadow: 0 0 0 2px rgba(255, 120, 120, 0.35), 0 0 16px rgba(255, 90, 90, 0.28); background: rgba(var(--node-bg-rgb), 0.6); }
.wt-card.done { opacity: .5; }
.wt-card.disabled { opacity: .75; }
.wt-card-top { display: flex; align-items: center; gap: 6px; margin-bottom: 5px; }
.wt-card-ico { flex-shrink: 0; }
.wt-card-name { flex: 1; min-width: 0; font-size: calc(12px * var(--font-scale)); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wt-check { flex-shrink: 0; font-size: calc(13px * var(--font-scale)); opacity: .8; }
.wt-card-meta { font-size: calc(10.5px * var(--font-scale)); opacity: .72; display: flex; gap: 6px; line-height: 1.6; }
.wt-card-meta .k { flex-shrink: 0; width: 34px; opacity: .6; }
.wt-card-tag { margin-top: 5px; font-size: calc(10px * var(--font-scale)); color: var(--red-ink); }
.wt-card-tag.ok { color: var(--green-ink); }
.wt-card-tag.warn { color: #ffd18a; }
</style>
