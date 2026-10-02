<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { useConvStore, useAgentStore, useCanvasStore, useModelStore, useSettingsStore } from '@/stores'
import { convDisplayName, toolLabel } from '@/stores/conversation'
import { renderMarkdown } from '@/utils/markdown'


const convs = useConvStore()
const agents = useAgentStore()
const canvases = useCanvasStore()
const settings = useSettingsStore()
const models = useModelStore()

const input = ref('')
const currentAgent = ref<number | null>(null)
const currentInterface = ref<number | null>(null)

const conv = computed(() => convs.currentData())
const canvas = computed(() => canvases.current)
const allInterfaces = computed(() => models.list)

/** 去掉消息首尾纯空行（保留中间换行）—— 用于修复气泡顶部空白 */
function trimMsgContent(s: string | undefined): string {
  if (!s) return ''
  return s.replace(/^\s*\n+/, '').replace(/\n+\s*$/, '')
}

/** Markdown 渲染缓存：key=content+streaming flag，避免重复解析 */
const _mdCache = new Map<string, string>()
const MD_CACHE_MAX = 100
function cachedRender(content: string, streaming: boolean): string {
  const key = content + '|' + (streaming ? '1' : '0')
  const cached = _mdCache.get(key)
  if (cached !== undefined) return cached
  // 超过上限时清一半（简单 LRU）
  if (_mdCache.size >= MD_CACHE_MAX) {
    let count = 0
    for (const k of _mdCache.keys()) {
      _mdCache.delete(k)
      if (++count >= MD_CACHE_MAX / 2) break
    }
  }
  const html = renderMarkdown(content)
  _mdCache.set(key, html)
  return html
}

/** 消息泡泡 Markdown 渲染（用户/助手统一走这里，含流式光标） */
function msgBubbleHtml(m: any): string {
  const trimmed = trimMsgContent(m.content)
  const html = cachedRender(trimmed, !!m?.streaming)
  return m?.streaming ? html + '<span class="stream-cursor">▌</span>' : html
}
/** 根据 agent_id 查智能体名 */
function agentNameOf(agentId: number | null | undefined): string {
  if (agentId == null) return ''
  return agents.list.find((a: any) => a.id === agentId)?.name || ''
}

// 自动滚动到底部（含流式内容增长）
const msgBox = ref<HTMLElement | null>(null)
const taRef = ref<HTMLTextAreaElement | null>(null)
const visibleMsgs = computed(() => (conv.value?.messages || []).filter((m: any) => !m.relay))

// —— 工具调用归属：每条 tool_call/tool_result 归属于「前面最近一条同 agent_id 的 assistant 消息」——
const expandedA2a = ref(new Set<number>())
const toolBarOpen = ref(new Set<number>())       // 哪个 assistant 的工具行展开
const toolDetailOpen = ref(new Map<number, string>())  // assistant idx → 当前展开详情的 tool_call_id

/** 按 assistant 索引 → 其后紧随的连续工具（同 agent_id） */
const msgToolsMap = computed(() => {
  const msgs = visibleMsgs.value
  const map = new Map<number, any[]>()
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i] as any
    if (m.role === 'assistant') {
      const bucket: any[] = []
      let j = i + 1
      while (j < msgs.length) {
        const n = msgs[j] as any
        if ((n.role === 'tool_call' || n.role === 'tool_result') && n.agent_id === m.agent_id) {
          bucket.push(n); j++
        } else { break }
      }
      if (bucket.length) map.set(i, bucket)
    }
  }
  return map
})

/** 当前正在流式调用的工具（TS:/C: 到达时 store 已设置） */
const speakingTool = computed(() => convs.speakingToolCall)

