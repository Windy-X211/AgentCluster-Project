/**
 * 定时触发的「人话 ⇄ 结构化 ⇄ cron」单一事实源。
 *
 * 设计约束（和 conversation.ts 的运行时匹配器必须严格一致）：
 *   只生成、只描述 5 字段 cron 的子集：`*` `,` `-` `/`，周字段 0-6（0=周日）。
 *   不引入 cronstrue / cron-parser 之类依赖，避免「界面说是每月最后周五、
 *   运行时却根本不匹配」的错位——UI 能力 = 运行时能力。
 *
 * 存储约定（agent.trigger）：
 *   { type: 'manual' | 'schedule' | 'once' | 'cron'(旧值，仅读),
 *     cron: '30 9 * * 1-5',   载体/运行时唯一依据
 *     at: '2026-11-22 09:30'  仅 once
 *     sched: Sched            UI 回填用（缺省时由 cron 反推）
 *     message, catch_up, last_run_at }
 */

export type SchedKind = 'every' | 'daily' | 'weekly' | 'monthly' | 'once' | 'cron'
export type SchedUnit = 'minute' | 'hour'

export interface Sched {
  kind: SchedKind
  /** kind=every：间隔数值 */
  every?: number
  /** kind=every：间隔单位 */
  unit?: SchedUnit
  /** daily/weekly/monthly/once：HH:mm */
  time?: string
  /** weekly：0=周日 … 6=周六 */
  days?: number[]
  /** monthly：1-31 */
  dom?: number
  /** once：YYYY-MM-DD */
  date?: string
  /** kind=cron：原始表达式 */
  cron?: string
}

export interface CronIssue {
  field: 'count' | 'minute' | 'hour' | 'dayOfMonth' | 'month' | 'dayOfWeek' | 'chars'
  msg: string
}

export const WEEK_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
/** 分钟间隔：只给能整除 60 的值，保证整点边界不跳档 */
export const MINUTE_STEPS = [1, 2, 5, 10, 15, 20, 30, 60]
/** 小时间隔：只给能整除 24 的值 */
export const HOUR_STEPS = [1, 2, 3, 4, 6, 8, 12]

const pad2 = (n: number) => (n < 10 ? `0${n}` : `${n}`)

/** 'HH:mm' → [h, m]；非法返回 null */
export function parseHHmm(s: string | null | undefined): [number, number] | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec((s || '').trim())
  if (!m) return null
  const h = Number(m[1])
  const mm = Number(m[2])
  if (h > 23 || mm > 59) return null
  return [h, mm]
}

export function fmtHHmm(h: number, m: number) {
  return `${pad2(h)}:${pad2(m)}`
}

/* ------------------------------------------------------------------ *
 * 结构化 → cron
 * ------------------------------------------------------------------ */

/** 连续值压缩：1,2,3,4,5 → '1-5'；1,3,5 → '1,3,5' */
function compressNums(nums: number[]): string {
  const arr = [...new Set(nums)].sort((a, b) => a - b)
  if (!arr.length) return '*'
  const out: string[] = []
  let start = arr[0]
  let prev = arr[0]
  for (let i = 1; i <= arr.length; i++) {
    const cur = arr[i]
    if (i === arr.length || cur !== prev + 1) {
      out.push(start === prev ? `${start}` : `${start}-${prev}`)
      if (i === arr.length) break
      start = cur
    }
    prev = cur
  }
  return out.join(',')
}

/** 结构化 → cron（once 返回空串；kind=cron 原样返回） */
export function buildCron(s: Sched | null | undefined): string {
  if (!s) return ''
  if (s.kind === 'cron') return (s.cron || '').trim()
  if (s.kind === 'once') return ''
  if (s.kind === 'every') {
    const n = Math.max(1, Math.floor(Number(s.every) || 1))
    if (s.unit === 'hour') return n === 1 ? '0 * * * *' : `0 */${n} * * *`
    return n === 1 ? '* * * * *' : `*/${n} * * * *`
  }
  const t = parseHHmm(s.time) || [9, 0]
  const [h, m] = t
  if (s.kind === 'daily') return `${m} ${h} * * *`
  if (s.kind === 'weekly') {
    const days = (s.days && s.days.length ? s.days : [1]).filter(d => d >= 0 && d <= 6)
    return `${m} ${h} * * ${compressNums(days)}`
  }
  if (s.kind === 'monthly') {
    const dom = Math.min(31, Math.max(1, Math.floor(Number(s.dom) || 1)))
    return `${m} ${h} ${dom} * *`
  }
  return ''
}

/* ------------------------------------------------------------------ *
 * cron 校验 / 匹配
 * ------------------------------------------------------------------ */

