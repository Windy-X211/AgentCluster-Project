<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useSettingsStore } from '@/stores'
import { api } from '@/api'
import type { ComboMember, ComboRecord } from '@/api'

const settings = useSettingsStore()

type CapType = 'commands' | 'skills' | 'plugins' | 'combos'
type GroupMap = Record<string, string[]>          // { groupName: [capKey, ...] }
interface GroupedList { groups: { name: string; keys: string[] }[]; ungrouped: string[] }

const TYPE_TABS: { key: CapType; label: string; icon: string }[] = [
  { key: 'commands', label: '命令', icon: 'keyboard' },
  { key: 'skills',   label: '技能', icon: 'sparkles' },
  { key: 'plugins',  label: '插件', icon: 'puzzle' },
  { key: 'combos',   label: '组合技', icon: 'target' },
]

const activeType = ref<CapType>('commands')
const selectedKey = ref<string | null>(null)
const collapsed = ref<Set<string>>(new Set())
const editingGroup = ref<string | null>(null)
const editingGroupName = ref('')
const newGroupNameOpen = ref(false)
const newGroupName = ref('')
const loading = ref(false)
const toast = ref<{ msg: string; kind: 'ok' | 'err' } | null>(null)

// —— 用户自定义分组（独立文件：{ROOT_CAPABILITY_GROUPS}/{type}.json，通过 API 读写）——
const userGroups = ref<GroupMap>({})
const groupsLoaded = ref(false)
let _loadSeq = 0

async function loadGroupsFor(type: CapType) {
  const seq = ++_loadSeq
  groupsLoaded.value = false
  try {
    const g = await api.settings.getCapabilityGroups(type)
    if (seq === _loadSeq) userGroups.value = { ...(g || {}) }
  } catch (e: any) {
    console.warn('加载能力分组失败', e)
    if (seq === _loadSeq) userGroups.value = {}
  } finally {
    if (seq === _loadSeq) groupsLoaded.value = true
  }
}

async function saveGroupsFor(type: CapType, groups: GroupMap) {
  try {
    await api.settings.setCapabilityGroups(type, groups)
    userGroups.value = { ...groups }
  } catch (e: any) {
    showToast(`保存分组失败：${e?.message || e}`, 'err')
    throw e
  }
}

// —— 获取某类型下的全部能力 key → meta 映射 ——
function metaFor(type: CapType): Record<string, any> {
  if (type === 'commands') return settings.commandsMeta || {}
  if (type === 'skills')   return settings.skillsMeta   || {}
  if (type === 'plugins')  return settings.pluginsMeta  || {}
  return {}
}

// —— 内置命令分组（后端 COMMAND_GROUPS），作为默认分组种子 ——
const builtinCommandGroups = computed<GroupMap>(() => {
  const caps = settings.data?.default_commands ?? [] // not used here
  const result: GroupMap = {}
  // 从 commandsMeta 里读 group 字段（后端 COMMAND_GROUPS 已经给每个命令 meta 注入了 group）
  for (const [key, m] of Object.entries(settings.commandsMeta || {})) {
    if ((m as any).group) {
      const g = (m as any).group as string
      if (!result[g]) result[g] = []
      if (!result[g].includes(key)) result[g].push(key)
    }
  }
  return result
})

// —— 用户自定义分组（来自独立文件）——
const userGroupMap = computed<GroupMap>(() => ({ ...userGroups.value }))

// —— 合并：用户分组覆盖内置分组（仅对 commands 有内置分组） ——
const effectiveGroups = computed<GroupMap>(() => {
  const type = activeType.value
  if (type === 'commands') {
    // 以用户分组为主，缺失时 fallback 到内置分组
    const merged: GroupMap = { ...builtinCommandGroups.value }
    for (const [g, keys] of Object.entries(userGroupMap.value)) merged[g] = keys
    return merged
  }
  return { ...userGroupMap.value }
})

// —— 当前类型下所有存在的能力 key ——
const allKeys = computed<string[]>(() => Object.keys(metaFor(activeType.value)))

// —— 计算好的分组 + 未分组列表 ——
const groupedList = computed<GroupedList>(() => {
  const groups: { name: string; keys: string[] }[] = []
  const used = new Set<string>()
  for (const [name, keys] of Object.entries(effectiveGroups.value)) {
    const filtered = keys.filter(k => allKeys.value.includes(k))
    filtered.forEach(k => used.add(k))
    // 空分组也要显示（新建分组时就是空的，方便用户拖入）
    groups.push({ name, keys: filtered })
  }
  const ungrouped = allKeys.value.filter(k => !used.has(k) && !(activeType.value === 'commands' && (settings.commandsMeta[k] as any)?.hidden))
  return { groups, ungrouped }
})

// —— 保存能力分组到独立文件 ——
// 传入 updatedMap 时直接用它；否则用当前 effectiveGroups 计算
function persistGroups(updatedMap?: GroupMap) {
  const source = updatedMap ?? effectiveGroups.value
  // 空分组也保存（空数组占位），否则新建分组刷新后会丢失；
  // 但内置命令分组里完全相同的（用户没改过）不重复存，节省体积
  const builtin = activeType.value === 'commands' ? builtinCommandGroups.value : {}
  const clean: GroupMap = {}
  for (const [g, keys] of Object.entries(source)) {
    const filtered = keys.filter(k => allKeys.value.includes(k))
    const bKeys = builtin[g]
    // 内置分组完全一致 → 不存（启动时自动从内置生成）
    if (bKeys && bKeys.length === filtered.length && bKeys.every((k, i) => k === filtered[i])) continue
    clean[g] = filtered
  }
  return saveGroupsFor(activeType.value, clean)
}

// —— Toast ——
let _toastTimer: number | null = null
function showToast(msg: string, kind: 'ok' | 'err' = 'ok') {
  toast.value = { msg, kind }
  if (_toastTimer) clearTimeout(_toastTimer)
  _toastTimer = window.setTimeout(() => { toast.value = null }, 2200)
}

// —— 展开/折叠分组 ——
function toggleCollapse(name: string) {
  const s = new Set(collapsed.value)
  if (s.has(name)) s.delete(name)
  else s.add(name)
  collapsed.value = s
}

// —— 刷新 ——
async function reload() {
  if (loading.value) return
  loading.value = true
  try {
    await settings.reloadCapabilities()
    if (activeType.value === 'combos') {
      await loadCombos()
      showToast(`已刷新：${comboList.value.length} 个组合技`)
    } else {
      await loadGroupsFor(activeType.value)
      showToast(`已刷新：${Object.keys(settings.commandsMeta).length} 命令 · ${Object.keys(settings.skillsMeta).length} 技能 · ${Object.keys(settings.pluginsMeta).length} 插件`)
    }
  } catch (e: any) {
    showToast(`刷新失败：${e?.message || e}`, 'err')
  } finally {
    loading.value = false
  }
}

// —— 分组操作 ——
function startRenameGroup(name: string) {
  editingGroup.value = name
  editingGroupName.value = name
}
function commitRenameGroup() {
  if (!editingGroup.value) return
  const newName = editingGroupName.value.trim()
  if (!newName || newName === editingGroup.value) { editingGroup.value = null; return }
  if (effectiveGroups.value[newName]) { showToast('分组名已存在', 'err'); return }
  const newMap: GroupMap = {}
  for (const [g, keys] of Object.entries(effectiveGroups.value)) {
    newMap[g === editingGroup.value ? newName : g] = keys
  }
  persistGroups(newMap)
  editingGroup.value = null
}
function deleteGroup(name: string) {
  if (!confirm(`删除分组「${name}」？其中的能力将回到未分组区域。`)) return
  const newMap: GroupMap = {}
  for (const [g, keys] of Object.entries(effectiveGroups.value)) {
    if (g !== name) newMap[g] = keys
  }
  persistGroups(newMap)
  showToast(`已删除分组「${name}」`)
}
function openNewGroup() {
  newGroupName.value = ''
  newGroupNameOpen.value = true
}
function commitNewGroup() {
  const name = newGroupName.value.trim()
  if (!name) { newGroupNameOpen.value = false; return }
  if (effectiveGroups.value[name]) { showToast('分组名已存在', 'err'); return }
  const newMap: GroupMap = { ...effectiveGroups.value, [name]: [] }
  persistGroups(newMap)
  newGroupNameOpen.value = false
  newGroupName.value = ''
  showToast(`已创建分组「${name}」`)
}

