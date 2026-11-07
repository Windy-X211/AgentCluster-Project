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

export function getAvatarInitial(name: string | undefined | null): string {
    if (!name) return '?'
    return name.slice(0, 1).toUpperCase()
}