<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useSettingsStore } from '@/stores'
import { api } from '@/api'
import ParamSelect from '@/components/ParamSelect.vue'

const settings = useSettingsStore()

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
function coerceToOption(row: any): void {
  const opts = paramValueOptions(row)
  if (!opts) return
  const cur = String(row.value ?? '').toLowerCase()
  const m = opts.find((o: any) => String(o.value).toLowerCase() === cur)
  if (m) {
    row.value = m.value
  } else {
    const p = currentTargetParams.value.find((x: any) => x.name === row.key)
    const def = p?.default
    const defOpt = (def !== undefined && def !== null && def !== '')
      ? opts.find((o: any) => String(o.value).toLowerCase() === String(def).toLowerCase())
      : null
    row.value = defOpt ? defOpt.value : opts[0].value
  }
}
function onFixedKeyChange(row: any) { coerceToOption(row) }
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

onMounted(loadPresets)
</script>

<template>
  <div class="pp-panel">
    <div class="panel glass">
      <div class="panel-title">
        <span>🔐 能力参数预设</span>
        <button class="btn" @click="openNewPreset">+ 新建预设</button>
      </div>
      <div class="hint" style="margin-bottom: 10px">
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

    <!-- 预设编辑弹窗 -->
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
            <span class="hint">敏感值加密保存 · 明确选项明文保存 · 模型不可见、不可更改</span>
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
            <span class="hint">文字描述仅供模型参考选择</span>
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
        <div v-if="!currentTargetParams.length" class="hint" style="margin-top: 6px">
          该能力未声明参数，可手动输入参数名。
        </div>

        <div class="modal-actions">
          <button class="btn ghost" @click="closePresetDialog">取消</button>
          <button class="btn" @click="savePreset" :disabled="presetSaving">
            {{ presetSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pp-panel { display: flex; flex-direction: column; gap: 12px; }
.panel { padding: 18px; }
.panel-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
.hint { font-size: calc(12px * var(--font-scale)); opacity: .6; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); }
.sm { padding: 3px 10px; font-size: calc(11px * var(--font-scale)); }

/* —— preset grid —— */
.preset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-top: 6px; }
.preset-card {
  border-radius: 10px; padding: 12px 14px;
  background: rgba(var(--surface-rgb), 0.05); border: 1px solid rgba(var(--surface-rgb), 0.1);
  transition: opacity .15s;
  &.off { opacity: .45; }
}
.pc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pc-type { font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 4px; background: rgba(var(--surface-rgb), 0.1); }
.pc-label { font-weight: 600; font-size: calc(13px * var(--font-scale)); }
.pc-tag { font-size: calc(11px * var(--font-scale)); opacity: .5; flex: 1; text-align: right; }
.pc-note { font-size: calc(11px * var(--font-scale)); opacity: .6; margin-top: 4px; }
.pc-counts { display: flex; gap: 8px; margin-top: 8px; }
.cnt { font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 4px; }
.cnt.red { background: rgba(220, 100, 100, 0.15); color: #e09090; }
.cnt.blue { background: rgba(100, 160, 255, 0.15); color: #8ab4ff; }
.pc-actions { display: flex; gap: 6px; margin-top: 10px; justify-content: flex-end; }

/* —— modal —— */
.modal-bg {
  position: fixed; inset: 0; z-index: 2000;
  background: rgba(var(--shadow-rgb), 0.4); backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 640px; max-width: 92vw; max-height: 88vh; overflow-y: auto;
  border-radius: 14px; padding: 18px 20px; display: flex; flex-direction: column; gap: 12px;
  box-shadow: 0 20px 60px rgba(var(--shadow-rgb), 0.5);
}
.preset-modal { width: 680px; }
.modal-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 4px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.grid label { display: flex; flex-direction: column; gap: 4px; font-size: calc(11px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); }
.grid label.full { grid-column: 1 / -1; }
.grid label.chk-full { grid-column: 1 / -1; flex-direction: row; align-items: center; gap: 6px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; padding-top: 8px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); }

/* —— preset blocks —— */
.preset-block {
  border-radius: 8px; padding: 12px;
  background: rgba(var(--surface-rgb), 0.03); border: 1px solid rgba(var(--surface-rgb), 0.08);
}
.preset-block + .preset-block { margin-top: 8px; }
.preset-block-title {
  font-size: calc(12px * var(--font-scale)); font-weight: 600; margin-bottom: 8px;
  display: flex; align-items: center; gap: 8px;
}
.kv-row { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }
.kv-row:last-of-type { margin-bottom: 0; }
.kv-row .input { flex: 1; min-width: 0; }
.mono { font-family: Consolas, monospace; }
.icon-btn { padding: 3px 6px; }

textarea.input { min-height: 32px; resize: vertical; }
</style>
