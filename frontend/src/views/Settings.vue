<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useModelStore, useSettingsStore, useLayoutStore, FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP } from '@/stores'
import { api } from '@/api'
import FileExplorerDialog from '@/components/FileExplorerDialog.vue'
import ParamSelect from '@/components/ParamSelect.vue'
import { getAvatarGradient } from '@/utils/avatar'

const models = useModelStore()
const settings = useSettingsStore()
const layout = useLayoutStore()

const themeOptions = [
  { id: 'standard', name: '标准配色-银河蓝', bg: '#0a0e1a', bgGrad: '#1a2340', text: '#e8ecf4', accent: '#6ea8ff', surface: 'rgba(255,255,255,0.12)' },
  { id: 'deep-black', name: '深邃黑', bg: '#000000', bgGrad: '#080808', text: '#d7d7d7', accent: '#6ea8ff', surface: 'rgba(255,255,255,0.12)' },
  { id: 'tech-white', name: '科技白', bg: '#ffffff', bgGrad: '#e6eef9', text: '#1e283c', accent: '#2563eb', surface: 'rgba(20,30,50,0.08)' },
  { id: 'eye-care', name: '护眼白', bg: '#f4ecd6', bgGrad: '#faf3df', text: '#46371e', accent: '#2b6cb0', surface: 'rgba(90,70,30,0.1)' },
]

const showDialog = ref(false)
const isEditing = ref(false)
const ifaceForm = ref<any>({})

// 数据存储路径
const storagePaths = ref<any>(null)

// 存储位置（可自定义，记忆到 settings，暂不迁移已有数据）
const storageLocation = ref('')
const storageSaving = ref(false)

// 文件浏览器弹窗
const explorerOpen = ref(false)
const explorerInitial = ref('')

// 默认能力的本地镜像（方便实时编辑、再写回 settings）
const defaults = ref<{ commands: string[]; skills: string[]; plugins: string[] }>({
  commands: [], skills: [], plugins: [],
})

// 工具调用最大轮数（可自定义，默认 50）
const maxRounds = ref<number>(50)

// 统一能力选择器（与 AgentEditDialog 同款）
const pickerOpen = ref<string | null>(null)
const selectedKeys = ref<Set<string>>(new Set())
const collapsedGroups = ref<Set<string>>(new Set())

// 能力刷新
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
    showToast(`已刷新：${Object.keys(settings.commandsMeta).length} 个命令 · ${Object.keys(settings.skillsMeta).length} 个技能 · ${Object.keys(settings.pluginsMeta).length} 个插件`)
  } catch (e: any) {
    showToast(`刷新失败：${e?.message || e}`, 'err')
  } finally {
    refreshLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([models.fetch(), settings.fetch()])
  defaults.value.commands = [...(settings.data.default_commands || [])]
  defaults.value.skills   = [...(settings.data.default_skills   || [])]
  defaults.value.plugins  = [...(settings.data.default_plugins  || [])]
  maxRounds.value = Number(settings.data.max_tool_rounds) || 50
  // 存储路径
  try { storagePaths.value = await api.storage.paths() } catch {}
  storageLocation.value = storagePaths.value?.root || ''
  loadPresets()
})

function openPath(p: string) { api.storage.open(p).catch(() => {}) }
// 将绝对路径转换为相对存储位置（root）的相对路径，供“路径”列展示
function relPath(p: string): string {
  const root = storagePaths.value?.root
  if (!root || !p) return p
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '')
  const r = norm(root)
  const pp = norm(p)
  if (pp.toLowerCase() === r.toLowerCase()) return '.'
  if (pp.toLowerCase().startsWith(r.toLowerCase() + '/')) {
    return pp.slice(r.length + 1)
  }
  return p
}
function openExplorer(p: string) {
  explorerInitial.value = p
  explorerOpen.value = true
}

async function persistStorageLocation() {
  const val = storageLocation.value.trim()
  if (!val) return
  if (val === storagePaths.value?.root) return
  storageSaving.value = true
  try {
    const r = await api.storage.setLocation(val)
    storagePaths.value = r?.paths ?? storagePaths.value
    storageLocation.value = storagePaths.value?.root || val
    // 前端同步刷新 settings / capabilities，让其它页也能立即感知
    await Promise.all([settings.fetch(), models.fetch()])
    showToast('已切换到新存储位置')
  } catch (e: any) {
    showToast(`切换失败：${e?.message || e}`, 'err')
  } finally {
    storageSaving.value = false
  }
}

async function browseStorage() {
  try {
    const r = await api.storage.pickFolder(storageLocation.value || storagePaths.value?.root)
    if (r?.path) {
      storageLocation.value = r.path
      await persistStorageLocation()
    }
  } catch (e: any) {
    showToast(`打开选择器失败：${e?.message || e}`, 'err')
  }
}

