import { marked } from 'marked'

marked.setOptions({
  gfm: true,
  breaks: true,
  // @ts-expect-error marked v15+ 支持 autolink
  autolink: true,   // 自动把裸 http(s) URL 转为 <a> 标签
})

/**
 * 流式 markdown 补丁：在渲染前修复常见的不完整情况。
 * 只做"安全且精准"的补丁，绝不破坏已闭合的正常 markdown。
 */
function patchStreamingMarkdown(text: string): string {
  if (!text) return text

  let result = text

  // 1. 修复未闭合的代码块：统计 ``` 行数量，奇数补一个闭合
  //    只匹配行首的 ```（可能带语言标记），这样不会误匹配代码块内的 ``` 文本
  const codeFenceMatches = result.match(/^```/gm)
  if (codeFenceMatches && codeFenceMatches.length % 2 !== 0) {
    result = result.trimEnd() + '\n```'
  }

  // 2. 修复未闭合的行内反引号：在"代码块外"统计 `，奇数补一个
  //    先用正则剥离成对的代码块，剩下的内容里统计反引号
  const codeBlockStripped = result.replace(/^```[\s\S]*?```/gm, '')
  const backtickMatches = codeBlockStripped.match(/`/g)
  if (backtickMatches && backtickMatches.length % 2 !== 0) {
    result = result.trimEnd() + '`'
  }

  // 3. 修复最后一行的悬空链接：只在最后一行、且整行就是不完整链接时才补
  //    正常 markdown 的完整链接 [text](url) 已经闭合，不会匹配 $
  const lastLineIdx = result.lastIndexOf('\n')
  const lastLine = lastLineIdx >= 0 ? result.slice(lastLineIdx + 1) : result
  // 仅在最后一行"全部"是不完整链接/图片语法时才处理（更保守）
  const openLinkMatch = lastLine.match(/^!?\[([^\]]*)\]\(([^)]*)$/)
  if (openLinkMatch) {
    const replacement = openLinkMatch[0] + ')'
    result = result.slice(0, result.length - lastLine.length) + replacement
  }

  return result
}

const ALLOWED_TAGS: Record<string, string[]> = {
  h1: ['align'],
  h2: ['align'],
  h3: ['align'],
  h4: ['align'],
  h5: ['align'],
  h6: ['align'],
  p: [],
  br: [],
  hr: [],
  blockquote: [],
  ul: ['align'],
  ol: ['align', 'start'],
  li: ['value'],
  em: [],
  strong: [],
  s: [],
  del: [],
  code: ['class'],
  pre: [],
  a: ['href', 'title', 'target', 'rel'],
  img: ['src', 'alt', 'title', 'width', 'height', 'align'],
  table: [],
  thead: [],
  tbody: [],
  tfoot: [],
  tr: [],
  th: ['align', 'colspan', 'rowspan'],
  td: ['align', 'colspan', 'rowspan'],
  span: ['class', 'style'],
  input: ['type', 'checked', 'disabled'],
  details: [],
  summary: [],
  kbd: [],
}

const SAFE_STYLES = [
  'color',
  'font-family',
  'font-size',
  'font-style',
  'font-weight',
  'text-align',
  'text-decoration',
  'background-color',
  'background',
  'border',
  'border-color',
  'border-radius',
  'border-style',
  'border-width',
  'margin',
  'margin-top',
  'margin-bottom',
  'margin-left',
  'margin-right',
  'padding',
  'padding-top',
  'padding-bottom',
  'padding-left',
  'padding-right',
  'display',
  'opacity',
]

