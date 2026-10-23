<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useSettingsStore } from '@/stores'

const settings = useSettingsStore()

// 默认能力的本地镜像
const defaults = ref<{ commands: string[]; skills: string[]; plugins: string[] }>({
  commands: [], skills: [], plugins: [],
})
const maxRounds = ref<number>(50)

// 能力选择器
const pickerOpen = ref<string | null>(null)
const selectedKeys = ref<Set<string>>(new Set())
const collapsedGroups = ref<Set<string>>(new Set())

// 刷新 & toast
const refreshLoading = ref(false)
const toast = ref<{ msg: string; kind: 'ok' | 'err' } | null>(null)
let _toastTimer: number | null = null
function showToast(msg: string, kind: 'ok' | 'err' = 'ok') {
  toast.value = { msg, kind }
  if (_toastTimer) clearTimeout(_toastTimer)
  _toastTimer = window.setTimeout(() => { toast.value = null }, 2500)
}

async function reloadCapabilities() {
  if (refreshLoading.value) return
  refreshLoading.value = true
  try {
    await settings.reloadCapabilities()
    showToast(`已刷新：${Object.keys(settings.commandsMeta).length} 命令 · ${Object.keys(settings.skillsMeta).length} 技能 · ${Object.keys(settings.pluginsMeta).length} 插件`)
  } catch (e: any) {
    showToast(`刷新失败：${e?.message || e}`, 'err')
  } finally {
    refreshLoading.value = false
  }
}

onMounted(async () => {
  await settings.fetch()
  defaults.value.commands = [...(settings.data.default_commands || [])]
  defaults.value.skills   = [...(settings.data.default_skills   || [])]
  defaults.value.plugins  = [...(settings.data.default_plugins  || [])]
  maxRounds.value = Number(settings.data.max_tool_rounds) || 50
})

const availableByType = (type: string) => {
  const source = type === 'commands' ? settings.commandsMeta
               : type === 'skills'   ? settings.skillsMeta
                                     : settings.pluginsMeta
  const added = new Set((defaults.value[type as keyof typeof defaults.value] || []) as string[])
  return Object.entries(source as Record<string, any>)
    .filter(([k, v]) => !added.has(k) && !(type === 'commands' && (v as any).hidden))
    .map(([k, v]) => ({
      key: k,
      label: v.label || k,
      desc: v.description || v.desc || '',
      extra: type === 'skills' ? (v.version ? `v${v.version}` : '') + (v.triggers?.length ? ` · ${v.triggers.join('/')}` : '') : '',
    }))
}
const currentPickerItems = computed(() => pickerOpen.value ? availableByType(pickerOpen.value) : [])

const currentPickerGroups = computed(() => {
  if (!pickerOpen.value || pickerOpen.value !== 'commands') return null
  const items = availableByType('commands')
  const byGroup = new Map<string, any[]>()
  for (const it of items) {
    const g = (settings.commandsMeta[it.key] || {}).group || '其他'
    if (!byGroup.has(g)) byGroup.set(g, [])
    byGroup.get(g)!.push(it)
  }
  const order = [...(settings.commandGroups || [])]
  for (const g of byGroup.keys()) if (!order.includes(g)) order.push(g)
  return order.filter(g => byGroup.has(g)).map(g => ({ name: g, items: byGroup.get(g)! }))
})
function toggleGroupExpand(name: string) {
  const s = new Set(collapsedGroups.value)
  if (s.has(name)) s.delete(name)
  else s.add(name)
  collapsedGroups.value = s
}
function isGroupSelected(group: { name: string; items: any[] }) {
  return group.items.length > 0 && group.items.every(it => selectedKeys.value.has(it.key))
}
function isGroupIndeterminate(group: { name: string; items: any[] }) {
  const cnt = group.items.filter(it => selectedKeys.value.has(it.key)).length
  return cnt > 0 && cnt < group.items.length
}
function toggleGroup(group: { name: string; items: any[] }) {
  if (isGroupSelected(group)) {
    group.items.forEach(it => selectedKeys.value.delete(it.key))
  } else {
    group.items.forEach(it => selectedKeys.value.add(it.key))
  }
}

function pickerTitle(type: string) {
  return type === 'commands' ? '选择默认命令' : type === 'skills' ? '选择默认技能' : '选择默认插件'
}
function openPicker(type: string) {
  selectedKeys.value = new Set()
  pickerOpen.value = type
  collapsedGroups.value = type === 'commands' ? new Set(settings.commandGroups || []) : new Set()
}
function closePicker() { pickerOpen.value = null; selectedKeys.value = new Set() }
function togglePick(key: string) {
  if (selectedKeys.value.has(key)) selectedKeys.value.delete(key)
  else selectedKeys.value.add(key)
}
function confirmAdd() {
  if (!pickerOpen.value) return
  const list: string[] = (defaults.value[pickerOpen.value as keyof typeof defaults.value] as string[]) || []
  selectedKeys.value.forEach(k => list.push(k))
  defaults.value[pickerOpen.value as keyof typeof defaults.value] = list
  persistDefaults()
  closePicker()
}
function removeDefault(type: 'commands' | 'skills' | 'plugins', key: string) {
  defaults.value[type] = defaults.value[type].filter(k => k !== key)
  persistDefaults()
}
function persistDefaults() {
  settings.update({
    default_commands: [...defaults.value.commands],
    default_skills:   [...defaults.value.skills],
    default_plugins:  [...defaults.value.plugins],
  })
}
function persistMaxRounds() {
  const v = Math.max(1, Math.floor(Number(maxRounds.value) || 50))
  maxRounds.value = v
  settings.update({ max_tool_rounds: v })
}

