<script setup lang="ts">
/**
 * 色谱浮窗 —— 画布调色板按钮点击后弹出。
 *
 * 组成：
 *   1) 色相光谱条（拖动选色相）
 *   2) 饱和度/明度方块（在当前色相下拖动）
 *   3) 预设色板（与内置调色板同源）
 *   4) 十六进制实时预览
 *
 * 交互约定（无「确定」步骤）：
 *   - 在光谱/方块上拖动、点预设色 → 颜色**实时**生效：
 *     画布上悬停的节点会立刻预览该色，点节点即直接上色。
 *   - 「自动配色」→ 选中"恢复默认配色"，同样点节点即生效。
 *   - 标题栏可拖动，改变浮窗位置（位置会被记住，下次打开沿用）。
 *   - 关闭方式：✕ 按钮 / Esc / 再点工具栏的 🎨 按钮。
 *     点画布空白**不**关闭浮窗 —— 它可以被拖到不挡节点的地方，
 *     供用户连续给多个节点上色。
 *   - 画布侧语义：浮窗即「配色模式」的开关，关闭浮窗 = 退出配色，
 *     父级会一并清掉待应用色，此后点节点恢复正常行为。
 */
import { ref, computed, watch, onMounted } from 'vue'
import { AVATAR_COLOR_PRESETS, hexToHsv, hsvToHex, normalizeHexColor } from '@/utils/avatar'

const props = defineProps<{
  /** 浮窗位置（视口坐标），可由父级持久化 */
  pos: { x: number; y: number }
  initial?: string | null
}>()

const emit = defineEmits<{
  /** 颜色实时变化（无需确认，直接可用于给节点上色） */
  (e: 'change', hex: string): void
  /** 关闭浮窗 */
  (e: 'close'): void
  /** 浮窗被拖动 → 同步位置给父级 */
  (e: 'update:pos', pos: { x: number; y: number }): void
}>()

// —— HSV 取色状态 ——
const h = ref(212)
const s = ref(0.62)
const v = ref(0.78)

const hueEl = ref<HTMLElement | null>(null)
const svEl = ref<HTMLElement | null>(null)
const popEl = ref<HTMLElement | null>(null)

const hex = computed(() => hsvToHex(h.value, s.value, v.value))
const hexLabel = computed(() => hex.value.toUpperCase())
const hueCss = computed(() => `hsl(${h.value}, 100%, 50%)`)

// —— 位置：本地即时更新（拖动跟手），同步给父级持久化 ——
const localPos = ref({ ...props.pos })
let dragging = false
watch(() => props.pos, (v) => {
  if (!dragging) localPos.value = { ...v }
})

/** 贴边保护：浮窗始终完整留在视口内 */
function clampPos(x: number, y: number) {
  const w = popEl.value?.offsetWidth ?? 244
  const hgt = popEl.value?.offsetHeight ?? 400
  const m = 8
  return {
    x: Math.max(m, Math.min(x, window.innerWidth - w - m)),
    y: Math.max(m, Math.min(y, window.innerHeight - hgt - m)),
  }
}
function setPos(next: { x: number; y: number }) {
  localPos.value = next
  emit('update:pos', next)
}

function applyHex(c: string | null | undefined) {
  const n = normalizeHexColor(c)
  if (!n) return
  const { h: hh, s: ss, v: vv } = hexToHsv(n)
  h.value = hh
  s.value = ss
  v.value = vv
}

onMounted(() => {
  applyHex(props.initial)
  // 初始位置贴边保护（父级按按钮位置算出，可能超出视口）
  const fixed = clampPos(localPos.value.x, localPos.value.y)
  if (fixed.x !== localPos.value.x || fixed.y !== localPos.value.y) setPos(fixed)
})

// —— 浮窗拖动（标题栏） ——
let dragOffset = { x: 0, y: 0 }
function onTitleDown(e: PointerEvent) {
  if (e.button !== 0) return
  const el = e.currentTarget as HTMLElement
  el.setPointerCapture(e.pointerId)
  dragging = true
  dragOffset = { x: e.clientX - localPos.value.x, y: e.clientY - localPos.value.y }
  e.preventDefault()   // 防止选中文本 / 拖拽原生行为
}
function onTitleMove(e: PointerEvent) {
  if (!dragging) return
  const el = e.currentTarget as HTMLElement
  if (!el.hasPointerCapture(e.pointerId)) return
  setPos(clampPos(e.clientX - dragOffset.x, e.clientY - dragOffset.y))
}
function onTitleUp(e: PointerEvent) {
  if (!dragging) return
  dragging = false
  const el = e.currentTarget as HTMLElement
  try { el.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
}

// —— 色相条拖动 ——
function onHueDown(e: PointerEvent) {
  const el = e.currentTarget as HTMLElement
  el.setPointerCapture(e.pointerId)
  updateHue(e)
}
function onHueMove(e: PointerEvent) {
  const el = e.currentTarget as HTMLElement
  if (!el.hasPointerCapture(e.pointerId)) return
  updateHue(e)
}
function updateHue(e: PointerEvent) {
  const el = hueEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const x = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1)
  h.value = x * 360
  emit('change', hex.value)   // 实时生效：无需点确定
}

// —— 饱和度/明度方块拖动 ——
function onSvDown(e: PointerEvent) {
  const el = e.currentTarget as HTMLElement
  el.setPointerCapture(e.pointerId)
  updateSv(e)
}
function onSvMove(e: PointerEvent) {
  const el = e.currentTarget as HTMLElement
  if (!el.hasPointerCapture(e.pointerId)) return
  updateSv(e)
}
function updateSv(e: PointerEvent) {
  const el = svEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const px = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1)
  const py = Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1)
  s.value = px
  v.value = 1 - py
  emit('change', hex.value)   // 实时生效
}

