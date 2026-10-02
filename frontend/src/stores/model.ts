import { defineStore } from 'pinia'
import { api } from '@/api'

export const useModelStore = defineStore('model', {
  state: () => ({ list: [] as any[] }),
  actions: {
    async fetch() { this.list = await api.models.list() },
    async create(d: any) { const m = await api.models.create(d); this.list.push(m); return m },
    async update(id: number, d: any) { const m = await api.models.update(id, d); const i = this.list.findIndex(x => x.id === id); if (i >= 0) this.list[i] = m; return m },
    async remove(id: number) { await api.models.remove(id); this.list = this.list.filter(x => x.id !== id) }
  }
})