function escHtml(s: string): string {
  return s
    .replace(/&(?!#?\w+;)/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function safeAttr(name: string, value: string, allowed: Set<string>): string {
  const v = value.trim()
  if (name === 'href') {
    if (!/^(https?:|mailto:|#|\/)/i.test(v)) return ''
    if (/\s|javascript:|data:|vbscript:|file:/i.test(v)) return ''
  } else if (name === 'src') {
    if (!/^(https?:|data:image\/(png|jpe?g|gif|webp|svg\+xml)|blob:|#|\/)/i.test(v)) return ''
  } else if (name === 'target') {
    return v === '_blank' ? ' target="_blank" rel="noopener noreferrer"' : ''
  } else if (name === 'style') {
    const safe = v
      .split(';')
      .map(part => {
        const idx = part.indexOf(':')
        if (idx < 0) return ''
        const prop = part.slice(0, idx).trim().toLowerCase()
        const val = part.slice(idx + 1).trim()
        if (!SAFE_STYLES.includes(prop)) return ''
        if (/\burl\s*\(|expression\s*\(|behavior\s*:/i.test(val)) return ''
        return `${prop}:${val}`
      })
      .filter(Boolean)
      .join(';')
    return safe ? ` style="${escHtml(safe)}"` : ''
  }
  return ` ${name}="${escHtml(v)}"`
}

function sanitizeHtml(html: string): string {
  let out = ''
  let i = 0
  while (i < html.length) {
    const lt = html.indexOf('<', i)
    if (lt < 0) {
      out += escHtml(html.slice(i))
      break
    }
    out += escHtml(html.slice(i, lt))
    const isClose = html[lt + 1] === '/'
    const isSelfClose = html[lt + 1] === '!' || html[lt + 1] === '?'
    const close = html.indexOf('>', lt)
    if (close < 0) {
      out += escHtml(html.slice(lt))
      break
    }
    const tagContent = html.slice(lt, close + 1)
    out += isSelfClose ? escHtml(tagContent) : sanitizeTag(tagContent, isClose)
    i = close + 1
  }
  return out
}

function sanitizeTag(tag: string, isClose: boolean): string {
  let body = tag.slice(1)
  if (isClose) {
    const name = body.slice(1, body.length - 1).toLowerCase().trim()
    return ALLOWED_TAGS[name] ? tag : escHtml(tag)
  }
  const end = body.lastIndexOf('>')
  if (end <= 0) return escHtml(tag)
  const inner = body.slice(0, end)
  const selfClose = inner.endsWith('/')
  const headMatch = inner.match(/^([a-zA-Z][a-zA-Z0-9-]*)([\s\S]*)$/)
  if (!headMatch) return escHtml(tag)
  const name = headMatch[1].toLowerCase()
  if (!(name in ALLOWED_TAGS)) return escHtml(tag)
  const allowed = new Set(ALLOWED_TAGS[name])
  const attrs = (headMatch[2] || '').match(/([\w-]+)(?:\s*=\s*"([^"]*)"|'([^']*)')?/g)
  let attrStr = ''
  if (attrs) {
    for (const a of attrs) {
      const m = a.match(/^([\w-]+)(?:\s*=\s*"([^"]*)"|'([^']*)')?$/)
      if (!m) continue
      const an = m[1].toLowerCase()
      if (!allowed.has(an)) continue
      const val = m[2] ?? m[3] ?? ''
      attrStr += safeAttr(an, val, allowed)
    }
  }
  return selfClose ? `<${name}${attrStr}/>` : `<${name}${attrStr}>`
}

/** Markdown → 安全 HTML。异常时返回转义后的原文，避免 v-html 出现裸内容。 */
export function renderMarkdown(text: string | undefined | null): string {
  const src = (text || '').trim()
  if (!src) return ''
  try {
    // 流式时内容可能不完整，先做补丁修复
    const patched = patchStreamingMarkdown(src)
    const html = marked.parse(patched, { async: false }) as string
    // marked 配置：autolink 自动把裸 URL 转成 <a>，breaks 已在 setOptions 中开启
    return _autoOpenExternalInNewTab(sanitizeHtml(html))
  } catch (e) {
    // 备用：尝试不带补丁直接解析
    try {
      const html = marked.parse(src, { async: false }) as string
      return _autoOpenExternalInNewTab(sanitizeHtml(html))
    } catch {
      return escHtml(src).replace(/\n/g, '<br>')
    }
  }
}

/** 给所有 http(s) 外链自动补 target="_blank"，避免把智能体集群 SPA 覆盖掉。
 *  - 已有 target 属性的（用户显式写了 _self/_top/...）保持不动
 *  - 站内锚点 / 相对路径 / mailto / javascript / data 协议不动
 */
function _autoOpenExternalInNewTab(html: string): string {
  // 匹配整个 <a ...> 开始标签（贪婪但正确：从 <a 到第一个 >）
  return html.replace(/<a\b([^>]*)>/gi, (match, attrs) => {
    // 已有 target → 保持不动
    if (/\starget\s*=/i.test(attrs)) return match
    // 找 href="..." 或 href='...'
    const hrefMatch = attrs.match(/\shref\s*=\s*(["'])([^"'<>]*?)\1/i)
    if (!hrefMatch) return match
    const url = hrefMatch[2]
    // 只处理 http(s) 外链
    if (!/^https?:/i.test(url)) return match
    // 在 href 后面插入 target + rel
    const insert = ' target="_blank" rel="noopener noreferrer"'
    const quoteChar = hrefMatch[1]
    const insertPos = hrefMatch.index! + hrefMatch[0].length
    const before = attrs.slice(0, insertPos)
    const after = attrs.slice(insertPos)
    return `<a${before}${insert}${after}>`
  })
}
