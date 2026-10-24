<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

interface Region { selector: string; name: string; icon: string; text: string }

const regions: Region[] = [
  {
    selector: '.region-settings', name: '设置中心', icon: '⚙️',
    text: '点击进入设置中心。\n· 模型接口：配置 API 地址、密钥、可用模型\n· 外观设置：配色方案 / 字号 / 智能体头像风格（差异化或统一）\n· 语音识别：安装 / 启用 SenseVoice，在对话中使用 🎤 同声传译\n· 数据存储：切换存储位置，查看并打开智能体、画布、聊天等数据目录\n· 新手引导：再次查看流程或区域引导',
  },
  {
    selector: '.region-plugins', name: '能力管理', icon: '🛠️',
    text: '点击进入能力管理页面，包含四个 Tab：\n· ⚡ 默认能力：新建智能体时自动勾选的命令 / 技能 / 插件\n· 🔐 能力参数预设：为插件 / 技能预填固定参数（如邮箱发送方地址），并标记必填项\n· 🧪 插件调试：选择插件目录 → 加载 → 填参数 → 运行，手动验证插件可用性\n· 📦 依赖安装：查看插件 / 技能的 pip、npm、external 依赖状态与安装提示',
  },
  {
    selector: '.canvases-pane', name: '画布列表', icon: '🗂️',
    text: '管理所有画布（集群编排）。\n· 点击切换当前画布\n· ＋ 按钮新建画布\n· 双击或右键可以重命名\n· 右键可删除画布\n· 拖动画布可改变顺序',
  },
  {
    selector: '.left', name: '智能体列表区', icon: '📋',
    text: '管理所有智能体的入口。\n· 按组（📁 图标）进行分类管理\n· 顶部 ＋ 新建组 / 组内 ＋ 新建智能体\n· 拖拽组或智能体可以改变顺序\n· 右键组：✏️ 重命名 / 🗑 删除 / ➕ 新建智能体\n· 右键智能体：✏️ 编辑 / 🗑 删除 / ➕ 归纳到分组\n· 拖拽智能体到画布创建节点\n· ◂ / ▸ 顶部按钮折叠 / 展开列表',
  },
  {
    selector: '.center', name: '画布区', icon: '🎨',
    text: '集群编排的核心画布。\n· 拖拽智能体到画布创建节点\n· 点击节点 🔗 连线按钮拖到另一节点连线\n· 双击连线删除\n· 点击节点 💬 对话按钮选择独立/集群对话（独立对话：智能体不感知其他智能体的存在。集群对话：智能体可与其他智能体协同合作），可以在当前会话中切换对话对象\n· 双击节点隐藏/显示消息泡泡\n· 右键节点：✏️ 编辑智能体 / 感知范围 / 触发（运行画布后如何触发智能体开始工作）/ ⏸ 停用 · ▶ 启用 / 🗑 从此画布移除\n· 右键空白区：重置视图，可自动调整画布缩放\n· 🗂 工作区：为智能体设置工作文件夹，支持预览、编辑、运行、🗑 回收站；📌 钉住到画布内\n· 工具栏：▶ 运行 / ⏹ 停止 / 串并行切换 / 📁 工作区 / 📋 规则 / 🔍 缩放 / 💾 保存 / 🗑 删除画布\n· 对于画布，支持滚轮缩放，拖动平移',
  },
  {
    selector: '.right', name: '对话区', icon: '💬',
    text: '与智能体实时对话的窗口。\n· 顶部 ＋ 独立 / ＋ 集群 新建或切换会话\n· 底部输入框发送消息，可拖入文件\n· 🎤 语音输入（需启用语音识别）\n· 查看流式回复\n· (▾过程) 可展开智能体 🧠 思考 / 工具调用等完整工作过程\n· 右键用户头像可更换\n· 右键用户消息可复制/删除/撤回/还原文件\n· 右键智能体消息可复制/删除\n· 支持图片、文件、表格展示',
  },
  {
    selector: '.topbar', name: '顶部栏', icon: '🔧',
    text: '全局导航与入口。\n· 🛠 能力管理：默认能力、参数预设、插件调试、依赖安装\n· ⚙ 设置：模型接口 / 外观 / 语音识别 / 数据存储 / 新手引导\n· 设置中心可再次开启新手引导\n· 支持四套配色方案与字体缩放',
  },
]

const currentSelector = ref('')
const current = computed(() => regions.find(r => r.selector === currentSelector.value) || null)
const visited = ref<Set<string>>(new Set())

function onMove(e: MouseEvent) {
  const t = e.target as HTMLElement | null
  if (!t || !t.closest) return
  let sel = ''
  for (const r of regions) {
    if (t.closest(r.selector)) { sel = r.selector; break }
  }
  if (sel && sel !== currentSelector.value) {
    currentSelector.value = sel
    if (sel) visited.value.add(sel)
  }
}

const panelPos = ref({ x: 0, y: 0 })
let dragging = false
let dragStart = { mx: 0, my: 0, px: 0, py: 0 }