// —— 选中能力 ——
function selectKey(key: string) { selectedKey.value = key }

// ============================================================
// 右键菜单 + 多选（仅技能/插件；命令是内置能力，不可删）
// ============================================================
const canManage = computed(() => activeType.value === 'skills' || activeType.value === 'plugins')
const multiMode = ref(false)
const multiSel = ref<Set<string>>(new Set())
const capCtx = ref<{ x: number; y: number; key: string } | null>(null)
const ctxMoveSub = ref(false)
const deleting = ref(false)

const allSelected = computed(() =>
  allKeys.value.length > 0 && allKeys.value.every(k => multiSel.value.has(k)),
)
const capCtxTitle = computed(() => {
  if (multiSel.value.size > 1) return `已选 ${multiSel.value.size} 项`
  const k = capCtx.value?.key
  if (!k) return ''
  return (metaFor(activeType.value)[k] as any)?.label || k
})
const groupNamesForMove = computed(() => Object.keys(effectiveGroups.value))

function onCapClick(key: string) {
  if (multiMode.value) toggleMulti(key)
  else selectKey(key)
}

function onCapContextMenu(e: MouseEvent, key: string) {
  if (!canManage.value) return
  e.preventDefault()
  e.stopPropagation()
  // 右键未选中的项 → 自动并入当前选择；右键已选中的项 → 保留整批选择
  if (!multiSel.value.has(key)) {
    multiSel.value = new Set([...multiSel.value, key])
  }
  ctxMoveSub.value = false
  const MENU_W = 210
  const MENU_H = multiMode.value ? 330 : 300
  capCtx.value = {
    x: Math.max(6, Math.min(e.clientX, window.innerWidth - MENU_W - 10)),
    y: Math.max(6, Math.min(e.clientY, window.innerHeight - MENU_H - 10)),
    key,
  }
}

function closeCapCtx() {
  capCtx.value = null
  ctxMoveSub.value = false
}

function enterMultiMode(key?: string) {
  multiMode.value = true
  if (key) multiSel.value = new Set([...multiSel.value, key])
  else if (!multiSel.value.size) multiSel.value = new Set()
  closeCapCtx()
}

function exitMultiMode() {
  multiMode.value = false
  multiSel.value = new Set()
  closeCapCtx()
}

function toggleMulti(key: string) {
  const s = new Set(multiSel.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  multiSel.value = s
}

function toggleSelectAll() {
  if (allSelected.value) multiSel.value = new Set()
  else multiSel.value = new Set(allKeys.value)
  closeCapCtx()
}

async function moveSelectedTo(group: string | null) {
  const keys = [...multiSel.value]
  if (!keys.length) { closeCapCtx(); return }
  const map: GroupMap = { ...effectiveGroups.value }
  for (const g of Object.keys(map)) map[g] = map[g].filter(k => !keys.includes(k))
  if (group) {
    if (!map[group]) map[group] = []
    for (const k of keys) if (!map[group].includes(k)) map[group].push(k)
  }
  closeCapCtx()
  try {
    await persistGroups(map)
    showToast(`已将 ${keys.length} 项移到「${group || '未分组'}」`)
  } catch { /* saveGroupsFor 内部已提示 */ }
}

async function deleteSelectedCaps() {
  const type = activeType.value
  if (type !== 'skills' && type !== 'plugins') return
  const keys = [...multiSel.value]
  if (!keys.length) { showToast('请先选择要删除的能力', 'err'); return }
  closeCapCtx()
  const label = type === 'skills' ? '技能' : '插件'
  const names = keys.map(k => (metaFor(type)[k] as any)?.label || k)
  if (!confirm(`删除 ${keys.length} 个${label}？\n这会移除本地安装目录，不可恢复。\n\n${names.join('、')}`)) return
  deleting.value = true
  try {
    const res = await api.capabilities.deleteCapabilities(type, keys)
    const deleted: string[] = res?.deleted || []
    const failed = res?.failed || []
    await settings.reloadCapabilities()

    if (deleted.length) {
      // 从分组里摘除（空分组保留）
      const nextGroups: GroupMap = {}
      for (const [g, ks] of Object.entries(effectiveGroups.value)) {
        nextGroups[g] = ks.filter(k => !deleted.includes(k))
      }
      try { await persistGroups(nextGroups) } catch { /* 已提示 */ }
      // 从组合技成员里摘除
      let combosChanged = false
      const nextCombos: Record<string, ComboRecord> = {}
      for (const [name, rec] of Object.entries(combosData.value)) {
        const members = (rec.members || []).filter(m => !(m.type === type && deleted.includes(m.key)))
        if (members.length !== (rec.members || []).length) combosChanged = true
        nextCombos[name] = { ...rec, members }
      }
      if (combosChanged) {
        combosData.value = nextCombos
        await persistCombos()
      }
      if (selectedKey.value && deleted.includes(selectedKey.value)) selectedKey.value = null
    }

    await loadGroupsFor(type)
    await loadCombos()
    multiMode.value = false
    multiSel.value = new Set()
    if (failed.length) {
      showToast(`已删除 ${deleted.length} 个${label}，${failed.length} 个失败：${failed.map(f => f.key).join('、')}`, 'err')
    } else {
      showToast(`已删除 ${deleted.length} 个${label}`)
    }
  } catch (e: any) {
    showToast(`删除失败：${e?.message || e}`, 'err')
  } finally {
    deleting.value = false
  }
}

function onDocClickCloseCtx() { if (capCtx.value) closeCapCtx() }
function onDocKeyCloseCtx(e: KeyboardEvent) {
  if (e.key === 'Escape' && capCtx.value) closeCapCtx()
}

// —— 详情数据 ——
const detail = computed(() => {
  if (!selectedKey.value) return null
  const meta = metaFor(activeType.value)[selectedKey.value]
  if (!meta) return null
  // 计算所属分组
  let group: string | null = null
  for (const [g, keys] of Object.entries(effectiveGroups.value)) {
    if (keys.includes(selectedKey.value!)) { group = g; break }
  }
  return { key: selectedKey.value, meta, group }
})

// ========== HTML5 Drag & Drop ==========
// dragged payload: { type, key }
interface DragPayload { type: CapType; key: string }
const dragOverTarget = ref<string | null>(null)  // group name, or "__ungrouped__"
const dragInsertKey = ref<string | null>(null)  // 插入位置（拖到某个具体 item 上时）

let _currentDrag: DragPayload | null = null

function onDragStart(e: DragEvent, key: string) {
  _currentDrag = { type: activeType.value, key }
  const target = e.currentTarget as HTMLElement
  target.classList.add('dragging')
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', key)
  }
}
function onDragEnd(e: DragEvent) {
  _currentDrag = null
  dragOverTarget.value = null
  dragInsertKey.value = null
  document.querySelectorAll('.cap-item.dragging').forEach(el => el.classList.remove('dragging'))
}
function onDragOverGroup(e: DragEvent, groupName: string) {
  if (!_currentDrag || _currentDrag.type !== activeType.value) return
  e.preventDefault()
  e.stopPropagation()
  if (dragOverTarget.value !== groupName) { dragOverTarget.value = groupName; dragInsertKey.value = null }
}
function onDragOverUngrouped(e: DragEvent) {
  if (!_currentDrag || _currentDrag.type !== activeType.value) return
  e.preventDefault()
  e.stopPropagation()
  dragOverTarget.value = '__ungrouped__'
  dragInsertKey.value = null
}
function onDragOverItem(e: DragEvent, key: string) {
  if (!_currentDrag || _currentDrag.type !== activeType.value) return
  e.preventDefault()
  e.stopPropagation()
  dragInsertKey.value = key
}
function onDropGroup(e: DragEvent, groupName: string) {
  e.preventDefault()
  e.stopPropagation()
  if (!_currentDrag) return
  moveToGroup(_currentDrag.key, groupName)
  _currentDrag = null
  dragOverTarget.value = null
  dragInsertKey.value = null
}
function onDropUngrouped(e: DragEvent) {
  e.preventDefault()
  e.stopPropagation()
  if (!_currentDrag) return
  moveToGroup(_currentDrag.key, null)
  _currentDrag = null
  dragOverTarget.value = null
  dragInsertKey.value = null
}

