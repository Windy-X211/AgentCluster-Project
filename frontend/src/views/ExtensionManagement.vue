<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api'
import { loadExtensions } from '@/extensions/loader'

interface MarketItem {
  id: string; name: string; version: string; type: string
  author?: string; description: string; icon?: string
  tags?: string[]; source?: string; frontend_files?: string[]
  viz_types?: string[]; status?: string
  installed?: boolean; enabled?: boolean
}

const market = ref<MarketItem[]>([])
const loading = ref(false)
const busy = ref<string>('')   // 正在操作的拓展 id
const toast = ref('')

const hasInstalled = computed(() => market.value.some(m => m.installed))

async function refresh() {
  loading.value = true
  try {
    const res = await api.extensions.list()
    market.value = res.market || []
  } catch (e) {
    toast.value = '加载失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}

async function install(m: MarketItem) {
  busy.value = m.id
  try {
    await api.extensions.install(m.id)
    toast.value = `已安装：${m.name}`
    await refresh()
    await loadExtensions(true)
  } catch (e) {
    toast.value = '安装失败：' + (e as Error).message
  } finally { busy.value = '' }
}
async function enable_(m: MarketItem) {
  busy.value = m.id
  try {
    await api.extensions.enable(m.id)
    toast.value = `已启用：${m.name}`
    await refresh()
    await loadExtensions(true)
  } catch (e) {
    toast.value = '启用失败：' + (e as Error).message
  } finally { busy.value = '' }
}
async function disable_(m: MarketItem) {
  busy.value = m.id
  try {
    await api.extensions.disable(m.id)
    toast.value = `已停用：${m.name}`
    await refresh()
    await loadExtensions(true)
  } catch (e) {
    toast.value = '停用失败：' + (e as Error).message
  } finally { busy.value = '' }
}
async function uninstall(m: MarketItem) {
  if (!confirm(`确认卸载「${m.name}」？将删除其代码与配置。`)) return
  busy.value = m.id
  try {
    await api.extensions.uninstall(m.id)
    toast.value = `已卸载：${m.name}`
    await refresh()
    await loadExtensions(true)
  } catch (e) {
    toast.value = '卸载失败：' + (e as Error).message
  } finally { busy.value = '' }
}

function typeLabel(t: string) {
  return ({ 'viz-renderer': '可视化渲染器', 'backend-service': '后端服务', 'ui-module': '界面模块', hybrid: '混合' } as Record<string, string>)[t] || t
}

onMounted(refresh)
</script>

<template>
  <div class="em-root region-extensions">
    <div class="top glass">
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <div class="top-drag pywebview-drag-region">
        <h2>🧩 功能拓展</h2>
        <span class="sub">安装 / 启用 / 卸载界面与功能拓展插件</span>
      </div>
    </div>

    <div class="info glass">
      <span class="info-ico">📖</span>
      <span class="info-text">
        拓展代码保存在 <code>AppData/extensions/</code>，开发规范见
        <code>AppData/extensions/EXTENSION_SPEC.md</code>。
        可视化交互类拓展通过智能体输出 <code>```viz:type</code> 代码块触发渲染。
      </span>
    </div>

    <div class="body">
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!market.length" class="empty">暂无可用拓展</div>
      <template v-else>
        <!-- 已安装分组 -->
        <div v-if="hasInstalled" class="group-title">已安装</div>
        <div v-for="m in market.filter(x => x.installed)" :key="m.id" class="ext-card glass" :class="{ disabled: !m.enabled }">
          <div class="ec-head">
            <span class="ec-icon">{{ m.icon || '🧩' }}</span>
            <div class="ec-title-wrap">
              <div class="ec-title">{{ m.name }}</div>
              <div class="ec-desc">{{ m.description }}</div>
            </div>
            <div class="ec-status" :class="m.enabled ? 'on' : 'off'">{{ m.enabled ? '● 已启用' : '○ 已停用' }}</div>
          </div>
          <div class="ec-meta">
            <span class="tag">v{{ m.version }}</span>
            <span class="tag">{{ typeLabel(m.type) }}</span>
            <span v-if="m.author" class="tag dim">{{ m.author }}</span>
            <span v-for="t in (m.tags || [])" :key="t" class="tag dim">#{{ t }}</span>
            <span v-if="m.viz_types && m.viz_types.length" class="tag dim">viz: {{ m.viz_types.join(', ') }}</span>
          </div>
          <div class="ec-actions">
            <button v-if="!m.enabled" class="act primary" :disabled="busy === m.id" @click="enable_(m)">启用</button>
            <button v-else class="act" :disabled="busy === m.id" @click="disable_(m)">停用</button>
            <button class="act danger" :disabled="busy === m.id" @click="uninstall(m)">卸载</button>
          </div>
        </div>

        <!-- 可安装分组 -->
        <div class="group-title">可安装</div>
        <div v-for="m in market.filter(x => !x.installed)" :key="m.id" class="ext-card glass">
          <div class="ec-head">
            <span class="ec-icon">{{ m.icon || '🧩' }}</span>
            <div class="ec-title-wrap">
              <div class="ec-title">{{ m.name }}</div>
              <div class="ec-desc">{{ m.description }}</div>
            </div>
            <span v-if="m.status === 'preview'" class="ec-badge">预览</span>
          </div>
          <div class="ec-meta">
            <span class="tag">v{{ m.version }}</span>
            <span class="tag">{{ typeLabel(m.type) }}</span>
            <span v-if="m.author" class="tag dim">{{ m.author }}</span>
            <span v-for="t in (m.tags || [])" :key="t" class="tag dim">#{{ t }}</span>
            <span v-if="m.viz_types && m.viz_types.length" class="tag dim">viz: {{ m.viz_types.join(', ') }}</span>
          </div>
          <div class="ec-actions">
            <button class="act primary" :disabled="busy === m.id || m.status === 'preview'" @click="install(m)">
              {{ m.status === 'preview' ? '待开发' : '安装' }}
            </button>
          </div>
        </div>
      </template>
    </div>

    <Teleport to="body">
      <div v-if="toast" class="ext-toast" @click="toast = ''">{{ toast }}</div>
    </Teleport>
  </div>
</template>

<style scoped>
.em-root {
  padding: 10px 16px 16px; height: calc(100vh - var(--titlebar-h));
  display: flex; flex-direction: column; min-height: 0; overflow: hidden;
}
.top {
  padding: 8px 16px; margin-bottom: 10px;
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
  h2 { font-size: calc(18px * var(--font-scale)); }
}
.top-drag { flex: 1; align-self: stretch; display: flex; align-items: center; gap: 12px; user-select: none; }
.sub { font-size: calc(12px * var(--font-scale)); opacity: .5; }

.info {
  padding: 8px 14px; margin-bottom: 12px; flex-shrink: 0;
  display: flex; align-items: center; gap: 10px;
  font-size: calc(12px * var(--font-scale)); opacity: .85;
}
.info-ico { font-size: 16px; }
.info-text code { background: rgba(255,255,255,.08); padding: 1px 5px; border-radius: 3px; font-size: .92em; }

.body { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 10px; }
.empty { text-align: center; opacity: .5; padding: 40px 0; }
.group-title { font-size: calc(12px * var(--font-scale)); font-weight: 600; opacity: .55; margin: 6px 2px 2px; }

.ext-card { padding: 12px 14px; border-radius: 10px; transition: opacity .2s; }
.ext-card.disabled { opacity: .6; }
.ec-head { display: flex; align-items: flex-start; gap: 10px; }
.ec-icon { font-size: 22px; line-height: 1; flex-shrink: 0; }
.ec-title-wrap { flex: 1; min-width: 0; }
.ec-title { font-weight: 600; font-size: calc(14px * var(--font-scale)); }
.ec-desc { font-size: calc(12px * var(--font-scale)); opacity: .65; margin-top: 2px; line-height: 1.4; }
.ec-status { font-size: 11px; flex-shrink: 0; padding: 2px 8px; border-radius: 10px; }
.ec-status.on { color: #6fcf97; background: rgba(111,207,151,.12); }
.ec-status.off { color: #999; background: rgba(255,255,255,.06); }
.ec-badge { font-size: 10px; padding: 2px 6px; border-radius: 8px; background: rgba(255,180,80,.18); color: #ffb84d; flex-shrink: 0; }

.ec-meta { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 8px 32px; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 8px; background: rgba(var(--accent-rgb,120,140,255),.14); color: var(--accent-light,#9db4ff); }
.tag.dim { background: rgba(255,255,255,.06); color: rgba(255,255,255,.55); }

.ec-actions { display: flex; gap: 8px; margin-left: 32px; }
.act { padding: 5px 14px; border-radius: 7px; border: 1px solid rgba(255,255,255,.15); background: transparent; color: inherit; cursor: pointer; font-size: 12px; transition: all .15s; }
.act:hover { background: rgba(255,255,255,.08); }
.act:disabled { opacity: .45; cursor: not-allowed; }
.act.primary { background: rgba(var(--accent-rgb,120,140,255),.2); border-color: rgba(var(--accent-rgb,120,140,255),.5); color: var(--accent-light,#9db4ff); }
.act.primary:hover { background: rgba(var(--accent-rgb,120,140,255),.3); }
.act.danger { color: #ff8080; border-color: rgba(255,128,128,.3); }
.act.danger:hover { background: rgba(255,128,128,.12); }

.ext-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); z-index: 10000; padding: 8px 18px; border-radius: 8px; background: rgba(20,25,40,.95); color: #fff; font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.4); cursor: pointer; }
</style>