<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useModelStore, useSettingsStore, useLayoutStore, useGuideStore, useAgentStore, FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP, FONT_FAMILIES, FONT_FAMILY_DEFAULT } from '@/stores'
import { api } from '@/api'
import FileExplorerDialog from '@/components/FileExplorerDialog.vue'
import { getAvatarGradient } from '@/utils/avatar'
import { testSuccess, testError } from '@/utils/sound'

const models = useModelStore()
const settings = useSettingsStore()
const agents = useAgentStore()
const layout = useLayoutStore()
const guide = useGuideStore()
const router = useRouter()
function startGuide(type: 'flow' | 'region') {
  guide.requestGuide(type)
  router.push('/')
}

// —— 右侧目录：快速跳转到各设置区块 ——
const sectionList = [
  { id: 'sec-model', name: '模型接口' },
  { id: 'sec-ctx', name: '历史上下文压缩' },
  { id: 'sec-agent', name: '默认智能体' },
  { id: 'sec-perm', name: '权限设置' },
  { id: 'sec-appearance', name: '外观设置' },
  { id: 'sec-guide', name: '新手引导' },
  { id: 'sec-sound', name: '提示音' },
  { id: 'sec-asr', name: '语音识别' },
  { id: 'sec-launch', name: '启动方式' },
  { id: 'sec-storage', name: '数据存储' },
] as const
const panelsRef = ref<HTMLElement | null>(null)
const activeSection = ref<string>(sectionList[0].id)

function sectionEl(id: string): HTMLElement | null {
  const root = panelsRef.value
  return root ? (root.querySelector('#' + id) as HTMLElement | null) : null
}

// 点击目录：让面板滚动容器滚到对应区块（留 8px 呼吸位）
function jumpToSection(id: string) {
  const root = panelsRef.value
  const el = sectionEl(id)
  if (!root || !el) return
  const top = el.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop - 8
  activeSection.value = id
  const from = root.scrollTop
  root.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
  // 兜底：窗口最小化 / 页面不可见时平滑滚动会被浏览器挂起，300ms 没动就直接定位
  window.setTimeout(() => {
    if (Math.abs(root.scrollTop - from) < 1 && Math.abs(from - top) > 1) root.scrollTop = Math.max(0, top)
  }, 320)
}

// 滚动监听：以容器顶部往下 28px 作为判定线，高亮当前区块
let _spyRaf = 0
function onPanelsScroll() {
  if (_spyRaf) return
  _spyRaf = requestAnimationFrame(() => {
    _spyRaf = 0
    const root = panelsRef.value
    if (!root) return
    const line = root.getBoundingClientRect().top + 28
    let current: string = sectionList[0].id
    for (const s of sectionList) {
      const el = sectionEl(s.id)
      if (el && el.getBoundingClientRect().top <= line) current = s.id
    }
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 8) current = sectionList[sectionList.length - 1].id
    activeSection.value = current
  })
}

const themeOptions = [
  { id: 'standard', name: '标准配色-深邃黑', bg: '#000000', bgGrad: '#080808', text: '#ececec', accent: '#6ea8ff', surface: 'rgba(255,255,255,0.12)' },
  { id: 'galaxy-blue', name: '银河蓝', bg: '#0a0e1a', bgGrad: '#1a2340', text: '#ececec', accent: '#6ea8ff', surface: 'rgba(255,255,255,0.12)' },
  { id: 'tech-white', name: '科技白', bg: '#ffffff', bgGrad: '#ffffff', text: '#0c121c', accent: '#2563eb', surface: '#eef3fb' },
  { id: 'eye-care', name: '护眼白', bg: '#f4ecd6', bgGrad: '#faf3df', text: '#46371e', accent: '#2b6cb0', surface: 'rgba(90,70,30,0.1)' },
]

const showDialog = ref(false)
const isEditing = ref(false)
const ifaceForm = ref<any>({})
const fetchingModels = ref(false)
const fetchMsg = ref<{ text: string; kind: 'ok' | 'err' } | null>(null)

// 数据存储路径
const storagePaths = ref<any>(null)

// 存储位置（可自定义，记忆到 settings，暂不迁移已有数据）
const storageLocation = ref('')
const storageSaving = ref(false)

// 文件浏览器弹窗
const explorerOpen = ref(false)
const explorerInitial = ref('')

// 迁移弹窗：把当前用户数据复制到「存储位置」输入框的路径，成功后切换存储根
const migrateOpen = ref(false)
const migrateTarget = ref('')
const migratePlan = ref<any>(null)   // dry-run 结果：copied / skipped / bytes / source / target
const migrateScanning = ref(false)
const migrating = ref(false)

// 删除源位置确认弹窗（迁移成功后弹出）
const deleteConfirmOpen = ref(false)
const deleteConfirmSource = ref('')  // 迁移完成后保留的源路径，供删除时使用
const deleteConfirmDeleting = ref(false)

// 默认能力的本地镜像（方便实时编辑、再写回 settings）
const defaults = ref<{ commands: string[]; skills: string[]; plugins: string[] }>({
  commands: [], skills: [], plugins: [],
})

// 工具调用最大轮数（可自定义，默认 50）
const maxRounds = ref<number>(50)

// 统一能力选择器（与 AgentEditDialog 同款）
const pickerOpen = ref<string | null>(null)
const selectedKeys = ref<Set<string>>(new Set())
const collapsedGroups = ref<Set<string>>(new Set())

// 能力刷新
const refreshLoading = ref(false)
const toast = ref<{ msg: string; kind: 'ok' | 'err' } | null>(null)
let _toastTimer: number | null = null
function showToast(msg: string, kind: 'ok' | 'err' = 'ok') {
  toast.value = { msg, kind }
  if (_toastTimer) clearTimeout(_toastTimer)
  _toastTimer = window.setTimeout(() => { toast.value = null }, 2500)
}
async function reloadCapabilities() {
  if (refreshLoading.value) return
  refreshLoading.value = true
  try {
    await settings.reloadCapabilities()
    showToast(`已刷新：${Object.keys(settings.commandsMeta).length} 个命令 · ${Object.keys(settings.skillsMeta).length} 个技能 · ${Object.keys(settings.pluginsMeta).length} 个插件`)
  } catch (e: any) {
    showToast(`刷新失败：${e?.message || e}`, 'err')
  } finally {
    refreshLoading.value = false
  }
}

// 背景管理
const bgList = ref<any[]>([])
const bgLoading = ref(false)
const bgUploading = ref(false)
const bgFileInput = ref<HTMLInputElement | null>(null)
const bgUploadType = ref<'animation' | 'video' | 'image'>('image')

async function loadBackgrounds() {
  bgLoading.value = true
  try { bgList.value = await api.backgrounds.list() } catch { /* 静默 */ }
  bgLoading.value = false
}

function pickBackgroundFiles(type: 'animation' | 'video' | 'image') {
  bgUploadType.value = type
  bgFileInput.value?.click()
}

async function onBgFiles(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files
  if (!files || !files.length) return
  const typeLabel = bgUploadType.value === 'animation' ? '动画' : bgUploadType.value === 'video' ? '视频' : '图片'
  if (!confirm(`将所选文件作为${typeLabel}背景上传？`)) { input.value = ''; return }
  bgUploading.value = true
  try {
    const form = new FormData()
    form.append('name', files[0].name.replace(/\.[^.]+$/, '') || '自定义背景')
    form.append('type', bgUploadType.value)
    for (const f of Array.from(files)) form.append('files', f)
    const created = await api.backgrounds.upload(form)
    await loadBackgrounds()
    layout.setBackground(created.id)
    showToast(`背景「${created.name}」上传成功`)
  } catch (err: any) {
    showToast('上传失败：' + (err?.message || err), 'err')
  } finally {
    bgUploading.value = false
    input.value = ''
  }
}

async function deleteBackground(bg: any) {
  if (bg.builtin) return
  if (!confirm(`确定删除背景「${bg.name}」？`)) return
  try {
    await api.backgrounds.remove(bg.id)
    if (layout.backgroundId === bg.id) layout.setBackground(null)
    await loadBackgrounds()
    showToast('已删除')
  } catch (err: any) {
    showToast('删除失败：' + (err?.message || err), 'err')
  }
}

function bgThumbStyle(bg: any): any {
  if (bg.thumbnail && bg.type !== 'video') {
    return { backgroundImage: `url(${api.backgrounds.rawUrl(bg.id, bg.thumbnail)})`, backgroundSize: 'cover', backgroundPosition: 'center' }
  }
  if (bg.preview) return { background: bg.preview }
  return { background: 'linear-gradient(135deg, #080808, #000000)' }
}

/* —— 背景缩略：静止态显示代表画面，悬停时换真实动画/视频并浮出放大预览 —— */
const bgHoverId = ref<string | null>(null)
const magnify = ref<{ id: string; name: string; url: string; x: number; y: number; w: number; h: number; base: any } | null>(null)
let bgHoverTimer: number | null = null
let bgMagnifyTimer: number | null = null
let hoverCardEl: HTMLElement | null = null

function bgRawUrl(bg: any) {
  return api.backgrounds.rawUrl(bg.id, bg.entry || bg.thumbnail)
}

/** 默认背景卡：按当前主题给出真实底光，避免科技白下显示成一块黑 */
const defaultBgStyle = computed(() => {
  if (layout.theme === 'tech-white') {
    return {
      background:
        'radial-gradient(58% 54% at 6% 0%, rgba(37,99,235,.15), transparent 62%),' +
        'radial-gradient(44% 44% at 94% 2%, rgba(124,58,237,.11), transparent 60%),' +
        'radial-gradient(52% 48% at 62% 102%, rgba(14,165,233,.12), transparent 64%),' +
        'linear-gradient(158deg, #ffffff 0%, #f6faff 46%, #ecf2fe 100%)',
    }
  }
  if (layout.theme === 'eye-care') {
    return { background: 'radial-gradient(ellipse at 30% 20%, #faf3df 0%, #f4ecd6 70%)' }
  }
  return { background: 'radial-gradient(ellipse at 30% 20%, #080808 0%, #000000 70%)' }
})

function clearBgTimers() {
  if (bgHoverTimer) { clearTimeout(bgHoverTimer); bgHoverTimer = null }
  if (bgMagnifyTimer) { clearTimeout(bgMagnifyTimer); bgMagnifyTimer = null }
}

function onBgEnter(bg: any, e: MouseEvent) {
  if (bg.type !== 'animation' && bg.type !== 'video') return
  const card = e.currentTarget as HTMLElement
  hoverCardEl = card
  clearBgTimers()
  bgHoverTimer = window.setTimeout(() => { bgHoverId.value = bg.id }, 120)
  bgMagnifyTimer = window.setTimeout(() => {
    magnify.value = { id: bg.id, name: bg.name, url: bgRawUrl(bg), x: 0, y: 0, w: 340, h: 213, base: bgThumbStyle(bg) }
    placeMagnify()
  }, 460)
}

/** 浮层跟随卡片：滚动时重算位置，避免浮层错位 */
function placeMagnify() {
  if (!magnify.value || !hoverCardEl) return
  const r = hoverCardEl.getBoundingClientRect()
  const { w, h } = magnify.value
  const gap = 14
  let x = r.right + gap
  if (x + w > window.innerWidth - 12) x = r.left - w - gap
  if (x < 12) x = Math.max(12, Math.min(window.innerWidth - w - 12, r.left))
  let y = r.top - 56
  if (y + h > window.innerHeight - 12) y = window.innerHeight - h - 12
  if (y < 12) y = 12
  magnify.value = { ...magnify.value, x, y }
}

let scrollRaf = 0
function onPageScroll() {
  if (!magnify.value || scrollRaf) return
  scrollRaf = window.requestAnimationFrame(() => {
    scrollRaf = 0
    placeMagnify()
  })
}

function onBgLeave() {
  clearBgTimers()
  bgHoverId.value = null
  magnify.value = null
  hoverCardEl = null
}

function hideMagnify() {
  clearBgTimers()
  magnify.value = null
}

