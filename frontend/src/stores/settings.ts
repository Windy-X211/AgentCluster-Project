import { defineStore } from 'pinia'
import { api } from '@/api'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    data: {} as any,
    loaded: false,           // fetch() 是否已写入 data（区分「未加载」与「未设置某字段」，供就绪监听用）
    commandsMeta: {} as any,   // {name: {label, params, group?, hidden?}}
    commandGroups: [] as string[],  // 命令分组展示顺序
    skillsMeta: {} as any,     // {name: {label, description, prompt, version, triggers, tags}}
    pluginsMeta: {} as any,    // {name: {label, params, description}}
    combos: {} as Record<string, import('@/api').ComboRecord>,  // 组合技（跨类型能力组合）
    asrAvailable: false,       // 语音识别是否可用（funasr-onnx 已安装）
    asrEnabled: localStorage.getItem('agentcluster.asrEnabled') !== 'false',  // 用户启停开关，默认开启
    ctxBadgeEnabled: localStorage.getItem('agentcluster.ctxBadge') !== 'false',  // 上下文长度指示开关，默认显示
  }),
  actions: {
    async fetch() {
      this.data = await api.settings.get()
      this.loaded = true
      const caps = await api.capabilities.all()
      this.commandsMeta = caps.commands || {}
      this.commandGroups = caps.command_groups || []
      this.skillsMeta = caps.skills || {}
      this.pluginsMeta = caps.plugins || {}
      try { this.combos = await api.settings.getCombos() } catch { this.combos = {} }
      try { this.asrAvailable = (await api.asr.status()).available } catch { this.asrAvailable = false }
    },
    setAsrEnabled(v: boolean) {
      this.asrEnabled = v
      localStorage.setItem('agentcluster.asrEnabled', String(v))
    },
    setCtxBadgeEnabled(v: boolean) {
      this.ctxBadgeEnabled = v
      localStorage.setItem('agentcluster.ctxBadge', String(v))
    },
    /** 让后端热加载插件/技能目录，再把最新元数据拉回前端（无需重启）。 */
    async reloadCapabilities() {
      await api.capabilities.reload()
      const caps = await api.capabilities.all()
      this.commandsMeta = caps.commands || {}
      this.commandGroups = caps.command_groups || []
      this.skillsMeta = caps.skills || {}
      this.pluginsMeta = caps.plugins || {}
    },
    /** 刷新组合技数据（能力管理/市场下载安装后调用） */
    async reloadCombos() {
      try { this.combos = await api.settings.getCombos() } catch { this.combos = {} }
    },
    async update(d: any) { this.data = await api.settings.update({ ...this.data, ...d }) },
    /** 设置全局默认工作区（会话级/画布级都没设时生效）。
     *  空串 = 清除（回退到进程 CWD）。设置成功后同步写入工作区历史，
     *  让它出现在对话区工作区下拉里。 */
    async setDefaultWorkdir(path: string) {
      const v = (path || '').trim()
      const res = await api.settings.setDefaultWorkdir(v)
      this.data = { ...this.data, default_workdir: res?.default_workdir || '' }
      if (res?.default_workdir) await this.recordWorkspace(res.default_workdir)
      return res
    },
    /** 工作区历史记录（settings.workspace_history，新在前）；确保已初始化（旧数据缺字段时播种） */
    async ensureWorkspaceHistory(seedPaths: string[] = []): Promise<string[]> {
      let list: string[] | undefined = this.data.workspace_history
      if (!Array.isArray(list)) {
        const seen = new Set<string>()
        list = []
        for (const p of seedPaths) {
          const v = (p || '').trim()
          if (v && !seen.has(v)) { seen.add(v); list.push(v) }
        }
        await this.update({ workspace_history: list })
      }
      return list
    },
    /** 记录一个用过的工作区（置顶去重，持久化） */
    async recordWorkspace(path: string) {
      const v = (path || '').trim()
      if (!v) return
      const cur = await this.ensureWorkspaceHistory()
      if (cur[0] === v) return
      const list = [v, ...cur.filter((x: string) => x !== v)]
      await this.update({ workspace_history: list })
    },
    /** 移除一条工作区记录（仅删记录，不影响任何会话/画布的当前设置） */
    async removeWorkspaceRecord(path: string) {
      const cur = await this.ensureWorkspaceHistory()
      const list = cur.filter((x: string) => x !== path)
      if (list.length === cur.length) return
      await this.update({ workspace_history: list })
    },
    applyDefaults(target: any) {
      if (!target.commands?.length) target.commands = [...(this.data.default_commands || [])]
      if (!target.skills?.length) target.skills = [...(this.data.default_skills || [])]
      if (!target.plugins?.length) target.plugins = [...(this.data.default_plugins || [])]
      if (!target.interface_id && this.data.default_model_interface) target.interface_id = this.data.default_model_interface
      return target
    }
  }
})
