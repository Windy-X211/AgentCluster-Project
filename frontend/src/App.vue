<script setup lang="ts">
import { watch } from 'vue'
import { RouterView } from 'vue-router'
import { useLayoutStore } from '@/stores'
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