function moveToGroup(key: string, targetGroup: string | null) {
  const currentMap = { ...effectiveGroups.value }
  // 从源分组移除
  for (const g of Object.keys(currentMap)) {
    currentMap[g] = currentMap[g].filter(k => k !== key)
  }
  // 注意：不自动删除空分组，保留给用户看（空分组是合法状态，比如刚创建的）

  if (targetGroup) {
    if (!currentMap[targetGroup]) currentMap[targetGroup] = []
    // 如果有 insertKey，插到它前面；否则追加
    if (dragInsertKey.value && currentMap[targetGroup].includes(dragInsertKey.value)) {
      const idx = currentMap[targetGroup].indexOf(dragInsertKey.value)
      currentMap[targetGroup].splice(idx, 0, key)
    } else {
      currentMap[targetGroup].push(key)
    }
  }
  // 写回（只写用户分组层，保存到独立文件）
  // 对于 commands：合并内置分组后提取用户覆盖的差异太复杂，这里简化为直接存当前完整 map
  // 但内置分组不应被当作用户分组保存，所以需要过滤掉完全相同的
  const builtin = activeType.value === 'commands' ? builtinCommandGroups.value : {}
  const userOnly: GroupMap = {}
  for (const [g, keys] of Object.entries(currentMap)) {
    // 如果和内置分组完全一样，就不存（下次启动自动从内置生成）
    const bKeys = builtin[g]
    if (bKeys && bKeys.length === keys.length && bKeys.every((k, i) => k === keys[i])) continue
    userOnly[g] = keys
  }
  saveGroupsFor(activeType.value, userOnly)
}

onMounted(async () => {
  window.addEventListener('click', onDocClickCloseCtx)
  window.addEventListener('keydown', onDocKeyCloseCtx)
  window.addEventListener('scroll', onDocClickCloseCtx, true)
  if (!settings.loaded) {
    await settings.fetch()
  }
  await loadGroupsFor(activeType.value)
  await loadCombos()
})

onBeforeUnmount(() => {
  window.removeEventListener('click', onDocClickCloseCtx)
  window.removeEventListener('keydown', onDocKeyCloseCtx)
  window.removeEventListener('scroll', onDocClickCloseCtx, true)
})

// 类型切换时加载对应分组
watch(activeType, async () => {
  selectedKey.value = null
  exitMultiMode()
  if (activeType.value === 'combos') {
    await loadCombos()
  } else {
    await loadGroupsFor(activeType.value)
  }
})

// ============================================================
// 组合技（跨类型能力组合：命令/技能/插件可混编）
// ============================================================
const combosData = ref<Record<string, ComboRecord>>({})
const combosLoaded = ref(false)
const selectedCombo = ref<string | null>(null)
const editingComboName = ref<string | null>(null)
const editingComboNameVal = ref('')
const newComboOpen = ref(false)
const newComboName = ref('')
const newComboDesc = ref('')

// 添加成员弹窗
const addMemberOpen = ref(false)
const addMemberType = ref<'commands' | 'skills' | 'plugins'>('commands')
const addMemberQuery = ref('')
const addMemberPicked = ref<Set<string>>(new Set())
const addMemberCollapsed = ref<Set<string>>(new Set())

const COMBO_TYPE_META: Record<'commands' | 'skills' | 'plugins', { label: string; icon: string }> = {
  commands: { label: '命令', icon: '⌨' },
  skills:   { label: '技能', icon: '✨' },
  plugins:  { label: '插件', icon: '🧩' },
}
const COMBO_TYPES = ['commands', 'skills', 'plugins'] as const

async function loadCombos() {
  combosLoaded.value = false
  try {
    const data = await api.settings.getCombos()
    combosData.value = { ...(data || {}) }
    // 同步到 settings store，供智能体编辑等其它界面读取
    settings.combos = { ...combosData.value }
  } catch (e: any) {
    console.warn('加载组合技失败', e)
    combosData.value = {}
  } finally {
    combosLoaded.value = true
  }
}

async function persistCombos() {
  try {
    await api.settings.setCombos(combosData.value)
    settings.combos = { ...combosData.value }
  } catch (e: any) {
    showToast(`保存组合技失败：${e?.message || e}`, 'err')
  }
}

const comboList = computed(() => {
  return Object.entries(combosData.value)
    .map(([name, rec]) => ({
      name,
      description: rec?.description || '',
      members: rec?.members || [],
      counts: countMembers(rec?.members || []),
    }))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
})

function countMembers(members: ComboMember[]) {
  const c = { commands: 0, skills: 0, plugins: 0 }
  for (const m of members) c[m.type]++
  return c
}

function comboSummary(counts: { commands: number; skills: number; plugins: number }) {
  const parts: string[] = []
  if (counts.commands) parts.push(`${counts.commands} 命令`)
  if (counts.skills) parts.push(`${counts.skills} 技能`)
  if (counts.plugins) parts.push(`${counts.plugins} 插件`)
  return parts.join(' · ') || '暂无成员'
}

const selectedComboDetail = computed(() => {
  if (!selectedCombo.value) return null
  const rec = combosData.value[selectedCombo.value]
  if (!rec) return null
  const members = rec.members || []
  return {
    name: selectedCombo.value,
    description: rec.description || '',
    members,
    counts: countMembers(members),
    market_id: rec.market_id,
  }
})

function selectCombo(name: string) {
  selectedCombo.value = name
}

function openNewCombo() {
  newComboName.value = ''
  newComboDesc.value = ''
  newComboOpen.value = true
}

function commitNewCombo() {
  const name = newComboName.value.trim()
  if (!name) { newComboOpen.value = false; return }
  if (combosData.value[name]) { showToast('组合技名称已存在', 'err'); return }
  combosData.value = {
    ...combosData.value,
    [name]: { description: newComboDesc.value.trim(), members: [] },
  }
  persistCombos()
  newComboOpen.value = false
  selectedCombo.value = name
  showToast(`已创建组合技「${name}」`)
}

function startRenameCombo(name: string) {
  editingComboName.value = name
  editingComboNameVal.value = name
}

function commitRenameCombo() {
  if (!editingComboName.value) return
  const old = editingComboName.value
  const newName = editingComboNameVal.value.trim()
  if (!newName || newName === old) { editingComboName.value = null; return }
  if (combosData.value[newName]) { showToast('名称已存在', 'err'); return }
  const next: Record<string, ComboRecord> = {}
  for (const [n, rec] of Object.entries(combosData.value)) {
    next[n === old ? newName : n] = rec
  }
  combosData.value = next
  if (selectedCombo.value === old) selectedCombo.value = newName
  persistCombos()
  editingComboName.value = null
  showToast(`已重命名为「${newName}」`)
}

function deleteCombo(name: string) {
  if (!confirm(`删除组合技「${name}」？引用它的智能体将不再展开该组合。`)) return
  const next = { ...combosData.value }
  delete next[name]
  combosData.value = next
  if (selectedCombo.value === name) selectedCombo.value = null
  persistCombos()
  showToast(`已删除组合技「${name}」`)
}

function onComboDescInput(e: Event) {
  if (!selectedCombo.value) return
  const val = (e.target as HTMLTextAreaElement).value
  const rec = combosData.value[selectedCombo.value]
  if (!rec) return
  combosData.value = {
    ...combosData.value,
    [selectedCombo.value]: { ...rec, description: val },
  }
}

