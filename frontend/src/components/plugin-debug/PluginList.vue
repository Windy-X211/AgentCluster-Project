<script setup lang="ts">
import { usePluginDebugStore } from '@/stores'

const store = usePluginDebugStore()
</script>

<template>
  <div class="plugin-list">
    <div v-if="store.scanError" class="scan-error">⚠ {{ store.scanError }}</div>
    <div v-else-if="store.scanning" class="empty">正在扫描…</div>
    <div v-else-if="!store.plugins.length && !store.failed.length" class="empty">
      未在所选目录下发现可用插件
    </div>

    <ul class="list">
      <li
        v-for="p in store.plugins"
        :key="p.name"
        class="item"
        :class="{ active: p.name === store.selectedPluginName }"
        @click="store.selectPlugin(p.name)"
      >
        <div class="item-head">
          <span class="icon">{{ p.icon || '🔌' }}</span>
          <span class="label">{{ p.label }}</span>
          <span class="version" v-if="p.version">v{{ p.version }}</span>
        </div>
        <div class="desc" v-if="p.description">{{ p.description }}</div>
        <div class="meta">
          <span v-if="p.author" class="author">👤 {{ p.author }}</span>
          <span class="tag" v-for="t in p.tags" :key="t">{{ t }}</span>
        </div>
        <div class="name">name: {{ p.name }}</div>
      </li>
    </ul>

    <div v-if="store.failed.length" class="failed-block">
      <div class="failed-title">加载失败（{{ store.failed.length }}）</div>
      <div v-for="f in store.failed" :key="f.dir_name" class="failed-item">
        <span class="failed-name">📁 {{ f.dir_name }}</span>
        <span class="failed-reason">{{ f.load_error }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plugin-list { display: flex; flex-direction: column; min-height: 0; height: 100%; overflow-y: auto; }
.list { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.item {
  padding: 10px 12px; border-radius: 10px; cursor: pointer;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  transition: all .15s;
  &:hover { background: rgba(110, 168, 255, 0.08); border-color: rgba(110, 168, 255, 0.3); }
  &.active { background: rgba(110, 168, 255, 0.16); border-color: rgba(110, 168, 255, 0.55); }
}
.item-head { display: flex; align-items: center; gap: 8px; }
.icon { font-size: 16px; }
.label { font-size: 14px; font-weight: 600; }
.version { font-size: 11px; opacity: .55; margin-left: auto; }
.desc { font-size: 12px; opacity: .7; margin-top: 4px; line-height: 1.4;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; }
.author { font-size: 11px; opacity: .6; }
.name { font-size: 10px; opacity: .45; font-family: Consolas, monospace; margin-top: 4px; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: 13px; }
.scan-error {
  padding: 10px 12px; margin-bottom: 8px; border-radius: 8px; font-size: 12px;
  background: rgba(255, 96, 96, 0.14); border: 1px solid rgba(255, 96, 96, 0.35); color: #ffb4b4;
}
.failed-block { margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(255, 255, 255, 0.08); }
.failed-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: .55; margin-bottom: 6px; }
.failed-item {
  display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: 8px; margin-bottom: 6px;
  background: rgba(255, 160, 96, 0.08); border: 1px solid rgba(255, 160, 96, 0.22);
}
.failed-name { font-size: 12px; opacity: .8; }
.failed-reason { font-size: 11px; color: #ffcba0; }
</style>