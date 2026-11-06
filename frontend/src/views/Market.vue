<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/api'
import { loadExtensions } from '@/extensions/loader'
import { useAgentStore, useCanvasStore } from '@/stores'

interface KindInfo { key: string; label: string; count: number }
interface MarketItem {
  kind: string; id: string; name: string; description?: string
  version?: string; author?: string; icon?: string; tags?: string[]
  installed?: boolean; enabled?: boolean; preview?: string; bg_type?: string; agent_count?: number
  // 我的发布：false=已发布到本地市场但未推送到远程；null/undefined=无法判断
  pushed?: boolean | null
}

const KIND_ICON: Record<string, string> = {
  agent: '🤖', canvas: '🗺', background: '🎨', skill: '🧠', plugin: '🔧', extension: '🧩',
}
const EXECUTION_MODE_LABEL: Record<string, string> = {
  serial: '串行', parallel: '并行', hybrid: '混合',
}
const LOCAL_KINDS = ['skill', 'plugin', 'background', 'extension']

const kinds = ref<KindInfo[]>([])
const mineCount = ref(0)
const activeKind = ref('all')
const keyword = ref('')
const items = ref<MarketItem[]>([])
const uninstalledOnly = ref(false)
const loading = ref(false)
const busy = ref('')
let toastTimer: number | undefined
function useAutoToast() {
  const _toast = ref('')
  const toast = computed({
    get: () => _toast.value,
    set: (v: string) => {
      _toast.value = v
      if (toastTimer) window.clearTimeout(toastTimer)
      if (v) toastTimer = window.setTimeout(() => { _toast.value = '' }, 3000)
    },
  })
  return toast
}
const toast = useAutoToast()
const needsSync = ref(false)
const syncing = ref(false)
const pushing = ref(false)
const auth = ref<any>({ logged_in: false })
const authLoading = ref(false)
const cacheInfo = ref<any>(null)
const cleaningCache = ref(false)
const loginOpen = ref(false)
const loginUsername = ref('')
const loginToken = ref('')
const loginError = ref('')
const loggingIn = ref(false)

const agents = useAgentStore()
const canvases = useCanvasStore()

const activeLabel = computed(() => {
  if (activeKind.value === 'mine') return '📤 我的发布'
  return kinds.value.find(k => k.key === activeKind.value)?.label || '推荐'
})
const isMineView = computed(() => activeKind.value === 'mine')
// kinds 渲染用：把后端返回的 kinds 前面拼上"📤 我的发布"
const allKinds = computed(() => [
  { key: 'mine', label: '📤 我的发布', count: mineCount.value } as KindInfo & { mine?: boolean },
  ...kinds.value,
])
const searchPlaceholder = computed(() =>
  activeKind.value === 'all' ? '在全部资源中搜索…' : `只搜索${activeLabel.value}…`)
const searchScope = computed(() =>
  activeKind.value === 'all' ? '推荐 · 搜索全部资源' : `搜索范围：${activeLabel.value}`)
const cacheTotalSize = computed(() => {
  const c = cacheInfo.value
  if (!c) return 0
  return (c.resource_bytes || 0) + (c.zip_cache_bytes || 0)
})
const cacheIndicatorText = computed(() => formatSize(cacheTotalSize.value))
const cacheIndicatorTitle = computed(() => {
  const c = cacheInfo.value
  if (!c) return ''
  const parts = [`资源：${c.resource_human || '0 B'}`]
  if (c.has_git) parts.push(`git：${c.git_human}`)

  parts.push('清理不影响已安装到本地的用户数据')
  return parts.join('\n')
})
// 未安装过滤：勾选后只展示本地尚未安装的资源（安装状态由后端随列表返回）
const visibleItems = computed(() =>
  uninstalledOnly.value ? items.value.filter(m => !m.installed) : items.value)
const emptyText = computed(() => {
  if (isMineView.value) {
    if (!auth.value?.logged_in) return '请先登录 GitHub 账号查看你的发布'
    return keyword.value.trim() ? '没有找到你发布的匹配资源' : '你还没有发布过资源，试试上传一个吧'
  }
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
    if (isMineView.value) {
      const r = await api.market.myItems(keyword.value.trim())
      items.value = r.items || []
    } else {
      const r = await api.market.items(
        activeKind.value === 'all' ? '' : activeKind.value,
        keyword.value.trim(),
      )
      items.value = r.items || []
    }
  } catch (e) {
    toast.value = '资源加载失败：' + (e as Error).message
    items.value = []
  } finally {
    loading.value = false
  }
}

async function loadMineCount() {
  try {
    const r = await api.market.myItems()
    mineCount.value = r.total || 0
  } catch {
    mineCount.value = 0
  }
}

