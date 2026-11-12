<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { usePermissionStore } from '@/stores/permission'

/**
 * 危险操作手动确认弹窗（全局挂载，见 App.vue）。
 * 设置 → 权限设置 → 操作权限 = 「手动确认」时，智能体每次执行危险操作
 * （写/删文件、运行命令、派生子智能体、插件…）都会先弹这里等用户拍板：
 * 允许 → 后端继续执行；拒绝 / 超时(120s) → 工具返回「用户拒绝执行」错误。
 */
const store = usePermissionStore()

const current = computed(() => store.pending[0] || null)
const extra = computed(() => Math.max(0, store.pending.length - 1))

const now = ref(Date.now())
let timer: number | null = null
onMounted(() => {
  // 本地倒计时：到点说明后端已按「拒绝」超时收场，弹窗自行消失
  timer = window.setInterval(() => {
    now.value = Date.now()
    const r = current.value
    if (r && left.value <= 0) store.expire(r.id)
  }, 500)
  store.hydrate()
})
onBeforeUnmount(() => { if (timer != null) window.clearInterval(timer) })

/** 请求到达时刻 + 后端超时 = 本地倒计时截止点（到点后端会自行按拒绝收场） */
const deadline = computed(() => {
  const r = current.value
  if (!r) return 0
  const wait = Number(r.remaining ?? r.timeout ?? 120)
  return (r.ts || Date.now()) + Math.max(1, wait) * 1000
})
const left = computed(() => Math.max(0, Math.ceil((deadline.value - now.value) / 1000)))

const argsText = computed(() => {
  const r = current.value
  if (!r || r.args == null) return ''
  try {
    const t = JSON.stringify(r.args, null, 2)
    return t && t !== '{}' ? t : ''
  } catch { return String(r.args) }
})

function decide(approve: boolean) {
  const r = current.value
  if (!r) return
  store.decide(r.id, approve)
}
function onKey(e: KeyboardEvent) {
  if (!current.value) return
  if (e.key === 'Escape') { e.preventDefault(); decide(false) }
}
onMounted(() => window.addEventListener('keydown', onKey, true))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey, true))
</script>

<template>
  <div v-if="current" class="perm-mask" @click.self="decide(false)">
    <div class="perm-card" role="dialog" aria-modal="true" aria-label="危险操作确认">
      <div class="perm-head">
        <span class="perm-badge">🛡 危险操作确认</span>
        <span class="perm-left" :class="{ warn: left <= 15 }">{{ left }}s</span>
      </div>

      <div class="perm-body">
        <div class="perm-title">
          <span class="perm-label">{{ current.label || current.tool }}</span>
          <span class="perm-tool mono">{{ current.tool }}</span>
        </div>
        <div class="perm-sub">
          智能体正请求执行该操作（手动确认模式）。允许后本次立即执行；拒绝则该调用以错误返回给智能体。
        </div>

        <pre v-if="argsText" class="perm-args mono">{{ argsText }}</pre>

        <div class="perm-foot-note">
          超过 {{ left }} 秒未处理将自动按「拒绝」处理。
          <template v-if="extra > 0">还有 {{ extra }} 个待确认请求排队中。</template>
        </div>
      </div>

      <div class="perm-actions">
        <button class="perm-btn ghost" @click="decide(false)">拒绝</button>
        <button class="perm-btn primary" @click="decide(true)">允许本次执行</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.perm-mask {
  position: fixed;
  inset: 0;
  z-index: 4000;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.perm-card {
  width: min(560px, 100%);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--panel-bg, #0a0a0a);
  color: var(--text, #ececec);
  border: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.14);
  border-radius: 14px;
  box-shadow: var(--popup-shadow, 0 12px 40px rgba(0, 0, 0, 0.5));
  overflow: hidden;
}
.perm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.1);
}
.perm-badge {
  font-weight: 600;
  font-size: 14px;
  color: var(--red-ink, #ff9090);
}
.perm-left {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--text-dim, rgba(236, 236, 236, 0.72));
  padding: 2px 8px;
  border: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.16);
  border-radius: 999px;
}
.perm-left.warn { color: var(--red-ink, #ff9090); border-color: var(--red-ink, #ff9090); }
.perm-body { padding: 16px 18px; overflow: auto; }
.perm-title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.perm-label { font-size: 16px; font-weight: 600; }
.perm-tool { font-size: 12px; color: var(--text-dim, rgba(236, 236, 236, 0.72)); }
.perm-sub {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-dim, rgba(236, 236, 236, 0.72));
}
.perm-args {
  margin: 12px 0 0;
  padding: 10px 12px;
  max-height: 260px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--tint-soft-bg, rgba(255, 255, 255, 0.045));
  border: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.08);
  border-radius: 8px;
}
.perm-foot-note {
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-dim, rgba(236, 236, 236, 0.6));
}
.perm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 18px;
  border-top: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.1);
}
.perm-btn {
  padding: 9px 18px;
  font-size: 13px;
  border-radius: 9px;
  cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb, 255, 255, 255), 0.18);
  background: transparent;
  color: var(--text, #ececec);
  transition: filter 0.15s ease, background 0.15s ease;
}
.perm-btn:hover { filter: brightness(1.15); }
.perm-btn.primary {
  background: rgb(var(--accent-rgb, 110, 168, 255));
  border-color: transparent;
  color: #0b1220;
  font-weight: 600;
}
.perm-btn.ghost:hover { background: rgba(var(--surface-rgb, 255, 255, 255), 0.08); }
.mono { font-family: Consolas, "JetBrains Mono", Menlo, monospace; }
</style>
