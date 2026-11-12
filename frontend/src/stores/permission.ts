import { defineStore } from 'pinia'
import { api } from '@/api'

/** 后端 P: 行 / 子智能体 permission 事件里的危险操作确认请求 */
export interface PermRequest {
  id: string
  tool: string
  label?: string
  args?: any
  conv_id?: number | null
  agent_id?: number | null
  /** 后端等待时长（秒），超时按拒绝处理 */
  timeout?: number
  /** GET /api/permissions/pending 补拉时的剩余秒数 */
  remaining?: number
  ts?: number
}

/**
 * 危险操作手动确认（设置 → 权限设置 → 操作权限 = 手动确认时启用）。
 *
 * 请求来源两路，都汇到本 store：
 *   1) 主循环 SSE 的 `P:{json}` 行（agent_runtime._exec_tool_gated 在派发工具前 yield）
 *   2) 子智能体事件 SUB: 里的 {t:"permission", ...}（subagent 的确认走事件队列）
 * 用户点允许/拒绝 → POST /api/permissions/confirm 唤醒后端阻塞的执行。
 */
export const usePermissionStore = defineStore('permission', {
  state: () => ({
    pending: [] as PermRequest[],
    /** 首次挂载时是否已从后端补拉过（刷新页面时把漏掉的请求找回来） */
    hydrated: false,
  }),
  actions: {
    push(req: PermRequest | null | undefined) {
      if (!req?.id) return
      if (this.pending.some(p => p.id === req.id)) return
      this.pending.push({ ts: Date.now(), ...req })
    },
    /** 断线重连 / 首次进入时补拉挂起请求（失败静默，弹窗本来就是 best-effort） */
    async hydrate() {
      if (this.hydrated) return
      this.hydrated = true
      try {
        const list = await api.permissions.pending()
        for (const r of list || []) this.push(r)
      } catch { /* 后端没起 / 无挂起请求，忽略 */ }
    },
    /** 后端已超时/已停止的请求：本地移除（弹窗消失） */
    expire(id: string) {
      this.pending = this.pending.filter(p => p.id !== id)
    },
    /** 用户做出选择：先关弹窗再上报（上报失败也不该把弹窗卡住） */
    async decide(id: string, approve: boolean) {
      this.expire(id)
      try {
        await api.permissions.confirm(id, approve)
      } catch { /* 请求已超时（404）或网络失败：后端会自行按拒绝/超时收场 */ }
    },
  }
})
