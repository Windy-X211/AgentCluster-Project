<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

interface Step { selector: string; title: string; icon: string; text: string; place: 'left' | 'right' | 'top' | 'bottom' }

const steps: Step[] = [
  {
    selector: '.left', title: '创建智能体', icon: '📋', place: 'right',
    text: '第一步：在智能体列表创建智能体\n· 右键空白处 → 新建组 进行分类\n· 组内 + 新建智能体\n· 拖拽组或智能体改变顺序\n· 右键智能体 → 编辑/删除/归纳到分组\n（若已折叠，请先点击展开）',
  },
  {
    selector: '.center', title: '拖拽创建节点', icon: '🎨', place: 'bottom',
    text: '第二步：拖拽智能体到画布\n· 从左侧列表拖入画布创建节点\n· 每个节点代表集群中的一个角色\n· 拖动节点可移动位置（带对齐辅助线）',
  },
  {
    selector: '.center', title: '连线搭建集群', icon: '🔗', place: 'bottom',
    text: '第三步：连线搭建集群\n· 点击节点底部 🔗 连线按钮\n· 拖到另一节点释放建立连线\n· 双击连线可删除\n· 连线表示智能体间的协作关系',
  },
  {
    selector: '.center', title: '运行与对话', icon: '▶️', place: 'bottom',
    text: '第四步：运行集群与对话\n· 点击节点 💬 对话按钮选择独立/集群对话\n· 工具栏 ▶ 运行 启动集群\n· 双击节点隐藏/显示消息泡泡\n· 右键节点可配置触发方式',
  },
  {
    selector: '.right', title: '发送消息', icon: '💬', place: 'left',
    text: '第五步：在对话区与智能体沟通\n· 输入消息发送，查看流式回复\n· 展开过程查看智能体思考工作过程\n· 右键消息可复制/删除/撤回等\n· 拖入文件可快速发送文件路径',
  },
]

const idx = ref(0)
const currentStep = computed(() => steps[idx.value])
const isLast = computed(() => idx.value === steps.length - 1)
const isFirst = computed(() => idx.value === 0)

const hole = ref({ left: 0, top: 0, width: 0, height: 0, visible: false })
const bubble = ref({ left: 0, top: 0 })
const actualPlace = ref<'left' | 'right' | 'top' | 'bottom' | 'inside'>('right')

const BUBBLE_W = 376
const BUBBLE_H = 280

function compute() {
  const step = currentStep.value
  const el = document.querySelector(step.selector) as HTMLElement | null
  if (!el) { hole.value.visible = false; return }
  const r = el.getBoundingClientRect()
  if (r.width === 0 || r.height === 0) { hole.value.visible = false; return }
  const pad = 6
  hole.value = { left: r.left - pad, top: r.top - pad, width: r.width + pad * 2, height: r.height + pad * 2, visible: true }

  const gap = 24
  const vw = window.innerWidth, vh = window.innerHeight
  const allPlaces: ('left' | 'right' | 'top' | 'bottom')[] = ['right', 'bottom', 'left', 'top']
  const order = [step.place, ...allPlaces.filter(p => p !== step.place)]

  for (const p of order) {
    let left = 0, top = 0
    if (p === 'right') { left = r.right + gap; top = r.top + r.height / 2 - BUBBLE_H / 2 }
    else if (p === 'left') { left = r.left - gap - BUBBLE_W; top = r.top + r.height / 2 - BUBBLE_H / 2 }
    else if (p === 'bottom') { left = r.left + r.width / 2 - BUBBLE_W / 2; top = r.bottom + gap }
    else { left = r.left + r.width / 2 - BUBBLE_W / 2; top = r.top - gap - BUBBLE_H }
    if (left >= 8 && left + BUBBLE_W <= vw - 8 && top >= 8 && top + BUBBLE_H <= vh - 8) {
      actualPlace.value = p
      bubble.value = { left, top }
      return
    }
  }

  actualPlace.value = 'inside'
  let left = r.left + 20, top = r.top + 20
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

let timer: number | null = null
onMounted(() => {
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
      <div v-if="hole.visible" class="fg-hole"
           :style="{ left: hole.left + 'px', top: hole.top + 'px', width: hole.width + 'px', height: hole.height + 'px' }" />
      <div class="fg-bubble popup-surface" :class="arrowClass" :style="bubbleStyle">
        <div class="fg-step">{{ idx + 1 }} / {{ steps.length }}</div>
        <div class="fg-head">
          <span class="fg-icon">{{ currentStep.icon }}</span>
          <span class="fg-title">{{ currentStep.title }}</span>
        </div>
        <div class="fg-text">{{ currentStep.text }}</div>
        <div class="fg-actions">
          <button class="btn ghost" @click="skip">跳过</button>
          <button class="btn ghost" @click="prev" :disabled="isFirst">上一步</button>
          <button class="btn primary" @click="next">{{ isLast ? '完成' : '下一步' }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.fg-root { position: fixed; inset: 0; z-index: 3200; }
.fg-hole {
  position: fixed; border-radius: 10px; z-index: 3201; pointer-events: none;
  box-shadow: 0 0 0 9999px rgba(var(--shadow-rgb), 0.55);
  border: 2px solid rgba(var(--accent-rgb), 0.6);
  transition: all .3s ease;
}
.fg-bubble {
  position: fixed; z-index: 3202; width: 340px; padding: 16px 18px;
  display: flex; flex-direction: column; gap: 10px;
  transition: top .3s ease, left .3s ease;
}
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
.fg-step {
  align-self: flex-start; font-size: calc(11px * var(--font-scale)); opacity: .5;
  font-family: Consolas, monospace; padding: 2px 8px; border-radius: 4px;
  background: rgba(var(--surface-rgb), 0.08);
}
.fg-head { display: flex; align-items: center; gap: 8px; }
.fg-icon { font-size: calc(20px * var(--font-scale)); }
.fg-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; }
.fg-text { font-size: calc(12.5px * var(--font-scale)); line-height: 1.7; white-space: pre-line; opacity: .85; }
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