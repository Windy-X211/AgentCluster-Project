<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useAgentStore, useCanvasStore, useModelStore, useSettingsStore, useConvStore, useLayoutStore } from '@/stores'
import TopBar from '@/components/TopBar.vue'
import AgentList from '@/components/AgentList.vue'
import MainView from '@/components/MainView.vue'
import RightPanel from '@/components/RightPanel.vue'

defineOptions({ name: 'Home' })


const agents = useAgentStore()
const canvases = useCanvasStore()
const models = useModelStore()
const settings = useSettingsStore()
const convs = useConvStore()
const layout = useLayoutStore()

// 4 列布局：left | center | handle(6px) | right；中间是 flex 的 1fr
// 画布关闭（current 为 null）时收敛为对话区布局，展开对话记录时为浮层留位：
//   - 会话记录折叠：2 列，对话区弹性占满原画布区
//   - 会话记录展开：3 列（left | 250px 浮层留位 | 对话区 1fr），浮层紧贴对话区左缘，
//     与画布打开时的浮层覆盖逻辑一致（250 = 浮层 240 + 10 间隙，RightPanel convFloatStyle 同步推算）
// 打开画布自动恢复原 4 列 + rightWidth
const gridStyle = computed(() => {
  const left = `${layout.agentsCollapsed ? 36 : 240}px`
  if (!canvases.current) {
    if (!layout.convsCollapsed) {
      return {
        gridTemplateColumns: `${left} 250px minmax(0, 1fr)`,
        gridTemplateAreas: '"topbar topbar topbar" "left convgutter right"',
      }
    }
    return {
      gridTemplateColumns: `${left} minmax(0, 1fr)`,
      gridTemplateAreas: '"topbar topbar" "left right"',
    }
  }
  return {
    gridTemplateColumns: `${left} minmax(0, 1fr) 6px ${layout.rightWidth}px`,
    gridTemplateAreas: '"topbar topbar topbar topbar" "left center handle right"',
  }
})

// —— 拖动调节右侧对话面板宽度 ——
function onHandleDown(e: MouseEvent) {
  e.preventDefault()
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
function onMove(e: MouseEvent) {
  // 右侧面板宽度 = 视口宽度 - 内边距(2×10) - 鼠标X
  const width = window.innerWidth - e.clientX - 20
  layout.setRightWidth(width)
}
function onUp() {
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  document.removeEventListener('mousemove', onMove)
  document.removeEventListener('mouseup', onUp)
}
onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onMove)
  document.removeEventListener('mouseup', onUp)
})

function onRootDblClick(e: MouseEvent) {
  const t = e.target as HTMLElement | null
  if (!t || !t.classList.contains('resize-handle')) return
  layout.resetRightWidth()
}

onMounted(async () => {
  try {
    await Promise.all([agents.fetch(), canvases.fetch(), models.fetch(), settings.fetch(), convs.fetch()])
  } catch (e) {
    console.error('[Home] 初始化数据加载失败:', e)
  }
  // 恢复上次选中的会话（localStorage 记忆），无记忆才回落最新活跃会话
  convs.ensureCurrent()
  // 接管后端仍在运行的会话（关窗重开 / 浏览器打开都能看到运行状态）
  void convs.restoreRunning()
  // 通知 Electron 主进程：Vue 数据已就绪，可以显示主窗口了（防白屏闪烁）
  // 浏览器模式下 window.electronAPI 不存在，?. 安全调用直接跳过
  window.electronAPI?.notifyReady?.()
})

// 离开首页前往设置/能力管理等页面前，保存右侧对话区与会话列表的滚动位置。
// 此守卫在 DOM 仍完整时触发，scrollTop 准确可靠（优于 onDeactivated，后者触发时 DOM
// 已被 KeepAlive 移出文档，scrollTop 会被浏览器重置为 0）。
onBeforeRouteLeave(() => {
  const chatBox = document.querySelector('.chat-box') as HTMLElement | null
  if (chatBox) layout.chatMsgScrollTop = chatBox.scrollTop
  const convList = document.querySelector('.conv-list-wrap') as HTMLElement | null
  if (convList) layout.convListScrollTop = convList.scrollTop
})
</script>

<template>
  <div class="app-root" :class="{ 'left-collapsed': layout.agentsCollapsed }" :style="gridStyle" @dblclick="onRootDblClick">
    <TopBar class="topbar glass" />
    <AgentList class="left glass" :collapsed="layout.agentsCollapsed" @toggle="layout.agentsCollapsed = !layout.agentsCollapsed" />
    <MainView v-if="canvases.current" class="center" />
    <div v-if="canvases.current" class="resize-handle" @mousedown="onHandleDown" title="拖动调整右侧面板宽度（双击复位）"></div>
    <RightPanel class="right" />
  </div>

</template>
