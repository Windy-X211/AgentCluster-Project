<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import DefaultCapabilitiesPanel from '@/components/capability-mgmt/DefaultCapabilitiesPanel.vue'
import ParamPresetsPanel from '@/components/capability-mgmt/ParamPresetsPanel.vue'
import PluginDebugPanel from '@/components/plugin-debug/PluginDebugPanel.vue'
import DependencyInstallPanel from '@/components/capability-mgmt/DependencyInstallPanel.vue'

type TabKey = 'defaults' | 'presets' | 'debug' | 'deps'
const tabs: { key: TabKey; label: string; icon: string; desc: string }[] = [
  { key: 'defaults', label: '默认能力',   icon: '⚡', desc: '新建智能体时自动勾选的命令 / 技能 / 插件' },
  { key: 'presets',  label: '能力参数预设', icon: '🔐', desc: '为插件/技能预填固定参数和可选参数描述' },
  { key: 'debug',    label: '插件调试',   icon: '🧪', desc: '选择插件目录 → 加载 → 填参数 → 运行调试' },
  { key: 'deps',     label: '依赖安装',   icon: '📦', desc: '查看插件/技能的 pip/npm/external 依赖状态与下载链接' },
]
const activeTab = ref<TabKey>('defaults')
const activeTabDesc = () => tabs.find(t => t.key === activeTab.value)?.desc || ''
</script>

<template>
  <div class="cm-root region-plugins">
    <div class="top glass">
      <!-- 返回按钮独立，不拖动 -->
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <!-- 标题 + 空白拖动区 -->
      <div class="top-drag pywebview-drag-region">
        <h2>🛠 能力管理</h2>
        <span class="sub">{{ activeTabDesc() }}</span>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs glass">
      <button v-for="t in tabs" :key="t.key" class="tab-btn"
              :class="{ active: activeTab === t.key }"
              @click="activeTab = t.key">
        <span class="t-icon">{{ t.icon }}</span>
        <span class="t-label">{{ t.label }}</span>
      </button>
    </div>

    <!-- 面板内容 -->
    <div class="body">
      <DefaultCapabilitiesPanel v-if="activeTab === 'defaults'" />
      <ParamPresetsPanel        v-else-if="activeTab === 'presets'" />
      <PluginDebugPanel         v-else-if="activeTab === 'debug'" class="debug-panel" />
      <DependencyInstallPanel   v-else-if="activeTab === 'deps'" />
    </div>
  </div>
</template>

<style scoped>
.cm-root {
  padding: 16px; height: calc(100vh - var(--titlebar-h));
  display: flex; flex-direction: column; min-height: 0; overflow: hidden;
}
.top {
  padding: 12px 20px; margin-bottom: 12px;
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
  h2 { font-size: calc(18px * var(--font-scale)); }
}
.top-drag { flex: 1; align-self: stretch; display: flex; align-items: center; gap: 12px; user-select: none; }
.sub { font-size: calc(12px * var(--font-scale)); opacity: .5; }

/* —— tabs —— */
.tabs {
  display: flex; gap: 4px; padding: 6px; margin-bottom: 12px;
  border-radius: 10px; flex-shrink: 0;
  overflow-x: auto;
}
.tab-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px; border-radius: 7px; border: none; background: transparent;
  font-size: calc(13px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7);
  cursor: pointer; transition: all .15s; white-space: nowrap;
  border: 1px solid transparent;
  &:hover { background: rgba(var(--surface-rgb), 0.06); color: rgb(var(--text-rgb)); }
  &.active {
    background: rgba(var(--accent-rgb), 0.14);
    border-color: rgba(var(--accent-rgb), 0.3);
    color: var(--accent-light);
    box-shadow: 0 2px 8px rgba(var(--accent-rgb), 0.18);
  }
}
.t-icon { font-size: calc(14px * var(--font-scale)); }

.body { flex: 1; min-height: 0; overflow: auto; }

/* 插件调试面板撑满 */
.debug-panel { height: 100%; }
</style>
