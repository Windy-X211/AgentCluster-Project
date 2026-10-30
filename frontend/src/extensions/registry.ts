/**
 * 可视化渲染器注册表
 *
 * 拓展的 renderer.js 通过 window.__registerVizRenderer__(type, component) 注册。
 * VizBlock 组件根据 type 从本注册表查找渲染器并渲染。
 *
 * ⚠️ 关键设计点：
 *   1. _renderers 必须是 Vue reactive 对象，才能让已挂载的 VizBlock 在拓展异步注册完成后
 *      自动响应更新（computed 追踪 reactive 依赖变更）。
 *   2. loader.ts 必须在顶部同步 import 本模块，保证 router 懒加载 Home.vue 之前
 *      window.__registerVizRenderer__ 就已挂好 —— 否则 loadExtensions 先跑、renderer.js
 *      执行时 window.__registerVizRenderer__ 还不存在，注册会丢失。
 */
import { reactive, type Component } from 'vue'
import { parseVizSegments, hasVizSegment, type VizSegment } from './viz-parse'

// re-export 供外部使用
export { parseVizSegments, hasVizSegment, type VizSegment }

// —— 渲染器注册表（reactive，让拓展异步注册能触发 UI 更新）——
const _renderers = reactive<Record<string, Component>>({})

/** 注册一个可视化渲染器 */
export function registerVizRenderer(type: string, comp: Component): void {
    _renderers[type] = comp
}

/** 取已注册的渲染器 */
export function getVizRenderer(type: string): Component | undefined {
    return _renderers[type]
}

/** 列出所有已注册的 type */
export function listVizTypes(): string[] {
    return Object.keys(_renderers)
}

// 暴露给拓展 renderer.js 调用的全局注册函数
; (window as any).__registerVizRenderer__ = registerVizRenderer
