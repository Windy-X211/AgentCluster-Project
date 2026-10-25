<script setup lang="ts">
import { onMounted, onBeforeUnmount } from 'vue'
import { isDesktop } from '@/utils/env'

let api: any = null
let ready = false

function bindApi() {
  const w = window as any
  if (w.pywebview?.api) { api = w.pywebview.api; ready = true }
}
onMounted(() => { bindApi(); if (!ready) window.addEventListener('pywebviewready', bindApi) })
onBeforeUnmount(() => window.removeEventListener('pywebviewready', bindApi))

// 手动缩放状态
interface DragState {
  dir: string
  startX: number
  startY: number
  startLeft: number
  startTop: number
  startW: number
  startH: number
  minW: number
  minH: number
}
let drag: DragState | null = null
let rafId = 0

function onMouseMove(ev: MouseEvent) {
  if (!drag || !api) return
  // 下一帧再调 Python，减少调用频率
  cancelAnimationFrame(rafId)
  rafId = requestAnimationFrame(() => {
    const { dir, startX, startY, startLeft, startTop, startW, startH, minW, minH } = drag!
    const cx = ev.screenX, cy = ev.screenY
    let newW = startW, newH = startH, newL = startLeft, newT = startTop
    const startRight = startLeft + startW
    const startBottom = startTop + startH

    switch (dir) {
      case 'right':
        newW = Math.max(cx - startLeft, minW); break
      case 'left':
        newW = Math.max(startRight - cx, minW);
        newL = startRight - newW; break
      case 'bottom':
        newH = Math.max(cy - startTop, minH); break
      case 'top':
        newH = Math.max(startBottom - cy, minH);
        newT = startBottom - newH; break
      case 'br':
        newW = Math.max(cx - startLeft, minW);
        newH = Math.max(cy - startTop, minH); break
      case 'bl':
        newW = Math.max(startRight - cx, minW);
        newH = Math.max(cy - startTop, minH);
        newL = startRight - newW; break
      case 'tr':
        newW = Math.max(cx - startLeft, minW);
        newH = Math.max(startBottom - cy, minH);
        newT = startBottom - newH; break
      case 'tl':
        newW = Math.max(startRight - cx, minW);
        newH = Math.max(startBottom - cy, minH);
        newL = startRight - newW;
        newT = startBottom - newH; break
    }
    // Fire-and-forget：单次 set_bounds（Win32 SetWindowPos 原子移动+缩放），
    // 比每帧两次 move+resize 桥调用少一半跨线程开销，避免 WebView2 重绘露白
    try { api.set_bounds(newL, newT, newW, newH) } catch {}
  })
}

function onMouseUp() {
  if (!drag) return
  drag = null
  cancelAnimationFrame(rafId)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
}

async function startResize(dir: string, ev: MouseEvent) {
  ev.preventDefault()
  if (!api?.get_bounds) return
  // 记录初始状态（Python 端拿窗口 bounds）
  let bounds: any = {}
  try { bounds = await api.get_bounds() } catch { return }
  drag = {
    dir,
    startX: ev.screenX, startY: ev.screenY,
    startLeft: bounds.x, startTop: bounds.y,
    startW: bounds.width, startH: bounds.height,
    minW: 960, minH: 600,  // 与 create_window min_size 一致，避免拖到更小被钳制跳动
  }
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
}
</script>

<template>
  <div v-if="isDesktop" class="rh-root">
    <div class="rh rh-top"     @mousedown.prevent="startResize('top', $event)"></div>
    <div class="rh rh-bottom" @mousedown.prevent="startResize('bottom', $event)"></div>
    <div class="rh rh-left"    @mousedown.prevent="startResize('left', $event)"></div>
    <div class="rh rh-right"   @mousedown.prevent="startResize('right', $event)"></div>
    <div class="rh rh-tl" @mousedown.prevent="startResize('tl', $event)"></div>
    <div class="rh rh-tr" @mousedown.prevent="startResize('tr', $event)"></div>
    <div class="rh rh-bl" @mousedown.prevent="startResize('bl', $event)"></div>
    <div class="rh rh-br" @mousedown.prevent="startResize('br', $event)"></div>
  </div>
</template>

<style scoped>
.rh-root {
  position: fixed; inset: 0; z-index: 99998; pointer-events: none;
}
.rh { position: absolute; pointer-events: auto; background: transparent; }
.rh-top, .rh-bottom  { left: 60px; right: 60px; height: 10px; }
.rh-top              { top: 0;    cursor: ns-resize; }
.rh-bottom           { bottom: 0; cursor: ns-resize; }
.rh-left, .rh-right  { top: 60px; bottom: 40px; width: 10px; }
.rh-left             { left: 0;   cursor: ew-resize; }
.rh-right            { right: 0;  cursor: ew-resize; }
.rh-tl { top: 0;    left: 0;    width: 20px; height: 20px; cursor: nwse-resize; }
.rh-tr { top: 0;    right: 0;   width: 20px; height: 20px; cursor: nesw-resize; }
.rh-bl { bottom: 0; left: 0;    width: 20px; height: 20px; cursor: nesw-resize; }
.rh-br { bottom: 0; right: 0;   width: 20px; height: 20px; cursor: nwse-resize; }
</style>