// —— 能力选择器逻辑 ——
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
  return type === 'commands' ? '选择默认命令' : type === 'skills' ? '选择默认技能' : '选择默认插件'
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
  const list: string[] = (defaults.value[pickerOpen.value as keyof typeof defaults.value] as string[]) || []
  selectedKeys.value.forEach(k => list.push(k))
  defaults.value[pickerOpen.value as keyof typeof defaults.value] = list
  // 立即持久化
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

// —— 模型接口 ——
function openNew() {
  ifaceForm.value = { name: '', url: '', api_key: '', default_model: 'gpt-4o-mini', models_str: '', models: [], description: '' }
  isEditing.value = false; showDialog.value = true
}
function edit(i: any) {
  ifaceForm.value = { ...i, models_str: (i.models || []).join(',') }
  isEditing.value = true; showDialog.value = true
}
function closeDialog() { showDialog.value = false; ifaceForm.value = {} }
async function saveIface() {
  ifaceForm.value.models = (ifaceForm.value.models_str || '').split(',').map((s: string) => s.trim()).filter(Boolean)
  const { models_str, ...clean } = ifaceForm.value
  if (isEditing.value) await models.update(ifaceForm.value.id, clean)
  else await models.create(clean)
  closeDialog(); await models.fetch()
}
async function setDefaultIface(id: number) { await settings.update({ default_model_interface: id }) }

// —— 能力参数预设 ——
const presets = ref<any[]>([])
const presetDialog = ref(false)
const presetSaving = ref(false)
const isEditingPreset = ref(false)
const presetForm = ref<any>({ target_type: 'plugin', target_name: '', label: '', note: '', enabled: true, fixed: [], optional: [] })

const currentTargetParams = computed(() => {
  const base = presetForm.value?.target_type === 'skill' ? settings.skillsMeta : settings.pluginsMeta
  const meta = (base || {})[presetForm.value?.target_name] || {}
  const norm = (arr: any[]) => (arr || [])
    .map((p: any) => typeof p === 'string'
      ? { name: p, desc: '', type: '', enum: null as any, default: undefined }
      : {
          name: p?.name || '',
          desc: p?.description || p?.desc || p?.label || '',
          type: p?.type || '',
          enum: Array.isArray(p?.enum) ? p.enum : null,
          default: p?.default,
        })
    .filter((p: any) => p.name)
  const fromParams = norm(meta.parameters)
  return fromParams.length ? fromParams : norm(meta.params)
})
function paramOptions(currentKey: string) {
  const opts = currentTargetParams.value.slice()
  if (currentKey && !opts.some(p => p.name === currentKey)) {
    opts.unshift({ name: currentKey, desc: '自定义', type: '', enum: null as any, default: undefined })
  }
  return opts
}

// —— 外观设置 ——
const avatarMode = computed({
  get() { return settings.data.avatar_mode || 'differentiated' },
  set(v: string) { settings.update({ avatar_mode: v }) }
})

function avatarPreviewGradient(name: string): string {
  if (avatarMode.value === 'unified') {
    return 'linear-gradient(135deg, #6376b8, #8d73c4)'
  }
  return getAvatarGradient(name)
}

// —— 固定参数值选项：当目标参数带 enum 或为 boolean 时，返回可选值列表（用于下拉框）；否则返回 null（用文本/密码输入） ——
function valueOptionsFor(targetType: string, targetName: string, key: string): any[] | null {
  if (!key) return null
  const base = targetType === 'skill' ? settings.skillsMeta : settings.pluginsMeta
  const meta = (base || {})[targetName] || {}
  const params: any[] = meta.parameters || meta.params || []
  const p = params.find((x: any) => (typeof x === 'string' ? x : x?.name) === key)
  if (!p || typeof p === 'string') return null
  if (Array.isArray(p.enum) && p.enum.length) {
    return p.enum.map((v: any) => ({ value: String(v), label: String(v) }))
  }
  if ((p.type || '').toLowerCase() === 'boolean') {
    return [{ value: 'false', label: 'false' }, { value: 'true', label: 'true' }]
  }
  return null
}
function paramValueOptions(row: any): any[] | null {
  const f = presetForm.value
  return valueOptionsFor(f?.target_type, f?.target_name, row?.key)
}
// 把当前值对齐到选项（用于加载旧数据 / 切换 key 时规范化）
function coerceToOption(row: any): void {
  const opts = paramValueOptions(row)
  if (!opts) return
  const cur = String(row.value ?? '').toLowerCase()
  const m = opts.find((o: any) => String(o.value).toLowerCase() === cur)
  if (m) {
    row.value = m.value
  } else {
    // 不在选项内 → 取参数 default（若 default 也在选项内）或首项
    const p = currentTargetParams.value.find((x: any) => x.name === row.key)
    const def = p?.default
    const defOpt = (def !== undefined && def !== null && def !== '')
      ? opts.find((o: any) => String(o.value).toLowerCase() === String(def).toLowerCase())
      : null
    row.value = defOpt ? defOpt.value : opts[0].value
  }
}
function onFixedKeyChange(row: any) {
  coerceToOption(row)
}
const presetTypeIcon = (t: string) => t === 'skill' ? '⚡' : '🧩'

