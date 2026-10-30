/**
 * viz 协议解析 —— 纯函数，无 Vue 依赖。
 * 从消息文本中提取 ```viz:type 代码块和裸 {"__viz__":...} JSON，切成 md 段 + viz 段。
 */

export interface VizSegment {
    kind: 'md' | 'viz'
    text?: string        // kind === 'md' 时的 markdown 文本
    type?: string        // kind === 'viz' 时的可视化类型
    data?: any           // kind === 'viz' 时的解析后数据（解析失败为 null）
    raw?: string         // kind === 'viz' 时的原始 JSON 字符串
}

/**
 * 把消息内容切成 md 段 + viz 段。
 * 识别两种触发：
 *  1. ```viz:<type>\n{...json...}\n```  代码块
 *  2. 整条消息是 JSON 且含 __viz__ 字段：{"__viz__":{"type":"...","data":{...}}}
 */
export function parseVizSegments(content: string): VizSegment[] {
    if (!content) return [{ kind: 'md', text: '' }]

    // 情况 2：整条消息是裸 __viz__ JSON
    const trimmed = content.trim()
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        try {
            const obj = JSON.parse(trimmed)
            if (obj && obj.__viz__ && obj.__viz__.type) {
                return [{ kind: 'viz', type: String(obj.__viz__.type), data: obj.__viz__.data || {}, raw: trimmed }]
            }
        } catch { /* 不是合法 viz JSON，走代码块解析 */ }
    }

    // 情况 1：扫描 ```viz:type 代码块
    const segs: VizSegment[] = []
    const re = /```viz:([a-zA-Z0-9_-]+)\s*\n([\s\S]*?)```/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(content)) !== null) {
        if (m.index > last) {
            segs.push({ kind: 'md', text: content.slice(last, m.index) })
        }
        const type = m[1]
        const raw = m[2]
        let data: any = null
        try { data = JSON.parse(raw) } catch { /* 解析失败保留 raw，渲染器回退 */ }
        segs.push({ kind: 'viz', type, data, raw })
        last = re.lastIndex
    }
    if (last < content.length) {
        segs.push({ kind: 'md', text: content.slice(last) })
    }
    if (!segs.length) segs.push({ kind: 'md', text: content })
    return segs
}

/** 是否含 viz 段（用于快速判断是否需要分段渲染） */
export function hasVizSegment(content: string): boolean {
    if (!content) return false
    if (/```viz:/.test(content)) return true
    const trimmed = content.trim()
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        try {
            const obj = JSON.parse(trimmed)
            if (obj && obj.__viz__ && obj.__viz__.type) return true
        } catch { /* ignore */ }
    }
    return false
}