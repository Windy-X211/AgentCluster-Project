<script setup lang="ts">
/**
 * 沉浸模式浮层（独立于 .app-root 网格，position: fixed 悬浮在背景之上）
 *
 * 组成：
 *   · 角落球   —— 常驻、唯一稳定可点的宿主入口（iframe 抢走键盘后 Esc 收不到时的兜底）
 *   · 细条顶栏 —— 进入时自动展开 3.2s，之后收起；含状态、工作输出、返回工作台、窗口三键
 *   · 状态徽标 —— 细条收起时，只要画布/智能体在工作就常驻顶部居中，点击直接看输出
 *   · 输出浮窗 —— RunPeekPanel（跨会话聚合的实时输出）
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useLayoutStore } from '@/stores'
import { isDesktop, windowAPI } from '@/utils/env'
import { useSnap } from '@/composables/useSnap'
import { useRunningUnits } from '@/composables/useRunningUnits'
import RunPeekPanel from './RunPeekPanel.vue'

const layout = useLayoutStore()
const { units, hasWork, summary } = useRunningUnits()

const isMax = ref(false)
const brandEl = ref<HTMLElement | null>(null)
const dragFillEl = ref<HTMLElement | null>(null)
const hint = ref('')

const barOpen = computed(() => layout.immersiveBarOpen)
const peekOpen = computed(() => layout.immersivePeek)

/** 细条收起 + 有工作在跑 → 顶部居中的状态徽标 */
const showChip = computed(() => hasWork.value && !barOpen.value)

let hintTimer: ReturnType<typeof setTimeout> | null = null
let barTimer: ReturnType<typeof setTimeout> | null = null

function clearTimers() {
  if (hintTimer) { clearTimeout(hintTimer); hintTimer = null }
  if (barTimer) { clearTimeout(barTimer); barTimer = null }
}

onMounted(() => {
  // 供全局 CSS 收起 body 级 Teleport 浮层（对话记录浮层 / 新手引导），它们不在网格内
  document.documentElement.classList.add('immersive-mode')
  const { attachDragRegion } = useSnap()
  attachDragRegion(brandEl.value)
  // 控制条中段（状态区）也可拖动窗口：单独的填满区既保证整条可拖，
  // 又不会像把类挂在整条上那样把按钮的点击吞掉（pywebview 按祖先链识别拖动区）
  attachDragRegion(dragFillEl.value)

  windowAPI.isMaximized?.().then((v: boolean | null) => { if (typeof v === 'boolean') isMax.value = v }).catch(() => {})
  window.electronAPI?.on?.('win:maximized', (v: boolean) => { isMax.value = v })

  window.addEventListener('keydown', onKeydown)
  // 提示 + 细条自动收起
  hint.value = layout.backgroundId
    ? '已进入沉浸模式 · 若背景是可交互 HTML（小游戏），点击画面即可操作；点右上角图标唤出控制条'
    : '已进入沉浸模式 · 当前还没设置背景，去 设置 → 外观设置 → 背景 选一个或导入 HTML 小游戏'
  hintTimer = setTimeout(() => { hint.value = '' }, 5200)
  barTimer = setTimeout(() => {
    if (!layout.immersivePeek) layout.immersiveBarOpen = false
  }, 3200)
})

onBeforeUnmount(() => {
  clearTimers()
  document.documentElement.classList.remove('immersive-mode')
  window.removeEventListener('keydown', onKeydown)
})

// 打开输出浮窗后保持细条展开，关掉浮窗不动（用户可能还在看状态）
watch(() => layout.immersivePeek, (v) => { if (v) layout.immersiveBarOpen = true })

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  // 层级回退：输出浮窗 → 细条 → 退出沉浸
  if (layout.immersivePeek) { layout.immersivePeek = false; return }
  if (layout.immersiveBarOpen) { layout.immersiveBarOpen = false; return }
  layout.setImmersive(false)
}

function exitImmersive() { layout.setImmersive(false) }
function toggleBar() { layout.immersiveBarOpen = !layout.immersiveBarOpen }
function openPeek() { layout.immersiveBarOpen = true; layout.immersivePeek = true }

async function minimize() { try { await windowAPI.minimize() } catch { /* ignore */ } }
async function toggleMax() {
  try {
    const r = await windowAPI.toggleMax()
    if (typeof r === 'boolean') isMax.value = r
    else isMax.value = !isMax.value
  } catch { /* ignore */ }
}
async function closeWin() { try { await windowAPI.close() } catch { /* ignore */ } }
</script>