async function loadPresets() {
  try { presets.value = await api.presets.list() } catch { presets.value = [] }
}
function openNewPreset() {
  presetForm.value = { target_type: 'plugin', target_name: '', label: '', note: '', enabled: true, fixed: [], optional: [] }
  isEditingPreset.value = false; presetDialog.value = true
}
function editPreset(p: any) {
  presetForm.value = {
    ...p,
    fixed: (p.fixed || []).map((x: any) => {
      const row = { ...x, _visible: false }
      // 加载时把 enum/布尔固定参数的值对齐到选项（旧加密条目解密后可能是 "False" 等大小写不一致的值）
      const opts = valueOptionsFor(p.target_type, p.target_name, row.key)
      if (opts && opts.length) {
        const cur = String(row.value ?? '').toLowerCase()
        const m = opts.find((o: any) => String(o.value).toLowerCase() === cur)
        row.value = m ? m.value : (opts[0]?.value ?? row.value)
      }
      return row
    }),
    optional: (p.optional || []).map((x: any) => ({ ...x })),
  }
  isEditingPreset.value = true; presetDialog.value = true
}
function closePresetDialog() { presetDialog.value = false; presetForm.value = {} }
async function savePreset() {
  presetSaving.value = true
  try {
    const f = presetForm.value
    const payload = {
      target_type: f.target_type,
      target_name: f.target_name,
      label: f.label || '', note: f.note || '', enabled: !!f.enabled,
      fixed: (f.fixed || []).map((x: any) => {
        const opts = paramValueOptions(x)
        const isPlain = !!(opts && opts.length)
        // 值对齐到选项；不在选项内时落到首项
        let val = x.value ?? ''
        if (isPlain && opts) {
          const cur = String(val).toLowerCase()
          const m = opts.find((o: any) => String(o.value).toLowerCase() === cur)
          val = m ? m.value : opts[0].value
        }
        return { key: (x.key || '').trim(), value: val, plain: isPlain }
      }).filter((x: any) => x.key),
      optional: (f.optional || []).map((x: any) => ({ key: (x.key || '').trim(), desc: x.desc ?? '' })).filter((x: any) => x.key),
    }
    if (isEditingPreset.value) await api.presets.update(f.id, payload)
    else await api.presets.create(payload)
    closePresetDialog(); await loadPresets()
  } catch (e: any) { alert('保存失败：' + (e?.message || e)) }
  finally { presetSaving.value = false }
}
async function removePreset(p: any) {
  if (!confirm(`删除预设「${p.label || p.target_name}」？`)) return
  await api.presets.remove(p.id); await loadPresets()
}
async function togglePreset(p: any) {
  await api.presets.update(p.id, { ...p, enabled: !p.enabled }); await loadPresets()
}
</script>

