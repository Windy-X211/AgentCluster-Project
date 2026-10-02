import { defineStore } from 'pinia'
import { api } from '@/api'

export const useCanvasStore = defineStore('canvas', {
  state: () => ({ list: [] as any[], current: null as any }),
  actions: {
    async fetch() {
      this.list = await api.canvases.list()
      const curId = this.current?.id
      this.current = (curId != null && this.list.find(x => x.id === curId)) || this.list[0] || null
    },
    async create(d: any = {}) {
      const name = d.name || `画布 ${this.list.length + 1}`
      const c = await api.canvases.create({ name, nodes: [], edges: [], ...d })
      this.list.push(c); this.current = c; return c
    },
    async saveCurrent() { if (!this.current) return; const c = await api.canvases.update(this.current.id, this.current); const i = this.list.findIndex(x => x.id === c.id); if (i >= 0) this.list[i] = c; this.current = c },
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
