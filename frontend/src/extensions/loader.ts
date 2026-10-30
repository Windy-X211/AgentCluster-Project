/**
 * 拓展加载器
 *
 * 启动时拉取已启用拓展（GET /api/extensions/active），对每个拓展的 renderer.js
 * 用 new Function 在主世界执行，内部调 window.__registerVizRenderer__ 注册渲染器。
 *
 * 同时暴露 window.Vue（Vue 运行时）和 window.__extAPI__（本程序提供给拓展的 API）。
 * extAPI 的具体实现由 RightPanel 在挂载时通过 setExtAPIImpl 注入（sendUserInput /
 * insertInput / openLightbox 等需要对话区上下文）。
 */
import * as Vue from 'vue'
import { api } from '@/api'
import { API_BASE } from '@/utils/env'
// 关键：必须同步 import registry，保证 window.__registerVizRenderer__ 在 loadExtensions 执行前就已挂上。
// 否则因为 router 路由懒加载（() => import(Home.vue)）时序不确定，renderer.js 执行时 registry 还没初始化 → 注册丢失。
import './registry'

// —— 暴露 Vue 运行时给拓展 renderer.js ——
; (window as any).Vue = Vue

// —— extAPI 桥：实现由 RightPanel 注入 ——
interface ExtAPIImpl {
    sendUserInput?: (text: string) => void
    insertInput?: (text: string) => void
    imageURL?: (path: string) => string
    openLightbox?: (src: string) => void
    getSettings?: () => any
}

const _impl: ExtAPIImpl = {}

/** RightPanel 挂载时调用，注入对话区相关的 API 实现 */
export function setExtAPIImpl(impl: ExtAPIImpl): void {
    Object.assign(_impl, impl)
}

// 构造 extAPI 对象（方法转发到 _impl，缺失时安全降级）
const extAPI = {
    sendUserInput: (text: string) => _impl.sendUserInput?.(text),
    insertInput: (text: string) => _impl.insertInput?.(text),
    imageURL: (path: string) => _impl.imageURL ? _impl.imageURL(path) : path,
    openLightbox: (src: string) => _impl.openLightbox?.(src),
    getSettings: () => _impl.getSettings?.() || {},
    fetchAPI: (path: string, opts?: RequestInit) => fetch((API_BASE || '') + path, opts),
}
; (window as any).__extAPI__ = extAPI

// —— 加载状态 ——
let _loaded = false
let _loading: Promise<void> | null = null

/** 加载所有已启用拓展。幂等，重复调用只执行一次（除非 force）。 */
export function loadExtensions(force = false): Promise<void> {
    if (_loaded && !force) return Promise.resolve()
    if (_loading) return _loading
    _loading = (async () => {
        try {
            const res = await api.extensions.active()
            for (const ext of res.extensions || []) {
                const manifest = ext.manifest || {}
                const files = ext.files || {}
                for (const fname of (manifest.frontend_files || ['renderer.js'])) {
                    const code = files[fname]
                    if (!code) continue
                    try {
                        // 用 new Function 在主世界执行（无独立模块作用域，可访问 window.*）
                        // 严格模式包裹，避免污染全局变量
                        const fn = new Function('"use strict";\n' + code)
                        fn.call(window)
                    } catch (e) {
                        console.error(`[extensions] 拓展 ${ext.id}/${fname} 执行失败：`, e)
                    }
                }
            }
            _loaded = true
            console.info('[extensions] 已加载拓展，渲染器类型:', (window as any).__registeredVizTypes__ || '(见各拓展日志)')
        } catch (e) {
            console.error('[extensions] 加载已启用拓展失败：', e)
        } finally {
            _loading = null
        }
    })()
    return _loading
}
