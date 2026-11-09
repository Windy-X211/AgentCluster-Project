<script setup lang="ts">
/**
 * 记忆管理面板（能力管理 → 🧠 记忆管理 Tab）
 *
 * 记忆条目**所有智能体共用一份**（后端 memories/shared.json）：
 * - 全局总开关（关闭后所有智能体立即失去记忆能力，记忆内容保留）
 * - 每个智能体的记忆模式：关闭 / 手动记忆 / 摘要记忆（决定各智能体如何参与共享记忆）
 *   · 手动记忆：仅当用户主动提出记入记忆时，智能体才写入
 *   · 摘要记忆：每轮对话后系统自动整理滚动摘要；用户主动要求的内容进固定记忆、永不遗忘
 * - 「记忆内容」区（默认开启展示）：查看 / 编辑 / 添加 / 删除 / 清空共享记忆
 * - 批量管理（默认收起）：勾选多个智能体一键调整记忆模式
 */
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api'

interface MemEntry { id: string; content: string; pinned: boolean; source: string; ts: string }
interface AgentRow {
  agent_id: number; name: string; avatar_color?: string | null
  configured_mode: string; mode: string; global_enabled: boolean
}
interface SharedDetail {
  summary_text: string
  pinned_entries: MemEntry[]; other_entries: MemEntry[]
  updated_at?: string | null
}

const rows = ref<AgentRow[]>([])
const loading = ref(true)
const globalEnabled = ref(true)
const selected = ref<Set<number>>(new Set())
const batchOpen = ref(false)  // 批量管理抽屉：初始收起，勾选状态收起时保留
// —— 共享记忆内容（查看区，默认开启展示） ——
const shared = ref<SharedDetail | null>(null)
const sharedLoading = ref(false)
const summaryDraft = ref('')
const summaryDirty = ref(false)
const newEntryText = ref('')
const newEntryPinned = ref(false)
const busy = ref(false)
const err = ref('')

const selectedIds = computed(() => [...selected.value])
const hasSelection = computed(() => selected.value.size > 0)
const allChecked = computed(() => rows.value.length > 0 && selected.value.size === rows.value.length)

function fmtTime(ts?: string | null): string {
  if (!ts) return '—'
  try { return ts.slice(0, 16).replace('T', ' ') } catch { return '—' }
}

function modeLabel(m: string): string {
  return m === 'manual' ? '手动记忆' : m === 'summary' ? '摘要记忆' : '关闭'
}

function avatarColor(row: AgentRow): string {
  return row.avatar_color || 'rgba(var(--accent-rgb), 0.55)'
}

