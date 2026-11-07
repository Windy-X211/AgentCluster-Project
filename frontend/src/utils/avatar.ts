/**
 * 头像颜色生成工具
 * 根据字符串（ID或名称）生成稳定的颜色
 *
 * 差异化配色：16 组低饱和渐变，按 agent_id / 名称哈希取色，
 * 保证不同智能体的头像一眼可辨（设置中心可切换为「统一」强调色风格）。
 */

const PALETTE: [string, string][] = [
    ['#6376b8', '#8d73c4'], // 雾蓝紫
    ['#c47f8d', '#c98f72'], // 陶粉橙
    ['#56998d', '#548fa8'], // 灰青蓝
    ['#b89558', '#ba7f59'], // 暖杏
    ['#8f76c4', '#bb6f98'], // 灰紫粉
    ['#52987f', '#5a80c4'], // 薄荷蓝
    ['#bd7288', '#ba71a2'], // 干枯玫瑰
    ['#4f9ca3', '#4f82b8'], // 青蓝
    ['#a79352', '#839f61'], // 橄榄
    ['#9772c4', '#ba71a2'], // 紫粉
    ['#bd8060', '#ab77ba'], // 橙紫
    ['#4f9ca8', '#6d79c4'], // 青紫
    ['#5e9e6c', '#b89558'], // 绿黄
    ['#ba6c6c', '#5a80c4'], // 砖红蓝
    ['#8f76c4', '#52987f'], // 紫绿
    ['#bd7288', '#4f9ca3'], // 粉青
]

function hashStr(s: string): number {
    let hash = 0
    for (let i = 0; i < s.length; i++) {
        const ch = s.charCodeAt(i)
        hash = ((hash << 5) - hash) + ch
        hash = hash & hash
    }
    return Math.abs(hash)
}

export function getAvatarColors(nameOrId: string | number): [string, string] {
    const hash = hashStr(String(nameOrId))
    return PALETTE[hash % PALETTE.length]
}

export function getAvatarGradient(nameOrId: string | number): string {
    const [c1, c2] = getAvatarColors(nameOrId)
    return `linear-gradient(135deg, ${c1}, ${c2})`
}

/** 十六进制色 → [r, g, b] */
export function hexToRgb(hex: string): [number, number, number] {
    const h = hex.replace('#', '')
    const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
    const n = parseInt(full, 16)
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/**
 * 智能体身份色（画布连线用）
 * 与头像取色同源：同一智能体在头像与连线上是同一个色相，
 * 保证连线既能被主题调和，又能区分归属。
 */
export function getAvatarRgb(nameOrId: string | number): string {
    const [c1] = getAvatarColors(nameOrId)
    const [r, g, b] = hexToRgb(c1)
    return `rgb(${r}, ${g}, ${b})`
}

/**
 * 调色板去重后的 rgb 列表（固定顺序）
 * 供「同组同色、异组异色」的场景依次取空闲色用。
 */
export function getPaletteRgbPool(): string[] {
    const seen = new Set<string>()
    const out: string[] = []
    for (const pair of PALETTE) {
        for (const hex of pair) {
            const [r, g, b] = hexToRgb(hex)
            const s = `rgb(${r}, ${g}, ${b})`
            if (!seen.has(s)) { seen.add(s); out.push(s) }
        }
    }
    return out
}

/** HSL → RGB */
function hslToRgb(h: number, s: number, l: number): [number, number, number] {
    const a = s * Math.min(l, 1 - l)
    const f = (n: number) => {
        const k = (n + h / 30) % 12
        return l - a * Math.max(-1, Math.min(Math.min(k - 3, 9 - k), 1))
    }
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)]
}

/**
 * 调色板用尽后的兜底色：黄金角递进的色相环
 * 低饱和度与调色板观感一致，且任意数量都不会重色。
 */
export function generateRgb(index: number): string {
    const [r, g, b] = hslToRgb((index * 137.508 + 205) % 360, 0.42, 0.55)
    return `rgb(${r}, ${g}, ${b})`
}

export function getAvatarInitial(name: string | undefined | null): string {
    if (!name) return '?'
    return name.slice(0, 1).toUpperCase()
}

// ══════════════════════════════════════════════════════════════
//  自定义头像配色（画布调色板 → 选中颜色 → 点击智能体节点生效）
// ══════════════════════════════════════════════════════════════

/**
 * 弹窗预设色板：内置调色板去重后的全部颜色（16 组 × 2 = 32 色）
 * 与自动配色同源，用户挑中的颜色观感不会割裂。
 */
export const AVATAR_COLOR_PRESETS: string[] = (() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const pair of PALETTE) {
        for (const hex of pair) {
            if (!seen.has(hex)) { seen.add(hex); out.push(hex) }
        }
    }
    return out
})()

