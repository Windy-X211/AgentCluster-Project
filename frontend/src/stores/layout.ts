import { defineStore } from 'pinia'

const KEY = 'agentcluster.layout.v1'
export const RIGHT_MIN = 240
export const RIGHT_MAX = 820
export const RIGHT_DEFAULT = 300

function readWidth(): number {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return RIGHT_DEFAULT
    const p = JSON.parse(raw)
    const n = Number(p?.rightWidth)
    if (!isFinite(n) || n <= 0) return RIGHT_DEFAULT
    return Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, Math.round(n)))
  } catch {
    return RIGHT_DEFAULT
  }
}

function writeWidth(v: number) {
  try {
    const raw = localStorage.getItem(KEY)
    const prev = raw ? JSON.parse(raw) : {}
    localStorage.setItem(KEY, JSON.stringify({ ...prev, rightWidth: v }))
  } catch {
    /* localStorage 不可用时静默 */
  }
}

export const useLayoutStore = defineStore('layout', {
  state: () => ({
    rightWidth: readWidth(),
  }),
  actions: {
    setRightWidth(v: number) {
      const n = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, Math.round(v)))
      this.rightWidth = n
      writeWidth(n)
    },
    resetRightWidth() {
      this.setRightWidth(RIGHT_DEFAULT)
    },
  },
})
