// 批量把 .vue template 里的 emoji 换成 <AppIcon>
// 用法：node scripts/replace-emojis.js

const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')

// emoji → lucide 图标名
const EMOJI_MAP = {
  '🤖': 'bot', '🧠': 'brain', '🎨': 'palette', '🗺': 'map', '🗺️': 'map',
  '🔧': 'wrench', '🧩': 'puzzle', '🔌': 'plug', '⚡': 'zap', '✨': 'sparkles',
  '🎯': 'target', '📦': 'package', '💬': 'message-square', '📋': 'clipboard-list',
  '📁': 'folder', '📂': 'folder-open', '🗂': 'folder', '🗂️': 'folder',
  '🔍': 'search', '🔄': 'refresh-cw', '🚀': 'rocket', '📤': 'upload',
  '👁': 'eye', '👁️': 'eye', '🙈': 'eye-off', '🗑': 'trash-2', '🗑️': 'trash-2',
  '✂️': 'scissors', '✏️': 'pencil', '📄': 'file-text', '📝': 'file-text',
  '📊': 'bar-chart-3', '🖼': 'image', '🖼️': 'image', '🎬': 'film', '🎞': 'film',
  '🎙': 'mic', '🎙️': 'mic', '🎤': 'mic', '🔇': 'mic-off', '🌐': 'globe',
  '⚠️': 'alert-triangle', '⚠': 'alert-triangle', '❌': 'circle-x', '✕': 'x',
  '✓': 'circle-check', '✅': 'circle-check-big', '📧': 'mail', '📨': 'mail',
  '📖': 'book-open', '⚙️': 'settings', '⚙': 'settings', '🖥️': 'monitor',
  '🖱️': 'mouse', '🖱': 'mouse', '🧪': 'flask-conical', '🔐': 'lock', '🧮': 'calculator',
  '👥': 'users', '🤝': 'handshake', '🐣': 'egg', '👤': 'paw', '👈': 'arrow-left',
  '📥': 'clipboard-paste', '🔔': 'bell', '🛡': 'shield-check',
  '🗜': 'settings-2', '🎵': 'music', '🧭': 'compass', '📍': 'pin',
  '🗓': 'calendar-days', '🛠': 'wrench', '🛠️': 'wrench',
  '💻': 'terminal', '🕑': 'loader', '⭐': 'star', '🌟': 'star',
  '▶️': 'play', '✔️': 'check-check', '🔗': 'link', '🔽': 'chevron-down',
  '▶': 'chevron-right', '⏹': 'square', '🎛': 'settings', '🖨': 'printer',
  '📌': 'pin', '🔑': 'lock', '🔓': 'lock', '🛑': 'octagon-stop',
  '📤': 'upload', '⬆': 'arrow-up', '⬇': 'arrow-down',
}

// 找到 Vue template 范围
function getTemplateRange(src) {
  const tmplStart = src.indexOf('<template')
  const tmplEnd = src.lastIndexOf('</template>')
  if (tmplStart < 0 || tmplEnd < 0) return null
  return [tmplStart, tmplEnd + '</template>'.length]
}

// 只替换 template 里的内容
function processFile(filePath) {
  const src = fs.readFileSync(filePath, 'utf8')
  const range = getTemplateRange(src)
  if (!range) return { hit: 0, skip: 1 }

  const [start, end] = range
  const tmpl = src.slice(start, end)
  let out = tmpl
  let hit = 0

  // 策略 1: {{ m.icon || '📦' }} / {{ xxx.icon }} → <AppIcon :name="..." />
  // 替换模板里渲染 icon 值的 mustache 表达式（icon 后缀的变量）
  const mustacheRe = /\{\{\s*([^}]*?)\bicon\b([^}]*?)\s*\}\}/g
  out = out.replace(mustacheRe, (match, before, after) => {
    hit++
    // before 含变量名，after 可能含 fallback（如 || '📦'）
    // 把 emoji fallback 也转掉
    const afterClean = after.replace(/'([^']*)'|"?([^"'\s]+)"?/g, (m2, p1, p2) => {
      const raw = p1 ?? p2
      return `'${EMOJI_MAP[raw] || raw}'`
    })
    return `<AppIcon :name="${before.trim() || '_' }icon${afterClean}" />`
  })

  // 策略 2: 硬写 emoji 标签 <span>🧠</span> / >🧠<
  // 找所有独立 emoji 字符（前后都是非 emoji）
  const emojiStr = Object.keys(EMOJI_MAP).join('|')
  // 只替换紧贴标签边界的 emoji（真正的图标用法），不动文案里的
  const boundaryRe = new RegExp(`(>)([${emojiStr.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}]+)(<)`, 'g')
  out = out.replace(boundaryRe, (m, l, emojis, r) => {
    // 处理多个连续 emoji
    const names = [...emojis].map(e => EMOJI_MAP[e] || e).filter(Boolean)
    if (!names.length) return m
    hit++
    return `${l}<AppIcon name="${names[0]}" />${r}`
  })

  // 策略 3: emoji + 文字 / 文字 + emoji 的按钮标题，如 "🖼️ 图片"
  // 只匹配常见按钮/面板标题模式：emoji + 空格 + 文字 或 文字 + 空格 + emoji
  const labeledRe = new RegExp(`([${emojiStr.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}]+)(\\s+[\\u4e00-\\u9fa5A-Za-z0-9]+|[\\u4e00-\\u9fa5A-Za-z0-9]+\\s+)([${emojiStr.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}]+)?`, 'g')
  // 太贪心——改用更窄的匹配：标签内容里 emoji 开头 + 空格 + 文字
  // 直接遍历 emoji 字符串做简单替换：>🧠 默认智能体< → >...<
  for (const [emoji, lucideName] of Object.entries(EMOJI_MAP)) {
    // 在标签的闭合尖括号之后、文本之前
    const re = new RegExp(`>${emoji.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}(\\s+)`, 'g')
    out = out.replace(re, (m, sp) => `><AppIcon name="${lucideName}" />${sp}`)
  }
  for (const [emoji, lucideName] of Object.entries(EMOJI_MAP)) {
    // 文字 + emoji + 闭合尖括号（如"📤"在 fallback 里已处理）
    const re = new RegExp(`(\\s+)${emoji.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}<`, 'g')
    out = out.replace(re, (m, sp) => `${sp}<AppIcon name="${lucideName}" /><`)
  }

  if (out !== tmpl) {
    const newSrc = src.slice(0, start) + out + src.slice(end)
    fs.writeFileSync(filePath, newSrc, 'utf8')
    return { hit, skip: 0, changed: true }
  }
  return { hit: 0, skip: 0, changed: false }
}

function walk(dir) {
  let out = []
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f)
    const stat = fs.statSync(p)
    if (stat.isDirectory()) out = out.concat(walk(p))
    else if (f.endsWith('.vue')) out.push(p)
  }
  return out
}

const files = walk(SRC)
let total = 0, changed = 0
for (const f of files) {
  const r = processFile(f)
  if (r.changed) {
    console.log(`✓ ${path.relative(ROOT, f)}  hit=${r.hit}`)
    changed++
    total += r.hit
  }
}
console.log(`\nDone: ${changed} files changed, ${total} replacements`)