<template>
  <div class="im-root">
    <!-- 角落球：展开细条（细条展开后由条内“收起”按钮代替，避免和窗口按钮重叠） -->
    <button v-show="!barOpen" class="im-orb" title="控制条（Esc 退出沉浸）" @click="toggleBar">
      <AppIcon name="eye" size="15" />
    </button>

    <!-- 细条顶栏 -->
    <div v-if="barOpen" class="im-bar">
      <div ref="brandEl" class="im-brand pywebview-drag-region">
        <img class="im-logo" src="/icon.jpg" alt="AgentCluster" />
        <span class="im-title">沉浸模式</span>
      </div>
      <!-- 中段整块可拖动窗口（有工作时光标左侧显示运行状态） -->
      <div ref="dragFillEl" class="im-drag-fill pywebview-drag-region">
        <span v-if="hasWork" class="im-status active">
          <span class="im-dot" />
          {{ summary }}
        </span>
      </div>
      <div class="im-actions">
        <button class="im-btn" :class="{ accent: hasWork }" title="查看运行中的输出" @click="openPeek">
          <AppIcon name="message-square" size="14" />
          <span>工作输出</span>
          <span v-if="units.length" class="im-badge">{{ units.length }}</span>
        </button>
        <button class="im-btn" title="返回工作台" @click="exitImmersive">
          <AppIcon name="arrow-left" size="14" />
          <span>返回工作台</span>
        </button>
        <button class="im-win" title="收起控制条（Esc）" @click="layout.immersiveBarOpen = false">
          <AppIcon name="chevron-down" size="14" />
        </button>
        <template v-if="isDesktop">
          <button class="im-win" title="最小化" @click="minimize">
            <svg width="11" height="11" viewBox="0 0 12 12"><rect x="1" y="5.4" width="10" height="1.2" fill="currentColor"/></svg>
          </button>
          <button class="im-win" title="最大化/还原" @click="toggleMax">
            <svg v-if="!isMax" width="11" height="11" viewBox="0 0 12 12"><rect x="1.6" y="1.6" width="8.8" height="8.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
            <svg v-else width="11" height="11" viewBox="0 0 12 12"><rect x="2.6" y="1" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="2.6" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
          </button>
          <button class="im-win im-close" title="关闭" @click="closeWin">
            <svg width="11" height="11" viewBox="0 0 12 12"><path d="M2.2 2.2 L9.8 9.8 M9.8 2.2 L2.2 9.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
          </button>
        </template>
      </div>
    </div>

    <!-- 状态徽标（细条收起时仍在） -->
    <div v-if="showChip" class="im-chip" title="点击查看运行输出" @click="openPeek">
      <span class="im-dot" />
      <span class="im-chip-text">{{ summary }}</span>
      <span class="im-chip-go">查看</span>
    </div>

    <!-- 运行输出浮窗 -->
    <RunPeekPanel v-if="peekOpen" />

    <!-- 首次进入提示 -->
    <div v-if="hint" class="im-toast">{{ hint }}</div>
  </div>
</template>

<style scoped>
.im-root {
  position: fixed;
  inset: 0;
  z-index: 6;
  pointer-events: none;
  font-family: var(--app-font);
}

/* —— 角落球 —— */
.im-orb {
  position: absolute;
  top: 8px;
  right: 10px;
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  border: 1px solid rgba(var(--surface-rgb), 0.16);
  background: rgba(8, 10, 14, 0.34);
  color: rgba(255, 255, 255, 0.86);
  cursor: pointer;
  pointer-events: auto;
  user-select: none;
  opacity: 0.28;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  transition: opacity .2s, transform .2s, background .2s;
}
.im-orb:hover { opacity: 1; transform: scale(1.06); background: rgba(8, 10, 14, 0.6); }
.im-orb.open { opacity: 0.9; }

/* —— 细条顶栏 —— */
.im-bar {
  position: absolute;
  top: 6px;
  left: 10px;
  right: 10px;
  height: 44px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px 0 12px;
  border-radius: 14px;
  pointer-events: auto;
  background: rgba(8, 10, 14, 0.52);
  backdrop-filter: blur(22px) saturate(160%);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  border: 1px solid rgba(255, 255, 255, 0.12);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.92);
  user-select: none;
  animation: im-drop .18s ease-out;
  /* 整条控制条都是窗口拖动区；按钮区单独 no-drag，否则点不到按钮 */
  -webkit-app-region: drag;
  app-region: drag;
}
@keyframes im-drop {
  from { opacity: 0; transform: translateY(-6px); }
  to { opacity: 1; transform: none; }
}

