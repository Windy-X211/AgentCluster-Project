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
    error?: string       // kind === 'viz' 且 JSON 解析失败（自动修复也无效）时的错误说明
}

/**
 * 修复 JSON 中常见的闭合括号错误：
 *  1. 错配：对象用 ] 闭合 / 数组用 } 闭合 —— 按语法上下文替换为正确闭合符
 *  2. 漏闭合：扫描结束仍未闭合 —— 按栈序补齐（栈序即唯一正确的闭合序列）
 *  3. 数组内出现 "key": 形式 —— JSON 数组内不可能有 key:value，必然是数组漏闭合，在该 key 前补 ]
 * 全部为确定性修复（语法上下文唯一确定），不做猜测。
 * 无法修复（多余闭合符 / 字符串未闭合 / 无任何问题）返回 null。
 */
function fixBracketMismatch(raw: string): string | null {
    const stack: string[] = []
    const fixes: Array<{ i: number, ch: string, insert?: boolean }> = []
    let esc = false
    for (let i = 0; i < raw.length; i++) {
        const c = raw[i]
        if (esc) { esc = false; continue }
        if (c === '"') {
            let j = i + 1
            while (j < raw.length) {
                if (raw[j] === '\\') { j += 2; continue }
                if (raw[j] === '"') break
                j++
            }
            if (j >= raw.length) return null   // 字符串未闭合：不猜
            let k = j + 1
            while (k < raw.length && /\s/.test(raw[k])) k++
            if (raw[k] === ':' && stack.length && stack[stack.length - 1] === '[') {
                let ins = i
                while (ins > 0 && /\s/.test(raw[ins - 1])) ins--
                if (ins > 0 && raw[ins - 1] === ',') ins--
                fixes.push({ i: ins, ch: ']', insert: true })
                stack.pop()
            }
            i = j
            continue
        }
        if (c === '{' || c === '[') { stack.push(c); continue }
        if (c === '}' || c === ']') {
            const top = stack.pop()
            if (top === undefined) return null  // 多余闭合符：不猜
            const expect = top === '{' ? '}' : ']'
            if (c !== expect) fixes.push({ i, ch: expect })
        }
    }
    if (!fixes.length && !stack.length) return null
    let out = raw
    for (let k = fixes.length - 1; k >= 0; k--) {
        const f = fixes[k]
        out = f.insert ? out.slice(0, f.i) + f.ch + out.slice(f.i)
                       : out.slice(0, f.i) + f.ch + out.slice(f.i + 1)
    }
    for (let k = stack.length - 1; k >= 0; k--) {
        out += stack[k] === '{' ? '}' : ']'
    }
    return out
}

/** 解析 viz JSON：失败时先尝试括号错配修复，仍失败则返回 null 并给出错误说明 */
function parseVizJson(raw: string): { data: any, error?: string } {
    try {
        return { data: JSON.parse(raw) }
    } catch (e1) {
        const fixed = fixBracketMismatch(raw)
        if (fixed != null) {
            try { return { data: JSON.parse(fixed) } } catch { /* 修复后仍非法，按原始错误报出 */ }
        }
        return { data: null, error: `JSON 解析失败：${(e1 as Error).message}` }
    }
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
        const { data, error } = parseVizJson(raw)
        segs.push({ kind: 'viz', type, data, raw, error })
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