<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, computed } from 'vue'
import { promptState, __promptConfirm, __promptCancel, __dialogCloseAll } from '@/composables/useDialog'

const input = ref('')
const errorShown = ref(false)

// 每次打开对话框时重置输入
watch(() => promptState.open, (v) => {
  if (v) {
    input.value = promptState.defaultValue ?? ''
    errorShown.value = false
    // 自动 focus（等 DOM 渲染完）
    setTimeout(() => {
      const el = document.querySelector('.prompt-input') as HTMLInputElement | null
      if (el) {
        el.focus()
        // 如果有默认值，选中方便用户直接覆盖
        if (promptState.defaultValue) el.select()
      }
    }, 0)
  }
})

function onConfirm() {
  const v = input.value.trim()
  if (!v) {
    errorShown.value = true
    setTimeout(() => { errorShown.value = false }, 1500)
    return
  }
  __promptConfirm(v)
}
function onCancel() { __promptCancel() }

function onKey(e: KeyboardEvent) {
  if (!promptState.open) return
  if (e.key === 'Enter') { e.preventDefault(); onConfirm() }
  else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
}

onMounted(() => {
  window.addEventListener('keydown', onKey, true)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey, true)
})

const messageLines = computed(() => promptState.message.split('\n'))
</script>

<template>
  <Teleport to="body">
    <div v-if="promptState.open" class="prompt-mask" @click.self="onCancel" role="dialog" aria-modal="true">
      <div class="prompt-card">
        <div class="prompt-body">
          <div v-for="(line, i) in messageLines" :key="i" class="prompt-line">{{ line || '\u00a0' }}</div>
          <input
            class="prompt-input"
            v-model="input"
            type="text"
            :placeholder="promptState.placeholder ?? ''"
            @keydown.enter.prevent="onConfirm"
            @keydown.escape.prevent="onCancel"
          />
          <div v-if="errorShown" class="prompt-error">请输入内容</div>
        </div>
        <div class="prompt-actions">
          <button class="prompt-btn ghost" @click="onCancel">取消</button>
          <button class="prompt-btn primary" @click="onConfirm">确定</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.prompt-mask {
  position: fixed; inset: 0; z-index: 9999;
  background: var(--modal-mask, rgba(var(--shadow-rgb), 0.5));
  backdrop-filter: var(--modal-mask-blur, blur(4px)); -webkit-backdrop-filter: var(--modal-mask-blur, blur(4px));
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.prompt-card {
  width: min(440px, 100%);
  background: var(--glass-panel-bg, var(--panel-bg, #0a0a0a));
  color: var(--text, #ececec);
  border: 1px solid var(--glass-panel-edge, rgba(var(--surface-rgb, 255, 255, 255), 0.14));
  border-radius: 14px;
  box-shadow: var(--glass-panel-shadow, 0 20px 60px rgba(var(--shadow-rgb), 0.5));
  backdrop-filter: var(--glass-panel-blur, none); -webkit-backdrop-filter: var(--glass-panel-blur, none);
  overflow: hidden;
}
.prompt-body { padding: 18px 20px 14px; }
.prompt-line { font-size: 13px; line-height: 1.6; color: var(--text, #ececec); }
.prompt-input {
  width: 100%; margin-top: 12px;
  padding: 9px 12px;
  background: var(--tint-input-bg, rgba(var(--surface-rgb), 0.04));
  border: 1px solid rgba(var(--surface-rgb), 0.15);
  border-radius: 8px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  box-sizing: border-box;
  transition: border-color .15s;
}
.prompt-input:focus { border-color: var(--accent); }
.prompt-error { margin-top: 8px; font-size: 12px; color: var(--red-ink, #ff8888); }
.prompt-actions {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 12px 20px; border-top: 1px solid rgba(var(--surface-rgb), 0.08);
}
.prompt-btn {
  padding: 8px 18px; font-size: 13px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.18); background: transparent; color: var(--text);
  transition: filter .15s, background .15s;
}
.prompt-btn:hover { filter: brightness(1.15); }
.prompt-btn.primary {
  background: rgb(var(--accent-rgb, 110, 168, 255));
  border-color: transparent;
  color: #0b1220; font-weight: 600;
}
.prompt-btn.ghost:hover { background: rgba(var(--surface-rgb), 0.08); }
</style>