<template>
  <div class="settings-root">
    <div class="top glass">
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <h2>设置中心</h2>
    </div>

    <div class="panels">
      <!-- 模型接口 -->
      <div class="panel glass">
        <div class="panel-title">
          <span>模型接口</span>
          <button class="btn" @click="openNew">+ 新建接口</button>
        </div>
        <table class="tbl" v-if="models.list.length">
          <thead><tr><th>名称</th><th>URL</th><th>默认模型</th><th>可用模型</th><th style="width:240px"></th></tr></thead>
          <tbody>
            <tr v-for="m in models.list" :key="m.id">
              <td>
                {{ m.name }}
                <span class="tag" v-if="settings.data.default_model_interface === m.id">默认</span>
              </td>
              <td class="mono">{{ m.url }}</td>
              <td>{{ m.default_model }}</td>
              <td class="mono sm-cell">{{ (m.models || []).join(', ') || '—' }}</td>
              <td class="actions">
                <button class="btn ghost" v-if="settings.data.default_model_interface !== m.id" @click="setDefaultIface(m.id)">设为默认</button>
                <button class="btn ghost" @click="edit(m)">编辑</button>
                <button class="btn ghost" @click="models.remove(m.id)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!models.list.length" class="empty">还没有接口，点击右上角"新建接口"</div>
      </div>

      <!-- 默认能力（改用 + 添加 选择器，与 AgentEditDialog 一致） -->
      <div class="panel glass">
        <div class="panel-title">
          <span>智能体默认能力</span>
          <span class="hint">新建智能体时自动勾选这些能力</span>
          <button class="btn sm ghost" style="margin-left:auto" @click="reloadCapabilities" :disabled="refreshLoading" :title="'重新扫描插件/技能目录'">
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

      <!-- 能力参数预设 -->
      <div class="panel glass">
        <div class="panel-title">
          <span>🔐 能力参数预设</span>
          <button class="btn" @click="openNewPreset">+ 新建预设</button>
        </div>
        <div class="hint-empty" style="margin-bottom: 4px">
          为插件/技能预填参数：固定参数加密保存，模型不可见、不可更改；可选参数仅以文字描述供模型参考选择。
        </div>

        <div v-if="presets.length" class="preset-grid">
          <div v-for="p in presets" :key="p.id" class="preset-card" :class="{ off: !p.enabled }">
            <div class="pc-head">
              <span class="pc-type">{{ presetTypeIcon(p.target_type) }} {{ p.target_type === 'skill' ? '技能' : '插件' }}</span>
              <span class="pc-label">{{ p.label || p.target_name }}</span>
              <span class="pc-tag mono">{{ p.target_name }}</span>
            </div>
            <div v-if="p.note" class="pc-note">{{ p.note }}</div>
            <div class="pc-counts">
              <span class="cnt red">🔒 固定 {{ (p.fixed || []).length }}</span>
              <span class="cnt blue">📝 可选 {{ (p.optional || []).length }}</span>
            </div>
            <div class="pc-actions">
              <button class="btn sm ghost" @click="togglePreset(p)">{{ p.enabled ? '停用' : '启用' }}</button>
              <button class="btn sm ghost" @click="editPreset(p)">编辑</button>
              <button class="btn sm ghost" @click="removePreset(p)">删除</button>
            </div>
          </div>
        </div>
        <div v-else class="empty">还没有预设，点击右上角"新建预设"</div>
      </div>

      <!-- 外观设置 -->
      <div class="panel glass">
        <div class="panel-title">
          <span>🎨 外观设置</span>
          <span class="hint">界面外观偏好</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">头像风格</span>
          </div>
          <div class="appearance-row">
            <label class="radio-label" :class="{ active: avatarMode === 'differentiated' }">
              <input type="radio" name="avatarMode" value="differentiated" v-model="avatarMode" />
              <span class="radio-dot"></span>
              <span class="radio-text">差异化</span>
            </label>
            <label class="radio-label" :class="{ active: avatarMode === 'unified' }">
              <input type="radio" name="avatarMode" value="unified" v-model="avatarMode" />
              <span class="radio-dot"></span>
              <span class="radio-text">统一化</span>
            </label>
          </div>
          <div class="avatar-preview">
            <div class="preview-item">
              <div class="preview-label">智能体 A</div>
              <div class="preview-avatar" :style="{ background: avatarPreviewGradient('Alpha') }">A</div>
            </div>
            <div class="preview-item">
              <div class="preview-label">智能体 B</div>
              <div class="preview-avatar" :style="{ background: avatarPreviewGradient('Beta') }">B</div>
            </div>
            <div class="preview-item">
              <div class="preview-label">用户</div>
              <div class="preview-avatar user" :style="{ background: 'linear-gradient(135deg, #f97316, #ec4899)' }">U</div>
            </div>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            差异化：每个智能体根据名称生成不同颜色
            <br />
            统一化：所有智能体使用统一的蓝紫色渐变
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">文字大小</span>
            <button class="btn sm ghost" @click="layout.resetFontScale" :disabled="layout.fontScale === 1">默认</button>
          </div>
          <div class="font-scale-row">
            <span class="font-scale-edge">小</span>
            <input class="font-scale-slider" type="range"
                   :min="FONT_SCALE_MIN" :max="FONT_SCALE_MAX" :step="FONT_SCALE_STEP"
                   :value="layout.fontScale"
                   @input="layout.setFontScale(Number(($event.target as HTMLInputElement).value))" />
            <span class="font-scale-edge">大</span>
            <span class="font-scale-value">{{ Math.round(layout.fontScale * 100) }}%</span>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            只调整文字大小，不影响界面布局；设置自动保存，刷新后仍然生效
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">配色方案</span>
          </div>
          <div class="theme-grid">
            <div v-for="t in themeOptions" :key="t.id"
                 class="theme-card" :class="{ active: layout.theme === t.id }"
                 @click="layout.setTheme(t.id)">
              <div class="theme-preview" :style="{ background: 'radial-gradient(ellipse at 30% 20%, ' + t.bgGrad + ' 0%, ' + t.bg + ' 70%)' }">
                <div class="tp-bar" :style="{ background: t.surface }">
                  <span class="tp-dot" :style="{ background: t.accent }"></span>
                  <span class="tp-text" :style="{ color: t.text }">Aa 文字</span>
                </div>
                <div class="tp-chip" :style="{ background: t.accent }"></div>
              </div>
              <div class="theme-name">{{ t.name }}</div>
            </div>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            切换全局配色方案，设置自动保存，刷新后仍然生效
          </div>
        </div>
      </div>

      <!-- 数据存储 -->
      <div class="panel glass">
        <div class="panel-title">
          <span>📂 数据存储</span>
          <span class="hint">v2 · 每条记录一个 json</span>
        </div>
        <div class="storage-loc">
          <label class="storage-loc-label" for="storageLoc">存储位置</label>
          <input id="storageLoc" class="input mono storage-loc-input" v-model="storageLocation"
                 placeholder="默认：项目文件夹中的 AppData"
                 @blur="persistStorageLocation" @keydown.enter.prevent="persistStorageLocation" />
          <button class="btn sm ghost" @click="browseStorage" :disabled="storageSaving">浏览…</button>
        </div>
        <div class="storage-loc-tip">默认位置为项目文件夹中的 AppData 文件夹；切换后应用会按新位置的现有数据接管</div>
        <table class="tbl" v-if="storagePaths">
          <thead><tr><th>名称</th><th>类型</th><th>相对路径</th><th>数量</th><th style="width: 120px"></th></tr></thead>
          <tbody>
            <tr>
              <td>⚙️ 配置设置</td>
              <td class="tag-t">单文件</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.settings_file) }}</td>
              <td class="mono">1</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.root)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.root)">打开</button></td>
            </tr>
            <tr>
              <td>🧠 智能体</td>
              <td class="tag-t">每 agent 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.agents_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.agents ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.agents_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.agents_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🎨 画布（集群编排）</td>
              <td class="tag-t">每 canvas 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.canvases_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.canvases ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.canvases_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.canvases_dir)">打开</button></td>
            </tr>
            <tr>
              <td>💬 聊天记录</td>
              <td class="tag-t">每 conversation 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.conversations_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.conversations ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.conversations_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.conversations_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🔌 模型接口</td>
              <td class="tag-t">每 interface 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.model_interfaces_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.model_interfaces ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.model_interfaces_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.model_interfaces_dir)">打开</button></td>
            </tr>
            <tr>
              <td>⚡ 自定义技能</td>
              <td class="tag-t">每技能一个文件夹</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.skills_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.skills ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.skills_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.skills_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🧩 自定义插件</td>
              <td class="tag-t">每插件一个文件夹</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.plugins_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.plugins ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.plugins_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.plugins_dir)">打开</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!storagePaths" class="empty">后端未返回路径信息</div>
      </div>
    </div>

    <!-- 统一能力选择器（与 AgentEditDialog 同款，居中弹窗） -->
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

    <!-- 接口编辑弹窗 -->
    <div class="modal-bg" v-if="showDialog">
      <div class="modal glass">
        <div class="modal-title">{{ isEditing ? '编辑接口' : '新建接口' }}</div>
        <div class="grid">
          <label>名称<input class="input" v-model="ifaceForm.name" /></label>
          <label>URL<input class="input" v-model="ifaceForm.url" placeholder="https://api.openai.com/v1" /></label>
          <label class="full">API Key<input class="input" v-model="ifaceForm.api_key" type="password" /></label>
          <label>默认模型<input class="input" v-model="ifaceForm.default_model" placeholder="gpt-4o-mini" /></label>
          <label class="full">可用模型（逗号分隔，如 gpt-4o,gpt-4o-mini,o1）<input class="input" v-model="ifaceForm.models_str" /></label>
          <label class="full">描述<input class="input" v-model="ifaceForm.description" /></label>
        </div>
        <div class="actions">
          <button class="btn ghost" @click="closeDialog">取消</button>
          <button class="btn" @click="saveIface">保存</button>
        </div>
      </div>
    </div>

    <!-- 能力参数预设编辑弹窗 -->
    <div class="modal-bg" v-if="presetDialog">
      <div class="modal preset-modal">
        <div class="modal-title">{{ isEditingPreset ? '编辑预设' : '新建预设' }}</div>
        <div class="grid">
          <label>类型
            <select class="input" v-model="presetForm.target_type" @change="presetForm.target_name = ''">
              <option value="plugin">🧩 插件</option>
              <option value="skill">⚡ 技能</option>
            </select>
          </label>
          <label>目标能力
            <select class="input" v-model="presetForm.target_name">
              <option value="" disabled>选择…</option>
              <option v-for="(v, k) in (presetForm.target_type === 'skill' ? settings.skillsMeta : settings.pluginsMeta)"
                      :key="k" :value="k">{{ v.label || k }}</option>
            </select>
          </label>
          <label>预设名称（选填）<input class="input" v-model="presetForm.label" placeholder="如：QQ邮箱发送配置" /></label>
          <label>备注（选填）<input class="input" v-model="presetForm.note" placeholder="如：用于发送通知邮件" /></label>
          <label class="chk-full"><input type="checkbox" v-model="presetForm.enabled" /> 启用预设</label>
        </div>

        <!-- 固定参数 -->
        <div class="preset-block">
          <div class="preset-block-title">
            <span>🔒 固定参数</span>
            <span class="hint-empty">敏感值加密保存 · 明确选项（下拉选择）明文保存 · 模型不可见、不可更改</span>
          </div>
          <div v-for="(row, i) in presetForm.fixed" :key="'f' + i" class="kv-row">
            <ParamSelect v-if="currentTargetParams.length && !row._manual" :modelValue="row.key"
                         @update:modelValue="(v: any) => { row.key = v; onFixedKeyChange(row) }"
                         :options="paramOptions(row.key)" />
            <input v-else class="input mono" v-model="row.key" @change="onFixedKeyChange(row)" placeholder="参数名（如 smtp_user）" list="preset-param-list" />
            <button v-if="currentTargetParams.length" type="button" class="btn sm ghost icon-btn" @click="row._manual = !row._manual" :title="row._manual ? '从列表选择' : '手动输入'">{{ row._manual ? '▾' : '✎' }}</button>
            <select v-if="paramValueOptions(row)" class="input" v-model="row.value">
              <option v-for="o in paramValueOptions(row)" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <template v-else>
              <input class="input" v-model="row.value" :type="row._visible ? 'text' : 'password'" placeholder="固定值（如发件邮箱、授权码）" />
              <button type="button" class="btn sm ghost icon-btn" @click="row._visible = !row._visible" :title="row._visible ? '隐藏' : '显示'">{{ row._visible ? '🙈' : '👁' }}</button>
            </template>
            <button class="btn sm ghost" @click="presetForm.fixed.splice(i, 1)">×</button>
          </div>
          <button class="btn sm ghost" @click="presetForm.fixed.push({ key: '', value: '', _visible: false })">+ 添加固定参数</button>
        </div>

        <!-- 可选参数 -->
        <div class="preset-block">
          <div class="preset-block-title">
            <span>📝 可选参数</span>
            <span class="hint-empty">文字描述仅供模型参考选择</span>
          </div>
          <div v-for="(row, i) in presetForm.optional" :key="'o' + i" class="kv-row">
            <ParamSelect v-if="currentTargetParams.length && !row._manual" v-model="row.key" :options="paramOptions(row.key)" />
            <input v-else class="input mono" v-model="row.key" placeholder="参数名（如 to_email）" list="preset-param-list" />
            <button v-if="currentTargetParams.length" type="button" class="btn sm ghost icon-btn" @click="row._manual = !row._manual" :title="row._manual ? '从列表选择' : '手动输入'">{{ row._manual ? '▾' : '✎' }}</button>
            <textarea class="input" v-model="row.desc" rows="2" placeholder="描述，如：小明 &lt;xiaoming@x.com&gt;、小红 &lt;xiaohong@x.com&gt;"></textarea>
            <button class="btn sm ghost" @click="presetForm.optional.splice(i, 1)">×</button>
          </div>
          <button class="btn sm ghost" @click="presetForm.optional.push({ key: '', desc: '' })">+ 添加可选参数</button>
        </div>

        <datalist id="preset-param-list">
          <option v-for="p in currentTargetParams" :key="p.name" :value="p.name" />
        </datalist>
        <div v-if="!currentTargetParams.length" class="hint-empty" style="margin-top: 6px">
          该能力未声明参数，可手动输入参数名。
        </div>

        <div class="actions">
          <button class="btn ghost" @click="closePresetDialog">取消</button>
          <button class="btn" @click="savePreset" :disabled="presetSaving">
            {{ presetSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 文件浏览器 -->
    <FileExplorerDialog
      v-model:visible="explorerOpen"
      :initial-path="explorerInitial"
      @saved="() => { storagePaths.value = null; api.storage.paths().then(r => storagePaths.value = r).catch(() => {}) }"
    />

    <!-- 刷新能力 toast -->
    <Transition name="fade">
      <div v-if="toast" class="toast" :class="toast.kind">
        {{ toast.msg }}
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.settings-root { padding: 16px; height: 100vh; overflow-y: auto; }
.top { padding: 12px 20px; margin: 0 auto 16px; max-width: 1100px; display: flex; align-items: center; gap: 16px; h2 { font-size: calc(18px * var(--font-scale)); } }
.panels { display: grid; gap: 16px; max-width: 1100px; margin: 0 auto; }
.panel { padding: 18px; }
.panel-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.panel-title .hint { font-size: calc(11px * var(--font-scale)); font-weight: normal; opacity: .5; }
.tbl { width: 100%; margin-top: 12px; border-collapse: collapse; th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(var(--surface-rgb), 0.05); font-size: calc(13px * var(--font-scale)); } th { font-size: calc(11px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); } }
.mono { font-family: Consolas, monospace; font-size: calc(12px * var(--font-scale)); }
.sm-cell { max-width: 200px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.actions { display: flex; gap: 6px; justify-content: flex-end; }
.storage-loc { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.storage-loc-label { font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); flex-shrink: 0; }
.storage-loc-input { flex: 1; min-width: 0; width: auto; }
.storage-loc-tip { font-size: calc(11px * var(--font-scale)); opacity: .5; margin: 6px 0 0; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); }
.tag-t { font-size: calc(10px * var(--font-scale)); padding: 2px 8px; border-radius: 4px; background: rgba(var(--accent-rgb), 0.15); color: var(--accent-light); border: 1px solid rgba(var(--accent-rgb), 0.25); font-family: Consolas, monospace; }

.cap-block { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.06); }
.cap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.cap-title { font-size: calc(12px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); font-weight: 500; }
.sm { padding: 3px 10px; font-size: calc(11px * var(--font-scale)); }
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

