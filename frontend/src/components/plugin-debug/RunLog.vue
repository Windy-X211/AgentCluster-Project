<script setup lang="ts">
import { usePluginDebugStore } from '@/stores'

const store = usePluginDebugStore()
</script>

<template>
  <div class="run-log">
    <div class="log-head">
      <span class="log-title">运行日志</span>
      <button class="btn ghost sm" v-if="store.logs.length" @click="store.clearLogs()">清空日志</button>
    </div>
    <div v-if="!store.logs.length" class="empty">暂无运行记录</div>
    <div v-else class="log-list">
      <div v-for="(l, i) in store.logs" :key="i" class="log-item" :class="l.status">
        <span class="status">{{ l.status === 'ok' ? '✓' : '✕' }}</span>
        <span class="time">{{ l.timestamp }}</span>
        <span class="plugin">{{ l.plugin_name }}</span>
        <span class="ms">{{ l.duration_ms }}ms</span>
        <span class="summary" :title="l.params_summary">{{ l.params_summary }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-log { display: flex; flex-direction: column; min-height: 0; }
.log-head { display: flex; align-items: center; margin-bottom: 8px; }
.log-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: .55; }
.sm { padding: 2px 10px; font-size: 11px; margin-left: auto; }
.log-list { display: flex; flex-direction: column; gap: 4px; max-height: 200px; overflow-y: auto; }
.log-item {
  display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; font-size: 11px;
  background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.06);
  font-family: Consolas, monospace;
}
.log-item.ok .status { color: #80e4b8; }
.log-item.err .status { color: #ffb0b0; }
.status { font-weight: bold; }
.time { opacity: .55; }
.plugin { font-weight: 600; }
.ms { opacity: .55; }
.summary { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: .6; }
.empty { padding: 16px; text-align: center; opacity: .5; font-size: 12px; }
</style>