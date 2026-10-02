<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { RouterLink } from 'vue-router'
import { useModelStore, useSettingsStore } from '@/stores'
import { api } from '@/api'
import FileExplorerDialog from '@/components/FileExplorerDialog.vue'

const models = useModelStore()
const settings = useSettingsStore()

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
    .filter(([k]) => !added.has(k))
    .map(([k, v]) => ({
      key: k,
      label: v.label || k,
      desc: v.description || v.desc || '',
      extra: type === 'skills' ? (v.version ? `v${v.version}` : '') + (v.triggers?.length ? ` · ${v.triggers.join('/')}` : '') : '',
    }))
}
const currentPickerItems = computed(() => pickerOpen.value ? availableByType(pickerOpen.value) : [])

function pickerTitle(type: string) {
  return type === 'commands' ? '选择默认命令' : type === 'skills' ? '选择默认技能' : '选择默认插件'
}

function openPicker(type: string) { selectedKeys.value = new Set(); pickerOpen.value = type }
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
    .map((p: any) => typeof p === 'string' ? { name: p, desc: '' } : { name: p?.name || '', desc: p?.description || p?.desc || p?.label || '' })
    .filter((p: any) => p.name)
  const fromParams = norm(meta.parameters)
  return fromParams.length ? fromParams : norm(meta.params)
})
function paramOptions(currentKey: string) {
  const opts = currentTargetParams.value.slice()
  if (currentKey && !opts.some(p => p.name === currentKey)) {
    opts.unshift({ name: currentKey, desc: '自定义' })
  }
  return opts
}
function trimDesc(d: string) {
  if (!d) return ''
  return d.length > 50 ? d.slice(0, 50) + '…' : d
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
    fixed: (p.fixed || []).map((x: any) => ({ ...x })),
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
      fixed: (f.fixed || []).map((x: any) => ({ key: (x.key || '').trim(), value: x.value ?? '' })).filter((x: any) => x.key),
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
          <thead><tr><th>名称</th><th>类型</th><th>路径</th><th>数量</th><th style="width: 120px"></th></tr></thead>
          <tbody>
            <tr>
              <td>⚙️ 配置设置</td>
              <td class="tag-t">单文件</td>
              <td class="mono sm-cell">{{ storagePaths.settings_file }}</td>
              <td class="mono">1</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.root)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.root)">打开</button></td>
            </tr>
            <tr>
              <td>🧠 智能体</td>
              <td class="tag-t">每 agent 一个 json</td>
              <td class="mono sm-cell">{{ storagePaths.agents_dir }}</td>
              <td class="mono">{{ storagePaths.stats?.agents ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.agents_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.agents_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🎨 画布（集群编排）</td>
              <td class="tag-t">每 canvas 一个 json</td>
              <td class="mono sm-cell">{{ storagePaths.canvases_dir }}</td>
              <td class="mono">{{ storagePaths.stats?.canvases ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.canvases_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.canvases_dir)">打开</button></td>
            </tr>
            <tr>
              <td>💬 聊天记录</td>
              <td class="tag-t">每 conversation 一个 json</td>
              <td class="mono sm-cell">{{ storagePaths.conversations_dir }}</td>
              <td class="mono">{{ storagePaths.stats?.conversations ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.conversations_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.conversations_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🔌 模型接口</td>
              <td class="tag-t">每 interface 一个 json</td>
              <td class="mono sm-cell">{{ storagePaths.model_interfaces_dir }}</td>
              <td class="mono">{{ storagePaths.stats?.model_interfaces ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.model_interfaces_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.model_interfaces_dir)">打开</button></td>
            </tr>
            <tr>
              <td>⚡ 自定义技能</td>
              <td class="tag-t">每技能一个文件夹</td>
              <td class="mono sm-cell">{{ storagePaths.skills_dir }}</td>
              <td class="mono">{{ storagePaths.stats?.skills ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.skills_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.skills_dir)">打开</button></td>
            </tr>
            <tr>
              <td>🧩 自定义插件</td>
              <td class="tag-t">每插件一个文件夹</td>
              <td class="mono sm-cell">{{ storagePaths.plugins_dir }}</td>
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
            <span class="hint-empty">加密保存 · 模型不可见、不可更改</span>
          </div>
          <div v-for="(row, i) in presetForm.fixed" :key="'f' + i" class="kv-row">
            <select v-if="currentTargetParams.length && !row._manual" class="input mono" v-model="row.key">
              <option value="" disabled>选择参数…</option>
              <option v-for="p in paramOptions(row.key)" :key="p.name" :value="p.name">{{ p.name }}{{ p.desc ? ' — ' + trimDesc(p.desc) : '' }}</option>
            </select>
            <input v-else class="input mono" v-model="row.key" placeholder="参数名（如 smtp_user）" list="preset-param-list" />
            <button v-if="currentTargetParams.length" type="button" class="btn sm ghost icon-btn" @click="row._manual = !row._manual" :title="row._manual ? '从列表选择' : '手动输入'">{{ row._manual ? '▾' : '✎' }}</button>
            <input class="input" v-model="row.value" type="password" placeholder="固定值（如发件邮箱、授权码）" />
            <button class="btn sm ghost" @click="presetForm.fixed.splice(i, 1)">×</button>
          </div>
          <button class="btn sm ghost" @click="presetForm.fixed.push({ key: '', value: '' })">+ 添加固定参数</button>
        </div>

        <!-- 可选参数 -->
        <div class="preset-block">
          <div class="preset-block-title">
            <span>📝 可选参数</span>
            <span class="hint-empty">文字描述仅供模型参考选择</span>
          </div>
          <div v-for="(row, i) in presetForm.optional" :key="'o' + i" class="kv-row">
            <select v-if="currentTargetParams.length && !row._manual" class="input mono" v-model="row.key">
              <option value="" disabled>选择参数…</option>
              <option v-for="p in paramOptions(row.key)" :key="p.name" :value="p.name">{{ p.name }}{{ p.desc ? ' — ' + trimDesc(p.desc) : '' }}</option>
            </select>
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
.top { padding: 12px 20px; margin: 0 auto 16px; max-width: 1100px; display: flex; align-items: center; gap: 16px; h2 { font-size: 18px; } }
.panels { display: grid; gap: 16px; max-width: 1100px; margin: 0 auto; }
.panel { padding: 18px; }
.panel-title { font-size: 15px; font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.panel-title .hint { font-size: 11px; font-weight: normal; opacity: .5; }
.tbl { width: 100%; margin-top: 12px; border-collapse: collapse; th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 13px; } th { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: rgba(232,236,244,0.6); } }
.mono { font-family: Consolas, monospace; font-size: 12px; }
.sm-cell { max-width: 200px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.actions { display: flex; gap: 6px; justify-content: flex-end; }
.storage-loc { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.storage-loc-label { font-size: 12px; color: rgba(232,236,244,0.7); flex-shrink: 0; }
.storage-loc-input { flex: 1; min-width: 0; width: auto; }
.storage-loc-tip { font-size: 11px; opacity: .5; margin: 6px 0 0; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: 13px; }
.tag-t { font-size: 10px; padding: 2px 8px; border-radius: 4px; background: rgba(110,168,255,0.15); color: #a8c8ff; border: 1px solid rgba(110,168,255,0.25); font-family: Consolas, monospace; }

.cap-block { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.06); }
.cap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.cap-title { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: rgba(232,236,244,0.6); font-weight: 500; }
.sm { padding: 3px 10px; font-size: 11px; }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; min-height: 26px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px 3px 10px; border-radius: 999px; font-size: 12px; cursor: default;
  border: 1px solid rgba(255,255,255,0.15);
  &.blue   { background: rgba(110,168,255,0.18);  color: #a8c8ff; border-color: rgba(110,168,255,0.3); }
  &.purple { background: rgba(180,114,255,0.18); color: #d4a8ff; border-color: rgba(180,114,255,0.3); }
  &.green  { background: rgba(100,220,160,0.18); color: #80e4b8; border-color: rgba(100,220,160,0.3); }
  .x { margin-left: 4px; opacity: .7; font-weight: bold; cursor: pointer; padding: 0 2px; &:hover { opacity: 1; } }
}
.hint-empty { font-size: 12px; opacity: .45; padding: 4px 0; }
.rounds-input { width: 140px; }

.picker-mask {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.35);
  backdrop-filter: blur(2px);
  z-index: 2000;
  display: flex; align-items: center; justify-content: center;
}
.picker-popup {
  width: 460px; max-height: 70vh; padding: 18px;
  display: flex; flex-direction: column;
  box-shadow: 0 20px 60px rgba(0,0,0,0.6);
}
.picker-title { display: flex; justify-content: space-between; align-items: center;
  font-size: 14px; font-weight: 600; padding-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 10px; }
.picker-sub { font-size: 11px; opacity: .5; font-weight: normal; }
.picker-list { overflow-y: auto; flex: 1; }
.picker-item { display: flex; gap: 10px; padding: 10px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent; font-size: 12px;
  &:hover { background: rgba(255,255,255,0.04); }
  &.selected { background: rgba(110,168,255,0.15); border-color: rgba(110,168,255,0.3); }
  input { accent-color: #6ea8ff; flex-shrink: 0; margin-top: 3px; }
}
.pi-body { flex: 1; min-width: 0; }
.pi-label { font-size: 13px; font-weight: 500; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.pi-extra { font-size: 10px; opacity: .55; font-weight: normal; }
.pi-key { font-family: Consolas, monospace; font-size: 11px; opacity: .55; margin-top: 1px; }
.pi-desc { font-size: 11px; opacity: .6; margin-top: 3px; }
.picker-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); }

.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 520px; padding: 20px; }
.modal-title { font-size: 16px; font-weight: 600; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: rgba(232,236,244,0.7); &.full { grid-column: 1 / -1; } } }
.actions-row { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* —— 能力参数预设 —— */
.preset-grid { display: grid; gap: 10px; margin-top: 10px; }
.preset-card {
  padding: 12px 14px; border-radius: 10px;
  background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
  &.off { opacity: .5; }
}
.pc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pc-type { font-size: 10px; padding: 2px 8px; border-radius: 4px; background: rgba(110,168,255,0.15); color: #a8c8ff; border: 1px solid rgba(110,168,255,0.25); }
.pc-label { font-size: 13px; font-weight: 600; }
.pc-tag { font-size: 11px; opacity: .5; }
.pc-note { font-size: 12px; opacity: .6; margin-top: 4px; }
.pc-counts { display: flex; gap: 8px; margin-top: 6px; }
.cnt { font-size: 11px; padding: 2px 8px; border-radius: 999px; }
.cnt.red { background: rgba(255,110,110,0.14); color: #ffb0b0; }
.cnt.blue { background: rgba(110,168,255,0.14); color: #a8c8ff; }
.pc-actions { display: flex; gap: 6px; margin-top: 8px; }

.preset-modal { width: 660px; max-height: 84vh; overflow-y: auto; }
.chk-full { flex-direction: row !important; align-items: center; gap: 6px; grid-column: 1 / -1; input { accent-color: #6ea8ff; } }
.preset-block { margin-top: 16px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.07); }
.preset-block-title { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; margin-bottom: 10px; }
.kv-row { display: flex; gap: 8px; margin-bottom: 8px; align-items: flex-start;
  input.mono, select.mono { flex: 0 0 40%; }
  input:not(.mono) { flex: 1; min-width: 0; }
  .btn { flex-shrink: 0; }
  textarea { flex: 1; resize: vertical; }
}
.icon-btn { padding: 8px 9px !important; font-size: 12px; line-height: 1; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 18px; border-radius: 8px; font-size: 13px; z-index: 9999;
  background: rgba(40, 40, 50, 0.95); border: 1px solid rgba(255,255,255,0.15);
  color: #e6e6ee; box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  backdrop-filter: blur(6px);
}
.toast.ok { border-color: rgba(110, 255, 160, 0.4); color: #b0ffc8; }
.toast.err { border-color: rgba(255, 110, 110, 0.4); color: #ffb0b0; }
.fade-enter-active, .fade-leave-active { transition: opacity .2s ease, transform .2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateX(-50%) translateY(8px); }
</style>

