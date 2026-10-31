<script setup lang="ts">
import { watch, computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { useLayoutStore, useConvStore, useCanvasStore } from '@/stores'
import GuideManager from '@/components/guide/GuideManager.vue'
import BackgroundLayer from '@/components/BackgroundLayer.vue'
import WindowControls from '@/components/WindowControls.vue'
import ResizeHandles from '@/components/ResizeHandles.vue'

const layout = useLayoutStore()
const route = useRoute()

// 临时对话小窗（精灵悬浮球拓展）：自绘标题栏 + 自管窗口生命周期，
// 全局窗口控制/边缘手柄/新手引导都不进小窗（WindowControls 的 IPC 操作的是主窗）
const isQuickChat = computed(() => route.path === '/quickchat')

function applyFontScale(v: number) {
  document.documentElement.style.setProperty('--font-scale', String(v))
}

function applyTheme(t: string) {
  document.documentElement.setAttribute('data-theme', t)
}

watch(() => layout.fontScale, applyFontScale, { immediate: true })
watch(() => layout.theme, applyTheme, { immediate: true })

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
