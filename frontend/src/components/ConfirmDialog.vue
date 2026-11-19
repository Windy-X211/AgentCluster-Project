<script setup lang="ts">
import { onMounted, onBeforeUnmount, computed } from 'vue'
import { confirmState, __confirmConfirm, __confirmCancel } from '@/composables/useDialog'

function onConfirm() { __confirmConfirm() }
function onCancel() { __confirmCancel() }

function onKey(e: KeyboardEvent) {
  if (!confirmState.open) return
  if (e.key === 'Enter') { e.preventDefault(); onConfirm() }
  else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
}

onMounted(() => { window.addEventListener('keydown', onKey, true) })
onBeforeUnmount(() => { window.removeEventListener('keydown', onKey, true) })

const messageLines = computed(() => confirmState.message.split('\n'))
const confirmClass = computed(() => confirmState.danger ? 'danger' : 'primary')
</script>

<template>
  <Teleport to="body">
    <div v-if="confirmState.open" class="cf-mask" @click.self="onCancel" role="dialog" aria-modal="true">
      <div class="cf-card">
        <template v-if="confirmState.title">
          <div class="cf-head">
            <span class="cf-title">{{ confirmState.title }}</span>
          </div>
        </template>
        <div class="cf-body">
          <div v-for="(line, i) in messageLines" :key="i" class="cf-line">{{ line || '\u00a0' }}</div>
        </div>
        <div class="cf-actions">
          <button class="cf-btn ghost" @click="onCancel">{{ confirmState.cancelText || '取消' }}</button>
          <button class="cf-btn" :class="confirmClass" @click="onConfirm">
            {{ confirmState.confirmText || '确定' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cf-mask {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(var(--shadow-rgb), 0.5);
  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.cf-card {
  width: min(460px, 100%);
  background: var(--panel-bg, #0a0a0a);
  color: var(--text, #ececec);
  border: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.14);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(var(--shadow-rgb), 0.5);
  overflow: hidden;
}
.cf-head {
  padding: 16px 20px 0;
}
.cf-title { font-size: 15px; font-weight: 600; }
.cf-body { padding: 14px 20px; }
.cf-line { font-size: 13px; line-height: 1.65; color: var(--text, #ececec); white-space: pre-wrap; word-break: break-word; }
.cf-actions {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 12px 20px; border-top: 1px solid rgba(var(--surface-rgb), 0.08);
}
.cf-btn {
  padding: 8px 18px; font-size: 13px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.18); background: transparent; color: var(--text);
  transition: filter .15s, background .15s;
}
.cf-btn:hover { filter: brightness(1.15); }
.cf-btn.primary {
  background: rgb(var(--accent-rgb, 110, 168, 255));
  border-color: transparent;
  color: #0b1220; font-weight: 600;
}
.cf-btn.danger {
  background: rgba(255, 80, 80, 0.9);
  border-color: transparent;
  color: #fff; font-weight: 600;
}
.cf-btn.danger:hover { filter: brightness(1.1); }
.cf-btn.ghost:hover { background: rgba(var(--surface-rgb), 0.08); }
</style>
