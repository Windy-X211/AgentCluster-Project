import { defineStore } from 'pinia'
import { api } from '@/api'
import { useLayoutStore } from './layout'

/** curve=平滑曲线 | straight=斜线直连 | ortho=直角折线 | mixed=逐段绘制（每段线型可不同） */
export type CanvasEdgeShape = 'curve' | 'straight' | 'ortho' | 'mixed'

/** 逐段绘制的一段：end 为段终点（画布绝对坐标），ctrl 为曲线控制点（垂线段为单位法线方向），
 *  bulge 为圆弧带符号归一化矢高。type: straight | curve | arc | vert */
export type CanvasEdgeSegment = {
  type: string
  end: { x: number; y: number }
  ctrl?: { x: number; y: number }[]
  bulge?: number
}

/** 画布内剪贴板：节点按原始绝对坐标保存，连线端点用 nodes 下标表示，粘贴时整体平移并重映射 id。 */
export interface CanvasClipboard {
  nodes: { agent_id: number; x: number; y: number }[]
  edges: {
    from: number
    to: number
    type: string
    shape: CanvasEdgeShape
    /** 直角折线方向偏好：cw=顺时针 | ccw=逆时针 | null=按默认朝向自动 */
    ortho_dir?: 'cw' | 'ccw' | null
    /** 手动锚点：相对节点边 + 沿边归一化位置，null=自动布线（粘贴时不随节点平移） */
    anchor_from?: { side: 'l' | 'r' | 't' | 'b'; t: number } | null
    anchor_to?: { side: 'l' | 'r' | 't' | 'b'; t: number } | null
    /** 逐段绘制的路径；复制时按绝对坐标保存，粘贴时随节点整体平移 */
    segments?: CanvasEdgeSegment[]
    waypoints: { x: number; y: number }[]
  }[]
  bbox: { x: number; y: number; w: number; h: number }
  /** 连续粘贴的级联偏移计数；重新复制时归零 */
  pasteCount: number
}

/** 旧版后端（Edge 未声明 ortho_dir / anchor_* / segments）会在 PUT / GET 返回值里吞掉这些字段，
 *  而 saveCurrent 用返回值整体替换 current，于是「只改一条线」会把所有线的设置一起冲掉。
 *  这里按「画布 id + 边 id」在浏览器本地留一份每条线各自的偏好（方向 + 手动锚点 + 分段路径）：
 *  · 服务端返回明确值 → 以服务端为准，并同步缓存；
 *  · 服务端缺字段或返回 null 而本地有设定 → 补回，下次保存时写回服务端；
 *  · 后端升级重启后字段正常往返，缓存自动让位，不留幽灵状态。 */
const EDGE_PREFS_CACHE_KEY = 'agent-cluster.canvas.ortho_dirs'  // 沿用旧 key，兼容已有缓存
type AnchorData = { side: string; t: number }
type EdgePrefs = { dir?: 'cw' | 'ccw'; from?: AnchorData; to?: AnchorData; segments?: CanvasEdgeSegment[] }
type EdgePrefsCache = Record<string, Record<string, EdgePrefs | 'cw' | 'ccw'>>

function readPrefsCache(): EdgePrefsCache {
  try {
    const v = JSON.parse(localStorage.getItem(EDGE_PREFS_CACHE_KEY) || '{}')
    return v && typeof v === 'object' ? v : {}
  } catch { return {} }
}
function writePrefsCache(c: EdgePrefsCache) {
  try { localStorage.setItem(EDGE_PREFS_CACHE_KEY, JSON.stringify(c)) } catch { /* 隐私模式等忽略 */ }
}
function isDir(v: any): v is 'cw' | 'ccw' { return v === 'cw' || v === 'ccw' }
function isAnchor(v: any): v is AnchorData {
  return !!v && typeof v === 'object' && ['l', 'r', 't', 'b'].includes(v.side)
    && Number.isFinite(Number(v.t))
}
function isPt(v: any): boolean {
  return !!v && Number.isFinite(Number(v.x)) && Number.isFinite(Number(v.y))
}
/** 校验并深拷贝分段路径，挡住旧格式 / 脏数据；空数组返回 null 表示「没有分段」 */
function cloneSegments(v: any): CanvasEdgeSegment[] | null {
  if (!Array.isArray(v) || !v.length) return null
  const out: CanvasEdgeSegment[] = []
  for (const s of v) {
    if (!isPt(s?.end)) continue
    out.push({
      type: typeof s.type === 'string' ? s.type : 'straight',
      end: { x: Number(s.end.x), y: Number(s.end.y) },
      ctrl: Array.isArray(s.ctrl) ? s.ctrl.filter(isPt).map((p: any) => ({ x: Number(p.x), y: Number(p.y) })) : [],
      bulge: Number.isFinite(Number(s.bulge)) ? Number(s.bulge) : 0.22,
    })
  }
  return out.length ? out : null
}
/** 兼容上一版只存方向字符串的缓存 */
function normalizePrefs(v: any): EdgePrefs {
  if (isDir(v)) return { dir: v }
  return v && typeof v === 'object' ? v as EdgePrefs : {}
}

