<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { api } from '@/api'

interface DepEntry {
  type: 'plugin' | 'skill'
  label: string
  status?: string              // "ok" | "dependency_error" | "load_error"
  load_error?: string | null   // exec_module 失败时的错误信息
  pip: string[]
  pip_installed: string[]
  pip_still_missing: string[]  // 最终仍未满足的 pip 包
  npm_installed: boolean | null
  npm_missing: boolean         // npm 依赖未满足（npm 不可用或 install 失败）
  external: { name: string; url: string; description: string }[]
}

interface DepSnapshot {
  plugins: Record<string, DepEntry>
  skills: Record<string, DepEntry>
  summary: { total: number; with_external: number; with_pip: number; with_npm: number }
}

function statusBadge(s: string | undefined): { cls: string; text: string } | null {
  if (s === 'dependency_error') return { cls: 'st-missing', text: '⚠ 依赖缺失' }
  if (s === 'load_error') return { cls: 'st-error', text: '✗ 加载失败' }
  return null
}

const loading = ref(false)
const error = ref('')
const data = ref<DepSnapshot | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.capabilities.dependencies()
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const refreshLoading = ref(false)
async function reload() {
  refreshLoading.value = true
  try {
    await api.capabilities.reload()
    await load()
  } finally {
    refreshLoading.value = false
  }
}

// 按"插件/技能"两列分别排序（有错误的最前，其次 external 多的，再次 pip 多的）
function sortEntries(map: Record<string, DepEntry> | undefined): { key: string; deps: DepEntry }[] {
  if (!map) return []
  return Object.entries(map)
    .map(([k, v]) => ({ key: k, deps: v }))
    .sort((a, b) => {
      const ae = a.deps.status && a.deps.status !== 'ok' ? 1 : 0
      const be_ = b.deps.status && b.deps.status !== 'ok' ? 1 : 0
      if (be_ !== ae) return be_ - ae
      const be = (b.deps.external?.length || 0) - (a.deps.external?.length || 0)
      if (be) return be
      return (b.deps.pip?.length || 0) - (a.deps.pip?.length || 0)
    })
}

// 两列结构：左列插件 / 右列技能
const columns = computed(() => [
  { id: 'plugins', title: '🧩 插件', cls: 'col-plugins', entries: sortEntries(data.value?.plugins) },
  { id: 'skills', title: '⚡ 技能', cls: 'col-skills', entries: sortEntries(data.value?.skills) },
])

// 有异常（依赖缺失 / 加载失败）的条目总数
const errorCount = computed(() => {
  const all = [...(data.value?.plugins ? Object.values(data.value.plugins) : []),
               ...(data.value?.skills ? Object.values(data.value.skills) : [])]
  return all.filter(e => e.status && e.status !== 'ok').length
})

function pipStatus(spec: string, entry: DepEntry): 'missing' | 'new' | 'ok' {
  if (entry.pip_still_missing?.includes(spec)) return 'missing'
  return entry.pip_installed?.includes(spec) ? 'new' : 'ok'
}

function columnSummary(entries: { key: string; deps: DepEntry }[]) {
  if (!entries.length) return null
  return {
    total: entries.length,
    with_pip: entries.filter(e => e.deps.pip?.length).length,
    with_external: entries.filter(e => e.deps.external?.length).length,
    with_errors: entries.filter(e => e.deps.status && e.deps.status !== 'ok').length,
  }
}
</script>

