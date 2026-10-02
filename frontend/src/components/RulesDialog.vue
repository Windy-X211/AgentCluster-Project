<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRulesStore, useAgentStore, useCanvasStore } from '@/stores'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const rules = useRulesStore()
const agents = useAgentStore()
const canvases = useCanvasStore()

const draft = ref('')
const draftIds = ref<number[] | null>(null) // null=全部
const editId = ref<number | null>(null)
const scopeOpen = ref(false)

const canvasId = computed(() => canvases.current?.id ?? null)
const canvasName = computed(() => canvases.current?.name ?? '')

// 仅展示当前画布节点上的智能体（规章制度作用于该集群）
const canvasAgentIds = computed(() => {
  const nodes = canvases.current?.nodes || []
  return new Set(nodes.map((n: any) => n.agent_id))
})
const canvasAgents = computed(() =>
  agents.list.filter((a: any) => canvasAgentIds.value.has(a.id))
)

function scopeLabel(ids: number[] | null): string {
  if (ids == null) return `全部智能体（${canvasAgents.value.length}）`
  if (!ids.length) return '未选智能体（不注入）'
  if (ids.length <= 2) {
    const names = ids.map(id => agents.list.find((a: any) => a.id === id)?.name ?? id)
    return names.join('、')
  }
  return `指定 ${ids.length} 个智能体`
}

async function reload() {
  if (canvasId.value == null) { rules.list = []; return }
  await rules.fetch(canvasId.value)
}

async function addRule() {
  const content = draft.value.trim()
  if (!content || canvasId.value == null) return
  await rules.create({ content, agent_ids: draftIds.value, canvas_id: canvasId.value })
  draft.value = ''
  draftIds.value = null
}

function startEdit(r: any) {
  editId.value = r.id
  draft.value = r.content
  draftIds.value = r.agent_ids
}

async function commitEdit() {
  if (editId.value == null) return
  const content = draft.value.trim()
  if (!content) return
  await rules.update(editId.value, { content, agent_ids: draftIds.value })
  editId.value = null
  draft.value = ''
  draftIds.value = null
}

function cancelEdit() {
  editId.value = null
  draft.value = ''
  draftIds.value = null
}

function toggleAgentInDraft(id: number) {
  if (draftIds.value == null) draftIds.value = []
  const i = draftIds.value.indexOf(id)
  if (i >= 0) draftIds.value.splice(i, 1)
  else draftIds.value.push(id)
}

function setScopeAll() { draftIds.value = null }
function setScopeSome() { if (draftIds.value == null) draftIds.value = [] }

async function removeRule(id: number) {
  if (!confirm('删除该条规章制度？')) return
  await rules.remove(id)
  if (editId.value === id) cancelEdit()
}

watch(() => props.open, async (v) => {
  if (v) {
    if (!agents.list.length) await agents.fetch()
    if (!canvases.list.length) await canvases.fetch()
    await reload()
    scopeOpen.value = false
  }
})

// 切换画布 → 重载该画布规则
watch(canvasId, async () => {
  if (props.open) {
    cancelEdit()
    scopeOpen.value = false
    await reload()
  }
})