function initPanelPos() {
  panelPos.value = { x: window.innerWidth - 460, y: 60 }
}
function onHeadDown(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('.rg-close')) return
  dragging = true
  dragStart = { mx: e.clientX, my: e.clientY, px: panelPos.value.x, py: panelPos.value.y }
  document.addEventListener('mousemove', onDragMove)
  document.addEventListener('mouseup', onDragUp)
}
function onDragMove(e: MouseEvent) {
  if (!dragging) return
  const x = dragStart.px + (e.clientX - dragStart.mx)
  const y = dragStart.py + (e.clientY - dragStart.my)
  panelPos.value = {
    x: Math.max(0, Math.min(x, window.innerWidth - 440)),
    y: Math.max(0, Math.min(y, window.innerHeight - 60)),
  }
}
function onDragUp() {
  dragging = false
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragUp)
}

onMounted(() => {
  initPanelPos()
  document.addEventListener('mouseover', onMove)
})
onBeforeUnmount(() => {
  document.removeEventListener('mouseover', onMove)
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragUp)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="rg-mask">
      <div class="rg-panel popup-surface" :style="{ left: panelPos.x + 'px', top: panelPos.y + 'px' }">
        <div class="rg-head" @mousedown="onHeadDown">
          <span class="rg-title">🖱️ 区域功能引导</span>
          <button class="rg-close" @click="emit('close')">✕</button>
        </div>
        <div class="rg-hint">将鼠标移至任一区域，此处自动显示其功能说明（可拖动本窗口）</div>
        <div class="rg-body">
          <template v-if="current">
            <div class="rg-card" :key="current.selector">
              <div class="rg-card-head">
                <span class="rg-card-icon">{{ current.icon }}</span>
                <span class="rg-card-name">{{ current.name }}</span>
              </div>
              <div class="rg-card-text">{{ current.text }}</div>
            </div>
          </template>
          <div v-else class="rg-empty">
            <div class="rg-empty-icon">👆</div>
            <div class="rg-empty-text">移动鼠标到能力管理、设置、画布列表、智能体列表、画布区、对话区或顶部栏</div>
          </div>
        </div>
        <div class="rg-progress">
          <span v-for="r in regions" :key="r.selector" class="rg-dot"
                :class="{ active: currentSelector === r.selector, done: visited.has(r.selector) }"
                :title="r.name" />
        </div>
        <div class="rg-footer">
          <span class="rg-count">已查看 {{ visited.size }} / {{ regions.length }}</span>
          <button class="btn" @click="emit('close')">完成引导</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rg-mask { position: fixed; inset: 0; z-index: 3200; pointer-events: none; }
.rg-panel {
  position: fixed; width: 440px;
  padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;
  pointer-events: auto;
}
.rg-head { display: flex; align-items: center; justify-content: space-between; cursor: move; user-select: none; }
.rg-title { font-size: calc(14px * var(--font-scale)); font-weight: 600; }
.rg-close {
  width: 26px; height: 26px; border-radius: 6px; cursor: pointer; border: 0;
  background: rgba(var(--surface-rgb), 0.08); color: rgb(var(--text-rgb)); font-size: 13px;
}
.rg-close:hover { background: rgba(var(--surface-rgb), 0.16); }
.rg-hint { font-size: calc(11px * var(--font-scale)); opacity: .5; line-height: 1.5; }
.rg-body { height: 300px; overflow-y: auto; }
.rg-card {
  padding: 12px 14px; border-radius: 10px;
  background: rgba(var(--accent-rgb), 0.1); border: 1px solid rgba(var(--accent-rgb), 0.25);
  animation: rg-fade .25s ease;
}
@keyframes rg-fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.rg-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.rg-card-icon { font-size: calc(18px * var(--font-scale)); }
.rg-card-name { font-size: calc(14px * var(--font-scale)); font-weight: 600; color: var(--accent-ink); }
.rg-card-text { font-size: calc(12px * var(--font-scale)); line-height: 1.7; white-space: pre-line; opacity: .85; }
.rg-empty { text-align: center; padding: 24px 0; }
.rg-empty-icon { font-size: calc(32px * var(--font-scale)); margin-bottom: 8px; }
.rg-empty-text { font-size: calc(12px * var(--font-scale)); opacity: .5; line-height: 1.6; }
.rg-progress { display: flex; gap: 6px; justify-content: center; }
.rg-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: rgba(var(--surface-rgb), 0.2); transition: all .2s;
}
.rg-dot.done { background: rgba(var(--accent-rgb), 0.5); }
.rg-dot.active { background: var(--accent); transform: scale(1.3); }
.rg-footer { display: flex; align-items: center; justify-content: space-between; }
.rg-count { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.btn {
  padding: 6px 16px; border-radius: 8px; font-size: calc(12px * var(--font-scale)); cursor: pointer;
  border: 1px solid rgba(var(--accent-rgb), 0.3); background: rgba(var(--accent-rgb), 0.2); color: var(--accent-ink);
}
.btn:hover { background: rgba(var(--accent-rgb), 0.35); }
</style>