<template>
  <div class="dep-panel">
    <!-- 头部汇总 -->
    <div class="dep-header">
      <div class="dep-summary" v-if="data">
        <span class="ds-chip">📦 共 {{ data.summary.total }} 项</span>
        <span class="ds-chip pip">🐍 pip {{ data.summary.with_pip }} 项</span>
        <span class="ds-chip npm">📦 npm {{ data.summary.with_npm }} 项</span>
        <span class="ds-chip ext">🔗 外部应用 {{ data.summary.with_external }} 项需下载</span>
        <span v-if="errorCount" class="ds-chip err">⚠ {{ errorCount }} 项异常</span>
      </div>
      <button class="btn sm ghost" @click="reload" :disabled="refreshLoading" :title="'重新扫描 + 自动安装缺失依赖'">
        <span v-if="refreshLoading">⟳ 扫描中…</span>
        <span v-else>⟳ 重新扫描 / 安装依赖</span>
      </button>
    </div>

    <!-- 加载 / 错误 -->
    <div v-if="loading && !data" class="dep-empty">加载中…</div>
    <div v-else-if="error" class="dep-empty err">加载失败：{{ error }}</div>
    <div v-else-if="!data" class="dep-empty">暂无数据</div>

    <!-- 两列布局：左插件 / 右技能 -->
    <div v-else class="dep-cols">
      <section v-for="col in columns" :key="col.id" class="dep-col" :class="col.cls">
        <header class="col-head">
          <h3>{{ col.title }}</h3>
          <span v-if="columnSummary(col.entries)" class="col-summary">
            {{ columnSummary(col.entries)!.total }} 项
            · 🐍 {{ columnSummary(col.entries)!.with_pip }}
            · 🔗 {{ columnSummary(col.entries)!.with_external }}
            <template v-if="columnSummary(col.entries)!.with_errors">
              · ⚠ {{ columnSummary(col.entries)!.with_errors }} 异常
            </template>
          </span>
        </header>

        <div v-if="col.entries.length" class="dep-list">
          <div v-for="item in col.entries" :key="item.key" class="dep-card glass">
            <!-- 卡片头部 -->
            <div class="dc-head">
              <span class="dc-type" :class="item.deps.type === 'plugin' ? 'tp' : 'ts'">
                {{ item.deps.type === 'plugin' ? '🧩 插件' : '⚡ 技能' }}
              </span>
              <span class="dc-label">{{ item.deps.label || item.key }}</span>
              <span class="dc-key mono">{{ item.key }}</span>
              <span v-if="statusBadge(item.deps.status)"
                    class="dc-status"
                    :class="statusBadge(item.deps.status)!.cls"
                    :title="item.deps.load_error || ''">
                {{ statusBadge(item.deps.status)!.text }}
              </span>
            </div>
            <!-- 加载失败原因 -->
            <div v-if="item.deps.load_error" class="dc-load-err mono">
              {{ item.deps.load_error }}
            </div>

            <!-- pip 依赖 -->
            <div class="dc-section">
              <div class="dc-sec-title">🐍 pip 依赖</div>
              <div v-if="item.deps.pip?.length" class="dc-pip-list">
                <div v-for="spec in item.deps.pip" :key="spec" class="pip-item"
                     :class="pipStatus(spec, item.deps)">
                  <span class="pip-spec mono">{{ spec }}</span>
                  <span v-if="pipStatus(spec, item.deps) === 'missing'" class="pip-badge missing">⚠ 缺失</span>
                  <span v-else-if="pipStatus(spec, item.deps) === 'new'" class="pip-badge new">本次新安装</span>
                  <span v-else class="pip-badge ok">已满足</span>
                </div>
              </div>
              <div v-else class="dc-none">— 无 pip 依赖 —</div>
            </div>

            <!-- npm 状态 -->
            <div class="dc-section">
              <div class="dc-sec-title">📦 npm 依赖</div>
              <div v-if="item.deps.npm_installed === null" class="dc-none">— 未声明 —</div>
              <div v-else-if="item.deps.npm_installed" class="npm-badge ok">✓ npm 依赖已就绪</div>
              <div v-else-if="item.deps.npm_missing" class="npm-badge unavail">⚠ npm 不可用或安装失败</div>
              <div v-else class="npm-badge err">✗ npm 依赖安装失败</div>
            </div>

            <!-- external 下载链接 -->
            <div v-if="item.deps.external?.length" class="dc-section dc-ext">
              <div class="dc-sec-title">🔗 外部应用（需手动安装）</div>
              <div v-for="ext in item.deps.external" :key="ext.name" class="ext-item">
                <div class="ext-name">📥 {{ ext.name }}</div>
                <div v-if="ext.description" class="ext-desc">{{ ext.description }}</div>
                <a v-if="ext.url" :href="ext.url" target="_blank" rel="noopener" class="ext-link">
                  下载 → {{ ext.url }}
                </a>
                <div v-else class="ext-no-url">（未提供下载地址）</div>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="dep-empty-col">暂无{{ col.id === 'plugins' ? '插件' : '技能' }}</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dep-panel { display: flex; flex-direction: column; gap: 12px; height: 100%; }
