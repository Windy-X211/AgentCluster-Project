<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api'
import { loadExtensions } from '@/extensions/loader'
import { useAgentStore, useCanvasStore } from '@/stores'

interface KindInfo { key: string; label: string; count: number }
interface MarketItem {
  kind: string; id: string; name: string; description?: string
  version?: string; author?: string; icon?: string; tags?: string[]
  installed?: boolean; enabled?: boolean; preview?: string; bg_type?: string; agent_count?: number
}

const KIND_ICON: Record<string, string> = {
  agent: '🤖', canvas: '🗺', background: '🎨', skill: '🧠', plugin: '🔧', extension: '🧩',
}
const LOCAL_KINDS = ['skill', 'plugin', 'background', 'extension']

const kinds = ref<KindInfo[]>([])
const activeKind = ref('all')
const keyword = ref('')
const items = ref<MarketItem[]>([])
const uninstalledOnly = ref(false)
const loading = ref(false)
const busy = ref('')
const toast = ref('')
const needsSync = ref(false)
const syncing = ref(false)
const pushing = ref(false)

const agents = useAgentStore()
const canvases = useCanvasStore()

const activeLabel = computed(() =>
  kinds.value.find(k => k.key === activeKind.value)?.label || '推荐')
const searchPlaceholder = computed(() =>
  activeKind.value === 'all' ? '在全部资源中搜索…' : `只搜索${activeLabel.value}…`)
const searchScope = computed(() =>
  activeKind.value === 'all' ? '推荐 · 搜索全部资源' : `搜索范围：${activeLabel.value}`)
// 未安装过滤：勾选后只展示本地尚未安装的资源（安装状态由后端随列表返回）
const visibleItems = computed(() =>
  uninstalledOnly.value ? items.value.filter(m => !m.installed) : items.value)
const emptyText = computed(() => {
  if (uninstalledOnly.value) {
    return keyword.value.trim() ? '没有找到未安装的匹配资源' : '该分类资源均已安装'
  }
  return keyword.value.trim() ? '没有找到匹配的资源' : '该分类暂无资源'
})

let searchTimer: number | undefined

const keyOf = (m: MarketItem) => `${m.kind}:${m.id}`
const kindLabel = (k: string) => kinds.value.find(x => x.key === k)?.label || k

async function loadKinds() {
  try {
    const r = await api.market.kinds()
    kinds.value = r.kinds || []
    needsSync.value = !!r.needs_sync
  } catch (e) {
    toast.value = '市场分类加载失败：' + (e as Error).message
  }
}

async function loadItems() {
  loading.value = true
  try {
    const r = await api.market.items(
      activeKind.value === 'all' ? '' : activeKind.value,
      keyword.value.trim(),
    )
    items.value = r.items || []
  } catch (e) {
    toast.value = '资源加载失败：' + (e as Error).message
    items.value = []
  } finally {
    loading.value = false
  }
}

function selectKind(k: KindInfo) {
  if (activeKind.value === k.key) return
  activeKind.value = k.key
  void loadItems()
}

function onSearchInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => { void loadItems() }, 220)
}
function clearSearch() {
  keyword.value = ''
  void loadItems()
}

async function install(m: MarketItem) {
  const k = keyOf(m)
  if (busy.value) return
  busy.value = k
  try {
    const r = await api.market.install(m.kind, m.id)
    let tip = `已下载：${m.name}`
    if (r?.kind === 'canvas' && r?.created_agents?.length) {
      tip += `（同时创建智能体：${r.created_agents.join('、')}）`
    }
    if (r?.kind === 'extension' && r?.need_restart) {
      tip += '，含后端服务，重启后端后生效'
    }
    toast.value = tip
    await loadKinds()
    await loadItems()
    await refreshLocal(m.kind)
  } catch (e) {
    toast.value = '下载失败：' + (e as Error).message
  } finally {
    busy.value = ''
  }
}

async function refreshLocal(kind: string) {
  try {
    if (kind === 'agent') await agents.fetch()
    else if (kind === 'canvas') await canvases.fetch()
    else if (kind === 'extension') await loadExtensions(true)
    // 技能/插件：后端安装时已做非阻塞热重载，这里不再触发（避免同步安装依赖阻塞 UI）
  } catch {
    /* 局部刷新失败不阻断下载结果 */
  }
}