const asrUninstalling = ref(false)
async function uninstallASR() {
  if (!confirm('确定卸载语音识别模型？\n\n这将删除 SenseVoiceSmall 模型文件（约 2GB）。\n依赖包保留，重新启用时将自动重新下载模型。')) return
  asrUninstalling.value = true
  try {
    await api.asr.uninstall()
    settings.setAsrEnabled(false)
    showToast('模型已卸载，已自动停用。重新启用时将自动下载模型')
  } catch (e: any) {
    showToast('卸载失败：' + (e?.message || e), 'err')
  } finally {
    asrUninstalling.value = false
  }
}

// 启动方式：client (默认) | web
const launchMode = ref<'client' | 'web'>('client')
const launchModeSaving = ref(false)

async function loadLaunchMode() {
  try {
    const r = await api.launchMode.get()
    launchMode.value = r.mode
  } catch { /* 静默，用默认 */ }
}

async function saveLaunchMode(mode: 'client' | 'web') {
  launchModeSaving.value = true
  try {
    await api.launchMode.set(mode)
    launchMode.value = mode
    showToast(mode === 'client' ? '已设置：下次启动打开桌面客户端' : '已设置：下次启动打开浏览器网页')
  } catch (e: any) {
    showToast('保存失败：' + (e?.message || e), 'err')
  } finally {
    launchModeSaving.value = false
  }
}

// 开机自启动
const autostartEnabled = ref(false)
const autostartSupported = ref(true)
const autostartSaving = ref(false)
const autostartCmd = ref('')

async function loadAutostart() {
  try {
    const r = await api.autostart.get()
    autostartEnabled.value = !!r.enabled
    autostartSupported.value = r.supported !== false
    autostartCmd.value = r.command || ''
  } catch { /* 静默 */ }
}

async function toggleAutostart() {
  if (!autostartSupported.value) return
  autostartSaving.value = true
  const next = !autostartEnabled.value
  try {
    const r = await api.autostart.set(next)
    autostartEnabled.value = !!r.enabled
    autostartCmd.value = r.command || ''
    showToast(next ? '已开启开机自启动' : '已关闭开机自启动')
  } catch (e: any) {
    showToast('设置失败：' + (e?.message || e), 'err')
  } finally {
    autostartSaving.value = false
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onIfaceKeydown)
  window.addEventListener('scroll', onPageScroll, true)
  await Promise.all([models.fetch(), settings.fetch(), agents.fetch(), loadLaunchMode(), loadAutostart()])
  syncCtxSettings()
  syncPermSettings()
  // 存储路径
  try { storagePaths.value = await api.storage.paths() } catch {}
  storageLocation.value = storagePaths.value?.root || ''
  loadBackgrounds()
  // 数据回来后布局高度会变，重新校准一次目录高亮
  setTimeout(onPanelsScroll, 60)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onIfaceKeydown)
  window.removeEventListener('scroll', onPageScroll, true)
  clearBgTimers()
})

function openPath(p: string) { api.storage.open(p).catch(() => {}) }
// 将绝对路径转换为相对存储位置（root）的相对路径，供“路径”列展示
function relPath(p: string): string {
  const root = storagePaths.value?.root
  if (!root || !p) return p
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '')
  const r = norm(root)
  const pp = norm(p)
  if (pp.toLowerCase() === r.toLowerCase()) return '.'
  if (pp.toLowerCase().startsWith(r.toLowerCase() + '/')) {
    return pp.slice(r.length + 1)
  }
  return p
}
function openExplorer(p: string) {
  explorerInitial.value = p
  explorerOpen.value = true
}

async function persistStorageLocation() {
  const val = storageLocation.value.trim()
  if (!val) return
  if (val === storagePaths.value?.root) return
  storageSaving.value = true
  try {
    const r = await api.storage.setLocation(val)
    storagePaths.value = r?.paths ?? storagePaths.value
    storageLocation.value = storagePaths.value?.root || val
    // 前端同步刷新 settings / capabilities，让其它页也能立即感知
    await Promise.all([settings.fetch(), models.fetch()])
  } catch (e: any) {
    alert('切换存储位置失败：' + (e?.message || e))
  } finally {
    storageSaving.value = false
  }
}

async function browseStorage() {
  try {
    const r = await api.storage.pickFolder(storageLocation.value || storagePaths.value?.root)
    // 只把所选目录填进输入框，不立即切换：切换/迁移由按钮或回车显式触发
    if (r?.path) storageLocation.value = r.path
  } catch (e: any) {
    alert('打开选择器失败：' + (e?.message || e))
  }
}

