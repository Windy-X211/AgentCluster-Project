<script setup lang="ts">
/**
 * 沉浸态「运行输出」浮窗
 *
 * 目标：用户在看背景（可能正在玩 HTML 小游戏）时，不切回工作台也能看到
 * 正在工作的画布/智能体输出。
 *   · 顶部 tab：跨会话聚合的所有运行单元（头像 + 名称 + 已运行时长）
 *   · 正文：复用 ChatBody（与对话区同一套 Markdown / viz 渲染）
 *   · 底部：停止该智能体 / 回到工作台
 *   · 头部可拖动，位置仅内存，关掉即忘
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useConvStore, useLayoutStore } from '@/stores'
import { useRunningUnits, formatElapsed } from '@/composables/useRunningUnits'
import ChatBody from '@/extensions/ChatBody.vue'

const layout = useLayoutStore()
const convs = useConvStore()
const { units, canvasWaiting } = useRunningUnits()

const extAPI = (window as any).__extAPI__

// —— 初始位置：贴右侧、顶栏下方；高度按窗口高度取 62%，限 300~640 ——
const pos = ref({
  x: Math.max(12, window.innerWidth - 476),
  y: 62,
})
const height = ref(Math.min(640, Math.max(300, Math.round(window.innerHeight * 0.62))))
const panelRef = ref<HTMLElement | null>(null)

const activeKey = ref<string>('')
const active = computed(() => units.value.find(u => u.key === activeKey.value) || units.value[0] || null)

watch(units, (list) => {
  if (!list.length) { activeKey.value = ''; return }
  if (!list.some(u => u.key === activeKey.value)) activeKey.value = list[0].key
}, { immediate: true, deep: false })

// —— 输出区自动跟随最新（用户手动上滑后暂停跟随） ——
const bodyEl = ref<HTMLElement | null>(null)
const stick = ref(true)

function onScroll() {
  const el = bodyEl.value
  if (!el) return
  stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}
watch(() => active.value?.content, async () => {
  if (!stick.value) return
  await nextTick()
  const el = bodyEl.value
  if (el) el.scrollTop = el.scrollHeight
})
watch(activeKey, async () => {
  stick.value = true
  await nextTick()
  const el = bodyEl.value
  if (el) el.scrollTop = el.scrollHeight
})

// —— 头部拖动 ——
let dragging = false
let dragDx = 0
let dragDy = 0
function onDragStart(e: PointerEvent) {
  if ((e.target as HTMLElement)?.closest('button')) return
  dragging = true
  dragDx = e.clientX - pos.value.x
  dragDy = e.clientY - pos.value.y
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', onDragEnd, { once: true })
  e.preventDefault()
}
function clampPos() {
  const w = panelRef.value?.offsetWidth || 460
  const h = panelRef.value?.offsetHeight || height.value
  pos.value.x = Math.min(Math.max(6, pos.value.x), window.innerWidth - w - 6)
  pos.value.y = Math.min(Math.max(40, pos.value.y), window.innerHeight - h - 6)
}
function onDragMove(e: PointerEvent) {
  if (!dragging) return
  pos.value.x = e.clientX - dragDx
  pos.value.y = e.clientY - dragDy
  clampPos()
}
function onDragEnd() {
  dragging = false
  window.removeEventListener('pointermove', onDragMove)
}
onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onDragMove)
})

// —— 操作 ——
async function stopUnit() {
  const u = active.value
  if (!u) return
  await convs.stopAgentRun(u.convId, u.agentId)
}
function revealInWorkbench() {
  const u = active.value
  if (u) convs.selectConv(u.convId)
  layout.setImmersive(false)
}
function backToWorkbench() {
  layout.setImmersive(false)
}
</script>

<template>
  <div ref="panelRef" class="peek" :style="{ left: pos.x + 'px', top: pos.y + 'px', height: height + 'px' }">
    <header class="pk-head" @pointerdown="onDragStart">
      <span class="pk-title">
        <AppIcon name="message-square" size="14" /> 运行输出
        <span v-if="units.length" class="pk-count">{{ units.length }}</span>
      </span>
      <span v-if="canvasWaiting" class="pk-canvas">画布「{{ canvasWaiting.name }}」待触发</span>
      <span class="pk-grow" />
      <button class="pk-icon-btn" title="在会话中查看" @click="revealInWorkbench">
        <AppIcon name="arrow-right" size="14" />
      </button>
      <button class="pk-icon-btn" title="关闭浮窗（Esc）" @click="layout.immersivePeek = false">
        <AppIcon name="x" size="14" />
      </button>
    </header>

    <div v-if="!units.length" class="pk-empty">
      <div class="pk-empty-ico"><AppIcon name="circle-check-big" size="26" /></div>
      <div>当前没有运行中的智能体</div>
      <div v-if="canvasWaiting" class="pk-empty-sub">画布「{{ canvasWaiting.name }}」正在等待触发</div>
    </div>

    <template v-else>
      <div class="pk-tabs">
        <button
          v-for="u in units" :key="u.key"
          class="pk-tab" :class="{ active: u.key === (active?.key || '') }"
          @click="activeKey = u.key">
          <span class="pk-ava" :style="{ background: u.avatar }">{{ (u.agentName || 'A').slice(0, 1) }}</span>
          <span class="pk-tab-name">{{ u.agentName }}</span>
          <span class="pk-tab-time">{{ formatElapsed(u.elapsed) }}</span>
        </button>
      </div>

      <div class="pk-body" ref="bodyEl" @scroll.passive="onScroll">
        <div v-if="active" class="pk-meta">
          <span class="pk-conv">{{ active.convName }}</span>
          <span class="pk-act" :class="{ tool: active.inTool }">
            <span class="pk-live-dot" />
            {{ active.activity }}
          </span>
        </div>

        <div v-if="active?.content" class="pk-content markdown">
          <ChatBody :content="active.content" :streaming="active.streaming" :extAPI="extAPI" />
        </div>
        <div v-else class="pk-wait">
          <span class="pk-spinner" />
          <span>等待输出…</span>
        </div>
      </div>

      <footer class="pk-foot">
        <button class="pk-btn danger" :disabled="!active" @click="stopUnit">
          <AppIcon name="square" size="12" /> 停止该智能体
        </button>
        <button class="pk-btn primary" @click="backToWorkbench">
          回到工作台
        </button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.peek {
  position: fixed;
  z-index: 8;
  width: 460px;
  max-width: calc(100vw - 24px);
  display: flex;
  flex-direction: column;
  pointer-events: auto;
  border-radius: 16px;
  overflow: hidden;
  background: rgba(var(--node-bg-rgb), 0.86);
  backdrop-filter: blur(26px) saturate(170%);
  -webkit-backdrop-filter: blur(26px) saturate(170%);
  border: 1px solid rgba(var(--surface-rgb), 0.16);
  box-shadow: 0 22px 60px rgba(var(--shadow-rgb), 0.55), inset 0 1px 0 rgba(var(--surface-rgb), 0.1);
  color: rgb(var(--text-rgb));
}

.pk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  cursor: grab;
  user-select: none;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.09);
  flex-shrink: 0;
}
.pk-head:active { cursor: grabbing; }
.pk-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: calc(12.5px * var(--font-scale));
  font-weight: 600;
  letter-spacing: .2px;
}
.pk-count {
  min-width: 18px; height: 18px; padding: 0 5px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 9px;
  font-size: calc(11px * var(--font-scale)); font-weight: 600;
  background: rgba(var(--accent-rgb), 0.22);
  color: var(--accent-light);
}
.pk-canvas {
  font-size: calc(11px * var(--font-scale));
  color: var(--text-dim);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 160px;
}
.pk-grow { flex: 1; }
.pk-icon-btn {
  width: 24px; height: 24px;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; border-radius: 7px; cursor: pointer;
  background: transparent; color: rgba(var(--text-rgb), 0.7);
}
.pk-icon-btn:hover { background: rgba(var(--surface-rgb), 0.1); color: rgb(var(--text-rgb)); }

.pk-tabs {
  display: flex; gap: 6px;
  padding: 8px 10px;
  overflow-x: auto;
  flex-shrink: 0;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.06);
}
.pk-tabs::-webkit-scrollbar { height: 4px; }
.pk-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 9px 4px 4px;
  border-radius: 999px;
  border: 1px solid rgba(var(--surface-rgb), 0.1);
  background: rgba(var(--surface-rgb), 0.04);
  color: var(--text-dim);
  font-size: calc(11.5px * var(--font-scale));
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
  transition: background .15s, border-color .15s, color .15s;
}
.pk-tab:hover { background: rgba(var(--surface-rgb), 0.08); }
.pk-tab.active {
  background: rgba(var(--accent-rgb), 0.14);
  border-color: rgba(var(--accent-rgb), 0.4);
  color: rgb(var(--text-rgb));
}
.pk-ava {
  width: 18px; height: 18px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: calc(9px * var(--font-scale)); color: #fff; font-weight: 600;
}
.pk-tab-name { max-width: 92px; overflow: hidden; text-overflow: ellipsis; }
.pk-tab-time { font-size: calc(10px * var(--font-scale)); opacity: .65; }

.pk-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 12px 14px;
}
.pk-meta {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 8px;
  font-size: calc(11px * var(--font-scale));
}
.pk-conv {
  color: var(--text-dim);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45%;
}
.pk-act {
  display: inline-flex; align-items: center; gap: 5px;
  color: rgba(var(--text-rgb), 0.8);
}
.pk-act.tool { color: var(--accent-light); }
.pk-live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 rgba(var(--accent-rgb), 0.5);
  animation: pk-pulse 1.6s ease-out infinite;
}
@keyframes pk-pulse {
  0% { box-shadow: 0 0 0 0 rgba(var(--accent-rgb), 0.45); }
  70% { box-shadow: 0 0 0 6px rgba(var(--accent-rgb), 0); }
  100% { box-shadow: 0 0 0 0 rgba(var(--accent-rgb), 0); }
}

/* 正文排版：ChatBody 输出 .cb-md，这里补最小 Markdown 视觉（不依赖对话区 scoped 样式） */
.pk-content {
  font-size: calc(12.5px * var(--font-scale));
  line-height: 1.68;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.pk-content :deep(p) { margin: 0 0 8px; }
.pk-content :deep(p:last-child) { margin-bottom: 0; }
.pk-content :deep(h1),
.pk-content :deep(h2),
.pk-content :deep(h3),
.pk-content :deep(h4) { font-size: calc(13.5px * var(--font-scale)); margin: 10px 0 6px; }
.pk-content :deep(ul),
.pk-content :deep(ol) { padding-left: 18px; margin: 0 0 8px; }
.pk-content :deep(code) {
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 0.92em;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--code-tint-strong);
  color: var(--code-ink);
}
.pk-content :deep(pre) {
  padding: 9px 11px;
  border-radius: 8px;
  background: var(--code-tint-pre);
  border: 1px solid rgba(var(--surface-rgb), 0.08);
  overflow-x: auto;
  margin: 0 0 8px;
}
.pk-content :deep(pre code) { background: transparent; color: inherit; padding: 0; }
.pk-content :deep(blockquote) {
  margin: 0 0 8px; padding: 6px 10px;
  border-left: 2px solid rgba(var(--accent-rgb), 0.45);
  background: rgba(var(--surface-rgb), 0.05);
  color: var(--text-dim);
}
.pk-content :deep(table) {
  width: 100%; border-collapse: collapse; margin: 0 0 8px;
  font-size: 0.95em;
}
.pk-content :deep(td),
.pk-content :deep(th) {
  border: 1px solid rgba(var(--surface-rgb), 0.12);
  padding: 4px 7px;
}
.pk-content :deep(img) { max-width: 100%; border-radius: 6px; }
.pk-content :deep(a) { color: var(--accent-light); }
.pk-content :deep(.stream-cursor),
.pk-content :deep(span.stream-cursor) { opacity: .45; animation: pk-blink 1s steps(2) infinite; }
@keyframes pk-blink { 50% { opacity: .05; } }

