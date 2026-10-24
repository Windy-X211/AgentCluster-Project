<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useLayoutStore } from '@/stores'
import { api } from '@/api'

const layout = useLayoutStore()
const list = ref<any[]>([])
const loaded = ref(false)
const err = ref('')

async function reload() {
  try {
    list.value = await api.backgrounds.list()
  } catch (e: any) {
    err.value = e?.message || String(e)
  }
}

onMounted(async () => {
  await reload()
  loaded.value = true
})

// 当前选中的背景不在列表中时（如刚上传的新背景），重新加载列表
watch(() => layout.backgroundId, async (id) => {
  if (id && !list.value.some(b => b.id === id)) {
    await reload()
  }
})

const current = computed(() =>
  layout.backgroundId ? list.value.find(b => b.id === layout.backgroundId) || null : null
)

const src = computed(() => {
  if (!current.value) return ''
  return api.backgrounds.rawUrl(current.value.id, current.value.entry)
})

const layerStyle = computed(() => ({
  opacity: layout.backgroundOpacity,
  filter: layout.backgroundBlur > 0 ? `blur(${layout.backgroundBlur}px)` : 'none',
}))
</script>

<template>
  <div v-if="current" class="bg-layer" :style="layerStyle">
    <iframe
      v-if="current.type === 'animation'"
      :src="src"
      class="bg-fill"
      sandbox="allow-scripts"
      allowtransparency="true"
    />
    <video
      v-else-if="current.type === 'video'"
      :src="src"
      class="bg-fill"
      autoplay
      muted
      loop
      playsinline
    />
    <img
      v-else-if="current.type === 'image'"
      :src="src"
      class="bg-fill"
      alt=""
    />
  </div>
</template>

<style scoped>
.bg-layer {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
  background: #0a0e1a;
}
.bg-fill {
  width: 100%;
  height: 100%;
  display: block;
  border: 0;
  object-fit: cover;
}
</style>