/** 给定 assistant 索引下的「完整工具列表」—— 包含已归档工具 + 正在流式运行的工具（如果该 assistant 就是发言者） */
function toolsOfAssistant(assistantIdx: number): any[] {
  const msgs = visibleMsgs.value
  const a = msgs[assistantIdx] as any
  const archived = msgToolsMap.value.get(assistantIdx) || []
  // 正在流式运行且属于这个 agent → 插入一条虚拟 running pill
  if (convs.streaming && speakingTool.value && speakingAgentSameAs(assistantIdx)) {
    const hasVirtual = archived.some((x: any) => x.role === 'tool_call' && x._running)
    if (!hasVirtual) {
      const lastCall = [...archived].reverse().find((x: any) => x.role === 'tool_call')
      if (!lastCall) {
        // 还没 push 任何 tool_call，也插入一条 running pill（用于 TS:/C: 刚到的瞬间）
        return [{ role: 'tool_call', name: speakingTool.value.name, tool_call_id: '_virtual_running', args: {}, _running: speakingTool.value.phase || 'args' }, ...archived]
      }
    }
  }
  return archived
}
function speakingAgentSameAs(assistantIdx: number): boolean {
  const msgs = visibleMsgs.value
  const a = msgs[assistantIdx] as any
  if (a == null) return false
  return a.agent_id != null && a.agent_id === convs.speakingAgent
}

/** 该 assistant 是否就是当前最末位（用于自动展开） */
function isLastAssistant(idx: number): boolean {
  const msgs = visibleMsgs.value
  for (let i = msgs.length - 1; i >= 0; i--) {
    if ((msgs[i] as any).role === 'assistant') return i === idx
  }
  return false
}
/** 是否有真实工具调用（用于决定是否显示工具行） */
function hasTools(idx: number): boolean {
  return toolsOfAssistant(idx).some((t: any) => t.role === 'tool_call')
}
/** 工具行（汇总 pill）是否展开 */
function toolBarIsOpen(idx: number): boolean {
  if (toolBarOpen.value.has(idx)) return true
  if (convs.streaming && isLastAssistant(idx) && hasTools(idx)) return true
  return false
}
function toggleToolBar(idx: number) {
  const s = new Set(toolBarOpen.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  toolBarOpen.value = s
}
function toggleToolDetail(assistantIdx: number, tcId: string) {
  const m = new Map(toolDetailOpen.value)
  if (m.get(assistantIdx) === tcId) m.delete(assistantIdx)
  else m.set(assistantIdx, tcId)
  toolDetailOpen.value = m
}
function toolDetailIsOpen(assistantIdx: number, tcId: string): boolean {
  return toolDetailOpen.value.get(assistantIdx) === tcId
}

function toggleA2a(idx: number) {
  const s = new Set(expandedA2a.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  expandedA2a.value = s
}

/** 工具是否仍在运行：在**整个** visibleMsgs 里找配对 tool_result（不再局限于 bucket）。
 *  之前只在 bucket 里找 → 多 tool_call 连续到达时，tool_result 被后续 assistant 截断，
 *  导致 toolIsRunning 永远返回 true → 执行中动画不停。 */
function toolIsRunning(tc: any): boolean {
  if (tc._running) return true
  if (tc.role !== 'tool_call') return false
  // 在整个 visibleMsgs 里搜索配对 tool_result，不再局限于 bucket
  const hasResult = visibleMsgs.value.some(
    (x: any) => x.role === 'tool_result' && x.tool_call_id === tc.tool_call_id
  )
  if (!hasResult) {
    console.log('[UI] toolIsRunning: tc.tool_call_id=', tc.tool_call_id, 'NOT FOUND in visibleMsgs, roles=', visibleMsgs.value.map((m: any) => m.role + ':' + m.tool_call_id))
  }
  return !hasResult
}

const latestToolInfoMap = computed(() => {
  const map = new Map<number, { name: string; running: boolean; total: number; index: number } | null>()
  const msgs = visibleMsgs.value
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i] as any
    if (m.role !== 'assistant') continue
    const tools = toolsOfAssistant(i)
    const calls = tools.filter((t: any) => t.role === 'tool_call')
    if (calls.length === 0) continue
    // 正在执行的优先；否则取最后一个（最新完成的）
    const runningIdx = calls.findIndex((t: any) => toolIsRunning(t))
    const pickedIdx = runningIdx >= 0 ? runningIdx : calls.length - 1
    const picked = calls[pickedIdx]
    map.set(i, {
      name: friendlyToolName(picked.name),
      running: runningIdx >= 0,
      total: calls.length,
      index: pickedIdx,
    })
  }
  return map
})
function latestToolInfo(idx: number) {
  return latestToolInfoMap.value.get(idx) || null
}

