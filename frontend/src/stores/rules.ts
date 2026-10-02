import { defineStore } from 'pinia'
import { api } from '@/api'

export const useRulesStore = defineStore('rules', {
  state: () => ({ list: [] as any[], canvasId: null as number | null }),
  actions: {
    /** 拉取指定画布的规章制度（按画布隔离） */
    async fetch(canvasId?: number | null) {
      const cid = canvasId !== undefined ? canvasId : this.canvasId
      this.canvasId = cid ?? null
      this.list = cid != null ? await api.rules.list(cid) : []
    },
    async create(d: any) {
      const r = await api.rules.create({ canvas_id: this.canvasId, ...d })
      this.list.push(r)
      return r
    },
    async update(id: number, d: any) {
      const r = await api.rules.update(id, d)
      const i = this.list.findIndex(x => x.id === id)
      if (i >= 0) this.list[i] = r
      return r
    },
    async remove(id: number) {
      await api.rules.remove(id)
      this.list = this.list.filter(x => x.id !== id)
    }
  }
})
