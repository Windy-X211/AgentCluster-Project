<script setup lang="ts">
import { RouterLink } from 'vue-router'
import PluginDebugPanel from '@/components/plugin-debug/PluginDebugPanel.vue'
</script>

<template>
  <div class="pd-root region-plugins">
    <div class="top glass">
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <h2>插件调试面板</h2>
      <span class="sub">选择插件目录 → 加载插件 → 填写参数 → 运行</span>
    </div>
    <PluginDebugPanel class="body" />
  </div>
</template>

<style scoped>
.pd-root { padding: 16px; height: 100vh; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.top {
  padding: 12px 20px; margin-bottom: 12px; display: flex; align-items: center; gap: 16px; flex-shrink: 0;
  h2 { font-size: calc(18px * var(--font-scale)); }
}
.sub { font-size: calc(12px * var(--font-scale)); opacity: .5; }
.body { flex: 1; min-height: 0; overflow: hidden; }
</style>