function selectKind(k: KindInfo) {
  if (activeKind.value === k.key) return
  activeKind.value = k.key
  uninstalledOnly.value = false  // mine 视图下没意义，清掉
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

async function install(m: MarketItem, version?: string) {
  const k = keyOf(m)
  if (busy.value) return
  busy.value = k
  try {
    const r = await api.market.install(m.kind, m.id, version)
    let tip = `已下载：${m.name}${version && version !== m.version ? ` v${version}` : ''}`
    if (r?.kind === 'canvas' && r?.created_agents?.length) {
      tip += `（创建智能体：${r.created_agents.join('、')}）`
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
async function loadAuth() {
  authLoading.value = true
  try {
    auth.value = await api.market.auth()
  } catch {
    /* 未登录或网络异常时保持未登录态 */
  } finally {
    authLoading.value = false
  }
}

function openLogin() {
  loginOpen.value = true
  loginError.value = ''
  loginUsername.value = auth.value?.logged_in ? auth.value.username : ''
  loginToken.value = ''
}

function closeLogin() {
  loginOpen.value = false
  loginToken.value = ''
  loginError.value = ''
  loggingIn.value = false
}

/** 打开 GitHub Token 页面：window.open 会被 Electron 的 setWindowOpenHandler 转交给系统浏览器 */
function openGitHubTokens() {
  window.open('https://github.com/settings/tokens', '_blank', 'noopener')
}

async function doLogin() {
  const username = loginUsername.value.trim()
  const token = loginToken.value.trim()
  if (!username) { loginError.value = '请填写 GitHub 用户名'; return }
  if (!token) { loginError.value = '请填写 Personal Access Token'; return }
  loggingIn.value = true
  loginError.value = ''
  try {
    auth.value = await api.market.login(username, token)
    loginToken.value = ''
    loginOpen.value = false
    toast.value = `已登录 GitHub：@${auth.value.username}`
  } catch (e) {
    loginError.value = (e as Error).message
  } finally {
    loggingIn.value = false
  }
}

async function doLogout() {
  try {
    await api.market.logout()
    auth.value = { logged_in: false }
    loginOpen.value = false
    toast.value = '已退出 GitHub 账号'
  } catch (e) {
    toast.value = '退出失败：' + (e as Error).message
  }
}

async function syncRemote(silent: boolean) {
  if (syncing.value) return
  syncing.value = true
  if (!silent) toast.value = '正在从大陆镜像同步市场，请稍候…'
  try {
    const r = await api.market.sync(true)
    if (!silent) {
      if (r?.up_to_date) {
        toast.value = '市场已是最新'
      } else {
        const src = r?.mirror || '官方'
        toast.value = `同步完成：新增 ${r?.added ?? 0} 项，更新 ${r?.changed ?? 0} 项（来源：${src}）`
      }
    }
    needsSync.value = false
    await loadKinds()
    await loadItems()
  } catch (e) {
    if (!silent) toast.value = '同步失败：' + (e as Error).message
  } finally {
    syncing.value = false
  }
}

// 手动「同步最新」：显示进度与结果提示
function manualSync() { void syncRemote(false) }
// 打开市场时自动同步：静默执行，不弹提示（同步中按钮自会显示"同步中…"）
function autoSyncOnOpen() { void syncRemote(true) }

async function pushRemote() {
  if (pushing.value) return
  if (!auth.value?.logged_in) {
    toast.value = '请先登录 GitHub 账号再推送'
    openLogin()
    return
  }
  pushing.value = true
  toast.value = '正在使用你的 GitHub 账号推送到远程仓库，请稍候…'
  try {
    const r = await api.market.push()
    toast.value = r?.message || '已推送到远程市场仓库'
    // 推送后所有条目的「未推送」状态都可能变化，刷新当前列表
    await loadKinds()
    await loadItems()
  } catch (e) {
    toast.value = '推送失败：' + (e as Error).message
  } finally {
    pushing.value = false
  }
}

async function fetchCacheInfo() {
  try {
    cacheInfo.value = await api.market.cache.overview()
  } catch {
    cacheInfo.value = null
  }
}

async function cleanCache() {
  if (cleaningCache.value) return
  cleaningCache.value = true
  try {
    const r = await api.market.cache.clean({ preserve_index: true })
    toast.value = `已清理市场缓存，释放 ${r?.freed_human || '空间'}`
    cacheInfo.value = r?.overview_after || null
  } catch (e) {
    toast.value = '清理失败：' + (e as Error).message
  } finally {
    cleaningCache.value = false
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
const publishable = ref<Array<{ id: string; name: string; description?: string; published?: boolean }>>([])
const uploadLoading = ref(false)
// 当前列表数据实际所属的资源类型（过渡 key 用它，避免切换瞬间用新 key 重渲染旧数据造成重复闪动）
const loadedKind = ref('')
const selectedId = ref('')
// 上传弹窗：只看未上传（未发布到市场）的本机资源
const unPublishedOnly = ref(false)
const visiblePublishable = computed(() =>
  unPublishedOnly.value ? publishable.value.filter(p => !p.published) : publishable.value)
// 勾选「未上传」或列表刷新后，若选中项已被过滤掉则清空选择，避免按钮指向不可见项
watch(visiblePublishable, list => {
  if (selectedId.value && !list.some(p => p.id === selectedId.value)) selectedId.value = ''
})
const publishing = ref(false)
const canPush = computed(() => !!auth.value?.logged_in)
const uploadKinds = computed(() => kinds.value.filter(k => k.key !== 'all'))
// 请求序号：快速连续切换类型时丢弃过期响应，避免旧类型数据覆盖新类型
let uploadReqSeq = 0

async function openUpload() {
  uploadOpen.value = true
  selectedId.value = ''
  publishable.value = []
  loadedKind.value = ''
  await loadPublishable()
}
function closeUpload() {
  uploadOpen.value = false
  publishing.value = false
  uploadReqSeq++ // 关闭后作废在途请求
}
function switchUploadKind(k: string) {
  if (uploadKind.value === k) return
  uploadKind.value = k
  selectedId.value = ''
  void loadPublishable()
}
async function loadPublishable() {
  const seq = ++uploadReqSeq
  const kind = uploadKind.value
  // 不清空旧列表：切换类型时旧内容原地淡出/新内容淡入，避免列表闪空与弹窗高度跳动
  uploadLoading.value = true
  try {
    const r = await api.market.publishable(kind)
    if (seq !== uploadReqSeq) return
    publishable.value = r.items || []
    loadedKind.value = kind
  } catch (e) {
    if (seq !== uploadReqSeq) return
    publishable.value = []
    loadedKind.value = ''
    toast.value = '读取本机资源失败：' + (e as Error).message
  } finally {
    if (seq === uploadReqSeq) uploadLoading.value = false
  }
}
async function doPublish() {
  if (!selectedId.value || publishing.value) return
  publishing.value = true
  const willPush = canPush.value  // 登录就自动推，无感
  try {
    const r = await api.market.publish(uploadKind.value, selectedId.value, willPush)
    const name = r?.item?.name || selectedId.value
    let tip = `已发布：${name}`
    if (r?.push) tip += '（已同步到远程市场）'
    else if (r?.push_error) tip += `（远程同步稍后重试：${r.push_error.split('\n')[0].slice(0, 60)}）`
    else if (!canPush.value) tip += '（未登录，仅发布到本地市场）'
    toast.value = tip
    uploadOpen.value = false
    await loadKinds()
    await loadMineCount()
    if (activeKind.value === 'all' || activeKind.value === uploadKind.value || isMineView.value) await loadItems()
  } catch (e) {
    toast.value = '发布失败：' + (e as Error).message
  } finally {
    publishing.value = false
  }
}

// —— 下架（删除自己发布的市场资源）——
const unpublishOpen = ref(false)
const unpublishing = ref(false)
const unpublishItem = ref<MarketItem | null>(null)

function openUnpublish(m: MarketItem) {
  unpublishItem.value = m
  unpublishOpen.value = true
}
function closeUnpublish() {
  unpublishOpen.value = false
  unpublishing.value = false
  unpublishItem.value = null
}
async function doUnpublish() {
  if (!unpublishItem.value || unpublishing.value) return
  unpublishing.value = true
  const m = unpublishItem.value
  const willPush = canPush.value  // 登录就自动推，无感
  try {
    const r = await api.market.deleteItem(m.kind, m.id, willPush)
    let tip = `已下架：${m.name}`
    if (r?.push) tip += '（已同步到远程）'
    else if (r?.push_error) tip += `（远程稍后同步：${r.push_error.split('\n')[0].slice(0, 60)}）`
    else if (!canPush.value) tip += '（未登录，仅下架本地市场）'
    toast.value = tip
    unpublishOpen.value = false
    await loadKinds()
    await loadMineCount()
    if (isMineView.value) await loadItems()
  } catch (e) {
    toast.value = '下架失败：' + (e as Error).message
  } finally {
    unpublishing.value = false
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (canvasZoomOpen.value) closeCanvasZoom()
  else if (previewOpen.value) closePreview()
  else if (loginOpen.value) closeLogin()
  else if (settingsOpen.value) closeSettings()
  else if (unpublishOpen.value) closeUnpublish()
  else if (uploadOpen.value) closeUpload()
}

function iconStyle(_m: MarketItem) {
  // 预览图已在模板内用 <img> 渲染，这里仅保留空样式占位
  return {}
}

function isPreviewImage(preview: string): boolean {
  if (!preview) return false
  const lower = preview.toLowerCase()
  return lower.startsWith('http') || lower.startsWith('data:image') || lower.endsWith('.png') || lower.endsWith('.jpg') || lower.endsWith('.jpeg') || lower.endsWith('.gif') || lower.endsWith('.webp') || lower.endsWith('.svg')
}

// —— 资源详情预览弹窗 ——
const previewOpen = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewData = ref<any>(null)
const previewVersions = ref<any[]>([])
const previewSelectedVersion = ref<string>('')

async function openPreview(m: MarketItem) {
  previewOpen.value = true
  previewLoading.value = true
  previewError.value = ''
  previewData.value = null
  previewVersions.value = []
  previewSelectedVersion.value = ''
  try {
    previewData.value = await api.market.preview(m.kind, m.id)
    // 拉版本历史（失败不阻塞预览）
    try {
      const vr = await api.market.itemVersions(m.kind, m.id)
      previewVersions.value = vr.versions || []
      previewSelectedVersion.value = vr.latest_version || ''
    } catch { /* 单版本资源没有历史，静默跳过 */ }
  } catch (e) {
    previewError.value = (e as Error).message
  } finally {
    previewLoading.value = false
    fetchCacheInfo()  // 预览触发按需拉取后刷新缓存显示
  }
}
function closePreview() {
  previewOpen.value = false
  previewData.value = null
  previewVersions.value = []
  previewError.value = ''
}

// —— 画布缩略图放大 ——
const canvasZoomOpen = ref(false)
function openCanvasZoom() { canvasZoomOpen.value = true }
function closeCanvasZoom() { canvasZoomOpen.value = false }
function onCanvasZoomKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && canvasZoomOpen.value) closeCanvasZoom()
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

// —— 画布缩略图计算 ——
const THUMB_PADDING = 80
const THUMB_NODE_R = 36
const canvasThumbReady = computed(() => {
  const nodes = previewData.value?.preview?.canvas?.nodes_raw
  return Array.isArray(nodes) && nodes.length > 0
})
const canvasThumbBox = computed(() => {
  const nodes = previewData.value?.preview?.canvas?.nodes_raw || []
  if (!nodes.length) return '0 0 100 100'
  const xs = nodes.map((n: any) => Number(n.x) || 0)
  const ys = nodes.map((n: any) => Number(n.y) || 0)
  const minX = Math.min(...xs) - THUMB_PADDING - THUMB_NODE_R
  const minY = Math.min(...ys) - THUMB_PADDING - THUMB_NODE_R
  const w = Math.max(Math.max(...xs) - Math.min(...xs) + (THUMB_PADDING + THUMB_NODE_R) * 2, 200)
  const h = Math.max(Math.max(...ys) - Math.min(...ys) + (THUMB_PADDING + THUMB_NODE_R) * 2, 160)
  return `${minX} ${minY} ${w} ${h}`
})
// 将智能体名称按节点圆可用宽度分行：中文字约占1em宽，英文/数字约0.55em
function wrapNodeLabel(name: string, nodeR: number, fontSize: number, maxLines = 3): string[] {
  if (!name) return ['?']
  // 节点内可用宽度（减去边距）
  const availW = nodeR * 2 - 10
  // 估算每字符宽度：中文≈1em，ASCII≈0.55em
  const charWidth = (ch: string) => /[\u4e00-\u9fff]/.test(ch) ? fontSize : fontSize * 0.55
  const lines: string[] = []
  let cur = ''
  let curW = 0
  for (const ch of name) {
    const w = charWidth(ch)
    if (curW + w > availW && cur) {
      lines.push(cur)
      if (lines.length >= maxLines - 1) { cur = ch; curW = w; break }
      cur = ch; curW = w
    } else {
      cur += ch; curW += w
    }
  }
  if (cur) {
    if (lines.length >= maxLines) {
      // 已到最大行数，最后一行末尾加省略号
      lines[lines.length - 1] = lines[lines.length - 1].replace(/(\S)$/, '$1…')
    } else {
      lines.push(cur)
    }
  }
  // 超过最大行数时，末行截断加省略号
  if (lines.length > maxLines) {
    const last = lines[maxLines - 1]
    lines.length = maxLines
    lines[maxLines - 1] = last.replace(/(\S)$/, '$1…')
  }
  return lines
}

const canvasThumbNodes = computed(() => {
  const nodes = previewData.value?.preview?.canvas?.nodes_raw || []
  return nodes.map((n: any) => {
    const name = (n.agent_ref || '').trim()
    // 缩略图用 11px 字号，放大弹窗用 13px；分行逻辑按 11px 基准
    const lines = wrapNodeLabel(name, THUMB_NODE_R, 11, 3)
    return {
      id: n.id,
      cx: Number(n.x) || 0,
      cy: Number(n.y) || 0,
      r: THUMB_NODE_R,
      lines,
      fullName: name,
    }
  })
})
const canvasThumbEdges = computed(() => {
  const canvas = previewData.value?.preview?.canvas
  const nodes: any[] = canvas?.nodes_raw || []
  const edges: any[] = canvas?.edges || []
  const nodeMap = new Map(nodes.map((n: any) => [n.id, n]))
  const nodeR = THUMB_NODE_R
  return edges.map((e: any) => {
    const from = nodeMap.get(e.from_node)
    const to = nodeMap.get(e.to_node)
    if (!from || !to) return { path: '' }
    const fx = Number(from.x) || 0, fy = Number(from.y) || 0
    const tx = Number(to.x) || 0, ty = Number(to.y) || 0
    // 方向向量，用于把终点从节点中心推到边缘
    const dx = tx - fx, dy = ty - fy
    const len = Math.hypot(dx, dy) || 1
    const ux = dx / len, uy = dy / len
    const sx = fx + ux * nodeR, sy = fy + uy * nodeR
    const ex = tx - ux * (nodeR + 6), ey = ty - uy * (nodeR + 6)
    // 中间控制点做平滑曲线
    const mx = (sx + ex) / 2
    const my = (sy + ey) / 2
    return { path: `M ${sx} ${sy} Q ${mx} ${sy} ${ex} ${ey}` }
  }).filter((e: any) => e.path)
})

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  await loadAuth()
  await loadKinds()
  await loadMineCount()
  await loadItems()
  fetchCacheInfo()  // 缓存概况非阻塞
  autoSyncOnOpen()  // 打开市场时静默自动同步最新（按钮显示"同步中…"，完成不弹提示）
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
        <button
          v-if="!auth.logged_in" class="btn ghost gh-login"
          :disabled="authLoading" @click="openLogin"
        >GitHub 登录</button>
        <button v-else class="btn ghost gh-login logged" title="GitHub 账号" @click="openLogin">
          <img v-if="auth.avatar_url" :src="auth.avatar_url" class="gh-avatar" alt="" />
          <span>@{{ auth.username }}</span>
        </button>
        <button class="btn ghost" :disabled="syncing" @click="manualSync">
          {{ syncing ? '同步中…' : '同步最新' }}
        </button>
        <span v-if="cacheInfo" class="cache-indicator" :title="cacheIndicatorTitle">
          💾 {{ cacheIndicatorText }}

          <button class="cache-clean-btn" @click="cleanCache" :disabled="cleaningCache"
            title="清理市场缓存（保留索引和用户数据）">
            {{ cleaningCache ? '清理中…' : '清理' }}
          </button>
        </span>
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
          v-for="k in allKinds" :key="k.key"
          class="kind-item"
          :class="{ active: activeKind === k.key, mine: k.key === 'mine' }"
          @click="selectKind(k)"
        >
          <span class="kind-name">{{ k.key === 'mine' ? '📤 我的发布' : (k.key === 'all' ? '🔥 推荐' : k.label) }}</span>
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
          <label v-if="!isMineView" class="filter-check" title="勾选后隐藏本机已安装的资源">
            <input v-model="uninstalledOnly" type="checkbox" />
            <span>未安装</span>
          </label>
          <span class="search-scope">{{ searchScope }}</span>
        </div>

        <div v-if="needsSync && !isMineView" class="sync-hint glass">
          <span>市场资源尚未下载到本机。使用大陆镜像一键同步最新资源：</span>
          <button class="btn primary" :disabled="syncing" @click="manualSync">
            {{ syncing ? '同步中…' : '从镜像同步' }}
          </button>
        </div>

        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!visibleItems.length" class="empty">{{ emptyText }}</div>
        <div v-else class="card-grid">
          <div v-for="m in visibleItems" :key="keyOf(m)" class="mk-card glass">
            <div class="mc-head">
              <span class="mc-icon" :style="iconStyle(m)">
                <img v-if="m.preview && isPreviewImage(m.preview)" :src="m.preview" class="mc-preview-img" alt="" />
                <span v-else class="mc-icon-emoji">{{ m.icon || KIND_ICON[m.kind] || '📦' }}</span>
              </span>
              <div class="mc-title-wrap">
                <div class="mc-title">{{ m.name }}</div>
                <div class="mc-sub">
                  {{ kindLabel(m.kind) }}<template v-if="m.version"> · v{{ m.version }}</template>
                  <template v-if="m.kind === 'canvas' && m.agent_count"> · 含 {{ m.agent_count }} 个智能体</template>
                </div>
              </div>
              <span v-if="m.installed" class="mc-badge">已安装</span>
              <span v-if="isMineView && m.pushed === false" class="mc-badge warn"
                title="已发布到本地市场，但还没有推送到远程仓库">未推送</span>
            </div>
            <div class="mc-desc">{{ m.description || '暂无描述' }}</div>
            <div class="mc-tags">
              <span v-for="t in (m.tags || []).slice(0, 4)" :key="t" class="tag">#{{ t }}</span>
            </div>
            <div class="mc-foot">
              <span v-if="m.author" class="mc-author" :title="'发布者：' + m.author">
                <span class="mc-author-avatar">👤</span>@{{ m.author }}
              </span>
              <div class="mc-actions">
                <button class="act ghost" @click="openPreview(m)">预览</button>
                <template v-if="isMineView">
                  <button class="act ghost danger" @click="openUnpublish(m)">下架</button>
                </template>
                <template v-else>
                  <button
                    class="act primary" :disabled="installDisabled(m)"
                    @click="install(m)"
                  >{{ installText(m) }}</button>
                </template>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <Teleport to="body">
      <div v-if="uploadOpen" class="mk-modal-mask">
        <div class="mk-modal mk-modal-upload">
          <div class="mm-head">
            <span class="mm-title">⬆ 上传资源到市场</span>
            <button class="mm-close" @click="closeUpload">✕</button>
          </div>
          <div class="mm-body mm-body-fixed">
            <div class="mm-tabs">
              <button
                v-for="k in uploadKinds" :key="k.key"
                class="mm-tab" :class="{ active: uploadKind === k.key }"
                @click="switchUploadKind(k.key)"
              >{{ k.label }}</button>
            </div>
            <div class="mm-hint-row">
              <div class="mm-hint">从本机已有的{{ kindLabel(uploadKind) }}中选择一项发布到市场。</div>
              <label class="mm-upload-filter" title="只显示尚未发布到市场的本机资源">
                <input v-model="unPublishedOnly" type="checkbox" />
                <span>未上传</span>
              </label>
            </div>
            <!-- 固定高度的列表区：内容多少不影响弹窗尺寸，切换类型时只做淡入淡出 -->
            <div class="mm-list-wrap" :class="{ 'is-loading': uploadLoading }">
              <Transition name="mm-fade" mode="out-in">
                <div v-if="uploadLoading && !publishable.length" key="loading" class="mm-empty">读取本机资源…</div>
                <div v-else-if="!visiblePublishable.length" key="empty" class="mm-empty">
                  {{ unPublishedOnly && publishable.length
                    ? `本机${kindLabel(uploadKind)}已全部上传`
                    : `本机暂无${kindLabel(uploadKind)}可发布` }}
                </div>
                <div v-else :key="'list-' + loadedKind" class="mm-list">
                  <label v-for="p in visiblePublishable" :key="p.id" class="mm-item" :class="{ checked: selectedId === p.id }">
                    <input v-model="selectedId" type="radio" :value="p.id" />
                    <div class="mm-item-body">
                      <div class="mm-item-name">
                        <span>{{ p.name }}</span>
                        <span v-if="p.published" class="mm-badge">已上传</span>
                      </div>
                      <div class="mm-item-desc">{{ p.description || '暂无描述' }}</div>
                    </div>
                  </label>
                </div>
              </Transition>
            </div>
          </div>
          <div class="mm-foot">
            <button class="btn ghost" @click="closeUpload">取消</button>
            <button class="btn primary" :disabled="!selectedId || publishing" @click="doPublish">
              {{ publishing ? '发布中…' : '发布到市场' }}
            </button>
          </div>
        </div>
      </div>
      <!-- 下架确认弹窗 -->
      <div v-if="unpublishOpen" class="mk-modal-mask">
        <div class="mk-modal mk-modal-sm">
          <div class="mm-head">
            <span class="mm-title">⚠ 下架市场资源</span>
            <button class="mm-close" @click="closeUnpublish">✕</button>
          </div>
          <div class="mm-body">
            <div v-if="unpublishItem" class="unpublish-confirm">
              <div class="unpublish-msg">
                确定要下架 <strong>{{ unpublishItem.name }}</strong> 吗？
              </div>
              <div class="mm-state">
                类型：{{ kindLabel(unpublishItem.kind) }} · ID：{{ unpublishItem.id }}
              </div>
              <div class="mm-tip">下架后将从本地市场索引和资源目录中移除，不会影响本机已安装的副本。</div>
            </div>
          </div>
          <div class="mm-foot">
            <button class="btn ghost" @click="closeUnpublish">取消</button>
            <button class="btn primary gh-danger" :disabled="unpublishing" @click="doUnpublish">
              {{ unpublishing ? '下架中…' : '确认下架' }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="settingsOpen" class="mk-modal-mask">
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
      <div v-if="loginOpen" class="mk-modal-mask">
        <div class="mk-modal mk-modal-sm">
          <div class="mm-head">
            <span class="mm-title">{{ auth.logged_in ? 'GitHub 账号' : 'GitHub 登录' }}</span>
            <button class="mm-close" @click="closeLogin">✕</button>
          </div>
          <div class="mm-body">
            <template v-if="auth.logged_in">
              <div class="gh-profile">
                <img v-if="auth.avatar_url" :src="auth.avatar_url" class="gh-avatar-lg" alt="" />
                <div>
                  <div class="gh-name">@{{ auth.username }}</div>
                  <div class="gh-sub">{{ auth.name || 'GitHub 账号' }}</div>
                </div>
              </div>
              <div class="mm-state">
                市场仓库写权限：
                <template v-if="auth.repo_permission?.checked">
                  <span :class="auth.repo_permission.can_push ? 'gh-ok' : 'gh-bad'">
                    {{ auth.repo_permission.can_push ? '可用' : '不可用' }}
                  </span>
                  <template v-if="auth.repo_permission.error"> · {{ auth.repo_permission.error }}</template>
                </template>
                <template v-else>未检查</template>
              </div>
              <div class="mm-tip">上传资源将使用该账号推送到远程市场仓库；Token 仅保存在本机，不会上传。</div>
            </template>
            <template v-else>
              <label class="mm-field">
                <span class="mm-label">GitHub 用户名</span>
                <input v-model="loginUsername" class="mm-input" spellcheck="false"
                  placeholder="例如 User123" autocomplete="username" />
              </label>
              <div v-if="loginUsername.trim()" class="gh-token-guide">
                <div class="gh-token-guide-head">
                  <span>已填写用户名，接下来按步骤创建 Token</span>
                  <button type="button" class="gh-token-btn" @click="openGitHubTokens">去获取 Token ↗</button>
                </div>
                <ol class="gh-token-steps">
                  <li>打开页面后点右上角 <b>Generate new token</b>，选 <b>Generate new token (classic)</b></li>
                  <li>Note 可填 <code>AgentCluster Market</code>，Expiration 建议选 90 days</li>
                  <li>权限勾选 <b>repo</b>（完整仓库读写，推送市场必需）</li>
                  <li>点页面底部 <b>Generate token</b>，复制 <code>ghp_</code> 开头的 Token 粘贴到下方</li>
                </ol>
              </div>
              <label class="mm-field">
                <span class="mm-label">Personal Access Token（需要 repo 写权限）</span>
                <input v-model="loginToken" type="password" class="mm-input"
                  placeholder="ghp_..." autocomplete="current-password" />
              </label>
              <div class="mm-tip">
                Token 仅保存在本机 <code>%LOCALAPPDATA%\AgentCluster\github_auth.json</code>，不会上传；
                账号还须是市场仓库的协作者。
              </div>
              <div v-if="loginError" class="mm-error">{{ loginError }}</div>
            </template>
          </div>
          <div class="mm-foot">
            <template v-if="auth.logged_in">
              <button class="btn ghost gh-danger" @click="doLogout">退出登录</button>
              <button class="btn primary" @click="closeLogin">完成</button>
            </template>
            <template v-else>
              <button class="btn ghost" @click="closeLogin">取消</button>
              <button class="btn primary" :disabled="loggingIn" @click="doLogin">
                {{ loggingIn ? '登录中…' : '登录' }}
              </button>
            </template>
          </div>
        </div>
      </div>
      <div v-if="toast" class="mk-toast" @click="toast = ''">{{ toast }}</div>
      <div v-if="previewOpen" class="mk-modal-mask">
        <div class="mk-modal preview-modal">
          <div class="mm-head">
            <span class="mm-title">
              📋 资源预览
              <template v-if="previewData">· {{ previewData.item?.name || '' }}</template>
            </span>
            <button class="mm-close" @click="closePreview">✕</button>
          </div>
          <div class="mm-body">
            <div v-if="previewLoading" class="mm-empty">加载预览中…</div>
            <div v-else-if="previewError" class="mm-error">{{ previewError }}</div>
            <template v-else-if="previewData">
              <div class="pv-header">
                <span class="pv-kind">{{ KIND_ICON[previewData.preview.kind] || '📦' }} {{ kindLabel(previewData.preview.kind) }}</span>
                <span v-if="previewData.item?.author" class="pv-author">@{{ previewData.item.author }}</span>
                <span v-if="previewData.item?.version" class="pv-version">v{{ previewData.item.version }}</span>
                <span v-if="previewData.item?.updated_at" class="pv-updated">更新于 {{ previewData.item.updated_at.slice(0, 16).replace('T', ' ') }}</span>
              </div>
              <div v-if="previewData.item?.description" class="pv-desc">{{ previewData.item.description }}</div>

              <!-- Agent 预览 -->
              <template v-if="previewData.preview.kind === 'agent'">
                <div class="pv-section">
                  <div class="pv-section-title">🎯 系统提示词</div>
                  <pre class="pv-code pv-prompt">{{ previewData.preview.agent?.system_prompt || '（未设置）' }}</pre>
                </div>
                <div class="pv-row">
                  <div class="pv-field"><label>模型</label><span>{{ previewData.preview.agent?.model || '—' }}</span></div>
                  <div class="pv-field"><label>工作流模式</label><span>{{ previewData.preview.agent?.workflow_mode || '—' }}</span></div>
                  <div class="pv-field"><label>感知范围</label><span>{{ previewData.preview.agent?.perception_scope || '—' }}</span></div>
                  <div class="pv-field"><label>推理级别</label><span>{{ previewData.preview.agent?.reasoning_level || '—' }}</span></div>
                </div>
                <div class="pv-section">
                  <div class="pv-section-title">🔧 可用命令（{{ (previewData.preview.agent?.commands || []).length }} 个）</div>
                  <div class="pv-chip-list">
                    <span v-for="c in previewData.preview.agent?.commands" :key="c" class="pv-chip">{{ c }}</span>
                  </div>
                </div>
                <div v-if="(previewData.preview.agent?.tools || []).length" class="pv-section">
                  <div class="pv-section-title">🧩 插件（{{ previewData.preview.agent.tools.length }} 个）</div>
                  <div class="pv-chip-list">
                    <span v-for="t in previewData.preview.agent.tools" :key="t" class="pv-chip">{{ t }}</span>
                  </div>
                </div>
              </template>

              <!-- Canvas 预览 -->
              <template v-else-if="previewData.preview.kind === 'canvas'">
                <div class="pv-canvas-thumb" :class="{ loading: !canvasThumbReady, zoomable: canvasThumbReady }"
                  @click="canvasThumbReady && openCanvasZoom()"
                  title="点击放大查看">
                  <svg v-if="canvasThumbReady" :viewBox="canvasThumbBox" class="pv-canvas-svg">
                    <!-- 连线 -->
                    <defs>
                      <marker id="pv-arrow" viewBox="0 0 10 10" refX="8" refY="5"
                        markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(120,140,255,0.7)" />
                      </marker>
                    </defs>
                    <g v-for="(edge, i) in canvasThumbEdges" :key="'e' + i">
                      <path :d="edge.path" class="pv-edge-line"
                        marker-end="url(#pv-arrow)" />
                    </g>
                    <!-- 节点 -->
                    <g v-for="(node, i) in canvasThumbNodes" :key="'n' + i">
                      <circle :cx="node.cx" :cy="node.cy" :r="node.r"
                        class="pv-node" />
                      <text :x="node.cx" :y="node.cy"
                        class="pv-node-label"
                        text-anchor="middle" dominant-baseline="middle"
                        :title="node.fullName">
                        <tspan
                          v-for="(line, li) in node.lines"
                          :key="li"
                          :x="node.cx"
                          :dy="li === 0 ? `${-(node.lines.length - 1) * 0.45}em` : '0.9em'"
                        >{{ line }}</tspan>
                      </text>
                    </g>
                  </svg>
                  <div v-else class="pv-thumb-loading">正在渲染画布缩略图…</div>
                  <div v-if="canvasThumbReady" class="pv-zoom-hint">
                    <span>🔍 点击放大</span>
                  </div>
                </div>
                <div class="pv-row">
                  <div class="pv-stat"><strong>{{ previewData.preview.canvas?.agent_count || 0 }}</strong> 智能体</div>
                  <div class="pv-stat"><strong>{{ previewData.preview.canvas?.node_count || 0 }}</strong> 节点</div>
                  <div class="pv-stat"><strong>{{ previewData.preview.canvas?.edge_count || 0 }}</strong> 连线</div>
                  <div class="pv-stat"><strong>{{ previewData.preview.canvas?.agent_group_count || 0 }}</strong> 分组</div>
                  <div class="pv-stat"><strong>{{ EXECUTION_MODE_LABEL[previewData.preview.canvas?.execution_mode] || previewData.preview.canvas?.execution_mode }}</strong> 执行模式</div>
                </div>
                <div class="pv-section">
                  <div class="pv-section-title">🤖 画布内智能体</div>
                  <div v-for="(a, i) in previewData.preview.canvas?.agents" :key="i" class="pv-agent-card">
                    <div class="pv-agent-name">{{ a.name }}<span v-if="a.group_name" class="pv-agent-group">· {{ a.group_name }}</span></div>
                    <div v-if="a.description" class="pv-agent-desc">{{ a.description }}</div>
                    <div class="pv-agent-meta">
                      <span v-if="a.model">模型：{{ a.model }}</span>
                      <span>命令 {{ a.commands?.length || 0 }}</span>
                      <span>工具 {{ a.tool_count || 0 }}</span>
                    </div>
                  </div>
                </div>
                <div v-if="previewData.preview.canvas?.nodes?.length" class="pv-section">
                  <div class="pv-section-title">🔗 节点连接</div>
                  <div class="pv-chip-list">
                    <span v-for="(n, i) in previewData.preview.canvas.edges" :key="i" class="pv-chip pv-edge">
                      {{ n.from }} → {{ n.to }}
                    </span>
                  </div>
                </div>
              </template>

              <!-- Background 预览 -->
              <template v-else-if="previewData.preview.kind === 'background'">
                <div v-if="previewData.preview.background?.entry_content" class="pv-section">
                  <div class="pv-section-title">🎬 背景实时预览</div>
                  <iframe class="pv-bg-iframe" :srcdoc="previewData.preview.background.entry_content"></iframe>
                </div>
                <div class="pv-row">
                  <div v-if="previewData.preview.background?.type" class="pv-field"><label>类型</label><span>{{ previewData.preview.background.type }}</span></div>
                  <div v-if="previewData.preview.background?.entry_file" class="pv-field"><label>入口文件</label><span>{{ previewData.preview.background.entry_file }}</span></div>
                  <div v-if="previewData.preview.background?.opacity != null" class="pv-field"><label>透明度</label><span>{{ previewData.preview.background.opacity }}</span></div>
                </div>
                <div v-if="previewData.preview.background?.preview" class="pv-section">
                  <div class="pv-section-title">🎨 CSS 样式参考</div>
                  <div class="pv-bg-preview" :style="{ background: previewData.preview.background.preview }"></div>
                  <pre class="pv-code">{{ previewData.preview.background.preview }}</pre>
                </div>
              </template>

              <!-- Skill 预览 -->
              <template v-else-if="previewData.preview.kind === 'skill'">
                <div v-if="previewData.preview.skill?.frontmatter" class="pv-section">
                  <div class="pv-section-title">📋 元数据</div>
                  <div class="pv-row">
                    <div v-if="previewData.preview.skill.frontmatter.label" class="pv-field"><label>标签</label><span>{{ previewData.preview.skill.frontmatter.label }}</span></div>
                    <div v-if="previewData.preview.skill.frontmatter.version" class="pv-field"><label>版本</label><span>{{ previewData.preview.skill.frontmatter.version }}</span></div>
                    <div v-if="previewData.preview.skill.frontmatter.author" class="pv-field"><label>作者</label><span>{{ previewData.preview.skill.frontmatter.author }}</span></div>
                  </div>
                </div>
                <div class="pv-section">
                  <div class="pv-section-title">📖 技能说明（Markdown）</div>
                  <div class="pv-markdown">{{ previewData.preview.skill?.body || '（暂无内容）' }}</div>
                </div>
              </template>

              <!-- Plugin 预览 -->
              <template v-else-if="previewData.preview.kind === 'plugin'">
                <div class="pv-section">
                  <div class="pv-section-title">📋 插件信息</div>
                  <div class="pv-row">
                    <div v-if="previewData.preview.plugin?.meta?.name" class="pv-field"><label>名称</label><span>{{ previewData.preview.plugin.meta.name }}</span></div>
                    <div v-if="previewData.preview.plugin?.meta?.version" class="pv-field"><label>版本</label><span>{{ previewData.preview.plugin.meta.version }}</span></div>
                    <div v-if="previewData.preview.plugin?.meta?.icon" class="pv-field"><label>图标</label><span>{{ previewData.preview.plugin.meta.icon }}</span></div>
                  </div>
                </div>
                <div v-if="previewData.preview.plugin?.meta?.dependencies" class="pv-section">
                  <div class="pv-section-title">📦 依赖</div>
                  <pre class="pv-code">{{ JSON.stringify(previewData.preview.plugin.meta.dependencies, null, 2) }}</pre>
                </div>
                <div v-if="previewData.preview.plugin?.tool_info" class="pv-section">
                  <div class="pv-section-title">🔧 工具配置（tool.yaml）</div>
                  <pre class="pv-code">{{ JSON.stringify(previewData.preview.plugin.tool_info, null, 2) }}</pre>
                </div>
                <div v-if="previewData.preview.plugin?.tool_source" class="pv-section">
                  <div class="pv-section-title">💻 工具源码（tool.py）</div>
                  <pre class="pv-code pv-python">{{ previewData.preview.plugin.tool_source }}</pre>
                </div>
              </template>

              <!-- Extension 预览 -->
              <template v-else-if="previewData.preview.kind === 'extension'">
                <div class="pv-section">
                  <div class="pv-section-title">📋 功能拓展信息</div>
                  <div class="pv-row">
                    <div v-if="previewData.preview.extension?.meta?.name" class="pv-field"><label>名称</label><span>{{ previewData.preview.extension.meta.name }}</span></div>
                    <div v-if="previewData.preview.extension?.meta?.version" class="pv-field"><label>版本</label><span>{{ previewData.preview.extension.meta.version }}</span></div>
                    <div v-if="previewData.preview.extension?.meta?.type" class="pv-field"><label>类型</label><span>{{ previewData.preview.extension.meta.type }}</span></div>
                  </div>
                </div>
                <div v-if="previewData.preview.extension?.readme" class="pv-section">
                  <div class="pv-section-title">📖 README</div>
                  <div class="pv-markdown">{{ previewData.preview.extension.readme }}</div>
                </div>
                <div v-if="previewData.preview.extension?.backend_code" class="pv-section">
                  <div class="pv-section-title">💻 后端代码（backend.py）</div>
                  <pre class="pv-code pv-python">{{ previewData.preview.extension.backend_code }}</pre>
                </div>
              </template>

              <!-- 文件列表 -->
              <div v-if="previewData.files?.length" class="pv-section">
                <div class="pv-section-title">📁 文件列表（{{ previewData.files.length }} 个）</div>
                <div class="pv-file-list">
                  <div v-for="f in previewData.files" :key="f.path" class="pv-file">
                    <span class="pv-file-name">{{ f.path }}</span>
                    <span class="pv-file-size">{{ formatSize(f.size) }}</span>
                  </div>
                </div>
              </div>

              <!-- 版本历史 -->
              <div v-if="previewVersions.length > 1" class="pv-section">
                <div class="pv-section-title">🕰 版本历史（{{ previewVersions.length }} 个版本）</div>
                <div class="pv-version-list">
                  <div
                    v-for="v in previewVersions" :key="v.version"
                    class="pv-version-row"
                    :class="{ active: previewSelectedVersion === v.version }"
                    @click="previewSelectedVersion = v.version"
                  >
                    <span class="pv-ver-num">v{{ v.version }}</span>
                    <span v-if="v.latest" class="pv-ver-tag latest">最新</span>
                    <span v-else-if="v.author && v.author !== previewData.item?.author" class="pv-ver-tag other-author">作者 @{{ v.author }}</span>
                    <span class="pv-ver-updated">{{ v.updated_at ? v.updated_at.slice(0, 16).replace('T', ' ') : '' }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>
          <div class="mm-foot">
            <div v-if="previewVersions.length > 1 && previewData?.item" class="pv-install-row">
              <label class="pv-install-label">安装版本：</label>
              <select class="pv-install-select" v-model="previewSelectedVersion">
                <option v-for="v in previewVersions" :key="v.version" :value="v.version">
                  v{{ v.version }}{{ v.latest ? '  (最新)' : '' }}
                </option>
              </select>
              <button
                class="btn primary"
                :disabled="busy !== ''"
                @click="install(previewData.item, previewSelectedVersion)"
              >{{ busy ? '下载中…' : '安装此版本' }}</button>
            </div>
            <div v-else-if="previewData?.item" class="pv-install-row">
              <button class="btn primary" :disabled="busy !== ''" @click="install(previewData.item)">
                {{ busy ? '下载中…' : `安装 v${previewData.item.version || '最新'}` }}
              </button>
            </div>
            <button class="btn ghost" @click="closePreview">关闭</button>
          </div>
        </div>
      </div>
      <!-- 画布放大弹窗 -->
      <div v-if="canvasZoomOpen && canvasThumbReady" class="mk-modal-mask">
        <div class="canvas-zoom-modal">
          <div class="cz-head">
            <span class="cz-title">🗺 画布详情预览</span>
            <button class="cz-close" @click="closeCanvasZoom">✕</button>
          </div>
          <div class="cz-body">
            <svg :viewBox="canvasThumbBox" class="cz-svg">
              <defs>
                <marker id="cz-arrow" viewBox="0 0 10 10" refX="8" refY="5"
                  markerWidth="8" markerHeight="8" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(120,140,255,0.85)" />
                </marker>
              </defs>
              <g v-for="(edge, i) in canvasThumbEdges" :key="'ze' + i">
                <path :d="edge.path" class="cz-edge-line"
                  marker-end="url(#cz-arrow)" />
              </g>
              <g v-for="(node, i) in canvasThumbNodes" :key="'zn' + i">
                <circle :cx="node.cx" :cy="node.cy" :r="node.r"
                  class="cz-node" />
                <text :x="node.cx" :y="node.cy"
                  class="cz-node-label"
                  text-anchor="middle" dominant-baseline="middle"
                  :title="node.fullName">
                  <tspan
                    v-for="(line, li) in node.lines"
                    :key="li"
                    :x="node.cx"
                    :dy="li === 0 ? `${-(node.lines.length - 1) * 0.45}em` : '0.9em'"
                  >{{ line }}</tspan>
                </text>
              </g>
            </svg>
          </div>
          <div class="cz-foot">
            <span class="cz-tip">ESC 或点击关闭按钮关闭</span>
            <button class="btn ghost" @click="closeCanvasZoom">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.mk-root {
  /* 顶部预留窗口最小化/最大化/关闭按钮区域，避免遮挡顶栏右侧「上传资源」等按钮 */
  padding: 40px 16px 16px; height: calc(100vh - var(--titlebar-h));
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
.gh-login { display: inline-flex; align-items: center; gap: 6px; }
.gh-login.logged { padding-left: 6px; }
.cache-indicator {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(12px * var(--font-scale)); color: rgba(232, 238, 252, .92);
  padding: 4px 10px; border-radius: 8px;
  background: rgba(255, 255, 255, .07);
  border: 1px solid rgba(255, 255, 255, .16);
  white-space: nowrap;
}

.cache-clean-btn {
  font-size: calc(11px * var(--font-scale));
  padding: 2px 10px; border-radius: 6px;
  background: rgba(var(--accent-rgb, 120, 140, 255), .2);
  border: 1px solid rgba(var(--accent-rgb, 120, 140, 255), .5);
  color: #d5deff;
  cursor: pointer; line-height: 1.5;
  transition: background .15s, color .15s, border-color .15s;
}
.cache-clean-btn:hover:not(:disabled) {
  background: rgba(255, 120, 120, .24);
  border-color: rgba(255, 120, 120, .55);
  color: #ffc9c9;
}
.cache-clean-btn:disabled { opacity: .55; cursor: not-allowed; }
.gh-avatar { width: 18px; height: 18px; border-radius: 50%; display: block; }
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
.kind-item.mine { border-top: 1px dashed rgba(255, 255, 255, .1); margin-top: 4px; padding-top: 10px; }
.kind-item.mine.active { background: rgba(255, 180, 120, .18); color: #ffc98a; }
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
  overflow: hidden;
}
.mc-icon-emoji { line-height: 1; }
.mc-preview-img { width: 100%; height: 100%; object-fit: cover; display: block; border-radius: 9px; }
.mc-title-wrap { flex: 1; min-width: 0; }
.mc-title { font-weight: 600; font-size: calc(14px * var(--font-scale)); }
.mc-sub { font-size: calc(11px * var(--font-scale)); opacity: .5; margin-top: 2px; }
.mc-badge { font-size: 10px; padding: 2px 7px; border-radius: 8px; background: rgba(111, 207, 151, .14); color: #6fcf97; flex-shrink: 0; }
.mc-badge.warn { background: rgba(255, 180, 120, .18); color: #ffc98a; }
.mc-desc {
  font-size: calc(12px * var(--font-scale)); opacity: .65; line-height: 1.5; min-height: 34px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.mc-tags { display: flex; flex-wrap: wrap; gap: 5px; min-height: 18px; }
.tag { font-size: 10px; padding: 1px 7px; border-radius: 8px; background: rgba(255, 255, 255, .06); color: rgba(255, 255, 255, .55); }
.mc-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 2px; }
.mc-author {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: calc(11px * var(--font-scale)); opacity: .55;
  padding: 3px 8px; border-radius: 10px;
  background: rgba(255, 255, 255, .04);
  transition: opacity .15s, background .15s;
}
.mc-author:hover { opacity: .85; background: rgba(255, 255, 255, .08); }
.mc-author-avatar {
  width: 16px; height: 16px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10px; background: rgba(var(--accent-rgb, 120, 140, 255), .25);
}
.act { padding: 5px 16px; border-radius: 7px; border: 1px solid rgba(255, 255, 255, .15); background: transparent; color: inherit; cursor: pointer; font-size: 12px; transition: all .15s; }
.act:hover { background: rgba(255, 255, 255, .08); }
.act:disabled { opacity: .45; cursor: not-allowed; }
.act.primary { background: rgba(var(--accent-rgb, 120, 140, 255), .2); border-color: rgba(var(--accent-rgb, 120, 140, 255), .5); color: var(--accent-light, #9db4ff); }
.act.primary:hover:not(:disabled) { background: rgba(var(--accent-rgb, 120, 140, 255), .3); }
.act.ghost { background: transparent; border-color: rgba(255, 255, 255, .1); color: rgba(255, 255, 255, .7); }
.act.ghost:hover:not(:disabled) { background: rgba(255, 255, 255, .06); color: inherit; }
.act.danger { background: rgba(230, 90, 90, .12); border-color: rgba(230, 90, 90, .45); color: #ff9c9c; }
.act.danger:hover:not(:disabled) { background: rgba(230, 90, 90, .22); color: #ffc0c0; }

/* —— 预览弹窗 —— */
.preview-modal { width: min(780px, 94vw); max-height: 86vh; }
.pv-header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-bottom: 10px; border-bottom: 1px solid rgba(255, 255, 255, .08); margin-bottom: 12px; }
.pv-kind { font-size: 14px; font-weight: 600; }
.pv-author { font-size: 12px; opacity: .6; padding: 2px 8px; border-radius: 8px; background: rgba(255, 255, 255, .05); }
.pv-version { font-size: 11px; opacity: .45; padding: 2px 6px; border-radius: 6px; border: 1px solid rgba(255, 255, 255, .1); }
.pv-updated { font-size: 11px; opacity: .4; margin-left: auto; }
.pv-desc { font-size: 13px; opacity: .75; line-height: 1.6; padding: 0 2px 12px; border-bottom: 1px solid rgba(255, 255, 255, .05); margin-bottom: 12px; white-space: pre-wrap; }

.pv-section { margin-bottom: 16px; }
.pv-section-title {
  font-size: 12px; font-weight: 600; opacity: .7; margin-bottom: 8px;
  padding-bottom: 6px; border-bottom: 1px solid rgba(255, 255, 255, .06);
}
.pv-row { display: flex; flex-wrap: wrap; gap: 10px; }
.pv-field {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 12px; border-radius: 8px; background: rgba(255, 255, 255, .03);
  min-width: 120px;
}
.pv-field label { font-size: 10px; opacity: .45; text-transform: uppercase; }
.pv-field span { font-size: 13px; }

.pv-stat {
  padding: 8px 14px; border-radius: 10px;
  background: rgba(var(--accent-rgb, 120, 140, 255), .1);
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  min-width: 90px;
  font-size: 11px; opacity: .8;
}
.pv-stat strong { font-size: 20px; font-weight: 700; color: var(--accent-light, #9db4ff); opacity: 1; }

.pv-chip-list { display: flex; flex-wrap: wrap; gap: 5px; }
.pv-chip {
  font-size: 11px; padding: 3px 9px; border-radius: 8px;
  background: rgba(255, 255, 255, .06); color: rgba(255, 255, 255, .65);
  font-family: ui-monospace, Consolas, monospace;
}
.pv-chip.pv-edge { color: rgba(var(--accent-rgb, 120, 140, 255), .9); }

.pv-code {
  background: rgba(0, 0, 0, .3); border: 1px solid rgba(255, 255, 255, .06);
  border-radius: 8px; padding: 12px 14px; margin: 0;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
  max-height: 320px; overflow: auto;
}
.pv-code.pv-prompt { max-height: 240px; color: #d4c1ff; }
.pv-code.pv-python { color: #a5d6ff; }
.pv-code.pv-html { color: #ffd4a5; }

.pv-markdown {
  background: rgba(0, 0, 0, .2); border: 1px solid rgba(255, 255, 255, .06);
  border-radius: 8px; padding: 12px 14px;
  font-size: 12px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
  max-height: 400px; overflow: auto;
}

.pv-agent-card {
  padding: 10px 12px; border-radius: 8px;
  background: rgba(255, 255, 255, .03);
  border: 1px solid rgba(255, 255, 255, .06);
  margin-bottom: 8px;
}
.pv-agent-name { font-size: 13px; font-weight: 600; }
.pv-agent-group { font-size: 11px; opacity: .5; font-weight: 400; }
.pv-agent-desc { font-size: 12px; opacity: .65; margin-top: 4px; line-height: 1.5; }
.pv-agent-meta {
  margin-top: 6px; display: flex; gap: 10px; flex-wrap: wrap;
  font-size: 11px; opacity: .5;
}

.pv-bg-preview {
  height: 80px; border-radius: 8px; margin-bottom: 8px;
  border: 1px solid rgba(255, 255, 255, .1);
}

.pv-canvas-thumb {
  background: rgba(0, 0, 0, .25);
  border: 1px solid rgba(255, 255, 255, .08);
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 14px;
  overflow: hidden;
  position: relative;
  min-height: 200px;
}
.pv-canvas-thumb.zoomable {
  cursor: zoom-in;
  transition: border-color .15s, box-shadow .15s;
}
.pv-canvas-thumb.zoomable:hover {
  border-color: rgba(var(--accent-rgb, 120, 140, 255), .4);
  box-shadow: 0 0 0 1px rgba(var(--accent-rgb, 120, 140, 255), .15);
}
.pv-canvas-thumb.loading .pv-canvas-svg { opacity: .3; }
.pv-canvas-svg {
  width: 100%; height: 260px;
  display: block;
  background:
    linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px);
  background-size: 24px 24px;
  border-radius: 8px;
}
.pv-zoom-hint {
  position: absolute; bottom: 12px; right: 12px;
  background: rgba(0, 0, 0, .55);
  padding: 4px 10px; border-radius: 6px;
  font-size: 11px; opacity: 0;
  transition: opacity .2s;
  pointer-events: none;
}
.pv-canvas-thumb.zoomable:hover .pv-zoom-hint { opacity: 1; }
.pv-node {
  fill: rgba(var(--accent-rgb, 120, 140, 255), .35);
  stroke: rgba(var(--accent-rgb, 120, 140, 255), .8);
  stroke-width: 2;
}
.pv-node-label {
  font-size: 11px;
  fill: rgba(255, 255, 255, .9);
  font-weight: 600;
  paint-order: stroke;
  stroke: rgba(0, 0, 0, .65);
  stroke-width: 3;
  line-height: 1;
}
.pv-edge-line {
  fill: none;
  stroke: rgba(120, 140, 255, .7);
  stroke-width: 2.5;
}
.pv-thumb-loading {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; opacity: .5;
}

/* —— 画布放大弹窗 —— */
.canvas-zoom-modal {
  width: min(900px, 96vw); height: min(680px, 88vh);
  display: flex; flex-direction: column;
  background: #0f1626; border: 1px solid rgba(255, 255, 255, .14);
  border-radius: 12px; overflow: hidden; box-shadow: 0 18px 48px rgba(0, 0, 0, .5);
}
.cz-head {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-bottom: 1px solid rgba(255, 255, 255, .09);
  flex-shrink: 0;
}
.cz-title { flex: 1; font-weight: 600; font-size: 14px; }
.cz-close {
  width: 28px; height: 28px; border-radius: 6px; cursor: pointer;
  border: 1px solid rgba(255, 255, 255, .16); background: transparent;
  color: inherit; font-size: 13px; line-height: 1;
}
.cz-close:hover { background: rgba(255, 255, 255, .08); }
.cz-body {
  flex: 1; min-height: 0; padding: 16px;
  background: rgba(0, 0, 0, .35);
  display: flex; align-items: center; justify-content: center;
  overflow: auto;
}
.cz-svg {
  max-width: 100%; max-height: 100%;
  background:
    linear-gradient(rgba(255,255,255,.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.04) 1px, transparent 1px);
  background-size: 28px 28px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, .08);
}
.cz-node {
  fill: rgba(var(--accent-rgb, 120, 140, 255), .4);
  stroke: rgba(var(--accent-rgb, 120, 140, 255), .9);
  stroke-width: 2.5;
  transition: fill .15s;
}
.cz-node:hover { fill: rgba(var(--accent-rgb, 120, 140, 255), .55); }
.cz-node-label {
  font-size: 14px;
  fill: rgba(255, 255, 255, .9);
  font-weight: 600;
  paint-order: stroke;
  stroke: rgba(0, 0, 0, .7);
  stroke-width: 3.5;
}
.cz-edge-line {
  fill: none;
  stroke: rgba(120, 140, 255, .75);
  stroke-width: 2.5;
}
.cz-foot {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; border-top: 1px solid rgba(255, 255, 255, .09);
  flex-shrink: 0;
}
.cz-tip { font-size: 11px; opacity: .45; }

.pv-bg-iframe {
  width: 100%; height: 220px;
  border: 1px solid rgba(255, 255, 255, .08);
  border-radius: 10px;
  background: #000;
  display: block;
}

.pv-file-list { display: flex; flex-direction: column; gap: 2px; }
.pv-file {
  display: flex; justify-content: space-between; align-items: center;
  padding: 5px 10px; border-radius: 5px;
  font-size: 11px; font-family: ui-monospace, Consolas, monospace;
}
.pv-file:nth-child(odd) { background: rgba(255, 255, 255, .02); }
.pv-file:hover { background: rgba(255, 255, 255, .05); }
.pv-file-name { color: rgba(255, 255, 255, .7); }
.pv-file-size { color: rgba(255, 255, 255, .4); }

.mc-actions { display: flex; gap: 6px; }

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
.mm-hint-row { display: flex; align-items: center; gap: 10px; }
.mm-hint { flex: 1; font-size: 12px; opacity: .55; }
.mm-upload-filter {
  display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0;
  font-size: calc(12px * var(--font-scale)); opacity: .8;
  cursor: pointer; user-select: none; white-space: nowrap;
}
.mm-upload-filter input { cursor: pointer; accent-color: rgb(var(--accent-rgb, 120, 140, 255)); }
.mm-empty { text-align: center; opacity: .5; padding: 30px 0; font-size: 13px; }
.mm-list { display: flex; flex-direction: column; gap: 6px; }
/* 上传弹窗：固定尺寸 + 固定高度列表区，切换资源类型时弹窗不跳动 */
.mk-modal-upload { height: min(560px, 78vh); }
.mm-body-fixed { overflow: hidden; }
.mm-list-wrap {
  flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
  display: flex; flex-direction: column; gap: 6px;
  transition: opacity .16s ease;
}
.mm-list-wrap.is-loading { opacity: .4; pointer-events: none; }
.mm-list-wrap > .mm-empty { margin: auto 0; }
.mm-fade-enter-active, .mm-fade-leave-active { transition: opacity .16s ease; }
.mm-fade-enter-from, .mm-fade-leave-to { opacity: 0; }
.mm-item {
  display: flex; align-items: flex-start; gap: 10px; padding: 9px 11px;
  border: 1px solid rgba(255, 255, 255, .1); border-radius: 8px; cursor: pointer;
}
.mm-item:hover { background: rgba(255, 255, 255, .05); }
.mm-item.checked { border-color: rgba(var(--accent-rgb, 120, 140, 255), .55); background: rgba(var(--accent-rgb, 120, 140, 255), .12); }
.mm-item input { margin-top: 3px; }
.mm-item-body { min-width: 0; }
.mm-item-name { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; }
.mm-badge {
  font-size: calc(10px * var(--font-scale)); font-weight: 400;
  padding: 1px 6px; border-radius: 5px; line-height: 1.4; white-space: nowrap;
  background: rgba(120, 220, 170, .16);
  border: 1px solid rgba(120, 220, 170, .4);
  color: #9be8c3;
}
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
.mk-modal-sm { width: min(480px, 92vw); }
.gh-profile { display: flex; align-items: center; gap: 12px; }
.gh-avatar-lg { width: 44px; height: 44px; border-radius: 50%; display: block; }
.gh-name { font-size: calc(14px * var(--font-scale)); font-weight: 600; }
.gh-sub { font-size: calc(12px * var(--font-scale)); opacity: .55; margin-top: 2px; }
.gh-token-guide {
  padding: 8px 10px; border-radius: 8px;
  background: rgba(var(--accent-rgb, 120, 140, 255), .10);
  border: 1px solid rgba(var(--accent-rgb, 120, 140, 255), .28);
  font-size: 11px; color: #aabce0;
}
.gh-token-guide-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.gh-token-steps { margin: 6px 0 0; padding-left: 16px; display: flex; flex-direction: column; gap: 3px; line-height: 1.5; }
.gh-token-steps b { color: #dfe9ff; font-weight: 600; }
.gh-token-steps code { font-size: 10px; opacity: .9; }
.gh-token-btn {
  flex-shrink: 0; padding: 4px 9px; border-radius: 6px; cursor: pointer;
  font-size: 11px; font-family: inherit; color: #d6e4ff;
  background: rgba(var(--accent-rgb, 120, 140, 255), .18);
  border: 1px solid rgba(var(--accent-rgb, 120, 140, 255), .4);
  white-space: nowrap;
}
.gh-token-btn:hover { background: rgba(var(--accent-rgb, 120, 140, 255), .32); }
.gh-ok { color: #6fcf97; }
.gh-bad { color: #f2994a; }
.gh-danger { color: #ff8a80; border-color: rgba(255, 138, 128, .35); }
.mm-error { font-size: 12px; color: #ff8a80; line-height: 1.5; white-space: pre-wrap; }
.mm-push-check.disabled { opacity: .55; cursor: not-allowed; }

.unpublish-confirm { display: flex; flex-direction: column; gap: 10px; }
.unpublish-msg { font-size: calc(13px * var(--font-scale)); line-height: 1.6; }
.unpublish-msg strong { color: #ffc98a; }

/* —— 版本历史 —— */
.pv-version-list { display: flex; flex-direction: column; gap: 4px; max-height: 260px; overflow-y: auto; }
.pv-version-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid rgba(255,255,255,.06);
  transition: background .15s, border-color .15s;
}
.pv-version-row:hover { background: rgba(255,255,255,.04); border-color: rgba(255,255,255,.12); }
.pv-version-row.active {
  background: rgba(var(--accent-rgb, 120,140,255), .15);
  border-color: rgba(var(--accent-rgb, 120,140,255), .4);
}
.pv-ver-num { font-weight: 600; font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: #d6deff; }
.pv-ver-tag { font-size: 11px; padding: 2px 6px; border-radius: 3px; }
.pv-ver-tag.latest { background: rgba(90, 200, 120, .18); color: #7fdfa0; }
.pv-ver-tag.other-author { background: rgba(200, 170, 90, .15); color: #e8c078; }
.pv-ver-updated { margin-left: auto; font-size: 11px; opacity: .55; }

/* 预览弹窗安装行 */
.pv-install-row { display: flex; align-items: center; gap: 10px; margin-right: auto; }
.pv-install-label { font-size: calc(12px * var(--font-scale)); opacity: .75; }
.pv-install-select {
  background: rgba(255,255,255,.06); color: #fff; border: 1px solid rgba(255,255,255,.15);
  border-radius: 4px; padding: 4px 8px; font-size: 12px; outline: none;
}
.pv-install-select:focus { border-color: rgba(var(--accent-rgb, 120,140,255), .6); }
</style>