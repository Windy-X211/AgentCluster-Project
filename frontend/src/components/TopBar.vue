<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="topbar-content">
    <div class="brand">
      <div class="logo">⬡</div>
      <div>
        <div class="title">智能体集群</div>
        <div class="subtitle">AgentCluster</div>
      </div>
    </div>
    <div class="spacer" />
    <RouterLink to="/settings" class="nav-item">⚙ 设置</RouterLink>
  </div>
</template>

<style scoped>
.topbar-content { display: flex; align-items: center; height: 100%; padding: 0 20px; gap: 16px; }
.brand { display: flex; align-items: center; gap: 12px; }
.logo { width: 32px; height: 32px; border-radius: 10px; background: linear-gradient(135deg, #6ea8ff, #b472ff); display: flex; align-items: center; justify-content: center; font-size: 18px; box-shadow: 0 4px 12px rgba(110,168,255,0.3); }
.title { font-weight: 600; font-size: 15px; }
.subtitle { font-size: 10px; opacity: .6; letter-spacing: 1px; }
.spacer { flex: 1; }
.nav-item { color: #e8ecf4; text-decoration: none; font-size: 13px; padding: 6px 12px; border-radius: 8px; transition: background .2s; &:hover { background: rgba(255,255,255,0.06); } }
</style>