function installDisabled(m: MarketItem) {
  if (busy.value === keyOf(m)) return true
  return !!m.installed && LOCAL_KINDS.includes(m.kind)
}

function installText(m: MarketItem) {
  if (busy.value === keyOf(m)) return '下载中…'
  if (m.installed && LOCAL_KINDS.includes(m.kind)) return '已安装'
  return '下载'
}

// —— 远程仓库：同步下载 / 推送上传 / 镜像设置 ——
async function syncRemote() {
  if (syncing.value) return
  syncing.value = true
  toast.value = '正在从大陆镜像同步市场，请稍候…'
  try {
    const r = await api.market.sync(true)
    if (r?.up_to_date) {
      toast.value = '市场已是最新'
    } else {
      const src = r?.mirror || '官方'
      toast.value = `同步完成：新增 ${r?.added ?? 0} 项，更新 ${r?.changed ?? 0} 项（来源：${src}）`
    }
    needsSync.value = false
    await loadKinds()
    await loadItems()
  } catch (e) {
    toast.value = '同步失败：' + (e as Error).message
  } finally {
    syncing.value = false
  }
}

async function pushRemote() {
  if (pushing.value) return
  pushing.value = true
  toast.value = '正在提交并推送到远程仓库，请稍候…'
  try {
    const r = await api.market.push()
    toast.value = r?.message || '已推送到远程市场仓库'
  } catch (e) {
    toast.value = '推送失败：' + (e as Error).message
  } finally {
    pushing.value = false
  }
}

const settingsOpen = ref(false)
const cfgRepo = ref('')
const cfgBranch = ref('main')
const cfgMirrors = ref('')
const cfgState = ref<any>({})
const savingCfg = ref(false)
const loadingCfg = ref(false)

async function openSettings() {
  settingsOpen.value = true
  loadingCfg.value = true
  try {
    const r = await api.market.config()
    cfgRepo.value = r.repo_url || ''
    cfgBranch.value = r.branch || 'main'
    cfgMirrors.value = (r.mirrors || []).join('\n')
    cfgState.value = r.state || {}
    if (!cfgMirrors.value) {
      cfgMirrors.value = 'https://ghfast.top/{url}\nhttps://gh-proxy.com/{url}'
    }
  } catch (e) {
    toast.value = '读取仓库配置失败：' + (e as Error).message
  } finally {
    loadingCfg.value = false
  }
}
function closeSettings() {
  settingsOpen.value = false
  savingCfg.value = false
}
async function saveSettings() {
  if (savingCfg.value) return
  savingCfg.value = true
  try {
    const r = await api.market.saveConfig({
      repo_url: cfgRepo.value.trim(),
      branch: cfgBranch.value.trim(),
      mirrors: cfgMirrors.value.split('\n').map(s => s.trim()).filter(Boolean),
    })
    cfgState.value = (await api.market.config()).state || cfgState.value
    toast.value = '已保存市场仓库配置' + (r?.config?.mirrors?.length ? `（${r.config.mirrors.length} 个镜像源）` : '')
    settingsOpen.value = false
  } catch (e) {
    toast.value = '保存失败：' + (e as Error).message
  } finally {
    savingCfg.value = false
  }
}

// —— 上传（把本机资源发布到市场）——
const uploadOpen = ref(false)
const uploadKind = ref('agent')
const publishable = ref<Array<{ id: string; name: string; description?: string }>>([])
const selectedId = ref('')
const publishing = ref(false)
const pushAfterPublish = ref(true)
const uploadKinds = computed(() => kinds.value.filter(k => k.key !== 'all'))

