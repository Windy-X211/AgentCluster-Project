<script setup lang="ts">
/**
 * VizBlock —— 通用可视化容器
 * 根据 type 从渲染器注册表查找对应组件并渲染；未注册时回退显示原始 JSON。
 *
 * 注册表 _renderers 是 Vue reactive 对象，所以 computed 能正确追踪
 * 拓展异步注册完成后的变更并自动重渲染为真正的组件。
 */
import { computed } from 'vue'
import { getVizRenderer } from './registry'

const props = defineProps<{
  type: string
  data?: any
  raw?: string
  extAPI?: any
}>()

const renderer = computed(() => getVizRenderer(props.type))
</script>

<template>
  <component
    v-if="renderer"
    :is="renderer"
    :data="data || {}"
    :raw="raw || ''"
    :extAPI="extAPI"
  />
  <div v-else class="viz-slot-fallback">
    <div class="vsf-head">⚠️ 未注册的可视化类型: <code>{{ type }}</code></div>
    <pre class="vsf-raw">{{ raw || JSON.stringify(data, null, 2) }}</pre>
  </div>
</template>

<style scoped>
.viz-slot-fallback {
  padding: 8px 12px;
  background: rgba(255, 180, 80, 0.12);
  border: 1px dashed rgba(255, 180, 80, 0.5);
  border-radius: 6px;
  font-size: 12px;
}
.vsf-head { margin-bottom: 4px; }
.vsf-head code { background: rgba(255,255,255,.1); padding: 1px 4px; border-radius: 3px; }
.vsf-raw { margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 11px; opacity: .8; }
</style>
