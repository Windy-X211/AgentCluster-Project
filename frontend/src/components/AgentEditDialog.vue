<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useAgentStore, useSettingsStore, useModelStore } from '@/stores'

const props = defineProps<{ agent: any }>()
const emit = defineEmits<{ close: [] }>()

const agents = useAgentStore()
const settings = useSettingsStore()
const models = useModelStore()
function initForm(a: any) {
  return {
    ...a,
    commands: [...(a?.commands || [])],
    skills: [...(a?.skills || [])],
    plugins: [...(a?.plugins || [])],
    perception_scope: a?.perception_scope || 'link',
    workflow_mode: a?.workflow_mode || 'none',
    group_id: a?.group_id ?? null,
  }
}
const form = ref<any>(initForm(props.agent))

// 联动: 选中 interface_id 后，模型下拉自动填充该接口的 models
const currentInterface = computed(() => models.list.find((m: any) => m.id === form.value.interface_id))
const modelOptions = computed(() => {
  const mi = currentInterface.value
  if (!mi?.models?.length) return []
  const list = [...mi.models]
  if (mi.default_model && !list.includes(mi.default_model)) list.unshift(mi.default_model)
  return list
})
function onInterfaceChange(id: number | null) {
  form.value.interface_id = id
  // 自动应用该接口的 default_model（仅当智能体没自己指定 model 或当前 model 不在新接口 models 里时）
  const mi = models.list.find((m: any) => m.id === id)
  if (mi?.default_model && (!form.value.model || !mi.models?.includes(form.value.model))) {
    form.value.model = mi.default_model
  }
}

// picker 类型: 'commands' | 'skills' | 'plugins'
const pickerOpen = ref<string | null>(null)
const selectedKeys = ref<Set<string>>(new Set())
const collapsedGroups = ref<Set<string>>(new Set())

watch(() => props.agent, (v) => { if (v) form.value = initForm(v) })

// 三种能力的"可选项"（去掉已添加的；命令同时隐藏运行时注入的常驻工具）
const availableByType = (type: string) => {
  const source = type === 'commands' ? settings.commandsMeta
               : type === 'skills'   ? settings.skillsMeta
                                     : settings.pluginsMeta
  const added = new Set((form.value[type] || []) as string[])
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

// 命令选择器分组（仅 commands；skills/plugins 返回 null 走扁平列表）
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
  return type === 'commands' ? '选择命令' : type === 'skills' ? '选择技能' : '选择插件'
}

function openPicker(type: string) {
  selectedKeys.value = new Set()
  pickerOpen.value = type
  // 命令组默认折叠，点击展开后单独勾选
  collapsedGroups.value = type === 'commands' ? new Set(settings.commandGroups || []) : new Set()
}
function closePicker() { pickerOpen.value = null; selectedKeys.value = new Set() }
function togglePick(key: string) {
  if (selectedKeys.value.has(key)) selectedKeys.value.delete(key)
  else selectedKeys.value.add(key)
}
function confirmAdd() {
  if (!pickerOpen.value) return
  const list: string[] = form.value[pickerOpen.value] || []
  selectedKeys.value.forEach(k => list.push(k))
  form.value[pickerOpen.value] = list
  closePicker()
}

function removeItem(type: string, key: string | number) {
  if (type === 'skills' && typeof key === 'number') form.value.skills.splice(key, 1)
  else form.value[type] = (form.value[type] || []).filter((k: any) => k !== key)
}

function _metaOf(type: string, key: string): any {
  const src = type === 'commands' ? settings.commandsMeta
            : type === 'skills' ? settings.skillsMeta
                                : settings.pluginsMeta
  let m = src[key]
  if (!m && key.includes('_')) m = src[key.replace(/_/g, '-')]
  if (!m && key.includes('-')) m = src[key.replace(/-/g, '_')]
  return m
}
function displayLabel(type: string, key: string): string {
  return _metaOf(type, key)?.label || key
}
function itemDesc(type: string, key: string): string {
  if (type === 'skills' || type === 'plugins') {
    const m = _metaOf(type, key)
    return m?.description || m?.desc || ''
  }
  return ''
}
function chipColor(type: string): string {
  return type === 'commands' ? 'blue' : type === 'skills' ? 'purple' : 'green'
}

async function save() {
  form.value.commands = form.value.commands || []
  form.value.skills = form.value.skills || []
  form.value.plugins = form.value.plugins || []
  form.value.perception_scope = form.value.perception_scope || 'link'
  form.value.workflow_mode = form.value.workflow_mode || 'none'
  // 工具调用最大轮数：空 = 跟随全局设置
  const r = form.value.max_tool_rounds
  form.value.max_tool_rounds = (r === '' || r == null || Number.isNaN(Number(r)))
    ? null
    : Math.max(1, Math.floor(Number(r)))
  if (form.value.id) await agents.update(form.value.id, form.value)
  else await agents.create(form.value)
  emit('close')
}
async function remove() {
  if (!form.value.id) return emit('close')
  await agents.remove(form.value.id); emit('close')
}
</script>

