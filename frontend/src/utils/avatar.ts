/**
 * 头像颜色生成工具
 * 根据字符串（ID或名称）生成稳定的颜色
 */

const PALETTE: [string, string][] = [
    ['#6ea8ff', '#b472ff'], // 蓝紫
    ['#ff6b9d', '#ff8e53'], // 粉橙
    ['#4ade80', '#22d3ee'], // 青绿
    ['#facc15', '#f97316'], // 黄橙
    ['#a78bfa', '#ec4899'], // 紫粉
    ['#34d399', '#60a5fa'], // 绿蓝
    ['#fb7185', '#f472b6'], // 红粉
    ['#2dd4bf', '#38bdf8'], // 青蓝
    ['#fbbf24', '#a3e635'], // 黄绿
    ['#c084fc', '#f472b6'], // 紫粉
    ['#fb923c', '#e879f9'], // 橙紫
    ['#22d3ee', '#818cf8'], // 青蓝
    ['#4ade80', '#facc15'], // 绿黄
    ['#f87171', '#60a5fa'], // 红蓝
    ['#a78bfa', '#34d399'], // 紫绿
    ['#fb7185', '#2dd4bf'], // 粉青
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

export function getAvatarInitial(name: string | undefined | null): string {
    if (!name) return '?'
    return name.slice(0, 1).toUpperCase()
}