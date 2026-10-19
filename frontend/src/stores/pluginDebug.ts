import { defineStore } from 'pinia'
import { api } from '@/api'
import type { ExecutionResult, FailedEntry, PluginEntry, RunLogEntry } from '@/api'

export interface ParamFormState {
    selected: boolean
    value: string
}

const SENSITIVE_RE = /password|passwd|token|secret|auth|api[_-]?key|access[_-]?key|private[_-]?key|credential/i

/** 参数名是否命中敏感模式（用于掩码展示与日志脱敏）。 */
export function isSensitiveParam(name: string): boolean {
    return SENSITIVE_RE.test(name || '')
}

function readError(e: unknown, fallback: string): string {
    if (e instanceof Error && e.message) return e.message
    if (typeof e === 'string' && e) return e
    return fallback
}

function defaultValueToText(def: unknown): string {
    if (def === undefined || def === null) return ''
    if (typeof def === 'string') return def
    if (typeof def === 'number' || typeof def === 'boolean') return String(def)
    try {
        return JSON.stringify(def)
    } catch {
        return String(def)
    }
}

function summarizeParams(params: Record<string, unknown>): string {
    const masked: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(params)) masked[k] = isSensitiveParam(k) ? '***' : v
    let text = JSON.stringify(masked)
    if (text.length > 200) text = text.slice(0, 200) + '…'
    return text
}

function buildParams(plugin: PluginEntry, form: Record<string, ParamFormState>): Record<string, unknown> {
    const params: Record<string, unknown> = {}
    for (const d of plugin.parameters || []) {
        const st = form[d.name]
        if (!st || !st.selected) continue
        const raw = (st.value ?? '').trim()
        const type = d.type || 'string'
        if (d.required && raw === '') throw new Error(`必填参数 ${d.name} 未填写`)
        if (raw === '' && type !== 'string') continue
        if ((type === 'integer' || type === 'number') && raw !== '' && Number.isNaN(Number(raw))) {
            throw new Error(`参数 ${d.name} 须为数字`)
        }
        if ((type === 'object' || type === 'array') && raw !== '') {
            try {
                JSON.parse(raw)
            } catch {
                throw new Error(`参数 ${d.name} 的 JSON 格式错误`)
            }
        }
        params[d.name] = raw
    }
    return params
}

function makeLogEntry(
    pluginName: string,
    params: Record<string, unknown>,
    durationMs: number,
    status: 'ok' | 'err'
): RunLogEntry {
    return {
        timestamp: new Date().toLocaleTimeString(),
        plugin_name: pluginName,
        params_summary: summarizeParams(params),
        duration_ms: durationMs,
        status
    }
}

export const usePluginDebugStore = defineStore('pluginDebug', {
    state: () => ({
        rootDir: '',
        plugins: [] as PluginEntry[],
        failed: [] as FailedEntry[],
        scanError: null as string | null,
        scanning: false,
        selectedPluginName: null as string | null,
        paramForm: {} as Record<string, ParamFormState>,
        running: false,
        result: null as ExecutionResult | null,
        logs: [] as RunLogEntry[]
    }),
    getters: {
        selectedPlugin(state): PluginEntry | null {
            return state.plugins.find((p) => p.name === state.selectedPluginName) || null
        }
    },
    actions: {
        clearSelection() {
            this.selectedPluginName = null
            this.paramForm = {}
            this.result = null
        },
        /** 扫描指定根目录，替换列表并清空当前选择（避免脏数据串扰）。 */
        async scan(rootDir: string) {
            const target = (rootDir || '').trim()
            if (!target) {
                this.scanError = '请先选择插件文件夹'
                return
            }
            this.rootDir = target
            this.scanning = true
            this.scanError = null
            try {
                const res = await api.pluginsDebug.list(target)
                this.plugins = res.plugins || []
                this.failed = res.failed || []
                this.scanError = res.error || null
                this.rootDir = res.root_dir || target
                this.clearSelection()
            } catch (e) {
                this.plugins = []
                this.failed = []
                const raw = readError(e, '插件列表加载失败，请检查后端服务或重试')
                // 前端资源未随服务一起更新时（旧模块缓存）给出可操作提示
                this.scanError = /undefined|not a function/i.test(raw)
                    ? '前端资源可能已过期，请硬刷新页面（Ctrl+Shift+R）后重试'
                    : raw
                this.clearSelection()
            } finally {
                this.scanning = false
            }
        },
        /** 选中插件并初始化表单（只预填必填参数，回填 default；其余参数由用户通过下拉框添加）。 */
        selectPlugin(name: string) {
            if (this.selectedPluginName === name) return
            this.selectedPluginName = name
            this.result = null
            const plugin = this.plugins.find((p) => p.name === name)
            const form: Record<string, ParamFormState> = {}
            for (const d of plugin?.parameters || []) {
                if (!d?.name || !d.required) continue
                form[d.name] = { selected: true, value: defaultValueToText(d.default) }
            }
            this.paramForm = form
        },
        /** 通过下拉框添加一个参数（必填参数已在选中插件时自动添加）。 */
        addParam(name: string) {
            if (this.paramForm[name]) return
            const def = this.selectedPlugin?.parameters.find((p) => p.name === name)
            if (!def) return
            this.paramForm[name] = { selected: true, value: defaultValueToText(def.default) }
        },
        /** 移除一个已添加的非必填参数。 */
        removeParam(name: string) {
            const def = this.selectedPlugin?.parameters.find((p) => p.name === name)
            if (def?.required) return
            delete this.paramForm[name]
        },
        setParamValue(name: string, value: string) {
            const st = this.paramForm[name]
            if (st) st.value = value
        },
        /** 弹出系统文件夹选择器；取消时保持原路径与列表不变。返回所选路径（取消为空串）。 */
        async pickFolder(): Promise<string> {
            const res = await api.storage.pickFolder(this.rootDir, '选择插件目录')
            const path = res?.path || ''
            if (path) await this.scan(path)
            return path
        },
        /** 执行当前插件（含执行前校验）。校验失败抛出 Error 供上层提示。 */
        async run(): Promise<ExecutionResult | null> {
            const plugin = this.selectedPlugin
            if (!plugin) throw new Error('请先选择插件')
            if (this.running) return this.result
            const params = buildParams(plugin, this.paramForm)
            this.running = true
            const started = Date.now()
            try {
                const res = await api.pluginsDebug.run(this.rootDir, plugin.name, params)
                this.result = res
                this.logs.unshift(makeLogEntry(plugin.name, params, res.duration_ms, res.ok ? 'ok' : 'err'))
                return res
            } catch (e) {
                const message = readError(e, '无法连接执行后端，请确认服务已启动')
                const res: ExecutionResult = { ok: false, data: null, error: message, duration_ms: Date.now() - started }
                this.result = res
                this.logs.unshift(makeLogEntry(plugin.name, params, res.duration_ms, 'err'))
                throw new Error(message)
            } finally {
                this.running = false
            }
        },
        clearLogs() {
            this.logs = []
        }
    }
})