// —— 迁移用户数据到指定位置 ——
function fmtBytes(n: number): string {
  const v = Number(n) || 0
  if (v < 1024) return v + ' B'
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB'
  if (v < 1024 * 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB'
  return (v / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}
const normPath = (s: string) => String(s || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()

async function openMigrate() {
  // 第 1 步：弹出系统文件夹选择器（初始定位到当前存储位置）
  let target = storageLocation.value.trim()
  try {
    const r = await api.storage.pickFolder(target || storagePaths.value?.root || '', '选择要迁移到的目标文件夹')
    if (!r?.path) return  // 用户取消，静默关闭
    target = r.path.trim()
    storageLocation.value = target  // 同时更新输入框显示
  } catch (e: any) {
    alert('打开选择器失败：' + (e?.message || e))
    return
  }
  if (!target) return
  // 第 2 步：路径校验
  if (storagePaths.value?.root && normPath(target) === normPath(storagePaths.value.root)) {
    showToast('该位置就是当前存储位置，无需迁移', 'err')
    return
  }
  // 第 3 步：弹迁移确认弹窗 + dry-run 预估
  migrateTarget.value = target
  migratePlan.value = null
  migrateScanning.value = true
  migrateOpen.value = true
  try {
    migratePlan.value = await api.storage.migrate(target, true)
    if (migratePlan.value?.target) migrateTarget.value = migratePlan.value.target
  } catch (e: any) {
    migrateOpen.value = false
    alert('检查目标位置失败：' + (e?.message || e))
  } finally {
    migrateScanning.value = false
  }
}

function closeMigrate() {
  if (migrating.value) return
  migrateOpen.value = false
  migratePlan.value = null
}

async function doMigrate() {
  if (migrating.value || migrateScanning.value || !migrateTarget.value) return
  migrating.value = true
  try {
    const r = await api.storage.migrate(migrateTarget.value, false, false)
    storagePaths.value = r?.paths ?? null
    if (!storagePaths.value) {
      try { storagePaths.value = await api.storage.paths() } catch {}
    }
    storageLocation.value = storagePaths.value?.root || migrateTarget.value
    // 保存源路径，供删除确认弹窗使用
    const sourcePath = r?.source || ''
    migrateOpen.value = false
    migratePlan.value = null
    // 新位置的 settings / 模型接口可能不同，同步刷新
    await Promise.all([settings.fetch(), models.fetch()])
    showToast(`已迁移 ${r?.copied ?? 0} 个文件（${fmtBytes(r?.bytes ?? 0)}）到 ${storagePaths.value?.root || migrateTarget.value}`)
    // 迁移完成，弹出是否删除原位置数据的询问
    if (sourcePath && !r?.source_deleted) {
      deleteConfirmSource.value = sourcePath
      deleteConfirmOpen.value = true
    }
  } catch (e: any) {
    showToast('迁移失败：' + (e?.message || e), 'err')
  } finally {
    migrating.value = false
  }
}

/** 用户确认删除原位置数据 */
async function confirmDeleteSource() {
  if (deleteConfirmDeleting.value) return
  deleteConfirmDeleting.value = true
  try {
    await api.storage.deleteSource(deleteConfirmSource.value)
    deleteConfirmOpen.value = false
    deleteConfirmSource.value = ''
    showToast('原位置数据已删除')
  } catch (e: any) {
    showToast('删除失败：' + (e?.message || e), 'err')
  } finally {
    deleteConfirmDeleting.value = false
  }
}

function skipDeleteSource() {
  deleteConfirmOpen.value = false
  deleteConfirmSource.value = ''
}

// —— 模型接口 ——
// 接口 URL 预设（点击自动填入 URL 与名称）
const BUILTIN_PRESETS = [
  { name: 'DeepSeek', url: 'https://api.deepseek.com/v1' },
  { name: '日日新', url: 'https://token.sensenova.cn/v1' },
  { name: '火山引擎', url: 'https://ark.cn-beijing.volces.com/api/v3' },
  { name: '阿里云百炼', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { name: '华为云 MaaS', url: 'https://api.modelarts-maas.com/v2' },
  { name: '本地 Ollama', url: 'http://localhost:11434/v1' },
]
const PRESETS_KEY = 'agent-cluster.iface.urlPresets'
function loadCustomPresets(): { name: string; url: string }[] {
  try {
    const v = JSON.parse(localStorage.getItem(PRESETS_KEY) || '[]')
    return Array.isArray(v) ? v.filter((p: any) => p?.name && p?.url) : []
  } catch { return [] }
}
const customPresets = ref<{ name: string; url: string }[]>(loadCustomPresets())
function persistCustomPresets() { localStorage.setItem(PRESETS_KEY, JSON.stringify(customPresets.value)) }
// 用 JS 维护 hover 态（backdrop-filter 遮罩上 CSS :hover 会粘滞不消失）
const hoverPreset = ref<{ name: string; url: string } | 'add' | null>(null)
function applyPreset(p: { name: string; url: string }, e?: MouseEvent) {
  ifaceForm.value.url = p.url
  ifaceForm.value.name = p.name
  ;(e?.currentTarget as HTMLElement | null)?.blur()
}
const showPresetForm = ref(false)
const presetName = ref('')
const presetUrl = ref('')
function openPresetForm() {
  showPresetForm.value = !showPresetForm.value
  if (showPresetForm.value) { presetName.value = ifaceForm.value.name || ''; presetUrl.value = ifaceForm.value.url || '' }
}
function addPreset() {
  const n = presetName.value.trim()
  const u = presetUrl.value.trim()
  if (!n || !/^https?:\/\//i.test(u)) return
  customPresets.value.push({ name: n, url: u })
  persistCustomPresets()
  showPresetForm.value = false
  applyPreset({ name: n, url: u })
}
function removePreset(i: number) {
  customPresets.value.splice(i, 1)
  persistCustomPresets()
}
// 默认模型下拉选项：解析「可用模型」字符串
const ifaceModelOptions = computed(() => (ifaceForm.value.models_str || '').split(',').map((s: string) => s.trim()).filter(Boolean))
function openNew() {
  ifaceForm.value = { name: '', url: '', api_key: '', default_model: '', models_str: '', models: [], description: '', windows_str: '' }
  fetchMsg.value = null
  showPresetForm.value = false; confirmClose.value = false
  dialogBaseline.value = ifaceSnapshot()
  isEditing.value = false; showDialog.value = true
}
function edit(i: any) {
  ifaceForm.value = { ...i, models_str: (i.models || []).join(','), windows_str: Object.entries(i.model_windows || {}).map(([k, v]) => `${k}:${v}`).join(', ') }
  fetchMsg.value = null
  showPresetForm.value = false; confirmClose.value = false
  dialogBaseline.value = ifaceSnapshot()
  isEditing.value = true; showDialog.value = true
}
function closeDialog() { showDialog.value = false; ifaceForm.value = {}; confirmClose.value = false; showPresetForm.value = false }
// —— 弹窗关闭确认（✕ / ESC）：有未保存修改时三按钮确认 ——
const dialogBaseline = ref('')
const confirmClose = ref(false)
const IFACE_FIELDS = ['name', 'url', 'api_key', 'default_model', 'models_str', 'windows_str', 'description']
function ifaceSnapshot() {
  const f: any = ifaceForm.value || {}
  return JSON.stringify(IFACE_FIELDS.map(k => f[k] || ''))
}
function isIfaceDirty() { return ifaceSnapshot() !== dialogBaseline.value }
function requestClose() {
  if (isIfaceDirty()) { confirmClose.value = true; return }
  closeDialog()
}
async function closeWithSave() {
  confirmClose.value = false
  await saveIface()
}
function closeWithoutSave() {
  confirmClose.value = false
  closeDialog()
}
function onIfaceKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !showDialog.value) return
  if (confirmClose.value) { confirmClose.value = false; return }
  if (showPresetForm.value) { showPresetForm.value = false; return }
  requestClose()
}
/** 窗口值解析：128000 / 128k / 1m / 2b → token 数 */
function parseWindowTokens(v: string): number {
  const m = /^(\d+(?:\.\d+)?)\s*([kmb])?$/i.exec((v || '').trim())
  if (!m) return 0
  const mult: Record<string, number> = { k: 1e3, m: 1e6, b: 1e9 }
  return Math.round(parseFloat(m[1]) * (mult[(m[2] || '').toLowerCase()] || 1))
}
async function saveIface() {
  ifaceForm.value.models = (ifaceForm.value.models_str || '').split(',').map((s: string) => s.trim()).filter(Boolean)
  const mw: Record<string, number> = {}
  ;(ifaceForm.value.windows_str || '').split(',').map((s: string) => s.trim()).filter(Boolean).forEach((pair: string) => {
    const idx = pair.lastIndexOf(':')
    if (idx <= 0) return
    const k = pair.slice(0, idx).trim()
    const n = parseWindowTokens(pair.slice(idx + 1))
    if (k && n > 0) mw[k] = n
  })
  const { models_str, windows_str, ...clean } = ifaceForm.value
  clean.model_windows = mw
  // 默认模型留空：回退第一个可用模型；仍为空则不传，由后端填默认值
  clean.default_model = (clean.default_model || '').trim() || (clean.models || [])[0] || ''
  if (!clean.default_model) delete clean.default_model
  if (isEditing.value) await models.update(ifaceForm.value.id, clean)
  else await models.create(clean)
  closeDialog(); await models.fetch()
}
async function setDefaultIface(id: number) { await settings.update({ default_model_interface: id }) }
async function fetchIfaceModels() {
  if (fetchingModels.value) return
  const url = (ifaceForm.value.url || '').trim()
  if (!url) { fetchMsg.value = { text: '请先填写接口 URL', kind: 'err' }; return }
  fetchingModels.value = true
  fetchMsg.value = null
  try {
    const r = await api.models.fetchModels({ url, api_key: ifaceForm.value.api_key || '' })
    const list = r?.models || []
    if (!list.length) { fetchMsg.value = { text: '未获取到模型列表', kind: 'err' }; return }
    ifaceForm.value.models_str = list.join(',')
    if (!ifaceForm.value.default_model || !list.includes(ifaceForm.value.default_model)) {
      ifaceForm.value.default_model = list[0]
    }
    fetchMsg.value = { text: `已获取 ${list.length} 个模型，可用模型与默认模型已自动填写`, kind: 'ok' }
  } catch (e: any) {
    fetchMsg.value = { text: `获取失败：${e?.message || e}`, kind: 'err' }
  } finally {
    fetchingModels.value = false
  }
}

// —— 历史上下文压缩设置 ——
const ctxThreshold = ref(0.8)
const ctxKeepRecent = ref(20)
const ctxDefaultWindow = ref(128000)
function syncCtxSettings() {
  const d = settings.data || {}
  ctxThreshold.value = Number(d.context_compress_threshold) || 0.8
  ctxKeepRecent.value = Number(d.context_keep_recent) || 20
  ctxDefaultWindow.value = Number(d.context_window_default) || 128000
}
async function saveCtx(k: string, v: any) {
  try { await settings.update({ [k]: v }) } catch { /* 保存失败静默，下次进入回读 */ }
}
const ctxCompressOn = computed(() => settings.data.context_compress_enabled !== false)

// —— 权限设置（操作权限模式 + 工作范围，执行端见 backend/core/permissions.py）——
const permMode = ref<'manual' | 'auto' | 'full'>('auto')
const scopeMode = ref<'workspace' | 'custom' | 'global'>('workspace')
const scopePathsText = ref('')
const scopeSaving = ref(false)

function syncPermSettings() {
  const d = settings.data || {}
  permMode.value = (d.permission_mode === 'manual' || d.permission_mode === 'full') ? d.permission_mode : 'auto'
  scopeMode.value = (d.work_scope_mode === 'custom' || d.work_scope_mode === 'global') ? d.work_scope_mode : 'workspace'
  scopePathsText.value = Array.isArray(d.scope_paths) ? d.scope_paths.join('\n') : ''
}

const permModeDesc = computed(() => {
  if (permMode.value === 'manual') return {
    title: '每次执行危险操作都先弹窗问你',
    sub: '写/删文件、运行命令、调用插件、派生子智能体前都会暂停等待你点「允许」；120 秒未处理按拒绝处理。最安全，但会打断智能体节奏。',
  }
  if (permMode.value === 'full') return {
    title: '不做任何检查，全部直接放行',
    sub: '危险操作与工作范围限制同时关闭，智能体可任意操作这台电脑。仅在完全信任的自动化场景使用。',
  }
  return {
    title: '危险操作自动放行（推荐）',
    sub: '不打断运行；工作范围限制仍然生效，越界写文件 / 跑命令会被拒绝并告知智能体原因。',
  }
})

const scopeModeDesc = computed(() => {
  if (scopeMode.value === 'global') return {
    title: '不限制目录，整台电脑可写可执行',
    sub: '智能体可以在任意路径改文件、运行命令。',
  }
  if (scopeMode.value === 'custom') return {
    title: '只允许在下方列出的目录里操作',
    sub: '读取不受限；写文件 / 删除 / 运行命令的目录必须落在指定范围内。',
  }
  return {
    title: '只允许在当前工作区内操作',
    sub: '工作区 = 会话 / 画布选中的工作目录，未设置时为全局默认工作区（设置页上方），再没有则为程序所在目录。',
  }
})

async function setPermMode(v: 'manual' | 'auto' | 'full') {
  permMode.value = v
  try { await settings.update({ permission_mode: v }) } catch { syncPermSettings() }
}
async function setScopeMode(v: 'workspace' | 'custom' | 'global') {
  scopeMode.value = v
  try { await settings.update({ work_scope_mode: v }) } catch { syncPermSettings() }
}
async function saveScopePaths() {
  scopeSaving.value = true
  try {
    const list = scopePathsText.value.split('\n').map(s => s.trim()).filter(Boolean)
    await settings.update({ scope_paths: list })
    scopePathsText.value = list.join('\n')
    showToast(list.length ? `已保存 ${list.length} 个范围目录` : '已清空指定范围（回退「仅限工作区」）')
  } catch (e: any) {
    showToast('保存失败：' + (e?.message || e), 'err')
  } finally {
    scopeSaving.value = false
  }
}

// —— 外观设置 ——
const avatarMode = computed({
  get() { return settings.data.avatar_mode || 'differentiated' },
  set(v: string) { settings.update({ avatar_mode: v }) }
})

// 界面字体：走 layout store（localStorage 持久化，与主题/文字大小同模式）
const fontFamilyId = computed({
  get() { return layout.fontFamily },
  set(v: string) { layout.setFontFamily(v) },
})
const currentFontStack = computed(() => FONT_FAMILIES.find(f => f.id === layout.fontFamily)?.stack || '')

// —— 对话区显示 ——
// 其他智能体发给当前智能体的消息：是否显示、显示在左侧还是右侧
const showOtherAgents = computed({
  get() { return settings.data.chat_show_other_agents !== false },
  set(v: boolean) { settings.update({ chat_show_other_agents: v }) }
})
const otherAgentsSide = computed({
  get() { return settings.data.chat_other_agents_side === 'left' ? 'left' : 'right' },
  set(v: string) { settings.update({ chat_other_agents_side: v }) }
})
// 对话区消息头像大小（px），默认 24
const chatAvatarSize = computed({
  get() {
    const v = Number(settings.data.chat_avatar_size)
    return Number.isFinite(v) && v >= 16 && v <= 48 ? v : 24
  },
  set(v: number) { settings.update({ chat_avatar_size: v }) }
})

function avatarPreviewGradient(name: string): string {
  if (avatarMode.value === 'unified') {
    return 'linear-gradient(135deg, var(--accent), var(--accent-2))'
  }
  return getAvatarGradient(name)
}

// —— 提示音设置 ——
const soundEnabled = computed({
  get() { return settings.data.sound_enabled !== false },
  set(v: boolean) { settings.update({ sound_enabled: v }) }
})
function toggleSound() { soundEnabled.value = !soundEnabled.value }

// —— 默认智能体（新建对话未选中智能体时自动选中） ——
const defaultAgentId = computed({
  get() {
    const v = Number(settings.data.default_agent_id)
    // 智能体已删除 → 视为未设置（下拉不残留无效选项）
    if (!Number.isFinite(v) || v <= 0 || !agents.list.some((a: any) => a.id === v)) return 0
    return v
  },
  set(v: number) { settings.update({ default_agent_id: v > 0 ? v : null }) },
})
/** 下拉选项：按分组 optgroup 分列（与对话区智能体下拉同结构） */
const agentGroupOptions = computed(() => {
  const out: { id: 'all' | number; name: string; agents: any[] }[] = [
    { id: 'all', name: '未分组', agents: agents.list.filter((a: any) => a.group_id == null) },
  ]
  for (const g of agents.groups) {
    out.push({ id: g.id, name: g.name, agents: agents.list.filter((a: any) => a.group_id === g.id) })
  }
  return out
})
</script>

<template>
  <div class="settings-root region-settings">
    <div class="top glass">
      <!-- 返回按钮是独立区域，不能拖动 -->
      <RouterLink to="/" class="btn ghost">← 返回</RouterLink>
      <!-- 标题 + 中间空白 合起来是可拖动区（pywebview 祖先链严格匹配） -->
      <div class="top-drag pywebview-drag-region">
        <h2>设置中心</h2>
      </div>
    </div>

    <div class="settings-body">
      <!-- 左侧目录：点击快速跳转到对应设置区块，滚动时自动高亮（.glass + z-index 抬到背景层之上） -->
      <aside class="settings-toc glass">
        <div class="toc-head">目录</div>
        <nav class="toc-list">
          <button
            v-for="s in sectionList"
            :key="s.id"
            type="button"
            class="toc-item"
            :class="{ active: activeSection === s.id }"
            @click="jumpToSection(s.id)"
          >
            <span class="toc-bar"></span>
            <span class="toc-text">{{ s.name }}</span>
          </button>
        </nav>
      </aside>

      <div class="panels" ref="panelsRef" @scroll.passive="onPanelsScroll">
      <!-- 模型接口 -->
      <div id="sec-model" class="panel glass">
        <div class="panel-title">
          <span>模型接口</span>
          <button class="btn" @click="openNew">+ 新建接口</button>
        </div>
        <table class="tbl" v-if="models.list.length">
          <thead><tr><th>名称</th><th>URL</th><th>默认模型</th><th>可用模型</th><th style="width:240px"></th></tr></thead>
          <tbody>
            <tr v-for="m in models.list" :key="m.id">
              <td>
                {{ m.name }}
                <span class="tag" v-if="settings.data.default_model_interface === m.id">默认</span>
              </td>
              <td class="mono">{{ m.url }}</td>
              <td>{{ m.default_model }}</td>
              <td class="mono sm-cell">{{ (m.models || []).join(', ') || '—' }}</td>
              <td class="actions">
                <button class="btn ghost" v-if="settings.data.default_model_interface !== m.id" @click="setDefaultIface(m.id)">设为默认</button>
                <button class="btn ghost" @click="edit(m)">编辑</button>
                <button class="btn ghost" @click="models.remove(m.id)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!models.list.length" class="empty">还没有接口，点击右上角"新建接口"</div>
      </div>

      <!-- 历史上下文压缩 -->
      <div id="sec-ctx" class="panel glass">
        <div class="panel-title">
          <span><AppIcon name="save" /> 历史上下文压缩</span>
          <span class="hint">达到压缩线时自动把较早历史压缩成摘要</span>
        </div>
        <div class="cap-block">
          <div class="asr-toggle">
            <button class="asr-switch" :class="{ on: ctxCompressOn }" @click="saveCtx('context_compress_enabled', !ctxCompressOn)">
              <span class="asr-switch-knob"></span>
            </button>
            <div class="sound-desc">
              <div class="sound-main">{{ ctxCompressOn ? '已启用' : '已关闭' }}</div>
              <div class="sound-sub">{{ ctxCompressOn ? '发送消息时检测：上下文达到压缩线 → 较早历史压缩成摘要，最近对话原样保留' : '不做任何压缩，历史全量进入模型上下文' }}</div>
            </div>
          </div>
          <div class="ctx-cfg-grid">
            <label>压缩线（窗口百分比）<input class="input" type="number" min="0.3" max="0.95" step="0.05" v-model.number="ctxThreshold" @change="saveCtx('context_compress_threshold', ctxThreshold)" /></label>
            <label>保留最近对话（条）<input class="input" type="number" min="4" step="1" v-model.number="ctxKeepRecent" @change="saveCtx('context_keep_recent', ctxKeepRecent)" /></label>
            <label>默认上下文窗口（token）<input class="input" type="number" min="8000" step="1000" v-model.number="ctxDefaultWindow" @change="saveCtx('context_window_default', ctxDefaultWindow)" /></label>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            各模型的窗口在上方接口编辑中按「模型:token」配置（支持 128k / 1m 写法）；未配置的模型使用此默认窗口。
            对话区发送按钮上方的数值边框颜色表示达到压缩线的进度（绿 → 黄 → 橙 → 红）。
          </div>
        </div>
      </div>

      <!-- 默认智能体 -->
      <div id="sec-agent" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><rect width="18" height="10" x="3" y="7" rx="2"/><path d="M12 11v4"/><path d="M8 11v4"/><path d="M16 11v4"/></svg>默认智能体</span>
          <span class="hint">新建对话时自动选中</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">默认智能体</span>
          </div>
          <select class="input" v-model.number="defaultAgentId" style="margin-top: 10px">
            <option :value="0">未设置（不自动选择）</option>
            <optgroup v-for="g in agentGroupOptions" :key="g.id" :label="g.name">
              <option v-for="a in g.agents" :key="a.id" :value="a.id">{{ a.name }}</option>
            </optgroup>
          </select>
          <div class="hint-empty" style="margin-top: 8px">
            新建对话（主窗口与临时对话小窗的「＋ 新建」）未选中智能体时，自动选中该智能体；已选中智能体时保持不变。集群 / 工作区分组内新建不预设智能体，不受此项影响。
          </div>
        </div>
      </div>

      <!-- 权限设置（危险操作授权 + 可操作范围，后端执行端见 core/permissions.py） -->
      <div id="sec-perm" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>权限设置</span>
          <span class="hint">限制智能体的危险操作与可操作范围</span>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">操作权限</span>
            <span class="hint">智能体执行危险操作（写/删文件、运行命令、插件、派生子智能体…）时如何授权</span>
          </div>
          <div class="appearance-row">
            <label class="radio-label" :class="{ active: permMode === 'manual' }" @click="setPermMode('manual')">
              <input type="radio" name="permissionMode" value="manual" :checked="permMode === 'manual'" />
              <span class="radio-dot"></span>
              <span class="radio-text">手动确认</span>
            </label>
            <label class="radio-label" :class="{ active: permMode === 'auto' }" @click="setPermMode('auto')">
              <input type="radio" name="permissionMode" value="auto" :checked="permMode === 'auto'" />
              <span class="radio-dot"></span>
              <span class="radio-text">自动审核</span>
            </label>
            <label class="radio-label" :class="{ active: permMode === 'full' }" @click="setPermMode('full')">
              <input type="radio" name="permissionMode" value="full" :checked="permMode === 'full'" />
              <span class="radio-dot"></span>
              <span class="radio-text">完全权限</span>
            </label>
          </div>
          <div class="sound-desc">
            <div class="sound-main">{{ permModeDesc.title }}</div>
            <div class="sound-sub">{{ permModeDesc.sub }}</div>
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">工作范围</span>
            <span class="hint">智能体可以在哪些目录里改文件、跑命令</span>
          </div>
          <div class="appearance-row">
            <label class="radio-label" :class="{ active: scopeMode === 'workspace' }" @click="setScopeMode('workspace')">
              <input type="radio" name="workScopeMode" value="workspace" :checked="scopeMode === 'workspace'" />
              <span class="radio-dot"></span>
              <span class="radio-text">仅限工作区</span>
            </label>
            <label class="radio-label" :class="{ active: scopeMode === 'custom' }" @click="setScopeMode('custom')">
              <input type="radio" name="workScopeMode" value="custom" :checked="scopeMode === 'custom'" />
              <span class="radio-dot"></span>
              <span class="radio-text">指定范围</span>
            </label>
            <label class="radio-label" :class="{ active: scopeMode === 'global' }" @click="setScopeMode('global')">
              <input type="radio" name="workScopeMode" value="global" :checked="scopeMode === 'global'" />
              <span class="radio-dot"></span>
              <span class="radio-text">电脑全局</span>
            </label>
          </div>
          <div class="sound-desc">
            <div class="sound-main">{{ scopeModeDesc.title }}</div>
            <div class="sound-sub">{{ scopeModeDesc.sub }}</div>
          </div>

          <div v-if="scopeMode === 'custom'" class="perm-paths">
            <textarea class="input perm-paths-input" v-model="scopePathsText" rows="4"
                      spellcheck="false"
                      placeholder="每行一个绝对目录，例如：&#10;D:\Projects\我的工作区&#10;E:\素材"></textarea>
            <div class="perm-paths-actions">
              <button class="btn" :disabled="scopeSaving" @click="saveScopePaths">
                {{ scopeSaving ? '保存中…' : '保存范围' }}
              </button>
              <span class="hint">目录不存在时会自动忽略；留空则回退到「仅限工作区」</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 外观设置 -->
      <div id="sec-appearance" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg>外观设置</span>
          <span class="hint">界面外观偏好</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">头像风格</span>
          </div>
          <div class="appearance-row">
            <label class="radio-label" :class="{ active: avatarMode === 'differentiated' }">
              <input type="radio" name="avatarMode" value="differentiated" v-model="avatarMode" />
              <span class="radio-dot"></span>
              <span class="radio-text">差异化</span>
            </label>
            <label class="radio-label" :class="{ active: avatarMode === 'unified' }">
              <input type="radio" name="avatarMode" value="unified" v-model="avatarMode" />
              <span class="radio-dot"></span>
              <span class="radio-text">统一化</span>
            </label>
          </div>
          <div class="avatar-preview">
            <div class="preview-item">
              <div class="preview-label">智能体 A</div>
              <div class="preview-avatar" :style="{ background: avatarPreviewGradient('Alpha') }">A</div>
            </div>
            <div class="preview-item">
              <div class="preview-label">智能体 B</div>
              <div class="preview-avatar" :style="{ background: avatarPreviewGradient('Beta') }">B</div>
            </div>
            <div class="preview-item">
              <div class="preview-label">用户</div>
              <div class="preview-avatar user" :style="{ background: 'linear-gradient(135deg, #f97316, #ec4899)' }">U</div>
            </div>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            差异化：每个智能体根据名称生成不同颜色
            <br />
            统一化：所有智能体使用统一的蓝紫色渐变
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">界面字体</span>
            <button class="btn sm ghost" @click="layout.resetFontFamily" :disabled="layout.fontFamily === FONT_FAMILY_DEFAULT">默认</button>
          </div>
          <select class="input font-select" v-model="fontFamilyId" :style="{ fontFamily: currentFontStack }">
            <option v-for="f in FONT_FAMILIES" :key="f.id" :value="f.id" :style="{ fontFamily: f.stack }">{{ f.name }}</option>
          </select>
          <div class="hint-empty" style="margin-top: 8px">
            全局正文与消息字体；未安装的字体自动回退，选择后立即生效并自动保存
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">文字大小</span>
            <button class="btn sm ghost" @click="layout.resetFontScale" :disabled="layout.fontScale === 1">默认</button>
          </div>
          <div class="font-scale-row">
            <span class="font-scale-edge">小</span>
            <input class="font-scale-slider" type="range"
                   :min="FONT_SCALE_MIN" :max="FONT_SCALE_MAX" :step="FONT_SCALE_STEP"
                   :value="layout.fontScale"
                   @input="layout.setFontScale(Number(($event.target as HTMLInputElement).value))" />
            <span class="font-scale-edge">大</span>
            <span class="font-scale-value">{{ Math.round(layout.fontScale * 100) }}%</span>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            只调整文字大小，不影响界面布局；设置自动保存，刷新后仍然生效
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">对话区显示</span>
          </div>
          <div class="asr-toggle">
            <button class="asr-switch" :class="{ on: showOtherAgents }" @click="showOtherAgents = !showOtherAgents">
              <span class="asr-switch-knob"></span>
            </button>
            <span class="asr-toggle-text">{{ showOtherAgents ? '显示其他智能体消息' : '隐藏其他智能体消息' }}</span>
          </div>
          <div class="asr-toggle" style="margin-top: 10px">
            <button class="asr-switch" :class="{ on: settings.ctxBadgeEnabled }" @click="settings.setCtxBadgeEnabled(!settings.ctxBadgeEnabled)">
              <span class="asr-switch-knob"></span>
            </button>
            <span class="asr-toggle-text">{{ settings.ctxBadgeEnabled ? '显示上下文长度指示' : '隐藏上下文长度指示' }}</span>
          </div>
          <div class="appearance-row" style="margin-top: 10px">
            <label class="radio-label" :class="{ active: otherAgentsSide === 'left', disabled: !showOtherAgents }">
              <input type="radio" name="otherAgentsSide" value="left" v-model="otherAgentsSide" :disabled="!showOtherAgents" />
              <span class="radio-dot"></span>
              <span class="radio-text">显示在左侧</span>
            </label>
            <label class="radio-label" :class="{ active: otherAgentsSide === 'right', disabled: !showOtherAgents }">
              <input type="radio" name="otherAgentsSide" value="right" v-model="otherAgentsSide" :disabled="!showOtherAgents" />
              <span class="radio-dot"></span>
              <span class="radio-text">显示在右侧</span>
            </label>
          </div>
          <div class="font-scale-row" style="margin-top: 10px">
            <span class="font-scale-edge">小</span>
            <input class="font-scale-slider" type="range" min="16" max="48" step="2"
                   :value="chatAvatarSize"
                   @input="chatAvatarSize = Number(($event.target as HTMLInputElement).value)" />
            <span class="font-scale-edge">大</span>
            <span class="font-scale-value">{{ chatAvatarSize }}px</span>
            <button class="btn sm ghost" :disabled="chatAvatarSize === 24" @click="chatAvatarSize = 24">默认</button>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            控制当前智能体视角下，其他智能体发给它的消息是否显示、以及显示在左侧还是右侧；头像大小调整对话区消息旁的用户与智能体头像
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">配色方案</span>
          </div>
          <div class="theme-grid">
            <div v-for="t in themeOptions" :key="t.id"
                 class="theme-card" :class="{ active: layout.theme === t.id }"
                 @click="layout.setTheme(t.id)">
              <div class="theme-preview" :style="{ background: 'radial-gradient(ellipse at 30% 20%, ' + t.bgGrad + ' 0%, ' + t.bg + ' 70%)' }">
                <div class="tp-bar" :style="{ background: t.surface }">
                  <span class="tp-dot" :style="{ background: t.accent }"></span>
                  <span class="tp-text" :style="{ color: t.text }">Aa 文字</span>
                </div>
                <div class="tp-chip" :style="{ background: t.accent }"></div>
              </div>
              <div class="theme-name">{{ t.name }}</div>
            </div>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            切换全局配色方案，设置自动保存，刷新后仍然生效
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">背景</span>
            <div class="bg-upload-btns">
              <button class="btn sm ghost" @click="pickBackgroundFiles('image')" :disabled="bgUploading"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>图片</button>
              <button class="btn sm ghost" @click="pickBackgroundFiles('video')" :disabled="bgUploading"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>视频</button>
              <button class="btn sm ghost" @click="pickBackgroundFiles('animation')" :disabled="bgUploading"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M12 3v18"/><path d="m16.6 7.6-2.4 4.4-4.8 5.4 2.4-4.4 4.8-5.4"/></svg>动画</button>
            </div>
          </div>
          <div class="bg-grid">
            <div class="bg-card" :class="{ active: layout.backgroundId === null }" @click="layout.setBackground(null)">
              <div class="bg-preview" :style="defaultBgStyle"></div>
              <div class="bg-name">默认渐变</div>
            </div>
            <div v-for="bg in bgList" :key="bg.id" class="bg-card" :class="{ active: layout.backgroundId === bg.id }"
                 @click="layout.setBackground(bg.id)"
                 @mouseenter="onBgEnter(bg, $event)" @mouseleave="onBgLeave">
              <div class="bg-preview" :style="bgThumbStyle(bg)">
                <iframe v-if="bg.type === 'animation' && bgHoverId === bg.id" class="bg-live"
                        :src="bgRawUrl(bg)" tabindex="-1" scrolling="no" aria-hidden="true"></iframe>
                <video v-else-if="bg.type === 'video' && bgHoverId === bg.id" class="bg-live"
                       :src="bgRawUrl(bg)" muted loop autoplay playsinline></video>
                <video v-else-if="bg.type === 'video'" class="bg-live"
                       :src="bgRawUrl(bg) + '#t=0.2'" muted preload="auto" playsinline></video>
                <span class="bg-type-tag">{{ bg.type === 'animation' ? '动画' : bg.type === 'video' ? '视频' : '图片' }}</span>
                <button v-if="!bg.builtin" class="bg-del" @click.stop="deleteBackground(bg)" title="删除"><AppIcon name="x" /></button>
              </div>
              <div class="bg-name">{{ bg.name }}</div>
            </div>
          </div>
          <Teleport to="body">
            <div v-if="magnify" class="bg-magnify"
                 :style="{ left: magnify.x + 'px', top: magnify.y + 'px', width: magnify.w + 'px', height: magnify.h + 'px', ...magnify.base }">
              <iframe :src="magnify.url" tabindex="-1" scrolling="no" aria-hidden="true"></iframe>
              <div class="bg-magnify-name">{{ magnify.name }} · 实时预览</div>
            </div>
          </Teleport>
          <div v-if="layout.backgroundId" class="bg-sliders">
            <div class="bg-slider-row">
              <span class="bg-slider-label">不透明度</span>
              <input class="font-scale-slider" type="range" min="0.1" max="1" step="0.05"
                     :value="layout.backgroundOpacity"
                     @input="layout.setBackgroundOpacity(Number(($event.target as HTMLInputElement).value))" />
              <span class="bg-slider-value">{{ Math.round(layout.backgroundOpacity * 100) }}%</span>
            </div>
            <div class="bg-slider-row">
              <span class="bg-slider-label">模糊</span>
              <input class="font-scale-slider" type="range" min="0" max="30" step="1"
                     :value="layout.backgroundBlur"
                     @input="layout.setBackgroundBlur(Number(($event.target as HTMLInputElement).value))" />
              <span class="bg-slider-value">{{ layout.backgroundBlur }}px</span>
            </div>
          </div>
          <input ref="bgFileInput" type="file" multiple style="display:none" @change="onBgFiles" />
          <div class="hint-empty" style="margin-top: 8px">
            选择背景或上传自定义背景（图片 / 视频 / 动画 HTML）；动画背景可由智能体自由生成，规范见项目 doc/spec/background-spec.md
          </div>
        </div>
      </div>

      <!-- 新手引导 -->
      <div id="sec-guide" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>新手引导</span>
          <span class="hint">再次查看引导说明</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">引导方式</span>
          </div>
          <div class="guide-actions">
            <button class="btn" @click="startGuide('flow')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21 3 6"/><line x1="9" x2="9" y1="3" y2="18"/><line x1="15" x2="15" y1="6" y2="21"/></svg>使用流程引导</button>
            <button class="btn" @click="startGuide('region')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M5.121 19 19 5.121"/><path d="M15 5v14"/><path d="M11 7v10"/><path d="M7 9v6"/><path d="M19 5H5"/><path d="M19 5v14H5"/></svg>区域功能引导</button>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            流程引导：按步骤箭头指引各区域用法；区域引导：鼠标移至区域自动显示说明
          </div>
        </div>
      </div>

          <span class="hint">任务结束时的声音反馈</span>
      <div id="sec-sound" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>提示音</span>
          <span class="hint">画布运行结束时的声音反馈</span>
        </div>
        <div class="cap-block">
          <div class="sound-row">
            <button class="asr-switch" :class="{ on: soundEnabled }" @click="toggleSound">
              <span class="asr-switch-knob"></span>
            </button>
            <div class="sound-desc">
              <div class="sound-main">{{ soundEnabled ? '已启用' : '已关闭' }}</div>
              <div class="sound-sub">{{ soundEnabled ? '任务完成播放愉悦音，出现错误播放急促音' : '任务结束时不播放任何声音' }}</div>
            </div>
          </div>
          <div v-if="soundEnabled" class="sound-test-row">
            <button class="btn sm ghost" @click="testSuccess"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>试听完成音</button>
            <button class="btn sm ghost" @click="testError"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>试听错误音</button>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            提示音使用浏览器 Web Audio 合成，无需加载外部音频文件
          </div>
        </div>
      </div>

      <!-- 语音识别 -->
      <div id="sec-asr" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>语音识别</span>
          <span class="hint">SenseVoice 同声传译</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">安装状态</span>
          </div>
          <div v-if="settings.asrAvailable" class="asr-status asr-ok">
            <span class="asr-badge asr-badge-ok"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:2px"><polyline points="20 6 9 17 4 12"/></svg>已安装</span>
            <span class="asr-desc">语音识别已就绪。</span>
          </div>
          <div v-else class="asr-status asr-missing">
            <span class="asr-badge asr-badge-no"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:2px"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>未安装</span>
            <span class="asr-desc">安装后可在对话框中使用 🎤 按钮进行同声传译输入。</span>
          </div>
          <div v-if="!settings.asrAvailable" class="asr-install">
            <div class="asr-install-label">安装命令（在后端目录执行）：</div>
            <div class="asr-cmd">pip install funasr-onnx funasr modelscope onnxscript</div>
            <div class="asr-note">安装后重启后端即可使用。首次识别会自动下载 SenseVoiceSmall 模型（约 936MB）。</div>
          </div>
          <div v-if="settings.asrAvailable" class="asr-toggle">
            <button class="asr-switch" :class="{ on: settings.asrEnabled }" @click="settings.setAsrEnabled(!settings.asrEnabled)">
              <span class="asr-switch-knob"></span>
            </button>
            <span class="asr-toggle-text">{{ settings.asrEnabled ? '已启用' : '已停用' }}</span>
            <span class="asr-toggle-hint">{{ settings.asrEnabled ? '对话框显示 🎤 按钮，可同声传译' : '对话框隐藏 🎤 按钮（模型保留，随时可重新启用）' }}</span>
          </div>
          <div v-if="settings.asrAvailable" class="asr-uninstall-row">
            <button class="btn sm ghost asr-uninstall-btn" @click="uninstallASR" :disabled="asrUninstalling">
              <template v-if="asrUninstalling">卸载中…</template>
              <template v-else><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>卸载模型</template>
            </button>
            <span class="asr-uninstall-hint">删除 SenseVoiceSmall 模型文件（约 936MB），释放磁盘空间。依赖包保留，重新使用时自动重新下载</span>
          </div>
        </div>
      </div>

      <!-- 启动方式 -->
      <div id="sec-launch" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.9A12.8 12.8 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></svg>启动方式</span>
          <span class="hint">下次双击 start.bat 时的行为</span>
        </div>
        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">启动模式</span>
          </div>
          <div class="appearance-row">
            <label class="radio-label" :class="{ active: launchMode === 'client' }" @click="!launchModeSaving && saveLaunchMode('client')">
              <input type="radio" name="launchMode" value="client" :checked="launchMode === 'client'" />
              <span class="radio-dot"></span>
              <span class="radio-text"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><rect width="16" height="10" x="4" y="2" rx="2" ry="2"/><path d="M2 20h20"/><path d="M7 20v-4"/><path d="M17 20v-4"/></svg>桌面客户端</span>
            </label>
            <label class="radio-label" :class="{ active: launchMode === 'web' }" @click="!launchModeSaving && saveLaunchMode('web')">
              <input type="radio" name="launchMode" value="web" :checked="launchMode === 'web'" />
              <span class="radio-dot"></span>
              <span class="radio-text"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>浏览器网页</span>
            </label>
          </div>
          <div class="hint-empty" style="margin-top: 8px">
            桌面客户端：启动后自动打开桌面应用窗口（完整功能）
            <br />
            浏览器网页：启动后端服务后自动打开浏览器 http://localhost:5173
          </div>
        </div>

        <div class="cap-block">
          <div class="cap-header">
            <span class="cap-title">开机自启动</span>
          </div>
          <div class="asr-toggle">
            <button class="asr-switch" :class="{ on: autostartEnabled, disabled: !autostartSupported }"
                    :disabled="!autostartSupported || autostartSaving"
                    @click="toggleAutostart">
              <span class="asr-switch-knob"></span>
            </button>
            <div class="sound-desc">
              <div class="sound-main">
                <span v-if="!autostartSupported">当前系统不支持</span>
                <span v-else-if="autostartSaving">保存中…</span>
                <span v-else-if="autostartEnabled">已开启</span>
                <span v-else>已关闭</span>
              </div>
              <div class="sound-sub" v-if="autostartSupported">
                {{ autostartEnabled ? '电脑开机后自动启动智能体集群（后台静默）' : '电脑开机时不会自动启动' }}
              </div>
              <div class="sound-sub" v-else>
                仅支持 Windows 系统
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 数据存储 -->
      <div id="sec-storage" class="panel glass">
        <div class="panel-title">
          <span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>数据存储</span>
          <span class="hint">v2 · 每条记录一个 json</span>
        </div>
        <div class="storage-loc">
          <label class="storage-loc-label" for="storageLoc">存储位置</label>
          <input id="storageLoc" class="input mono storage-loc-input" v-model="storageLocation"
                 placeholder="默认：%LOCALAPPDATA%\AgentCluster"
                 @blur="persistStorageLocation" @keydown.enter.prevent="persistStorageLocation" />
          <!-- mousedown.prevent：阻止输入框 blur 先把存储位置切走，否则迁移会变成「原地迁移」 -->
          <button class="btn sm ghost" @mousedown.prevent @click="browseStorage" :disabled="storageSaving || migrating" title="选择目标目录（只填入路径，不立即切换）">浏览…</button>
          <button class="btn sm ghost" @mousedown.prevent @click="openMigrate" :disabled="migrating"
                  title="把当前用户数据复制到该位置并切换存储位置（迁移完成后可选择删除原数据）">迁移</button>
        </div>
        <div class="storage-loc-tip">默认位置为用户数据目录（Windows：%LOCALAPPDATA%\AgentCluster，已在项目文件夹之外）；输入框回车/失焦 = 仅切换（按该位置现有数据接管），要把当前数据搬过去请点「迁移」</div>
        <table class="tbl storage-tbl" v-if="storagePaths">
          <thead><tr><th>名称</th><th>类型</th><th>相对路径</th><th>数量</th><th style="width: 120px"></th></tr></thead>
          <tbody>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>配置设置</td>
              <td class="tag-t">单文件</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.settings_file) }}</td>
              <td class="mono">1</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.root)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.root)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>智能体</td>
              <td class="tag-t">每 agent 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.agents_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.agents ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.agents_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.agents_dir)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg>画布（集群编排）</td>
              <td class="tag-t">每 canvas 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.canvases_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.canvases ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.canvases_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.canvases_dir)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>聊天记录</td>
              <td class="tag-t">每 conversation 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.conversations_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.conversations ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.conversations_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.conversations_dir)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M6 8h12"/><path d="M19 12v2a7 7 0 0 1-14 0v-2"/><rect x="12" y="14" width="4" height="8" rx="1"/></svg>模型接口</td>
              <td class="tag-t">每 interface 一个 json</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.model_interfaces_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.model_interfaces ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.model_interfaces_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.model_interfaces_dir)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>自定义技能</td>
              <td class="tag-t">每技能一个文件夹</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.skills_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.skills ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.skills_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.skills_dir)">打开</button></td>
            </tr>
            <tr>
              <td><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M15.5 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.5L15.5 3Z"/><path d="M15 3v6h6"/><path d="M10 12h4"/><path d="M10 16h4"/></svg>自定义插件</td>
              <td class="tag-t">每插件一个文件夹</td>
              <td class="mono sm-cell">{{ relPath(storagePaths.plugins_dir) }}</td>
              <td class="mono">{{ storagePaths.stats?.plugins ?? '-' }}</td>
              <td class="actions"><button class="btn sm ghost" @click="openExplorer(storagePaths.plugins_dir)">编辑</button><button class="btn sm ghost" @click="openPath(storagePaths.plugins_dir)">打开</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!storagePaths" class="empty">后端未返回路径信息</div>
      </div>
      </div>
    </div>

    <!-- 接口编辑弹窗（popup-surface 实底：glass 的 blur+saturate 采样深色遮罩，浅色主题下整窗发灰） -->
    <div class="modal-bg" v-if="showDialog">
      <div class="modal popup-surface">
        <div class="modal-head">
          <div class="modal-title">{{ isEditing ? '编辑接口' : '新建接口' }}</div>
          <button type="button" class="dlg-close" title="关闭" @click="requestClose"><AppIcon name="x" /></button>
        </div>
        <div class="grid">
          <label class="full">
            <span class="field-head"><span>URL</span></span>
            <span class="iface-presets">
              <button type="button" class="iface-preset" :class="{ hover: hoverPreset === p }" v-for="p in BUILTIN_PRESETS" :key="p.name" :title="p.url" @mouseenter="hoverPreset = p" @mouseleave="hoverPreset = null" @click="applyPreset(p, $event)">{{ p.name }}</button>
              <button type="button" class="iface-preset" :class="{ hover: hoverPreset === p }" v-for="(p, i) in customPresets" :key="'c' + i" :title="p.url" @mouseenter="hoverPreset = p" @mouseleave="hoverPreset = null" @click="applyPreset(p, $event)">{{ p.name }}<span class="preset-x" title="删除预设" @click.stop="removePreset(i)"><AppIcon name="x" /></span></button>
              <button type="button" class="iface-preset add" :class="{ hover: hoverPreset === 'add' }" @mouseenter="hoverPreset = 'add'" @mouseleave="hoverPreset = null" @click="openPresetForm">＋预设</button>
            </span>
            <span v-if="showPresetForm" class="preset-form">
              <input class="input" v-model="presetName" placeholder="预设名称" @keydown.enter="addPreset" />
              <input class="input" v-model="presetUrl" placeholder="https://..." @keydown.enter="addPreset" />
              <button type="button" class="btn sm ghost" @click="addPreset">添加</button>
              <button type="button" class="dlg-close" title="取消" @click="showPresetForm = false"><AppIcon name="x" /></button>
            </span>
            <input class="input" v-model="ifaceForm.url" placeholder="https://api.openai.com/v1" />
          </label>
          <label>名称<input class="input" v-model="ifaceForm.name" /></label>
          <label>API Key<input class="input" v-model="ifaceForm.api_key" type="password" /></label>
          <label class="full">
            <span class="field-head">
              <span>可用模型（逗号分隔，如 gpt-4o,gpt-4o-mini,o1）</span>
              <button class="btn sm ghost" :disabled="fetchingModels" @click="fetchIfaceModels">{{ fetchingModels ? '获取中…' : '获取模型列表' }}</button>
            </span>
            <input class="input" v-model="ifaceForm.models_str" />
            <span v-if="fetchMsg" class="fetch-msg" :class="fetchMsg.kind">{{ fetchMsg.text }}</span>
          </label>
          <label class="full">
            <span class="field-head">
              <span>默认模型（可选）</span>
              <select v-if="ifaceModelOptions.length" class="input model-pick" v-model="ifaceForm.default_model">
                <option value="">从可用模型选择</option>
                <option v-for="m in ifaceModelOptions" :key="m" :value="m">{{ m }}</option>
              </select>
            </span>
            <input class="input" v-model="ifaceForm.default_model" placeholder="留空则自动使用第一个可用模型，也可手动输入" />
          </label>
          <label class="full">上下文窗口（可选，格式 模型:token，逗号分隔，如 gpt-4o:128k, glm-4.5:200k；未配置的模型用全局默认窗口）<input class="input" v-model="ifaceForm.windows_str" placeholder="gpt-4o:128k, o1:200k" /></label>
          <label class="full">描述（可选）<input class="input" v-model="ifaceForm.description" /></label>
        </div>
        <div class="actions">
          <button class="btn" @click="saveIface">保存</button>
        </div>
      </div>
    </div>

    <!-- 接口弹窗关闭确认 -->
    <div class="modal-bg confirm-bg" v-if="confirmClose">
      <div class="modal popup-surface confirm-box">
        <div class="modal-title">未保存的修改</div>
        <div class="confirm-text">接口信息已修改但尚未保存，是否保存后关闭？</div>
        <div class="actions">
          <button class="btn ghost" @click="confirmClose = false">取消</button>
          <button class="btn ghost" @click="closeWithoutSave">不保存</button>
          <button class="btn" @click="closeWithSave">保存并关闭</button>
        </div>
      </div>
    </div>

    <!-- 迁移用户数据确认弹窗 -->
    <div class="modal-bg" v-if="migrateOpen">
      <div class="modal popup-surface migrate-box">
        <div class="modal-head">
          <div class="modal-title">迁移用户数据</div>
          <button type="button" class="dlg-close" title="关闭" :disabled="migrating" @click="closeMigrate"><AppIcon name="x" /></button>
        </div>
        <div class="migrate-paths">
          <div class="migrate-path"><span class="migrate-path-label">源</span><span class="mono">{{ migratePlan?.source || storagePaths?.root || '—' }}</span></div>
          <div class="migrate-path"><span class="migrate-path-label">目标</span><span class="mono">{{ migratePlan?.target || migrateTarget }}</span></div>
        </div>
        <div v-if="migrateScanning" class="migrate-sum">正在统计目标位置…</div>
        <div v-else-if="migratePlan" class="migrate-sum">
          将复制 <b>{{ migratePlan.copied }}</b> 个文件（{{ fmtBytes(migratePlan.bytes) }}）<span
            v-if="migratePlan.skipped">；目标已有 <b>{{ migratePlan.skipped }}</b> 个同名文件，将跳过、不覆盖</span><span
            v-else>；目标为空，无同名文件</span>
        </div>
        <div class="migrate-note">
          迁移只复制、不删除：原位置数据完整保留，确认新位置数据无误后可自行清理。<br />
          _legacy 历史归档不在迁移范围；迁移完成后应用立即使用新位置。
        </div>
        <div class="actions">
          <button class="btn ghost" :disabled="migrating" @click="closeMigrate">取消</button>
          <button class="btn" :disabled="migrating || migrateScanning || !migratePlan" @click="doMigrate">
            {{ migrating ? '迁移中…' : '开始迁移' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 迁移成功后：是否删除原位置数据 -->
    <div class="modal-bg" v-if="deleteConfirmOpen">
      <div class="modal popup-surface delete-confirm-box">
        <div class="modal-head">
          <div class="modal-title">迁移完成</div>
        </div>
        <div class="delete-confirm-body">
          <div class="delete-confirm-hint">用户数据已成功迁移到新位置，原位置数据仍完整保留：</div>
          <div class="delete-confirm-path">
            <span class="delete-confirm-path-label">原位置</span>
            <span class="mono">{{ deleteConfirmSource }}</span>
          </div>
          <div class="delete-confirm-question">是否删除原位置数据？</div>
          <div class="delete-confirm-warn">
            删除后将无法恢复。建议先打开新位置确认数据完整，再选择删除。
          </div>
        </div>
        <div class="actions">
          <button class="btn ghost" :disabled="deleteConfirmDeleting" @click="skipDeleteSource">暂不删除</button>
          <button class="btn danger" :disabled="deleteConfirmDeleting" @click="confirmDeleteSource">
            {{ deleteConfirmDeleting ? '删除中…' : '删除原位置数据' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 文件浏览器 -->
    <FileExplorerDialog
      v-model:visible="explorerOpen"
      :initial-path="explorerInitial"
      @saved="() => { storagePaths.value = null; api.storage.paths().then(r => storagePaths.value = r).catch(() => {}) }"
    />
  </div>
</template>

<style scoped>
.settings-root {
  padding: 16px; height: calc(100vh - var(--titlebar-h));
  display: flex; flex-direction: column; min-height: 0; overflow: hidden;
}
/* 顶栏与下方面板区完全同宽同缘对齐（1300px 居中，左侧让出目录列）：按钮和文字贴左，
   顶栏内右侧空白（.top-drag flex:1）留作拖拽区 */
.top {
  padding: 12px 20px; margin: 0 auto 16px; max-width: 1300px; width: 100%;
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
  h2 { font-size: calc(18px * var(--font-scale)); }
}
/* 标题 + 空白拖动区：flex:1 占满剩余空间，撑满高度才能接收鼠标事件 */
.top-drag { flex: 1; align-self: stretch; display: flex; align-items: center; user-select: none; }
/* 顶栏固定常驻，面板区内部滚动（目录 180 + 间距 16 + 面板 1104 = 1300，与顶栏左右同缘） */
.settings-body {
  flex: 1; min-height: 0; width: 100%; max-width: 1300px; margin: 0 auto;
  display: flex; gap: 16px; align-items: stretch;
}
.panels { display: grid; gap: 16px; width: 100%; max-width: 1104px; flex: 1 1 auto; min-height: 0; min-width: 0; overflow-y: auto; align-self: stretch; }
.panel { padding: 18px; }

/* 左侧目录（固定列，不随面板滚动；条目过多时内部滚动）
   material 用全局 .glass（有 backdrop-filter 才有毛玻璃质感），
   position/z-index 必须显式给出：全局背景层是 fixed + z-index:0，
   普通无定位的块会被它盖住（表现为「背景一换目录就看不见」）。 */
.settings-toc {
  width: 180px; flex: 0 0 180px; align-self: flex-start; max-height: 100%;
  position: relative; z-index: 1;
  display: flex; flex-direction: column; gap: 6px; padding: 14px 10px;
  overflow-y: auto;
}
.toc-head {
  font-size: calc(11px * var(--font-scale)); font-weight: 600; letter-spacing: 1px;
  color: rgba(var(--text-rgb), 0.5); padding: 0 8px 6px;
}
.toc-list { display: flex; flex-direction: column; gap: 2px; }
.toc-item {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 8px;
  border: none; background: transparent; border-radius: 8px; cursor: pointer; text-align: left;
  color: rgba(var(--text-rgb), 0.68); font-family: inherit;
  font-size: calc(12.5px * var(--font-scale)); transition: background .15s ease, color .15s ease;
}
.toc-item:hover { background: rgba(var(--surface-rgb), 0.10); color: rgba(var(--text-rgb), 0.95); }
.toc-item.active { background: rgba(var(--surface-rgb), 0.10); color: rgba(var(--text-rgb), 1); font-weight: 600; }
.toc-bar {
  width: 3px; height: 13px; border-radius: 2px; flex-shrink: 0;
  background: transparent; transition: background .15s ease;
}
.toc-item.active .toc-bar { background: var(--accent); }
.toc-text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* 窗口不够宽时隐藏目录，回到原来 1100px 单列布局 */
@media (max-width: 1240px) {
  .top { max-width: 1100px; }
  .settings-body { max-width: 1100px; }
  .settings-toc { display: none; }
}
.panel-title { font-size: calc(15px * var(--font-scale)); font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.panel-title .hint { font-size: calc(11px * var(--font-scale)); font-weight: normal; opacity: .5; }
.tbl { width: 100%; margin-top: 12px; border-collapse: collapse; th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(var(--surface-rgb), 0.05); font-size: calc(13px * var(--font-scale)); } th { font-size: calc(11px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); } }
.mono { font-family: Consolas, monospace; font-size: calc(12px * var(--font-scale)); }
.sm-cell { max-width: 200px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.actions { display: flex; gap: 6px; justify-content: flex-end; }
.storage-loc { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.storage-loc-label { font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); flex-shrink: 0; }
.storage-loc-input { flex: 1; min-width: 0; width: auto; }
.migrate-box { width: 560px; }
.migrate-paths {
  display: grid; gap: 6px; margin-bottom: 12px; padding: 10px 12px; border-radius: 10px;
  background: rgba(var(--surface-rgb), 0.05); border: 1px solid rgba(var(--surface-rgb), 0.08);
}
.migrate-path { display: flex; gap: 8px; align-items: baseline; font-size: calc(12px * var(--font-scale)); word-break: break-all; }
.migrate-path-label { flex-shrink: 0; opacity: .6; }
.migrate-sum { font-size: calc(12px * var(--font-scale)); line-height: 1.6; }
.migrate-note { margin-top: 10px; font-size: calc(11px * var(--font-scale)); line-height: 1.7; opacity: .6; }
.migrate-box .actions { margin-top: 16px; }

.delete-confirm-box { width: 480px; }
.delete-confirm-body { padding: 4px 0 8px; }
.delete-confirm-hint { font-size: calc(12px * var(--font-scale)); line-height: 1.6; margin-bottom: 12px; opacity: .8; }
.delete-confirm-path {
  display: flex; gap: 8px; align-items: baseline; font-size: calc(12px * var(--font-scale));
  padding: 8px 12px; border-radius: 8px; margin-bottom: 14px;
  background: rgba(var(--surface-rgb), 0.05); border: 1px solid rgba(var(--surface-rgb), 0.08); word-break: break-all;
}
.delete-confirm-path-label { flex-shrink: 0; opacity: .6; }
.delete-confirm-question { font-size: calc(13px * var(--font-scale)); font-weight: 600; margin-bottom: 6px; }
.delete-confirm-warn { font-size: calc(11px * var(--font-scale)); line-height: 1.7; opacity: .55; }
.btn.danger {
  background: rgba(239, 68, 68, 0.15); color: #f87171;
  border: 1px solid rgba(239, 68, 68, 0.35);
  &:hover:not(:disabled) { background: rgba(239, 68, 68, 0.25); border-color: rgba(239, 68, 68, 0.55); }
  &:disabled { opacity: .5; cursor: not-allowed; }
}

.storage-loc-tip { font-size: calc(11px * var(--font-scale)); opacity: .5; margin: 6px 0 0; }
.empty { padding: 24px; text-align: center; opacity: .5; font-size: calc(13px * var(--font-scale)); }
.tag-t { font-size: calc(11px * var(--font-scale)); padding: 0; border: none; background: none; color: rgba(var(--text-rgb), 0.55); font-family: inherit; font-weight: 400; }

/* 数据存储表：名称做主信息（更大、更深、加粗），类型退成次要说明，不再高亮底色 */
.storage-tbl td:first-child {
  font-size: calc(14px * var(--font-scale));
  font-weight: 600;
  color: rgb(var(--text-rgb));
}
.storage-tbl .tag-t { opacity: .9; }

.cap-block { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.06); }
.cap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.guide-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.guide-actions .btn { padding: 8px 16px; }
.cap-title { font-size: calc(12px * var(--font-scale)); text-transform: uppercase; letter-spacing: 1px; color: rgba(var(--text-rgb), 0.6); font-weight: 500; }
.sm { padding: 3px 10px; font-size: calc(11px * var(--font-scale)); }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; min-height: 26px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px 3px 10px; border-radius: 999px; font-size: calc(12px * var(--font-scale)); cursor: default;
  border: 1px solid rgba(var(--surface-rgb), 0.15);
  &.blue   { background: rgba(var(--accent-rgb), 0.18);  color: var(--accent-light); border-color: rgba(var(--accent-rgb), 0.3); }
  &.purple { background: rgba(var(--accent2-rgb), 0.18); color: var(--accent2-light); border-color: rgba(var(--accent2-rgb), 0.3); }
  &.green  { background: rgba(100,220,160,0.18); color: var(--success-light); border-color: rgba(100,220,160,0.3); }
  .x { margin-left: 4px; opacity: .7; font-weight: bold; cursor: pointer; padding: 0 2px; &:hover { opacity: 1; } }
}
.hint-empty { font-size: calc(12px * var(--font-scale)); opacity: .45; padding: 4px 0; }
.rounds-input { width: 140px; }

/* —— 语音识别 —— */
.asr-status { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.asr-badge { padding: 2px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; white-space: nowrap; }
.asr-badge-ok { background: rgba(34,197,94,0.18); color: #4ade80; }
.asr-badge-no { background: rgba(239,68,68,0.18); color: #f87171; }
.asr-desc { font-size: 13px; color: var(--text); opacity: 0.7; }
.asr-install { margin-top: 10px; padding: 10px 12px; border-radius: 8px; background: rgba(var(--surface-rgb), 0.06); border: 1px solid rgba(var(--surface-rgb), 0.12); }
.asr-install-label { font-size: 13px; margin-bottom: 6px; color: var(--text); opacity: 0.8; }
.asr-cmd { font-family: monospace; font-size: 13px; padding: 6px 10px; border-radius: 6px; background: rgba(var(--shadow-rgb), 0.18); color: var(--code-ink, #ffb088); word-break: break-all; }
.asr-note { font-size: 12px; margin-top: 6px; color: var(--text); opacity: 0.5; }
.asr-toggle { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.asr-switch { position: relative; width: 42px; height: 24px; border-radius: 12px; border: none; cursor: pointer; background: rgba(var(--surface-rgb), 0.2); transition: background .2s; padding: 0; flex-shrink: 0; }
.asr-switch.on { background: rgba(34,197,94,0.5); }
.asr-switch-knob { position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: transform .2s; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }
.asr-switch.on .asr-switch-knob { transform: translateX(18px); }
.asr-toggle-text { font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; }
.asr-toggle-hint { font-size: 12px; color: var(--text); opacity: 0.5; }
.asr-uninstall-row { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.asr-uninstall-btn { color: #f87171 !important; border-color: rgba(239,68,68,0.3) !important; }
.asr-uninstall-btn:hover:not(:disabled) { background: rgba(239,68,68,0.12) !important; }
.asr-uninstall-hint { font-size: 12px; color: var(--text); opacity: 0.5; }

/* —— 提示音 —— */
.sound-row { display: flex; align-items: flex-start; gap: 10px; margin-top: 4px; }
.sound-desc { flex: 1; min-width: 0; }
.sound-main { font-size: 14px; font-weight: 600; color: var(--text); }
.sound-sub { font-size: 12px; color: var(--text); opacity: 0.55; margin-top: 2px; }
.sound-test-row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }

/* —— 外观设置 —— */
.appearance-row { display: flex; gap: 12px; margin-bottom: 12px; }
.radio-label {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(var(--surface-rgb), 0.1);
  background: rgba(var(--surface-rgb), 0.03);
  transition: all .15s;
  flex: 1; justify-content: center;
  input { display: none; }
  .radio-dot {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid rgba(var(--surface-rgb), 0.3);
    position: relative;
    transition: all .15s;
    &::after {
      content: '';
      position: absolute; top: 50%; left: 50%;
      transform: translate(-50%, -50%) scale(0);
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--accent);
      transition: transform .15s;
    }
  }
  .radio-text { font-size: calc(13px * var(--font-scale)); }
  &:hover { border-color: rgba(var(--accent-rgb), 0.4); background: rgba(var(--accent-rgb), 0.08); }
  &.active {
    border-color: var(--accent);
    background: rgba(var(--accent-rgb), 0.15);
    .radio-dot { border-color: var(--accent); &::after { transform: translate(-50%, -50%) scale(1); } }
  }
}
.perm-paths { display: flex; flex-direction: column; gap: 8px; }
.perm-paths-input {
  width: 100%; resize: vertical; min-height: 76px;
  font-family: Consolas, "JetBrains Mono", Menlo, monospace;
  font-size: calc(12px * var(--font-scale)); line-height: 1.7;
}
.perm-paths-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.avatar-preview {
  display: flex; gap: 16px; align-items: flex-end; padding: 12px;
  background: rgba(var(--surface-rgb), 0.03); border-radius: 8px;
  border: 1px solid rgba(var(--surface-rgb), 0.06);
}
.preview-item { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.preview-label { font-size: calc(11px * var(--font-scale)); opacity: .6; }
.preview-avatar {
  position: relative; overflow: hidden;
  width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: calc(14px * var(--font-scale)); font-weight: 600; color: #fff;
  text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.3);
  box-shadow: inset 0 1px 1px rgba(var(--surface-rgb), 0.45),
              inset 0 -3px 6px rgba(var(--shadow-rgb), 0.32),
              inset 0 0 0 1px rgba(var(--surface-rgb), 0.08),
              0 3px 8px rgba(var(--shadow-rgb), 0.4);
}
.preview-avatar::before {
  content: '';
  position: absolute; inset: 0;
  background:
    linear-gradient(165deg, rgba(var(--surface-rgb), 0.45) 0%, rgba(var(--surface-rgb), 0.10) 32%, rgba(var(--surface-rgb), 0) 52%),
    linear-gradient(0deg, rgba(var(--shadow-rgb), 0.24) 0%, rgba(var(--shadow-rgb), 0) 42%);
  pointer-events: none;
}
.preview-avatar.user {
  background: linear-gradient(135deg, #c47f8d, #ba71a2);
}

/* —— 文字大小滑块（小巧精致）—— */
.font-scale-row { display: flex; align-items: center; gap: 10px; }
.font-scale-edge { font-size: calc(12px * var(--font-scale)); opacity: .55; flex-shrink: 0; }
.font-scale-slider {
  flex: 1; min-width: 0;
  -webkit-appearance: none; appearance: none;
  height: 3px; border-radius: 2px;
  background: rgba(var(--surface-rgb), 0.12);
  outline: none; cursor: pointer;
  padding: 4px 0;
}
.font-scale-slider::-webkit-slider-runnable-track {
  height: 3px; border-radius: 2px;
  background: rgba(var(--surface-rgb), 0.15);
}
.font-scale-slider::-moz-range-track {
  height: 3px; border-radius: 2px; border: none;
  background: rgba(var(--surface-rgb), 0.15);
}
.font-scale-slider::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent-light, #9db4ff);
  box-shadow: 0 1px 3px rgba(0,0,0,.35), 0 0 0 1px rgba(255,255,255,.15) inset;
  margin-top: -3.5px;
  transition: transform .1s, box-shadow .1s;
}
.font-scale-slider::-webkit-slider-thumb:hover { transform: scale(1.2); }
.font-scale-slider::-webkit-slider-thumb:active { transform: scale(1.05); }
.font-scale-slider::-moz-range-thumb {
  width: 10px; height: 10px; border-radius: 50%; border: none;
  background: var(--accent-light, #9db4ff); cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,.35);
}
.font-scale-value {
  font-size: calc(12px * var(--font-scale)); font-weight: 600; min-width: 44px; text-align: right;
  color: var(--accent); font-variant-numeric: tabular-nums; flex-shrink: 0;
}

/* —— 配色方案 —— */
.theme-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.theme-card {
  cursor: pointer; border-radius: 10px; overflow: hidden;
  border: 2px solid rgba(var(--surface-rgb), 0.1);
  transition: border-color .15s, transform .12s;
}
.theme-card:hover { transform: translateY(-2px); border-color: rgba(var(--accent-rgb), 0.4); }
.theme-card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.theme-preview { height: 64px; padding: 8px; display: flex; flex-direction: column; gap: 6px; justify-content: center; }
.tp-bar { display: flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 6px; }
.tp-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.tp-text { font-size: calc(11px * var(--font-scale)); }
.tp-chip { width: 24px; height: 4px; border-radius: 2px; margin-left: 8px; }
.theme-name { padding: 6px 8px; text-align: center; font-size: calc(12px * var(--font-scale)); }

/* —— 背景选择 —— */
.bg-upload-btns { display: flex; gap: 6px; }
.bg-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }
.bg-card {
  cursor: pointer; border-radius: 10px; overflow: hidden;
  border: 2px solid rgba(var(--surface-rgb), 0.1);
  transition: border-color .15s, transform .12s;
}
.bg-card:hover { transform: translateY(-2px); border-color: rgba(var(--accent-rgb), 0.4); }
.bg-card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.bg-preview { height: 64px; position: relative; background-size: cover; background-position: center; }
.bg-type-tag {
  position: absolute; top: 4px; left: 4px;
  font-size: calc(10px * var(--font-scale)); padding: 1px 6px; border-radius: 4px;
  background: rgba(0,0,0,0.5); color: #fff; backdrop-filter: blur(4px);
}
.bg-del {
  position: absolute; top: 4px; right: 4px;
  width: 18px; height: 18px; border-radius: 50%; border: none; cursor: pointer;
  background: rgba(0,0,0,0.5); color: #fff; font-size: 11px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  &:hover { background: rgba(220,60,60,0.85); }
}
.bg-name { padding: 6px 8px; text-align: center; font-size: calc(12px * var(--font-scale)); }
/* 缩略画面：静止态代表图 + 悬停真实动画，暗色背景在浅色主题下补一圈亮边 */
.bg-preview {
  overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.55), inset 0 0 0 2px rgba(12, 18, 28, 0.08);
}
.bg-live {
  position: absolute; inset: 0; width: 100%; height: 100%;
  border: 0; pointer-events: none; display: block; object-fit: cover;
}
.bg-magnify {
  position: fixed; z-index: 1300; border-radius: 12px; overflow: hidden;
  pointer-events: none; background: var(--panel-bg, #fff);
  box-shadow: 0 24px 60px rgba(20, 30, 50, 0.24), 0 0 0 1px rgba(37, 99, 235, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  animation: bg-mag-in .16s ease-out;
}
.bg-magnify iframe { width: 100%; height: 100%; border: 0; display: block; pointer-events: none; }
.bg-magnify-name {
  position: absolute; left: 0; right: 0; bottom: 0; padding: 16px 10px 6px;
  font-size: calc(12px * var(--font-scale)); color: #fff;
  background: linear-gradient(transparent, rgba(0, 0, 0, 0.5));
}
@keyframes bg-mag-in { from { opacity: 0; transform: translateY(4px) scale(.97); } to { opacity: 1; transform: none; } }
.bg-sliders { margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.bg-slider-row { display: flex; align-items: center; gap: 10px; }
.bg-slider-label { font-size: calc(12px * var(--font-scale)); min-width: 56px; opacity: .8; }
.bg-slider-value { font-size: calc(12px * var(--font-scale)); font-weight: 600; min-width: 44px; text-align: right; color: var(--accent); font-variant-numeric: tabular-nums; flex-shrink: 0; }

.picker-mask {
  position: fixed; inset: 0;
  background: rgba(var(--shadow-rgb), 0.35);
  backdrop-filter: blur(2px);
  z-index: 2000;
  display: flex; align-items: center; justify-content: center;
}
.picker-popup {
  width: 460px; max-height: 70vh; padding: 18px;
  display: flex; flex-direction: column;
  box-shadow: 0 20px 60px rgba(var(--shadow-rgb), 0.6);
}
.picker-title { display: flex; justify-content: space-between; align-items: center;
  font-size: calc(14px * var(--font-scale)); font-weight: 600; padding-bottom: 10px; border-bottom: 1px solid rgba(var(--surface-rgb), 0.08); margin-bottom: 10px; }
.picker-sub { font-size: calc(11px * var(--font-scale)); opacity: .5; font-weight: normal; }
.picker-list { overflow-y: auto; flex: 1; }
.picker-group { margin-bottom: 4px; }
.pg-head {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: 8px; cursor: pointer;
  background: rgba(var(--surface-rgb), 0.03); border: 1px solid rgba(var(--surface-rgb), 0.06);
  &:hover { background: rgba(var(--surface-rgb), 0.06); }
  input { accent-color: var(--accent); flex-shrink: 0; }
}
.pg-name { font-size: calc(13px * var(--font-scale)); font-weight: 600; flex: 1; }
.pg-count { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pg-arrow { font-size: calc(11px * var(--font-scale)); opacity: .6; width: 12px; text-align: center; }
.pg-items { padding-left: 10px; border-left: 1px solid rgba(var(--surface-rgb), 0.06); margin-left: 8px; }
.picker-item { display: flex; gap: 10px; padding: 10px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent; font-size: calc(12px * var(--font-scale));
  &:hover { background: rgba(var(--surface-rgb), 0.04); }
  &.selected { background: rgba(var(--accent-rgb), 0.15); border-color: rgba(var(--accent-rgb), 0.3); }
  input { accent-color: var(--accent); flex-shrink: 0; margin-top: 3px; }
}
.pi-body { flex: 1; min-width: 0; }
.pi-label { font-size: calc(13px * var(--font-scale)); font-weight: 500; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.pi-extra { font-size: calc(10px * var(--font-scale)); opacity: .55; font-weight: normal; }
.pi-key { font-family: Consolas, monospace; font-size: calc(11px * var(--font-scale)); opacity: .55; margin-top: 1px; }
.pi-desc { font-size: calc(11px * var(--font-scale)); opacity: .6; margin-top: 3px; }
.picker-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(var(--surface-rgb), 0.08); }

.modal-bg { position: fixed; inset: 0; background: rgba(var(--shadow-rgb), 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 520px; padding: 20px; border-radius: 16px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 14px; }
.modal-head .modal-title { margin-bottom: 0; }
.dlg-close { width: 26px; height: 26px; border-radius: 6px; cursor: pointer; border: 1px solid rgba(var(--surface-rgb), 0.16); background: transparent; color: rgba(var(--text-rgb), 0.75); font-size: 13px; line-height: 1; flex-shrink: 0; transition: background .15s, color .15s; }
.dlg-close:hover { background: rgba(232, 92, 92, 0.14); color: #e85c5c; }
.modal-title { font-size: calc(16px * var(--font-scale)); font-weight: 600; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; label { display: flex; flex-direction: column; gap: 4px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); &.full { grid-column: 1 / -1; } } }
.field-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.field-head .btn { flex-shrink: 0; }
.field-head .btn:disabled { opacity: .5; cursor: not-allowed; }
.fetch-msg { font-size: calc(11px * var(--font-scale)); line-height: 1.5; word-break: break-all; }
.fetch-msg.ok { color: var(--accent-light); }
.fetch-msg.err { color: #e05a5a; }
.iface-presets { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.iface-preset {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 10px; border-radius: 999px;
  border: 1px solid rgba(var(--surface-rgb), 0.16);
  background: rgba(var(--surface-rgb), 0.06);
  color: rgb(var(--text-rgb));
  font-size: calc(11px * var(--font-scale));
  cursor: pointer; outline: none;
  transition: border-color .15s, background .15s;
  &.hover { border-color: rgba(var(--accent-rgb), 0.55); background: rgba(var(--accent-rgb), 0.12); }
  &.add { border-style: dashed; color: rgba(var(--text-rgb), 0.65); }
}
.model-pick { width: auto; flex-shrink: 0; padding: 4px 10px; font-size: calc(11px * var(--font-scale)); max-width: 60%; }
.preset-x { font-size: calc(10px * var(--font-scale)); opacity: .55; line-height: 1; &:hover { opacity: 1; } }
.preset-form { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; .input { flex: 1; min-width: 0; padding: 5px 10px; } .input:nth-child(2) { flex: 1.6; } .dlg-close { width: 24px; height: 24px; } }
.confirm-bg { z-index: 1100; }
.confirm-box { width: 400px; }
.confirm-text { font-size: calc(13px * var(--font-scale)); line-height: 1.6; opacity: .85; margin-bottom: 4px; }
.ctx-cfg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 12px; label { display: flex; flex-direction: column; gap: 4px; font-size: calc(12px * var(--font-scale)); color: rgba(var(--text-rgb), 0.7); } .input { width: 100%; } }
.actions-row { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* —— 能力参数预设 —— */
.preset-grid { display: grid; gap: 10px; margin-top: 10px; }
.preset-card {
  padding: 12px 14px; border-radius: 10px;
  background: rgba(var(--surface-rgb), 0.03); border: 1px solid rgba(var(--surface-rgb), 0.08);
  &.off { opacity: .5; }
}
.pc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pc-type { font-size: calc(10px * var(--font-scale)); padding: 2px 8px; border-radius: 4px; background: rgba(var(--accent-rgb), 0.15); color: var(--accent-light); border: 1px solid rgba(var(--accent-rgb), 0.25); }
.pc-label { font-size: calc(13px * var(--font-scale)); font-weight: 600; }
.pc-tag { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.pc-note { font-size: calc(12px * var(--font-scale)); opacity: .6; margin-top: 4px; }
.pc-counts { display: flex; gap: 8px; margin-top: 6px; }
.cnt { font-size: calc(11px * var(--font-scale)); padding: 2px 8px; border-radius: 999px; }
.cnt.red { background: rgba(255,110,110,0.14); color: #ffb0b0; }
.cnt.blue { background: rgba(var(--accent-rgb), 0.14); color: var(--accent-light); }
.pc-actions { display: flex; gap: 6px; margin-top: 8px; }

.preset-modal { width: 660px; max-height: 84vh; overflow-y: auto; }
.chk-full { flex-direction: row !important; align-items: center; gap: 6px; grid-column: 1 / -1; input { accent-color: var(--accent); } }
.preset-block { margin-top: 16px; padding-top: 12px; border-top: 1px solid rgba(var(--surface-rgb), 0.07); }
.preset-block-title { display: flex; align-items: center; gap: 8px; font-size: calc(12px * var(--font-scale)); font-weight: 600; margin-bottom: 10px; }
.kv-row { display: flex; gap: 8px; margin-bottom: 8px; align-items: flex-start;
  input.mono, select.mono { flex: 0 0 40%; }
  input:not(.mono), select:not(.mono) { flex: 1; min-width: 0; }
  .btn { flex-shrink: 0; }
  textarea { flex: 1; resize: vertical; }
}
.icon-btn { padding: 8px 9px !important; font-size: calc(12px * var(--font-scale)); line-height: 1; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 18px; border-radius: 8px; font-size: calc(13px * var(--font-scale)); z-index: 9999;
  background: rgba(40, 40, 50, 0.95); border: 1px solid rgba(var(--surface-rgb), 0.15);
  color: #e6e6ee; box-shadow: 0 4px 16px rgba(var(--shadow-rgb), 0.4);
  backdrop-filter: blur(6px);
}
.toast.ok { border-color: rgba(110, 255, 160, 0.4); color: #b0ffc8; }
.toast.err { border-color: rgba(255, 110, 110, 0.4); color: #ffb0b0; }
.fade-enter-active, .fade-leave-active { transition: opacity .2s ease, transform .2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateX(-50%) translateY(8px); }
</style>

