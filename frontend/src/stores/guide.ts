import { defineStore } from 'pinia'

const KEY = 'agentcluster.guide.v1'
const GUIDE_VERSION = 1

function readStore(): Record<string, any> {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return {}
    return JSON.parse(raw) || {}
  } catch {
    return {}
  }
}

function writeStore(patch: Record<string, any>) {
  try {
    const prev = readStore()
    localStorage.setItem(KEY, JSON.stringify({ ...prev, ...patch }))
  } catch {
    /* localStorage 不可用时静默 */
  }
}

function readCompleted(): boolean {
  const v = readStore()?.completed
  return v === true
}

export type GuideType = 'flow' | 'region'

export const useGuideStore = defineStore('guide', {
  state: () => ({
    completed: readCompleted(),
    version: GUIDE_VERSION,
    pending: '' as '' | GuideType,
  }),
  actions: {
    markCompleted() {
      this.completed = true
      writeStore({ completed: true, version: GUIDE_VERSION })
    },
    reset() {
      this.completed = false
      writeStore({ completed: false, version: GUIDE_VERSION })
    },
    requestGuide(type: GuideType) {
      this.pending = type
    },
    clearPending() {
      this.pending = ''
    },
  },
})