.dep-header {
  display: flex; align-items: center; gap: 12px; justify-content: space-between;
  padding: 12px 16px; border-radius: 10px; flex-shrink: 0;
  background: rgba(var(--surface-rgb), 0.04); border: 1px solid rgba(var(--surface-rgb), 0.08);
}
.dep-summary { display: flex; gap: 8px; flex-wrap: wrap; }
.ds-chip {
  font-size: calc(12px * var(--font-scale)); padding: 3px 10px; border-radius: 999px;
  background: rgba(var(--surface-rgb), 0.08); border: 1px solid rgba(var(--surface-rgb), 0.12);
}
.ds-chip.pip { background: rgba(80, 160, 255, 0.15); color: #8ab4ff; border-color: rgba(80, 160, 255, 0.3); }
.ds-chip.npm { background: rgba(203, 56, 56, 0.15); color: #e09090; border-color: rgba(203, 56, 56, 0.3); }
.ds-chip.ext { background: rgba(100, 200, 160, 0.15); color: #80d8b0; border-color: rgba(100, 200, 160, 0.3); }
.ds-chip.err { background: rgba(220, 100, 100, 0.15); color: #e09090; border-color: rgba(220, 100, 100, 0.3); }
.dep-empty { padding: 40px; text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); }
.dep-empty.err { color: #e08080; opacity: 1; }

/* —— 两列布局 —— */
.dep-cols {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  flex: 1; min-height: 0;
}
@media (max-width: 960px) { .dep-cols { grid-template-columns: 1fr; } }
.dep-col {
  display: flex; flex-direction: column; min-height: 0;
  background: rgba(var(--surface-rgb), 0.02); border: 1px solid rgba(var(--surface-rgb), 0.06);
  border-radius: 10px; padding: 10px; overflow: hidden;
}
.col-plugins { border-color: rgba(140, 180, 255, 0.18); }
.col-skills  { border-color: rgba(200, 150, 255, 0.18); }

.col-head {
  display: flex; align-items: center; gap: 10px; margin-bottom: 8px; padding: 4px 8px;
  flex-shrink: 0; border-radius: 6px;
  background: rgba(var(--surface-rgb), 0.04);
}
.col-head h3 { font-size: calc(13px * var(--font-scale)); margin: 0; }
.col-summary { font-size: calc(11px * var(--font-scale)); opacity: .55; margin-left: auto; }

.dep-list { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; flex: 1; padding-right: 2px; }
.dep-empty-col { padding: 30px; text-align: center; opacity: .45; font-size: calc(12px * var(--font-scale)); }

/* —— 卡片 —— */
.dep-card { padding: 12px 14px; }
.dc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.dc-type {
  font-size: calc(10px * var(--font-scale)); padding: 2px 7px; border-radius: 4px;
  border: 1px solid rgba(var(--surface-rgb), 0.15); background: rgba(var(--surface-rgb), 0.06);
}
.dc-type.tp { color: #8ab4ff; border-color: rgba(140, 180, 255, 0.3); background: rgba(140, 180, 255, 0.1); }
.dc-type.ts { color: #d8a8ff; border-color: rgba(200, 150, 255, 0.3); background: rgba(200, 150, 255, 0.1); }
.dc-label { font-weight: 600; font-size: calc(13px * var(--font-scale)); }
.dc-key { font-size: calc(10px * var(--font-scale)); opacity: .5; }
.dc-status {
  font-size: calc(10px * var(--font-scale)); padding: 2px 7px; border-radius: 4px;
  border: 1px solid; margin-left: auto;
}
.dc-status.st-missing { color: #f0c060; border-color: rgba(220, 180, 60, 0.35); background: rgba(220, 180, 60, 0.1); }
.dc-status.st-error { color: #e09090; border-color: rgba(220, 100, 100, 0.35); background: rgba(220, 100, 100, 0.1); }
.dc-load-err {
  font-size: calc(10px * var(--font-scale)); padding: 5px 8px; border-radius: 5px;
  margin-bottom: 8px; color: #e09090; opacity: .85;
  background: rgba(220, 100, 100, 0.06); border: 1px solid rgba(220, 100, 100, 0.18);
  word-break: break-all; line-height: 1.5;
}
.dc-section { margin-top: 8px; }
.dc-sec-title {
  font-size: calc(10px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px;
  color: rgba(var(--text-rgb), 0.5); margin-bottom: 5px;
}
.dc-none { font-size: calc(11px * var(--font-scale)); opacity: .4; padding: 2px 0; }

.dc-pip-list { display: flex; flex-wrap: wrap; gap: 5px; }
.pip-item {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 5px; font-size: calc(11px * var(--font-scale));
  border: 1px solid rgba(var(--surface-rgb), 0.15); background: rgba(var(--surface-rgb), 0.04);
}
.pip-item.new { border-color: rgba(100, 220, 160, 0.4); background: rgba(100, 220, 160, 0.08); }
.pip-spec { font-size: calc(10px * var(--font-scale)); }
.pip-badge { font-size: calc(9px * var(--font-scale)); padding: 1px 5px; border-radius: 3px; }
.pip-badge.ok { color: #80d8b0; opacity: .7; }
.pip-badge.new { color: #a0ffc8; background: rgba(100, 220, 160, 0.2); }
.pip-badge.missing { color: #f0c060; background: rgba(220, 180, 60, 0.2); }
.pip-item.missing { border-color: rgba(220, 180, 60, 0.4); background: rgba(220, 180, 60, 0.08); }

.npm-badge { display: inline-block; font-size: calc(11px * var(--font-scale)); padding: 3px 8px; border-radius: 5px; }
.npm-badge.ok { color: #80d8b0; background: rgba(100, 220, 160, 0.1); border: 1px solid rgba(100, 220, 160, 0.3); }
.npm-badge.err { color: #e09090; background: rgba(220, 100, 100, 0.1); border: 1px solid rgba(220, 100, 100, 0.3); }
.npm-badge.unavail { color: #f0c060; background: rgba(220, 180, 60, 0.1); border: 1px solid rgba(220, 180, 60, 0.3); }

.dc-ext {
  background: rgba(100, 200, 160, 0.04); border: 1px dashed rgba(100, 200, 160, 0.25);
  border-radius: 7px; padding: 8px 10px; margin-top: 10px;
}
.ext-item { padding: 5px 0; border-bottom: 1px dashed rgba(var(--surface-rgb), 0.08); }
.ext-item:last-child { border-bottom: none; }
.ext-name { font-weight: 600; font-size: calc(12px * var(--font-scale)); }
.ext-desc { font-size: calc(11px * var(--font-scale)); opacity: .75; margin: 2px 0 3px; }
.ext-link {
  display: inline-block; color: var(--accent); text-decoration: none; font-size: calc(11px * var(--font-scale));
  padding: 3px 8px; border-radius: 5px; background: rgba(var(--accent-rgb), 0.12); border: 1px solid rgba(var(--accent-rgb), 0.25);
  transition: background .15s; word-break: break-all;
}
.ext-link:hover { background: rgba(var(--accent-rgb), 0.22); }
.ext-no-url { font-size: calc(10px * var(--font-scale)); opacity: .4; font-style: italic; }
</style>