/** 十六进制色规范化：#rgb / #rrggbb → #rrggbb；非法返回 null */
export function normalizeHexColor(input: string | null | undefined): string | null {
    const raw = String(input || '').trim().toLowerCase()
    let s = raw.startsWith('#') ? raw.slice(1) : raw
    if (s.length === 3) s = s.split('').map(c => c + c).join('')
    if (!/^[0-9a-f]{6}$/.test(s)) return null
    return '#' + s
}

/** 十六进制 → HSL（h: 0-360，s/l: 0-1） */
export function hexToHsl(hex: string): [number, number, number] {
    const [r, g, b] = hexToRgb(hex)
    const rr = r / 255, gg = g / 255, bb = b / 255
    const max = Math.max(rr, gg, bb), min = Math.min(rr, gg, bb)
    const l = (max + min) / 2
    let h = 0, s = 0
    if (max !== min) {
        const d = max - min
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
        if (max === rr) h = (gg - bb) / d + (gg < bb ? 6 : 0)
        else if (max === gg) h = (bb - rr) / d + 2
        else h = (rr - gg) / d + 4
        h *= 60
    }
    return [h, s, l]
}

/** HSL → 十六进制 */
export function hslToHex(h: number, s: number, l: number): string {
    const [r, g, b] = hslToRgb(h, s, l)
    return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('')
}

/** 十六进制 → HSV（h: 0-360，s/v: 0-1）——色谱浮窗的取色模型 */
export function hexToHsv(hex: string): { h: number; s: number; v: number } {
    const [r, g, b] = hexToRgb(hex)
    const rr = r / 255, gg = g / 255, bb = b / 255
    const max = Math.max(rr, gg, bb), min = Math.min(rr, gg, bb)
    const d = max - min
    let h = 0
    if (d !== 0) {
        if (max === rr) h = ((gg - bb) / d) % 6
        else if (max === gg) h = (bb - rr) / d + 2
        else h = (rr - gg) / d + 4
        h *= 60
        if (h < 0) h += 360
    }
    return { h, s: max === 0 ? 0 : d / max, v: max }
}

/** HSV → 十六进制 */
export function hsvToHex(h: number, s: number, v: number): string {
    const c = v * s
    const hp = ((((h % 360) + 360) % 360) / 60)
    const x = c * (1 - Math.abs((hp % 2) - 1))
    let r = 0, g = 0, b = 0
    if (hp < 1) { r = c; g = x }
    else if (hp < 2) { r = x; g = c }
    else if (hp < 3) { g = c; b = x }
    else if (hp < 4) { g = x; b = c }
    else if (hp < 5) { r = x; b = c }
    else { r = c; b = x }
    const m = v - c
    return '#' + [r + m, g + m, b + m]
        .map(x2 => Math.round(Math.max(0, Math.min(1, x2)) * 255).toString(16).padStart(2, '0'))
        .join('')
}

/** 十六进制 → 'rgb(r, g, b)'（连线配色用） */
export function hexToRgbString(hex: string): string {
    const [r, g, b] = hexToRgb(hex)
    return `rgb(${r}, ${g}, ${b})`
}

/**
 * 由单一主色生成头像渐变。
 * 观感对齐内置调色板（135°、第二色相偏移 +18°、低饱和）；
 * 过亮/过暗的主色会被收敛到可读区间（保证白色首字始终看得清）。
 * 灰阶（无色相）保持中性，不强行补饱和度，避免纯黑/纯白被染成偏色。
 */
export function gradientFromColor(hex: string): string {
    const c = normalizeHexColor(hex)
    if (!c) return getAvatarGradient(0)
    let [h, s, l] = hexToHsl(c)
    const gray = s < 0.08          // 无色相：色相偏移无意义，保持中性灰
    if (!gray) s = Math.min(0.85, Math.max(0.28, s))
    l = Math.min(0.58, Math.max(0.30, l))
    const c1 = hslToHex(h, s, l)
    const c2 = gray
        ? hslToHex(h, s, Math.min(0.72, l + 0.10))
        : hslToHex((h + 18) % 360, s * 0.92, Math.min(0.72, l + 0.10))
    return `linear-gradient(135deg, ${c1}, ${c2})`
}

/**
 * 智能体头像背景（统一入口）：
 *   1) 手动指定的自定义色优先 —— 用户显式选择，最高优先级
 *   2) 统一风格 → 主题强调色
 *   3) 差异化 → 按 id 哈希取内置调色板
 */
export function agentAvatarGradient(agent: any, mode?: string | null): string {
    const custom = normalizeHexColor(agent?.avatar_color)
    if (custom) return gradientFromColor(custom)
    if ((mode || 'differentiated') === 'unified') return 'linear-gradient(135deg, var(--accent), var(--accent-2))'
    return getAvatarGradient(agent?.id ?? '')
}