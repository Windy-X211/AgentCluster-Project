<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { RouterLink } from 'vue-router'
import { isDesktop, windowAPI } from '@/utils/env'

const isMax = ref(false)

// 监听 Electron 的最大化状态变化（主进程事件）
onMounted(() => {
  document.documentElement.classList.add('desktop-frameless')
  // 尝试获取当前最大化状态
  windowAPI.isMaximized?.().then((v: any) => { if (typeof v === 'boolean') isMax.value = v }).catch(() => {})
  // Electron 事件监听
  if (window.electronAPI?.on) {
    window.electronAPI.on('win:maximized', (v: boolean) => { isMax.value = v })
  }
})
onBeforeUnmount(() => {
  document.documentElement.classList.remove('desktop-frameless')
})

async function minimize() { try { await windowAPI.minimize() } catch {} }
async function toggleMax() {
  try {
    const result = await windowAPI.toggleMax()
    if (typeof result === 'boolean') isMax.value = result
    else {
      // pywebview 路径：前端自行判断
      isMax.value = !isMax.value
      if (isMax.value) await windowAPI.maximize()
      else await windowAPI.restore()
    }
  } catch {}
}
async function close() { try { await windowAPI.close() } catch {} }
</script>

<template>
  <div class="topbar-content">
    <!-- 拖动区 1：品牌+图标 -->
    <div class="brand drag-region">
      <img class="logo" src="/icon.png" alt="AgentCluster" />
      <div>
        <div class="title">智能体集群</div>
        <div class="subtitle">AgentCluster</div>
      </div>
    </div>
    <!-- 拖动区 2：中间空白 -->
    <div class="spacer drag-region" />
    <!-- 下面这些默认不可拖（按钮/链接等），Electron 上 .nav-item 和 .win-btn 会自动 no-drag -->
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

/* 拖动区：Electron 用 -webkit-app-region: drag，pywebview 用 .pywebview-drag-region */
.drag-region {
  user-select: none;
  -webkit-app-region: drag;        /* Electron */
  app-region: drag;                 /* 未来标准 */
}
/* 全局回退：如果 .pywebview-drag-region 样式存在，保持兼容 */

.brand { display: flex; align-items: center; gap: 12px; }
.logo { width: 32px; height: 32px; border-radius: 10px; object-fit: cover; box-shadow: 0 4px 12px rgba(var(--accent-rgb), 0.3); }
.title { font-weight: 600; font-size: calc(15px * var(--font-scale)); }
.subtitle { font-size: calc(10px * var(--font-scale)); opacity: .6; letter-spacing: 1px; }
/* spacer 是空 div，flex align-items:center 下会塌到 0 高，无法接收鼠标事件 */
/* 必须加 align-self:stretch + height:100% 让它撑满顶栏高度 */
.spacer { flex: 1; min-width: 20px; align-self: stretch; height: 100%; }

/* 按钮/链接是可点击区，覆盖拖动属性 */
.nav-item, .win-btn {
  -webkit-app-region: no-drag;
  app-region: no-drag;
}
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
