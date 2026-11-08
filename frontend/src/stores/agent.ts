import { defineStore } from 'pinia'
import { api } from '@/api'
import { useCanvasStore } from './canvas'

export const useAgentStore = defineStore('agent', {
  state: () => ({ list: [] as any[], groups: [] as any[] }),
  actions: {
    async fetch() {
      this.list = await api.agents.list()
      await this.fetchGroups()
    },
    async fetchGroups() {
      // 容错：分组接口不可用（如旧后端）时不应阻断智能体列表加载
      try {
        this.groups = await api.agent_groups.list()
      } catch {
        this.groups = []
      }
    },
    async create(d: any) { const a = await api.agents.create(d); this.list.push(a); return a },
    async update(id: number, d: any) {
      const a = await api.agents.update(id, d)
      const i = this.list.findIndex(x => x.id === id)
      if (i >= 0) this.list[i] = a
      return a
    },
    async remove(id: number) {
      await api.agents.remove(id)
      this.list = this.list.filter(x => x.id !== id)
      // 后端会同步清理画布节点/连线，刷新画布保持一致
      await useCanvasStore().fetch()
    },
    /**
     * 设置智能体的自定义头像色（画布调色板 → 选色 → 点节点）。
     * 走独立接口，不动提示词等其它字段；空串表示清除自定义色、回到默认配色。
     */
    async setAvatarColor(id: number, color: string) {
      const a = await api.agents.setAvatarColor(id, color || '')
      const i = this.list.findIndex(x => x.id === id)
      if (i >= 0) this.list[i] = { ...this.list[i], avatar_color: a.avatar_color || '' }
      return a
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
    },
    // 将智能体归纳到某个分组；groupId=null 表示移回"全部"组（从当前组移除）
    async assignToGroup(agentId: number, groupId: number | null) {
      const a = this.list.find(x => x.id === agentId)
      if (!a) return
      await this.update(agentId, { ...a, group_id: groupId })
    },
    async createGroup(d: any) {
      const g = await api.agent_groups.create(d)
      this.groups.push(g)
      return g
    },
    async updateGroup(id: number, d: any) {
      const g = await api.agent_groups.update(id, d)
      const i = this.groups.findIndex(x => x.id === id)
      if (i >= 0) this.groups[i] = g
      return g
    },
    /** 删除分组：组本身删除，组内智能体移回「全部」组（智能体保留） */
    async removeGroup(id: number) {
      await api.agent_groups.remove(id)
      this.groups = this.groups.filter(x => x.id !== id)
      // 后端删除组时已把该组内智能体的 group_id 置空，同步本地
      this.list.forEach(a => { if (a.group_id === id) a.group_id = null })
    },
    /** 清空分组：保留组，组内智能体移回「全部」组（智能体保留） */
    async clearGroup(id: number) {
      await api.agent_groups.clear(id)
      this.list.forEach(a => { if (a.group_id === id) a.group_id = null })
    },
    /** 删除组和智能体：组与组内智能体全部删除，后端会清理画布节点/连线 */
    async removeGroupWithAgents(id: number) {
      await api.agent_groups.purge(id)
      this.list = this.list.filter(a => a.group_id !== id)
      this.groups = this.groups.filter(x => x.id !== id)
      // 后端已清理这些智能体在所有画布上的节点与连线，刷新画布保持一致
      await useCanvasStore().fetch()
    },
    async reorderGroups(ids: number[]) {
      // 乐观更新：先按新顺序本地重排，界面立即响应
      const pos = new Map(ids.map((id, i) => [id, i]))
      this.groups = [...this.groups].sort(
        (a, b) => (pos.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (pos.get(b.id) ?? Number.MAX_SAFE_INTEGER)
      )
      try {
        await api.agent_groups.reorder(ids)
      } catch {
        await this.fetchGroups()
      }
    }
  }
})
