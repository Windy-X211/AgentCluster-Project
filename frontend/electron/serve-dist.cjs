/**
 * 极简 Node.js 静态文件服务器 —— 用于 prod 模式下服务 dist/
 * 替代 Python http.server，内置 SPA fallback 和正确的 MIME type
 *
 * 用法：node serve-dist.cjs <dist目录> <端口>
 *   例：node serve-dist.cjs ./dist 5173
 */
const http = require('http')
const fs = require('fs')
const path = require('path')

const root = path.resolve(process.argv[2] || './dist')
const port = parseInt(process.argv[3] || '5173', 10)

if (!fs.existsSync(root)) {
  console.error(`[serve-dist] root dir not found: ${root}`)
  process.exit(1)
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'application/javascript; charset=utf-8',
  '.mjs':  'application/javascript; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg':  'image/svg+xml',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif':  'image/gif',
  '.ico':  'image/x-icon',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2':'font/woff2',
  '.ttf':  'font/ttf',
  '.eot':  'application/vnd.ms-fontobject',
  '.txt':  'text/plain',
  '.map':  'application/json',
}

// 这些扩展名直接回 404（不做 SPA fallback）
// 其余找不到的路径（如 /settings、/agents）→ SPA fallback 回 index.html
const ASSET_EXTS = new Set(Object.keys(MIME))
const SPA_FALLBACK_EXTS = new Set(['.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.gif', '.woff', '.woff2', '.ttf', '.eot', '.webp', '.map'])

const server = http.createServer((req, res) => {
  // URL decode + strip query/hash
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0].split('#')[0])
  let filePath = path.join(root, urlPath)

  // 防目录穿越：确保解析后的路径在 root 内
  const resolved = path.resolve(filePath)
  if (!resolved.startsWith(path.resolve(root))) {
    res.writeHead(400); res.end('Bad Request'); return
  }

  // 目录 → index.html
  try {
    const stat = fs.statSync(resolved)
    if (stat.isDirectory()) {
      filePath = path.join(resolved, 'index.html')
      if (fs.existsSync(filePath)) {
        sendFile(res, filePath); return
      }
    } else {
      sendFile(res, resolved); return
    }
  } catch {}

  // ---- 文件不存在 ----
  const ext = path.extname(resolved).toLowerCase()

  // 资源文件（有扩展名如 .js/.css/.png）→ 真正 404
  if (SPA_FALLBACK_EXTS.has(ext)) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end(`404 Not Found: ${urlPath}`)
    return
  }

  // SPA fallback：无扩展名的路径（/settings、/agents、/chat/123）→ 回 index.html
  const indexPath = path.join(root, 'index.html')
  if (fs.existsSync(indexPath)) {
    sendFile(res, indexPath)
  } else {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('index.html not found')
  }
})

function sendFile(res, filePath) {
  const ext = path.extname(filePath).toLowerCase()
  const mime = MIME[ext] || 'application/octet-stream'
  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(500); res.end('Read error'); return }
    res.writeHead(200, { 'Content-Type': mime })
    res.end(data)
  })
}

server.listen(port, '127.0.0.1', () => {
  console.log(`[serve-dist] http://127.0.0.1:${port} serving ${root}`)
})