const FIELD_RANGE: Record<string, [number, number]> = {
  minute: [0, 59], hour: [0, 23], dayOfMonth: [1, 31], month: [1, 12], dayOfWeek: [0, 6],
}
const FIELD_CN: Record<string, string> = {
  minute: '分钟', hour: '小时', dayOfMonth: '日', month: '月', dayOfWeek: '周',
}

function checkField(field: keyof typeof FIELD_RANGE, raw: string): CronIssue | null {
  const [lo, hi] = FIELD_RANGE[field]
  if (raw === '*') return null
  if (!/^[\d*,/-]+$/.test(raw)) {
    return { field: 'chars', msg: `${FIELD_CN[field]}字段含非法字符：${raw}（只支持 * , - / 和数字）` }
  }
  for (const part of raw.split(',')) {
    if (!part) return { field: 'chars', msg: `${FIELD_CN[field]}字段有空项（多余的逗号）` }
    const slash = part.split('/')
    if (slash.length > 2) return { field: 'chars', msg: `${FIELD_CN[field]}字段 / 只能出现一次：${part}` }
    const base = slash[0]
    if (slash.length === 2) {
      const step = Number(slash[1])
      if (!step || step < 1) return { field: 'chars', msg: `${FIELD_CN[field]}字段的步长必须是正整数：${part}` }
    }
    if (base === '*') continue
    if (base.includes('-')) {
      const [a, b] = base.split('-').map(Number)
      if (Number.isNaN(a) || Number.isNaN(b)) return { field: 'chars', msg: `${FIELD_CN[field]}字段区间写法不对：${part}` }
      if (a < lo || b > hi || a > b) {
        return { field: field as any, msg: `${FIELD_CN[field]}应在 ${lo}-${hi} 之间且左小右大：${part}` }
      }
      continue
    }
    const v = Number(base)
    if (Number.isNaN(v)) return { field: 'chars', msg: `${FIELD_CN[field]}字段无法识别：${part}` }
    if (v < lo || v > hi) {
      if (field === 'dayOfWeek' && v === 7) {
        return { field: 'dayOfWeek', msg: '周字段用 0 表示周日（不支持 7）' }
      }
      return { field: field as any, msg: `${FIELD_CN[field]}应在 ${lo}-${hi} 之间：${v}` }
    }
  }
  return null
}

/** 5 字段 cron 校验（与运行时匹配器同能力） */
export function checkCron(expr: string): { ok: boolean; issues: CronIssue[]; warn?: string } {
  const parts = (expr || '').trim().split(/\s+/).filter(Boolean)
  if (!expr || !expr.trim()) return { ok: false, issues: [{ field: 'count', msg: '表达式为空' }] }
  if (parts.length !== 5) {
    return { ok: false, issues: [{ field: 'count', msg: `需要 5 段（分 时 日 月 周），当前 ${parts.length} 段` }] }
  }
  const issues: CronIssue[] = []
  const names: (keyof typeof FIELD_RANGE)[] = ['minute', 'hour', 'dayOfMonth', 'month', 'dayOfWeek']
  for (let i = 0; i < 5; i++) {
    const issue = checkField(names[i], parts[i])
    if (issue) issues.push(issue)
  }
  let warn: string | undefined
  if (parts[2] !== '*' && parts[4] !== '*') {
    warn = '「日」和「周」同时写死是"或"关系（cron 规则），不是"且"；一般只写其中一个'
  }
  return { ok: !issues.length, issues, warn }
}

/** 单字段匹配（* , - /） */
function fieldMatch(field: string, value: number): boolean {
  if (!field || field === '*') return true
  for (const part of field.split(',')) {
    if (part.includes('/')) {
      const [range, stepStr] = part.split('/')
      const step = parseInt(stepStr, 10)
      if (!step || step < 1) continue
      const base = range === '*' ? 0 : parseInt(range, 10)
      if (Number.isNaN(base)) continue
      if (value >= base && (value - base) % step === 0) return true
    } else if (part.includes('-')) {
      const [a, b] = part.split('-').map(x => parseInt(x, 10))
      if (!Number.isNaN(a) && !Number.isNaN(b) && value >= a && value <= b) return true
    } else {
      if (parseInt(part, 10) === value) return true
    }
  }
  return false
}

/** 5 字段 cron 匹配（分钟精度）——运行时唯一判定入口 */
export function cronMatch(expr: string, d: Date): boolean {
  const parts = (expr || '').trim().split(/\s+/)
  if (parts.length !== 5) return false
  const [min, hour, dom, mon, dow] = parts
  return fieldMatch(min, d.getMinutes()) && fieldMatch(hour, d.getHours()) &&
    fieldMatch(dom, d.getDate()) && fieldMatch(mon, d.getMonth() + 1) && fieldMatch(dow, d.getDay())
}