<template>
  <div class="modal-bg">
    <div class="modal glass">
      <div class="modal-title">{{ form.id ? '编辑智能体' : '新建智能体' }}</div>
      <div class="grid">
        <label>名称<input class="input" v-model="form.name" /></label>
        <label>描述<input class="input" v-model="form.description" /></label>
        <label>模型接口
          <select class="input" v-model="form.interface_id" @change="onInterfaceChange(form.interface_id)">
            <option :value="null">默认</option>
            <option v-for="m in models.list" :key="m.id" :value="m.id">{{ m.name }}  ·  {{ m.default_model }}</option>
          </select>
        </label>
        <label>模型
          <select v-if="modelOptions.length" class="input" v-model="form.model">
            <option v-for="o in modelOptions" :key="o" :value="o">{{ o }}</option>
          </select>
          <input v-else class="input" v-model="form.model" :placeholder="currentInterface?.default_model || '留空则用接口默认'" />
        </label>
        <label>工具调用最大轮数
          <input class="input" type="number" min="1" step="1"
                 :value="form.max_tool_rounds ?? ''"
                 @input="form.max_tool_rounds = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"
                 :placeholder="'默认 ' + (settings.data.max_tool_rounds || 50) + '（跟随全局）'" />
        </label>
        <label>感知范围
          <select class="input" v-model="form.perception_scope">
            <option value="link">仅连线邻居（默认）</option>
            <option value="global">画布全体成员</option>
          </select>
        </label>
        <label>工作流模式
          <select class="input" v-model="form.workflow_mode">
            <option value="none">无（普通对话）</option>
            <option value="plan">Plan · 先出计划，确认后再执行</option>
            <option value="spec">Spec · 规格/任务/验收三件套驱动</option>
            <option value="goal">Goal · 目标驱动，自评达成度收敛</option>
          </select>
        </label>
        <label>分组
          <select class="input" v-model="form.group_id">
            <option :value="null">未分组</option>
            <option v-for="g in agents.groups" :key="g.id" :value="g.id">{{ g.name }}</option>
          </select>
        </label>
        <label class="full">系统提示词<textarea class="input ta" v-model="form.system_prompt" /></label>
      </div>

      <!-- 命令 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">命令</span>
          <button class="btn sm ghost" @click="openPicker('commands')" :disabled="availableByType('commands').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="k in form.commands" :key="k" class="chip blue" :title="itemDesc('commands', k)">
            {{ displayLabel('commands', k) }} <span class="x" @click="removeItem('commands', k)">×</span>
          </div>
          <div v-if="!form.commands?.length" class="hint-empty">未添加，点击 "+ 添加"</div>
        </div>
      </div>

      <!-- 技能 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">技能</span>
          <button class="btn sm ghost" @click="openPicker('skills')" :disabled="availableByType('skills').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="s in form.skills" :key="s" class="chip purple" :title="itemDesc('skills', s)">
            {{ displayLabel('skills', s) }} <span class="x" @click="removeItem('skills', s)">×</span>
          </div>
          <div v-if="!form.skills?.length" class="hint-empty">未添加，点击 "+ 添加" 进入技能库选择</div>
        </div>
      </div>

      <!-- 插件 -->
      <div class="cap-block">
        <div class="cap-header">
          <span class="cap-title">插件</span>
          <button class="btn sm ghost" @click="openPicker('plugins')" :disabled="availableByType('plugins').length === 0">+ 添加</button>
        </div>
        <div class="chip-row">
          <div v-for="k in form.plugins" :key="k" class="chip green" :title="itemDesc('plugins', k)">
            {{ displayLabel('plugins', k) }} <span class="x" @click="removeItem('plugins', k)">×</span>
          </div>
          <div v-if="!form.plugins?.length" class="hint-empty">未添加，点击 "+ 添加" 进入插件库选择</div>
        </div>
      </div>

      <div class="actions">
        <button class="btn ghost" v-if="form.id" @click="remove">删除</button>
        <div style="flex:1" />
        <button class="btn ghost" @click="emit('close')">取消</button>
        <button class="btn" @click="save">保存</button>
      </div>
    </div>

    <!-- 统一能力选择器（居中弹窗） -->
    <Teleport to="body">
      <div v-if="pickerOpen" class="picker-mask">
        <div class="picker-popup glass">
          <div class="picker-title">
            <span>{{ pickerTitle(pickerOpen) }}</span>
            <span class="picker-sub">共 {{ currentPickerItems.length }} 项</span>
          </div>
          <div class="picker-list">
            <!-- 命令：按分组展示，可整组勾选或展开后单独勾选 -->
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

            <!-- 技能 / 插件：扁平列表 -->
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
  </div>
