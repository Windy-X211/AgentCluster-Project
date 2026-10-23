<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useAgentStore, useCanvasStore, useConvStore } from '@/stores'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const agents = useAgentStore()
const canvases = useCanvasStore()
const convs = useConvStore()

interface Step {
  selectors: string[]
  title: string
  icon: string
  text: string
  place: 'left' | 'right' | 'top' | 'bottom'
  doneWhen?: () => boolean
}

const baselineAgentCount = ref(0)

const steps = computed<Step[]>(() => [
  {
    selectors: ['.left'], title: '创建智能体', icon: '📋', place: 'right',
    text: '第一步：在智能体列表创建智能体（可边看边操作）\n· 顶部 +新建组 用来存放同一群智能体，或不分组直接点击进入在全部组\n· 组内 + 新建智能体，设置名称、描述、系统提示词（角色定位）、勾选能力，最后保存\n· 拖拽组或智能体改变顺序\n· 右键智能体 → 编辑/删除/归纳到分组\n（若已折叠，请先点击展开）',
    doneWhen: () => agents.list.length > baselineAgentCount.value,
  },
  {
    selectors: ['.left', '.center'], title: '拖拽创建节点', icon: '🎨', place: 'bottom',
    text: '第二步：拖拽智能体到画布\n· 从左侧列表拖入画布创建节点\n· 每个节点代表集群中的一个角色\n· 拖动节点可移动位置（带对齐辅助线）',
    doneWhen: () => (canvases.current?.nodes?.length || 0) > 0,
  },
  {
    selectors: ['.center'], title: '连线搭建集群', icon: '🔗', place: 'bottom',
    text: '第三步：连线搭建集群\n· 点击节点底部 🔗 连线按钮\n· 再点击接收消息的另一节点即可建立连线\n· 双击连线可删除\n· 连线表示智能体间的信息传递方向\n· 右键节点可配置触发方式，默认发送消息后触发，也可定时触发',
    doneWhen: () => (canvases.current?.edges?.length || 0) > 0,
  },
  {
    selectors: ['.center', '.right'], title: '运行与对话', icon: '▶️', place: 'bottom',
    text: '第四步：运行集群与对话\n· 在对话区新建会话，然后点击节点 💬 对话按钮选择独立/集群对话\n· 智能体开始输出后可以双击节点隐藏/显示消息泡泡，或者右键画布空白区一键显隐所有消息泡泡',
    doneWhen: () => convs.running === true,
  },
  {
    selectors: ['.right'], title: '发送消息', icon: '💬', place: 'left',
    text: '第五步：在对话区与智能体沟通\n· 输入消息发送\n· 回复完成后可以点击消息顶部的（<过程）查看智能体思考工作过程\n· 右键消息可复制/删除/撤回等\n· 拖入文件可快速发送文件路径',
    doneWhen: () => {
      const c = convs.currentData()
      return !!(c?.messages || []).some((m: any) => m.role === 'user')
    },
  },
  {
    selectors: ['.right'], title: '智能编排：集群编排师', icon: '🤖', place: 'left',
    text: '第六步：使用集群编排师快速搭建或调整集群\n· 点击右侧「＋ 独立」新建独立对话\n· 在对话框顶部的智能体下拉框中选择内置的集群编排师\n· 发送你的请求，集群编排师创建智能体并编排集群',
    doneWhen: () => convs.list.some((c: any) => c.mode === 'linked' && (c.messages?.length || 0) > 0),
  },
])

const idx = ref(0)
const currentStep = computed(() => steps.value[idx.value])
const isLast = computed(() => idx.value === steps.value.length - 1)
const isFirst = computed(() => idx.value === 0)

const currentDone = computed(() => {
  const fn = currentStep.value?.doneWhen
  return fn ? fn() : false
})

const holes = ref<Array<{ left: number, top: number, width: number, height: number }>>([])
const bubble = ref({ left: 0, top: 0 })
const actualPlace = ref<'left' | 'right' | 'top' | 'bottom' | 'inside'>('right')

const BUBBLE_W = 376
const BUBBLE_H = 300

