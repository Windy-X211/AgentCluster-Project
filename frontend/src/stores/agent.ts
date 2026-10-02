import { defineStore } from 'pinia'
import { api } from '@/api'
import { useCanvasStore } from './canvas'

export const useAgentStore = defineStore('agent', {
  state: () => ({ list: [] as any[] }),
  actions: {
    async fetch() { this.list = await api.agents.list() },
    async create(d: any) { const a = await api.agents.create(d); this.list.push(a); return a },
    async update(id: number, d: any) { const a = await api.agents.update(id, d); const i = this.list.findIndex(x => x.id === id); if (i >= 0) this.list[i] = a; return a },
    async remove(id: number) {
      await api.agents.remove(id)
      this.list = this.list.filter(x => x.id !== id)
      // 后端会同步清理画布节点/连线，刷新画布保持一致
      await useCanvasStore().fetch()
    },
    async reorder(ids: number[]) {
      // 乐观更新：先按新顺序本地重排，界面立即响应
      const pos = new Map(ids.map((id, i) => [id, i]))
      this.list = [...this.list].sort(
        (a, b) => (pos.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (pos.get(b.id) ?? Number.MAX_SAFE_INTEGER)
      )
      try {
        await api.agents.reorder(ids)
      } catch {
        // 失败则回滚为后端真实顺序
        await this.fetch()
      }
    }
  }
})
