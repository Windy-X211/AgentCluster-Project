<script setup lang="ts">
import { RouterLink } from 'vue-router'

</script>

<template>
  <div class="topbar-content">
    <div class="brand">
      <img class="logo" src="/icon.png" alt="AgentCluster" />
      <div>
        <div class="title">智能体集群</div>
        <div class="subtitle">AgentCluster</div>
      </div>
    </div>
    <div class="spacer" />
    <RouterLink to="/plugins" class="nav-item region-plugins">🔌 插件调试</RouterLink>
    <RouterLink to="/settings" class="nav-item region-settings">⚙ 设置</RouterLink>

  </div>
</template>

<style scoped>
.topbar-content { display: flex; align-items: center; height: 100%; padding: 0 20px; gap: 16px; }
.brand { display: flex; align-items: center; gap: 12px; }
.logo { width: 32px; height: 32px; border-radius: 10px; object-fit: cover; box-shadow: 0 4px 12px rgba(var(--accent-rgb), 0.3); }
.title { font-weight: 600; font-size: calc(15px * var(--font-scale)); }
.subtitle { font-size: calc(10px * var(--font-scale)); opacity: .6; letter-spacing: 1px; }
.spacer { flex: 1; }
.nav-item { color: rgb(var(--text-rgb)); text-decoration: none; font-size: calc(13px * var(--font-scale)); padding: 6px 12px; border-radius: 8px; transition: background .2s; &:hover { background: rgba(var(--surface-rgb), 0.06); } }
</style>
