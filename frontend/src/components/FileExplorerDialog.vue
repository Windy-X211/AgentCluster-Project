<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-bg" @click.self="close">
      <div class="modal file-explorer">
        <div class="modal-header">
          <span class="modal-title">📂 数据文件浏览器</span>
          <div class="path-bar">{{ currentPath }}</div>
          <button class="btn sm ghost" @click="close">✕</button>
        </div>
        <div class="modal-body">
          <!-- 左侧：目录树 -->
          <div class="tree-pane">
            <div class="tree-toolbar">
              <button class="btn sm ghost" @click="goUp" :disabled="!canGoUp">↑ 上级</button>
              <button class="btn sm ghost" @click="loadDir" title="刷新">⟳</button>
            </div>
            <div class="tree-root" @dblclick="openRoot">根目录</div>
            <div class="tree-list">
              <template v-for="item in items" :key="item.path">
                <div
                  class="tree-item"
                  :class="{ selected: selectedPath === item.path, dir: item.is_dir, file: !item.is_dir }"
                  @click="selectItem(item)"
                  @dblclick="openItem(item)"
                >
                  <span class="tree-ico">{{ iconFor(item) }}</span>
                  <span class="tree-name">{{ item.name }}</span>
                  <span v-if="!item.is_dir" class="tree-size">{{ formatSize(item.size) }}</span>
                </div>
              </template>
              <div v-if="!items.length" class="tree-empty">(空目录)</div>
            </div>
          </div>

          <!-- 右侧：编辑区 -->
          <div class="editor-pane">
            <div v-if="!editing" class="editor-empty">
              <div class="empty-big">🗂️</div>
              <div class="empty-hint">双击左侧文件打开编辑</div>
              <div class="empty-hint sm">支持 json / yaml / md / py / txt / toml 等文本格式</div>
            </div>
            <div v-else class="editor-area">
              <div class="editor-bar">
                <span class="editor-file">📄 {{ editing.name }}</span>
                <span class="editor-meta mono">{{ formatSize(editing.size) }} · {{ editing.ext }}</span>
                <span v-if="isDirty" class="editor-dirty">● 未保存</span>
                <div class="editor-actions">
                  <button class="btn sm ghost" @click="revert" :disabled="!isDirty">↺ 还原</button>
                  <button class="btn sm ghost danger" @click="confirmDelete">🗑</button>
                  <button class="btn sm primary" @click="save" :disabled="!isDirty || saving">
                    {{ saving ? '保存中…' : '💾 保存' }}
                  </button>
                </div>
              </div>
              <textarea
                class="editor-text"
                v-model="editContent"
                spellcheck="false"
                :class="'lang-' + editing.ext"
                @keydown.ctrl.enter.prevent="save"
                placeholder="文件内容…"
              ></textarea>
              <div class="editor-footer">Ctrl+Enter 保存 · 点击文件树另一文件自动提示保存</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '@/api'

const props = defineProps<{
  visible: boolean
  initialPath?: string
}>()
const emit = defineEmits<{ (e: 'update:visible', v: boolean): void; (e: 'saved'): void }>()

const rootPath = ref('')
const currentPath = ref('')
const items = ref<any[]>([])

const selectedPath = ref<string | null>(null)
const editing = ref<{ path: string; name: string; ext: string; size: number; original: string } | null>(null)
const editContent = ref('')
const isDirty = ref(false)
const saving = ref(false)

const canGoUp = computed(() => {
  if (!rootPath.value || !currentPath.value) return false
  const cur = currentPath.value.replace(/\\/g, '/')
  const root = rootPath.value.replace(/\\/g, '/')
  return cur !== root
})

function goUp() {
  if (!canGoUp.value) return
  const p = currentPath.value.replace(/\\/g, '/')
  const root = rootPath.value.replace(/\\/g, '/')
  if (p === root) return
  const parts = p.split('/')
  parts.pop()
  currentPath.value = parts.join('/')
  loadDir()
}

function openRoot() {
  currentPath.value = rootPath.value
  loadDir()
}

async function loadDir() {
  try {
    const r = await api.fs.list(currentPath.value)
    items.value = r.items || []
    currentPath.value = r.path
    if (!rootPath.value) rootPath.value = r.root
  } catch (e: any) {
    alert('加载目录失败：' + (e?.response?.data?.detail || e.message))
  }
}

function iconFor(item: any): string {
  if (item.is_dir) return '📁'
  const ext = item.ext
  if (['json', 'yaml', 'yml'].includes(ext)) return '🗂'
  if (['py'].includes(ext)) return '🐍'
  if (['md'].includes(ext)) return '📝'
  if (['txt', 'log'].includes(ext)) return '📄'
  if (['svg', 'png', 'jpg'].includes(ext)) return '🖼'
  return '📎'
}

