// 更精准的 Vue template emoji → AppIcon 替换
const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')

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
  '✓': 'check-check', '✅': 'circle-check-big', '📧': 'mail', '📨': 'mail',
  '📖': 'book-open', '⚙️': 'settings', '🖥️': 'monitor',
  '🖱️': 'mouse', '🧪': 'flask-conical', '🔐': 'lock', '🧮': 'calculator',
  '👥': 'users', '🤝': 'handshake', '🐣': 'egg', '👤': 'paw', '👈': 'arrow-left',
  '📥': 'clipboard-paste', '🔔': 'bell', '🛡': 'shield-check',
  '🗜': 'settings-2', '🎵': 'music', '🧭': 'compass', '📍': 'pin',
  '📅': 'calendar-days', '🛠': 'wrench', '⌨️': 'keyboard', '⌐': 'corner-down-left',
  '➕': 'plus', '➖': 'minus', '⏸': 'pause', '▶': 'play', '⏱': 'timer',
  '⏲': 'timer', '⟳': 'refresh-cw', '⚙': 'settings', '🎛': 'settings',
  '📤': 'upload', '🎞': 'film', '🧹': 'sparkles',
}

const EMOJI_CHARS = Object.keys(EMOJI_MAP).join('|')

function getTemplate(src) {
  const s = src.indexOf('<template')
  const e = src.lastIndexOf('</template>')
  if (s < 0 || e < 0) return null
  return [s, e + '</template>'.length]
}

function processTemplate(tmpl) {
  let out = tmpl
  let hit = 0

  // 只替换 template 里 tag 边界的独立 emoji
  // 模式1: >emoji< （独立 emoji）
  const r1 = new RegExp(`>(${EMOJI_CHARS.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})<`, 'g')
  out = out.replace(r1, (m, em) => { hit++; return `><AppIcon name="${EMOJI_MAP[em]||em}" /><` })

  // 模式2: >emoji 文字 （emoji+空格+标题）
  const r2 = new RegExp(`>(${EMOJI_CHARS.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})(\\s+)(?=[^<{]|<br)`, 'g')
  out = out.replace(r2, (m, em, sp) => { hit++; return `><AppIcon name="${EMOJI_MAP[em]||em}" />${sp}` })

  // 模式3: 文字 空格 emoji< （标题结尾 emoji）
  const r3 = new RegExp(`(?:^|[^<{\\u{1F000}-\\u{1FAFF}\\u{2600}-\\u{27BF}])(\\s+)(${EMOJI_CHARS.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})<`, 'gu')
  out = out.replace(r3, (m, sp, em) => { hit++; return `${sp}<AppIcon name="${EMOJI_MAP[em]||em}" /><` })

  // 模式4: title="emoji xxx" 属性值里的 emoji
  const r4 = new RegExp(`(title="[${EMOJI_CHARS.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}])`, 'g')
  // 不处理 title 属性，用户鼠标 hover 才看到，且 title 不能放 HTML

  return { out, hit }
}

function processFile(fp) {
  const src = fs.readFileSync(fp, 'utf8')
  const r = getTemplate(src)
  if (!r) return 0
  const [s, e] = r
  const tmpl = src.slice(s, e)
  const { out, hit } = processTemplate(tmpl)
  if (hit > 0 && out !== tmpl) {
    fs.writeFileSync(fp, src.slice(0, s) + out + src.slice(e), 'utf8')
    return hit
  }
  return 0
}

function walk(dir, out = []) {
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f)
    const st = fs.statSync(p)
    if (st.isDirectory()) walk(p, out)
    else if (f.endsWith('.vue') && f !== 'AppIcon.vue') out.push(p)
  }
  return out
}

let total = 0
for (const f of walk(SRC)) {
  const hit = processFile(f)
  if (hit) {
    console.log(`✓ ${path.relative(ROOT, f)} +${hit}`)
    total += hit
  }
}
console.log(`\nTotal: ${total} replacements`)