function scrollToBottom(smooth = false) {
  const el = msgBox.value
  if (!el) return
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  } else {
    // 先清一次：某些浏览器在 v-for 换数据瞬间 height 尚未稳定
    el.scrollTop = el.scrollHeight
    requestAnimationFrame(() => {
      if (msgBox.value) msgBox.value.scrollTop = msgBox.value.scrollHeight
    })
  }
}

const scrollKey = computed(() => {
  const msgs = conv.value?.messages || []
  const last = msgs[msgs.length - 1]
  return `${convs.streaming ? 1 : 0}:${msgs.length}:${(last?.content || '').length}`
})
watch(scrollKey, async () => {
  await nextTick()
  scrollToBottom(false)
})

// 切换对话记录时，等消息重新渲染后再滑到底部
watch(() => convs.current, async (id, prev) => {
  if (id == null) return
  await nextTick()
  scrollToBottom(false)
})

// ① 节点💬直接对话：回填选中智能体并聚焦
watch(() => convs.selectedAgentId, (id) => {
  if (id != null) currentAgent.value = id
})
watch(() => convs.focusChatAt, async (t) => {
  if (!t) return
  await nextTick()
  taRef.value?.focus()
})

async function send() {
  const text = input.value.trim(); if (!text) return
  const agentId = currentAgent.value ?? agents.list[0]?.id
  if (!agentId) return alert('请先新建一个智能体（或等后端启动自动创建「集群编排师」）')
  await convs.send(agentId, text)
  input.value = ''
}

function createConv(mode: string) {
  convs.create({ name: '新对话', mode, agent_ids: [currentAgent.value].filter(Boolean), canvas_id: canvas.value?.id })
}
function selectConv(id: number) { convs.current = id }

onMounted(async () => {
  // 默认选中「集群编排师」，没有则选第一个
  const orchestrator = agents.list.find(a => a.name === '集群编排师')
  currentAgent.value = orchestrator?.id ?? agents.list[0]?.id ?? null
  if (!convs.current && convs.list.length) convs.current = convs.list[0].id
})

// tool_call 参数美化
function prettyArgs(args: any): string {
  if (!args) return ''
  try { return JSON.stringify(args, null, 2) } catch { return String(args) }
}
function prettyResult(result: any): string {
  if (!result) return ''
  try {
    const s = JSON.stringify(result, null, 2)
    return s.length > 600 ? s.slice(0, 600) + '\n...（已截断）' : s
  } catch { return String(result) }
}

/** 归属该 assistant 的「分享截图」：智能体调用 browser_control screenshot(purpose=share) 保存给用户看的页面截图。
 *  返回 [{file, url}]，前端在对话界面直接内嵌渲染 <img>。inspect 类截图不在此列（不落盘、只给智能体看）。 */
function shareShots(assistantIdx: number): { file: string; url: string }[] {
  const shots: { file: string; url: string }[] = []
  for (const t of toolsOfAssistant(assistantIdx)) {
    if (t.role !== 'tool_result') continue
    const r = t.result
    if (r && typeof r === 'object' && r.purpose === 'share' && r.file) {
      shots.push({ file: String(r.file), url: '/api/fs/image?path=' + encodeURIComponent(String(r.file)) })
    }
  }
  return shots
}

// A2A 信封卡片
function kindLabel(kind: string): string {
  return ({ handoff: '成果移交', message: '交流讨论', artifact: '成果' } as Record<string, string>)[kind] || kind
}

// 工具行文字与能力标签完全一致：统一取自 stores/conversation.ts 的 toolLabel（后端 commandsMeta/pluginsMeta/skillsMeta）
const friendlyToolName = toolLabel
</script>