function formatSize(n: number): string {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

function selectItem(item: any) {
  selectedPath.value = item.path
}

async function openItem(item: any) {
  if (item.is_dir) {
    currentPath.value = item.path
    await loadDir()
    return
  }
  // 先检查当前编辑的是否有未保存
  if (isDirty.value && editing.value && editing.value.path !== item.path) {
    const ok = confirm(`当前文件 "${editing.value.name}" 有未保存修改，丢弃并打开新文件？`)
    if (!ok) return
    isDirty.value = false
  }
  await openFile(item.path)
}

async function openFile(path: string) {
  try {
    const r = await api.fs.read(path)
    editing.value = {
      path: r.path,
      name: r.name,
      ext: r.ext,
      size: r.size,
      original: r.content,
    }
    editContent.value = r.content
    isDirty.value = false
    selectedPath.value = path
  } catch (e: any) {
    alert('打开失败：' + (e?.response?.data?.detail || e.message))
  }
}

watch(editContent, (v) => {
  if (editing.value) {
    isDirty.value = v !== editing.value.original
  }
})

async function save() {
  if (!editing.value || !isDirty.value) return
  saving.value = true
  try {
    await api.fs.write(editing.value.path, editContent.value)
    editing.value.original = editContent.value
    isDirty.value = false
    emit('saved')
  } catch (e: any) {
    alert('保存失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}

function revert() {
  if (!editing.value) return
  editContent.value = editing.value.original
  isDirty.value = false
}

async function confirmDelete() {
  if (!editing.value) return
  if (!confirm(`确定删除文件 "${editing.value.name}"？`)) return
  try {
    await api.fs.del(editing.value.path)
    editing.value = null
    editContent.value = ''
    isDirty.value = false
    await loadDir()
  } catch (e: any) {
    alert('删除失败：' + (e?.response?.data?.detail || e.message))
  }
}

function close() {
  if (isDirty.value && editing.value) {
    const ok = confirm(`文件 "${editing.value.name}" 有未保存修改，关闭将丢弃。确认关闭？`)
    if (!ok) return
  }
  emit('update:visible', false)
}

onMounted(() => {
  if (props.initialPath) {
    currentPath.value = props.initialPath
  }
  loadDir()
})

// 每次打开弹窗时导航到目标目录（initialPath 只在 onMounted 读一次是不够的）
watch(
  () => props.visible,
  (v) => {
    if (!v) return
    currentPath.value = props.initialPath || rootPath.value || ''
    loadDir()
  }
)
</script>

<style scoped>
/* 遮罩层：组件内必须自定义，否则 Teleport 到 body 后会渲染到视口外 */
.modal-bg {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1500;
}
/* 弹窗面板 */
.modal {
  background: rgba(16, 22, 40, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  overflow: hidden;
}
.modal.file-explorer {
  width: 960px;
  max-width: 92vw;
  height: 640px;
  max-height: 92vh;
  display: flex;
  flex-direction: column;
}
.modal-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255,255,255,.08);
}
.modal-title { font-weight: 600; }
.path-bar {
  flex: 1;
  font-family: Consolas, monospace;
  font-size: 12px;
  color: rgba(255,255,255,.6);
  background: rgba(255,255,255,.04);
  padding: 4px 10px;
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.modal-body {
  flex: 1;
  display: flex;
  flex-direction: row;
  overflow: hidden;
}

.tree-pane {
  width: 280px;
  min-width: 220px;
  border-right: 1px solid rgba(255,255,255,.08);
  display: flex;
  flex-direction: column;
  background: rgba(255,255,255,.02);
}
.tree-toolbar {
  display: flex;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid rgba(255,255,255,.06);
}
.tree-root {
  padding: 8px 12px;
  font-size: 12px;
  color: rgba(255,255,255,.5);
  border-bottom: 1px solid rgba(255,255,255,.04);
  cursor: pointer;
  font-family: Consolas, monospace;
}
.tree-root:hover { background: rgba(110,168,255,.08); }
.tree-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.tree-item {
  padding: 5px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: background .15s;
  user-select: none;
}
.tree-item:hover { background: rgba(110,168,255,.1); }
.tree-item.selected { background: rgba(110,168,255,.18); color: #e8f0ff; }
.tree-item.dir .tree-name { font-weight: 500; }
.tree-ico { font-size: 15px; }
.tree-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-size { font-size: 11px; opacity: .5; font-family: Consolas, monospace; }
.tree-empty { padding: 20px; text-align: center; opacity: .4; font-size: 12px; }

/* 编辑区 */
.editor-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 320px;
}
.editor-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: rgba(255,255,255,.4);
}
.empty-big { font-size: 48px; opacity: .5; }
.empty-hint { font-size: 14px; }
.empty-hint.sm { font-size: 12px; opacity: .7; }

.editor-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.editor-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid rgba(255,255,255,.08);
  background: rgba(0,0,0,.15);
}
.editor-file { font-weight: 500; font-size: 13px; }
.editor-meta { font-size: 11px; opacity: .5; }
.editor-dirty { color: #ffcc66; font-size: 12px; }
.editor-actions { margin-left: auto; display: flex; gap: 6px; }
.editor-text {
  flex: 1;
  width: 100%;
  background: #0d1117;
  color: #e6edf3;
  border: none;
  padding: 14px 16px;
  font-family: "Cascadia Code", Consolas, "Courier New", monospace;
  font-size: 13px;
  line-height: 1.55;
  resize: none;
  outline: none;
  tab-size: 2;
}
.editor-text.lang-json { color: #7ee787; }
.editor-text.lang-yaml, .editor-text.lang-yml { color: #79c0ff; }
.editor-text.lang-md { color: #d2a8ff; }
.editor-text.lang-py { color: #ffa657; }
.editor-footer {
  padding: 4px 14px;
  font-size: 11px;
  color: rgba(255,255,255,.35);
  border-top: 1px solid rgba(255,255,255,.06);
  background: rgba(0,0,0,.12);
}

.btn.sm.danger { background: rgba(255,80,80,.15); color: #ff9090; border-color: rgba(255,80,80,.25); }
.btn.sm.danger:hover { background: rgba(255,80,80,.25); }
</style>
