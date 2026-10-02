<script setup lang="ts">
import { ref, watch } from 'vue'
import { useCanvasStore } from '@/stores'
import CanvasBoard from './CanvasBoard.vue'
import WorkspaceOverlay from './WorkspaceOverlay.vue'

const canvases = useCanvasStore()

// ========================================================
// 全局唯一的工作区浮窗状态（跨画布统一）
// ========================================================
type Bbox = { x: number; y: number; w: number; h: number; hasNodes: boolean }

// 钉住位置模型：按"最近相邻边对"保持面板(a)与节点总包围盒(b)的相对位移一致。
// xMode / yMode 表示 a 与 b 配对的边缘：
//   xMode: 0=a左↔b左  1=a左↔b右  2=a右↔b左  3=a右↔b右
//   yMode: 0=a上↔b上  1=a上↔b下  2=a下↔b上  3=a下↔b下
// xOff/yOff 为该边对的位移（画布坐标，正方向：面板边 − b 边）。
type EdgeState = { xMode: number; xOff: number; yMode: number; yOff: number }

const wsGlobal = ref<{
  pinned: boolean
  edge: EdgeState
  floatX: number
  floatY: number
}>({
  pinned: false,
  edge: { xMode: 1, xOff: 12, yMode: 0, yOff: 12 },  // 默认：紧贴 bbox 右上角
  floatX: 80,
  floatY: 80,
})

const WS_KEY = 'agent-cluster.workspace-global'
let lsNeedsRewrite = false
try {
  const raw = localStorage.getItem(WS_KEY)
  if (raw) {
    const saved = JSON.parse(raw)
    wsGlobal.value = { ...wsGlobal.value, ...saved }
    // 旧版数据兼容：若存在 dx/dy/anchor 但无 edge，则忽略旧字段使用默认 edge
    if (!wsGlobal.value.edge) wsGlobal.value.edge = { xMode: 1, xOff: 12, yMode: 0, yOff: 12 }
    // 坏值修复：边对位移绝对值过大说明面板被钉到画布外了
    if (Math.abs(wsGlobal.value.edge.xOff) > 5000 || Math.abs(wsGlobal.value.edge.yOff) > 5000) {
      wsGlobal.value.edge = { xMode: 1, xOff: 12, yMode: 0, yOff: 12 }
      wsGlobal.value.pinned = false
      lsNeedsRewrite = true
    }
  }
} catch {}
if (lsNeedsRewrite) {
  try { localStorage.setItem(WS_KEY, JSON.stringify(wsGlobal.value)) } catch {}
}
watch(wsGlobal, (v) => {
  try { localStorage.setItem(WS_KEY, JSON.stringify(v)) } catch {}
}, { deep: true })

// —— 当前画布传递过来的数据 ——
const currentBbox = ref<Bbox | null>(null)
const currentWorkdir = ref<string | null>(null)
const currentCanvasId = ref<number | null>(null)
const currentScale = ref(1)

// CanvasBoard 组件引用（用于拿 viewportRef / computeNodeBbox）
const canvasBoardRef = ref<InstanceType<typeof CanvasBoard> | null>(null)
const viewportEl = ref<HTMLElement | null>(null)

// —— 浮窗可见（钉住或解锁打开都显示） ——
const wsVisible = ref(false)

// —— CanvasBoard 事件处理 ——
function onCanvasBbox(bbox: Bbox) { currentBbox.value = bbox }
function onCanvasActivate(payload: {
  id: number; workdir: string | null; nodeBbox: Bbox | null; scale: number
}) {
  currentCanvasId.value = payload.id
  currentWorkdir.value = payload.workdir
  currentScale.value = payload.scale
  if (payload.nodeBbox) currentBbox.value = payload.nodeBbox
  // 刷新 viewport DOM 引用（钉住模式 Teleport 目标）
  viewportEl.value = (canvasBoardRef.value as any)?.viewportRef ?? null
}
function onOpenWorkspace(pos: { clientX: number; clientY: number }) {
  if (wsVisible.value) {
    // 已经显示：切换钉住/解锁状态
    wsVisible.value = false
  } else {
    // 首次打开：以鼠标位置为中心
    const halfW = 180
    wsGlobal.value.floatX = Math.max(8, Math.min(pos.clientX - halfW, window.innerWidth - 360 - 8))
    wsGlobal.value.floatY = Math.max(8, Math.min(pos.clientY - 24, window.innerHeight - 420))
    wsVisible.value = true
  }
}
function onCloseWorkspace() { wsVisible.value = false }
function onSetupWorkdir() {
  // WorkspaceOverlay 的"去设置"按钮 → 递增信号 → CanvasBoard 监听后弹出对话框
  setupWdSignal.value++
}

// —— WorkspaceOverlay emit 处理 ——
function onSetPinned(v: boolean) {
  if (v && !wsGlobal.value.pinned) {
    // 从浮动切钉住：给一个默认边对（紧贴 bbox 右上角），拖动后由 Overlay 重算并下发
    wsGlobal.value.edge = { xMode: 1, xOff: 12, yMode: 0, yOff: 12 }
  }
  wsGlobal.value.pinned = v
}
function onSetEdge(edge: EdgeState) {
  // 钉住拖动：Overlay 计算好"最近相邻边对 + 位移"后直接覆盖（非累加）
  wsGlobal.value.edge = edge
}
function onUpdateFloatPos(x: number, y: number) { wsGlobal.value.floatX = x; wsGlobal.value.floatY = y }

// 信号：CanvasBoard 监听 setupWdSignal 变化弹出 wd 对话框
const setupWdSignal = ref(0)
</script>

<template>
  <div class="main-view">
    <!-- CanvasBoard：画布本体 -->
    <CanvasBoard v-if="canvases.current" ref="canvasBoardRef"
                 :setup-wd-signal="setupWdSignal"
                 @canvas-bbox="onCanvasBbox"
                 @canvas-activate="onCanvasActivate"
                 @open-workspace="onOpenWorkspace"
                 @close-workspace="onCloseWorkspace" />
    <div v-else class="empty-center glass">
      <div style="font-size:24px; margin-bottom:8px">🎨</div>
      <div style="font-size:15px; margin-bottom:8px">还没有画布</div>
      <div style="font-size:12px; opacity:.6">在右侧「画布列表」点 "+" 新建，或从左侧拖拽智能体到此处</div>
    </div>

    <!-- 🗂 全局唯一的工作区浮窗（跨画布单例，解锁时 body 下 fixed，钉住时 viewport 内 absolute） -->
    <WorkspaceOverlay v-if="wsVisible"
                      :visible="wsVisible"
                      :canvas-id="currentCanvasId"
                      :workdir="currentWorkdir"
                      :pinned="wsGlobal.pinned"
                      :edge="wsGlobal.edge"
                      :scale="currentScale"
                      :bbox="currentBbox"
                      :viewport-el="viewportEl"
                      :float-x="wsGlobal.floatX"
                      :float-y="wsGlobal.floatY"
                      @close="onCloseWorkspace"
                      @setup-workdir="onSetupWorkdir"
                      @set-pinned="onSetPinned"
                      @set-edge="onSetEdge"
                      @update-float-pos="onUpdateFloatPos" />
  </div>
</template>

<style scoped>
.main-view { display: flex; flex-direction: column; min-height: 0; }
.empty-center { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; }
</style>