function compute() {
  const step = currentStep.value
  const rects: DOMRect[] = []
  for (const sel of step.selectors) {
    const el = document.querySelector(sel) as HTMLElement | null
    if (!el) continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    rects.push(r)
  }
  if (rects.length === 0) { holes.value = []; return }
  const pad = 6
  holes.value = rects.map(r => ({
    left: r.left - pad, top: r.top - pad,
    width: r.width + pad * 2, height: r.height + pad * 2,
  }))

  const minX = Math.min(...rects.map(r => r.left))
  const minY = Math.min(...rects.map(r => r.top))
  const maxX = Math.max(...rects.map(r => r.right))
  const maxY = Math.max(...rects.map(r => r.bottom))
  const box = { left: minX, top: minY, right: maxX, bottom: maxY, width: maxX - minX, height: maxY - minY }

  const gap = 24
  const vw = window.innerWidth, vh = window.innerHeight
  const allPlaces: ('left' | 'right' | 'top' | 'bottom')[] = ['right', 'bottom', 'left', 'top']
  const order = [step.place, ...allPlaces.filter(p => p !== step.place)]

  for (const p of order) {
    let left = 0, top = 0
    if (p === 'right') { left = box.right + gap; top = box.top + box.height / 2 - BUBBLE_H / 2 }
    else if (p === 'left') { left = box.left - gap - BUBBLE_W; top = box.top + box.height / 2 - BUBBLE_H / 2 }
    else if (p === 'bottom') { left = box.left + box.width / 2 - BUBBLE_W / 2; top = box.bottom + gap }
    else { left = box.left + box.width / 2 - BUBBLE_W / 2; top = box.top - gap - BUBBLE_H }
    if (left >= 8 && left + BUBBLE_W <= vw - 8 && top >= 8 && top + BUBBLE_H <= vh - 8) {
      actualPlace.value = p
      bubble.value = { left, top }
      return
    }
  }

  actualPlace.value = 'inside'
  let left = box.left + 20, top = box.top + 20
  left = Math.max(8, Math.min(left, vw - BUBBLE_W - 8))
  top = Math.max(8, Math.min(top, vh - BUBBLE_H - 8))
  bubble.value = { left, top }
}

const bubbleStyle = computed(() => ({
  left: bubble.value.left + 'px',
  top: bubble.value.top + 'px',
}))

const arrowClass = computed(() => {
  const p = actualPlace.value
  if (p === 'inside') return 'no-arrow'
  return p === 'right' ? 'arrow-left' : p === 'left' ? 'arrow-right' : p === 'bottom' ? 'arrow-top' : 'arrow-bottom'
})

const maskPath = computed(() => {
  if (holes.value.length === 0) return ''
  const vw = window.innerWidth, vh = window.innerHeight
  let d = `M0,0 H${vw} V${vh} H0 Z`
  for (const h of holes.value) {
    const l = h.left, t = h.top, r = h.left + h.width, b = h.top + h.height
    d += ` M${l},${t} H${r} V${b} H${l} Z`
  }
  return d
})

const nextLabel = computed(() => {
  if (isLast.value) return '完成'
  return '下一步'
})

let timer: number | null = null

onMounted(() => {
  baselineAgentCount.value = agents.list.length
  compute()
  timer = window.setInterval(compute, 200)
  window.addEventListener('resize', compute)
})
onBeforeUnmount(() => {
  if (timer) { clearInterval(timer); timer = null }

  window.removeEventListener('resize', compute)
})
watch(idx, () => { compute() })

