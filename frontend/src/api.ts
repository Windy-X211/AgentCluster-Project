// 统一 API 客户端
// 必须用绝对地址直打后端：prod 模式的 dist server 没有 /api proxy，
// 相对路径会被 SPA fallback 兜成 index.html → JSON.parse 报 "not valid JSON"
import { API_BASE } from '@/utils/env'
const BASE = API_BASE + '/api'

// —— 插件调试面板领域类型 ——
export interface ParamDefinition {
  name: string
  type?: string
  required?: boolean
  description?: string
  enum?: unknown[]
  default?: unknown
  example?: unknown
  [extra: string]: unknown
}

export interface PluginEntry {
  name: string
  dir_name?: string
  label: string
  description: string
  tool_description: string
  version: string
  author: string
  icon: string
  tags: string[]
  parameters: ParamDefinition[]
}

export interface FailedEntry {
  dir_name: string
  load_error: string
}

export interface PluginScanResult {
  root_dir: string
  plugins: PluginEntry[]
  failed: FailedEntry[]
  error: string | null
}

export interface ExecutionResult {
  ok: boolean
  data: unknown
  error: string | null
  duration_ms: number
}

export interface RunLogEntry {
  timestamp: string
  plugin_name: string
  params_summary: string
  duration_ms: number
  status: 'ok' | 'err'
}

// 默认请求超时（ms）：后端挂死/连接半开时避免 await 永挂导致按钮状态锁死；
// 长耗时端点（工具执行/市场同步/压缩/上传/Office 转换）在调用处单独传大超时
const DEFAULT_TIMEOUT = 30_000

