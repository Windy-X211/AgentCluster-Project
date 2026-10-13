<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps<{
  modelValue: string
  options: { name: string; desc: string }[]
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const open = ref(false)
const triggerRef = ref<HTMLElement | null>(null)
const listStyle = ref<Record<string, string>>({})

const tipVisible = ref(false)
const hoverEl = ref<HTMLElement | null>(null)
const hoverText = ref('')
const tipStyle = ref<Record<string, string>>({})
const TIP_W = 360

const label = computed(() => {
  if (props.modelValue) {
    const hit = props.options.find(o => o.name === props.modelValue)
    return hit ? hit.name : props.modelValue
  }
  return '选择参数…'
})
const currentOption = computed(() => props.options.find(o => o.name === props.modelValue) || null)

function syncListPos() {
  const el = triggerRef.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const spaceBelow = window.innerHeight - r.bottom - 8
  const openUp = spaceBelow < 160 && r.top > spaceBelow
  listStyle.value = {
    position: 'fixed',
    left: `${r.left}px`,
    width: `${r.width}px`,
    maxHeight: `${Math.max(120, Math.min(260, openUp ? r.top - 8 : spaceBelow))}px`,
    ...(openUp ? { bottom: `${window.innerHeight - r.top + 4}px` } : { top: `${r.bottom + 4}px` }),
  }
}

function placeTip() {
  const el = hoverEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const maxH = Math.max(80, Math.min(420, window.innerHeight - 16))
  const placeLeft = (window.innerWidth - r.right - 12) < TIP_W && r.left > TIP_W + 12
  let left = placeLeft ? r.left - TIP_W - 12 : r.right + 12
  left = Math.max(8, Math.min(left, window.innerWidth - TIP_W - 8))
  const top = Math.max(8, Math.min(r.top, window.innerHeight - maxH - 8))
  tipStyle.value = {
    position: 'fixed',
    left: `${left}px`,
    top: `${top}px`,
    width: `${TIP_W}px`,
    maxHeight: `${maxH}px`,
  }
}

function onHover(el: HTMLElement | null, text: string) {
  if (!text) { hideTip(); return }
  cancelHide()
  hoverEl.value = el
  hoverText.value = text
  tipVisible.value = true
  placeTip()
}
const HIDE_DELAY = 180
let hideTimer: ReturnType<typeof setTimeout> | undefined

function cancelHide() {
  if (hideTimer !== undefined) { clearTimeout(hideTimer); hideTimer = undefined }
}
function hideTip() { cancelHide(); tipVisible.value = false; hoverEl.value = null; hoverText.value = '' }
function scheduleHide() {
  cancelHide()
  hideTimer = setTimeout(() => { hideTip() }, HIDE_DELAY)
}
function onItemEnter(e: MouseEvent, o: { name: string; desc: string }) { onHover(e.currentTarget as HTMLElement, o.desc) }
function onTriggerEnter(e: MouseEvent) { onHover(e.currentTarget as HTMLElement, currentOption.value?.desc || '') }

function syncAll(e: Event) {
  const t = e.target as HTMLElement | null
  if (t && t.closest && t.closest('.ps-tip')) return
  if (open.value) syncListPos()
  if (tipVisible.value) placeTip()
}

function detach() {
  window.removeEventListener('scroll', syncAll, true)
  window.removeEventListener('resize', syncAll)
}

function toggle() {
  open.value = !open.value
  if (open.value) {
    syncListPos()
    window.addEventListener('scroll', syncAll, true)
    window.addEventListener('resize', syncAll)
  } else {
    detach()
  }
}
function close() { open.value = false; detach(); hideTip() }
function pick(name: string) { emit('update:modelValue', name); close() }

function onDocMousedown(e: MouseEvent) {
  if (!open.value) return
  const t = e.target as Node
  if (triggerRef.value?.contains(t)) return
  const el = t as HTMLElement
  if (el?.closest?.('.ps-list') || el?.closest?.('.ps-tip')) return
  close()
}

onMounted(() => document.addEventListener('mousedown', onDocMousedown))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocMousedown)
  detach()
  cancelHide()
})
</script>

<template>
  <div class="param-select">
    <button ref="triggerRef" type="button" class="ps-trigger mono" :class="{ open }"
            @click="toggle" @mouseenter="onTriggerEnter" @mouseleave="scheduleHide">
      <span class="ps-cur">{{ label }}</span>
      <span v-if="currentOption?.desc" class="ps-brief">{{ currentOption.desc }}</span>
      <span class="ps-caret">▾</span>
    </button>
    <Teleport to="body">
      <div v-if="open" class="ps-list" :style="listStyle">
        <button v-for="o in options" :key="o.name" type="button"
                class="ps-item" :class="{ active: o.name === modelValue }"
                @click="pick(o.name)" @mouseenter="onItemEnter($event, o)" @mouseleave="scheduleHide">
          <span class="ps-name">{{ o.name }}</span>
          <span v-if="o.desc" class="ps-brief">{{ o.desc }}</span>
        </button>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="tipVisible" class="ps-tip" :style="tipStyle"
           @mouseenter="cancelHide" @mouseleave="hideTip">{{ hoverText }}</div>
    </Teleport>
  </div>
</template>

<style scoped>
.param-select { flex: 0 0 40%; min-width: 0; }
.ps-trigger {
  display: flex; align-items: center; gap: 8px;
  width: 100%; padding: 8px 12px; border-radius: 8px; cursor: pointer; text-align: left;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.1);
  color: rgb(var(--text-rgb)); font-size: 13px; outline: none;
  transition: border-color .2s;
  &:hover, &.open { border-color: var(--accent); }
}
.mono { font-family: Consolas, monospace; font-size: 12px; }
.ps-cur { flex-shrink: 0; }
.ps-caret { opacity: .55; flex-shrink: 0; font-size: 10px; margin-left: auto; }
.ps-list {
  z-index: 3000; overflow-y: auto; padding: 4px; border-radius: 8px;
  background: rgba(26,31,48,0.98);
  border: 1px solid rgba(255,255,255,0.12);
  box-shadow: 0 12px 30px rgba(0,0,0,0.55);
  backdrop-filter: blur(8px);
}
.ps-item {
  display: flex; align-items: baseline; gap: 8px; width: 100%;
  padding: 6px 10px; border-radius: 6px; cursor: pointer;
  text-align: left; font-size: 12px; line-height: 1.4;
  background: transparent; border: none; color: rgb(var(--text-rgb));
  &:hover { background: rgba(110,168,255,0.15); }
  &.active { background: rgba(110,168,255,0.22); }
}
.ps-name { font-family: Consolas, monospace; font-weight: 600; flex-shrink: 0; }
.ps-brief {
  flex: 1 1 0; min-width: 0; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; opacity: .55;
}
.ps-tip {
  z-index: 3100; padding: 10px 12px; border-radius: 8px;
  font-size: 12px; line-height: 1.55; overflow-y: auto; overflow-x: hidden;
  background: rgba(30,34,50,0.98);
  border: 1px solid rgba(255,255,255,0.14);
  color: rgb(var(--text-rgb));
  box-shadow: 0 12px 30px rgba(0,0,0,0.55);
  backdrop-filter: blur(8px);
  white-space: pre-wrap; word-break: break-word;
  cursor: default;
  scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.25) transparent;
}
</style>