function onComboDescBlur() {
  persistCombos()
}

function memberLabel(m: ComboMember) {
  return metaFor(m.type)?.[m.key]?.label || m.key
}

function removeMember(m: ComboMember) {
  if (!selectedCombo.value) return
  const rec = combosData.value[selectedCombo.value]
  if (!rec) return
  const members = (rec.members || []).filter(
    x => !(x.type === m.type && x.key === m.key),
  )
  combosData.value = {
    ...combosData.value,
    [selectedCombo.value]: { ...rec, members },
  }
  persistCombos()
}

function openAddMember(type: 'commands' | 'skills' | 'plugins') {
  addMemberType.value = type
  addMemberQuery.value = ''
  addMemberPicked.value = new Set(
    (selectedComboDetail.value?.members || [])
      .filter(m => m.type === type)
      .map(m => m.key),
  )
  // 命令分组默认折叠，技能/插件默认展开
  addMemberCollapsed.value = type === 'commands'
    ? new Set([...(settings.commandGroups || [])])
    : new Set()
  // 组合技编辑可能切换到了其它 tab，这里确保对应类型的能力分组已加载
  loadGroupsFor(type)
  addMemberOpen.value = true
}

// 添加成员：各类型的分组结构（命令用内置命令组 + 用户覆盖；技能/插件用用户自定义分组，未分组直接显示）
const addMemberGroups = computed<{ name: string; items: { key: string; label: string; used: boolean }[] }[]>(() => {
  const type = addMemberType.value
  const meta = metaFor(type)
  const used = new Set(
    (selectedComboDetail.value?.members || [])
      .filter(m => m.type === type)
      .map(m => m.key),
  )
  const q = addMemberQuery.value.trim().toLowerCase()
  const match = (label: string, key: string) =>
    !q || label.toLowerCase().includes(q) || key.toLowerCase().includes(q)
  // 候选能力（去掉隐藏命令）
  const candidateKeys = Object.keys(meta)
    .filter(key => !(type === 'commands' && (meta[key] as any)?.hidden))
    .filter(key => match((meta[key] as any)?.label || key, key))

  // 取分组：命令用 effectiveGroups 风格（内置+用户），其它类型用 userGroups
  let groupsMap: GroupMap
  if (type === 'commands') {
    const merged: GroupMap = { ...builtinCommandGroups.value }
    for (const [g, keys] of Object.entries(userGroupMap.value)) merged[g] = keys
    groupsMap = merged
  } else {
    groupsMap = { ...userGroups.value }
  }

  const byGroup = new Map<string, { key: string; label: string; used: boolean }[]>()
  const seen = new Set<string>()
  // 分组顺序：commands 用内置顺序优先；其它用用户分组文件里的顺序
  const order: string[] = []
  if (type === 'commands') {
    for (const g of (settings.commandGroups || [])) if (groupsMap[g] && !order.includes(g)) order.push(g)
  }
  for (const g of Object.keys(groupsMap)) if (!order.includes(g)) order.push(g)

  for (const gname of order) {
    for (const k of groupsMap[gname] || []) {
      if (seen.has(k) || !candidateKeys.includes(k)) continue
      seen.add(k)
      if (!byGroup.has(gname)) byGroup.set(gname, [])
      byGroup.get(gname)!.push({
        key: k,
        label: (meta[k] as any)?.label || k,
        used: used.has(k),
      })
    }
  }
  // 未分组：候选里没被任何分组收纳的
  const ungrouped: { key: string; label: string; used: boolean }[] = []
  for (const k of candidateKeys) {
    if (seen.has(k)) continue
    ungrouped.push({ key: k, label: (meta[k] as any)?.label || k, used: used.has(k) })
  }
  const result: { name: string; items: { key: string; label: string; used: boolean }[] }[] = []
  for (const gname of order) {
    const items = byGroup.get(gname)
    if (items && items.length) result.push({ name: gname, items })
  }
  if (ungrouped.length) result.push({ name: '未分组', items: ungrouped })
  return result
})

const addMemberTotalCount = computed(() =>
  addMemberGroups.value.reduce((n, g) => n + g.items.length, 0))