/** 记录一张画布上每条线显式设定过的偏好；自动（null）不记，等于清掉旧设定 */
function rememberEdgePrefs(canvasId: any, edges: any[]) {
  if (canvasId == null || !Array.isArray(edges)) return
  const cache = readPrefsCache()
  const key = String(canvasId)
  const map: Record<string, EdgePrefs> = {}
  for (const e of edges) {
    if (!e || e.id == null) continue
    const p: EdgePrefs = {}
    if (isDir(e.ortho_dir)) p.dir = e.ortho_dir
    if (isAnchor(e.anchor_from)) p.from = { side: e.anchor_from.side, t: Number(e.anchor_from.t) }
    if (isAnchor(e.anchor_to)) p.to = { side: e.anchor_to.side, t: Number(e.anchor_to.t) }
    const segs = cloneSegments(e.segments)
    if (segs) p.segments = segs
    if (Object.keys(p).length) map[String(e.id)] = p
  }
  if (Object.keys(map).length) cache[key] = map
  else delete cache[key]
  writePrefsCache(cache)
}
/** 把服务端返回值里被吞掉的偏好逐条补回；返回的对象仍是同一个引用 */
function restoreEdgePrefs(c: any, fallbackId?: any): any {
  if (!c || !Array.isArray(c.edges)) return c
  const cache = readPrefsCache()
  const key = String(c.id ?? fallbackId)
  const saved = cache[key] || {}
  const next: Record<string, EdgePrefs> = {}
  const fields: [string, 'from' | 'to'][] = [['anchor_from', 'from'], ['anchor_to', 'to']]
  for (const e of c.edges) {
    if (!e || e.id == null) continue
    const id = String(e.id)
    const prev = normalizePrefs(saved[id])
    const p: EdgePrefs = {}
    // 方向偏好
    if (isDir(e.ortho_dir)) p.dir = e.ortho_dir
    else if (isDir(prev.dir)) { e.ortho_dir = prev.dir; p.dir = prev.dir }
    else if (!('ortho_dir' in e)) e.ortho_dir = null
    // 两端手动锚点
    for (const [field, slot] of fields) {
      if (isAnchor(e[field])) p[slot] = { side: e[field].side, t: Number(e[field].t) }
      else if (isAnchor(prev[slot])) {
        e[field] = { ...prev[slot] }
        p[slot] = { ...prev[slot] }
      } else if (!(field in e)) e[field] = null
    }
    // 分段路径：旧后端未声明 segments 时 PUT/GET 会整段吞掉，按边 id 逐条补回
    const segs = cloneSegments(e.segments)
    if (segs) p.segments = segs
    else {
      const cached = cloneSegments(prev.segments)
      if (cached) { e.segments = cached; p.segments = cached }
      else if (!('segments' in e)) e.segments = []
    }
    if (Object.keys(p).length) next[id] = p
  }
  if (Object.keys(next).length) cache[key] = next
  else delete cache[key]
  writePrefsCache(cache)
  return c
}

export const useCanvasStore = defineStore('canvas', {
  state: () => ({ list: [] as any[], current: null as any, clipboard: null as CanvasClipboard | null }),
  actions: {
    async fetch() {
      this.list = await api.canvases.list()
      // 旧版后端会吞掉 ortho_dir：按边 id 逐条补回，刷新页面后每条线仍保持自己的方向
      for (const c of this.list) restoreEdgePrefs(c)
      const curId = this.current?.id
      // 整页重载后内存清空 → 从 localStorage 恢复上次选中的画布
      let savedId: number | null = null
      try { savedId = +(localStorage.getItem('agent-cluster.canvas.current') || '') || null } catch { /* ignore */ }
      this.current = (curId != null && this.list.find(x => x.id === curId)) ||
        (savedId != null && this.list.find((x: any) => x.id === savedId)) ||
        this.list[0] || null
    },
    async create(d: any = {}) {
      const name = d.name || `画布 ${this.list.length + 1}`
      const c = restoreEdgePrefs(await api.canvases.create({ name, nodes: [], edges: [], ...d }))
      this.list.push(c); this.current = c
      // 新建画布成功后自动展开左侧智能体列表，方便向空画布拖入智能体
      useLayoutStore().agentsCollapsed = false
      return c
    },
    async saveCurrent() {
      if (!this.current) return
      const local = this.current
      // 保存前记下每条线各自的方向；保存后按边 id 补回被旧版后端吞掉的字段，
      // 保证「改一条线」不会把其他线的方向一起覆盖掉。
      rememberEdgePrefs(local.id, local.edges)
      const c = await api.canvases.update(local.id, local)
      if (!c) return
      restoreEdgePrefs(c, local.id)
      const i = this.list.findIndex(x => x.id === c.id)
      if (i >= 0) this.list[i] = c
      this.current = c
    },
    async closeCurrent() {
      if (!this.current) return
      await this.saveCurrent()
      this.current = null
      try { localStorage.removeItem('agent-cluster.canvas.current') } catch { /* ignore */ }
    },
    async rename(id: number, name: string) { const c = this.list.find(x => x.id === id); if (!c) return; c.name = name; await this.saveCurrent() },
    async remove(id: number) {
      await api.canvases.remove(id)
      const cache = readPrefsCache(); delete cache[String(id)]; writePrefsCache(cache)
      this.list = this.list.filter(x => x.id !== id)
      if (this.current?.id === id) this.current = this.list[0] || null
    },
    async reorder(ids: number[]) {
      // 乐观更新：先按新顺序本地重排，界面立即响应
      const pos = new Map(ids.map((id, i) => [id, i]))
      const sorted = [...this.list].sort(
        (a, b) => (pos.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (pos.get(b.id) ?? Number.MAX_SAFE_INTEGER)
      )
      const cur = this.current
      this.list = sorted
      if (cur) this.current = sorted.find(x => x.id === cur.id) || cur
      try {
        await api.canvases.reorder(ids)
      } catch {
        // 失败则回滚为后端真实顺序
        await this.fetch()
      }
    }
  }
})
