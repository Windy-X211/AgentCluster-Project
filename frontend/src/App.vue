<script setup lang="ts">
import { watch } from 'vue'
import { RouterView } from 'vue-router'
import { useLayoutStore, useConvStore, useCanvasStore } from '@/stores'
import GuideManager from '@/components/guide/GuideManager.vue'
import BackgroundLayer from '@/components/BackgroundLayer.vue'
import WindowControls from '@/components/WindowControls.vue'
import ResizeHandles from '@/components/ResizeHandles.vue'

const layout = useLayoutStore()

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
  <ResizeHandles />
  <!-- 全局窗口控制：右上角 min/max/close（非 Home 页面） -->
  <WindowControls />
  <BackgroundLayer />
  <RouterView v-slot="{ Component }">
    <KeepAlive :include="['Home']">
      <component :is="Component" />
    </KeepAlive>
  </RouterView>
  <GuideManager />
</template>