function amToggleGroupCollapse(name: string) {
  const s = new Set(addMemberCollapsed.value)
  if (s.has(name)) s.delete(name)
  else s.add(name)
  addMemberCollapsed.value = s
}
function amIsGroupSelected(group: { items: { key: string }[] }) {
  return group.items.length > 0 && group.items.every(it => addMemberPicked.value.has(it.key))
}
function amIsGroupIndeterminate(group: { items: { key: string }[] }) {
  const cnt = group.items.filter(it => addMemberPicked.value.has(it.key)).length
  return cnt > 0 && cnt < group.items.length
}
function amToggleGroup(group: { items: { key: string }[] }) {
  const s = new Set(addMemberPicked.value)
  if (amIsGroupSelected(group)) group.items.forEach(it => s.delete(it.key))
  else group.items.forEach(it => s.add(it.key))
  addMemberPicked.value = s
}
function amTogglePick(key: string) {
  const s = new Set(addMemberPicked.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  addMemberPicked.value = s
}

function commitAddMember() {
  if (!selectedCombo.value) { addMemberOpen.value = false; return }
  const rec = combosData.value[selectedCombo.value]
  if (!rec) { addMemberOpen.value = false; return }
  const type = addMemberType.value
  // 替换该类型下的成员为选中集合
  const others = (rec.members || []).filter(m => m.type !== type)
  const added: ComboMember[] = [...addMemberPicked.value].map(k => ({ type, key: k }))
  const members = [...others, ...added]
  combosData.value = {
    ...combosData.value,
    [selectedCombo.value]: { ...rec, members },
  }
  persistCombos()
  addMemberOpen.value = false
  const n = added.length
  showToast(`已设置 ${n} 项${COMBO_TYPE_META[type].label}`)
}
</script>

<template>
  <div class="clp-root">
    <!-- 顶部：类型切换 -->
    <div class="clp-top glass">
      <div class="type-tabs">
        <button v-for="t in TYPE_TABS" :key="t.key"
                class="type-tab" :class="{ active: activeType === t.key }"
                @click="activeType = t.key">
          <span class="tt-icon"><AppIcon :name="t.icon" /></span>
          <span>{{ t.label }}</span>
          <span class="tt-count">{{ t.key === 'combos' ? comboList.length : Object.keys(metaFor(t.key)).length }}</span>
        </button>
      </div>
      <div class="top-actions">
        <button v-if="activeType === 'combos'" class="btn sm ghost" @click="openNewCombo">＋ 新建组合技</button>
        <button v-else class="btn sm ghost" @click="openNewGroup">＋ 新建分组</button>
        <template v-if="canManage">
          <button v-if="!multiMode" class="btn sm ghost" @click="enterMultiMode()">☑ 多选</button>
          <template v-else>
            <span class="multi-badge">已选 {{ multiSel.size }}</span>
            <button class="btn sm ghost" @click="toggleSelectAll">{{ allSelected ? '取消全选' : '全选' }}</button>
            <button class="btn sm ghost danger" :disabled="!multiSel.size || deleting"
                    @click="deleteSelectedCaps">🗑 删除所选</button>
            <button class="btn sm ghost" @click="exitMultiMode">退出多选</button>
          </template>
        </template>
        <button class="btn sm ghost" @click="reload" :disabled="loading">
          <span v-if="loading">⟳</span><span v-else>⟳ 刷新</span>
        </button>
      </div>
    </div>

    <!-- 主体：左右分栏 -->
    <div class="clp-body" v-if="activeType !== 'combos'">
      <!-- 左侧：分组列表 -->
      <div class="clp-left glass"
           @dragover="onDragOverUngrouped"
           @drop="onDropUngrouped"
           :class="{ 'drag-over': dragOverTarget === '__ungrouped__' }">

        <!-- 分组 -->
        <template v-for="g in groupedList.groups" :key="g.name">
          <div class="group-header"
               :class="{ collapsed: collapsed.has(g.name), 'drag-over': dragOverTarget === g.name }"
               @click="toggleCollapse(g.name)"
               @dragover="onDragOverGroup($event, g.name)"
               @drop="onDropGroup($event, g.name)">
            <span class="g-arrow">{{ collapsed.has(g.name) ? '▸' : '▾' }}</span>
            <template v-if="editingGroup === g.name">
              <input v-model="editingGroupName" class="g-rename-input"
                     @blur="commitRenameGroup"
                     @keyup.enter="commitRenameGroup"
                     @keyup.escape="editingGroup = null"
                     @click.stop />
            </template>
            <template v-else>
              <span class="g-name" @dblclick.stop="startRenameGroup(g.name)">{{ g.name }}</span>
            </template>
            <span class="g-count">{{ g.keys.length }}</span>
            <span class="g-spacer"></span>
            <button class="g-btn ghost sm" @click.stop="startRenameGroup(g.name)" title="重命名">✎</button>
            <button class="g-btn ghost sm danger" @click.stop="deleteGroup(g.name)" title="删除分组">✕</button>
          </div>

          <div v-if="!collapsed.has(g.name)" class="group-items">
            <div v-for="k in g.keys" :key="k"
                 class="cap-item"
                 :class="{ selected: selectedKey === k && !multiMode, dragging: false, 'insert-before': dragInsertKey === k && dragOverTarget === g.name, 'multi-on': multiMode, 'multi-checked': multiSel.has(k) }"
                 draggable="true"
                 @click="onCapClick(k)"
                 @contextmenu.prevent="onCapContextMenu($event, k)"
                 @dragstart="onDragStart($event, k)"
                 @dragend="onDragEnd"
                 @dragover="onDragOverItem($event, k)">
              <span v-if="multiMode" class="ci-check">{{ multiSel.has(k) ? '✓' : '' }}</span>
              <span class="ci-icon">{{ activeType === 'commands' ? '⌨' : activeType === 'skills' ? '✨' : '🧩' }}</span>
              <span class="ci-label">{{ (metaFor(activeType)[k] as any)?.label || k }}</span>
              <span class="ci-key">{{ k }}</span>
            </div>
            <div v-if="g.keys.length === 0" class="empty-hint">（空分组，拖进来吧）</div>
          </div>
        </template>

        <!-- 未分组 -->
        <div class="group-header ungrouped-header"
             :class="{ 'drag-over': dragOverTarget === '__ungrouped__' }"
             @click="toggleCollapse('__ungrouped__')">
          <span class="g-arrow">
            {{ collapsed.has('__ungrouped__') ? '▸' : '▾' }}
          </span>
          <span class="g-name">未分组</span>
          <span class="g-count">{{ groupedList.ungrouped.length }}</span>
        </div>
        <div v-if="!collapsed.has('__ungrouped__')" class="group-items ungrouped-items">
          <div v-for="k in groupedList.ungrouped" :key="k"
               class="cap-item"
               :class="{ selected: selectedKey === k && !multiMode, 'insert-before': dragInsertKey === k && dragOverTarget === '__ungrouped__', 'multi-on': multiMode, 'multi-checked': multiSel.has(k) }"
               draggable="true"
               @click="onCapClick(k)"
               @contextmenu.prevent="onCapContextMenu($event, k)"
               @dragstart="onDragStart($event, k)"
               @dragend="onDragEnd"
               @dragover="onDragOverItem($event, k)">
            <span v-if="multiMode" class="ci-check">{{ multiSel.has(k) ? '✓' : '' }}</span>
            <span class="ci-icon">{{ activeType === 'commands' ? '⌨' : activeType === 'skills' ? '✨' : '🧩' }}</span>
            <span class="ci-label">{{ (metaFor(activeType)[k] as any)?.label || k }}</span>
            <span class="ci-key">{{ k }}</span>
          </div>
          <div v-if="groupedList.ungrouped.length === 0" class="empty-hint">（全部已分组）</div>
        </div>
      </div>

      <!-- 右侧：详情 -->
      <div class="clp-right glass">
        <template v-if="detail">
          <div class="detail-head">
            <span class="d-icon">{{ activeType === 'commands' ? '⌨' : activeType === 'skills' ? '✨' : '🧩' }}</span>
            <div class="d-title-area">
              <div class="d-label">{{ detail.meta.label || detail.key }}</div>
              <div class="d-key">{{ detail.key }}</div>
            </div>
            <div class="d-group-badge" v-if="detail.group">📁 {{ detail.group }}</div>
          </div>

          <div class="detail-body">
            <!-- 描述 -->
            <div v-if="detail.meta.description || detail.meta.desc || detail.meta.tool_description" class="d-block">
              <div class="d-block-title">描述</div>
              <div class="d-desc">{{ detail.meta.description || detail.meta.desc || detail.meta.tool_description }}</div>
            </div>

            <!-- 命令特有：参数 -->
            <div v-if="activeType === 'commands' && detail.meta.params?.length" class="d-block">
              <div class="d-block-title">参数</div>
              <div class="d-params">
                <code v-for="p in detail.meta.params" :key="p" class="d-param">{{ p }}</code>
              </div>
            </div>

            <!-- 技能特有：版本、触发词、标签、prompt 预览 -->
            <template v-if="activeType === 'skills'">
              <div v-if="detail.meta.version" class="d-kv"><span class="d-k-label">版本</span><span>{{ detail.meta.version }}</span></div>
              <div v-if="detail.meta.triggers?.length" class="d-block">
                <div class="d-block-title">触发词</div>
                <div class="d-params">
                  <code v-for="t in detail.meta.triggers" :key="t" class="d-param trigger">{{ t }}</code>
                </div>
              </div>
              <div v-if="detail.meta.tags?.length" class="d-block">
                <div class="d-block-title">标签</div>
                <div class="d-params">
                  <span v-for="tg in detail.meta.tags" :key="tg" class="d-tag">{{ tg }}</span>
                </div>
              </div>
              <div v-if="detail.meta.prompt" class="d-block">
                <div class="d-block-title">Prompt 预览</div>
                <pre class="d-prompt">{{ detail.meta.prompt.slice(0, 500) }}{{ detail.meta.prompt.length > 500 ? '…' : '' }}</pre>
              </div>
            </template>

            <!-- 插件特有：作者、版本、标签、参数 -->
            <template v-if="activeType === 'plugins'">
              <div class="d-kv-row">
                <div v-if="detail.meta.version" class="d-kv"><span class="d-k-label">版本</span><span>{{ detail.meta.version }}</span></div>
                <div v-if="detail.meta.author" class="d-kv"><span class="d-k-label">作者</span><span>{{ detail.meta.author }}</span></div>
              </div>
              <div v-if="detail.meta.tags?.length" class="d-block">
                <div class="d-block-title">标签</div>
                <div class="d-params">
                  <span v-for="tg in detail.meta.tags" :key="tg" class="d-tag">{{ tg }}</span>
                </div>
              </div>
              <div v-if="detail.meta.parameters?.length" class="d-block">
                <div class="d-block-title">参数</div>
                <div class="d-params-list">
                  <div v-for="p in detail.meta.parameters" :key="p.name" class="d-param-row">
                    <code class="d-param-name">{{ p.name }}</code>
                    <span v-if="p.type" class="d-param-type">{{ p.type }}</span>
                    <span v-if="p.required" class="d-param-req">必填</span>
                    <span v-if="p.description" class="d-param-desc">— {{ p.description }}</span>
                  </div>
                </div>
              </div>
            </template>

            <!-- 命令分组来源 -->
            <div v-if="activeType === 'commands'" class="d-kv"><span class="d-k-label">分组</span><span>{{ detail.group || '未分组' }}</span></div>
          </div>
        </template>

        <div v-else class="empty-detail">
          <div class="ed-icon">👈</div>
          <div>在左侧列表中点击一项能力查看详情</div>
        </div>
      </div>
    </div>

    <!-- ============ 组合技视图 ============ -->
    <div class="clp-body" v-else>
      <!-- 左侧：组合技列表 -->
      <div class="clp-left glass">
        <div class="combo-tip">
          <div class="combo-tip-icon">🎯</div>
          <div class="combo-tip-text">组合技可把<b>命令 / 技能 / 插件</b>混编成一个组合，挂载到智能体后自动展开其中的能力。</div>
        </div>

        <div v-for="c in comboList" :key="c.name"
             class="combo-item" :class="{ selected: selectedCombo === c.name }"
             @click="selectCombo(c.name)">
          <template v-if="editingComboName === c.name">
            <input v-model="editingComboNameVal" class="combo-rename"
                   @blur="commitRenameCombo" @keyup.enter="commitRenameCombo"
                   @keyup.escape="editingComboName = null" @click.stop autofocus />
          </template>
          <template v-else>
            <div class="combo-item-head">
              <span class="combo-name">{{ c.name }}</span>
              <span class="combo-actions">
                <button class="g-btn ghost sm" @click.stop="startRenameCombo(c.name)" title="重命名">✎</button>
                <button class="g-btn ghost sm danger" @click.stop="deleteCombo(c.name)" title="删除">✕</button>
              </span>
            </div>
            <div class="combo-summary">{{ comboSummary(c.counts) }}</div>
            <div v-if="c.description" class="combo-desc">{{ c.description }}</div>
          </template>
        </div>

        <div v-if="comboList.length === 0" class="empty-hint">
          （还没有组合技，点击右上角「＋ 新建组合技」创建）
        </div>
      </div>

      <!-- 右侧：组合技详情/编辑 -->
      <div class="clp-right glass">
        <template v-if="selectedComboDetail">
          <div class="detail-head">
            <span class="d-icon">🎯</span>
            <div class="d-title-area">
              <div class="d-label">{{ selectedComboDetail.name }}</div>
              <div class="d-key">{{ comboSummary(selectedComboDetail.counts) }}</div>
            </div>
            <div class="d-group-badge" v-if="selectedComboDetail.market_id">🛒 来自市场</div>
          </div>

          <div class="detail-body">
            <!-- 描述编辑 -->
            <div class="d-block">
              <div class="d-block-title">描述</div>
              <textarea class="combo-desc-input" rows="2"
                        :value="selectedComboDetail.description"
                        @input="onComboDescInput" @blur="onComboDescBlur"
                        placeholder="补充这个组合技的用途说明（可选）"></textarea>
            </div>

            <!-- 按类型分组的成员管理 -->
            <div v-for="t in COMBO_TYPES" :key="t" class="d-block combo-type-block">
              <div class="combo-type-head">
                <span class="combo-type-icon">{{ COMBO_TYPE_META[t].icon }}</span>
                <span class="combo-type-title">{{ COMBO_TYPE_META[t].label }}</span>
                <span class="combo-type-count">{{ selectedComboDetail.counts[t] }}</span>
                <button class="btn sm ghost" @click="openAddMember(t)">＋ 管理</button>
              </div>
              <div class="combo-member-chips">
                <span v-for="m in selectedComboDetail.members.filter(x => x.type === t)"
                      :key="t + ':' + m.key" class="combo-chip">
                  {{ memberLabel(m) }}
                  <button class="combo-chip-x" @click="removeMember(m)" title="移除">✕</button>
                </span>
                <span v-if="selectedComboDetail.counts[t] === 0" class="combo-empty-chip">未添加{{ COMBO_TYPE_META[t].label }}</span>
              </div>
            </div>
          </div>
        </template>

        <div v-else class="empty-detail">
          <div class="ed-icon">👈</div>
          <div>在左侧选择一个组合技查看 / 编辑</div>
        </div>
      </div>
    </div>

    <!-- 新建组合技弹窗 -->
    <Teleport to="body">
      <div v-if="newComboOpen" class="modal-mask" @click="newComboOpen = false">
        <div class="modal glass" @click.stop>
          <div class="modal-title">新建组合技</div>
          <input v-model="newComboName" class="input" placeholder="组合技名称"
                 @keyup.enter="commitNewCombo" @keyup.escape="newComboOpen = false" autofocus />
          <textarea v-model="newComboDesc" class="input" rows="2"
                    placeholder="用途描述（可选）" style="margin-top:8px;resize:vertical;"></textarea>
          <div class="modal-actions">
            <button class="btn ghost" @click="newComboOpen = false">取消</button>
            <button class="btn" @click="commitNewCombo">创建</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 添加组合技成员弹窗 -->
    <Teleport to="body">
      <div v-if="addMemberOpen" class="modal-mask" @click="addMemberOpen = false">
        <div class="modal glass" style="width:440px;max-height:80vh;display:flex;flex-direction:column;" @click.stop>
          <div class="modal-title">
            {{ COMBO_TYPE_META[addMemberType].icon }} 选择{{ COMBO_TYPE_META[addMemberType].label }}
          </div>
          <input v-model="addMemberQuery" class="input" placeholder="搜索…" />
          <div class="member-list">
            <div v-for="g in addMemberGroups" :key="g.name" class="am-group">
              <div class="am-group-head" @click="amToggleGroupCollapse(g.name)">
                <input type="checkbox"
                       :checked="amIsGroupSelected(g)"
                       :indeterminate="amIsGroupIndeterminate(g)"
                       @click.stop="amToggleGroup(g)" title="整组勾选" />
                <span class="am-group-name">{{ g.name }}</span>
                <span class="am-group-count">{{ g.items.length }}</span>
                <span class="am-group-arrow">{{ addMemberCollapsed.has(g.name) ? '▸' : '▾' }}</span>
              </div>
              <div v-if="!addMemberCollapsed.has(g.name)" class="am-group-items">
                <label v-for="c in g.items" :key="c.key" class="member-row" @click="amTogglePick(c.key)">
                  <input type="checkbox" :checked="addMemberPicked.has(c.key)" readonly />
                  <span class="member-label">{{ c.label }}</span>
                  <code class="member-key">{{ c.key }}</code>
                </label>
              </div>
            </div>
            <div v-if="addMemberTotalCount === 0" class="empty-hint">没有匹配的能力</div>
          </div>
          <div class="modal-actions">
            <button class="btn ghost" @click="addMemberOpen = false">取消</button>
            <button class="btn" @click="commitAddMember">确定（{{ addMemberPicked.size }} 项）</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 新建分组弹窗 -->
    <Teleport to="body">
      <div v-if="newGroupNameOpen" class="modal-mask" @click="newGroupNameOpen = false">
        <div class="modal glass" @click.stop>
          <div class="modal-title">新建分组</div>
          <input v-model="newGroupName" class="input" placeholder="分组名称"
                 @keyup.enter="commitNewGroup" @keyup.escape="newGroupNameOpen = false"
                 autofocus />
          <div class="modal-actions">
            <button class="btn ghost" @click="newGroupNameOpen = false">取消</button>
            <button class="btn" @click="commitNewGroup">创建</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 能力右键菜单（仅技能/插件） -->
    <Teleport to="body">
      <div v-if="capCtx" class="cap-ctx glass"
           :style="{ left: capCtx.x + 'px', top: capCtx.y + 'px' }"
           @click.stop @contextmenu.prevent>
        <div class="ctx-title">{{ capCtxTitle }}</div>
        <div v-if="!multiMode" class="ctx-item" @click.stop="enterMultiMode(capCtx.key)">☑ 多选模式</div>
        <div class="ctx-item" @click.stop="toggleSelectAll">{{ allSelected ? '✕ 取消全选' : '☑ 全选当前列表' }}</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click.stop="ctxMoveSub = !ctxMoveSub">
          📁 移到分组
          <span class="ctx-arrow">{{ ctxMoveSub ? '▾' : '▸' }}</span>
        </div>
        <div v-if="ctxMoveSub" class="ctx-sub">
          <div class="ctx-item sub" v-for="g in groupNamesForMove" :key="g"
               @click.stop="moveSelectedTo(g)">{{ g }}</div>
          <div v-if="!groupNamesForMove.length" class="ctx-item sub disabled">暂无分组</div>
          <div class="ctx-item sub" @click.stop="moveSelectedTo(null)">（未分组）</div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item danger" @click.stop="deleteSelectedCaps">
          🗑 删除{{ multiSel.size > 1 ? `所选（${multiSel.size}）` : '' }}
        </div>
        <div v-if="multiMode" class="ctx-sep"></div>
        <div v-if="multiMode" class="ctx-item" @click.stop="exitMultiMode">退出多选</div>
      </div>
    </Teleport>

    <Transition name="fade">
      <div v-if="toast" class="toast" :class="toast.kind">{{ toast.msg }}</div>
    </Transition>
  </div>
</template>

<style scoped>
.clp-root { display: flex; flex-direction: column; gap: 10px; height: 100%; min-height: 0; }

.clp-top {
  display: flex; align-items: center; gap: 12px; padding: 8px 12px; flex-shrink: 0;
}
.type-tabs { display: flex; gap: 4px; }
.type-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px; border-radius: 7px; border: 1px solid transparent; background: transparent;
  font-size: calc(13px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7);
  cursor: pointer; transition: all .15s;
  &:hover { background: rgba(var(--surface-rgb), 0.08); color: rgb(var(--text-rgb)); }
  &.active {
    background: rgba(var(--accent-rgb), 0.14);
    border-color: rgba(var(--accent-rgb), 0.3);
    color: var(--accent-light);
  }
}
.tt-icon { font-size: calc(14px * var(--font-scale)); }
.tt-count {
  padding: 1px 7px; border-radius: 999px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.2); opacity: .8;
}
.top-actions { margin-left: auto; display: flex; gap: 6px; }

