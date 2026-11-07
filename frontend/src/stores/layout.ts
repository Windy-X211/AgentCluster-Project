import { defineStore } from 'pinia'

const KEY = 'agentcluster.layout.v1'
export const RIGHT_MIN = 180
export const RIGHT_DEFAULT = 300
export const FONT_SCALE_MIN = 0.85
export const FONT_SCALE_MAX = 1.4
export const FONT_SCALE_DEFAULT = 1.0
export const FONT_SCALE_STEP = 0.05
export const THEMES = ['standard', 'galaxy-blue', 'tech-white', 'eye-care'] as const
export type ThemeId = typeof THEMES[number]
export const THEME_DEFAULT: ThemeId = 'standard'

// 界面字体候选（id 持久化；stack 为 CSS font-family 回退链，本机未安装的字体自动跳到下一项）
// 「跟随系统」放在首位，即默认字体（FONT_FAMILY_DEFAULT 取首项）
export const FONT_FAMILIES: { id: string; name: string; stack: string }[] = [
  { id: 'system', name: '跟随系统', stack: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Segoe UI Variable Text", "PingFang SC", "Microsoft YaHei", sans-serif' },
  { id: 'noto-sans-sc', name: '思源黑体 · Noto Sans SC', stack: '"Noto Sans SC", "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif' },
  { id: 'noto-serif-sc', name: '思源宋体 · Noto Serif SC', stack: '"Noto Serif SC", "Source Han Serif SC", "SimSun", serif' },
  { id: 'dengxian', name: '等线 · DengXian', stack: '"DengXian", "等线", "Segoe UI", "Microsoft YaHei", sans-serif' },
  { id: 'yahei', name: '微软雅黑 · Microsoft YaHei', stack: '"Microsoft YaHei", "微软雅黑", "Segoe UI", "PingFang SC", sans-serif' },
  { id: 'stxihei', name: '华文细黑', stack: '"STXihei", "华文细黑", "Microsoft YaHei", sans-serif' },
  { id: 'kaiti', name: '楷体', stack: '"KaiTi", "楷体", "STKaiti", "华文楷体", serif' },
  { id: 'youyuan', name: '幼圆', stack: '"YouYuan", "幼圆", "Microsoft YaHei", sans-serif' },
]
export const FONT_FAMILY_DEFAULT: string = FONT_FAMILIES[0].id

// 背景相关
export const BG_OPACITY_MIN = 0.1
export const BG_OPACITY_MAX = 1.0
export const BG_OPACITY_DEFAULT = 1.0
export const BG_BLUR_MIN = 0
export const BG_BLUR_MAX = 30
export const BG_BLUR_DEFAULT = 0

function readStore(): Record<string, any> {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return {}
    return JSON.parse(raw) || {}
  } catch {
    return {}
  }
}

function writeStore(patch: Record<string, any>) {
  try {
    const prev = readStore()
    localStorage.setItem(KEY, JSON.stringify({ ...prev, ...patch }))
  } catch {
    /* localStorage 不可用时静默 */
  }
}

function readWidth(): number {
  const n = Number(readStore()?.rightWidth)
  if (!isFinite(n) || n <= 0) return RIGHT_DEFAULT
  return Math.max(RIGHT_MIN, Math.round(n))
}

function readFontScale(): number {
  const n = Number(readStore()?.fontScale)
  if (!isFinite(n) || n <= 0) return FONT_SCALE_DEFAULT
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, Math.round(n * 100) / 100))
}

function readTheme(): ThemeId {
  const t = readStore()?.theme
  // 「深邃黑」槽位已改为「银河蓝」：迁移旧持久化值
  if (t === 'deep-black') return 'galaxy-blue'
  return (THEMES as readonly string[]).includes(t) ? t : THEME_DEFAULT
}

function readFontFamily(): string {
  const v = readStore()?.fontFamily
  return FONT_FAMILIES.some(f => f.id === v) ? v : FONT_FAMILY_DEFAULT
}

function readBackgroundId(): string | null {
  const v = readStore()?.backgroundId
  return v == null ? null : String(v)
}

function readBackgroundOpacity(): number {
  const n = Number(readStore()?.backgroundOpacity)
  if (!isFinite(n) || n <= 0) return BG_OPACITY_DEFAULT
  return Math.min(BG_OPACITY_MAX, Math.max(BG_OPACITY_MIN, Math.round(n * 100) / 100))
}

function readBackgroundBlur(): number {
  const n = Number(readStore()?.backgroundBlur)
  if (!isFinite(n) || n < 0) return BG_BLUR_DEFAULT
  return Math.min(BG_BLUR_MAX, Math.max(BG_BLUR_MIN, Math.round(n)))
}

export const useLayoutStore = defineStore('layout', {
  state: () => ({
    rightWidth: readWidth(),
    fontScale: readFontScale(),
    theme: readTheme(),
    fontFamily: readFontFamily(),
    backgroundId: readBackgroundId(),
    backgroundOpacity: readBackgroundOpacity(),
    backgroundBlur: readBackgroundBlur(),
    // —— 滚动位置缓存（仅内存，不持久化）——
    // 从设置/能力管理等页面返回首页时，恢复对话框与会话列表的滚动位置。
    // 放在 store 里是为了应对 KeepAlive 未命中（组件被重建）的情况。
    chatMsgScrollTop: 0,
    convListScrollTop: 0,
    // 左侧智能体列表折叠态（仅内存，不持久化）。新建画布成功后自动展开（见 canvas store create）。
    agentsCollapsed: true,
    // 对话记录浮层折叠态（仅内存，不持久化）。放 store 是因为 Home 布局需感知：
    // 画布关闭时展开对话记录，对话区要缩回为浮层留位（见 Home.vue gridStyle 三态）。
    convsCollapsed: true,
  }),
  actions: {
    setRightWidth(v: number) {
      const n = Math.max(RIGHT_MIN, Math.round(v))
      this.rightWidth = n
      writeStore({ rightWidth: n })
    },
    resetRightWidth() {
      this.setRightWidth(RIGHT_DEFAULT)
    },
    setFontScale(v: number) {
      const n = Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, Math.round(v * 100) / 100))
      this.fontScale = n
      writeStore({ fontScale: n })
    },
    resetFontScale() {
      this.setFontScale(FONT_SCALE_DEFAULT)
    },
    setTheme(t: string) {
      const v = (THEMES as readonly string[]).includes(t) ? (t as ThemeId) : THEME_DEFAULT
      this.theme = v
      writeStore({ theme: v })
    },
    resetTheme() {
      this.setTheme(THEME_DEFAULT)
    },
    setFontFamily(id: string) {
      const v = FONT_FAMILIES.some(f => f.id === id) ? id : FONT_FAMILY_DEFAULT
      this.fontFamily = v
      writeStore({ fontFamily: v })
    },
    resetFontFamily() {
      this.setFontFamily(FONT_FAMILY_DEFAULT)
    },
    setBackground(id: string | null) {
      this.backgroundId = id
      writeStore({ backgroundId: id })
    },
    setBackgroundOpacity(v: number) {
      const n = Math.min(BG_OPACITY_MAX, Math.max(BG_OPACITY_MIN, Math.round(v * 100) / 100))
      this.backgroundOpacity = n
      writeStore({ backgroundOpacity: n })
    },
    setBackgroundBlur(v: number) {
      const n = Math.min(BG_BLUR_MAX, Math.max(BG_BLUR_MIN, Math.round(v)))
      this.backgroundBlur = n
      writeStore({ backgroundBlur: n })
    },
  },
})