<template>
  <div class="main-chat glass">
    <div class="chat-sidebar" v-if="convs.list.length || true">
      <div class="section-title"><span>对话</span>
        <div class="btns">
          <button class="btn sm ghost" @click="createConv('independent')">+独立</button>
          <button class="btn sm ghost" @click="createConv('linked')">+关联</button>
        </div>
      </div>
      <div class="conv-list">
        <div v-for="c in convs.list" :key="c.id" :class="['conv-item', { active: conv?.id === c.id }]" @click="selectConv(c.id)">
          <span class="mode-tag" :class="c.mode">{{ c.mode === 'linked' ? '集群' : '独立' }}</span>
          <span class="conv-name">{{ convDisplayName(c) }}</span>
          <span class="del" @click.stop="convs.remove(c.id)">×</span>
        </div>
      </div>
    </div>

    <div class="chat-main">
      <div class="chat-messages" ref="msgBox">
        <div v-if="!conv" class="no-conv">请新建或选择一个对话</div>

                <!-- 渲染可见消息（过滤接力段 relay）；tool_call/tool_result 不单独出块，归属到紧接其前的 assistant -->
        <template v-for="(m, idx) in visibleMsgs" :key="m.ts + '-' + idx">
          <template v-if="m.role !== 'tool_call' && m.role !== 'tool_result'">
            <!-- 用户消息（靠右） -->
            <div v-if="m.role === 'user'" class="msg user">
              <div class="role">你</div>
              <div class="bubble markdown" v-html="msgBubbleHtml(m)"></div>
            </div>

            <!-- A2A 信封（折叠头部常驻） -->
            <div v-else-if="m.role === 'a2a'" class="tool-card a2a">
              <div class="tc-header a2a-toggle" @click="toggleA2a(idx)">
                <span class="tc-ico">🤝</span>
                <span class="tc-name">{{ kindLabel(m.envelope?.kind) }}</span>
                <span class="a2a-route">{{ m.envelope?.from?.name }} → {{ m.envelope?.to?.name }}</span>
                <span class="tc-id">{{ m.envelope?.protocol }} · {{ m.envelope?.task?.state }}</span>
                <span class="tg-chev">{{ expandedA2a.has(idx) ? '▾' : '▸' }}</span>
              </div>
              <div v-if="expandedA2a.has(idx)" class="a2a-body">
                <div class="a2a-meta">
                  <span class="a2a-chip">{{ m.envelope?.task_id }}</span>
                  <span class="a2a-chip dim">{{ m.envelope?.message_id }}</span>
                </div>
                <template v-for="(p, pi) in m.envelope?.parts || []" :key="pi">
                  <div v-if="p.type === 'text'" class="a2a-text">{{ p.text }}</div>
                  <details v-else-if="p.type === 'artifact'" class="tc-body" open>
                    <summary>📎 {{ p.name }}（{{ p.mime_type }}）</summary>
                    <pre>{{ p.content }}</pre>
                  </details>
                </template>
              </div>
            </div>

            <!-- 助手回复 + 内嵌工具行（所有工具归属到这个 assistant 下方） -->
            <div v-else-if="m.role === 'assistant'" class="msg assistant" :class="{ empty: !trimMsgContent(m.content) && !hasTools(idx), streaming: m.streaming }">
              <div class="col-wrap">
                <div class="head-row">
                  <div class="avatar">{{ (agentNameOf(m.agent_id) || '?').slice(0,1).toUpperCase() }}</div>
                  <div v-if="agentNameOf(m.agent_id)" class="who-assistant">{{ agentNameOf(m.agent_id) }}</div>
                </div>
                <!-- 文本泡泡：有内容渲染 markdown；无内容但有工具调用则省略；流式且无工具时显示光标 -->
                <template v-if="trimMsgContent(m.content)">
                  <div class="bubble markdown" v-html="msgBubbleHtml(m)"></div>
                </template>
                <div v-else-if="m.streaming && !hasTools(idx)" class="bubble">▌</div>

                <!-- —— 分享截图：智能体主动保存给用户看的页面截图，直接内嵌在对话里 —— -->
                <div v-if="shareShots(idx).length" class="shot-list">
                  <a v-for="s in shareShots(idx)" :key="s.file" :href="s.url" target="_blank" rel="noopener">
                    <img class="shot-img" :src="s.url" :alt="s.file" @click.stop />
                  </a>
                </div>

                <!-- —— 工具行：只在有真实 tool_call 时出现 —— -->
                <div v-if="hasTools(idx)" class="tool-row">
                  <!-- 一级：正在执行的 / 最新工具 + 状态 -->
                  <div class="tr-summary" :class="{ open: toolBarIsOpen(idx), running: latestToolInfo(idx)?.running }" @click="toggleToolBar(idx)">
                    <span class="tr-icon" :class="{ spinning: latestToolInfo(idx)?.running }">{{ latestToolInfo(idx)?.running ? '⟳' : '✓' }}</span>
                    <span class="tr-summary-name">{{ latestToolInfo(idx)?.name }}</span>
                    <span class="tr-status">{{ latestToolInfo(idx)?.running ? '执行中' : '已完成' }}</span>
                    <span class="tr-count">共 {{ latestToolInfo(idx)?.total || 0 }} 个工具</span>
                    <span class="tr-chev">{{ toolBarIsOpen(idx) ? '▾' : '▸' }}</span>
                  </div>

                  <!-- 展开：工具清单 + 详情 -->
                  <div v-if="toolBarIsOpen(idx)" class="tr-items">
                    <div v-for="t in toolsOfAssistant(idx)" :key="t.tool_call_id || ('tc-' + idx + '-' + t.name)" class="tr-item">
                      <div v-if="t.role === 'tool_call'" class="tr-pill"
                           :class="{ running: toolIsRunning(t), expanded: toolDetailIsOpen(idx, t.tool_call_id) }"
                           @click="toggleToolDetail(idx, t.tool_call_id)">
                        <span class="tr-icon" :class="{ spinning: toolIsRunning(t) }">{{ toolIsRunning(t) ? '⟳' : '✓' }}</span>
                        <span class="tr-name">{{ friendlyToolName(t.name) }}</span>
                        <span class="tr-toggle">{{ toolDetailIsOpen(idx, t.tool_call_id) ? '▾' : '▸' }}</span>
                      </div>
                      <!-- 详情（参数 + 结果） -->
                      <div v-if="t.role === 'tool_call' && toolDetailIsOpen(idx, t.tool_call_id)" class="tr-detail">
                        <div class="tr-block">
                          <div class="tr-block-title">参数</div>
                          <pre>{{ prettyArgs(t.args) }}</pre>
                        </div>
                        <template v-for="res in toolsOfAssistant(idx).filter(x => x.role === 'tool_result' && x.tool_call_id === t.tool_call_id)" :key="res.tool_call_id + '-r'">
                          <div class="tr-block">
                            <div class="tr-block-title">📥 结果</div>
                            <pre>{{ prettyResult(res.result) }}</pre>
                          </div>
                        </template>
                        <div v-if="toolIsRunning(t)" class="tr-block running">
                          <span class="ti-spinner"></span>
                          <span>执行中…{{ speakingTool?.phase === 'args' ? '（参数流式中）' : '' }}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </template>
      </div>

      <div class="chat-input">
        <div class="row1">
          <select class="input sm" v-model="currentAgent">
            <option v-for="a in agents.list" :key="a.id" :value="a.id">🧠 {{ a.name }}</option>
          </select>
          <select class="input sm" v-if="allInterfaces.length" v-model="currentInterface">
            <option :value="null">默认接口</option>
            <option v-for="m in allInterfaces" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </div>
        <div class="row2">
          <textarea
            ref="taRef"
            class="input input-ta"
            v-model="input"
            :placeholder="
              currentAgent == null
                ? '输入消息…'
                : agents.list.find(a => a.id === currentAgent)?.name === '集群编排师'
                  ? '告诉编排师你想要的集群，比如「帮我搭一个产品研发集群」「加一个设计师角色」「让 A 连接 B」…'
                  : '输入消息…'
            "
            @keydown.enter.exact.prevent="send"
          ></textarea>
          <button class="btn send" :disabled="convs.streaming" @click="send">发送</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.main-chat { display: flex; overflow: hidden; flex: 1; min-height: 0; }