/* 主体 */
.clp-body {
  flex: 1; min-height: 0; display: flex; gap: 10px;
}
.clp-left {
  width: 320px; flex-shrink: 0;
  padding: 8px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 2px;
}
.clp-right {
  flex: 1; min-width: 0; padding: 14px 18px; overflow-y: auto;
}

/* 分组 */
.group-header {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 8px; border-radius: 6px; cursor: pointer; user-select: none;
  font-size: calc(12px * var(--font-scale));
  &:hover { background: rgba(var(--surface-rgb), 0.08); }
  &.collapsed .group-items { display: none; }
  &.drag-over {
    background: rgba(var(--accent-rgb), 0.1);
    outline: 2px dashed rgba(var(--accent-rgb), 0.5);
  }
  &.ungrouped-header { margin-top: 10px; border-top: 1px solid rgba(var(--surface-rgb), 0.12); padding-top: 10px; }
}
.g-arrow { opacity: .5; font-size: calc(10px * var(--font-scale)); width: 12px; text-align: center; }
.g-name { font-weight: 600; flex: 1; }
.g-count {
  padding: 1px 7px; border-radius: 999px; font-size: calc(10px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.15); color: rgba(var(--text-rgb), 0.6);
}
.g-spacer { flex: 1; }
.g-btn {
  opacity: 0;
  transition: opacity .15s, background .15s, color .15s;
  padding: 2px 6px;
  font-size: calc(11px * var(--font-scale));
  /* 关键：必须显式给颜色。按钮默认走浏览器 UA 的 ButtonText（黑/白），
     在深色玻璃卡片上会和背景同色 → 编辑/删除图标看不见 */
  color: rgba(var(--text-rgb), 0.72);
  background: transparent;
  border: 0;
  border-radius: 4px;
  line-height: 1.2;
  cursor: pointer;
}
.group-header:hover .g-btn { opacity: 0.85; }
.combo-item:hover .g-btn { opacity: 0.85; }
.g-btn:hover {
  opacity: 1 !important;
  color: rgb(var(--text-rgb));
  background: rgba(var(--surface-rgb), 0.12);
}
.g-btn.danger { color: #ff9a9a; }
.g-btn.danger:hover {
  color: #ffb0b0;
  background: rgba(232, 92, 92, 0.18);
}
.g-rename-input {
  flex: 1; padding: 2px 6px; border-radius: 4px;
  background: rgba(var(--surface-rgb), 0.15); border: 1px solid rgba(var(--accent-rgb), 0.4);
  color: rgb(var(--text-rgb)); font-size: calc(12px * var(--font-scale));
}

/* 未分组拖拽高亮 */
.clp-left.drag-over {
  outline: 2px dashed rgba(var(--accent-rgb), 0.5);
}

/* 能力项 */
.group-items { padding-left: 4px; display: flex; flex-direction: column; gap: 1px; }
.cap-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 5px; cursor: pointer; user-select: none;
  font-size: calc(12px * var(--font-scale));
  border: 1px solid transparent;
  transition: background .1s;
  &:hover { background: rgba(var(--surface-rgb), 0.08); }
  &.selected {
    background: rgba(var(--accent-rgb), 0.12);
    border-color: rgba(var(--accent-rgb), 0.35);
  }
  &.dragging { opacity: .4; }
  &.insert-before { box-shadow: inset 2px 0 0 var(--accent); }
}
.ci-icon { width: 16px; text-align: center; flex-shrink: 0; opacity: .7; }
/* 中文名称（label）：完整显示，不压缩、不省略 */
.ci-label { flex-shrink: 0; white-space: nowrap; }
/* 英文 key：占用剩余空间，过长时省略号缩略 */
.ci-key {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: Consolas, monospace; font-size: calc(10px * var(--font-scale));
  opacity: .4;
}

