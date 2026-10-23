import { defineStore } from 'pinia'
import { api } from '@/api'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    data: {} as any,
    commandsMeta: {} as any,   // {name: {label, params, group?, hidden?}}
    commandGroups: [] as string[],  // 命令分组展示顺序
    skillsMeta: {} as any,     // {name: {label, description, prompt, version, triggers, tags}}
    pluginsMeta: {} as any,    // {name: {label, params, description}}
    asrAvailable: false,       // 语音识别是否可用（funasr-onnx 已安装）
    asrEnabled: localStorage.getItem('agentcluster.asrEnabled') !== 'false',  // 用户启停开关，默认开启
  }),
  actions: {
    async fetch() {
      this.data = await api.settings.get()
      const caps = await api.capabilities.all()
      this.commandsMeta = caps.commands || {}
      this.commandGroups = caps.command_groups || []
      this.skillsMeta = caps.skills || {}
      this.pluginsMeta = caps.plugins || {}
      try { this.asrAvailable = (await api.asr.status()).available } catch { this.asrAvailable = false }
    },
    setAsrEnabled(v: boolean) {
      this.asrEnabled = v
      localStorage.setItem('agentcluster.asrEnabled', String(v))
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
    async update(d: any) { this.data = await api.settings.update({ ...this.data, ...d }) },
    applyDefaults(target: any) {
      if (!target.commands?.length) target.commands = [...(this.data.default_commands || [])]
      if (!target.skills?.length) target.skills = [...(this.data.default_skills || [])]
      if (!target.plugins?.length) target.plugins = [...(this.data.default_plugins || [])]
      if (!target.interface_id && this.data.default_model_interface) target.interface_id = this.data.default_model_interface
      return target
    }
  }
})