async function loadShared() {
  sharedLoading.value = true
  try {
    const d = await api.memory.shared()
    shared.value = d
    summaryDraft.value = d.summary_text || ''
    summaryDirty.value = false
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { sharedLoading.value = false }
}

async function refresh() {
  err.value = ''
  try {
    rows.value = (await api.memory.overview()) || []
    if (rows.value.length) globalEnabled.value = rows.value[0].global_enabled !== false
    // 刷新后剔除已不存在的选中项
    const ids = new Set(rows.value.map(r => r.agent_id))
    for (const sid of [...selected.value]) if (!ids.has(sid)) selected.value.delete(sid)
    await loadShared()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function toggleGlobal() {
  const v = !globalEnabled.value
  busy.value = true
  try {
    const r = await api.memory.setGlobal(v)
    globalEnabled.value = r?.enabled !== false
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

async function setMode(row: AgentRow, mode: string) {
  if (row.configured_mode === mode) return
  busy.value = true
  try {
    await api.memory.setMode(row.agent_id, mode)
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

function toggleChecked(row: AgentRow) {
  if (selected.value.has(row.agent_id)) selected.value.delete(row.agent_id)
  else selected.value.add(row.agent_id)
  selected.value = new Set(selected.value)  // 触发响应式
}

function toggleAll() {
  selected.value = allChecked.value ? new Set() : new Set(rows.value.map(r => r.agent_id))
}

async function batchApply(mode: string) {
  if (!hasSelection.value || busy.value) return
  busy.value = true
  try {
    await api.memory.batchMode(selectedIds.value, mode)
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

// —— 共享记忆内容操作 ——
async function addEntry() {
  if (!newEntryText.value.trim() || busy.value) return
  busy.value = true
  try {
    await api.memory.addEntry(newEntryText.value.trim(), newEntryPinned.value)
    newEntryText.value = ''; newEntryPinned.value = false
    await loadShared()
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

async function delEntry(e: MemEntry) {
  if (busy.value) return
  if (!window.confirm(e.pinned ? '删除这条固定记忆？（固定记忆为用户主动要求保存的内容）' : '删除这条记忆？')) return
  busy.value = true
  try {
    await api.memory.deleteEntry(e.id)
    await loadShared()
    await refresh()
  } catch (ex: any) {
    err.value = ex?.message || String(ex)
  } finally { busy.value = false }
}

async function togglePin(e: MemEntry) {
  if (busy.value) return
  busy.value = true
  try {
    await api.memory.updateEntry(e.id, { pinned: !e.pinned })
    await loadShared()
    await refresh()
  } catch (ex: any) {
    err.value = ex?.message || String(ex)
  } finally { busy.value = false }
}

async function saveSummary() {
  if (busy.value) return
  busy.value = true
  try {
    await api.memory.updateSummary(summaryDraft.value)
    summaryDirty.value = false
    await loadShared()
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

async function clearMem(keepPinned: boolean) {
  if (busy.value) return
  const msg = keepPinned
    ? '清空摘要记忆与普通记忆条目？（固定记忆保留）'
    : '清空全部记忆（含固定记忆），不可恢复？'
  if (!window.confirm(msg)) return
  busy.value = true
  try {
    await api.memory.clear(keepPinned)
    await loadShared()
    await refresh()
  } catch (e: any) {
    err.value = e?.message || String(e)
  } finally { busy.value = false }
}

onMounted(refresh)
</script>

<template>
  <!-- 面板形态：填满能力管理 .body 区域 -->
  <div class="mm-panel">
    <!-- 全局开关 -->
    <div class="mm-global glass">
      <div class="mm-global-text">
        <span class="mm-global-title">记忆功能总开关</span>
        <span class="mm-global-desc">关闭后所有智能体立即失去记忆能力（读写均不可用），已保存的记忆内容保留，重新开启即恢复。</span>
      </div>
      <label class="mm-switch" :class="{ on: globalEnabled }">
        <input type="checkbox" :checked="globalEnabled" :disabled="busy" @change="toggleGlobal">
        <span class="mm-slider"></span>
      </label>
    </div>

    <!-- 模式说明 -->
    <div class="mm-modes glass">
      <div class="mm-mode-item"><span class="mm-mode-icon">✋</span><b>手动记忆</b><span class="mm-mode-desc">仅当用户主动提出记入记忆时，智能体才写入记忆</span></div>
      <div class="mm-mode-item"><span class="mm-mode-icon">📝</span><b>摘要记忆</b><span class="mm-mode-desc">智能体根据会话内容自动写入并整理成摘要（每次写入重新整理，避免杂乱冗长）；用户主动要求记住的内容进固定记忆、永不遗忘</span></div>
    </div>

    <!-- 记忆内容（所有智能体共用一份，默认开启展示） -->
    <div class="mm-view glass">
      <div class="mm-view-head">
        <span class="mm-view-title">🧠 记忆内容</span>
        <span class="mm-view-sub">所有智能体共用 · 更新于 {{ fmtTime(shared?.updated_at) }}</span>
        <div class="mm-view-actions">
          <button class="btn small" :disabled="busy" @click="clearMem(true)">清空摘要与普通记忆</button>
          <button class="btn small danger" :disabled="busy" @click="clearMem(false)">清空全部</button>
        </div>
      </div>
      <div class="mm-view-body">
        <div v-if="loading || sharedLoading" class="mm-hint">加载中…</div>
        <template v-else-if="shared">
          <!-- 固定记忆 -->
          <div class="mm-sec">
            <div class="mm-sec-title">📌 固定记忆（用户主动要求记住，永不遗忘）</div>
            <div v-if="!shared.pinned_entries.length" class="mm-empty">暂无固定记忆</div>
            <div v-for="e in shared.pinned_entries" :key="e.id" class="mm-entry pinned">
              <span class="mm-entry-content">{{ e.content }}</span>
              <span class="mm-entry-ts">{{ fmtTime(e.ts) }}</span>
              <button class="mm-entry-btn" :disabled="busy" title="取消固定" @click="togglePin(e)">取消固定</button>
              <button class="mm-entry-btn danger" :disabled="busy" title="删除" @click="delEntry(e)">删除</button>
            </div>
          </div>

          <!-- 摘要记忆 -->
          <div class="mm-sec">
            <div class="mm-sec-title">📝 摘要记忆（摘要记忆模式下每轮自动整理）</div>
            <textarea v-model="summaryDraft" class="mm-summary" rows="4" placeholder="暂无摘要" @input="summaryDirty = true"></textarea>
            <div class="mm-summary-actions">
              <button class="btn small" :disabled="busy || !summaryDirty" @click="saveSummary">保存摘要</button>
            </div>
          </div>

          <!-- 普通条目 -->
          <div class="mm-sec">
            <div class="mm-sec-title">🗂 其他记忆条目（{{ shared.other_entries.length }}）</div>
            <div v-if="!shared.other_entries.length" class="mm-empty">暂无</div>
            <div v-for="e in shared.other_entries" :key="e.id" class="mm-entry">
              <span class="mm-entry-content">{{ e.content }}</span>
              <span class="mm-entry-ts">{{ fmtTime(e.ts) }}</span>
              <button class="mm-entry-btn" :disabled="busy" title="设为固定记忆" @click="togglePin(e)">固定</button>
              <button class="mm-entry-btn danger" :disabled="busy" title="删除" @click="delEntry(e)">删除</button>
            </div>
          </div>

          <!-- 手动添加 -->
          <div class="mm-sec">
            <div class="mm-sec-title">➕ 添加记忆</div>
            <div class="mm-add-row">
              <input v-model="newEntryText" class="mm-add-input" placeholder="输入要记入记忆的内容…" @keyup.enter="addEntry">
              <label class="mm-add-pin"><input v-model="newEntryPinned" type="checkbox"> 固定</label>
              <button class="btn small" :disabled="busy || !newEntryText.trim()" @click="addEntry">添加</button>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div v-if="err" class="mm-err">⚠️ {{ err }}</div>

    <!-- 批量管理（可伸缩抽屉：收起只留标题行，展开后拉出全部智能体列表） -->
    <div class="mm-batch glass" :class="{ open: batchOpen }">
      <button class="mm-batch-head" @click="batchOpen = !batchOpen">
        <span class="mm-batch-arrow" :class="{ open: batchOpen }">
          <svg width="10" height="10" viewBox="0 0 10 10"><path d="M3 1.5 L7 5 L3 8.5" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <span class="mm-batch-title">批量管理</span>
        <span v-if="selected.size" class="mm-batch-info">已选 {{ selected.size }} 个</span>
        <span v-else class="mm-batch-hint">展开后显示智能体列表，可勾选批量调整</span>
        <span v-if="rows.length" class="mm-batch-count">{{ rows.length }} 个智能体</span>
      </button>
      <div v-show="batchOpen" class="mm-batch-body">
        <div class="mm-batch-tools">
          <label class="mm-checkall">
            <input type="checkbox" :checked="allChecked" @change="toggleAll">
            全选（{{ selected.size }}/{{ rows.length }}）
          </label>
          <div class="mm-batch-btns">
            <button class="btn small" :disabled="!hasSelection || busy || !globalEnabled" @click="batchApply('summary')">批量开启摘要记忆</button>
            <button class="btn small" :disabled="!hasSelection || busy || !globalEnabled" @click="batchApply('manual')">批量开启手动记忆</button>
            <button class="btn small danger" :disabled="!hasSelection || busy" @click="batchApply('off')">批量关闭记忆</button>
          </div>
        </div>

        <!-- 智能体列表（模式开关；记忆内容共用，见上方「记忆内容」区） -->
        <div class="mm-list">
          <div v-if="loading" class="mm-hint">加载中…</div>
          <div v-else-if="!rows.length" class="mm-hint">还没有任何智能体</div>
          <div v-for="row in rows" :key="row.agent_id" class="mm-card glass">
            <div class="mm-card-main">
              <input class="mm-check" type="checkbox" :checked="selected.has(row.agent_id)" @change="toggleChecked(row)">
              <span class="mm-avatar" :style="{ background: avatarColor(row) }">{{ row.name.slice(0, 1) }}</span>
              <span class="mm-name-text" :title="row.name">{{ row.name }}</span>
              <div class="mm-seg" :class="{ disabled: !globalEnabled || busy }">
                <button :class="{ active: row.configured_mode === 'off' }" @click="setMode(row, 'off')">关闭</button>
                <button :class="{ active: row.configured_mode === 'manual' }" @click="setMode(row, 'manual')">手动记忆</button>
                <button :class="{ active: row.configured_mode === 'summary' }" @click="setMode(row, 'summary')">摘要记忆</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mm-panel {
  height: 100%; min-height: 0;
  display: flex; flex-direction: column; gap: 10px;
  /* 整体可滚动：窗口偏矮时各区块保持最小高度、把面板撑出滚动条，不压缩裁剪 */
  overflow-y: auto; overflow-x: hidden; padding: 2px;
}
.mm-panel::-webkit-scrollbar { width: 8px; }
.mm-panel::-webkit-scrollbar-thumb { background: rgba(var(--text-rgb), .15); border-radius: 4px; }

.glass { border-radius: 12px; }

/* —— 全局开关 —— */
.mm-global { flex: none; display: flex; align-items: center; gap: 14px; padding: 10px 14px; }
.mm-global-text { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.mm-global-title { font-size: calc(13.5px * var(--font-scale)); }
.mm-global-desc { font-size: calc(11.5px * var(--font-scale)); opacity: .55; }
.mm-switch { position: relative; width: 44px; height: 24px; flex: none; cursor: pointer; }
.mm-switch input { opacity: 0; width: 0; height: 0; }
.mm-slider {
  position: absolute; inset: 0; border-radius: 12px;
  background: rgba(var(--shadow-rgb), 0.35); transition: all .2s;
}
.mm-slider::before {
  content: ''; position: absolute; left: 3px; top: 3px;
  width: 18px; height: 18px; border-radius: 50%;
  background: #fff; transition: all .2s; box-shadow: 0 1px 4px rgba(var(--shadow-rgb), .3);
}
.mm-switch.on .mm-slider { background: rgb(var(--accent-rgb)); }
.mm-switch.on .mm-slider::before { transform: translateX(20px); }

/* —— 模式说明 —— */
.mm-modes { flex: none; display: flex; gap: 14px; padding: 8px 14px; }
.mm-mode-item { flex: 1; display: flex; align-items: baseline; gap: 6px; font-size: calc(12px * var(--font-scale)); min-width: 0; }
.mm-mode-item b { white-space: nowrap; }
.mm-mode-desc { opacity: .55; }
.mm-mode-icon { flex: none; }

/* —— 记忆内容（共享，默认开启展示；不内部滚动，随面板整体滚动） —— */
.mm-view { flex: none; padding: 0; display: flex; flex-direction: column; }
.mm-view-head {
  flex: none; display: flex; align-items: center; gap: 10px;
  padding: 8px 14px;
  border-bottom: 1px solid rgba(var(--accent-rgb), .12);
}
.mm-view-title { font-weight: 600; font-size: calc(12.5px * var(--font-scale)); white-space: nowrap; }
.mm-view-sub { flex: 1; min-width: 0; font-size: calc(11.5px * var(--font-scale)); opacity: .5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mm-view-actions { display: flex; gap: 8px; flex: none; }
.mm-view-body { padding: 10px 14px; display: flex; flex-direction: column; gap: 10px; }

/* —— 批量管理（可伸缩抽屉；展开后内容自然撑高、随面板整体滚动） —— */
.mm-batch { flex: none; padding: 0; overflow: hidden; display: flex; flex-direction: column; }
.mm-batch.open { flex: none; }
.mm-batch-head {
  flex: none;
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 14px; border: none; background: transparent;
  color: rgb(var(--text-rgb)); cursor: pointer; text-align: left;
  font-size: calc(12.5px * var(--font-scale));
  transition: background .15s;
}
.mm-batch-head:hover { background: rgba(var(--accent-rgb), .08); }
.mm-batch-arrow { display: flex; align-items: center; opacity: .6; transition: transform .18s; flex: none; }
.mm-batch-arrow.open { transform: rotate(90deg); }
.mm-batch-title { font-weight: 600; white-space: nowrap; }
.mm-batch-info { color: var(--accent-light); font-size: calc(11.5px * var(--font-scale)); white-space: nowrap; }
.mm-batch-hint { opacity: .5; font-size: calc(11.5px * var(--font-scale)); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mm-batch-count { margin-left: auto; opacity: .55; font-size: calc(11.5px * var(--font-scale)); white-space: nowrap; }
.mm-batch-body { display: flex; flex-direction: column; padding: 0 10px 10px; }
.mm-batch-tools { flex: none; display: flex; align-items: center; gap: 12px; padding: 0 4px 8px; flex-wrap: wrap; }
.mm-checkall { display: flex; align-items: center; gap: 6px; font-size: calc(12.5px * var(--font-scale)); cursor: pointer; }
.mm-batch-btns { display: flex; gap: 8px; flex: 1; justify-content: flex-end; flex-wrap: wrap; }

.btn {
  padding: 5px 12px; border-radius: 8px; border: 1px solid rgba(var(--accent-rgb), .3);
  background: rgba(var(--accent-rgb), .12); color: var(--accent-light);
  cursor: pointer; font-size: calc(12.5px * var(--font-scale)); transition: all .15s;
}
.btn:hover:not(:disabled) { background: rgba(var(--accent-rgb), .22); }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn.small { padding: 4px 10px; font-size: calc(12px * var(--font-scale)); }
.btn.danger { border-color: rgba(255, 90, 90, .35); background: rgba(255, 90, 90, .1); color: var(--red-ink); }
.btn.danger:hover:not(:disabled) { background: rgba(255, 90, 90, .2); }

.mm-err { flex: none; padding: 6px 12px; border-radius: 8px; background: rgba(255, 90, 90, .12); color: var(--red-ink); font-size: calc(12px * var(--font-scale)); }

/* —— 智能体列表 —— */
.mm-list { display: flex; flex-direction: column; gap: 8px; padding-right: 2px; }
.mm-hint { text-align: center; opacity: .5; padding: 24px 0; font-size: calc(13px * var(--font-scale)); }
.mm-card { flex: none; overflow: hidden; }
.mm-card-main { display: flex; align-items: center; gap: 10px; padding: 8px 12px; }
.mm-check { flex: none; cursor: pointer; }
.mm-avatar {
  flex: none; width: 26px; height: 26px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: calc(13px * var(--font-scale));
}
.mm-name-text { font-size: calc(13.5px * var(--font-scale)); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }

.mm-seg { display: flex; flex: none; border: 1px solid rgba(var(--accent-rgb), .25); border-radius: 8px; overflow: hidden; }
.mm-seg.disabled { opacity: .5; pointer-events: none; }
.mm-seg button {
  border: none; background: transparent; padding: 4px 10px; cursor: pointer;
  font-size: calc(12px * var(--font-scale)); color: rgb(var(--text-rgb)); opacity: .65;
  border-right: 1px solid rgba(var(--accent-rgb), .18);
  transition: all .15s; white-space: nowrap;
}
.mm-seg button:last-child { border-right: none; }
.mm-seg button:hover { opacity: 1; background: rgba(var(--accent-rgb), .1); }
.mm-seg button.active { background: rgba(var(--accent-rgb), .2); color: var(--accent-light); opacity: 1; }

/* —— 记忆条目 —— */
.mm-sec { display: flex; flex-direction: column; gap: 5px; }
.mm-sec-title { font-size: calc(12px * var(--font-scale)); opacity: .65; }
.mm-empty { font-size: calc(12px * var(--font-scale)); opacity: .4; padding: 2px 0 4px; }

.mm-entry {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 8px;
  background: rgba(var(--surface-rgb), 0.08);
  border: 1px solid rgba(var(--accent-rgb), .12);
}
.mm-entry.pinned { border-color: rgba(255, 190, 70, .3); background: rgba(255, 190, 70, .07); }
.mm-entry-content { flex: 1; min-width: 0; font-size: calc(12.5px * var(--font-scale)); word-break: break-all; }
.mm-entry-ts { flex: none; font-size: calc(10.5px * var(--font-scale)); opacity: .4; white-space: nowrap; }
.mm-entry-btn {
  flex: none; border: none; background: transparent; cursor: pointer;
  font-size: calc(11px * var(--font-scale)); color: var(--accent-light); opacity: .75;
  padding: 2px 6px; border-radius: 5px;
}
.mm-entry-btn:hover:not(:disabled) { opacity: 1; background: rgba(var(--accent-rgb), .15); }
.mm-entry-btn.danger { color: var(--red-ink); }
.mm-entry-btn.danger:hover:not(:disabled) { background: rgba(255, 90, 90, .15); }

.mm-summary {
  width: 100%; resize: vertical; min-height: 60px;
  padding: 8px 10px; border-radius: 8px;
  border: 1px solid rgba(var(--accent-rgb), .2);
  background: var(--tint-input-bg);
  color: rgb(var(--text-rgb));
  font-size: calc(12.5px * var(--font-scale)); font-family: inherit;
  line-height: 1.5;
}
.mm-summary:focus { outline: none; border-color: rgba(var(--accent-rgb), .5); }
.mm-summary-actions { display: flex; justify-content: flex-end; }

.mm-add-row { display: flex; align-items: center; gap: 8px; }
.mm-add-input {
  flex: 1; min-width: 0; padding: 6px 10px; border-radius: 8px;
  border: 1px solid rgba(var(--accent-rgb), .2);
  background: var(--tint-input-bg); color: rgb(var(--text-rgb));
  font-size: calc(12.5px * var(--font-scale));
}
.mm-add-input:focus { outline: none; border-color: rgba(var(--accent-rgb), .5); }
.mm-add-pin { display: flex; align-items: center; gap: 4px; font-size: calc(12px * var(--font-scale)); opacity: .7; cursor: pointer; white-space: nowrap; }

/* 滚动条：仅面板整体一条（mm-panel） */
.mm-panel::-webkit-scrollbar-thumb { background: rgba(var(--text-rgb), .15); border-radius: 4px; }
</style>