.chat-sidebar { width: 180px; border-right: 1px solid rgba(255,255,255,0.08); padding: 10px 6px; display: flex; flex-direction: column; }
.conv-list { overflow-y: auto; flex: 1; }
.conv-item { padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; display: flex; align-items: center; gap: 6px; margin-bottom: 2px; position: relative; }
.conv-item:hover { background: rgba(255,255,255,0.05); }
.conv-item.active { background: rgba(110,168,255,0.15); border: 1px solid rgba(110,168,255,0.25); }
.conv-item .conv-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-item .del { opacity: 0; font-size: 16px; padding: 0 2px; }
.conv-item .del:hover { opacity: 1; }
.conv-item:hover .del { opacity: .6; }
.mode-tag { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: rgba(255,255,255,0.1); }
.mode-tag.linked { background: rgba(180,114,255,0.2); color: #d4a8ff; }
.btns { display: flex; gap: 4px; }
.sm { padding: 3px 8px; font-size: 11px; }

.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.chat-messages { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.no-conv { text-align: center; opacity: .5; margin: 40px auto; }

.msg { display: flex; gap: 8px; max-width: 85%; }
.msg.user { align-self: flex-start; flex-direction: row; }
.msg.user .bubble { background: linear-gradient(135deg, rgba(110,168,255,0.3), rgba(180,114,255,0.25)); border-color: rgba(110,168,255,0.3); }
.msg.assistant { align-self: flex-start; flex-direction: column; }
.msg.assistant .bubble { background: rgba(255,255,255,0.06); border-top-left-radius: 4px; }
.msg.assistant.streaming .bubble { border-color: rgba(110,168,255,0.35); }
.col-wrap { display: flex; flex-direction: column; gap: 4px; }
.head-row { display: flex; align-items: center; gap: 8px; }
.avatar { width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, #6ea8ff, #b472ff); display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex-shrink: 0; color: #fff; }
.who-assistant { font-size: 12px; color: rgba(232,236,244,0.75); opacity: .9; line-height: 1; }
.bubble { padding: 10px 14px; border-radius: 12px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.1); }

/* 分享截图：智能体主动展示给用户的页面截图 */
.shot-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; max-width: 460px; }
.shot-list a { display: inline-block; }
.shot-img { display: block; max-width: 100%; max-height: 280px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.16); box-shadow: 0 2px 12px rgba(0,0,0,0.32); cursor: zoom-in; transition: transform .15s, border-color .15s; }
.shot-img:hover { transform: scale(1.015); border-color: rgba(110,168,255,0.55); }