/* ------------------------------------------------------------------ *
 * cron → 结构化（老配置回填 UI）
 * ------------------------------------------------------------------ */

export function parseCron(expr: string): Sched | null {
  const check = checkCron(expr)
  if (!check.ok) return null
  const [min, hour, dom, mon, dow] = expr.trim().split(/\s+/)

  // 每隔 N 分钟
  if (min === '*') {
    if (hour === '*' && dom === '*' && mon === '*' && dow === '*') return { kind: 'every', every: 1, unit: 'minute' }
    return null
  }
  const minStep = /^\*\/\d+$/.exec(min)
  if (minStep) {
    if (hour === '*' && dom === '*' && mon === '*' && dow === '*') {
      return { kind: 'every', every: parseInt(minStep[0].slice(2), 10), unit: 'minute' }
    }
    return null
  }
  // 每隔 N 小时
  if (dom === '*' && mon === '*' && dow === '*') {
    const hourStep = /^\*\/(\d+)$/.exec(hour)
    if (hourStep) {
      const n = parseInt(hourStep[1], 10)
      if (min === '0') return { kind: 'every', every: n, unit: 'hour' }
      return null
    }
  }
  // 每小时整点
  if (min === '0' && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
    return { kind: 'every', every: 1, unit: 'hour' }
  }
  if (!/^\d+$/.test(min) || !/^\d+$/.test(hour)) return null
  const time = `${pad2(Number(hour))}:${pad2(Number(min))}`
  if (mon === '*' && dow === '*') {
    if (dom === '*') return { kind: 'daily', time }
    if (/^\d+$/.test(dom)) return { kind: 'monthly', dom: Number(dom), time }
    return null
  }
  if (mon === '*' && dom === '*') {
    // 展开周字段（支持 1-5 / 1,3,5 / 单值）
    const days: number[] = []
    for (const part of dow.split(',')) {
      if (part.includes('-')) {
        const [a, b] = part.split('-').map(Number)
        if (Number.isNaN(a) || Number.isNaN(b)) return null
        for (let i = a; i <= b; i++) days.push(i)
      } else if (/^\d+$/.test(part)) {
        days.push(Number(part))
      } else return null
    }
    if (days.length) return { kind: 'weekly', days, time }
  }
  return { kind: 'cron', cron: expr.trim() }
}

/** 统一的「配置 → 结构化」出口：优先 sched，其次由 cron 反推，兜底高级模式 */
export function schedFromTrigger(t: any): Sched {
  const trig = t || {}
  if (trig.type === 'once') {
    const raw = String(trig.at || '')
    const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})/.exec(raw)
    if (m) return { kind: 'once', date: m[1], time: m[2] }
    return { kind: 'once', date: isoDate(new Date()), time: '09:00' }
  }
  if (trig.sched && typeof trig.sched === 'object' && trig.sched.kind) {
    return { ...trig.sched }
  }
  const cron = String(trig.cron || '').trim()
  if (!cron) return { kind: 'daily', time: '09:00' }
  return parseCron(cron) || { kind: 'cron', cron }
}

/* ------------------------------------------------------------------ *
 * 人话描述
 * ------------------------------------------------------------------ */

export function describe(s: Sched | null | undefined): string {
  if (!s) return '未设置'
  if (s.kind === 'once') {
    const d = parseLocal(s.date, s.time)
    if (!d) return '仅执行一次（时间未填）'
    return `仅执行一次 · ${fmtWhen(d)}`
  }
  if (s.kind === 'every') {
    const n = Math.max(1, Number(s.every) || 1)
    if (s.unit === 'hour') return n === 1 ? '每小时整点' : `每 ${n} 小时`
    return n === 1 ? '每分钟' : `每 ${n} 分钟`
  }
  const t = parseHHmm(s.time) || [9, 0]
  const hh = fmtHHmm(t[0], t[1])
  if (s.kind === 'daily') return `每天 ${hh}`
  if (s.kind === 'weekly') {
    const days = [...new Set((s.days && s.days.length ? s.days : [1]) as number[])].sort((a, b) => a - b)
    const label = days.length === 7
      ? '每天'
      : (days.join(',') === '1,2,3,4,5' ? '每个工作日' : `每周${days.map(d => WEEK_LABELS[d]?.replace('周', '') ?? d).join('、')}`)
    return `${label} ${hh}`
  }
  if (s.kind === 'monthly') return `每月 ${Math.min(31, Math.max(1, Number(s.dom) || 1))} 日 ${hh}`
  if (s.kind === 'cron') return `自定义规则 · ${s.cron || '(空)'}`
  return '未设置'
}

