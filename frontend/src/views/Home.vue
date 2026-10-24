<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
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
const leftCollapsed = ref(true)

// 4 列布局：left | center | handle(6px) | right；中间是 flex 的 1fr
const gridStyle = computed(() => ({
  gridTemplateColumns: `${leftCollapsed.value ? 36 : 240}px minmax(0, 1fr) 6px ${layout.rightWidth}px`,
}))

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
  await Promise.all([agents.fetch(), canvases.fetch(), models.fetch(), settings.fetch(), convs.fetch()])
  if (!convs.current && convs.list.length) convs.current = convs.list[0].id
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
  <div class="app-root" :class="{ 'left-collapsed': leftCollapsed }" :style="gridStyle" @dblclick="onRootDblClick">
    <TopBar class="topbar glass" />
    <AgentList class="left glass" :collapsed="leftCollapsed" @toggle="leftCollapsed = !leftCollapsed" />
    <MainView class="center" />
    <div class="resize-handle" @mousedown="onHandleDown" title="拖动调整右侧面板宽度（双击复位）"></div>
    <RightPanel class="right" />
  </div>

</template>