</template>

<style scoped>
.modal-bg { position: fixed; inset: 0; background: rgba(var(--shadow-rgb), 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 560px; max-height: 85vh; overflow-y: auto; padding: 20px; }
.modal-title { font-size: calc(16px * var(--font-scale)); font-weight: 600; margin-bottom: 16px; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  label { display: flex; flex-direction: column; gap: 4px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7);
    &.full { grid-column: 1 / -1; }
  }
}
.ta { min-height: 80px; resize: vertical; }

.cap-block { margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(var(--surface-rgb), 0.06); }
.cap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.cap-title { font-size: calc(12px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); font-weight: 500; }
.sm { padding: 3px 10px; font-size: calc(11px * var(--font-scale)); }

.chip-row { display: flex; flex-wrap: wrap; gap: 6px; min-height: 26px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px 3px 10px; border-radius: 999px; font-size: calc(12px * var(--font-scale)); cursor: default;
  border: 1px solid rgba(var(--surface-rgb), 0.15);
  &.blue   { background: rgba(var(--accent-rgb), 0.18);  color: var(--accent-ink); border-color: rgba(var(--accent-rgb), 0.3); }
  &.purple { background: rgba(var(--accent2-rgb), 0.18); color: var(--purple-ink); border-color: rgba(var(--accent2-rgb), 0.3); }
  &.green  { background: rgba(100,220,160,0.18); color: var(--green-ink); border-color: rgba(100,220,160,0.3); }
  .x { margin-left: 4px; opacity: .7; font-weight: bold; cursor: pointer; padding: 0 2px; &:hover { opacity: 1; } }
}
.hint-empty { font-size: calc(12px * var(--font-scale)); opacity: .45; padding: 4px 0; }
.actions { display: flex; gap: 8px; margin-top: 20px; padding-top: 14px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); }

/* 能力选择器 —— 通过 Teleport 挂到 body，真正居中在画面中央 */
.picker-mask {
  position: fixed; inset: 0;
  background: rgba(var(--shadow-rgb), 0.35);
  backdrop-filter: blur(2px);
  z-index: 2000;
  display: flex; align-items: center; justify-content: center;
}
.picker-popup {
  width: 460px; max-height: 70vh; padding: 18px;
  display: flex; flex-direction: column;
  box-shadow: 0 20px 60px rgba(var(--shadow-rgb), 0.6);
  animation: pickerIn .2s ease;
}
@keyframes pickerIn {
  from { transform: scale(.94); opacity: 0; }
  to   { transform: scale(1); opacity: 1; }
}
.picker-title { display: flex; justify-content: space-between; align-items: center;
  font-size: calc(14px * var(--font-scale)); font-weight: 600; padding-bottom: 10px; border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); margin-bottom: 10px; }
.picker-sub { font-size: calc(11px * var(--font-scale)); opacity: .5; font-weight: normal; }
.picker-list { overflow-y: auto; flex: 1; }
.picker-group { margin-bottom: 4px; }
.pg-head {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: 8px; cursor: pointer;
  background: rgba(var(--surface-rgb), 0.03); border: 1px solid rgba(var(--surface-rgb), 0.06);
  &:hover { background: rgba(var(--surface-rgb), 0.06); }
  input { accent-color: var(--accent); flex-shrink: 0; }
}
.pg-name { font-size: calc(13px * var(--font-scale)); font-weight: 600; flex: 1; }
.pg-count { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pg-arrow { font-size: calc(11px * var(--font-scale)); opacity: .6; width: 12px; text-align: center; }
.pg-items { padding-left: 10px; border-left: 1px solid rgba(var(--surface-rgb), 0.06); margin-left: 8px; }
.picker-item { display: flex; gap: 10px; padding: 10px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent; font-size: calc(12px * var(--font-scale));
  &:hover { background: rgba(var(--surface-rgb), 0.04); }
  &.selected { background: rgba(var(--accent-rgb), 0.15); border-color: rgba(var(--accent-rgb), 0.3); }
  input { accent-color: var(--accent); flex-shrink: 0; margin-top: 3px; }
}
.pi-body { flex: 1; min-width: 0; }
.pi-label { font-size: calc(13px * var(--font-scale)); font-weight: 500; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.pi-extra { font-size: calc(10px * var(--font-scale)); opacity: .55; font-weight: normal; }
.pi-key { font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale)); opacity: .55; margin-top: 1px; }
.pi-desc { font-size: calc(11px * var(--font-scale)); opacity: .6; margin-top: 3px; }
.picker-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); }
</style>
