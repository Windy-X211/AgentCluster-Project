// 统一 API 客户端
const BASE = '/api'

async function req<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json', ...opts.headers }, ...opts })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
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
  canvases: {
    list: () => req<any[]>('/canvases'),
    create: (d: any) => req('/canvases', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/canvases/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/canvases/${id}`, { method: 'DELETE' }),
    reorder: (ids: number[]) => req('/canvases/reorder', { method: 'POST', body: JSON.stringify({ ids }) }),
    setWorkdir: (id: number, workdir: string | null) =>
      req(`/canvases/${id}/workdir`, { method: 'POST', body: JSON.stringify({ workdir }) }),
    browse: () => req<{ path: string | null }>('/canvases/browse', { method: 'POST' }),
    workspace: (id: number) => req<any>(`/canvases/${id}/workspace`),
    trash: (id: number) => req<any>(`/canvases/${id}/trash`),
    restoreTrash: (id: number, items: { conv_id: number; op_idx: number }[]) =>
      req(`/canvases/${id}/trash/restore`, { method: 'POST', body: JSON.stringify({ items }) }),
    purgeTrash: (id: number, items: { conv_id: number; op_idx: number }[]) =>
      req(`/canvases/${id}/trash/purge`, { method: 'POST', body: JSON.stringify({ items }) }),
    fileRead: (id: number, path: string) =>
      req<any>(`/canvases/${id}/file?path=` + encodeURIComponent(path)),
    fileWrite: (id: number, path: string, content: string) =>
      req(`/canvases/${id}/file`, { method: 'PUT', body: JSON.stringify({ path, content }) }),
    fileRename: (id: number, path: string, newName: string) =>
      req(`/canvases/${id}/file/rename`, { method: 'POST', body: JSON.stringify({ path, new_name: newName }) }),
    fileCopy: (id: number, src: string, destDir: string) =>
      req(`/canvases/${id}/file/copy`, { method: 'POST', body: JSON.stringify({ src, dest_dir: destDir }) }),
    fileDelete: (id: number, path: string) =>
      req(`/canvases/${id}/file/delete`, { method: 'POST', body: JSON.stringify({ path }) })
  },
  convs: {
    list: () => req<any[]>('/conversations'),
    create: (d: any) => req('/conversations', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/conversations/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/conversations/${id}`, { method: 'DELETE' }),
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
  },
  models: {
    list: () => req<any[]>('/model_interfaces'),
    create: (d: any) => req('/model_interfaces', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/model_interfaces/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/model_interfaces/${id}`, { method: 'DELETE' })
  },
  settings: {
    get: () => req<any>('/settings'),
    update: (d: any) => req('/settings', { method: 'PUT', body: JSON.stringify(d) })
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
    pickFolder: (initial?: string) => req<any>('/storage/pick_folder', { method: 'POST', body: JSON.stringify({ initial: initial || '' }) }),
    setLocation: (path: string) => req<any>('/storage/set_location', { method: 'POST', body: JSON.stringify({ path }) })
  },
  fs: {
    list: (path: string) => req<any>('/fs/list?path=' + encodeURIComponent(path)),
    read: (path: string) => req<any>('/fs/read?path=' + encodeURIComponent(path)),
    write: (path: string, content: string) => req<any>('/fs/write', { method: 'PUT', body: JSON.stringify({ path, content }) }),
    del: (path: string) => req<any>('/fs/delete', { method: 'DELETE', body: JSON.stringify({ path }) })
  },
  capabilities: {
    all: () => req<any>('/commands'),          // 返回 {commands, skills, plugins}
    commands: () => req<any>('/commands/commands'),
    plugins: () => req<any>('/commands/plugins'),
    reload: () => req<any>('/commands/reload', { method: 'POST' }),
    run: (d: any) => req('/commands/run', { method: 'POST', body: JSON.stringify(d) })
  },
  presets: {
    list: () => req<any[]>('/param_presets'),
    create: (d: any) => req('/param_presets', { method: 'POST', body: JSON.stringify(d) }),
    update: (id: number, d: any) => req(`/param_presets/${id}`, { method: 'PUT', body: JSON.stringify(d) }),
    remove: (id: number) => req(`/param_presets/${id}`, { method: 'DELETE' })
  }
}
