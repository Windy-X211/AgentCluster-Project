<script setup lang="ts">
/**
 * 临时对话小窗视图（精灵悬浮球拓展 · 快速问答）
 *
 * 设计原则：**不重复造渲染轮子** —— 直接复用主窗口 RightPanel 的全部能力
 * （markdown / viz 思维导图 / 思考折叠 / 附件卡片 / 工具过程框 / 流式输出），
 * 本视图只做精简壳：自绘标题栏 + 隐藏画布列，套一层 KeepAlive 让
 * RightPanel 的 onActivated/onDeactivated 全局监听（点击外部关弹层、灯箱 ESC 等）正常工作。
 *
 * 会话管理：
 * - 打开即创建独立「⚡ 临时对话」会话（直接走 api，不经 convs.create ——
 *   后者会顺带清理其他空会话，跨窗口时会误删主窗正在看的空会话）
 * - 关窗时未发送任何消息（0 消息）→ beforeunload keepalive 通知后端 cleanup 删除；
 *   发过消息的保留，可在主窗口会话列表回看
 * - 截图提问（小窗去向）：主进程发 IPC quick:shot，由 RightPanel 预填附件（不新建会话）
 */
import { onMounted, ref } from 'vue'
import RightPanel from '@/components/RightPanel.vue'
import { useConvStore, useAgentStore, useCanvasStore, useModelStore, useSettingsStore } from '@/stores'
import { api } from '@/api'
import { API_BASE } from '@/utils/env'

const convs = useConvStore()
const agents = useAgentStore()
const canvases = useCanvasStore()
const models = useModelStore()
const settings = useSettingsStore()
const ready = ref(false)

declare global {
  interface Window { __qcConvId?: number | null }
}

onMounted(async () => {
  try {
    // 与 Home.vue 挂载初始化同一份数据清单（小窗是独立窗口/独立 Vue 实例，
    // 主窗拉过的 store 数据小窗没有，必须自己拉）：
    // - models：智能体下拉按「模型接口」分组，缺失会显示「暂无模型接口」
    // - settings：extAPI.getSettings / 推理等级检测 / 提示音等依赖
    // - canvases：会话列表按画布分组、runningCanvasId 等展示逻辑
    await Promise.all([
      agents.fetch(),
      models.fetch(),
      settings.fetch(),
      canvases.fetch(),
      convs.fetch(),
    ])
    // 直接 api 建临时会话并设为 current（RightPanel 的 ensureCurrent 会保持它）
    const c = await api.convs.create({ name: '⚡ 临时对话', mode: 'independent', agent_ids: [] })
    convs.list = [c, ...convs.list]
    convs.current = c.id
    window.__qcConvId = c.id
  } catch (e) {
    console.error('[quickchat] 临时会话创建失败:', e)
  } finally {
    ready.value = true
  }
})

function closeSelf() {
  try { window.electronAPI?.fairy?.closeWin?.() } catch { /* ignore */ }
  window.close()
}

// 关窗清理：0 消息空会话删除（keepalive 保证窗口卸载期间请求仍发出）
window.addEventListener('beforeunload', () => {
  const cid = window.__qcConvId
  if (cid == null) return
  try {
    fetch(`${API_BASE}/api/ext/fairy_ball/quickchat/cleanup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv_id: cid }),
      keepalive: true,
    }).catch(() => { /* ignore */ })
  } catch { /* ignore */ }
})
</script>

<template>
  <div class="quickchat-view">
    <div class="qc-titlebar">
      <span class="qc-logo">🧚</span>
      <span class="qc-title">临时对话 · 智能体集群</span>
      <button class="qc-close" title="关闭" @click="closeSelf">✕</button>
    </div>
    <div class="qc-body">
      <KeepAlive>
        <RightPanel v-if="ready" class="qc-panel" />
      </KeepAlive>
      <div v-if="!ready" class="qc-loading">正在准备临时会话…</div>
    </div>
  </div>
</template>

<style scoped>
.quickchat-view {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #0a0e1a;
  overflow: hidden;
}
.qc-titlebar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 36px;
  padding: 0 8px 0 12px;
  background: rgba(15, 22, 38, .92);
  border-bottom: 1px solid rgba(120, 150, 220, .18);
  -webkit-app-region: drag;
}
.qc-logo { font-size: 14px; }
.qc-title { font-size: 12.5px; color: #8b9bc0; letter-spacing: .5px; }
.qc-close {
  margin-left: auto;
  -webkit-app-region: no-drag;
  width: 26px; height: 26px;
  border: none; border-radius: 7px;
  background: transparent; color: #8b9bc0;
  font-size: 14px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.qc-close:hover { background: rgba(255, 90, 90, .18); color: #ff9a9a; }
.qc-body { flex: 1; min-height: 0; display: flex; position: relative; }
.qc-panel { flex: 1; min-width: 0; }
.qc-loading {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: #8b9bc0; font-size: 13px;
}
/* 精简：隐藏画布列表与分隔线（只保留对话区，渲染能力与主窗完全一致） */
.qc-body :deep(.canvases-pane) { display: none !important; }
.qc-body :deep(.divider) { display: none !important; }
</style>