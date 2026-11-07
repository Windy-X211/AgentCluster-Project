import { createServer } from 'vite'
import { readFileSync } from 'node:fs'

globalThis.location = new URL('http://localhost:5173/')
globalThis.navigator ||= { userAgent: 'node' }

const server = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'silent',

})
const { renderMarkdown } = await server.ssrLoadModule('/src/utils/markdown.ts')

const src = readFileSync('C:/Users/qiqi/AppData/Local/AgentCluster/conversations/_math_msg.txt', 'utf-8')
const html = renderMarkdown(src)

// 找出所有 katex-error 与其 title（ParseError 详情）
const errs = html.match(/<span class="katex-error"[^>]*>[^<]*<\/span>/g) || []
console.log('katex-error 数量:', errs.length)
for (const e of errs) console.log('  ERROR:', e.slice(0, 300))

// 检查反例警示段
const idx = html.indexOf('反例警示')
console.log('\n反例警示段渲染片段:')
console.log(html.slice(idx - 50, idx + 500).replace(/></g, '>\n<').split('\n').slice(0, 12).join('\n'))

await server.close()