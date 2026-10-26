/**
 * 桌面环境检测
 *  Electron 进程内 window.electronAPI 存在 → Electron 模式
 *  URL ?app=pywebview → 旧 pywebview 模式（兼容）
 *  其他 → 浏览器模式
 */
declare global {
  interface Window {
    electronAPI?: any
    pywebview?: any
  }
}

const urlApp = new URLSearchParams(location.search).get('app')
const hasElectron = typeof window !== 'undefined' && !!window.electronAPI
const hasPywebview = typeof window !== 'undefined' && !!window.pywebview

export const isDesktop = hasElectron || hasPywebview || urlApp === 'pywebview' || urlApp === 'electron'
export const isElectron = hasElectron
export const isPywebview = hasPywebview || urlApp === 'pywebview'

/**
 * API 基础 URL —— 所有 fetch('/api/...') 统一用这个
 *
 * 为什么不能简单用相对路径？
 *   - dev 模式：Vite proxy 把 /api 转发到 8870，相对路径 ✅
 *   - prod Electron 模式：以前是 file:// 协议，fetch('/api') → file:///api/... ❶
 *     现在 prod 也走 http://localhost:5173 了，但 dist server 没有 /api proxy
 *   - prod 浏览器模式：fetch('/api') → http://localhost:5173/api/... → dist server SPA fallback → index.html → JSON.parse 炸 ❶❶
 *
 * 结论：**全部用绝对 URL 直打后端 http://127.0.0.1:8870**，一劳永逸。
 * Vite dev proxy 的存在只是为了前端不改代码，现在统一绝对 URL 也不需要 proxy 了。
 */
const _electronApiBase = typeof window !== 'undefined' && (window as any).electronAPI?.apiBase
// Electron preload 里设了 apiBase = 'http://127.0.0.1:8870'，这里取之
// 浏览器模式下 window.electronAPI 不存在，fallback 到硬编码后端地址
export const API_BASE = _electronApiBase || 'http://127.0.0.1:8870'

/**
 * 统一的窗口控制 API 适配层 —— 屏蔽 pywebview / Electron 差异
 *  前端组件只需 import { windowAPI } from '@/utils/windowApi' 然后调用 .minimize() 等
 */
export const windowAPI = {
  get _raw() {
    if (window.electronAPI?.window) return window.electronAPI.window
    if (window.pywebview?.api) return window.pywebview.api
    return null
  },
  minimize() { try { return this._raw?.minimize?.() } catch {} },
  toggleMax() {
    try {
      if (window.electronAPI?.window) return window.electronAPI.window.toggleMax()
      if (window.pywebview?.api) return window.pywebview.api.maximize()
    } catch {}
    return null
  },
  maximize() { try { return this._raw?.maximize?.() } catch {} },
  restore() { try { return this._raw?.restore?.() } catch {} },
  close() { try { return this._raw?.close?.() } catch {} },
  getBounds() {
    try {
      return this._raw?.get_bounds?.() ?? this._raw?.getBounds?.() ?? { x: 0, y: 0, width: 1280, height: 820 }
    } catch { return { x: 0, y: 0, width: 1280, height: 820 } }
  },
  setBounds(x: number, y: number, w: number, h: number) {
    try {
      const raw = this._raw
      if (!raw) return
      if (raw.set_bounds) return raw.set_bounds(x, y, w, h)
      if (raw.setBounds) return raw.setBounds(x, y, w, h)
    } catch {}
  },
  resize(w: number, h: number) { try { return this._raw?.resize?.(w, h) } catch {} },
  move(x: number, y: number) { try { return this._raw?.move?.(x, y) } catch {} },
  // isMaximized 必须返回 Promise（即使浏览器模式下），否则调用方 .then() 崩
  async isMaximized(): Promise<boolean | null> {
    try {
      if (window.electronAPI?.window) return await window.electronAPI.window.isMaximized()
    } catch {}
    return null
  },
}
