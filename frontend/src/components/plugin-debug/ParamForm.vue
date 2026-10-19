<script setup lang="ts">
import { computed, ref } from 'vue'
import { usePluginDebugStore } from '@/stores'
import { isSensitiveParam } from '@/stores/pluginDebug'
import type { ParamDefinition } from '@/api'
import ParamSelect from '@/components/ParamSelect.vue'
import HoverTip from '@/components/HoverTip.vue'

const store = usePluginDebugStore()

const plugin = computed(() => store.selectedPlugin)

// 已添加的参数（按插件声明顺序展示）
const addedParams = computed(() => {
  if (!plugin.value) return []
  return plugin.value.parameters.filter((d) => store.paramForm[d.name])
})

// 尚未添加、可下拉选择的参数（展示方式与能力参数预设一致）
const availableOptions = computed(() => {
  if (!plugin.value) return []
  return plugin.value.parameters
    .filter((d) => !store.paramForm[d.name])
    .map((d) => ({
      name: d.name,
      desc: d.description || d.type || '',
    }))
})

const addParamValue = ref('')

function onAddParam(name: string) {
  if (!name) return
  store.addParam(name)
  addParamValue.value = ''
}

type Control = 'enum' | 'boolean' | 'number' | 'json' | 'text' | 'password'

function controlType(d: ParamDefinition): Control {
  if (Array.isArray(d.enum) && d.enum.length) return 'enum'
  const t = d.type || 'string'
  if (t === 'boolean') return 'boolean'
  if (t === 'integer' || t === 'number') return 'number'
  if (t === 'object' || t === 'array') return 'json'
  if (t === 'string') return isSensitiveParam(d.name) ? 'password' : 'text'
  return 'text'
}

function exampleText(d: ParamDefinition): string {
  if (d.example === undefined || d.example === null) return ''
  return typeof d.example === 'string' ? d.example : JSON.stringify(d.example)
}

function placeholderFor(d: ParamDefinition): string {
  const ex = exampleText(d)
  if (ex) return ex
  const t = controlType(d)
  if (t === 'json') return '按 JSON 格式填写，如 {"k": 1}'
  if (t === 'number') return '请输入数字'
  return '请输入'
}

function typeLabel(d: ParamDefinition): string {
  if (Array.isArray(d.enum) && d.enum.length) return `enum · ${d.type || 'string'}`
  return d.type || 'string'
}

function onInput(name: string, e: Event) {
  const el = e.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
  store.setParamValue(name, el.value)
}
</script>

<template>
  <div class="param-form">
    <!-- 固定头部（不随滚轮移动）：工具描述（默认折叠）+ 运行按钮 + 参数添加下拉框 -->
    <div class="head">
      <details v-if="plugin" class="tool-desc">
        <summary class="tool-desc-summary">
          <span class="tool-desc-label">工具描述</span>
          <span class="caret">▾</span>
        </summary>
        <pre class="tool-desc-text">{{ plugin.tool_description || '该插件未提供工具描述' }}</pre>
      </details>

      <div class="run-row">
        <slot name="run" />
        <div v-if="availableOptions.length" class="add-param">
          <ParamSelect
            :modelValue="addParamValue"
            @update:modelValue="onAddParam"
            :options="availableOptions"
            placeholder="+ 添加参数"
          />
        </div>
      </div>
    </div>

    <!-- 可滚动区：参数列表 -->
    <div class="body">
      <template v-if="plugin">
        <div v-if="!addedParams.length" class="empty">该插件没有参数</div>
        <div v-for="d in addedParams" :key="d.name" class="param">
          <div class="param-head">
            <span class="pname">{{ d.name }}<span v-if="d.required" class="req">*</span></span>
            <span class="ptype">{{ typeLabel(d) }}</span>
            <button
              v-if="!d.required"
              class="btn sm ghost remove-btn"
              title="移除该参数"
              @click="store.removeParam(d.name)"
            >×</button>
          </div>
          <HoverTip v-if="d.description" class="pdesc" :text="d.description">{{ d.description }}</HoverTip>

          <select
            v-if="controlType(d) === 'enum'"
            class="input"
            :value="store.paramForm[d.name]?.value ?? ''"
            @change="onInput(d.name, $event)"
          >
            <option value="">（请选择）</option>
            <option v-for="(opt, i) in d.enum" :key="i" :value="String(opt)">{{ String(opt) }}</option>
          </select>

          <select
            v-else-if="controlType(d) === 'boolean'"
            class="input"
            :value="store.paramForm[d.name]?.value ?? ''"
            @change="onInput(d.name, $event)"
          >
            <option value="">（请选择）</option>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>

          <input
            v-else-if="controlType(d) === 'number'"
            class="input"
            type="number"
            :value="store.paramForm[d.name]?.value ?? ''"
            :placeholder="placeholderFor(d)"
            @input="onInput(d.name, $event)"
          />

          <input
            v-else-if="controlType(d) === 'password'"
            class="input"
            type="password"
            :value="store.paramForm[d.name]?.value ?? ''"
            :placeholder="placeholderFor(d)"
            @input="onInput(d.name, $event)"
          />

          <textarea
            v-else-if="controlType(d) === 'json'"
            class="input ta"
            rows="3"
            :value="store.paramForm[d.name]?.value ?? ''"
            :placeholder="placeholderFor(d)"
            @input="onInput(d.name, $event)"
          ></textarea>

          <input
            v-else
            class="input"
            type="text"
            :value="store.paramForm[d.name]?.value ?? ''"
            :placeholder="placeholderFor(d)"
            @input="onInput(d.name, $event)"
          />
        </div>
      </template>

      <div v-else class="empty">请从左侧选择一个插件</div>
    </div>
  </div>
</template>

<style scoped>
.param-form { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.head { flex-shrink: 0; display: flex; flex-direction: column; gap: 12px; }
.body { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding-top: 12px; }

.tool-desc {
  border-radius: 10px; overflow: hidden;
  background: rgba(110, 168, 255, 0.08); border: 1px solid rgba(110, 168, 255, 0.22);
}
.tool-desc-summary {
  display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none;
  padding: 9px 12px; list-style: none;
}
.tool-desc-summary::-webkit-details-marker { display: none; }
.tool-desc-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: .75; color: #cfe2ff; }
.caret { margin-left: auto; font-size: 10px; opacity: .6; transition: transform .2s; }
.tool-desc[open] .caret { transform: rotate(180deg); }
.tool-desc-text {
  font-family: inherit; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  color: #cfe2ff; margin: 0; padding: 0 12px 10px;
}

.run-row { display: flex; align-items: center; gap: 10px; }
.add-param { flex: 1; min-width: 0; }

.param { padding: 10px 12px; border-radius: 10px; background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.07); }
.param-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pname { font-size: 13px; font-weight: 600; font-family: Consolas, monospace; }
.req { color: #ff7a7a; margin-left: 3px; }
.ptype { margin-left: auto; font-size: 10px; opacity: .5; font-family: Consolas, monospace; }
.remove-btn { padding: 0 8px; font-size: 14px; line-height: 1.4; }
.pdesc { font-size: 12px; opacity: .65; margin-bottom: 6px; line-height: 1.4;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ta { min-height: 72px; resize: vertical; font-family: Consolas, monospace; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: 13px; }
</style>