/* —— 外观设置 —— */
.appearance-row { display: flex; gap: 12px; margin-bottom: 12px; }
.radio-label {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.1);
  background: rgba(var(--surface-rgb), 0.03);
  transition: all .15s;
  flex: 1; justify-content: center;
  input { display: none; }
  .radio-dot {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid rgba(var(--surface-rgb), 0.3);
    position: relative;
    transition: all .15s;
    &::after {
      content: '';
      position: absolute; top: 50%; left: 50%;
      transform: translate(-50%, -50%) scale(0);
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--accent);
      transition: transform .15s;
    }
  }
  .radio-text { font-size: calc(13px * var(--font-scale)); }
  &:hover { border-color: rgba(var(--accent-rgb), 0.4); background: rgba(var(--accent-rgb), 0.08); }
  &.active {
    border-color: var(--accent);
    background: rgba(var(--accent-rgb), 0.15);
    .radio-dot { border-color: var(--accent); &::after { transform: translate(-50%, -50%) scale(1); } }
  }
}
.avatar-preview {
  display: flex; gap: 16px; align-items: flex-end; padding: 12px;
  background: rgba(var(--surface-rgb), 0.03); border-radius: 8px;
  border: 1px solid rgba(var(--surface-rgb), 0.06);
}
.preview-item { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.preview-label { font-size: calc(11px * var(--font-scale)); opacity: .6; }
.preview-avatar {
  position: relative; overflow: hidden;
  width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: calc(14px * var(--font-scale)); font-weight: 600; color: #fff;
  text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.3);
  box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45),
              inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32),
              inset 0 0 0 1px rgba(var(--surface-rgb), 0.08),
              0 3px 8px rgba(var(--shadow-rgb), 0.4);
}
.preview-avatar::before {
  content: '';
  position: absolute; inset: 0;
  background:
    linear-gradient(165deg, rgba(var(--surface-rgb), 0.45) 0%, rgba(var(--surface-rgb), 0.10) 32%, rgba(var(--surface-rgb), 0) 52%),
    linear-gradient(0deg, rgba(var(--shadow-rgb), 0.24) 0%, rgba(var(--shadow-rgb), 0) 42%);
  pointer-events: none;
}
.preview-avatar.user {
  background: linear-gradient(135deg, #c47f8d, #ba71a2);
}

/* —— 文字大小滑块 —— */
.font-scale-row { display: flex; align-items: center; gap: 10px; }
.font-scale-edge { font-size: calc(12px * var(--font-scale)); opacity: .55; flex-shrink: 0; }
.font-scale-slider {
  flex: 1; min-width: 0;
  -webkit-appearance: none; appearance: none;
  height: 4px; border-radius: 2px;
  background: rgba(var(--surface-rgb), 0.12);
  outline: none; cursor: pointer;
}
.font-scale-slider::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 2px 6px rgba(var(--accent-rgb), 0.4);
  transition: transform .12s;
}
.font-scale-slider::-webkit-slider-thumb:hover { transform: scale(1.15); }
.font-scale-slider::-moz-range-thumb {
  width: 16px; height: 16px; border-radius: 50%; border: none;
  background: var(--accent); cursor: pointer;
  box-shadow: 0 2px 6px rgba(var(--accent-rgb), 0.4);
}
.font-scale-value {
  font-size: calc(12px * var(--font-scale)); font-weight: 600; min-width: 44px; text-align: right;
  color: var(--accent); font-variant-numeric: tabular-nums; flex-shrink: 0;
}

