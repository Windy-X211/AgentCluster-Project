<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

const props = defineProps<{ text: string }>()

const visible = ref(false)
const triggerRef = ref<HTMLElement | null>(null)
const tipStyle = ref<Record<string, string>>({})

const HIDE_DELAY = 180
const TIP_W = 360
let hideTimer: ReturnType<typeof setTimeout> | undefined

function placeTip() {
  const el = triggerRef.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const maxH = Math.max(80, Math.min(420, window.innerHeight - 16))
  const placeLeft = window.innerWidth - r.right - 12 < TIP_W && r.left > TIP_W + 12
  let left = placeLeft ? r.left - TIP_W - 12 : r.right + 12
  left = Math.max(8, Math.min(left, window.innerWidth - TIP_W - 8))
  const top = Math.max(8, Math.min(r.top, window.innerHeight - maxH - 8))
  tipStyle.value = {
    position: 'fixed',
    left: `${left}px`,
    top: `${top}px`,
    width: `${TIP_W}px`,
    maxHeight: `${maxH}px`
  }
}

function syncPos() {
  if (visible.value) placeTip()
}

function cancelHide() {
  if (hideTimer !== undefined) {
    clearTimeout(hideTimer)
    hideTimer = undefined
  }
}

function show() {
  if (!props.text) return
  cancelHide()
  visible.value = true
  placeTip()
  window.addEventListener('scroll', syncPos, true)
  window.addEventListener('resize', syncPos)
}

function hide() {
  cancelHide()
  visible.value = false
  window.removeEventListener('scroll', syncPos, true)
  window.removeEventListener('resize', syncPos)
}

function scheduleHide() {
  cancelHide()
  hideTimer = setTimeout(hide, HIDE_DELAY)
}

onBeforeUnmount(() => {
  window.removeEventListener('scroll', syncPos, true)
  window.removeEventListener('resize', syncPos)
  cancelHide()
})
</script>

<template>
  <div ref="triggerRef" class="ht-trigger" @mouseenter="show" @mouseleave="scheduleHide">
    <slot />
    <Teleport to="body">
      <div v-if="visible" class="ht-tip" :style="tipStyle" @mouseenter="cancelHide" @mouseleave="hide">{{ text }}</div>
    </Teleport>
  </div>
</template>

<style scoped>
.ht-tip {
  z-index: 3100; padding: 10px 12px; border-radius: 8px;
  font-size: 12px; line-height: 1.55; overflow-y: auto; overflow-x: hidden;
  background: rgba(30, 34, 50, 0.98);
  border: 1px solid rgba(255, 255, 255, 0.14);
  color: rgb(var(--text-rgb));
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(8px);
  white-space: pre-wrap; word-break: break-word;
  cursor: default;
  scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, 0.25) transparent;
}
</style>