.empty-hint { font-size: calc(11px * var(--font-scale)); opacity: .4; padding: 6px 10px; }

/* 右侧详情 */
.detail-head {
  display: flex; align-items: center; gap: 12px;
  padding-bottom: 14px; margin-bottom: 14px;
  border-bottom: 1px solid rgba(var(--surface-rgb), 0.1);
}
.d-icon { font-size: 28px; }
.d-title-area { flex: 1; min-width: 0; }
.d-label { font-size: calc(16px * var(--font-scale)); font-weight: 700; }
.d-key {
  font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale));
  opacity: .5; margin-top: 2px;
}
.d-group-badge {
  padding: 3px 10px; border-radius: 999px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.15); color: var(--accent-light);
}

.detail-body { display: flex; flex-direction: column; gap: 14px; }
.d-block-title {
  font-size: calc(11px * var(--font-scale)); font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.5);
  margin-bottom: 6px;
}
.d-desc { font-size: calc(12px * var(--font-scale)); line-height: 1.6; color: rgba(var(--text-rgb), 0.85); }
.d-params { display: flex; flex-wrap: wrap; gap: 5px; }
.d-param {
  padding: 2px 8px; border-radius: 4px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.18);
  font-family: Consolas, monospace;
  &.trigger { background: rgba(var(--accent-rgb), 0.18); color: var(--accent-light); }
}
.d-tag {
  padding: 2px 8px; border-radius: 4px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.12); color: var(--accent-light);
}
.d-kv { display: flex; gap: 10px; font-size: calc(12px * var(--font-scale)); }
.d-kv-row { display: flex; gap: 16px; flex-wrap: wrap; }
.d-k-label { color: rgba(var(--text-rgb), 0.5); min-width: 42px; }
.d-prompt {
  padding: 10px 12px; border-radius: 6px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.1);
  font-family: Consolas, monospace; white-space: pre-wrap; max-height: 200px; overflow-y: auto;
  margin: 0;
}
.d-params-list { display: flex; flex-direction: column; gap: 4px; }
.d-param-row {
  display: flex; align-items: baseline; gap: 8px;
  padding: 4px 8px; border-radius: 4px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.08);
}
.d-param-name { font-family: Consolas, monospace; font-weight: 600; }
.d-param-type { opacity: .5; }
.d-param-req { color: #e09090; font-size: calc(10px * var(--font-scale)); }
.d-param-desc { opacity: .6; }

/* 空状态 */
.empty-detail {
  height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 10px;
  color: rgba(var(--text-rgb), 0.45); font-size: calc(13px * var(--font-scale));
}
.ed-icon { font-size: 32px; }

/* ============ 组合技 ============ */
.combo-tip {
  display: flex; gap: 8px; padding: 8px 10px; border-radius: 7px;
  background: rgba(var(--accent-rgb), 0.08); border: 1px solid rgba(var(--accent-rgb), 0.18);
  font-size: calc(11px * var(--font-scale)); line-height: 1.5; color: rgba(var(--text-rgb), 0.8);
  margin-bottom: 6px;
}
.combo-tip-icon { flex-shrink: 0; }
.combo-tip-text { flex: 1; min-width: 0; }
.combo-tip-text b { color: var(--accent-light); }

.combo-item {
  padding: 8px 10px; border-radius: 7px; cursor: pointer;
  border: 1px solid transparent; transition: background .1s, border-color .1s;
  &:hover { background: rgba(var(--surface-rgb), 0.08); }
  &.selected {
    background: rgba(var(--accent-rgb), 0.12);
    border-color: rgba(var(--accent-rgb), 0.35);
  }
}
.combo-item-head { display: flex; align-items: center; gap: 6px; }
.combo-name { font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.combo-actions { opacity: 0; transition: opacity .15s; }
.combo-item:hover .combo-actions { opacity: 1; }
.combo-summary { font-size: calc(11px * var(--font-scale)); opacity: .6; margin-top: 2px; }
.combo-desc {
  font-size: calc(11px * var(--font-scale)); opacity: .5; margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.combo-rename {
  width: 100%; padding: 3px 6px; border-radius: 4px;
  background: rgba(var(--surface-rgb), 0.15); border: 1px solid rgba(var(--accent-rgb), 0.4);
  color: rgb(var(--text-rgb)); font-size: calc(12px * var(--font-scale));
}
.combo-desc-input {
  width: 100%; padding: 7px 10px; border-radius: 6px;
  background: rgba(var(--surface-rgb), 0.1); border: 1px solid rgba(var(--surface-rgb), 0.2);
  color: rgb(var(--text-rgb)); font-size: calc(12px * var(--font-scale));
  font-family: inherit; resize: vertical; outline: none;
  &:focus { border-color: rgba(var(--accent-rgb), 0.5); }
}
.combo-type-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.combo-type-icon { font-size: calc(14px * var(--font-scale)); }
.combo-type-title { font-weight: 600; font-size: calc(12px * var(--font-scale)); }
.combo-type-count {
  padding: 0 7px; border-radius: 999px; font-size: calc(10px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.15); color: rgba(var(--text-rgb), 0.6);
}
.combo-member-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.combo-chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 999px; font-size: calc(11px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.12); color: var(--accent-light);
  border: 1px solid rgba(var(--accent-rgb), 0.2);
}
.combo-chip-x {
  border: none; background: transparent; color: inherit; cursor: pointer;
  opacity: .55; padding: 0 2px; font-size: calc(10px * var(--font-scale));
  &:hover { opacity: 1; }
}
.combo-empty-chip {
  font-size: calc(11px * var(--font-scale)); opacity: .35; padding: 3px 0;
}
.member-list { flex: 1; overflow-y: auto; margin-top: 10px; display: flex; flex-direction: column; gap: 2px; }
.am-group { margin-bottom: 4px; }
.am-group-head {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 6px; cursor: pointer; user-select: none;
  font-size: calc(12px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.06);
  &:hover { background: rgba(var(--surface-rgb), 0.1); }
}
.am-group-name { font-weight: 600; flex: 1; }
.am-group-count {
  padding: 0 7px; border-radius: 999px; font-size: calc(10px * var(--font-scale));
  background: rgba(var(--surface-rgb), 0.18); color: rgba(var(--text-rgb), 0.6);
}
.am-group-arrow { opacity: .5; font-size: calc(10px * var(--font-scale)); }
.am-group-items { display: flex; flex-direction: column; gap: 1px; padding-left: 22px; }
.member-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 8px; border-radius: 5px; cursor: pointer;
  font-size: calc(12px * var(--font-scale));
  &:hover { background: rgba(var(--surface-rgb), 0.08); }
}
.member-label { flex-shrink: 0; }
.member-key {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: Consolas, monospace; font-size: calc(10px * var(--font-scale)); opacity: .4;
  text-align: right;
}

/* ============ 多选 & 右键菜单 ============ */
.multi-badge {
  display: inline-flex; align-items: center;
  padding: 2px 9px; border-radius: 999px;
  font-size: calc(11px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.14);
  border: 1px solid rgba(var(--accent-rgb), 0.25);
  color: var(--accent-light);
}
.ci-check {
  width: 14px; height: 14px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 4px; border: 1px solid rgba(var(--surface-rgb), 0.35);
  font-size: calc(10px * var(--font-scale)); line-height: 1;
  color: rgb(var(--text-rgb));
}
.cap-item.multi-checked {
  background: rgba(var(--accent-rgb), 0.12);
  border-color: rgba(var(--accent-rgb), 0.32);
}
.cap-item.multi-checked .ci-check {
  background: rgba(var(--accent-rgb), 0.55);
  border-color: rgba(var(--accent-rgb), 0.7);
}
.cap-item.multi-on { cursor: pointer; }

.cap-ctx {
  position: fixed; z-index: 2200; min-width: 190px; max-width: 240px;
  padding: 5px; border-radius: 8px;
  max-height: 70vh; overflow-y: auto;
  box-shadow: 0 10px 30px rgba(var(--shadow-rgb), 0.28);
  font-size: calc(12px * var(--font-scale));
}
.ctx-title {
  padding: 6px 9px 7px; font-weight: 600;
  color: rgba(var(--text-rgb), 0.55);
  font-size: calc(11px * var(--font-scale));
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ctx-item {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 9px; border-radius: 6px; cursor: pointer; user-select: none;
  color: rgba(var(--text-rgb), 0.85);
}
.ctx-item:hover { background: rgba(var(--surface-rgb), 0.12); }
.ctx-item.danger { color: #e08080; }
.ctx-item.danger:hover { background: rgba(220, 100, 100, 0.14); }
.ctx-item.disabled { opacity: .4; cursor: default; }
.ctx-item.sub { padding-left: 22px; }
.ctx-arrow { margin-left: auto; opacity: .5; font-size: calc(10px * var(--font-scale)); }
.ctx-sep { height: 1px; margin: 4px 6px; background: rgba(var(--surface-rgb), 0.14); }
.ctx-sub {
  display: flex; flex-direction: column;
  max-height: 38vh; overflow-y: auto;
  border-left: 2px solid rgba(var(--accent-rgb), 0.25);
  margin-left: 8px;
}
.btn.danger {
  color: #e08080;
  border-color: rgba(220, 100, 100, 0.35);
}
.btn.danger:hover:not(:disabled) { background: rgba(220, 100, 100, 0.14); }

/* Toast */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 8px 18px; border-radius: 999px; font-size: calc(13px * var(--font-scale)); z-index: 3000;
  backdrop-filter: blur(6px);
  &.ok { background: rgba(100, 200, 160, 0.2); color: #80d8b0; border: 1px solid rgba(100, 200, 160, 0.35); }
  &.err { background: rgba(220, 100, 100, 0.2); color: #e09090; border: 1px solid rgba(220, 100, 100, 0.35); }
}
.fade-enter-active, .fade-leave-active { transition: opacity .2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* 弹窗 */
.modal-mask {
  position: fixed; inset: 0; z-index: 2000;
  background: rgba(var(--shadow-rgb), 0.18); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
}
.modal { width: 360px; padding: 18px; }
.modal-title { font-weight: 600; margin-bottom: 12px; font-size: calc(14px * var(--font-scale)); }
.modal .input {
  width: 100%; padding: 7px 10px; border-radius: 6px;
  background: rgba(var(--surface-rgb), 0.15);
  border: 1px solid rgba(var(--surface-rgb), 0.2);
  color: rgb(var(--text-rgb)); font-size: calc(13px * var(--font-scale));
  outline: none;
  &:focus { border-color: rgba(var(--accent-rgb), 0.5); }
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
</style>
