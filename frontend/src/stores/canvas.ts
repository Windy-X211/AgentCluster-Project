import { defineStore } from 'pinia'
import { api } from '@/api'
import { useLayoutStore } from './layout'

/** curve=平滑曲线 | straight=斜线直连 | ortho=直角折线（L 形，一个 90° 折角） */
export type CanvasEdgeShape = 'curve' | 'straight' | 'ortho'

/** 画布内剪贴板：节点按原始绝对坐标保存，连线端点用 nodes 下标表示，粘贴时整体平移并重映射 id。 */
export interface CanvasClipboard {
  nodes: { agent_id: number; x: number; y: number }[]
  edges: {
    from: number
    to: number
    type: string
    shape: CanvasEdgeShape
    waypoints: { x: number; y: number }[]
  }[]
  bbox: { x: number; y: number; w: number; h: number }
  /** 连续粘贴的级联偏移计数；重新复制时归零 */
  pasteCount: number
}

export const useCanvasStore = defineStore('canvas', {
  state: () => ({ list: [] as any[], current: null as any, clipboard: null as CanvasClipboard | null }),
  actions: {
    async fetch() {
      this.list = await api.canvases.list()
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
      const c = await api.canvases.create({ name, nodes: [], edges: [], ...d })
      this.list.push(c); this.current = c
      // 新建画布成功后自动展开左侧智能体列表，方便向空画布拖入智能体
      useLayoutStore().agentsCollapsed = false
      return c
    },
    async saveCurrent() { if (!this.current) return; const c = await api.canvases.update(this.current.id, this.current); const i = this.list.findIndex(x => x.id === c.id); if (i >= 0) this.list[i] = c; this.current = c },
    async closeCurrent() {
      if (!this.current) return
      await this.saveCurrent()
      this.current = null
      try { localStorage.removeItem('agent-cluster.canvas.current') } catch { /* ignore */ }
    },
    async rename(id: number, name: string) { const c = this.list.find(x => x.id === id); if (!c) return; c.name = name; await this.saveCurrent() },
    async remove(id: number) { await api.canvases.remove(id); this.list = this.list.filter(x => x.id !== id); if (this.current?.id === id) this.current = this.list[0] || null },
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
