<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useLayoutStore } from '@/stores'
import { api } from '@/api'

const layout = useLayoutStore()
const list = ref<any[]>([])
const loaded = ref(false)
const err = ref('')

async function reload() {
  // 后端启动可能晚于前端，首次请求可能失败，重试几次
  for (let i = 0; i < 5; i++) {
    try {
      list.value = await api.backgrounds.list()
      err.value = ''
      return
    } catch (e: any) {
      err.value = e?.message || String(e)
      await new Promise(r => setTimeout(r, 300 * (i + 1)))
    }
  }
}

onMounted(() => {
  loaded.value = true
})

// 启动恢复与手动切换走同一逻辑：选中背景不在列表中则重新加载
watch(() => layout.backgroundId, async (id) => {
  if (id && !list.value.some(b => b.id === id)) {
    await reload()
  }
}, { immediate: true })

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

// 交互式动画背景：背景层不接收鼠标事件，改为在 window 上监听并把坐标转发给 iframe
const iframeRef = ref<HTMLIFrameElement | null>(null)
let lastPostAt = 0

function postPointer(kind: 'move' | 'down', e: PointerEvent) {
  if (!current.value?.interactive) return
  const win = iframeRef.value?.contentWindow
  if (!win) return
  win.postMessage({
    type: 'bg-pointer',
    kind,
    x: e.clientX / (window.innerWidth || 1),
    y: e.clientY / (window.innerHeight || 1),
  }, '*')
}

function onPointerMove(e: PointerEvent) {
  const now = performance.now()
  if (now - lastPostAt < 24) return
  lastPostAt = now
  postPointer('move', e)
}

function onPointerDown(e: PointerEvent) {
  postPointer('down', e)
}

onMounted(() => {
  window.addEventListener('pointermove', onPointerMove, { passive: true })
  window.addEventListener('pointerdown', onPointerDown, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerdown', onPointerDown)
})
</script>

<template>
  <div v-if="current" class="bg-layer" :style="layerStyle">
    <iframe
      v-if="current.type === 'animation'"
      ref="iframeRef"
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