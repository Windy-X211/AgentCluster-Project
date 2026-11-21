<script setup lang="ts">
import { computed, ref } from 'vue'
import { usePluginDebugStore } from '@/stores'

const store = usePluginDebugStore()
const copied = ref(false)

const text = computed(() => {
  const r = store.result
  if (!r) return ''
  if (r.ok) {
    const d = r.data
    if (d === null || d === undefined) return '（无返回数据）'
    if (typeof d === 'string') return d
    try {
      return JSON.stringify(d, null, 2)
    } catch {
      return String(d)
    }
  }
  return r.error || '执行失败'
})

async function copy() {
  if (!text.value) return
  try {
    await navigator.clipboard.writeText(text.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    /* 剪贴板不可用时静默忽略 */
  }
}
</script>

<template>
  <div class="result-panel">
    <div v-if="!store.result" class="empty">尚未执行，结果将展示在此处</div>
    <template v-else>
      <div class="result-head">
        <span class="badge" :class="store.result.ok ? 'ok' : 'err'">
          {{ store.result.ok ? '成功' : '失败' }}
        </span>
        <span class="ms">{{ store.result.duration_ms }} ms</span>
        <button class="btn ghost sm" @click="copy">{{ copied ? '已复制' : '复制' }}</button>
      </div>
      <pre class="result-body" :class="store.result.ok ? 'ok' : 'err'">{{ text }}</pre>
    </template>
  </div>
</template>

<style scoped>
.result-panel { display: flex; flex-direction: column; min-height: 0; }
.result-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.badge { font-size: calc(11px * var(--font-scale)); padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge.ok { background: color-mix(in srgb, var(--ok-ink, #c8f5e0) 18%, transparent); color: var(--ok-ink, #c8f5e0); border: 1px solid color-mix(in srgb, var(--ok-ink, #c8f5e0) 42%, transparent); }
.badge.err { background: color-mix(in srgb, var(--err-ink, #ff9a9a) 18%, transparent); color: var(--err-ink, #ff9a9a); border: 1px solid color-mix(in srgb, var(--err-ink, #ff9a9a) 42%, transparent); }
.ms { font-size: calc(11px * var(--font-scale)); opacity: .55; }
.sm { padding: 2px 10px; font-size: calc(11px * var(--font-scale)); margin-left: auto; }
.result-body {
  margin: 0; padding: 12px; border-radius: 10px; font-family: Consolas, monospace;
  font-size: calc(12px * var(--font-scale)); line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  max-height: 260px; overflow: auto;
}
/* 执行结果用「墨色 + 同色淡底」，浅色主题下自动变深绿/深红，不再出现浅粉浅绿看不清 */
.result-body.ok {
  background: color-mix(in srgb, var(--ok-ink, #c8f5e0) 9%, transparent);
  border: 1px solid color-mix(in srgb, var(--ok-ink, #c8f5e0) 30%, transparent);
  color: var(--ok-ink, #c8f5e0);
}
.result-body.err {
  background: color-mix(in srgb, var(--err-ink, #ff9a9a) 9%, transparent);
  border: 1px solid color-mix(in srgb, var(--err-ink, #ff9a9a) 32%, transparent);
  color: var(--err-ink, #ff9a9a);
}
.empty { padding: 24px; text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); }
</style>