onMounted(() => {
  if (props.open) {
    void agents.fetch().then(() => reload())
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="rules-overlay" @click.self="emit('close')">
      <div class="rules-dialog glass">
        <div class="rules-head">
          <span class="rules-title">📋 规章制度</span>
          <span class="rules-hint">画布「{{ canvasName || '未选择' }}」· 按画布独立设置，注入该集群智能体</span>
          <button class="btn ghost sm" type="button" @click="emit('close')">✕</button>
        </div>

        <div v-if="canvasId == null" class="rules-empty">
          请先创建/选择一张画布，规章制度按画布独立设置。
        </div>

        <template v-else>
          <!-- 新增 / 编辑 -->
          <div class="rules-form">
            <textarea class="input rules-input" v-model="draft" rows="3"
                      :placeholder="editId != null ? '编辑规章制度内容…' : '输入一条规章制度（集群工作流程 / 规则），Ctrl+Enter 或点右侧按钮添加…'"
                      @keydown.ctrl.enter.prevent="editId != null ? commitEdit() : addRule()" />
            <div class="form-row">
              <div class="scope">
                <button class="btn ghost sm" type="button" @click="scopeOpen = !scopeOpen">
                  注入：{{ scopeLabel(draftIds) }} ▾
                </button>
                <div v-if="scopeOpen" class="scope-menu">
                  <button type="button" class="scope-opt" :class="{ on: draftIds == null }" @click="setScopeAll(); scopeOpen = false">
                    本画布全部智能体（{{ canvasAgents.length }}）
                  </button>
                  <button type="button" class="scope-opt" :class="{ on: draftIds != null }" @click="setScopeSome">
                    指定智能体…
                  </button>
                  <template v-if="draftIds != null">
                    <div class="scope-agents">
                      <label v-for="a in canvasAgents" :key="a.id" class="scope-agent">
                        <input type="checkbox" :checked="draftIds.includes(a.id)" @change="toggleAgentInDraft(a.id)" />
                        <span>{{ a.name }}</span>
                      </label>
                      <div v-if="!canvasAgents.length" class="scope-empty">该画布暂无智能体节点</div>
                    </div>
                  </template>
                </div>
              </div>
              <div class="form-actions">
                <button v-if="editId != null" class="btn ghost sm" type="button" @click="cancelEdit">取消</button>
                <button class="btn sm" type="button"
                        :disabled="!draft.trim()"
                        @click="editId != null ? commitEdit() : addRule()">
                  {{ editId != null ? '保存修改' : '＋ 添加' }}
                </button>
              </div>
            </div>
          </div>

          <!-- 列表 -->
          <div class="rules-list">
            <div v-for="(r, i) in rules.list" :key="r.id" class="rule-item" :class="{ editing: editId === r.id }">
              <span class="rule-idx">{{ i + 1 }}</span>
              <div class="rule-body">
                <div class="rule-text">{{ r.content }}</div>
                <div class="rule-meta">
                  <span class="tag" :class="{ all: r.agent_ids == null }">{{ scopeLabel(r.agent_ids) }}</span>
                  <span v-if="r.enabled === false" class="tag off">已停用</span>
                </div>
              </div>
              <div class="rule-actions">
                <button class="btn ghost sm" type="button" title="编辑" @click="startEdit(r)">✎</button>
                <button class="btn ghost sm" type="button" title="删除" @click="removeRule(r.id)">🗑</button>
              </div>
            </div>
            <div v-if="!rules.list.length" class="rules-empty">
              本画布还没有规章制度。在上方按条添加，例如：「先输出大纲再写正文」「禁止编造数据」。
            </div>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rules-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.45); backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.rules-dialog {
  width: min(720px, 100%); max-height: min(80vh, 720px);
  display: flex; flex-direction: column; padding: 16px 18px; gap: 12px;
}
.rules-head { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.rules-title { font-size: 15px; font-weight: 600; }
.rules-hint { font-size: 11px; opacity: .55; flex: 1; }
.rules-form { display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }
.rules-input { resize: vertical; min-height: 64px; font-family: inherit; line-height: 1.5; }
.form-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.form-actions { display: flex; gap: 6px; margin-left: auto; }
.scope { position: relative; }
.scope-menu {
  position: absolute; top: calc(100% + 4px); left: 0; z-index: 20;
  min-width: 260px; max-height: 280px; overflow-y: auto;
  padding: 6px; display: flex; flex-direction: column; gap: 2px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  /* 下拉弹层需要实底，覆盖 glass-soft 的半透明，防止与底层 modal 文字重叠 */
  background: rgba(18, 24, 38, 0.98) !important;
  backdrop-filter: blur(16px) saturate(140%);
  -webkit-backdrop-filter: blur(16px) saturate(140%);
  border: 1px solid rgba(110,168,255,0.25);
}
.scope-opt {
  appearance: none; border: 0; background: transparent; color: #e8ecf4;
  font-size: 12px; text-align: left; padding: 7px 10px; border-radius: 6px; cursor: pointer;
}
.scope-opt:hover { background: rgba(110,168,255,0.15); }
.scope-opt.on { background: rgba(110,168,255,0.2); color: #a8c8ff; }
.scope-agents { border-top: 1px dashed rgba(255,255,255,0.1); margin-top: 4px; padding-top: 4px; max-height: 180px; overflow-y: auto; }
.scope-agent {
  display: flex; align-items: center; gap: 8px; font-size: 12px;
  padding: 5px 8px; border-radius: 6px; cursor: pointer;
}
.scope-agent:hover { background: rgba(255,255,255,0.06); }
.scope-agent input { accent-color: #6ea8ff; }
.scope-empty { font-size: 11px; opacity: .5; padding: 8px; }
.rules-list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.rule-item {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 10px 12px; border-radius: 10px;
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
}
.rule-item.editing { border-color: rgba(110,168,255,0.45); background: rgba(110,168,255,0.1); }
.rule-idx {
  flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%;
  background: rgba(110,168,255,0.2); color: #a8c8ff;
  font-size: 11px; display: flex; align-items: center; justify-content: center;
  font-family: Consolas, monospace; margin-top: 1px;
}
.rule-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.rule-text { font-size: 13px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
.rule-meta { display: flex; gap: 6px; flex-wrap: wrap; }
.tag {
  font-size: 10px; padding: 2px 7px; border-radius: 4px;
  background: rgba(180,114,255,0.18); color: #d4a8ff;
}
.tag.all { background: rgba(110,168,255,0.18); color: #a8c8ff; }
.tag.off { background: rgba(255,80,80,0.15); color: #ffb0b0; }
.rule-actions { display: flex; gap: 4px; flex-shrink: 0; }
.rules-empty { text-align: center; font-size: 12px; opacity: .5; padding: 28px 16px; line-height: 1.7; }
.sm { padding: 3px 8px; font-size: 11px; }
</style>
