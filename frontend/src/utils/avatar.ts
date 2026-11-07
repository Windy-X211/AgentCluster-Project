/**
 * 头像颜色生成工具
 * 根据字符串（ID或名称）生成稳定的颜色
 *
 * 配色收敛：原 16 组彩色渐变（粉/橙/青/紫/橄榄…）过于花哨，
 * 统一为一组低饱和的石板灰蓝调，只靠明度做细微区分，
 * 头像之间的辨识度由首字母承担，整体界面只保留一个强调色。
 */

const PALETTE: [string, string][] = [
    ['#4b5563', '#69737f'], // 石板灰
    ['#4a5566', '#687486'], // 深灰蓝
    ['#525a67', '#707a89'], // 灰蓝
    ['#4f5860', '#6d767f'], // 钢灰
    ['#555a64', '#73787f'], // 银灰
    ['#4d545c', '#6b727a'], // 炭灰
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