function displayLabel(type: string, key: string): string {
  const src = type === 'commands' ? settings.commandsMeta
            : type === 'skills' ? settings.skillsMeta
                                : settings.pluginsMeta
  let m = src[key]
  if (!m && key.includes('_')) m = src[key.replace(/_/g, '-')]
  if (!m && key.includes('-')) m = src[key.replace(/-/g, '_')]
  return m?.label || key
}
</script>

<template>
  <div class="dc-panel">
    <div class="panel glass">
      <div class="panel-title">
        <span>智能体默认能力</span>
        <span class="hint">新建智能体时自动勾选这些能力</span>
        <button class="btn sm ghost" style="margin-left:auto" @click="reloadCapabilities" :disabled="refreshLoading">
          <span v-if="refreshLoading">⟳ 刷新中…</span>
          <span v-else>⟳ 刷新能力</span>
        </button>
      </div>

      <!-- 默认命令 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">默认命令</span>
          <button class="btn sm ghost" @click="openPicker('commands')" :disabled="availableByType('commands').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="k in defaults.commands" :key="k" class="chip blue">
            {{ displayLabel('commands', k) }} <span class="x" @click="removeDefault('commands', k)">×</span>
          </div>
          <div v-if="!defaults.commands.length" class="hint-empty">未设置，点击 "+ 添加"</div>
        </div>
      </div>

      <!-- 默认技能 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">默认技能</span>
          <button class="btn sm ghost" @click="openPicker('skills')" :disabled="availableByType('skills').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="s in defaults.skills" :key="s" class="chip purple">
            {{ displayLabel('skills', s) }} <span class="x" @click="removeDefault('skills', s)">×</span>
          </div>
          <div v-if="!defaults.skills.length" class="hint-empty">未设置，点击 "+ 添加"</div>
        </div>
      </div>

      <!-- 默认插件 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">默认插件</span>
          <button class="btn sm ghost" @click="openPicker('plugins')" :disabled="availableByType('plugins').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="p in defaults.plugins" :key="p" class="chip green">
            {{ displayLabel('plugins', p) }} <span class="x" @click="removeDefault('plugins', p)">×</span>
          </div>
          <div v-if="!defaults.plugins.length" class="hint-empty">未设置，点击 "+ 添加"</div>
        </div>
      </div>

      <!-- 工具调用最大轮数 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">工具调用最大轮数</span>
          <span class="hint-empty">单次回复最多迭代轮数，默认 50，失焦即保存</span>
        </div>
        <input class="input rounds-input" type="number" min="1" step="1"
               v-model.number="maxRounds" @change="persistMaxRounds" />
      </div>
    </div>

    <!-- 统一能力选择器弹窗 -->
    <Teleport to="body">
      <div v-if="pickerOpen" class="picker-mask">
        <div class="picker-popup glass">
          <div class="picker-title">
            <span>{{ pickerTitle(pickerOpen) }}</span>
            <span class="picker-sub">共 {{ currentPickerItems.length }} 项</span>
          </div>
          <div class="picker-list">
            <template v-if="currentPickerGroups">
              <div v-for="g in currentPickerGroups" :key="g.name" class="picker-group">
                <div class="pg-head" @click="toggleGroupExpand(g.name)">
                  <input type="checkbox"
                         :checked="isGroupSelected(g)"
                         :indeterminate="isGroupIndeterminate(g)"
                         @click.stop="toggleGroup(g)" />
                  <span class="pg-name">{{ g.name }}</span>
                  <span class="pg-count">{{ g.items.length }}</span>
                  <span class="pg-arrow">{{ collapsedGroups.has(g.name) ? '▸' : '▾' }}</span>
                </div>
                <div v-if="!collapsedGroups.has(g.name)" class="pg-items">
                  <label v-for="item in g.items" :key="item.key"
                         :class="['picker-item', { selected: selectedKeys.has(item.key) }]" @click="togglePick(item.key)">
                    <input type="checkbox" :checked="selectedKeys.has(item.key)" readonly />
                    <div class="pi-body">
                      <div class="pi-label">{{ item.label }}</div>
                      <div class="pi-key">{{ item.key }}</div>
                      <div v-if="item.desc" class="pi-desc">{{ item.desc }}</div>
                    </div>
                  </label>
                </div>
              </div>
              <div v-if="currentPickerGroups.length === 0" class="hint-empty">全部已添加</div>
            </template>

            <template v-else>
              <label v-for="item in currentPickerItems" :key="item.key"
                     :class="['picker-item', { selected: selectedKeys.has(item.key) }]" @click="togglePick(item.key)">
                <input type="checkbox" :checked="selectedKeys.has(item.key)" readonly />
                <div class="pi-body">
                  <div class="pi-label">
                    {{ item.label }}
                    <span v-if="item.extra" class="pi-extra">{{ item.extra }}</span>
                  </div>
                  <div class="pi-key">{{ item.key }}</div>
                  <div v-if="item.desc" class="pi-desc">{{ item.desc }}</div>
                </div>
              </label>
              <div v-if="currentPickerItems.length === 0" class="hint-empty">全部已添加</div>
            </template>
          </div>
          <div class="picker-actions">
            <button class="btn ghost" @click="closePicker">取消</button>
            <button class="btn" @click="confirmAdd" :disabled="selectedKeys.size === 0">
              添加 ({{ selectedKeys.size }})
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <Transition name="fade">
      <div v-if="toast" class="toast" :class="toast.kind">{{ toast.msg }}</div>
    </Transition>
  </div>
