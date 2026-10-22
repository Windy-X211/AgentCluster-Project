<script setup lang="ts">
defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'start-flow'): void; (e: 'start-region'): void; (e: 'skip'): void }>()
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="gw-overlay" @click.self="emit('skip')">
      <div class="gw-dialog popup-surface">
        <div class="gw-head">
          <div class="gw-icon">👋</div>
          <div class="gw-title">欢迎使用智能体集群</div>
          <div class="gw-sub">首次使用？选择一种引导方式快速上手</div>
        </div>
        <div class="gw-cards">
          <div class="gw-card" @click="emit('start-flow')">
            <div class="gc-icon">🗺️</div>
            <div class="gc-name">使用流程引导</div>
            <div class="gc-desc">按操作流程一步步提示，箭头指引每个区域的功能与用法，适合首次了解整体流程</div>
            <div class="gc-btn">开始引导</div>
          </div>
          <div class="gw-card" @click="emit('start-region')">
            <div class="gc-icon">🖱️</div>
            <div class="gc-name">区域功能引导</div>
            <div class="gc-desc">将鼠标移至任意区域，自动显示该区域的功能说明，适合自由探索各模块</div>
            <div class="gc-btn">开始引导</div>
          </div>
        </div>
        <div class="gw-footer">
          <button class="btn ghost" @click="emit('skip')">跳过，我自己探索</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.gw-overlay {
  position: fixed; inset: 0; z-index: 3300;
  background: rgba(var(--shadow-rgb), 0.55); backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.gw-dialog {
  width: min(640px, 100%); padding: 28px 28px 20px; border-radius: 16px;
  display: flex; flex-direction: column; gap: 20px;
}
.gw-head { text-align: center; display: flex; flex-direction: column; align-items: center; gap: 6px; }
.gw-icon { font-size: calc(42px * var(--font-scale)); }
.gw-title { font-size: calc(20px * var(--font-scale)); font-weight: 600; }
.gw-sub { font-size: calc(13px * var(--font-scale)); opacity: .6; }
.gw-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.gw-card {
  padding: 18px 16px; border-radius: 12px; cursor: pointer;
  background: rgba(var(--surface-rgb), 0.06); border: 1px solid rgba(var(--surface-rgb), 0.1);
  display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  transition: all .2s;
}
.gw-card:hover {
  background: rgba(var(--accent-rgb), 0.12); border-color: rgba(var(--accent-rgb), 0.4);
  transform: translateY(-2px);
}
.gc-icon { font-size: calc(30px * var(--font-scale)); }
.gc-name { font-size: calc(15px * var(--font-scale)); font-weight: 600; }
.gc-desc { font-size: calc(12px * var(--font-scale)); opacity: .65; line-height: 1.6; min-height: calc(48px * var(--font-scale)); }
.gc-btn {
  margin-top: 4px; padding: 6px 18px; border-radius: 8px; font-size: calc(12px * var(--font-scale));
  background: rgba(var(--accent-rgb), 0.2); color: var(--accent-ink); border: 1px solid rgba(var(--accent-rgb), 0.3);
}
.gw-card:hover .gc-btn { background: rgba(var(--accent-rgb), 0.35); }
.gw-footer { display: flex; justify-content: center; }
.btn { padding: 7px 16px; border-radius: 8px; font-size: calc(13px * var(--font-scale)); cursor: pointer; border: 1px solid rgba(var(--surface-rgb), 0.12); background: rgba(var(--surface-rgb), 0.06); color: rgb(var(--text-rgb)); }
.btn.ghost { background: transparent; }
.btn:hover { background: rgba(var(--surface-rgb), 0.1); }
</style>