import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { isDesktop } from './utils/env'
import { loadExtensions } from './extensions/loader'
import AppIcon from './components/AppIcon.vue'
import './styles/liquid.scss'
import 'katex/dist/katex.min.css'

// 桌面无边框窗口标记：在应用入口添加，保证任意路由（含刷新直接进入设置/能力管理页）都生效
if (isDesktop) document.documentElement.classList.add('desktop-frameless')

// —— 双击误选保护 ——
// Electron/WebView2 无边框窗口里，双击窗口拖动区或空白处，偶尔会把整页文字一起选中（整窗变蓝）。
// 这里做两层兜底：双击落在「非可复制」区域时不触发选词，并在双击后清掉异常大的选区。
const SELECTABLE_SEL =
  'input, textarea, select, [contenteditable="true"], pre, code, .cb-md, .markdown, .b-text, .md-body, .selectable, .copyable'

document.addEventListener(
  'mousedown',
  (e) => {
    if (e.detail !== 2) return // 只处理双击的第二下
    const t = e.target as HTMLElement | null
    if (t && t.closest(SELECTABLE_SEL)) return
    e.preventDefault()
    window.getSelection()?.removeAllRanges()
  },
  true
)

document.addEventListener(
  'dblclick',
  () => {
    const sel = window.getSelection()
    // 正常双击只会选中一个词，超过 60 字的选区一定是误选（整页选中）
    if (sel && sel.rangeCount > 0 && sel.toString().trim().length > 60) sel.removeAllRanges()
  },
  true
)

const app = createApp(App)
app.component('AppIcon', AppIcon)
app.use(createPinia()).use(router).mount('#app')

// 挂载后异步加载已启用的功能拓展（可视化渲染器等）
loadExtensions().catch((e) => console.error('[main] 加载拓展失败：', e))
