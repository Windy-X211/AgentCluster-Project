<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useAgentStore, useSettingsStore, useModelStore } from '@/stores'
import AgentEditDialog from './AgentEditDialog.vue'

const props = defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()

const agents = useAgentStore()
const settings = useSettingsStore()
const models = useModelStore()
const editing = ref<any>(null)
const showHidden = ref(true)

// 能力标签折叠：默认最多显示 MAX_TAGS 个，点击 +N 展开全部，再点收起
const MAX_TAGS = 6
const expandedIds = ref<Set<number>>(new Set())
function toggleExpand(id: number) {
  const s = new Set(expandedIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expandedIds.value = s
}
// 取某类能力的中文标签（带 _ / - 别名兜底，兼容 novel_writing ↔ novel-writing）
function labelOf(type: 'commands' | 'skills' | 'plugins', key: string): string {
  const meta: any = type === 'commands' ? settings.commandsMeta
                  : type === 'skills' ? settings.skillsMeta
                                      : settings.pluginsMeta
  let m = meta[key]
  if (!m && key.includes('_')) m = meta[key.replace(/_/g, '-')]
  if (!m && key.includes('-')) m = meta[key.replace(/-/g, '_')]
  return m?.label || key
}
type TagItem = { key: string; label: string; kind: '' | 'skill' | 'plugin' }
function allTags(a: any): TagItem[] {
  const out: TagItem[] = []
  for (const c of a.commands || []) out.push({ key: `c:${c}`, label: labelOf('commands', c), kind: '' })
  for (const s of a.skills || []) out.push({ key: `s:${s}`, label: labelOf('skills', s), kind: 'skill' })
  for (const p of a.plugins || []) out.push({ key: `p:${p}`, label: labelOf('plugins', p), kind: 'plugin' })
  return out
}
function visibleTags(a: any): TagItem[] {
  const all = allTags(a)
  return expandedIds.value.has(a.id) ? all : all.slice(0, MAX_TAGS)
}
function extraCount(a: any): number {
  return Math.max(0, allTags(a).length - MAX_TAGS)
}

// 右键菜单
const ctx = ref<{ a: any; x: number; y: number } | null>(null)
function onCtx(e: MouseEvent, a: any) {
  e.preventDefault(); e.stopPropagation()
  ctx.value = { a, x: e.clientX, y: e.clientY }
}
function closeCtx() { ctx.value = null }
function ctxEdit() {
  if (!ctx.value) return
  editing.value = ctx.value.a
  closeCtx()
}
async function ctxDelete() {
  if (!ctx.value) return
  const a = ctx.value.a
  closeCtx()
  if (window.confirm(`确定删除智能体「${a.name}」吗？其在画布上的节点与连线会一并清理。`)) {
    await agents.remove(a.id)
  }
}
onMounted(() => window.addEventListener('click', closeCtx))
onBeforeUnmount(() => window.removeEventListener('click', closeCtx))

function openNew() {
  const a = settings.applyDefaults({ name: '', system_prompt: '', model: '', commands: [], skills: [], plugins: [], trigger: { type: 'manual', cron: '', message: '' }, perception_scope: 'link' })
  editing.value = a
}

// —— 列表内拖拽排序 —— 与"拖到画布"共用同一次拖拽：dragstart 仍写入 'agent' 数据
// 供 CanvasBoard 接收；落点是列表内其它项时改为重排顺序。
const dragId = ref<number | null>(null)
const overId = ref<number | null>(null)
const overPos = ref<'before' | 'after'>('before')

function onDragStart(e: DragEvent, a: any) {
  dragId.value = a.id
  e.dataTransfer!.setData('agent', JSON.stringify(a))
  // 部分浏览器需 effectAllowed 才能触发 drop
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}
function onDragEnd() { dragId.value = null; overId.value = null }
function onItemDragOver(e: DragEvent, a: any) {
  if (dragId.value == null) return          // 不是从列表发起的拖拽，交给画布处理
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  overPos.value = (e.clientY - rect.top) < rect.height / 2 ? 'before' : 'after'
  overId.value = a.id
}
function onItemDragLeave(a: any) {
  if (overId.value === a.id) overId.value = null
}
function onItemDrop(e: DragEvent, a: any) {
  if (dragId.value == null) return           // 画布拖拽落点不在这里处理
  e.preventDefault(); e.stopPropagation()
  const srcId = dragId.value
  dragId.value = null; overId.value = null
  if (srcId === a.id) return
  const cur = agents.list.map(x => x.id)
  const from = cur.indexOf(srcId)
  if (from < 0) return
  const without = cur.filter(id => id !== srcId)
  let to = without.indexOf(a.id)
  if (to < 0) to = without.length
  if (overPos.value === 'after') to++
  without.splice(to, 0, srcId)
  agents.reorder(without)
}
</script>

<template>
  <div class="agent-list" :class="{ collapsed: props.collapsed }">
    <!-- 折叠态：只显示一条窄条 + 展开按钮 -->
    <template v-if="props.collapsed">
      <button class="expand-strip" @click="emit('toggle')" title="展开智能体列表">
        <span class="expand-arrow">▸</span>
        <span class="expand-label">智能体</span>
      </button>
    </template>

    <!-- 展开态：完整列表 -->
    <template v-else>
      <div class="section-title">
        <div class="title-left">
          <button class="collapse-btn" @click="emit('toggle')" title="折叠智能体列表">◂</button>
          <span>智能体</span>
        </div>
        <button class="btn ghost" @click="openNew">+ 新建</button>
      </div>
      <div class="section-title">
        <span>能力标签</span>
        <label class="toggle">
          <input type="checkbox" v-model="showHidden" /> {{ showHidden ? '显示' : '隐藏' }}
        </label>
      </div>
      <div class="list">
        <div class="agent-item glass-soft"
          v-for="a in agents.list" :key="a.id"
          draggable="true"
          :class="{ dragging: dragId === a.id, 'drop-before': overId === a.id && overPos === 'before' && dragId !== a.id, 'drop-after': overId === a.id && overPos === 'after' && dragId !== a.id }"
          @dragstart="onDragStart($event, a)"
          @dragend="onDragEnd"
          @dragover="onItemDragOver($event, a)"
          @dragleave="onItemDragLeave(a)"
          @drop="onItemDrop($event, a)"
          @contextmenu.prevent="onCtx($event, a)">
          <div class="row">
            <div class="avatar">{{ (a.name || '?').slice(0, 1).toUpperCase() }}</div>
            <div class="meta">
              <div class="name">{{ a.name }}</div>
              <div class="desc" v-if="a.description">{{ a.description }}</div>
            </div>
          </div>
          <div class="tags" v-if="showHidden">
            <span v-for="t in visibleTags(a)" :key="t.key" class="tag" :class="t.kind">{{ t.label }}</span>
            <button v-if="extraCount(a) > 0" class="tag more" @click.stop="toggleExpand(a.id)" :title="expandedIds.has(a.id) ? '收起标签' : '展开全部标签'">
              {{ expandedIds.has(a.id) ? '收起' : `+${extraCount(a)}` }}
            </button>
          </div>
        </div>
        <div class="empty" v-if="!agents.list.length">还没有智能体，点击"+ 新建"</div>
      </div>
    </template>

    <AgentEditDialog v-if="editing" :agent="editing" @close="editing = null" />

    <!-- 右键菜单（Teleport 到 body，fixed 定位） -->
    <Teleport to="body">
      <div v-if="ctx" class="agent-ctx glass" :style="{ left: ctx.x + 'px', top: ctx.y + 'px' }" @click.stop>
        <div class="ctx-title">{{ ctx.a.name }}</div>
        <div class="ctx-item" @click="ctxEdit">✏️ 编辑</div>
        <div class="ctx-item danger" @click="ctxDelete">🗑 删除</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.agent-list { overflow: hidden; display: flex; flex-direction: column; }
.list { flex: 1; overflow-y: auto; padding: 4px 10px 12px; }
.agent-item { padding: 10px; margin-bottom: 8px; cursor: grab; user-select: none; }
.agent-item:active { cursor: grabbing; }
.agent-item.dragging { opacity: .4; }
/* 拖拽落点指示线：上/下各一条高亮边 */
.agent-item.drop-before { box-shadow: 0 -2px 0 0 #6ea8ff, 0 1px 0 0 rgba(110,168,255,0.25); }
.agent-item.drop-after { box-shadow: 0 2px 0 0 #6ea8ff, 0 -1px 0 0 rgba(110,168,255,0.25); }
.row { display: flex; gap: 10px; align-items: center; }
.avatar { width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, #6ea8ff, #b472ff); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 12px; flex-shrink: 0; }
.name { font-size: 13px; font-weight: 500; }
.desc { font-size: 11px; opacity: .6; margin-top: 2px; }
.tags { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 4px; }
.tag.more {
  padding: 2px 8px; border-radius: 999px; font-size: 11px; cursor: pointer; line-height: 1.4;
  background: rgba(255,255,255,0.06); color: rgba(232,236,244,0.7);
  border: 1px dashed rgba(255,255,255,0.18);
  &:hover { background: rgba(110,168,255,0.18); color: #a8c8ff; border-color: rgba(110,168,255,0.4); }
}
.empty { text-align: center; padding: 40px 0; opacity: .5; font-size: 13px; }
.toggle { display: flex; align-items: center; gap: 4px; font-size: 11px; cursor: pointer; }

/* 标题行左侧分组（折叠按钮 + 标题） */
.title-left { display: flex; align-items: center; gap: 6px; min-width: 0; }
.collapse-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; color: rgba(232,236,244,0.7); font-size: 11px; cursor: pointer; line-height: 1; transition: all .15s; }
.collapse-btn:hover { background: rgba(255,255,255,0.08); color: #fff; border-color: rgba(110,168,255,0.4); }

/* 折叠态窄条 */
.expand-strip { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; background: transparent; border: 0; cursor: pointer; color: rgba(232,236,244,0.6); transition: all .15s; padding: 0; }
.expand-strip:hover { color: #fff; background: rgba(255,255,255,0.05); }
.expand-arrow { font-size: 16px; line-height: 1; }
.expand-label { writing-mode: vertical-rl; font-size: 11px; letter-spacing: 2px; opacity: .8; }

/* 右键菜单 */
.agent-ctx { position: fixed; min-width: 140px; padding: 6px; z-index: 9999; box-shadow: 0 12px 40px rgba(0,0,0,0.5); }
.ctx-title { font-size: 11px; padding: 6px 10px; color: rgba(232,236,244,0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(255,255,255,0.08); margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ctx-item { padding: 8px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; transition: background .15s; user-select: none;
  &:hover { background: rgba(110,168,255,0.18); }
  &.danger:hover { background: rgba(255,80,80,0.18); color: #ff9090; }
}
</style>