/* 流式输出光标 */
.stream-cursor {
  display: inline-block; margin-left: 2px; color: #b472ff;
  animation: cursorBlink 1s steps(2) infinite;
}
@keyframes cursorBlink { 50% { opacity: 0; } }

/* 工具调用卡片 */
.tool-card { align-self: flex-start; max-width: 90%; border-radius: 10px; font-size: 12px; border: 1px solid rgba(255,255,255,0.1); overflow: hidden; }
.tool-card.call { background: rgba(110,168,255,0.1); border-color: rgba(110,168,255,0.35); }
.tool-card.result { background: rgba(100,220,160,0.08); border-color: rgba(100,220,160,0.25); align-self: flex-start; }
.tool-card.a2a { background: rgba(180,114,255,0.12); border-color: rgba(180,114,255,0.4); align-self: flex-start; }
.tool-card.a2a .tc-name { color: #d4a8ff; }
.a2a-route { font-weight: 600; color: #d4a8ff; font-size: 12px; }
.a2a-meta { display: flex; gap: 6px; padding: 0 12px 4px; flex-wrap: wrap; }
.a2a-chip { font-family: Consolas, monospace; font-size: 10px; background: rgba(0,0,0,0.3); padding: 1px 6px; border-radius: 4px; color: #d4a8ff; }
.a2a-chip.dim { opacity: .55; }
.a2a-text { padding: 4px 12px 8px; font-size: 12px; line-height: 1.55; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
.tc-header { padding: 8px 12px; display: flex; align-items: center; gap: 8px; font-size: 12px; }
.tc-ico { font-size: 14px; }
.tc-name { font-weight: 600; color: #a8c8ff; }
.tool-card.result .tc-header .tc-name { color: #80e4b8; }
.tc-id { font-family: Consolas, monospace; font-size: 10px; opacity: .45; margin-left: auto; }
.tc-body { padding: 0 12px 8px; }
.tc-body summary { cursor: pointer; font-size: 11px; opacity: .6; padding: 4px 0; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px; }
.tc-body summary::-webkit-details-marker { display: none; }
.tr-summary { color: #80e4b8; }
.tc-body pre { font-family: Consolas, "Courier New", monospace; font-size: 11px; background: rgba(0,0,0,0.25); padding: 8px 10px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin-top: 4px; max-height: 300px; }

/* —— 助手消息下的工具行：汇总 pill + 展开清单 + 单项详情 —— */
.tool-row { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
.tr-summary { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 10px; background: rgba(110,168,255,0.08); border: 1px solid rgba(110,168,255,0.22); cursor: pointer; font-size: 11.5px; color: rgba(232,236,244,0.72); user-select: none; width: fit-content; max-width: 100%; transition: background .15s; }
.tr-summary:hover { background: rgba(110,168,255,0.18); }
.tr-summary:hover .tr-chev { color: #a8c8ff; }
.tr-summary.open { background: rgba(110,168,255,0.22); border-color: rgba(110,168,255,0.45); color: rgba(232,236,244,0.95); }
.tr-summary.open .tr-chev { color: #a8c8ff; }
.tr-summary.running { border-color: rgba(110,168,255,0.55); background: rgba(110,168,255,0.12); color: #a8c8ff; }
.tr-summary.running .tr-icon { color: #a8c8ff; }
.tr-summary-name { font-weight: 500; color: rgba(232,236,244,0.92); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
.tr-status { font-size: 10.5px; opacity: .6; flex-shrink: 0; }
.tr-items { display: flex; flex-direction: column; gap: 3px; padding-left: 2px; }
.tr-ico { font-size: 12px; }
.tr-count { font-weight: 600; color: #a8c8ff; font-family: Consolas, monospace; }
.tr-label { opacity: .75; }
.tr-list { display: inline-flex; gap: 2px; flex-wrap: wrap; max-width: 260px; overflow: hidden; }
.tr-chip { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: rgba(0,0,0,0.28); color: rgba(232,236,244,0.7); white-space: nowrap; }
.tr-chip.more { background: rgba(110,168,255,0.2); color: #a8c8ff; }
.tr-chev { color: rgba(232,236,244,0.4); font-size: 11px; margin-left: 4px; transition: color .15s; }
/* 展开后的清单 */
.tr-item { display: flex; flex-direction: column; gap: 2px; }
.tr-pill { display: flex; align-items: center; gap: 5px; padding: 3px 8px; border-radius: 8px; background: rgba(0,0,0,0.18); border: 1px solid rgba(255,255,255,0.08); cursor: pointer; font-size: 11px; color: rgba(232,236,244,0.85); user-select: none; }
.tr-pill:hover { background: rgba(0,0,0,0.28); border-color: rgba(110,168,255,0.3); }
.tr-pill.running { border-color: rgba(110,168,255,0.55); background: rgba(110,168,255,0.1); color: #a8c8ff; }
.tr-pill.expanded { border-color: rgba(110,168,255,0.4); }
.tr-icon { width: 12px; text-align: center; font-size: 11px; transition: transform .3s; }
.tr-icon.spinning { display: inline-block; animation: tiSpin .8s linear infinite; }
@keyframes tiSpin { to { transform: rotate(360deg); } }
.tr-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tr-toggle { color: rgba(232,236,244,0.4); font-size: 10px; flex-shrink: 0; }
/* 详情块 */
.tr-detail { padding-left: 14px; border-left: 2px solid rgba(110,168,255,0.2); margin-top: 2px; display: flex; flex-direction: column; gap: 4px; }
.tr-block { padding: 4px 8px; border-radius: 6px; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.07); font-size: 11px; }
.tr-block.running { display: inline-flex; align-items: center; gap: 5px; background: rgba(110,168,255,0.1); border-color: rgba(110,168,255,0.3); color: #a8c8ff; padding: 3px 8px; width: fit-content; }
.tr-block-title { font-size: 10px; opacity: .65; margin-bottom: 2px; font-family: Consolas, monospace; }
.tr-block pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 10.5px; max-height: 200px; overflow-y: auto; }
/* 复用 ti-spinner（来自 tool-indicator，仍保留其关键规则供 .running 使用） */
.ti-spinner { width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid rgba(110,168,255,0.25); border-top-color: #6ea8ff; animation: tiSpin .8s linear infinite; flex-shrink: 0; display: inline-block; }
/* A2A 信封折叠（复用 .tg-chev 保持一致） */
.a2a-toggle { cursor: pointer; user-select: none; }
.a2a-toggle:hover { opacity: 0.85; }
.tg-chev { flex-shrink: 0; color: rgba(232,236,244,0.5); font-size: 12px; }
.chat-input { padding: 12px 16px; border-top: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; gap: 8px; }
.row1 { display: flex; gap: 8px; }
.row2 { display: flex; gap: 8px; align-items: flex-end; }
.input-ta { min-height: 50px; max-height: 120px; resize: vertical; font-family: inherit; }
.send { height: 38px; padding: 0 20px; }
</style>

<!-- 全局（非 scoped）样式：v-html 注入的 markdown 元素无法被 scoped 选择器命中，必须用全局样式 -->
<style>
.bubble.markdown { white-space: normal; word-break: break-word; }
.bubble.markdown > :first-child { margin-top: 0; }
.bubble.markdown > :last-child { margin-bottom: 0; }
.bubble.markdown p { margin: 0 0 6px; }
.bubble.markdown p:last-child { margin-bottom: 0; }
.bubble.markdown h1, .bubble.markdown h2, .bubble.markdown h3,
.bubble.markdown h4, .bubble.markdown h5, .bubble.markdown h6 {
  font-size: 14px; font-weight: 600; margin: 8px 0 4px; line-height: 1.4;
}
.bubble.markdown h1:first-child, .bubble.markdown h2:first-child,
.bubble.markdown h3:first-child { margin-top: 0; }
.bubble.markdown ul, .bubble.markdown ol { margin: 4px 0 6px; padding-left: 20px; }
.bubble.markdown li { margin: 2px 0; }
.bubble.markdown code {
  font-family: Consolas, "Courier New", monospace; font-size: 12px;
  background: rgba(0,0,0,0.3); padding: 1px 5px; border-radius: 4px;
}
.bubble.markdown pre {
  background: rgba(0,0,0,0.35); border-radius: 6px; padding: 8px 10px;
  margin: 6px 0; overflow-x: auto; white-space: pre;
}
.bubble.markdown pre code {
  background: none; padding: 0; font-size: 11.5px; color: #e8ecf4;
}
.bubble.markdown blockquote {
  border-left: 3px solid rgba(110,168,255,0.4); padding: 2px 10px;
  margin: 6px 0; color: rgba(232,236,244,0.75); background: rgba(255,255,255,0.03);
  border-radius: 0 6px 6px 0;
}
.bubble.markdown blockquote p { margin: 0; }
.bubble.markdown a { color: #a8c8ff; text-decoration: underline; text-underline-offset: 2px; }
.bubble.markdown a:hover { color: #d4a8ff; }
.bubble.markdown hr { border: none; border-top: 1px solid rgba(255,255,255,0.12); margin: 8px 0; }
/* 表格：深色实底 + 高对比边框，避免与泡泡半透明背景混在一起 */
.bubble.markdown table {
  border-collapse: collapse; border: 1px solid rgba(255,255,255,0.32);
  background: rgba(0,0,0,0.45); margin: 8px 0; font-size: 12px;
  width: max-content; max-width: 100%; border-radius: 6px; overflow: hidden;
}
.bubble.markdown th, .bubble.markdown td {
  border: 1px solid rgba(255,255,255,0.3); padding: 5px 9px; text-align: left;
  vertical-align: top; max-width: 300px;
}
.bubble.markdown th { background: rgba(255,255,255,0.12); font-weight: 600; border-bottom: 2px solid rgba(255,255,255,0.45); }
.bubble.markdown tbody tr:nth-child(even) td { background: rgba(255,255,255,0.04); }
.bubble.markdown strong { font-weight: 600; }
.bubble.markdown em { font-style: italic; }
.bubble.markdown del, .bubble.markdown s { text-decoration: line-through; opacity: .7; }
.bubble.markdown img { max-width: 100%; border-radius: 6px; margin: 4px 0; }

/* 流式输出光标（v-html 注入，需全局） */
.stream-cursor {
  display: inline-block; margin-left: 2px; color: #b472ff;
  animation: cursorBlink 1s steps(2) infinite;
}
@keyframes cursorBlink { 50% { opacity: 0; } }
</style>
