<script setup lang="ts">
/**
 * ChatBody —— 消息正文渲染容器
 *
 * 把消息内容按 viz 协议切成「Markdown 段」+「viz 段」：
 *   - Markdown 段：v-html 渲染（纯文本，无交互组件）
 *   - viz 段：交给 VizBlock 以真正的 Vue 组件渲染
 *
 * 关键点：viz 组件是 Vue 组件树的一部分，流式追加 / 列表更新时由 Vue 正常 patch，
 * 不会像「v-html + 手动 render() 挂载」那样整体重设 innerHTML，从而避免
 * 输入框被销毁重建、无法获得焦点的老问题。
 */
import { computed } from 'vue'
import VizBlock from './VizBlock.vue'
import { parseVizSegments } from './viz-parse'
import { renderMarkdownSegment } from '@/utils/markdown'

const props = defineProps<{
  content?: string
  streaming?: boolean
  extAPI?: any
}>()

const segs = computed(() => parseVizSegments(props.content || ''))

function mdHtml(text?: string): string {
  return renderMarkdownSegment(text || '')
}
</script>

<template>
  <template v-for="(s, i) in segs" :key="i">
    <div v-if="s.kind === 'md'" class="cb-md" v-html="mdHtml(s.text)"></div>
    <VizBlock v-else :type="s.type || ''" :data="s.data" :raw="s.raw" :extAPI="extAPI" />
  </template>
  <span v-if="streaming" class="stream-cursor">▌</span>
</template>
