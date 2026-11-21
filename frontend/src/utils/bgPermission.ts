/**
 * 背景「本地存储（同源）」权限 —— 默认开启，可按背景单独关闭
 *
 * 为什么默认开：animation 背景以 sandbox="allow-scripts" 缺省运行时源是不透明的，
 * 访问 localStorage / IndexedDB 会抛 SecurityError。背景脚本通常在启动阶段读存档，
 * 一旦抛错就中断后续初始化（典型症状：resizeCanvas 没执行，canvas 停在默认 300×150，
 * 整个游戏布局被压扁，与浏览器打开完全不一致）。snake.html 就是这种情况。
 *
 * 安全边界：背景帧由后端 127.0.0.1:8870 提供，应用本体在 localhost:5173，
 * 两者不同源 —— 开 allow-same-origin 只让背景拥有自己稳定的源（能用自己的 localStorage），
 * 拿不到应用自身的 localStorage / DOM。市场下载的不可信背景仍可在设置里逐个关掉。
 * 状态存在 localStorage（按背景 id 存 false 表示关闭）。
 */
import { ref } from 'vue'

const KEY = 'agent-cluster.bg.same-origin'

/** 变更计数：BackgroundLayer 依赖它重算 iframe 的 sandbox */
export const bgPermissionVersion = ref(0)

function read(): Record<string, boolean> {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '{}')
    return v && typeof v === 'object' ? v : {}
  } catch {
    return {}
  }
}

export function isBgSameOriginAllowed(id?: string | null): boolean {
  if (!id) return false
  // 未记录 = 默认允许；只有显式存了 false 才是关闭
  return read()[String(id)] !== false
}

export function setBgSameOriginAllowed(id: string, allowed: boolean) {
  if (!id) return
  try {
    const m = read()
    // 必须显式写入 true/false：删除键会退回「默认允许」
    m[String(id)] = !!allowed
    localStorage.setItem(KEY, JSON.stringify(m))
  } catch {
    /* localStorage 不可用时静默 */
  }
  bgPermissionVersion.value++
}