/* —— 配色方案 —— */
.theme-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.theme-card {
  cursor: pointer; border-radius: 10px; overflow: hidden;
  border: 2px solid rgba(var(--surface-rgb), 0.1);
  transition: border-color .15s, transform .12s;
}
.theme-card:hover { transform: translateY(-2px); border-color: rgba(var(--accent-rgb), 0.4); }
.theme-card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.theme-preview { height: 64px; padding: 8px; display: flex; flex-direction: column; gap: 6px; justify-content: center; }
.tp-bar { display: flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 6px; }
.tp-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.tp-text { font-size: calc(11px * var(--font-scale)); }
.tp-chip { width: 24px; height: 4px; border-radius: 2px; margin-left: 8px; }
.theme-name { padding: 6px 8px; text-align: center; font-size: calc(12px * var(--font-scale)); }

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

.modal-bg { position: fixed; inset: 0; background: rgba(var(--shadow-rgb), 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 520px; padding: 20px; }
.modal-title { font-size: calc(16px * var(--font-scale)); font-weight: 600; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; label { display: flex; flex-direction: column; gap: 4px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); &.full { grid-column: 1 / -1; } } }
.actions-row { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* —— 能力参数预设 —— */
.preset-grid { display: grid; gap: 10px; margin-top: 10px; }
.preset-card {
  padding: 12px 14px; border-radius: 10px;
  background: rgba(var(--surface-rgb), 0.03); border: 1px solid rgba(var(--surface-rgb), 0.08);
  &.off { opacity: .5; }
}
.pc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pc-type { font-size: calc(10px * var(--font-scale)); padding: 2px 8px; border-radius: 4px; background: rgba(var(--accent-rgb), 0.15); color: var(--accent-light); border: 1px solid rgba(var(--accent-rgb), 0.25); }
.pc-label { font-size: calc(13px * var(--font-scale)); font-weight: 600; }
.pc-tag { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pc-note { font-size: calc(12px * var(--font-scale)); opacity: .6; margin-top: 4px; }
.pc-counts { display: flex; gap: 8px; margin-top: 6px; }
.cnt { font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 999px; }
.cnt.red { background: rgba(255,110,110,0.14); color: #ffb0b0; }
.cnt.blue { background: rgba(var(--accent-rgb), 0.14); color: var(--accent-light); }
.pc-actions { display: flex; gap: 6px; margin-top: 8px; }

.preset-modal { width: 660px; max-height: 84vh; overflow-y: auto; }
.chk-full { flex-direction: row !important; align-items: center; gap: 6px; grid-column: 1 / -1; input { accent-color: var(--accent); } }
.preset-block { margin-top: 16px; padding-top: 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.07); }
.preset-block-title { display: flex; align-items: center; gap: 8px; font-size: calc(12px * var(--font-scale)); font-weight: 600; margin-bottom: 10px; }
.kv-row { display: flex; gap: 8px; margin-bottom: 8px; align-items: flex-start;
  input.mono, select.mono { flex: 0 0 40%; }
  input:not(.mono), select:not(.mono) { flex: 1; min-width: 0; }
  .btn { flex-shrink: 0; }
  textarea { flex: 1; resize: vertical; }
}
.icon-btn { padding: 8px 9px !important; font-size: calc(12px * var(--font-scale)); line-height: 1; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 18px; border-radius: 8px; font-size: calc(13px * var(--font-scale)); z-index: 9999;
  background: rgba(40, 40, 50, 0.95); border: 1px solid rgba(var(--surface-rgb), 0.15);
  color: #e6e6ee; box-shadow: 0 4px 16px rgba(var(--shadow-rgb), 0.4);
  backdrop-filter: blur(6px);
}
.toast.ok { border-color: rgba(110, 255, 160, 0.4); color: #b0ffc8; }
.toast.err { border-color: rgba(255, 110, 110, 0.4); color: #ffb0b0; }
.fade-enter-active, .fade-leave-active { transition: opacity .2s ease, transform .2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateX(-50%) translateY(8px); }
</style>

