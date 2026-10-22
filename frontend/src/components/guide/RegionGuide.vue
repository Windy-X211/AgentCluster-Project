<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

interface Region { selector: string; name: string; icon: string; text: string }

const regions: Region[] = [
  {
    selector: '.region-settings', name: '设置中心', icon: '⚙️',
    text: '点击进入设置中心。\n· 模型接口：配置 API 地址与密钥\n· 默认能力：命令 / 技能 / 插件\n· 能力参数预设与加密保存\n· 外观设置：主题 / 字号 / 头像\n· 数据存储：路径管理/切换\n· 新手引导：再次查看说明',
  },
  {
    selector: '.region-plugins', name: '插件调试', icon: '🔌',
    text: '点击进入插件调试面板。\n· 选择插件目录 → 加载插件\n· 填写参数 → 运行插件\n· 查看执行结果与运行日志\n· 支持枚举/布尔/数字/密码/JSON 参数\n· 独立调试插件能力，不影响集群',
  },
  {
    selector: '.canvases-pane', name: '画布列表', icon: '🗂️',
    text: '管理所有画布（集群编排）。\n· 点击切换当前画布\n· + 按钮新建画布\n· 双击或右键可以重命名\n· 右键可删除画布\n· 拖动画布可改变顺序\n· 显示节点数与运行状态\n· 可折叠 / 展开',
  },
  {
    selector: '.left', name: '智能体列表区', icon: '📋',
    text: '管理所有智能体的入口。\n· 按组进行分类管理\n· + 新建组 / 组内 + 新建智能体\n· 拖拽组或智能体可以改变顺序\n· 右键组：重命名 / 删除 / 新建智能体\n· 右键智能体：编辑 / 删除 / 归纳到分组\n· 拖拽智能体到画布创建节点\n· 顶部按钮折叠 / 展开列表',
  },
  {
    selector: '.center', name: '画布区', icon: '🎨',
    text: '集群编排的核心画布。\n· 拖拽智能体到画布创建节点\n· 点击节点 🔗 连线按钮拖到另一节点连线\n· 双击连线删除\n· 点击节点 💬 对话按钮选择独立/集群对话\n· 双击节点隐藏/显示消息泡泡\n· 右键节点：编辑/感知范围/触发/启停/移除\n· 右键空白：重置视图/工作区/消息泡泡显隐\n· 工具栏：运行停止/串并行/工作区/规则/缩放保存\n· 🗂 工作区：管理画布文件，预览/编辑/运行/回收站\n· 滚轮缩放，空格+拖动平移',
  },
  {
    selector: '.right', name: '对话区', icon: '💬',
    text: '与智能体实时对话的窗口。\n· 顶部切换或新建会话（独立/集群）\n· 底部输入框发送消息，可拖入文件\n· 查看流式回复\n· 展开过程可显示智能体思考工作过程\n· 右键用户头像可进行头像更换\n· 右键用户消息可复制/删除/撤回/还原文件\n· 右键智能体消息可复制/删除\n· 支持图片、文件、表格展示',
  },
  {
    selector: '.topbar', name: '顶部栏', icon: '🔧',
    text: '全局导航与入口。\n· 🔌 插件调试：独立调试插件能力\n· ⚙ 设置：配置模型/外观/能力/存储\n· 设置中心可再次开启新手引导\n· 支持四套配色方案与字体缩放',
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
            <div class="rg-empty-text">移动鼠标到设置、画布列表、智能体列表、画布区、对话区或顶部栏</div>
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