async function openUpload() {
  uploadOpen.value = true
  selectedId.value = ''
  await loadPublishable()
}
function closeUpload() {
  uploadOpen.value = false
  publishing.value = false
}
function switchUploadKind(k: string) {
  uploadKind.value = k
  selectedId.value = ''
  void loadPublishable()
}
async function loadPublishable() {
  publishable.value = []
  try {
    const r = await api.market.publishable(uploadKind.value)
    publishable.value = r.items || []
  } catch (e) {
    toast.value = '读取本机资源失败：' + (e as Error).message
  }
}
async function doPublish() {
  if (!selectedId.value || publishing.value) return
  publishing.value = true
  try {
    const r = await api.market.publish(uploadKind.value, selectedId.value, pushAfterPublish.value)
    const name = r?.item?.name || selectedId.value
    let tip = `已发布到市场：${name}`
    if (r?.push_error) tip += `（已写入本地，推送失败：${r.push_error}）`
    else if (r?.push) tip += '，已推送到远程仓库'
    toast.value = tip
    uploadOpen.value = false
    await loadKinds()
    if (activeKind.value === 'all' || activeKind.value === uploadKind.value) await loadItems()
  } catch (e) {
    toast.value = '发布失败：' + (e as Error).message
  } finally {
    publishing.value = false
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (settingsOpen.value) closeSettings()
  else if (uploadOpen.value) closeUpload()
}

function iconStyle(m: MarketItem) {
  if (m.kind === 'background' && m.preview) return { background: m.preview }
  return {}
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  await loadKinds()
  await loadItems()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  if (searchTimer) window.clearTimeout(searchTimer)
})
</script>

