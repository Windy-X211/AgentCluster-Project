import { defineStore } from 'pinia'

const KEY = 'agentcluster.layout.v1'
export const RIGHT_MIN = 240
export const RIGHT_MAX = 820
export const RIGHT_DEFAULT = 300
export const FONT_SCALE_MIN = 0.85
export const FONT_SCALE_MAX = 1.4
export const FONT_SCALE_DEFAULT = 1.0
export const FONT_SCALE_STEP = 0.05
export const THEMES = ['standard', 'deep-black', 'tech-white', 'eye-care'] as const
export type ThemeId = typeof THEMES[number]
export const THEME_DEFAULT: ThemeId = 'standard'

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
  return Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, Math.round(n)))
}

function readFontScale(): number {
  const n = Number(readStore()?.fontScale)
  if (!isFinite(n) || n <= 0) return FONT_SCALE_DEFAULT
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, Math.round(n * 100) / 100))
}

function readTheme(): ThemeId {
  const t = readStore()?.theme
  return (THEMES as readonly string[]).includes(t) ? t : THEME_DEFAULT
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
    backgroundId: readBackgroundId(),
    backgroundOpacity: readBackgroundOpacity(),
    backgroundBlur: readBackgroundBlur(),
  }),
  actions: {
    setRightWidth(v: number) {
      const n = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, Math.round(v)))
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