.im-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  user-select: none;
  flex-shrink: 0;
  align-self: stretch;
}
.im-logo { width: 22px; height: 22px; border-radius: 7px; object-fit: cover; }
.im-title { font-size: calc(12px * var(--font-scale)); letter-spacing: .4px; opacity: .9; }

/* 中段拖动区：占满剩余宽度与整条高度，状态文案放在里面（文案本身不可点，不影响拖动） */
.im-drag-fill {
  flex: 1;
  min-width: 24px;
  height: 100%;
  display: flex;
  align-items: center;
  overflow: hidden;
  cursor: grab;
}
.im-drag-fill:active { cursor: grabbing; }

.im-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  font-size: calc(11.5px * var(--font-scale));
  color: rgba(255, 255, 255, 0.66);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.im-status.active { color: rgba(255, 255, 255, 0.9); }

.im-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #7ee2a8;
  flex-shrink: 0;
  animation: im-pulse 1.7s ease-out infinite;
}
@keyframes im-pulse {
  0% { box-shadow: 0 0 0 0 rgba(126, 226, 168, 0.45); }
  70% { box-shadow: 0 0 0 7px rgba(126, 226, 168, 0); }
  100% { box-shadow: 0 0 0 0 rgba(126, 226, 168, 0); }
}

.im-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  -webkit-app-region: no-drag;
  app-region: no-drag;
}
.im-btn {
  height: 28px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.84);
  font-size: calc(11.5px * var(--font-scale));
  cursor: pointer;
  transition: background .15s, border-color .15s;
}
.im-btn:hover { background: rgba(255, 255, 255, 0.14); }
.im-btn.accent {
  border-color: rgba(126, 226, 168, 0.38);
  background: rgba(126, 226, 168, 0.14);
  color: #d7ffe7;
}
.im-badge {
  min-width: 16px; height: 16px; padding: 0 4px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 8px;
  font-size: calc(10px * var(--font-scale));
  font-weight: 600;
  background: rgba(126, 226, 168, 0.26);
  color: #eafff2;
}

.im-win {
  width: 27px; height: 26px;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; border-radius: 6px;
  background: transparent;
  color: rgba(255, 255, 255, 0.72);
  cursor: pointer;
  transition: background .15s, color .15s;
}
.im-win:hover { background: rgba(255, 255, 255, 0.14); color: #fff; }
.im-close:hover { background: #e81123; color: #fff; }

/* —— 状态徽标（细条收起时） —— */
.im-chip {
  position: absolute;
  top: 10px;
  left: 50%;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 60vw;
  padding: 5px 10px 5px 9px;
  border-radius: 999px;
  pointer-events: auto;
  cursor: pointer;
  background: rgba(8, 10, 14, 0.42);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid rgba(255, 255, 255, 0.14);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
  color: rgba(255, 255, 255, 0.9);
  font-size: calc(11.5px * var(--font-scale));
  opacity: 0.72;
  transition: opacity .18s, background .18s;
  animation: im-fade .3s ease-out;
}
.im-chip:hover { opacity: 1; background: rgba(8, 10, 14, 0.66); }
@keyframes im-fade { from { opacity: 0; } to { opacity: .72; } }
.im-chip-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.im-chip-go {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.12);
  font-size: calc(10.5px * var(--font-scale));
  opacity: .9;
}

/* —— 首次提示 —— */
.im-toast {
  position: absolute;
  left: 50%;
  bottom: 28px;
  transform: translateX(-50%);
  max-width: 70vw;
  padding: 8px 16px;
  border-radius: 10px;
  pointer-events: none;
  background: rgba(8, 10, 14, 0.56);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: rgba(255, 255, 255, 0.9);
  font-size: calc(12px * var(--font-scale));
  line-height: 1.5;
  text-align: center;
  animation: im-fade-in .25s ease-out;
}
@keyframes im-fade-in { from { opacity: 0; transform: translate(-50%, 6px); } to { opacity: 1; transform: translate(-50%, 0); } }
</style>