/** 带超时的 fetch：超时 abort 并抛出可读错误；调用方自带 signal 时不套超时（交由其自行管理） */
async function fetchT(url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  if (!timeoutMs || timeoutMs <= 0 || init.signal) return fetch(url, init)
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: ctrl.signal })
  } catch (e: any) {
    if (e?.name === 'AbortError') throw new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：${url}`)
    throw e
  } finally {
    clearTimeout(timer)
  }
}

type ReqOpts = RequestInit & { timeout?: number }

async function req<T = any>(path: string, opts: ReqOpts = {}): Promise<T> {
  const { timeout = DEFAULT_TIMEOUT, ...rest } = opts
  const r = await fetchT(BASE + path, { headers: { 'Content-Type': 'application/json', ...rest.headers }, ...rest }, timeout)
  if (!r.ok) {
    // 优先展示后端 detail（同步/推送失败原因都靠它），读不到再退回状态码
    const text = await r.text().catch(() => '')
    let detail: unknown = text
    try {
      const j = JSON.parse(text)
      detail = j?.detail ?? j?.message ?? text
    } catch {
      /* 非 JSON 响应，保留原文 */
    }
    if (detail && typeof detail === 'object') detail = JSON.stringify(detail)
    throw new Error(`${r.status} ${String(detail || r.statusText)}`)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

export const api = {
  health: () => req('/health'),
  agents: {
    list: () => req<any[]>('/agents'),
    create: (d: any) => req('/agents', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/agents/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/agents/${id}`, { method: 'DELETE' }),
    reorder: (ids: number[]) => req('/agents/reorder', { method: 'POST', body: JSON.stringify({ ids }) })
  },
  agent_groups: {
    list: () => req<any[]>('/agent_groups'),
    create: (d: any) => req('/agent_groups', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/agent_groups/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/agent_groups/${id}`, { method: 'DELETE' }),
    reorder: (ids: number[]) => req('/agent_groups/reorder', { method: 'POST', body: JSON.stringify({ ids }) })
  },
  canvases: {
    list: () => req<any[]>('/canvases'),
    create: (d: any) => req('/canvases', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/canvases/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/canvases/${id}`, { method: 'DELETE' }),
    reorder: (ids: number[]) => req('/canvases/reorder', { method: 'POST', body: JSON.stringify({ ids }) }),
    setWorkdir: (id: number, workdir: string | null) =>
      req(`/canvases/${id}/workdir`, { method: 'POST', body: JSON.stringify({ workdir }) }),
    browse: () => req<{ path: string | null }>('/canvases/browse', { method: 'POST' }),
    // rev: 上次拿到的树版本号；未变化时后端只回 {changed:false}（轮询用），rev<0 强制全量
    workspace: (id: number, rev = -1) =>
      req<any>(`/canvases/${id}/workspace` + (rev >= 0 ? `?rev=${rev}` : '')),
    trash: (id: number) => req<any>(`/canvases/${id}/trash`),
    restoreTrash: (id: number, items: { conv_id: number; op_idx: number }[]) =>
      req(`/canvases/${id}/trash/restore`, { method: 'POST', body: JSON.stringify({ items }) }),
    purgeTrash: (id: number, items: { conv_id: number; op_idx: number }[]) =>
      req(`/canvases/${id}/trash/purge`, { method: 'POST', body: JSON.stringify({ items }) }),
    fileRead: (id: number, path: string) =>
      req<any>(`/canvases/${id}/file?path=` + encodeURIComponent(path)),
    // 二进制文件原始地址（图片 <img> / PDF <iframe> 用；v=修改时间防缓存）
    fileRawUrl: (id: number, path: string, v?: number) =>
      BASE + `/canvases/${id}/file/raw?path=` + encodeURIComponent(path) + (v ? `&v=${v}` : ''),
    // 取二进制内容（xlsx/docx/pptx 等需在前端解析的类型）
    fileRaw: async (id: number, path: string): Promise<ArrayBuffer> => {
      const r = await fetchT(BASE + `/canvases/${id}/file/raw?path=` + encodeURIComponent(path), {}, 300_000)
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
      return r.arrayBuffer()
    },
    // Office(doc/docx/ppt) 经后端 COM 转 PDF：返回 blob URL 供 iframe 渲染（调用方负责 revoke）
    fileOfficePdf: async (id: number, path: string): Promise<string> => {
      const r = await fetchT(BASE + `/canvases/${id}/file/office-pdf?path=` + encodeURIComponent(path), {}, 300_000)
      if (!r.ok) {
        let d: any = null
        try { d = await r.json() } catch { /* 非 JSON 错误体 */ }
        throw new Error(d?.detail || `${r.status} ${r.statusText}`)
      }
      return URL.createObjectURL(await r.blob())
    },
    fileWrite: (id: number, path: string, content: string) =>
      req(`/canvases/${id}/file`, { method: 'PUT', body: JSON.stringify({ path, content }), timeout: 120_000 }),
    fileRename: (id: number, path: string, newName: string) =>
      req(`/canvases/${id}/file/rename`, { method: 'POST', body: JSON.stringify({ path, new_name: newName }) }),
    fileCopy: (id: number, src: string, destDir: string) =>
      req(`/canvases/${id}/file/copy`, { method: 'POST', body: JSON.stringify({ src, dest_dir: destDir }) }),
    fileDelete: (id: number, path: string) =>
      req(`/canvases/${id}/file/delete`, { method: 'POST', body: JSON.stringify({ path }) }),
    fileRun: (id: number, path: string) =>
      req<any>(`/canvases/${id}/file/run`, { method: 'POST', body: JSON.stringify({ path }) }),
    filePyInput: (id: number, sid: string, text: string) =>
      req<any>(`/canvases/${id}/file/pyconsole/${sid}/input`, { method: 'POST', body: JSON.stringify({ text }) }),
    filePyStop: (id: number, sid: string, force = false) =>
      req<any>(`/canvases/${id}/file/pyconsole/${sid}/stop`, { method: 'POST', body: JSON.stringify({ force }) }),
    filePyClose: (id: number, sid: string) =>
      req<any>(`/canvases/${id}/file/pyconsole/${sid}/close`, { method: 'POST' }),
    filePyStream: (id: number, sid: string, onEvent: (ev: any) => void, signal?: AbortSignal) =>
      new Promise<void>(async (res, rej) => {
        try {
          const r = await fetch(BASE + `/canvases/${id}/file/pyconsole/${sid}/stream`, { signal })
          if (!r.ok || !r.body) return rej(new Error(`${r.status} ${r.statusText}`))
          const reader = r.body.getReader()
          const dec = new TextDecoder()
          let buf = ''
          for (; ;) {
            const { done, value } = await reader.read()
            if (done) break
            buf += dec.decode(value, { stream: true })
            let i
            while ((i = buf.indexOf('\n\n')) >= 0) {
              const raw = buf.slice(0, i)
              buf = buf.slice(i + 2)
              const line = raw.trim()
              if (!line.startsWith('data:')) continue
              try { onEvent(JSON.parse(line.slice(5).trim())) } catch { }
            }
          }
          res()
        } catch (e) { rej(e) }
      })
  },
  // 根级文件访问（不依赖 canvasId：上传附件 / 外部路径等）
  fs: {
    list: (path: string) => req<any>('/fs/list?path=' + encodeURIComponent(path)),
    read: (path: string) => req<any>('/fs/read?path=' + encodeURIComponent(path)),
    write: (path: string, content: string) => req<any>('/fs/write', { method: 'PUT', body: JSON.stringify({ path, content }), timeout: 120_000 }),
    del: (path: string) => req<any>('/fs/delete', { method: 'DELETE', body: JSON.stringify({ path }) }),
    // —— 根级文件原始字节 / 二进制预览（供 FileViewer 预览上传附件等不属于任何画布的文件）——
    rawUrl: (path: string, v?: number) =>
      BASE + `/fs/raw?path=` + encodeURIComponent(path) + (v ? `&v=${v}` : ''),
    raw: async (path: string): Promise<ArrayBuffer> => {
      const r = await fetchT(BASE + `/fs/raw?path=` + encodeURIComponent(path), {}, 300_000)
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
      return r.arrayBuffer()
    },
    officePdf: async (path: string): Promise<string> => {
      const r = await fetchT(BASE + `/fs/office-pdf?path=` + encodeURIComponent(path), {}, 300_000)
      if (!r.ok) {
        let d: any = null
        try { d = await r.json() } catch { /* 非 JSON 错误体 */ }
        throw new Error(d?.detail || `${r.status} ${r.statusText}`)
      }
      return URL.createObjectURL(await r.blob())
    },
    run: async (path: string) =>
      req<any>(`/fs/run?path=` + encodeURIComponent(path), { method: 'POST' }),
  },
  convs: {
    list: () => req<any[]>('/conversations'),
    get: (id: number) => req<any>(`/conversations/${id}`),
    create: (d: any) => req('/conversations', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/conversations/${id}`, { method: 'PUT', body: JSON.stringify(d), timeout: 120_000 }),
    remove: (id: number) => req(`/conversations/${id}`, { method: 'DELETE' }),
    runs: () => req<any>('/conversations/runs'),
    running: (id: number) => req<any>(`/conversations/${id}/running?include_base=1`),
    // 当前智能体视角的上下文长度（估算 token）与压缩线进度
    context: (id: number, agentId: number) => req<any>(`/conversations/${id}/context?agent_id=${agentId}`),
    // 手动触发历史压缩（force=true 跳过压缩线判定；返回 compressed=false 表示无可压缩区间/摘要生成失败）
    compress: (id: number, agentId: number, force = true) =>
      req<any>(`/conversations/${id}/compress?agent_id=${agentId}&force=${force}`, { method: 'POST', timeout: 300_000 }),
    // agentId 给定 → 只停该智能体；缺省 → 停该会话全部智能体
    stop: (id: number, agentId?: number | null) =>
      req(`/conversations/${id}/stop`, { method: 'POST', body: JSON.stringify(agentId != null ? { agent_id: agentId } : {}) }),
    send: (id: number, d: any, onChunk: (s: string) => void) => new Promise<void>(async (res, rej) => {
      try {
        const r = await fetch(BASE + `/conversations/${id}/send`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d) })
        if (!r.ok) return rej(new Error(r.statusText))
        const reader = r.body!.getReader(); const decoder = new TextDecoder(); let buf = ''
        while (true) { const { done, value } = await reader.read(); if (done) break; buf += decoder.decode(value, { stream: true }); onChunk(buf) }
        res()
      } catch (e) { rej(e) }
    }),
    // 文件操作历史 + 还原
    fileOps: (id: number, sinceTs?: string) =>
      req<any>(`/conversations/${id}/file_ops` + (sinceTs ? `?since_ts=${encodeURIComponent(sinceTs)}` : '')),
    restoreFileOps: (id: number, opIndices: number[]) =>
      req<any>(`/conversations/${id}/file_ops/restore`, { method: 'POST', body: JSON.stringify({ op_indices: opIndices }) }),
    restoreOneFileOp: (id: number, opIdx: number) =>
      req<any>(`/conversations/${id}/file_ops/restore_one/${opIdx}`, { method: 'POST' }),
    // 截断消息 / 删除单条 / 撤销
    deleteMessage: (id: number, index: number) =>
      req<any>(`/conversations/${id}/delete_message`, { method: 'POST', body: JSON.stringify({ index }) }),
    truncate: (id: number, index: number) =>
      req<any>(`/conversations/${id}/truncate`, { method: 'POST', body: JSON.stringify({ index }) }),
    undoFrom: (id: number, index: number) =>
      req<any>(`/conversations/${id}/undo_from`, { method: 'POST', body: JSON.stringify({ index }) }),
  },
  models: {
    list: () => req<any[]>('/model_interfaces'),
    create: (d: any) => req('/model_interfaces', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/model_interfaces/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/model_interfaces/${id}`, { method: 'DELETE' }),
    fetchModels: async (d: { url: string; api_key?: string }) => {
      const r = await fetchT(BASE + '/model_interfaces/fetch_models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(d),
      }, 60_000)
      if (!r.ok) {
        let e: any = null
        try { e = await r.json() } catch { /* 非 JSON 错误体 */ }
        throw new Error(e?.detail || `${r.status} ${r.statusText}`)
      }
      return r.json() as Promise<{ models: string[]; count: number; endpoint: string }>
    },
    detectReasoning: (iid: number, modelName: string) => req<any>(`/model_interfaces/${iid}/reasoning/${encodeURIComponent(modelName)}`),
  },
  settings: {
    get: () => req<any>('/settings'),
    update: (d: any) => req('/settings', { method: 'PUT', body: JSON.stringify(d) })
  },
  backgrounds: {
    list: () => req<any[]>('/backgrounds'),
    rawUrl: (id: string, file: string, v?: number) =>
      BASE + `/backgrounds/${encodeURIComponent(id)}/raw/${encodeURI(file)}` + (v ? `?v=${v}` : ''),
    upload: async (form: FormData) => {
      const r = await fetchT(BASE + '/backgrounds/upload', { method: 'POST', body: form }, 300_000)
      if (!r.ok) {
        let d: any = null; try { d = await r.json() } catch { /* 非 JSON 错误体 */ }
        throw new Error(d?.detail || `${r.status} ${r.statusText}`)
      }
      return r.json()
    },
    remove: (id: string) => req<any>(`/backgrounds/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  },
  rules: {
    list: (canvasId?: number | null) => req<any[]>('/rules' + (canvasId != null ? `?canvas_id=${canvasId}` : '')),
    create: (d: any) => req('/rules', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/rules/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/rules/${id}`, { method: 'DELETE' })
  },
  storage: {
    paths: () => req<any>('/storage/paths'),
    open: (path: string) => req<any>('/storage/open', { method: 'POST', body: JSON.stringify({ path }) }),
    pickFolder: (initial?: string, title?: string) =>
      req<{ path: string }>('/storage/pick_folder', { method: 'POST', body: JSON.stringify({ initial: initial || '', title: title || '' }) }),
    setLocation: (path: string) => req<any>('/storage/set_location', { method: 'POST', body: JSON.stringify({ path }), timeout: 600_000 })
  },
  capabilities: {
    all: () => req<any>('/commands'),          // 返回 {commands, skills, plugins}
    commands: () => req<any>('/commands/commands'),
    plugins: () => req<any>('/commands/plugins'),
    reload: () => req<any>('/commands/reload', { method: 'POST' }),
    run: (d: any) => req('/commands/run', { method: 'POST', body: JSON.stringify(d), timeout: 600_000 }),
    dependencies: () => req<any>('/commands/dependencies'),
  },
  presets: {
    list: () => req<any[]>('/param_presets'),
    create: (d: any) => req('/param_presets', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/param_presets/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/param_presets/${id}`, { method: 'DELETE' })
  },
  pluginsDebug: {
    list: (rootDir: string) =>
      req<PluginScanResult>('/plugins_debug/list', { method: 'POST', body: JSON.stringify({ root_dir: rootDir }), timeout: 120_000 }),
    run: (rootDir: string, pluginName: string, params: Record<string, unknown>) =>
      req<ExecutionResult>('/plugins_debug/run', {
        method: 'POST',
        body: JSON.stringify({ root_dir: rootDir, plugin_name: pluginName, params }),
        timeout: 600_000,
      })
  },
  asr: {
    status: () => req<{ available: boolean }>('/asr/status'),
    uninstall: () => req<{ ok: boolean; msg: string }>('/asr/uninstall', { method: 'DELETE', timeout: 120_000 }),
  },
  launchMode: {
    get: () => req<{ mode: 'client' | 'web' }>('/launch_mode'),
    set: (mode: 'client' | 'web') => req('/launch_mode', { method: 'PUT', body: JSON.stringify({ mode }) }),
  },
  autostart: {
    get: () => req<{ enabled: boolean; command: string; supported: boolean }>('/autostart'),
    set: (enabled: boolean) => req('/autostart', { method: 'PUT', body: JSON.stringify({ enabled }) }),
  },
  extensions: {
    list: () => req<{ market: any[]; installed: Record<string, any> }>('/extensions'),
    active: () => req<{ extensions: Array<{ id: string; manifest: any; files: Record<string, string> }> }>('/extensions/active'),
    install: (id: string) => req<any>('/extensions/install', { method: 'POST', body: JSON.stringify({ id }), timeout: 300_000 }),
    enable: (id: string) => req<any>(`/extensions/${encodeURIComponent(id)}/enable`, { method: 'POST' }),
    disable: (id: string) => req<any>(`/extensions/${encodeURIComponent(id)}/disable`, { method: 'POST' }),
    uninstall: (id: string) => req<any>(`/extensions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    file: (id: string, path: string) =>
      req<{ ok: boolean; content: string }>(`/extensions/${encodeURIComponent(id)}/file?path=` + encodeURIComponent(path)),
  },
  market: {
    kinds: () => req<{ kinds: Array<{ key: string; label: string; count: number }>; updated_at?: string; needs_sync?: boolean }>('/market/kinds'),
    preview: (kind: string, id: string) =>
      req<{ item: any; files: Array<{ path: string; size: number }>; preview: any }>(
        `/market/item/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`
      ),
    cache: {
      overview: () => req<any>('/market/cache/overview'),
      clean: (payload?: { preserve_index?: boolean }) =>
        req<any>('/market/cache/clean', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload ?? {}),
        }),
    },
    items: (kind?: string, q?: string) => {
      // 用 URLSearchParams 统一拼接：kind 为空（推荐分类）时直接拼 "&q=" 会得到 /items&q=... → 404
      const params = new URLSearchParams()
      if (kind) params.set('kind', kind)
      if (q) params.set('q', q)
      const qs = params.toString()
      return req<{ kind: string; total: number; items: any[]; updated_at?: string; needs_sync?: boolean }>(
        '/market/items' + (qs ? `?${qs}` : '')
      )
    },
    publishable: (kind: string) =>
      req<{ kind: string; items: Array<{ id: string; name: string; description?: string }> }>(
        `/market/publishable?kind=${encodeURIComponent(kind)}`
      ),
    install: (kind: string, id: string, version?: string) =>
      req<any>('/market/install', {
        method: 'POST',
        body: JSON.stringify({ kind, id, ...(version ? { version } : {}) }),
        timeout: 300_000,
      }),
    itemVersions: (kind: string, id: string) =>
      req<{ kind: string; id: string; name?: string; latest_version?: string; total: number; versions: any[] }>(
        `/market/item/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/versions`,
      ),
    publish: (kind: string, id: string, push = false) =>
      req<any>('/market/publish', { method: 'POST', body: JSON.stringify({ kind, id, push }), timeout: 300_000 }),
    config: () => req<any>('/market/config'),
    saveConfig: (data: any) =>
      req<any>('/market/config', { method: 'PUT', body: JSON.stringify(data) }),
    sync: (force = true) =>
      req<any>('/market/sync', { method: 'POST', body: JSON.stringify({ force }), timeout: 300_000 }),
    push: (message?: string) =>
      req<any>('/market/push', { method: 'POST', body: JSON.stringify({ message }), timeout: 300_000 }),
    auth: () => req<any>('/market/auth'),
    login: (username: string, token: string) =>
      req<any>('/market/auth', { method: 'POST', body: JSON.stringify({ username, token }) }),
    logout: () => req<any>('/market/auth', { method: 'DELETE' }),
    myItems: (q?: string) =>
      req<{ kind: string; total: number; items: any[]; updated_at?: string; mine?: boolean }>(
        '/market/my-items' + (q ? '?q=' + encodeURIComponent(q) : '')
      ),
    deleteItem: (kind: string, id: string, push = false) =>
      req<any>(
        `/market/items/${encodeURIComponent(kind)}/${encodeURIComponent(id)}` + (push ? '?push=1' : ''),
        { method: 'DELETE', timeout: 300_000 },
      ),
  }
}