// —— 预设色板 ——
function pickPreset(c: string) {
  applyHex(c)
  emit('change', c)   // 实时生效
}

// —— 自动配色（清除自定义色） ——
function pickAuto() {
  emit('change', '')
}
</script>

<template>
  <div ref="popEl" class="csp-pop popup-surface"
       :style="{ left: localPos.x + 'px', top: localPos.y + 'px' }"
       @click.stop @mousedown.stop @contextmenu.prevent.stop>
    <div class="csp-title csp-drag"
         title="按住拖动可移动浮窗"
         @pointerdown="onTitleDown" @pointermove="onTitleMove" @pointerup="onTitleUp" @pointercancel="onTitleUp">
      <span>🎨 头像配色</span>
      <button class="csp-x" @click.stop="emit('close')" @pointerdown.stop title="关闭">✕</button>
    </div>

    <!-- 预览 + 十六进制 -->
    <div class="csp-preview-row">
      <span class="csp-preview" :style="{ background: hex }"></span>
      <span class="csp-hex">{{ hexLabel }}</span>
      <span class="csp-tip">点节点即上色</span>
    </div>

    <!-- 色相光谱 -->
    <div class="csp-hue" ref="hueEl"
         @pointerdown="onHueDown" @pointermove="onHueMove">
      <span class="csp-hue-dot" :style="{ left: (h / 360 * 100) + '%' }"></span>
    </div>

    <!-- 饱和度 × 明度 -->
    <div class="csp-sv" ref="svEl"
         :style="{ background: `linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, transparent), ${hueCss}` }"
         @pointerdown="onSvDown" @pointermove="onSvMove">
      <span class="csp-sv-dot" :style="{ left: (s * 100) + '%', top: ((1 - v) * 100) + '%' }"></span>
    </div>

    <!-- 预设色板 -->
    <div class="csp-presets">
      <button v-for="c in AVATAR_COLOR_PRESETS" :key="c" class="csp-chip"
              :style="{ background: c }" :title="c" @click="pickPreset(c)"></button>
    </div>

    <div class="csp-actions">
      <button class="btn xs ghost csp-auto" @click="pickAuto" title="清除自定义颜色，回到默认配色">
        ↺ 自动配色
      </button>
      <button class="btn xs ghost" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.csp-pop {
  position: fixed;
  z-index: 10000;
  width: 244px;
  padding: 12px;
  border-radius: 12px;
  user-select: none;
}

.csp-title {
  display: flex; align-items: center; justify-content: space-between;
  font-size: calc(12.5px * var(--font-scale)); font-weight: 600;
  color: var(--text); margin-bottom: 10px;
}
/* 标题栏 = 拖动把手 */
.csp-drag { cursor: grab; margin: -4px -4px 6px; padding: 4px; border-radius: 8px; }
.csp-drag:active { cursor: grabbing; }
.csp-drag:hover { background: rgba(var(--surface-rgb), 0.06); }
.csp-x {
  background: none; border: none; cursor: pointer; padding: 2px 4px;
  color: rgba(var(--text-rgb), 0.6); font-size: calc(12px * var(--font-scale)); border-radius: 6px;
}
.csp-x:hover { background: rgba(var(--surface-rgb), 0.08); color: var(--text); }

/* 预览 */
.csp-preview-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.csp-preview {
  width: 26px; height: 26px; border-radius: 8px; flex-shrink: 0;
  box-shadow: inset 0 1px 1px rgba(255,255,255,0.4), 0 2px 6px rgba(var(--shadow-rgb), 0.35);
}
.csp-hex {
  font-family: Consolas, monospace; font-size: calc(12px * var(--font-scale));
  color: rgba(var(--text-rgb), 0.85); letter-spacing: 0.5px;
}
.csp-tip { margin-left: auto; font-size: calc(10.5px * var(--font-scale)); color: rgba(var(--text-rgb), 0.5); }

/* 色相条 */
.csp-hue {
  position: relative; height: 14px; border-radius: 7px; cursor: pointer;
  background: linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00);
  box-shadow: inset 0 0 0 1px rgba(var(--surface-rgb), 0.12);
}
.csp-hue-dot {
  position: absolute; top: 50%; width: 12px; height: 12px; margin-left: -6px;
  transform: translateY(-50%); border-radius: 50%;
  background: #fff; box-shadow: 0 0 0 2px rgba(0,0,0,0.45), 0 1px 3px rgba(0,0,0,0.5);
  pointer-events: none;
}

/* 饱和度 × 明度方块 */
.csp-sv {
  position: relative; height: 104px; margin-top: 10px; border-radius: 8px; cursor: crosshair;
  box-shadow: inset 0 0 0 1px rgba(var(--surface-rgb), 0.12);
}
.csp-sv-dot {
  position: absolute; width: 12px; height: 12px; margin-left: -6px; margin-top: -6px;
  border-radius: 50%; background: #fff; pointer-events: none;
  box-shadow: 0 0 0 2px rgba(0,0,0,0.45), 0 1px 3px rgba(0,0,0,0.5);
}

/* 预设色板 */
.csp-presets {
  display: grid; grid-template-columns: repeat(8, 1fr); gap: 5px; margin-top: 10px;
}
.csp-chip {
  width: 100%; aspect-ratio: 1; border-radius: 5px; cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.16); padding: 0;
  transition: transform .12s, box-shadow .12s;
}
.csp-chip:hover { transform: scale(1.18); box-shadow: 0 2px 8px rgba(var(--shadow-rgb), 0.5); }

/* 底部操作 */
.csp-actions {
  display: flex; align-items: center; justify-content: flex-end; gap: 6px; margin-top: 12px;
}
.csp-auto { margin-right: auto; }
</style>