.pk-wait {
  display: flex; align-items: center; gap: 8px;
  padding: 18px 2px;
  color: var(--text-dim);
  font-size: calc(12px * var(--font-scale));
}
.pk-spinner {
  width: 13px; height: 13px; border-radius: 50%;
  border: 2px solid rgba(var(--accent-rgb), 0.25);
  border-top-color: var(--accent);
  animation: pk-spin .8s linear infinite;
}
@keyframes pk-spin { to { transform: rotate(360deg); } }

.pk-empty {
  flex: 1; min-height: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px;
  color: var(--text-dim);
  font-size: calc(12px * var(--font-scale));
}
.pk-empty-ico { color: var(--success-light); opacity: .8; }
.pk-empty-sub { font-size: calc(11px * var(--font-scale)); opacity: .7; }

.pk-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
  padding: 9px 10px;
  border-top: 1px solid rgba(var(--surface-rgb), 0.08);
  flex-shrink: 0;
}
.pk-btn {
  height: 28px; padding: 0 12px;
  display: inline-flex; align-items: center; gap: 5px;
  border-radius: 8px;
  border: 1px solid rgba(var(--surface-rgb), 0.12);
  background: rgba(var(--surface-rgb), 0.05);
  color: rgba(var(--text-rgb), 0.85);
  font-size: calc(11.5px * var(--font-scale));
  cursor: pointer;
  transition: background .15s, border-color .15s;
}
.pk-btn:hover:not(:disabled) { background: rgba(var(--surface-rgb), 0.1); }
.pk-btn:disabled { opacity: .4; cursor: default; }
.pk-btn.danger:hover:not(:disabled) {
  border-color: rgba(232, 17, 35, 0.45);
  background: rgba(232, 17, 35, 0.16);
  color: #ffb3b3;
}
.pk-btn.primary {
  background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.3), rgba(var(--accent2-rgb), 0.26));
  border-color: rgba(var(--accent-rgb), 0.4);
  color: rgb(var(--text-rgb));
}
</style>
