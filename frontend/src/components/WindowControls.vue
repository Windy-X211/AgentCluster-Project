<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { isDesktop } from '@/utils/env'

const route = useRoute()
const isMax = ref(false)
let api: any = null
let ready = false

const onHome = computed(() => route.path === '/' || route.path === '')

function bindApi() {
  const w = window as any
  if (w.pywebview?.api) { api = w.pywebview.api; ready = true }
}
onMounted(() => {
  bindApi()
  if (!ready) window.addEventListener('pywebviewready', bindApi)
})
onBeforeUnmount(() => {
  window.removeEventListener('pywebviewready', bindApi)
})
async function minimize() { try { await api?.minimize() } catch {} }
async function toggleMax() {
  try {
    if (isMax.value) { await api?.restore(); isMax.value = false }
    else { await api?.maximize(); isMax.value = true }
  } catch {}
}
async function close() { try { await api?.close() } catch {} }
</script>

<template>
  <div v-if="isDesktop && !onHome" class="wc-root">
    <!-- 顶部 6px 透明拖动条（非 Home 页面顶栏缺拖动区） -->
    <!-- pywebview-drag-region 让 pywebview body 级 onBodyMouseDown 识别 -->
    <div class="wc-drag-strip pywebview-drag-region" @dblclick="toggleMax" />

    <!-- 右上角窗口控制按钮 -->
    <div class="wc-buttons">
      <button class="wc-btn" title="最小化" @click="minimize">
        <svg width="10" height="10" viewBox="0 0 12 12"><rect x="1" y="5.4" width="10" height="1.2" fill="currentColor"/></svg>
      </button>
      <button class="wc-btn" title="最大化/还原" @click="toggleMax">
        <svg v-if="!isMax" width="10" height="10" viewBox="0 0 12 12"><rect x="1.6" y="1.6" width="8.8" height="8.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
        <svg v-else width="10" height="10" viewBox="0 0 12 12"><rect x="2.6" y="1" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="2.6" width="7.4" height="7.4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
      <button class="wc-btn wc-close" title="关闭" @click="close">
        <svg width="10" height="10" viewBox="0 0 12 12"><path d="M2.2 2.2 L9.8 9.8 M9.8 2.2 L2.2 9.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.wc-root {
  position: fixed; top: 0; left: 0; right: 0;
  z-index: 99997;
  pointer-events: none;
}

/* 6px 透明拖动条：纯 WebView2 原生 -webkit-app-region: drag */
/* background: transparent —— 不要白色白条，完全隐形 */
.wc-drag-strip {
  height: 6px; width: 100%;
  pointer-events: auto;
  background: transparent;
  cursor: default;
  user-select: none;
  -webkit-app-region: drag;
  app-region: drag;
}

/* 右上角控制按钮（非 Home 页面） */
.wc-buttons {
  position: absolute;
  top: 8px; right: 8px;
  display: flex; gap: 2px;
  pointer-events: auto;
  -webkit-app-region: no-drag;
  app-region: no-drag;
  opacity: 0.55;
  transition: opacity .15s;
}
.wc-buttons:hover { opacity: 1; }

.wc-btn {
  width: 28px; height: 24px; border: none; background: transparent;
  color: rgba(var(--text-rgb), 0.85); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 5px;
  transition: background .15s, color .15s;
}
.wc-btn:hover { background: rgba(var(--surface-rgb), 0.2); color: rgb(var(--text-rgb)); }
.wc-close:hover { background: #e81123; color: #fff; }
</style>