<template>
  <div class="mk-root">
    <div class="mk-top glass">
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <div class="top-drag pywebview-drag-region">
        <h2>🛒 市场</h2>
        <span class="sub">下载走大陆镜像，上传推送远程仓库</span>
      </div>
      <div class="mk-actions">
        <button class="btn ghost" :disabled="syncing" @click="syncRemote">
          {{ syncing ? '同步中…' : '同步最新' }}
        </button>
        <button class="btn ghost" :disabled="pushing" @click="pushRemote">
          {{ pushing ? '推送中…' : '推送远程' }}
        </button>
        <button class="btn ghost" @click="openSettings">设置</button>
        <button class="btn primary upload-btn" @click="openUpload">上传资源</button>
      </div>
    </div>

    <div class="mk-main">
      <aside class="mk-kinds glass">
        <div class="kinds-title">资源分类</div>
        <button
          v-for="k in kinds" :key="k.key"
          class="kind-item" :class="{ active: activeKind === k.key }"
          @click="selectKind(k)"
        >
          <span class="kind-name">{{ k.key === 'all' ? '🔥 推荐' : k.label }}</span>
          <span class="kind-count">{{ k.count }}</span>
        </button>
      </aside>

      <section class="mk-content">
        <div class="search-row">
          <div class="search-box glass">
            <span class="search-ico">🔍</span>
            <input
              v-model="keyword" class="search-input" :placeholder="searchPlaceholder"
              @input="onSearchInput"
            />
            <button v-if="keyword" class="search-clear" title="清除搜索" @click="clearSearch">✕</button>
          </div>
          <label class="filter-check" title="勾选后隐藏本机已安装的资源">
            <input v-model="uninstalledOnly" type="checkbox" />
            <span>未安装</span>
          </label>
          <span class="search-scope">{{ searchScope }}</span>
        </div>

        <div v-if="needsSync" class="sync-hint glass">
          <span>市场资源尚未下载到本机。使用大陆镜像一键同步最新资源：</span>
          <button class="btn primary" :disabled="syncing" @click="syncRemote">
            {{ syncing ? '同步中…' : '从镜像同步' }}
          </button>
        </div>

        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!visibleItems.length" class="empty">{{ emptyText }}</div>
        <div v-else class="card-grid">
          <div v-for="m in visibleItems" :key="keyOf(m)" class="mk-card glass">
            <div class="mc-head">
              <span class="mc-icon" :style="iconStyle(m)">{{ m.icon || KIND_ICON[m.kind] || '📦' }}</span>
              <div class="mc-title-wrap">
                <div class="mc-title">{{ m.name }}</div>
                <div class="mc-sub">
                  {{ kindLabel(m.kind) }}<template v-if="m.version"> · v{{ m.version }}</template>
                  <template v-if="m.kind === 'canvas' && m.agent_count"> · 含 {{ m.agent_count }} 个智能体</template>
                  <template v-if="m.author"> · {{ m.author }}</template>
                </div>
              </div>
              <span v-if="m.installed" class="mc-badge">已安装</span>
            </div>
            <div class="mc-desc">{{ m.description || '暂无描述' }}</div>
            <div class="mc-tags">
              <span v-for="t in (m.tags || []).slice(0, 4)" :key="t" class="tag">#{{ t }}</span>
            </div>
            <div class="mc-actions">
              <button
                class="act primary" :disabled="installDisabled(m)"
                @click="install(m)"
              >{{ installText(m) }}</button>
            </div>
          </div>
        </div>
      </section>
    </div>

    <Teleport to="body">
      <div v-if="uploadOpen" class="mk-modal-mask" @click.self="closeUpload">
        <div class="mk-modal">
          <div class="mm-head">
            <span class="mm-title">⬆ 上传资源到市场</span>
            <button class="mm-close" @click="closeUpload">✕</button>
          </div>
          <div class="mm-body">
            <div class="mm-tabs">
              <button
                v-for="k in uploadKinds" :key="k.key"
                class="mm-tab" :class="{ active: uploadKind === k.key }"
                @click="switchUploadKind(k.key)"
              >{{ k.label }}</button>
            </div>
            <div class="mm-hint">从本机已有的{{ kindLabel(uploadKind) }}中选择一项发布到市场。</div>
            <div v-if="!publishable.length" class="mm-empty">本机暂无{{ kindLabel(uploadKind) }}可发布</div>
            <div v-else class="mm-list">
              <label v-for="p in publishable" :key="p.id" class="mm-item" :class="{ checked: selectedId === p.id }">
                <input v-model="selectedId" type="radio" :value="p.id" />
                <div class="mm-item-body">
                  <div class="mm-item-name">{{ p.name }}</div>
                  <div class="mm-item-desc">{{ p.description || '暂无描述' }}</div>
                </div>
              </label>
            </div>
          </div>
          <div class="mm-foot">
            <label class="mm-push-check">
              <input v-model="pushAfterPublish" type="checkbox" />
              <span>发布后推送到远程仓库</span>
            </label>
            <button class="btn ghost" @click="closeUpload">取消</button>
            <button class="btn primary" :disabled="!selectedId || publishing" @click="doPublish">
              {{ publishing ? '发布中…' : '发布到市场' }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="settingsOpen" class="mk-modal-mask" @click.self="closeSettings">
        <div class="mk-modal">
          <div class="mm-head">
            <span class="mm-title">⚙ 市场远程仓库设置</span>
            <button class="mm-close" @click="closeSettings">✕</button>
          </div>
          <div class="mm-body">
            <div v-if="loadingCfg" class="mm-empty">加载中…</div>
            <template v-else>
              <label class="mm-field">
                <span class="mm-label">仓库地址（上传推送目标）</span>
                <input v-model="cfgRepo" class="mm-input" spellcheck="false"
                  placeholder="https://github.com/Windy-X211/AgentCluster-Market.git" />
              </label>
              <label class="mm-field">
                <span class="mm-label">分支</span>
                <input v-model="cfgBranch" class="mm-input" spellcheck="false" placeholder="main" />
              </label>
              <label class="mm-field">
                <span class="mm-label">镜像源（每行一个，<code>{url}</code> 为占位符；末尾自动回退官方地址）</span>
                <textarea v-model="cfgMirrors" class="mm-input mm-textarea" spellcheck="false" rows="3"
                  placeholder="https://ghfast.top/{url}"></textarea>
              </label>
              <div class="mm-state">
                本地仓库：{{ cfgState.is_repo ? (cfgState.dirty ? `有 ${cfgState.dirty_files} 个未提交改动` : '已初始化') : '未初始化（首次推送时自动创建）' }}
                <template v-if="cfgState.branch"> · 分支 {{ cfgState.branch }}</template>
                <template v-if="cfgState.ahead"> · 领先 {{ cfgState.ahead }} 个提交</template>
              </div>
              <div class="mm-tip">下载（同步）走镜像加速；上传（推送）需要访问官方仓库地址，镜像一般只读、不支持 push。</div>
            </template>
          </div>
          <div class="mm-foot">
            <button class="btn ghost" @click="closeSettings">取消</button>
            <button class="btn primary" :disabled="loadingCfg || savingCfg" @click="saveSettings">
              {{ savingCfg ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="toast" class="mk-toast" @click="toast = ''">{{ toast }}</div>
    </Teleport>
  </div>
</template>

<style scoped>
.mk-root {
  padding: 10px 16px 16px; height: calc(100vh - var(--titlebar-h));
  display: flex; flex-direction: column; min-height: 0; overflow: hidden;
}
.mk-top {
  padding: 8px 16px; margin-bottom: 10px; flex-shrink: 0;
  display: flex; align-items: center; gap: 16px;
  h2 { font-size: calc(18px * var(--font-scale)); }
}
.top-drag { flex: 1; align-self: stretch; display: flex; align-items: center; gap: 12px; user-select: none; }
.sub { font-size: calc(12px * var(--font-scale)); opacity: .5; }
.upload-btn { flex-shrink: 0; background: rgba(var(--accent-rgb, 120, 140, 255), .28); border-color: rgba(var(--accent-rgb, 120, 140, 255), .5); }
.mk-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.mk-actions .btn { padding: 5px 12px; font-size: calc(12px * var(--font-scale)); white-space: nowrap; }
.sync-hint {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  border-radius: 10px; font-size: calc(12px * var(--font-scale)); flex-shrink: 0;
}
.sync-hint span { flex: 1; opacity: .75; }

.mk-main { flex: 1; min-height: 0; display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 12px; }

.mk-kinds { padding: 10px; display: flex; flex-direction: column; gap: 4px; overflow: auto; }
.kinds-title { font-size: calc(12px * var(--font-scale)); opacity: .5; padding: 4px 8px 8px; }
.kind-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 10px; border: none; border-radius: 8px; background: transparent;
  color: inherit; cursor: pointer; font-size: calc(13px * var(--font-scale));
  transition: background .15s, color .15s;
}
.kind-item:hover { background: rgba(255, 255, 255, .06); }
.kind-item.active { background: rgba(var(--accent-rgb, 120, 140, 255), .2); color: var(--accent-light, #9db4ff); }
.kind-count { font-size: 11px; opacity: .55; }

.mk-content { min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 10px; overflow: auto; }
.search-row { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.search-box {
  flex: 1; display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 10px;
}
.search-ico { font-size: 13px; opacity: .6; }
.search-input {
  flex: 1; min-width: 0; border: none; background: transparent; color: inherit;
  font-size: calc(13px * var(--font-scale)); outline: none; padding: 4px 0;
}
.search-clear {
  border: none; background: transparent; color: inherit; cursor: pointer;
  opacity: .5; font-size: 12px; padding: 2px 4px;
}
.search-clear:hover { opacity: 1; }
.filter-check {
  display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0;
  font-size: calc(12px * var(--font-scale)); opacity: .8; cursor: pointer; user-select: none;
  padding: 6px 10px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, .1);
}
.filter-check:hover { background: rgba(255, 255, 255, .05); }
.filter-check input { cursor: pointer; accent-color: rgb(var(--accent-rgb, 120, 140, 255)); }
.search-scope { font-size: calc(11px * var(--font-scale)); opacity: .45; flex-shrink: 0; }

.empty { text-align: center; opacity: .5; padding: 48px 0; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
.mk-card { padding: 12px 14px; border-radius: 10px; display: flex; flex-direction: column; gap: 8px; }
.mc-head { display: flex; align-items: flex-start; gap: 10px; }
.mc-icon {
  width: 38px; height: 38px; border-radius: 9px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 20px; background: rgba(255, 255, 255, .06);
  background-size: cover; background-position: center;
}
.mc-title-wrap { flex: 1; min-width: 0; }
.mc-title { font-weight: 600; font-size: calc(14px * var(--font-scale)); }
.mc-sub { font-size: calc(11px * var(--font-scale)); opacity: .5; margin-top: 2px; }
.mc-badge { font-size: 10px; padding: 2px 7px; border-radius: 8px; background: rgba(111, 207, 151, .14); color: #6fcf97; flex-shrink: 0; }
.mc-desc {
  font-size: calc(12px * var(--font-scale)); opacity: .65; line-height: 1.5; min-height: 34px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.mc-tags { display: flex; flex-wrap: wrap; gap: 5px; min-height: 18px; }
.tag { font-size: 10px; padding: 1px 7px; border-radius: 8px; background: rgba(255, 255, 255, .06); color: rgba(255, 255, 255, .55); }
.mc-actions { display: flex; justify-content: flex-end; }
.act { padding: 5px 16px; border-radius: 7px; border: 1px solid rgba(255, 255, 255, .15); background: transparent; color: inherit; cursor: pointer; font-size: 12px; transition: all .15s; }
.act:hover { background: rgba(255, 255, 255, .08); }
.act:disabled { opacity: .45; cursor: not-allowed; }
.act.primary { background: rgba(var(--accent-rgb, 120, 140, 255), .2); border-color: rgba(var(--accent-rgb, 120, 140, 255), .5); color: var(--accent-light, #9db4ff); }
.act.primary:hover:not(:disabled) { background: rgba(var(--accent-rgb, 120, 140, 255), .3); }

.mk-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); z-index: 10000; padding: 8px 18px; border-radius: 8px; background: rgba(20, 25, 40, .95); color: #fff; font-size: 13px; box-shadow: 0 4px 16px rgba(0, 0, 0, .4); cursor: pointer; }

/* —— 上传弹窗 —— */
.mk-modal-mask { position: fixed; inset: 0; z-index: 9999; background: rgba(6, 10, 20, .62); display: flex; align-items: center; justify-content: center; }
.mk-modal {
  width: min(680px, 92vw); max-height: 78vh; display: flex; flex-direction: column;
  background: #0f1626; border: 1px solid rgba(255, 255, 255, .14);
  border-radius: 12px; overflow: hidden; box-shadow: 0 18px 48px rgba(0, 0, 0, .5);
}
.mm-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid rgba(255, 255, 255, .09); flex-shrink: 0; }
.mm-title { flex: 1; font-weight: 600; font-size: 13px; }
.mm-close { width: 26px; height: 26px; border-radius: 6px; cursor: pointer; border: 1px solid rgba(255, 255, 255, .16); background: transparent; color: inherit; font-size: 13px; line-height: 1; }
.mm-close:hover { background: rgba(255, 255, 255, .08); }
.mm-body { flex: 1; min-height: 0; overflow: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.mm-tabs { display: flex; flex-wrap: wrap; gap: 6px; }
.mm-tab { padding: 5px 12px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, .12); background: transparent; color: inherit; cursor: pointer; font-size: 12px; }
.mm-tab:hover { background: rgba(255, 255, 255, .06); }
.mm-tab.active { background: rgba(var(--accent-rgb, 120, 140, 255), .2); border-color: rgba(var(--accent-rgb, 120, 140, 255), .5); color: var(--accent-light, #9db4ff); }
.mm-hint { font-size: 12px; opacity: .55; }
.mm-empty { text-align: center; opacity: .5; padding: 30px 0; font-size: 13px; }
.mm-list { display: flex; flex-direction: column; gap: 6px; }
.mm-item {
  display: flex; align-items: flex-start; gap: 10px; padding: 9px 11px;
  border: 1px solid rgba(255, 255, 255, .1); border-radius: 8px; cursor: pointer;
}
.mm-item:hover { background: rgba(255, 255, 255, .05); }
.mm-item.checked { border-color: rgba(var(--accent-rgb, 120, 140, 255), .55); background: rgba(var(--accent-rgb, 120, 140, 255), .12); }
.mm-item input { margin-top: 3px; }
.mm-item-body { min-width: 0; }
.mm-item-name { font-size: 13px; font-weight: 600; }
.mm-item-desc { font-size: 11px; opacity: .55; margin-top: 2px; }
.mm-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 14px; border-top: 1px solid rgba(255, 255, 255, .09); flex-shrink: 0; }
.mm-push-check {
  margin-right: auto; display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(12px * var(--font-scale)); opacity: .75; cursor: pointer; user-select: none;
}
.mm-push-check input { cursor: pointer; accent-color: rgb(var(--accent-rgb, 120, 140, 255)); }
.mm-field { display: flex; flex-direction: column; gap: 4px; }
.mm-label { font-size: 12px; opacity: .6; }
.mm-label code { font-size: 11px; opacity: .9; }
.mm-input {
  padding: 7px 10px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, .14);
  background: rgba(255, 255, 255, .04); color: inherit; font-size: 12px;
  font-family: inherit; outline: none;
}
.mm-input:focus { border-color: rgba(var(--accent-rgb, 120, 140, 255), .55); }
.mm-textarea { resize: vertical; min-height: 64px; line-height: 1.5; }
.mm-state { font-size: 11px; opacity: .55; }
.mm-tip { font-size: 11px; opacity: .45; line-height: 1.5; }
</style>