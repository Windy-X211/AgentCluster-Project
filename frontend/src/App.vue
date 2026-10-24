<script setup lang="ts">
import { watch } from 'vue'
import { RouterView } from 'vue-router'
import { useLayoutStore } from '@/stores'
import GuideManager from '@/components/guide/GuideManager.vue'
import BackgroundLayer from '@/components/BackgroundLayer.vue'

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
  <BackgroundLayer />
  <RouterView />
  <GuideManager />
</template>
