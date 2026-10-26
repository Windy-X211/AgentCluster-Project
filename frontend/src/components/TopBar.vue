<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { RouterLink } from 'vue-router'
import { isDesktop, windowAPI } from '@/utils/env'
import { useSnap } from '@/composables/useSnap'

const isMax = ref(false)

onMounted(() => {
  if (isDesktop) document.documentElement.classList.add('desktop-frameless')
  // 同步初始最大化状态
  windowAPI.isMaximized?.().then((v: boolean | null) => { if (typeof v === 'boolean') isMax.value = v }).catch(() => {})
  // Electron 最大化状态事件
  if (window.electronAPI?.on) {
    window.electronAPI.on('win:maximized', (v: boolean) => { isMax.value = v })
  }
  // Aero Snap 边缘吸附：叠加在原生拖动之上
  if (isDesktop) {
    const { attachDragRegion } = useSnap()
    const brand = document.querySelector('.brand.pywebview-drag-region') as HTMLElement | null
    const spacer = document.querySelector('.spacer.pywebview-drag-region') as HTMLElement | null
    brand && attachDragRegion(brand)
    spacer && attachDragRegion(spacer)
  }
})
onBeforeUnmount(() => {
  document.documentElement.classList.remove('desktop-frameless')
})
async function minimize() { try { await windowAPI.minimize() } catch {} }
async function toggleMax() {
  try {
    const r = await windowAPI.toggleMax()
    if (typeof r === 'boolean') isMax.value = r
    else isMax.value = !isMax.value
  } catch {}
}
async function close() { try { await windowAPI.close() } catch {} }
</script>

<template>
  <!-- TopBar 不加 pywebview-drag-region —— 只让 .brand 和 .spacer 可拖 -->
  <!-- 这样按钮/链接的祖先链不匹配 selector，正常点击 -->
  <div class="topbar-content">
    <!-- 拖动区 1：品牌+图标 -->
    <div class="brand pywebview-drag-region">
      <img class="logo" src="/icon.png" alt="AgentCluster" />
      <div>
        <div class="title">智能体集群</div>
        <div class="subtitle">AgentCluster</div>
      </div>
    </div>
    <!-- 拖动区 2：中间空白 -->
    <div class="spacer pywebview-drag-region" />
    <!-- 下面这些都没有 pywebview-drag-region 类 → 可正常点击 -->
    <RouterLink to="/capabilities" class="nav-item">🛠 能力管理</RouterLink>
    <RouterLink to="/settings" class="nav-item">⚙ 设置</RouterLink>
    <template v-if="isDesktop">
      <button class="win-btn" title="最小化" @click="minimize">
        <svg width="12" height="12" viewBox="0 0 12 12"><rect x="1" y="5.4" width="10" height="1.2" fill="currentColor"/></svg>
      </button>
      <button class="win-btn" title="最大化/还原" @click="toggleMax">
        <svg v-if="!isMax" width="12" height="12" viewBox="0 0 12 12"><rect x="1.6" y="1.6" width="8.8" height="8.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
        <svg v-else width="12" height="12" viewBox="0 0 12 12"><rect x="2.6" y="1" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="2.6" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
      <button class="win-btn win-close" title="关闭" @click="close">
        <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.2 2.2 L9.8 9.8 M9.8 2.2 L2.2 9.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </template>
  </div>
</template>

<style scoped>
.topbar-content {
  display: flex; align-items: center; height: 100%; padding: 0 20px; gap: 16px;
}

/* pywebview-drag-region：pywebview body 级 onBodyMouseDown 识别的类名
   （customize.js 第 89 行 document.querySelectorAll('%(drag_selector)s')）
   只在 easy_drag=False 时这个机制才生效（easy_drag=True 会额外让整个 window 可拖） */
.pywebview-drag-region {
  user-select: none;
}

.brand { display: flex; align-items: center; gap: 12px; }
.logo { width: 32px; height: 32px; border-radius: 10px; object-fit: cover; box-shadow: 0 4px 12px rgba(var(--accent-rgb), 0.3); }
.title { font-weight: 600; font-size: calc(15px * var(--font-scale)); }
.subtitle { font-size: calc(10px * var(--font-scale)); opacity: .6; letter-spacing: 1px; }
/* spacer 是空 div，flex align-items:center 下会塌到 0 高，无法接收鼠标事件 */
/* 必须加 align-self:stretch + height:100% 让它撑满顶栏高度 */
.spacer { flex: 1; min-width: 20px; align-self: stretch; height: 100%; }

.nav-item { color: rgb(var(--text-rgb)); text-decoration: none; font-size: calc(13px * var(--font-scale)); padding: 6px 12px; border-radius: 8px; transition: background .2s; user-select: none; &:hover { background: rgba(var(--surface-rgb), 0.06); } }
.win-btn {
  width: 32px; height: 28px; border: none; background: transparent;
  color: rgb(var(--text-rgb)); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 6px; transition: background 0.15s, color 0.15s, opacity 0.15s;
  opacity: 0.7;
}
.win-btn:hover { background: rgba(var(--surface-rgb), 0.12); opacity: 1; }
.win-close:hover { background: #e81123; color: #fff; opacity: 1; }
</style>