function prev() { if (idx.value > 0) idx.value-- }
function next() { if (isLast.value) emit('close'); else idx.value++ }
function skip() { emit('close') }
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fg-root">
      <svg v-if="holes.length" class="fg-mask">
        <path :d="maskPath" fill-rule="evenodd" />
      </svg>
      <div v-for="(h, i) in holes" :key="i" class="fg-hole"
           :style="{ left: h.left + 'px', top: h.top + 'px', width: h.width + 'px', height: h.height + 'px' }" />
      <div class="fg-bubble popup-surface" :class="[arrowClass, { done: currentDone }]" :style="bubbleStyle">
        <div class="fg-top-row">
          <div class="fg-step">{{ idx + 1 }} / {{ steps.length }}</div>
          <div v-if="currentDone" class="fg-done-badge">✓ 已完成</div>
        </div>
        <div class="fg-head">
          <span class="fg-icon">{{ currentStep.icon }}</span>
          <span class="fg-title">{{ currentStep.title }}</span>
        </div>
        <div class="fg-text">{{ currentStep.text }}</div>

        <div class="fg-actions">
          <button class="btn ghost" @click="skip">跳过</button>
          <button class="btn ghost" @click="prev" :disabled="isFirst">上一步</button>
          <button class="btn primary" @click="next">{{ nextLabel }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.fg-mask {
  position: fixed; inset: 0; z-index: 3201; pointer-events: none;
  width: 100%; height: 100%;
}
.fg-mask path { fill: rgba(var(--shadow-rgb), 0.45); }
.fg-hole {
  position: fixed; border-radius: 10px; z-index: 3202; pointer-events: none;
  border: 2px solid rgba(var(--accent-rgb), 0.6);
  transition: all .3s ease;
}
.fg-bubble {
  position: fixed; z-index: 3203; width: 340px; padding: 16px 18px;
  position: fixed; z-index: 3202; width: 340px; padding: 16px 18px;
  display: flex; flex-direction: column; gap: 10px;
  transition: top .3s ease, left .3s ease;
  pointer-events: auto;
}
.fg-bubble.done { border: 1px solid rgba(var(--accent-rgb), 0.5); }
.fg-bubble.arrow-left::before {
  content: ''; position: absolute; left: -10px; top: 50%; transform: translateY(-50%);
  border: 10px solid transparent; border-right-color: rgba(var(--node-bg-rgb), 0.98);
}
.fg-bubble.arrow-right::before {
  content: ''; position: absolute; right: -10px; top: 50%; transform: translateY(-50%);
  border: 10px solid transparent; border-left-color: rgba(var(--node-bg-rgb), 0.98);
}
.fg-bubble.arrow-top::before {
  content: ''; position: absolute; top: -10px; left: 50%; transform: translateX(-50%);
  border: 10px solid transparent; border-bottom-color: rgba(var(--node-bg-rgb), 0.98);
}
.fg-bubble.arrow-bottom::before {
  content: ''; position: absolute; bottom: -10px; left: 50%; transform: translateX(-50%);
  border: 10px solid transparent; border-top-color: rgba(var(--node-bg-rgb), 0.98);
}
.fg-top-row { display: flex; align-items: center; justify-content: space-between; }
.fg-step {
  font-size: calc(11px * var(--font-scale)); opacity: .5;
  font-family: Consolas, monospace; padding: 2px 8px; border-radius: 4px;
  background: rgba(var(--surface-rgb), 0.08);
}
.fg-done-badge {
  font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 4px;
  background: rgba(var(--accent-rgb), 0.22); color: var(--accent-ink);
}
.fg-head { display: flex; align-items: center; gap: 8px; }
.fg-icon { font-size: calc(20px * var(--font-scale)); }
.fg-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; }
.fg-text { font-size: calc(12.5px * var(--font-scale)); line-height: 1.7; white-space: pre-line; opacity: .85; }
.fg-hint { font-size: calc(11px * var(--font-scale)); opacity: .55; line-height: 1.5; }
.fg-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
.btn {
  padding: 6px 16px; border-radius: 8px; font-size: calc(12px * var(--font-scale)); cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.12); background: rgba(var(--surface-rgb), 0.06); color: rgb(var(--text-rgb));
}
.btn.ghost { background: transparent; }
.btn.ghost:hover:not(:disabled) { background: rgba(var(--surface-rgb), 0.1); }
.btn.ghost:disabled { opacity: .35; cursor: not-allowed; }
.btn.primary {
  background: rgba(var(--accent-rgb), 0.25); color: var(--accent-ink);
  border-color: rgba(var(--accent-rgb), 0.4);
}
.btn.primary:hover { background: rgba(var(--accent-rgb), 0.4); }
</style>