/** 任意 trigger / 表达式 → 人话 */
export function describeAny(cronOrSched: string | Sched | null | undefined): string {
  if (!cronOrSched) return '未设置'
  if (typeof cronOrSched === 'string') {
    const s = parseCron(cronOrSched)
    return s ? describe(s) : `自定义规则 · ${cronOrSched}`
  }
  return describe(cronOrSched)
}

/* ------------------------------------------------------------------ *
 * 下一次触发时间
 * ------------------------------------------------------------------ */

export function parseLocal(date?: string, time?: string): Date | null {
  if (!date) return null
  const t = parseHHmm(time) || [9, 0]
  const d = new Date(`${date}T${fmtHHmm(t[0], t[1])}:00`)
  return Number.isNaN(d.getTime()) ? null : d
}

export function isoDate(d: Date) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 未来 N 次 cron 触发（分钟精度；最多向后找 70 天，防死循环） */
export function nextCronRuns(expr: string, from: Date, count = 3): Date[] {
  const out: Date[] = []
  if (!checkCron(expr).ok) return out
  const d = new Date(from.getTime())
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() + 1) // 从下一分钟起算，当前分钟已过一半就不算数
  const limit = 60 * 24 * 180 // 最多向后找 180 天（月任务也能凑满 3 次，同时防「永远不匹配」死循环）
  for (let i = 0; i < limit && out.length < count; i++) {
    if (cronMatch(expr, d)) out.push(new Date(d.getTime()))
    d.setMinutes(d.getMinutes() + 1)
  }
  return out
}

/** 未来 N 次触发（once 只返回该时间，过期则空） */
export function nextRuns(s: Sched | null | undefined, from = new Date(), count = 3): Date[] {
  if (!s) return []
  if (s.kind === 'once') {
    const d = parseLocal(s.date, s.time)
    return d && d.getTime() > from.getTime() ? [d] : []
  }
  const cron = buildCron(s)
  return nextCronRuns(cron, from, count)
}

export function fmtWhen(d: Date, now = new Date()): string {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((target.getTime() - today.getTime()) / 86400000)
  const prefix = diff === 0 ? '今天' : diff === 1 ? '明天' : diff === 2 ? '后天' : ''
  const body = `${d.getMonth() + 1}月${d.getDate()}日(${WEEK_LABELS[d.getDay()]}) ${fmtHHmm(d.getHours(), d.getMinutes())}`
  return prefix ? `${prefix} ${body}` : body
}

/** 距离下次触发的人话倒计时（0 分钟内返回"即将触发"） */
export function humanizeGap(d: Date, now = new Date()): string {
  const ms = d.getTime() - now.getTime()
  if (ms <= 0) return '即将触发'
  const min = Math.floor(ms / 60000)
  if (min < 60) return `${min} 分钟后`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时后`
  return `${Math.floor(hr / 24)} 天后`
}

/* ------------------------------------------------------------------ *
 * 报警约束：表单只产出运行时能匹配的值
 * ------------------------------------------------------------------ */

export function clampEvery(n: number, unit: SchedUnit): number {
  const steps = unit === 'hour' ? HOUR_STEPS : MINUTE_STEPS
  const v = Math.floor(Number(n) || steps[0])
  return steps.includes(v) ? v : steps[0]
}

/** 表单级校验（结构化输入本身就受限，这里兜「一次性」的日期合法性） */
export function checkSched(s: Sched, now = new Date()): string | null {
  if (s.kind === 'once') {
    if (!s.date) return '请选择执行日期'
    if (!parseHHmm(s.time || '')) return '请选择执行时间'
    const d = parseLocal(s.date, s.time)
    if (!d) return '请选择执行日期和时间'
    if (d.getTime() <= now.getTime()) return '执行时间已经过去，请选一个将来的时间'
    return null
  }
  if (s.kind === 'daily' || s.kind === 'weekly' || s.kind === 'monthly') {
    if (!parseHHmm(s.time || '')) return '请选择触发时间'
  }
  if (s.kind === 'weekly' && !(s.days && s.days.length)) return '请至少选择一天'
  if (s.kind === 'monthly') {
    const dom = Number(s.dom)
    if (!dom || dom < 1 || dom > 31) return '日期应在 1-31 之间'
  }
  if (s.kind === 'cron') {
    const c = checkCron(s.cron || '')
    if (!c.ok) return c.issues[0].msg
  }
  if (s.kind === 'every') {
    if (s.unit === 'hour' && !HOUR_STEPS.includes(Number(s.every))) return '小时间隔只支持 1/2/3/4/6/8/12'
    if (s.unit === 'minute' && !MINUTE_STEPS.includes(Number(s.every))) return '分钟间隔只支持 1/2/5/10/15/20/30/60'
  }
  return null
}