</template>

<style scoped>
.dc-panel { display: flex; flex-direction: column; gap: 12px; }
.panel { padding: 18px; }
.panel-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.panel-title .hint { font-size: calc(11px * var(--font-scale)); font-weight: normal; opacity: .5; }
.sm { padding: 3px 10px; font-size: calc(11px * var(--font-scale)); }

.cap-block { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.06); }
.cap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.cap-title { font-size: calc(12px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); font-weight: 500; }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; min-height: 26px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px 3px 10px; border-radius: 999px; font-size: calc(12px * var(--font-scale)); cursor: default;
  border: 1px solid rgba(var(--surface-rgb), 0.15);
  &.blue   { background: rgba(var(--accent-rgb), 0.18);  color: var(--accent-light); border-color: rgba(var(--accent-rgb), 0.3); }
  &.purple { background: rgba(var(--accent2-rgb), 0.18); color: var(--accent2-light); border-color: rgba(var(--accent2-rgb), 0.3); }
  &.green  { background: rgba(100,220,160,0.18); color: var(--success-light); border-color: rgba(100,220,160,0.3); }
  .x { margin-left: 4px; opacity: .7; font-weight: bold; cursor: pointer; padding: 0 2px; &:hover { opacity: 1; } }
}
.hint-empty { font-size: calc(12px * var(--font-scale)); opacity: .45; padding: 4px 0; }
.rounds-input { width: 140px; }

/* —— picker 弹窗 —— */
.picker-mask {
  position: fixed; inset: 0;
  background: rgba(var(--shadow-rgb), 0.35);
  backdrop-filter: blur(2px);
  z-index: 2000;
  display: flex; align-items: center; justify-content: center;
}
.picker-popup {
  width: 560px; max-width: 92vw; max-height: 80vh;
  display: flex; flex-direction: column;
}
.picker-title {
  padding: 14px 18px 10px; display: flex; align-items: center; gap: 10px;
  font-weight: 600; font-size: calc(14px * var(--font-scale));
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.08);
}
.picker-sub { font-size: calc(11px * var(--font-scale)); opacity: .5; font-weight: normal; }
.picker-list { padding: 6px 10px; overflow-y: auto; flex: 1; }
.picker-group { margin-bottom: 4px; }
.pg-head {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px; cursor: pointer;
  border-radius: 6px; font-size: calc(12px * var(--font-scale));
  &:hover { background: rgba(var(--surface-rgb), 0.06); }
}
.pg-name { font-weight: 500; flex: 1; }
.pg-count { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pg-arrow { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pg-items { padding-left: 12px; }
.picker-item {
  display: flex; align-items: flex-start; gap: 10px; padding: 8px 10px;
  border-radius: 6px; cursor: pointer; font-size: calc(12px * var(--font-scale));
  border: 1px solid transparent;
  &:hover { background: rgba(var(--surface-rgb), 0.06); }
  &.selected { background: rgba(var(--accent-rgb), 0.12); border-color: rgba(var(--accent-rgb), 0.3); }
}
.picker-item input[type=checkbox] { margin-top: 3px; }
.pi-body { flex: 1; min-width: 0; }
.pi-label { font-weight: 500; }
.pi-extra { font-size: calc(10px * var(--font-scale)); opacity: .5; margin-left: 4px; }
.pi-key { font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pi-desc { font-size: calc(11px * var(--font-scale)); opacity: .7; margin-top: 2px; }
.picker-actions {
  padding: 12px 18px; display: flex; justify-content: flex-end; gap: 8px;
  border-top: 1px solid rgba(var(--surface-rgb), 0.08);
}

/* toast 与全局一致 */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 8px 18px; border-radius: 999px; font-size: calc(13px * var(--font-scale)); z-index: 3000;
  backdrop-filter: blur(6px);
  &.ok { background: rgba(100, 200, 160, 0.2); color: #80d8b0; border: 1px solid rgba(100, 200, 160, 0.35); }
  &.err { background: rgba(220, 100, 100, 0.2); color: #e09090; border: 1px solid rgba(220, 100, 100, 0.35); }
}
.fade-enter-active, .fade-leave-active { transition: opacity .2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
