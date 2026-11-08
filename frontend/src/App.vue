<script setup lang="ts">
import { watch, computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { useLayoutStore, useConvStore, useCanvasStore, FONT_FAMILIES } from '@/stores'
import GuideManager from '@/components/guide/GuideManager.vue'
import BackgroundLayer from '@/components/BackgroundLayer.vue'
import WindowControls from '@/components/WindowControls.vue'
import ResizeHandles from '@/components/ResizeHandles.vue'

const layout = useLayoutStore()
const route = useRoute()

// 临时对话小窗（精灵悬浮球拓展）：自绘标题栏 + 自管窗口生命周期，
// 全局窗口控制/边缘手柄/新手引导都不进小窗（WindowControls 的 IPC 操作的是主窗）
const isQuickChat = computed(() => route.path === '/quickchat')
const isHomeRoute = computed(() => route.path === '/' || route.path === '')

/**
 * KeepAlive 缓存 Home 页面时，Home 不卸载但被 deactivated。
 * 此时 Home 子组件内 <Teleport to="body"> 的全屏遮罩层（modal-bg / picker-mask /
 * confirm-mask / modal-backdrop / rules-overlay / gw-overlay 等）会残留在 body 上，
 * 覆盖后续设置/市场等路由页面，导致点击无反应。
 *
 * 主修复方案是在各 Home 子组件的 onDeactivated 中主动关闭 modal 状态。
 * 这里再加一层 DOM 级兜底：当路由不在 Home 时，强制隐藏 body 上所有 Home 相关的
 * 全屏遮罩元素（Vue 还管理着这些 DOM，我们只隐藏不删除）。
 */
const HOME_MASK_SELECTORS = [
  '.modal-bg', '.picker-mask', '.confirm-mask', '.modal-backdrop',
  '.rules-overlay', '.gw-overlay', '.mk-modal-mask',
]
watch(isHomeRoute, (home) => {
  if (home) {
    // 回到 Home：恢复所有遮罩（由 Vue 重新控制显隐）
    document.body.classList.remove('home-deactivated')
    HOME_MASK_SELECTORS.forEach(sel => {
      document.querySelectorAll<HTMLElement>(sel).forEach(el => {
        el.style.display = ''
      })
    })
  } else {
    // 离开 Home：强制隐藏所有残留的全屏遮罩
    document.body.classList.add('home-deactivated')
    HOME_MASK_SELECTORS.forEach(sel => {
      document.querySelectorAll<HTMLElement>(sel).forEach(el => {
        // 只隐藏那些全屏覆盖的（position:fixed + inset:0）
        const cs = getComputedStyle(el)
        if (cs.position === 'fixed') {
          el.style.display = 'none'
        }
      })
    })
  }
}, { immediate: true })

function applyFontScale(v: number) {
  document.documentElement.style.setProperty('--font-scale', String(v))
}

function applyTheme(t: string) {
  document.documentElement.setAttribute('data-theme', t)
}

function applyFontFamily(id: string) {
  const f = FONT_FAMILIES.find(x => x.id === id) || FONT_FAMILIES[0]
  document.documentElement.style.setProperty('--app-font', f.stack)
}

watch(() => layout.fontScale, applyFontScale, { immediate: true })
watch(() => layout.theme, applyTheme, { immediate: true })
watch(() => layout.fontFamily, applyFontFamily, { immediate: true })

// 会话/画布选中项跨会话记忆：整页重载（Electron 重开窗 / 浏览器刷新）后恢复上次所在位置
const convs = useConvStore()
const canvases = useCanvasStore()
watch(() => convs.current, v => {
  if (v != null) { try { localStorage.setItem('agent-cluster.conversation.current', String(v)) } catch { /* ignore */ } }
})
watch(() => canvases.current?.id, v => {
  if (v != null) { try { localStorage.setItem('agent-cluster.canvas.current', String(v)) } catch { /* ignore */ } }
})
</script>

<template>
  <!-- 边缘缩放手柄（透明 overlay，点击调 Python API） -->
  <ResizeHandles v-if="!isQuickChat" />
  <!-- 全局窗口控制：右上角 min/max/close（非 Home 页面；小窗自绘标题栏） -->
  <WindowControls v-if="!isQuickChat" />
  <BackgroundLayer />
  <RouterView v-slot="{ Component }">
    <KeepAlive :include="['Home']">
      <component :is="Component" />
    </KeepAlive>
  </RouterView>
  <GuideManager v-if="!isQuickChat" />
</template>
