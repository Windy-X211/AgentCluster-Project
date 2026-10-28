<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useAgentStore, useSettingsStore, useModelStore } from '@/stores'
import { toolLabel } from '@/stores/conversation'
import { getAvatarGradient } from '@/utils/avatar'
import AgentEditDialog from './AgentEditDialog.vue'

const props = defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()

const agents = useAgentStore()
const settings = useSettingsStore()
const models = useModelStore()
const editing = ref<any>(null)
const SHOW_HIDDEN_KEY = 'agent-cluster.agent-list.show-hidden'
const showHidden = ref((() => {
  try {
    const raw = localStorage.getItem(SHOW_HIDDEN_KEY)
    return raw == null ? true : raw === '1'
  } catch { return true }
})())
watch(showHidden, (v) => {
  try { localStorage.setItem(SHOW_HIDDEN_KEY, v ? '1' : '0') } catch {}
})

// 分组视图状态：
//   null  → 组列表（收纳）视图
//   'all' → 全部组详情视图
//   number → 某个自定义组详情视图
const activeGroupId = ref<number | 'all' | null>(null)

// 组列表中每组默认折叠显示前 MAX_PREVIEW 个智能体名字，点击 +N 展开
const MAX_PREVIEW = 6
const expandedGroupIds = ref<Set<number | 'all'>>(new Set())
function toggleGroupExpand(id: number | 'all') {
  const s = new Set(expandedGroupIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expandedGroupIds.value = s
}

// 能力标签折叠：默认最多显示 MAX_TAGS 个，点击 +N 展开全部，再点收起
const MAX_TAGS = 6
const expandedIds = ref<Set<number>>(new Set())
function toggleExpand(id: number) {
  const s = new Set(expandedIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expandedIds.value = s
}
type TagItem = { key: string; label: string; kind: '' | 'skill' | 'plugin' }
function allTags(a: any): TagItem[] {
  const out: TagItem[] = []
  for (const c of a.commands || []) out.push({ key: `c:${c}`, label: toolLabel(c), kind: '' })
  for (const s of a.skills || [])   out.push({ key: `s:${s}`, label: toolLabel(s), kind: 'skill' })
  for (const p of a.plugins || [])  out.push({ key: `p:${p}`, label: toolLabel(p), kind: 'plugin' })
  return out
}
function visibleTags(a: any): TagItem[] {
  const all = allTags(a)
  return expandedIds.value.has(a.id) ? all : all.slice(0, MAX_TAGS)
}
function extraCount(a: any): number {
  return Math.max(0, allTags(a).length - MAX_TAGS)
}

// —— 组与智能体的计算 ——
function agentsOfGroup(id: number | 'all'): any[] {
  if (id === 'all') return agents.list
  return agents.list.filter(a => a.group_id === id)
}
function groupNameOf(groupId: number | null): string {
  if (groupId == null) return ''
  const g = agents.groups.find(x => x.id === groupId)
  return g?.name || ''
}
function previewAgents(id: number | 'all'): any[] {
  const all = agentsOfGroup(id)
  return expandedGroupIds.value.has(id) ? all : all.slice(0, MAX_PREVIEW)
}
function previewExtra(id: number | 'all'): number {
  return Math.max(0, agentsOfGroup(id).length - MAX_PREVIEW)
}
const currentAgents = computed(() =>
  activeGroupId.value == null ? [] : agentsOfGroup(activeGroupId.value)
)
const currentGroupName = computed(() => {
  if (activeGroupId.value === 'all') return '全部'
  if (activeGroupId.value == null) return ''
  const g = agents.groups.find(x => x.id === activeGroupId.value)
  return g?.name || '未命名组'
})

function enterGroup(id: number | 'all') { activeGroupId.value = id }
function backToGroups() { activeGroupId.value = null }

// 当前所在的组被删除时，自动回到组列表视图
watch(() => agents.groups, (gs) => {
  if (typeof activeGroupId.value === 'number' && !gs.find(g => g.id === activeGroupId.value)) {
    activeGroupId.value = null
  }
}, { deep: true })

// —— 新建 ——
// 组列表视图顶部按钮：新建组
async function openNewGroup() {
  const name = window.prompt('请输入组名')?.trim()
  if (!name) return
  await agents.createGroup({ name })
}
// 组详情视图顶部按钮 / 组右键菜单：新建智能体（自动归属当前组）
function openNew(groupId?: number | 'all' | null) {
  const gid = groupId == null || groupId === 'all' ? null : groupId
  const a = settings.applyDefaults({
    name: '', system_prompt: '', model: '',
    commands: [], skills: [], plugins: [],
    trigger: { type: 'manual', cron: '', message: '' },
    perception_scope: 'link',
    workflow_mode: 'none',
    group_id: gid,
  })
  editing.value = a
}

// 右键菜单位置夹紧：渲染后测量实际尺寸，确保不溢出视口
function clampToViewport(el: HTMLElement | null, data: { x: number; y: number } | null) {
  if (!el || !data) return
  const r = el.getBoundingClientRect()
  const m = 8
  let x = data.x, y = data.y
  if (x + r.width > window.innerWidth - m) x = Math.max(m, window.innerWidth - r.width - m)
  if (y + r.height > window.innerHeight - m) y = Math.max(m, window.innerHeight - r.height - m)
  data.x = x
  data.y = y
}

// —— 组右键菜单 ——
const groupCtx = ref<{ g: any; x: number; y: number } | null>(null)
const groupCtxEl = ref<HTMLElement | null>(null)
function onGroupCtx(e: MouseEvent, g: any) {
  e.preventDefault(); e.stopPropagation()
  groupCtx.value = { g, x: e.clientX, y: e.clientY }
  nextTick(() => clampToViewport(groupCtxEl.value, groupCtx.value))
}
function closeGroupCtx() { groupCtx.value = null }
async function ctxRenameGroup() {
  if (!groupCtx.value) return
  const g = groupCtx.value.g
  closeGroupCtx()
  const name = window.prompt('请输入新组名', g.name)?.trim()
  if (!name) return
  await agents.updateGroup(g.id, { name })
}
async function ctxDeleteGroup() {
  if (!groupCtx.value) return
  const g = groupCtx.value.g
  closeGroupCtx()
  if (window.confirm(`确定删除分组「${g.name}」吗？组内智能体会移回"全部"组。`)) {
    await agents.removeGroup(g.id)
  }
}
function ctxNewAgentInGroup() {
  if (!groupCtx.value) return
  const g = groupCtx.value.g
  closeGroupCtx()
  openNew(g.id)
}

// —— 智能体右键菜单（原有编辑/删除 + 归纳到分组） ——
const ctx = ref<{ a: any; x: number; y: number } | null>(null)
const ctxEl = ref<HTMLElement | null>(null)
function onCtx(e: MouseEvent, a: any) {
  e.preventDefault(); e.stopPropagation()
  ctx.value = { a, x: e.clientX, y: e.clientY }
  nextTick(() => clampToViewport(ctxEl.value, ctx.value))
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
async function ctxAssign(groupId: number | null) {
  if (!ctx.value) return
  const a = ctx.value.a
  closeCtx()
  await agents.assignToGroup(a.id, groupId)
}
async function ctxNewGroupAndAssign() {
  if (!ctx.value) return
  const a = ctx.value.a
  closeCtx()
  const name = window.prompt('请输入组名')?.trim()
  if (!name) return
  const g = await agents.createGroup({ name })
  await agents.assignToGroup(a.id, g.id)
}

function closeAllMenus() { ctx.value = null; groupCtx.value = null }
onMounted(() => window.addEventListener('click', closeAllMenus))
onBeforeUnmount(() => window.removeEventListener('click', closeAllMenus))

// —— 列表内拖拽排序 —— 与"拖到画布"共用同一次拖拽：dragstart 仍写入 'agent' 数据
// 供 CanvasBoard 接收；落点是列表内其它项时改为重排顺序（仅在当前可见列表内重排）。
const dragId = ref<number | null>(null)
const overId = ref<number | null>(null)
const overPos = ref<'before' | 'after'>('before')

function onDragStart(e: DragEvent, a: any) {
  dragId.value = a.id
  e.dataTransfer!.setData('agent', JSON.stringify(a))
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}
function onDragEnd() { dragId.value = null; overId.value = null }
function onItemDragOver(e: DragEvent, a: any) {
  if (dragId.value == null) return
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
  if (dragId.value == null) return
  e.preventDefault(); e.stopPropagation()
  const srcId = dragId.value
  dragId.value = null; overId.value = null
  if (srcId === a.id) return
  const cur = currentAgents.value.map(x => x.id)
  const from = cur.indexOf(srcId)
  if (from < 0) return
  const without = cur.filter(id => id !== srcId)
  let to = without.indexOf(a.id)
  if (to < 0) to = without.length
  if (overPos.value === 'after') to++
  without.splice(to, 0, srcId)
  agents.reorder(without)
}

// —— 组列表内拖拽排序（仅作用于自定义组，全部组固定置顶不可拖拽） ——
const groupDragId = ref<number | null>(null)
const groupOverId = ref<number | null>(null)
const groupOverPos = ref<'before' | 'after'>('before')

function onGroupDragStart(e: DragEvent, g: any) {
  groupDragId.value = g.id
  // 组拖拽仅用于列表内排序，不写入 'agent' 数据（不供画布接收）
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
  e.dataTransfer!.setData('text/plain', String(g.id))
}
function onGroupDragEnd() { groupDragId.value = null; groupOverId.value = null }
function onGroupDragOver(e: DragEvent, g: any) {
  if (groupDragId.value == null) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  groupOverPos.value = (e.clientY - rect.top) < rect.height / 2 ? 'before' : 'after'
  groupOverId.value = g.id
}
function onGroupDragLeave(g: any) {
  if (groupOverId.value === g.id) groupOverId.value = null
}
function onGroupDrop(e: DragEvent, g: any) {
  if (groupDragId.value == null) return
  e.preventDefault(); e.stopPropagation()
  const srcId = groupDragId.value
  groupDragId.value = null; groupOverId.value = null
  if (srcId === g.id) return
  const cur = agents.groups.map(x => x.id)
  const from = cur.indexOf(srcId)
  if (from < 0) return
  const without = cur.filter(id => id !== srcId)
  let to = without.indexOf(g.id)
  if (to < 0) to = without.length
  if (groupOverPos.value === 'after') to++
  without.splice(to, 0, srcId)
  agents.reorderGroups(without)
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

    <!-- 展开态 -->
    <template v-else>
      <!-- ===== 组列表（收纳）视图 ===== -->
      <template v-if="activeGroupId == null">
        <div class="section-title">
          <div class="title-left">
            <button class="collapse-btn" @click="emit('toggle')" title="折叠智能体列表">◂</button>
            <span>智能体</span>
          </div>
          <button class="btn ghost" @click="openNewGroup">+ 新建组</button>
        </div>
        <div class="list">
          <!-- 全部组（虚拟，固定置顶，不可右键） -->
          <div class="group-card glass-soft" @click="enterGroup('all')" @dragover.prevent @drop.prevent>
            <div class="group-head">
              <span class="group-icon">📁</span>
              <span class="group-name">全部</span>
              <span class="group-count">{{ agents.list.length }}</span>
              <span class="group-arrow">▸</span>
            </div>
            <div class="group-preview" v-if="agents.list.length">
              <div v-for="a in previewAgents('all')" :key="a.id" class="preview-item">
                <span class="dot"></span><span class="preview-name">{{ a.name }}</span>
              </div>
              <button v-if="previewExtra('all') > 0" class="preview-more"
                @click.stop="toggleGroupExpand('all')"
                :title="expandedGroupIds.has('all') ? '收起' : '展开全部'">
                {{ expandedGroupIds.has('all') ? '收起' : `+${previewExtra('all')}` }}
              </button>
            </div>
            <div class="group-empty" v-else>暂无智能体</div>
          </div>

          <!-- 自定义组（可右键：重命名 / 删除 / 新建智能体） -->
          <div class="group-card glass-soft" v-for="g in agents.groups" :key="g.id"
            draggable="true"
            :class="{ dragging: groupDragId === g.id, 'drop-before': groupOverId === g.id && groupOverPos === 'before' && groupDragId !== g.id, 'drop-after': groupOverId === g.id && groupOverPos === 'after' && groupDragId !== g.id }"
            @click="enterGroup(g.id)"
            @contextmenu.prevent="onGroupCtx($event, g)"
            @dragstart="onGroupDragStart($event, g)"
            @dragend="onGroupDragEnd"
            @dragover="onGroupDragOver($event, g)"
            @dragleave="onGroupDragLeave(g)"
            @drop="onGroupDrop($event, g)">
            <div class="group-head">
              <span class="group-icon">📁</span>
              <span class="group-name">{{ g.name }}</span>
              <span class="group-count">{{ agentsOfGroup(g.id).length }}</span>
              <span class="group-arrow">▸</span>
            </div>
            <div class="group-preview" v-if="agentsOfGroup(g.id).length">
              <div v-for="a in previewAgents(g.id)" :key="a.id" class="preview-item">
                <span class="dot"></span><span class="preview-name">{{ a.name }}</span>
              </div>
              <button v-if="previewExtra(g.id) > 0" class="preview-more"
                @click.stop="toggleGroupExpand(g.id)"
                :title="expandedGroupIds.has(g.id) ? '收起' : '展开全部'">
                {{ expandedGroupIds.has(g.id) ? '收起' : `+${previewExtra(g.id)}` }}
              </button>
            </div>
            <div class="group-empty" v-else>暂无智能体</div>
          </div>
        </div>
      </template>

      <!-- ===== 组详情视图 ===== -->
      <template v-else>
        <div class="section-title">
          <div class="title-left">
            <button class="collapse-btn" @click="emit('toggle')" title="折叠智能体列表">◂</button>
            <button class="back-btn" @click="backToGroups" title="返回分组列表">←</button>
            <span class="active-group-name">{{ currentGroupName }}</span>
          </div>
          <button class="btn ghost" @click="openNew(activeGroupId)">+ 新建智能体</button>
        </div>
        <div class="section-title">
          <span>能力标签</span>
          <label class="toggle">
            <input type="checkbox" v-model="showHidden" /> {{ showHidden ? '显示' : '隐藏' }}
          </label>
        </div>
        <div class="list">
          <div class="agent-item glass-soft"
            v-for="a in currentAgents" :key="a.id"
            draggable="true"
            :class="{ dragging: dragId === a.id, 'drop-before': overId === a.id && overPos === 'before' && dragId !== a.id, 'drop-after': overId === a.id && overPos === 'after' && dragId !== a.id }"
            @dragstart="onDragStart($event, a)"
            @dragend="onDragEnd"
            @dragover="onItemDragOver($event, a)"
            @dragleave="onItemDragLeave(a)"
            @drop="onItemDrop($event, a)"
            @contextmenu.prevent="onCtx($event, a)">
            <span v-if="activeGroupId === 'all' && a.group_id != null" class="group-badge" :title="groupNameOf(a.group_id)">{{ groupNameOf(a.group_id) }}</span>
            <div class="row">
              <div class="avatar" :style="{ background: (settings.data.avatar_mode === 'unified') ? 'linear-gradient(135deg, var(--accent), var(--accent-2))' : getAvatarGradient(a.id) }">{{ (a.name || '?').slice(0, 1).toUpperCase() }}</div>
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
          <div class="empty" v-if="!currentAgents.length">该组暂无智能体，点击"+ 新建智能体"</div>
        </div>
      </template>
    </template>

    <AgentEditDialog v-if="editing" :agent="editing" @close="editing = null" />

    <!-- 智能体右键菜单（Teleport 到 body，fixed 定位） -->
    <Teleport to="body">
      <div v-if="ctx" ref="ctxEl" class="agent-ctx glass" :style="{ left: ctx.x + 'px', top: ctx.y + 'px' }" @click.stop>
        <div class="ctx-title">{{ ctx.a.name }}</div>
        <div class="ctx-item" @click="ctxEdit">✏️ 编辑</div>
        <div class="ctx-item danger" @click="ctxDelete">🗑 删除</div>
        <div class="ctx-sep"></div>
        <div class="ctx-sub">归纳到分组</div>
        <div class="ctx-item" @click="ctxNewGroupAndAssign">➕ 新建组…</div>
        <div class="ctx-item" @click="ctxAssign(null)">
          全部<span v-if="ctx.a.group_id == null" class="ctx-cur">✓</span>
        </div>
        <div class="ctx-item" v-for="g in agents.groups" :key="g.id" @click="ctxAssign(g.id)">
          {{ g.name }}<span v-if="ctx.a.group_id === g.id" class="ctx-cur">✓</span>
        </div>
      </div>
    </Teleport>

    <!-- 组右键菜单（Teleport 到 body，fixed 定位） -->
    <Teleport to="body">
      <div v-if="groupCtx" ref="groupCtxEl" class="agent-ctx glass" :style="{ left: groupCtx.x + 'px', top: groupCtx.y + 'px' }" @click.stop>
        <div class="ctx-title">{{ groupCtx.g.name }}</div>
        <div class="ctx-item" @click="ctxRenameGroup">✏️ 重命名</div>
        <div class="ctx-item danger" @click="ctxDeleteGroup">🗑 删除组</div>
        <div class="ctx-item" @click="ctxNewAgentInGroup">➕ 新建智能体</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.agent-list { overflow: hidden; display: flex; flex-direction: column; }
.list { flex: 1; overflow-y: auto; padding: 4px 10px 12px; }

/* —— 组卡片（收纳视图） —— */
.group-card { padding: 10px 12px; margin-bottom: 8px; cursor: pointer; user-select: none; transition: background .15s, border-color .15s;
  &:hover { background: rgba(var(--accent-rgb), 0.08); border-color: rgba(var(--accent-rgb), 0.3); }
}
.group-card.dragging { opacity: .4; }
.group-card.drop-before { box-shadow: 0 -2px 0 0 var(--accent), 0 1px 0 0 rgba(var(--accent-rgb), 0.25); }
.group-card.drop-after { box-shadow: 0 2px 0 0 var(--accent), 0 -1px 0 0 rgba(var(--accent-rgb), 0.25); }
.group-head { display: flex; align-items: center; gap: 8px; }
.group-icon { font-size: calc(14px * var(--font-scale)); flex-shrink: 0; }
.group-name { font-size: calc(13px * var(--font-scale)); font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.group-count { font-size: calc(11px * var(--font-scale)); opacity: .55; background: rgba(var(--surface-rgb), 0.08); padding: 1px 7px; border-radius: 999px; flex-shrink: 0; }
.group-arrow { font-size: calc(12px * var(--font-scale)); opacity: .5; flex-shrink: 0; }
.group-preview { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; }
.preview-item { display: flex; align-items: center; gap: 7px; font-size: calc(12px * var(--font-scale)); opacity: .7; padding-left: 2px; }
.dot { width: 4px; height: 4px; border-radius: 50%; background: rgba(var(--text-rgb), 0.35); flex-shrink: 0; }
.preview-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-more { align-self: flex-start; padding: 2px 8px; border-radius: 999px; font-size: calc(11px * var(--font-scale)); cursor: pointer; line-height: 1.4;
  background: rgba(var(--surface-rgb), 0.06); color: rgba(var(--text-rgb), 0.7); border: 1px dashed rgba(var(--surface-rgb), 0.18); margin-top: 2px;
  &:hover { background: rgba(var(--accent-rgb), 0.18); color: var(--accent-ink); border-color: rgba(var(--accent-rgb), 0.4); }
}
.group-empty { font-size: calc(11px * var(--font-scale)); opacity: .4; margin-top: 4px; padding-left: 2px; }

/* —— 智能体卡片（详情视图，原有样式） —— */
.agent-item { padding: 10px; margin-bottom: 8px; cursor: grab; user-select: none; position: relative; }
.agent-item:active { cursor: grabbing; }
.agent-item.dragging { opacity: .4; }
.agent-item.drop-before { box-shadow: 0 -2px 0 0 var(--accent), 0 1px 0 0 rgba(var(--accent-rgb), 0.25); }
.agent-item.drop-after { box-shadow: 0 2px 0 0 var(--accent), 0 -1px 0 0 rgba(var(--accent-rgb), 0.25); }
.group-badge { position: absolute; top: 6px; right: 8px; font-size: calc(10px * var(--font-scale)); padding: 2px 7px; border-radius: 999px; background: rgba(var(--accent-rgb), 0.14); color: var(--accent-ink); border: 1px solid rgba(var(--accent-rgb), 0.28); max-width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; pointer-events: none; }
.row { display: flex; gap: 10px; align-items: center; }
.avatar { position: relative; overflow: hidden; width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: calc(12px * var(--font-scale)); flex-shrink: 0; color: #fff; text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.3); box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45), inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32), inset 0 0 0 1px rgba(var(--surface-rgb), 0.08), 0 3px 8px rgba(var(--shadow-rgb), 0.4); }
.avatar::before { content: ''; position: absolute; inset: 0; background: linear-gradient(165deg, rgba(var(--surface-rgb), 0.45) 0%, rgba(var(--surface-rgb), 0.10) 32%, rgba(var(--surface-rgb), 0) 52%), linear-gradient(0deg, rgba(var(--shadow-rgb), 0.24) 0%, rgba(var(--shadow-rgb), 0) 42%); pointer-events: none; }
.name { font-size: calc(13px * var(--font-scale)); font-weight: 500; }
.desc { font-size: calc(11px * var(--font-scale)); opacity: .6; margin-top: 2px; }
.tags { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 4px; }
.tag.more {
  padding: 2px 8px; border-radius: 999px; font-size: calc(11px * var(--font-scale)); cursor: pointer; line-height: 1.4;
  background: rgba(var(--surface-rgb), 0.06); color: rgba(var(--text-rgb), 0.7);
  border: 1px dashed rgba(var(--surface-rgb), 0.18);
  &:hover { background: rgba(var(--accent-rgb), 0.18); color: var(--accent-ink); border-color: rgba(var(--accent-rgb), 0.4); }
}
.empty { text-align: center; padding: 40px 0; opacity: .5; font-size: calc(13px * var(--font-scale)); }
.toggle { display: flex; align-items: center; gap: 4px; font-size: calc(11px * var(--font-scale)); cursor: pointer; }

/* 标题行左侧分组（折叠按钮 + 标题） */
.title-left { display: flex; align-items: center; gap: 6px; min-width: 0; }
.active-group-name { font-size: calc(13px * var(--font-scale)); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; transition: color .15s;
  &:hover { color: var(--accent-ink); }
}
.collapse-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 6px; color: rgba(var(--text-rgb), 0.7); font-size: calc(11px * var(--font-scale)); cursor: pointer; line-height: 1; transition: all .15s; }
.collapse-btn:hover { background: rgba(var(--surface-rgb), 0.08); color: rgb(var(--text-rgb)); border-color: rgba(var(--accent-rgb), 0.4); }
.back-btn { flex-shrink: 0; width: 22px; height: 22px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 1px solid rgba(var(--surface-rgb), 0.12); border-radius: 6px; color: rgba(var(--text-rgb), 0.7); font-size: calc(13px * var(--font-scale)); cursor: pointer; line-height: 1; transition: all .15s; }
.back-btn:hover { background: rgba(var(--accent-rgb), 0.12); color: var(--accent-ink); border-color: rgba(var(--accent-rgb), 0.4); }

/* 折叠态窄条 */
.expand-strip { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; background: transparent; border: 0; cursor: pointer; color: rgba(var(--text-rgb), 0.6); transition: all .15s; padding: 0; }
.expand-strip:hover { color: rgb(var(--text-rgb)); background: rgba(var(--surface-rgb), 0.05); }
.expand-arrow { font-size: calc(16px * var(--font-scale)); line-height: 1; }
.expand-label { writing-mode: vertical-rl; font-size: calc(11px * var(--font-scale)); letter-spacing: 2px; opacity: .8; }

/* 右键菜单 */
.agent-ctx { position: fixed; min-width: 160px; max-height: 70vh; overflow-y: auto; padding: 6px; z-index: 9999; box-shadow: 0 12px 40px rgba(var(--shadow-rgb), 0.5); }
.ctx-title { font-size: calc(11px * var(--font-scale)); padding: 6px 10px; color: rgba(var(--text-rgb), 0.5); text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ctx-item { padding: 8px 12px; border-radius: 6px; font-size: calc(13px * var(--font-scale)); cursor: pointer; transition: background .15s; user-select: none; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  &:hover { background: rgba(var(--accent-rgb), 0.18); }
  &.danger:hover { background: rgba(255,80,80,0.18); color: #ff9090; }
}
.ctx-sep { height: 1px; background: rgba(var(--surface-rgb), 0.08); margin: 4px 0; }
.ctx-sub { font-size: calc(10px * var(--font-scale)); padding: 4px 10px; color: rgba(var(--text-rgb), 0.4); letter-spacing: 1px; }
.ctx-cur { color: var(--accent); font-size: calc(13px * var(--font-scale)); }
</style>
