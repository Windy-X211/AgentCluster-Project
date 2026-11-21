<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
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
  // 沉浸模式：背景是主角，临时忽略用户设的不透明度/模糊度，恢复原始清晰度
  opacity: layout.immersive ? 1 : layout.backgroundOpacity,
  filter: (!layout.immersive && layout.backgroundBlur > 0) ? `blur(${layout.backgroundBlur}px)` : 'none',
}))

// 沉浸模式 + 动画背景 → 可交互（HTML 小游戏）：iframe 直接接收可信鼠标/键盘事件。
// 两类动画背景的交互方式不同，必须区分，否则会互相拖死：
//   · manifest.playable === true  → 原生交互（游戏）：iframe 自己收鼠标/键盘
//   · manifest.interactive === true（且未声明 playable）→ 视差协议：保持 pointer-events:none，
//     继续由 window 监听 + postMessage 转发（内置星空/流星雨这类背景依赖此协议）
//   · 两者都没有 → 默认按原生交互处理（导入的 HTML 小游戏通常不会写 interactive）
const playable = computed(() => {
  const c: any = current.value
  if (!layout.immersive || c?.type !== 'animation') return false
  if (c.playable === true) return true
  if (c.playable === false) return false
  return c.interactive !== true
})

// 沙箱能力说明：
//  · 可交互态（小游戏）额外需要指针锁（FPS 类）与弹窗（部分游戏的全屏提示）
//  · HTML 背景一律带 allow-same-origin：缺了它帧的源是不透明的，localStorage 直接抛
//    SecurityError，而背景脚本通常在启动阶段读存档，一抛错就中断后续初始化
//    （snake.html 就是停在 resizeCanvas 之前，canvas 保持默认 300×150，布局被压扁）。
//  · 安全性：背景帧由 127.0.0.1:8870 提供，应用本体在 localhost:5173，二者不同源，
//    同源授权只让背景拥有自己稳定的源，拿不到应用自身的 localStorage / DOM。
const iframeSandbox = computed(() => {
  const base = playable.value
    ? 'allow-scripts allow-pointer-lock allow-popups'
    : 'allow-scripts'
  return current.value?.type === 'animation' ? `${base} allow-same-origin` : base
})
// pointer-lock 不是合法的 permission-policy 名称（沙箱 token 已经覆盖），带上会被浏览器告警
const iframeAllow = 'fullscreen; autoplay; encrypted-media; gamepad'

const iframeRef = ref<HTMLIFrameElement | null>(null)

// 进入可交互模式时把焦点交给 iframe，键盘（WASD/方向键）才能落到游戏里。
// 宿主文档仍可用 Esc 退出（用户点角落球时焦点会回到宿主）。
watch([playable, () => current.value?.id], async ([p]) => {
  if (!p) return
  await nextTick()
  const el = iframeRef.value
  el?.focus()
  try { el?.contentWindow?.focus() } catch { /* 沙箱内不可达时忽略 */ }
}, { immediate: true })

function focusFrame() {
  if (!playable.value) return
  const el = iframeRef.value
  el?.focus()
  try { el?.contentWindow?.focus() } catch { /* ignore */ }
}

// 交互式动画背景：背景层不接收鼠标事件，改为在 window 上监听并把坐标转发给 iframe
let lastPostAt = 0

function postPointer(kind: 'move' | 'down', e: PointerEvent) {
  if (!current.value?.interactive || playable.value) return
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
  <div v-if="current" class="bg-layer" :class="{ playable }" :style="layerStyle">
    <iframe
      v-if="current.type === 'animation'"
      ref="iframeRef"
      :src="src"
      class="bg-fill"
      :sandbox="iframeSandbox"
      :allow="iframeAllow"
      allowfullscreen
      tabindex="0"
      @load="focusFrame"
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
  background: var(--bg-base);
}
/* 沉浸态可交互背景：让 iframe 直接接收鼠标/触摸（宿主 grid 此时 pointer-events:none，
   所以命中测试会穿过界面层落到这里） */
.bg-layer.playable { pointer-events: auto; }
.bg-layer.playable .bg-fill { pointer-events: auto; cursor: default; }
.bg-fill {
  width: 100%;
  height: 100%;
  display: block;
  border: 0;
  object-fit: cover;
  outline: none;
}
</style>