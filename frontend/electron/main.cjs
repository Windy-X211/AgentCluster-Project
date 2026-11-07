/**
 * Electron 主进程 —— 替代 start_tray.py + app_window.py
 *
 * 职责：
 *  1. 创建 frameless BrowserWindow，内嵌前端 dist（生产）或开发服务器 URL（dev）
 *  2. 通过 Tray 提供系统托盘（替代 pystray）
 *  3. 通过 child_process 拉起 Python FastAPI 后端（替代 start_tray.py 的 start_backend）
 *  4. 单例互斥量（只允一个实例跑）
 *  5. IPC 注册：窗口控制 / 后端健康检查
 *
 * 与 pywebview 方案的关键差异：
 *  - 窗口控制 API 从 pywebview.api → Electron BrowserWindow API
 *  - 拖动区从 .pywebview-drag-region → -webkit-app-region: drag
 *  - 边缘缩放不再需要（Electron frameless 在 Win11 上自带 native resize）
 */

// === 调试日志（写到项目根目录） ===
const _fs = require('fs')
const _path = require('path')
const _LOG = _path.join(__dirname, '..', '..', 'electron-main.log')

// ---- 日志轮转：单文件超 10MB 滚动为 .old（覆盖上一份），防止 appendFileSync 无限膨胀
// （electron-main.log 曾涨到 128MB，同步追加写还会拖慢 Electron 主进程自身）
const _LOG_ROTATE_MAX = 10 * 1024 * 1024
let _logRotateLastCheck = 0
function _appendLog(file, buf) {
  try {
    const now = Date.now()
    if (now - _logRotateLastCheck > 60000) {
      _logRotateLastCheck = now
      try {
        const st = _fs.statSync(file)
        if (st.size > _LOG_ROTATE_MAX) {
          const old = file + '.old'
          try { _fs.unlinkSync(old) } catch { }
          try { _fs.renameSync(file, old) } catch { }
        }
      } catch { /* 文件不存在等，忽略 */ }
    }
    _fs.appendFileSync(file, buf)
  } catch { }
}

function _log(m) { try { _appendLog(_LOG, '[' + new Date().toISOString() + '] ' + m + '\n') } catch { } }
_log('=== Electron main.cjs starting ===')
process.on('uncaughtException', (e) => { _log('UNCAUGHT: ' + e.message); _log(e.stack || '') })
process.on('unhandledRejection', (e) => { _log('REJECTION: ' + (e?.message || e)) })

// ---- ELECTRON_RUN_AS_NODE 自恢复 ----
// IDE / CI / 某些启动器会注入 ELECTRON_RUN_AS_NODE，让 Electron 以纯 Node 模式运行，
// require('electron') 拿到的只是 Node 模块、app 为 undefined，结果就是"双击没反应、无窗口"。
// 修复策略：如果此变量已被设置，清除它并用干净的环境重新 spawn 自身，然后当前进程退出。
// 这样无论用户怎么启动（IDE、快捷方式、直接双击）都能自恢复，不需要依赖 start.bat。
if (process.env.ELECTRON_RUN_AS_NODE) {
  _log('检测到 ELECTRON_RUN_AS_NODE=' + process.env.ELECTRON_RUN_AS_NODE + '，清除并重启自身...')
  const { spawn } = require('child_process')
  const env = { ...process.env }
  delete env.ELECTRON_RUN_AS_NODE
  try {
    const child = spawn(process.execPath, process.argv.slice(1), {
      env,
      stdio: 'inherit',
      detached: false,
      windowsHide: false,
    })
    child.on('spawn', () => {
      _log('✅ 已用干净环境 spawn 新实例 PID=' + child.pid + '，当前进程退出')
      process.exit(0)
    })
    child.on('error', (err) => {
      _log('❌ spawn 失败: ' + err.message)
      process.exit(1)
    })
    // 兜底：如果 spawn 同步失败（不太可能），直接在下一 tick 退出
    setTimeout(() => process.exit(0), 300)
    return   // ← 关键：不让代码继续往下 require('electron')，避免拿不到 app 崩溃
  } catch (err) {
    _log('❌ 重启异常: ' + err.message + '\n' + err.stack)
    process.exit(1)
  }
}

const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell, screen, dialog, desktopCapturer } = require('electron')

// 双保险：即使上面的自恢复没触发（极端情况），这里也防止 app 为 undefined 时崩溃
if (!app || typeof app.commandLine?.appendSwitch !== 'function') {
  const msg = 'Electron 以 Node 模式运行（ELECTRON_RUN_AS_NODE 被设置），无法创建窗口。'
  _log('FATAL: ' + msg)
  try { console.error(msg) } catch { }
  process.exit(1)
}
const path = require('path')
const fs = require('fs')
const os = require('os')
const { spawn } = require('child_process')

// 精灵球窗动画冻结修复：右键弹独立菜单时全屏透明捕获层会盖住球窗，
// Windows 的 Chromium native occlusion 计算按"窗口几何覆盖"判定（不识别透明像素），
// 球窗被判完全遮挡 → 停止产出渲染帧 → CSS 动画冻结。全局关闭该计算
// （本应用窗口数量少，放弃遮挡省电优化的代价可忽略）。必须在 app ready 前执行。
app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion')

_log('app.isPackaged=' + app.isPackaged + ' argv=' + JSON.stringify(process.argv.slice(2)))

// ---------- 配置 ----------
// app.isPackaged: electron-builder 打包后自动为 true
// --prod 命令行参数: 直接跑 electron . 时强制用 dist/（开发机调试用）
const FORCE_PROD = process.argv.includes('--prod')
const TRAY_ONLY = process.argv.includes('--tray-only')
const NO_SINGLE_INSTANCE = process.argv.includes('--no-single-instance')
const IS_DEV = !app.isPackaged && !FORCE_PROD
const ROOT = path.resolve(__dirname, '..')               // frontend/ (开发) 或 app.asar/ (打包)
const PROJECT_ROOT = path.resolve(ROOT, '..')           // 项目根（backend/ 在这里）
// 后端端口：
//   开发/--prod 调试：直接用项目根/backend
//   真正打包后：resourcesPath/backend（通过 electron-builder extraResources 拷过去）
const BACKEND_DIR = path.join(PROJECT_ROOT, 'backend')
const BACKEND_PORT = 8870
const DIST_PORT = 5173        // prod 模式也用 5173 服务 dist → 浏览器也能访问
const DEV_URL = 'http://127.0.0.1:5173'
const PROD_URL = `http://127.0.0.1:${DIST_PORT}`
const PRELOAD = path.join(__dirname, 'preload.cjs')

// 进程句柄
let backendProc = null
let distServerProc = null
let mainWindow = null

// ---- 后端 watchdog：异常退出自动重启（10 分钟窗口内最多 5 次，防死循环） ----
// backendStopping=true 表示用户主动停止（托盘「停止全部服务」/退出程序），不重启
let backendStopping = false
let backendRestartCount = 0
let backendRestartWindowStart = 0
const BACKEND_RESTART_MAX = 5
const BACKEND_RESTART_WINDOW_MS = 10 * 60 * 1000

// ---------- 后端进程管理 ----------

function _killPortIfBusy(port) {
  // 简化版：用 netstat 找到监听该端口的 PID，taskkill 掉
  try {
    const out = require('child_process').execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8', timeout: 3000 })
    const lines = out.trim().split(/\r?\n/)
    for (const line of lines) {
      const parts = line.trim().split(/\s+/)
      const pid = parseInt(parts[parts.length - 1], 10)
      if (!pid || pid === process.pid) continue
      _log(`端口 ${port} 被 PID=${pid} 占用，执行 taskkill`)
      try {
        require('child_process').execSync(`taskkill /F /PID ${pid}`, { encoding: 'utf8', timeout: 3000 })
      } catch { }
    }
    // 等端口释放
    for (let i = 0; i < 10; i++) {
      try {
        require('child_process').execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8', timeout: 1000 })
        require('child_process').execSync('timeout /t 0.3', { encoding: 'utf8', timeout: 1000 })
      } catch { break }
    }
  } catch {
    // netstat 找不到 LISTENING 行 → 端口空闲，正常
  }
}

// 检测系统 Node.js（Electron 内置 Node 不能 spawn 外部 http server，会 ACCESS_VIOLATION）
function _detectSystemNode() {
  // Electron 内置 Node 不能 spawn 外部 http server（会 ACCESS_VIOLATION），
  // 必须用系统 Node.js；优先硬编码候选目录（跨设备、自启 PATH 被裁剪时也能命中），
  // 最后才 fallback 到 where node（依赖 PATH 完整）。
  const home = os.homedir()
  const appData = process.env.APPDATA || ''
  const localAppData = process.env.LOCALAPPDATA || ''

  // 候选目录按优先级排序：项目内置 > 开发机固定位置 > 系统常见安装 > nvm > scoop > PATH
  const candidates = [
    path.join(PROJECT_ROOT, 'node-v20.18.0-win-x64', 'node.exe'),
    path.join(PROJECT_ROOT, 'node', 'node.exe'),
    path.join('E:', 'app', 'node-v20.18.0-win-x64', 'node.exe'),
    path.join('C:', 'Program Files', 'nodejs', 'node.exe'),
    path.join('C:', 'Program Files (x86)', 'nodejs', 'node.exe'),
    path.join(localAppData, 'Programs', 'nodejs', 'node.exe'),
    path.join(home, 'scoop', 'apps', 'nodejs', 'current', 'node.exe'),
  ]

  // nvm-windows：遍历 APPDATA\nvm 下所有已安装版本
  const nvmRoot = path.join(appData, 'nvm')
  try {
    if (fs.existsSync(nvmRoot)) {
      const entries = fs.readdirSync(nvmRoot)
      for (const name of entries) {
        const p = path.join(nvmRoot, name, 'node.exe')
        if (fs.existsSync(p)) candidates.push(p)
      }
    }
  } catch { }

  // 项目里 __dirname 是 frontend/electron，PROJECT_ROOT 是项目根
  for (const exePath of candidates) {
    if (fs.existsSync(exePath)) {
      _log('Found system node via hardcoded path: ' + exePath)
      return exePath
    }
  }

  // 最后 fallback：where node（依赖 PATH 完整）
  try {
    const where = require('child_process').execSync('where node', { encoding: 'utf8', timeout: 2000 }).trim()
    const first = where.split('\n')[0].trim()
    if (first && fs.existsSync(first)) {
      _log('Found system node via PATH: ' + first)
      return first
    }
  } catch {
    _log('where node failed, PATH 中找不到 Node.js')
  }

  _log('⚠️ 所有候选路径均未找到 node.exe')
  return null
}

function _detectPython() {
  // 优先项目内 venv（相对路径，跨设备可用），否则走 PATH
  const venvCandidates = [
    path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(PROJECT_ROOT, 'venv', 'Scripts', 'python.exe'),
  ]
  for (const p of venvCandidates) {
    if (fs.existsSync(p)) {
      _log('Found venv python: ' + p)
      return p
    }
  }
  // 走 PATH（会用用户系统配置的 python）
  _log('Falling back to PATH "python"')
  return 'python'
}

function startBackend() {
  return new Promise((resolve, reject) => {
    // ---- 端口冲突防御 ----
    // 如果 8870 已在监听，先查占用者 PID：若是我们自己上次残留的 backend，先 kill 再重启
    _killPortIfBusy(BACKEND_PORT)
    backendStopping = false  // 主动启动（含 watchdog 重启/托盘「重启全部」）→ 清除停止标志

    const python = _detectPython()
    _log('startBackend: python=' + python + ' cwd=' + BACKEND_DIR)

    backendProc = spawn(python, ['-u', 'main.py'], {
      cwd: BACKEND_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: false,
      env: process.env,
    })

    _log('Backend spawned PID=' + backendProc.pid)

    // 简单日志：把 stdout/stderr 追加写文件（带 10MB 轮转，防无限膨胀拖慢主进程）
    const logPath = path.join(BACKEND_DIR, 'electron_backend.log')
    backendProc.stdout?.on('data', (buf) => _appendLog(logPath, buf))
    backendProc.stderr?.on('data', (buf) => _appendLog(logPath, buf))

    backendProc.on('error', (err) => {
      _log('Backend spawn ERROR: ' + err.message)
      reject(err)
    })

    backendProc.on('exit', (code) => {
      _log('Backend exit code=' + code)
      backendProc = null
      // ---- watchdog：非用户主动停止的退出（崩溃/被误杀）→ 自动重启 ----
      if (backendStopping) return
      const now = Date.now()
      if (now - backendRestartWindowStart > BACKEND_RESTART_WINDOW_MS) {
        backendRestartWindowStart = now
        backendRestartCount = 0
      }
      if (backendRestartCount >= BACKEND_RESTART_MAX) {
        _log('watchdog: 10 分钟内重启超过 ' + BACKEND_RESTART_MAX + ' 次，放弃自动重启（疑似配置/环境问题）')
        return
      }
      backendRestartCount++
      _log(`watchdog: 后端异常退出，2s 后自动重启（窗口内第 ${backendRestartCount}/${BACKEND_RESTART_MAX} 次）`)
      setTimeout(() => {
        if (backendStopping) return
        startBackend().catch((e) => _log('watchdog 重启失败: ' + (e?.message || e)))
      }, 2000)
    })

    // 等待端口就绪（最多 25s）
    let waited = 0
    const interval = setInterval(async () => {
      waited += 0.5
      try {
        const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1000) })
        if (res.ok) {
          clearInterval(interval)
          console.log(`[Electron] 后端就绪 (PID=${backendProc?.pid})`)
          resolve()
        }
      } catch { }
      if (waited > 25) {
        clearInterval(interval)
        console.warn('[Electron] 后端启动超时')
        reject(new Error('backend timeout'))
      }
    }, 500)
  })
}

function stopBackend() {
  // 主动停止标志：exit 事件据此跳过 watchdog 自动重启
  // （托盘「停止全部服务」/「退出程序」/before-quit 都走这里）
  backendStopping = true
  if (!backendProc) return
  try {
    backendProc.kill('SIGTERM')
    setTimeout(() => {
      if (backendProc && !backendProc.killed) {
        try { backendProc.kill('SIGKILL') } catch { }
      }
    }, 3000)
  } catch { }
  backendProc = null
}

// ---------- Prod 模式静态文件服务器 ----------
// 用 Node.js 脚本服务 dist/ 到 localhost:5173（内置 SPA fallback）
// 好处：Electron 和浏览器都能通过 HTTP 访问，避开 file:// 的坑
//       子路径（/settings、/agents）→ 自动回 index.html，Vue Router 接管
function startDistServer() {
  return new Promise((resolve, reject) => {
    const distDir = path.join(ROOT, 'dist')
    const distIndex = path.join(distDir, 'index.html')
    if (!fs.existsSync(distIndex) || fs.statSync(distIndex).size < 100) {
      reject(new Error('dist/index.html 无效（请先 npm run build）'))
      return
    }

    // ---- 端口冲突防御 ----
    // 如果 5173 已在监听（比如用户已经开了 dev 模式 Vite），直接用现成的就行
    try {
      const existing = require('child_process').execSync(
        `netstat -ano | findstr :${DIST_PORT} | findstr LISTENING`,
        { encoding: 'utf8', timeout: 2000 }
      )
      if (existing && existing.trim()) {
        _log(`端口 ${DIST_PORT} 已被占用（可能是 Vite dev server），复用`)
        resolve()
        return
      }
    } catch {
      // 端口空闲，正常启动
    }

    // 用系统 Node.js（Electron 内置 Node spawn 外部脚本会 ACCESS_VIOLATION）
    // 按优先级找：项目本地 node > 用户目录 node > 环境变量 node
    let nodeBin = _detectSystemNode()
    if (!nodeBin) {
      _log('⚠️ 找不到系统 Node.js，dist server 启动失败')
      reject(new Error('找不到 Node.js（请安装 Node.js 或检查 PATH）'))
      return
    }
    const serveScript = path.join(__dirname, 'serve-dist.cjs')
    _log(`startDistServer: node=${nodeBin} script=${serveScript} port=${DIST_PORT}`)

    distServerProc = require('child_process').spawn(
      nodeBin, [serveScript, distDir, String(DIST_PORT)],
      {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,  // ← 隐藏 Node.js 黑窗口
        shell: false,
      }
    )

    distServerProc.stdout.on('data', d => _log('[dist-server] ' + d.toString().trim()))
    distServerProc.stderr.on('data', d => _log('[dist-server-err] ' + d.toString().trim()))
    distServerProc.on('exit', (code) => {
      _log('Dist server exit code=' + code)
      distServerProc = null
    })

    // 等待端口就绪（最多 90s：开机冷启动时杀软扫描 node.exe 可达 1 分钟）
    let waited = 0
    const interval = setInterval(async () => {
      waited += 0.5
      try {
        const res = await fetch(`http://127.0.0.1:${DIST_PORT}/index.html`, { signal: AbortSignal.timeout(1000) })
        if (res.ok) {
          clearInterval(interval)
          _log(`✅ Dist server 就绪 (PID=${distServerProc?.pid}, port=${DIST_PORT})`)
          resolve()
        }
      } catch { }
      if (waited > 90) {
        clearInterval(interval)
        reject(new Error('dist server timeout'))
      }
    }, 500)
  })
}

function stopDistServer() {
  if (!distServerProc) return
  try { distServerProc.kill('SIGTERM') } catch { }
  distServerProc = null
}

// ---------- 启动中窗口（简化版） ----------

// ---------- 启动中特效窗口 ----------

let _splashWindow = null
let _startupAborted = false
let _splashIpcReady = false

// splash 窗口专属 IPC（与 win:* 主窗口 IPC 隔离；幂等注册，createSplash 可多次调用）
function _registerSplashIPC() {
  if (_splashIpcReady) return
  _splashIpcReady = true
  ipcMain.on('splash:minimize', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.minimize()
  })
  ipcMain.on('splash:abort', (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!_splashWindow || win !== _splashWindow) return
    // 主窗口已存在（托盘「重启全部」等场景）：仅关掉启动画面，不打断后台流程
    if (mainWindow && !mainWindow.isDestroyed()) { closeSplash(); return }
    // 初次启动阶段点 ✕：视为放弃启动，中止后续步骤并退出程序
    _startupAborted = true
    _log('用户在启动画面点击关闭，中止启动并退出程序')
    closeSplash()
    stopBackend()
    stopDistServer()
    app.quit()
  })
}

function createSplash() {
  if (_splashWindow) return _splashWindow
  _registerSplashIPC()

  // 图标路径（与原 launch_splash.py 一致）
  const iconPaths = [
    path.join(ROOT, 'public', 'icon.png'),
    path.join(ROOT, 'public', 'icon.jpg'),
    path.join(PROJECT_ROOT, '1791461860623.webp.jpg'),
  ]
  let iconDataUrl = ''
  for (const p of iconPaths) {
    if (fs.existsSync(p)) {
      try {
        const img = nativeImage.createFromPath(p)
        if (!img.isEmpty()) {
          iconDataUrl = img.resize({ width: 144, height: 144 }).toDataURL()
          break
        }
      } catch { }
    }
  }

  _splashWindow = new BrowserWindow({
    width: 520, height: 340,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: false,   // 保留任务栏条目：最小化后可从任务栏点回还原
    resizable: false,
    show: false,
    hasShadow: false,   // 透明窗口禁用系统阴影（DWM 阴影在透明窗上呈灰底）
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  const iconHtml = iconDataUrl
    ? `<img src="${iconDataUrl}" width="72" height="72" style="border-radius:18px;background:rgba(91,140,255,0.12);padding:10px;box-shadow:0 8px 24px rgba(91,140,255,0.25)">`
    : `<div style="width:92px;height:92px;border-radius:22px;background:linear-gradient(135deg,#5b8cff,#a78bfa);display:flex;align-items:center;justify-content:center;font-size:42px;color:#fff;font-weight:700">✦</div>`

  const splashHtml = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;width:100%;height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:#eaf1ff;user-select:none}
 .card{position:absolute;inset:4px;border-radius:26px;background:#0f1626;border:1px solid #243450;overflow:hidden}
.glow-top{position:absolute;left:40px;right:40px;top:34px;height:46px;background:linear-gradient(180deg,rgba(91,140,255,0.22),transparent);border-radius:8px}
.win-btns{position:absolute;right:18px;top:10px;display:flex;gap:2px;z-index:10}
.win-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:16px;color:#94a8cf;cursor:pointer;border-radius:8px;transition:background 0.15s}
.win-btn:hover{background:rgba(148,168,207,0.18);color:#eaf1ff}
.win-btn.close:hover{background:rgba(255,100,100,0.15);color:#ff9a9a}
.content{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:30px}
.icon-wrap{margin-bottom:18px;animation:float 3s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
.title{font-size:16px;font-weight:600;letter-spacing:0.5px;margin-bottom:4px}
.subtitle{font-size:11px;color:#94a8cf;margin-bottom:18px}
.step{font-size:13px;color:#c9d6ef;margin-bottom:10px;min-height:18px;transition:opacity 0.3s}
.progress{width:280px;height:3px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;position:relative}
.progress-bar{height:100%;width:12%;background:linear-gradient(90deg,#5b8cff,#a78bfa);border-radius:2px;transition:width 0.4s ease-out}
.progress-bar.done{background:linear-gradient(90deg,#3ddc97,#5bf0b0)}
.bottom-text{position:absolute;bottom:18px;left:0;right:0;text-align:center;font-size:11px;color:rgba(148,168,207,0.5)}
.spinner{position:absolute;top:-1px;left:0;width:12%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.3),transparent);animation:spin 1.6s ease-in-out infinite}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(1000%)}}
.fade-in{animation:fadeIn 0.32s ease-out}
@keyframes fadeIn{from{opacity:0;transform:scale(0.97)}to{opacity:1;transform:scale(1)}}
</style></head><body>
<div class="card fade-in" id="card">
  <div class="glow-top"></div>
  <div class="win-btns">
    <div class="win-btn" id="minBtn" title="最小化">&#8211;</div>
    <div class="win-btn close" id="closeX" title="取消启动并退出">&#215;</div>
  </div>
  <div class="content">
    <div class="icon-wrap">${iconHtml}</div>
    <div class="title">AgentCluster 启动中</div>
    <div class="subtitle">智能体集群 · 桌面客户端</div>
    <div class="step" id="step">正在初始化启动器...</div>
    <div class="progress"><div class="progress-bar" id="bar"><div class="spinner" id="spinner"></div></div></div>
  </div>
  <div class="bottom-text">启动中可最小化 · ESC 取消启动</div>
</div>
<script>
const stepEl=document.getElementById('step'),barEl=document.getElementById('bar'),spinnerEl=document.getElementById('spinner'),minBtn=document.getElementById('minBtn'),closeX=document.getElementById('closeX');
let _progress=0,_done=false;
// 最小化：收起启动画面（任务栏可还原）；✕/ESC：中止启动退出程序（主窗已存在时仅关画面）
minBtn.addEventListener('click',()=>window.electronAPI?.splashMinimize?.());
closeX.addEventListener('click',()=>window.electronAPI?.splashAbort?.());
document.addEventListener('keydown',e=>{if(e.key==='Escape')window.electronAPI?.splashAbort?.()});
document.addEventListener('contextmenu',e=>e.preventDefault());

window.electronAPI?.onSplashStep?.((text,pct)=>{
  if(!text)return;
  if(text.startsWith('DONE')){
    _done=true;
    stepEl.textContent='✅ 启动完成';
    stepEl.style.color='#3ddc97';
    barEl.classList.add('done');
    spinnerEl.remove();
    barEl.style.width='100%';
    setTimeout(()=>window.close(),650);
    return;
  }
  stepEl.textContent=text;
  if(typeof pct==='number'){
    _progress=Math.min(pct,96);
    barEl.style.width=_progress+'%';
  }
});
// 初始 spinner 占位
setTimeout(()=>barEl.style.width='18%',120);
</script></body></html>`

  _splashWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(splashHtml))
  _splashWindow.on('closed', () => { _splashWindow = null })
  _splashWindow.show()
  return _splashWindow
}

function splashStep(text, pct) {
  if (_splashWindow && !_splashWindow.isDestroyed()) {
    _splashWindow.webContents.send('splash:step', text, pct)
  }
}

function closeSplash() {
  if (_splashWindow && !_splashWindow.isDestroyed()) {
    _splashWindow.close()
    _splashWindow = null
  }
}

// ---------- 工具：等待前端就绪（dev 模式下 Vite 可能还没起来） ----------

async function waitFrontendReady(url, timeoutMs = 60000) {
  if (!url.startsWith('http')) return true  // 生产模式 file:// 直接 OK
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(1500) })
      if (res.ok) return true
    } catch { }
    await new Promise(r => setTimeout(r, 800))
  }
  console.warn(`[Electron] 前端 ${url} 等待超时`)
  return false
}

// ---------- 主窗口 ----------

async function createMainWindow() {
  // dev 模式先等 Vite 起来
  if (IS_DEV) {
    await waitFrontendReady(DEV_URL)
  }
  // 窗口图标：优先项目根的 agentcluster.ico（Windows 多分辨率最佳），再 fallback png
  const _winIconPath = fs.existsSync(path.join(PROJECT_ROOT, 'agentcluster.ico'))
    ? path.join(PROJECT_ROOT, 'agentcluster.ico')
    : (fs.existsSync(path.join(ROOT, 'public', 'icon.png')) ? path.join(ROOT, 'public', 'icon.png') : undefined)

  mainWindow = new BrowserWindow({
    width: 1280, height: 820,
    minWidth: 960, minHeight: 600,
    frame: false,              // frameless，靠前端自绘标题栏
    resizable: true,
    movable: true,
    fullscreenable: true,
    title: '智能体集群 AgentCluster',
    backgroundColor: '#000000', // 与前端 --bg-base 一致，防白闪
    icon: _winIconPath,
    show: false,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      devTools: IS_DEV,
    },
  })

  // 生产模式：加载本地 dist/index.html（先检查 dist 有效性，空文件 = 白屏）
  let startURL
  if (IS_DEV) {
    startURL = DEV_URL
  } else {
    // Prod 模式：统一走 HTTP（Python http.server 服务 dist）
    // 好处：Electron 和浏览器都能通过 localhost 访问，避开 file:// 协议坑
    startURL = PROD_URL + '/?app=electron'
  }
  _log('createMainWindow: startURL=' + startURL)

  mainWindow.loadURL(startURL)

  // frameless 无菜单栏 → 手动接管刷新/DevTools 快捷键（dev 迭代与排障必需）
  mainWindow.webContents.on('before-input-event', (_e, input) => {
    if (input.type !== 'keyDown') return
    const key = (input.key || '').toLowerCase()
    if (key === 'r' && (input.control || input.meta)) {
      mainWindow.webContents.reloadIgnoringCache()
    } else if (key === 'f5') {
      mainWindow.webContents.reloadIgnoringCache()
    } else if (key === 'f12' && IS_DEV) {
      mainWindow.webContents.toggleDevTools()
    }
  })

  // ---- 诊断日志（白屏时救命用）----
  // 连接类失败（dist server 冷启动未就绪）→ 静默延迟重试，最多 30 次（60s）
  // 其余失败 → 开 DevTools 让用户看到错误
  let _loadRetry = 0
  mainWindow.webContents.on('did-fail-load', (_e, errCode, errDesc, url) => {
    _log('❌ 主窗口加载失败: ' + errCode + ' ' + errDesc + ' url=' + url)
    const desc = String(errDesc || '')
    const transient = !IS_DEV && (
      desc.includes('ERR_CONNECTION_REFUSED') ||
      desc.includes('ERR_CONNECTION_TIMED_OUT') ||
      desc.includes('ERR_NAME_NOT_RESOLVED'))
    if (transient && _loadRetry < 30) {
      _loadRetry++
      _log(`⏳ dist server 未就绪，第 ${_loadRetry}/30 次重试（2s 后）...`)
      setTimeout(() => {
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadURL(startURL)
      }, 2000)
      return
    }
    // 自动开 DevTools 让用户看到错误
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  })
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    _log('❌ 渲染进程挂了: reason=' + details.reason + ' exitCode=' + details.exitCode)
  })
  mainWindow.webContents.on('console-message', (_e, level, msg, line, sourceId) => {
    _log('[renderer][' + level + '] ' + msg + ' (' + sourceId + ':' + line + ')')
  })
  mainWindow.webContents.on('did-finish-load', () => {
    _log('✅ 主窗口 did-finish-load, url=' + mainWindow.webContents.getURL())
    const runDiag = (label) => {
      const script = `(function(){
        console.log('[DIAG] ====== ` + label + ` ======');
        // 1. 扫所有 think-stream（含是否有 open 类）
        var tss = document.querySelectorAll('.think-stream');
        console.log('[DIAG] think-stream total=' + tss.length);
        tss.forEach(function(ts, i){
          var cs = getComputedStyle(ts);
          console.log('[DIAG]   ts[' + i + '] classes=[' + String(ts.className) + '] cW=' + ts.clientWidth + ' sW=' + ts.scrollWidth +
            ' textLen=' + (ts.textContent?.length||0) + ' parent=' + (ts.parentElement?.tagName + '.' + String(ts.parentElement?.className||'').split(' ')[0]).substring(0,50));
        });
        // 2. 扫 think-stream.open（已展开的折叠态）
        var topens = document.querySelectorAll('.think-stream.open');
        console.log('[DIAG] think-stream.open total=' + topens.length);
        // 3. 扫 think-full（展开后的全文区）
        var tfulls = document.querySelectorAll('.think-full');
        console.log('[DIAG] think-full total=' + tfulls.length);
        tfulls.forEach(function(tf, i){
          var cs = getComputedStyle(tf);
          console.log('[DIAG]   tf[' + i + '] cW=' + tf.clientWidth + ' sW=' + tf.scrollWidth + ' oX=' + cs.overflowX +
            ' textLen=' + (tf.textContent?.length||0));
        });
        // 4. 扫 think-full-text
        var tft = document.querySelectorAll('.think-full-text');
        console.log('[DIAG] think-full-text total=' + tft.length);
        tft.forEach(function(t, i){
          var cs = getComputedStyle(t);
          console.log('[DIAG]   tft[' + i + '] cW=' + t.clientWidth + ' sW=' + t.scrollWidth + ' oX=' + cs.overflowX +
            ' wb=' + cs.wordBreak + ' ow=' + cs.overflowWrap + ' ws=' + cs.whiteSpace +
            ' disp=' + cs.display + ' textLen=' + (t.textContent?.length||0) +
            ' first80=' + (t.textContent||'').substring(0,80).replace(/\\n/g,'↵'));
        });
        // 5. Vue 内部状态：尝试从 __vue_app__ 找 thinkOpen
        try {
          var app = document.querySelector('#app');
          if (app && app.__vue_app__) {
            console.log('[DIAG] Vue app found');
          } else {
            console.log('[DIAG] no Vue app');
          }
        } catch(e) { console.log('[DIAG] Vue probe error: ' + e.message); }
        return 'done ts=' + tss.length + ' topens=' + topens.length + ' tfulls=' + tfulls.length;
      })();`;
      mainWindow.webContents.executeJavaScript(script).catch(e => _log('[DIAG] exec error: ' + e.message))
    }
    setTimeout(() => runDiag('3s'), 3000)
    setTimeout(() => runDiag('12s'), 12000)
    setTimeout(() => runDiag('25s'), 25000)
  })

  // 显示策略：等 Vue 数据加载完（app:ready IPC）再 show，防白屏闪烁
  // 兜底：8 秒内没收到就强制 show（防止 IPC 丢失导致窗口永远不显示）
  let _showed = false
  const _doShow = (from) => {
    if (_showed) return
    _showed = true
    _log(`✅ 显示主窗口 (${from})`)
    closeSplash()
    mainWindow.show()
  }

  mainWindow.once('ready-to-show', () => {
    _log('✅ ready-to-show (等 Vue app:ready...)')
    // ready-to-show 只标记，不立即 show
    // 如果 8 秒后还没收到 app:ready，兜底显示
    setTimeout(() => _doShow('timeout-fallback'), 8000)
  })

  // Vue 数据加载完后发 app:ready
  ipcMain.on('app:ready', () => _doShow('vue-app-ready'))

  mainWindow.on('closed', () => { mainWindow = null })

  // 新窗口走系统默认浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  // 开发工具快捷键（F12 / Ctrl+Shift+I）
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12' || (input.control && input.shift && input.key.toLowerCase() === 'i')) {
      mainWindow.webContents.toggleDevTools()
    }
  })
}

// ---------- Tray ----------

let tray = null
let _trayRefreshTimer = null
let _trayStatus = '⏹ 已停止'

async function _checkHealth() {
  const result = { backend: false, frontend: false }
  const opener = fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1500) }).then(r => r.ok).catch(() => false)
  const url = IS_DEV ? DEV_URL : DEV_URL
  const opener2 = fetch(url, { signal: AbortSignal.timeout(1500) }).then(r => r.ok).catch(() => false)
  const [b, f] = await Promise.all([opener, opener2])
  result.backend = b; result.frontend = f
  return result
}

function _buildTrayMenuTemplate(statusText) {
  if (TRAY_ONLY) {
    // Web 模式：后端+dist server 由 start.bat 启动
    return [
      { label: `Web 模式 · ${statusText}`, enabled: false },
      { type: 'separator' },
      { label: '🌐 打开浏览器', default: true, click: () => shell.openExternal(`http://localhost:5173`) },
      {
        label: '🔄 重启全部', click: async () => {
          _dialogSync('重启提示', '请直接关闭 start.bat 窗口后重新运行')
        }
      },
      { type: 'separator' },
      { label: '⚙ 打开设置', click: () => shell.openExternal(`http://localhost:5173/#/settings`) },
      { type: 'separator' },
      {
        label: '❌ 退出并停止服务', click: () => {
          _killWebProcesses()
          app.quit()
        }
      },
    ]
  }
  return [
    { label: `状态: ${statusText}`, enabled: false },
    { type: 'separator' },
    {
      label: '🚀 启动服务', click: () => {
        // Electron 下默认就是启动了（app.whenReady 已经自动拉了后端+窗口），
        // 如果用户手动关了窗口，这里重新打开
        if (!mainWindow || !mainWindow.isVisible()) {
          if (!mainWindow) { createMainWindow().catch(() => { }); return }
          mainWindow.show(); mainWindow.focus()
        }
      }
    },
    {
      label: '🔄 重启全部', click: async () => {
        splashStep('正在重启服务...', 10); createSplash()
        stopBackend()
        splashStep('正在重启后端...', 35)
        await startBackend().catch(() => { })
        splashStep('正在准备界面...', 80)
        if (!mainWindow || !mainWindow.isVisible()) {
          if (!mainWindow) await createMainWindow().catch(() => { })
          else { mainWindow.show(); mainWindow.focus() }
        }
        splashStep('DONE')
        setTimeout(closeSplash, 800)
      }
    },
    {
      label: '⏹ 停止全部服务', click: () => {
        stopBackend()
        if (mainWindow) { mainWindow.close() }
        _updateTrayStatus('⏹ 已停止')
      }
    },
    { type: 'separator' },
    {
      label: '🌐 打开界面', default: true, click: async () => {
        if (!mainWindow) { await createMainWindow().catch(() => { }) }
        else if (!mainWindow.isVisible()) { mainWindow.show() }
        mainWindow?.focus()
      }
    },
    { label: '🌐 用浏览器打开', click: () => shell.openExternal(IS_DEV ? DEV_URL : DEV_URL) },

    { type: 'separator' },
    {
      label: '❌ 退出程序', click: () => {
        stopBackend()
        app.quit()
      }
    },
  ]
}

function _updateTrayStatus(newStatus) {
  if (!tray || newStatus === _trayStatus) return
  _trayStatus = newStatus
  tray.setToolTip(`AgentCluster  ${newStatus}\n双击打开界面 · 右键管理程序`)
  try { tray.setContextMenu(Menu.buildFromTemplate(_buildTrayMenuTemplate(newStatus))) } catch { }
}

// Web 模式退出时：杀掉 start.bat 启动的 python + node（按端口匹配）
function _killWebProcesses() {
  try {
    const { execSync } = require('child_process')
    for (const port of [BACKEND_PORT, 5173]) {
      try {
        const out = execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8' })
        const m = out.trim().split(/\s+/)
        const pid = parseInt(m[m.length - 1], 10)
        if (pid && pid !== process.pid) {
          try { process.kill(pid, 'SIGTERM') } catch { }
          try { execSync(`taskkill /F /PID ${pid}`, { encoding: 'utf8', stdio: 'pipe' }) } catch { }
        }
      } catch { }
    }
  } catch { }
}

function _dialogSync(title, msg) {
  try {
    dialog.showMessageBoxSync({ message: `${title}\n\n${msg}`, type: 'info', title: 'AgentCluster' })
  } catch { }
}

function createTray() {
  // 图标：优先项目 agentcluster.ico（Windows 多分辨率最佳），否则 png fallback
  const iconPaths = [
    path.join(PROJECT_ROOT, 'agentcluster.ico'),
    path.join(ROOT, 'public', 'icon.png'),
    path.join(ROOT, 'dist', 'icon.png'),
    path.join(ROOT, 'public', 'icon.jpg'),
    path.join(PROJECT_ROOT, '1791461860623.webp.jpg'),
  ]
  let img = null
  for (const p of iconPaths) {
    if (fs.existsSync(p)) {
      img = nativeImage.createFromPath(p)
      if (!img.isEmpty()) break
      img = null
    }
  }
  if (!img || img.isEmpty()) {
    // fallback: 用 Electron 自带的 icon
    try {
      const exePath = app.getPath('exe')
      const resources = path.join(path.dirname(exePath), 'resources')
      for (const f of ['icon.png', 'default_app.asar/icon.png']) {
        const p = path.join(resources, f)
        if (fs.existsSync(p)) { img = nativeImage.createFromPath(p); break }
      }
    } catch { }
  }
  if (!img || img.isEmpty()) {
    img = nativeImage.createEmpty()
  }

  tray = new Tray(img)
  tray.setToolTip('AgentCluster  正在启动...\n双击打开界面 · 右键管理程序')
  tray.setContextMenu(Menu.buildFromTemplate(_buildTrayMenuTemplate('⏹ 已停止')))

  // 左键 = 打开界面（default: true 的项）
  tray.on('click', async () => {
    if (!mainWindow) { await createMainWindow().catch(() => { }) }
    else if (!mainWindow.isVisible()) { mainWindow.show() }
    mainWindow?.focus()
  })
  tray.on('double-click', async () => {
    if (!mainWindow) { await createMainWindow().catch(() => { }) }
    else if (!mainWindow.isVisible()) { mainWindow.show() }
    mainWindow?.focus()
  })

  // 每 3s 刷一次状态（与原 start_tray.py 一致）
  _trayRefreshTimer = setInterval(async () => {
    const h = await _checkHealth()
    let status
    if (h.backend && h.frontend) status = '✅ 运行中'
    else if (!h.backend && !h.frontend) status = '⏹ 已停止'
    else status = `⚠️ 部分失败 (后端=${h.backend ? 'OK' : 'FAIL'}, 前端=${h.frontend ? 'OK' : 'FAIL'})`
    _updateTrayStatus(status)
  }, 3000)
}

// ---------- 精灵悬浮球拓展（fairy_ball） ----------
// 球窗 / 区域截图选区窗 / 临时对话小窗的宿主壳。
// 页面 HTML 全部由拓展后端下发（/api/ext/fairy_ball/page|snip|quickchat），
// 改球外观无需重建前端 dist；本文件只提供窗口与截图能力。

const FAIRY_ID = 'fairy_ball'
const FAIRY_BASE = `http://127.0.0.1:${BACKEND_PORT}/api/ext/${FAIRY_ID}`
let fairyWin = null        // 悬浮球窗口
let quickWin = null        // 临时对话小窗
let snipWin = null         // 区域截图选区窗（全屏）
let fairyPollTimer = null
let _snipFullImg = null    // 选区期间的全屏 nativeImage（物理分辨率）
let _snipScale = 1         // 主屏 DPI 缩放（CSS 逻辑坐标 → 物理像素）

// 右键菜单窗内容：预读 menu.html 编码为 data URL，彻底消除每次右键的 HTTP 回环请求。
// menu.html 是纯静态页面（皮肤选择、动作分发），不需要后端动态下发；
// 改动后重启 Electron 生效。
let _menuDataUrl = ''
try {
  const _menuHtmlPath = path.join(PROJECT_ROOT, 'AppData', 'extensions', '_builtin', 'fairy_ball', 'menu.html')
  const _menuHtml = fs.readFileSync(_menuHtmlPath, 'utf-8')
  _menuDataUrl = 'data:text/html;charset=utf-8,' + encodeURIComponent(_menuHtml)
} catch (e) {
  _log('[fairy] 预读 menu.html 失败，将 fallback HTTP 加载：' + e.message)
}

function _fairyWebPrefs() {
  // backgroundThrottling:false —— fairy 系窗口（球窗/菜单窗/小窗等）失去焦点或被垫层
  // 覆盖时 Chromium 不节流 rAF/timer：球窗的逐帧命中测试与状态轮询保持实时
  return { preload: PRELOAD, contextIsolation: true, nodeIntegration: false, sandbox: false, backgroundThrottling: false }
}

function createFairyWin() {
  if (fairyWin && !fairyWin.isDestroyed()) return fairyWin
  const wa = screen.getPrimaryDisplay().workAreaSize
  // 精灵窗口:透明无框,尺寸要足够装下 HOME 3D 动画。
  // WebM 动画原始 360px 高,按 .pk 容器 max-height 160px 显示,加上发光阴影和呼吸动画,
  // 给足 padding 避免边缘被窗口裁切。从 180×170 扩大到 260×260。
  fairyWin = new BrowserWindow({
    width: 260, height: 260,
    x: wa.width - 285, y: wa.height - 290,
    frame: false, transparent: true, alwaysOnTop: true,
    skipTaskbar: true, resizable: false, hasShadow: false,
    show: false,
    focusable: false,   // 不可聚焦：点击球体不抢系统焦点，主窗 isFocused() 判断不被扰动（单击切换最小化的关键）
    webPreferences: _fairyWebPrefs(),
  })
  fairyWin.setAlwaysOnTop(true, 'screen-saver')
  fairyWin.loadURL(`${FAIRY_BASE}/page`)
  // 初始即穿透（页面脚本加载前的窗口里大部分是空白；页面加载后由 hit-test 动态翻转）
  try { fairyWin.setIgnoreMouseEvents(true, { forward: true }) } catch { }
  fairyWin.once('ready-to-show', () => fairyWin.show())
  fairyWin.webContents.on('did-fail-load', (_e, code) => {
    // 后端未就绪 / 拓展刚停用 → 关闭，等下一轮轮询重试
    _log(`[fairy] 球页面加载失败 code=${code}，稍后重试`)
    try { fairyWin.close() } catch { }
    fairyWin = null
  })
  fairyWin.on('closed', () => { fairyWin = null })
  return fairyWin
}

async function showMainWindow(route) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    await createMainWindow()
  }
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
  if (route) {
    // hash 路由跳转（避免整页刷新）
    mainWindow.webContents.executeJavaScript(`location.hash = ${JSON.stringify(route)}; true`).catch(() => { })
  }
}

// ---------- 精灵右键独立菜单窗 ----------
// 菜单不再塞在球窗 180×170 里（受边界钳制、内容要视图切换滚动），
// 而是独立透明置顶小窗：位置随鼠标、屏幕自由弹出、内容平铺。
// 「点击外部关闭」用全屏透明捕获层实现（原生菜单行为）：
//   捕获层 level 1（先 show，盖住桌面/球窗）→ 菜单窗 level 2（最上）；
//   用户点捕获层任意处（含点球）= 关闭菜单（本次点击被吃掉不透传，与原生一致）。
// 两窗均 focusable:false —— 不抢系统焦点，主窗 isFocused() 三态切换判断不被扰动。
let fairyMenuWin = null
let fairyCatchWin = null
let _menuAnchor = { x: 0, y: 0 }   // 弹出锚点（右键鼠标屏幕坐标，重定位用）
let _menuHadMainFocus = false      // 弹菜单前主窗是否持有焦点（选完关闭后归还，防打字被打断后回不来）

// 捕获层页面：全屏透明，任意 mousedown/contextmenu → 请求关闭菜单
const _FAIRY_CATCH_HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;width:100%;height:100%;background:transparent;overflow:hidden;cursor:default}
</style></head><body><script>
function close(){ try{ window.electronAPI && window.electronAPI.fairy && window.electronAPI.fairy.closeMenu() }catch(e){} }
document.addEventListener('mousedown', close, true);
document.addEventListener('contextmenu', e => { e.preventDefault(); close() });
</script></body></html>`

/**
 * 关闭菜单（含捕获层）。
 * restoreFocus=true：菜单页自身收起（选完皮肤/大小）→ 归还弹菜单前主窗的焦点；
 * false（点外部/失焦/动作分发）：焦点留给用户点击的目标或后续动作窗口，不强行拉回主窗。
 */
function closeFairyMenu(restoreFocus) {
  const had = _menuHadMainFocus
  _menuHadMainFocus = false
  if (fairyMenuWin && !fairyMenuWin.isDestroyed()) { try { fairyMenuWin.close() } catch { } }
  fairyMenuWin = null
  if (fairyCatchWin && !fairyCatchWin.isDestroyed()) { try { fairyCatchWin.close() } catch { } }
  fairyCatchWin = null
  if (restoreFocus && had && mainWindow && !mainWindow.isDestroyed()
    && mainWindow.isVisible() && !mainWindow.isMinimized()) {
    try { mainWindow.focus() } catch { }
  }
}

/** 菜单位置：锚点右下偏移，工作区四缘钳制（右/下缘超界朝左/上翻转） */
function _clampMenuPos(anchor, w, h, workArea) {
  let mx = anchor.x + 8, my = anchor.y + 8
  if (mx + w > workArea.x + workArea.width - 6) mx = anchor.x - w - 8
  if (my + h > workArea.y + workArea.height - 6) my = anchor.y - h - 8
  mx = Math.max(workArea.x + 4, Math.min(mx, workArea.x + workArea.width - w - 4))
  my = Math.max(workArea.y + 4, Math.min(my, workArea.y + workArea.height - h - 4))
  return [Math.round(mx), Math.round(my)]
}

function openFairyMenu(x, y) {
  closeFairyMenu()   // 重开 = 重定位（幂等）
  _menuAnchor = { x: Math.round(x), y: Math.round(y) }
  // 弹菜单前记录主窗焦点状态（菜单窗要抢焦点使 blur 机制生效，关闭后据此归还）
  _menuHadMainFocus = !!(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused())
  const disp = screen.getDisplayNearestPoint({ x: _menuAnchor.x, y: _menuAnchor.y })
  // 1) 全屏透明捕获层（先建先 show → 垫在菜单窗下面）
  fairyCatchWin = new BrowserWindow({
    x: disp.bounds.x, y: disp.bounds.y,
    width: disp.bounds.width, height: disp.bounds.height,
    frame: false, transparent: true, hasShadow: false,
    resizable: false, movable: false, fullscreenable: false,
    skipTaskbar: true, show: false, focusable: false,
    webPreferences: _fairyWebPrefs(),
  })
  fairyCatchWin.setAlwaysOnTop(true, 'screen-saver', 1)
  fairyCatchWin.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(_FAIRY_CATCH_HTML))
  fairyCatchWin.once('ready-to-show', () => { if (fairyCatchWin && !fairyCatchWin.isDestroyed()) fairyCatchWin.show() })
  fairyCatchWin.on('closed', () => { if (fairyCatchWin) fairyCatchWin = null })
  // 2) 菜单窗（menu.html 已预读为 data URL，零延迟加载；尺寸由页面上报后 setContentSize）
  fairyMenuWin = new BrowserWindow({
    width: 200, height: 300, useContentSize: true,
    frame: false, transparent: true, hasShadow: false,
    resizable: false, movable: false, fullscreenable: false,
    skipTaskbar: true, show: false,
    focusable: true,   // 可聚焦：失焦自动消失（blur 关闭）+ 能收键盘（ESC）
    webPreferences: _fairyWebPrefs(),
  })
  fairyMenuWin.setAlwaysOnTop(true, 'screen-saver', 2)   // 盖在捕获层之上
  const [mx, my] = _clampMenuPos(_menuAnchor, 200, 300, disp.workArea)
  fairyMenuWin.setPosition(mx, my)
  // 优先 data URL（启动时预读的 menu.html，零延迟）；fallback HTTP 加载（预读失败时）
  fairyMenuWin.loadURL(_menuDataUrl || `${FAIRY_BASE}/menu`)
  // 页面上报实际尺寸 → 调整窗尺寸并重定位；150ms 兜底强制显示（data URL 加载极快，150ms 足够）
  let shown = false
  const showMenu = () => {
    if (shown || !fairyMenuWin || fairyMenuWin.isDestroyed()) return
    shown = true
    fairyMenuWin.show()
    fairyMenuWin.focus()   // 抢焦点使 blur 机制生效
  }
  fairyMenuWin._showMenu = showMenu
  setTimeout(showMenu, 150)
  // 失焦自动关（点其他应用/窗口、Alt+Tab 切走）；短延迟复核防 show 焦点竞态误关
  fairyMenuWin.on('blur', () => {
    setTimeout(() => {
      if (fairyMenuWin && !fairyMenuWin.isDestroyed() && !fairyMenuWin.isFocused()) closeFairyMenu(false)
    }, 120)
  })
  fairyMenuWin.on('closed', () => {
    if (fairyMenuWin) fairyMenuWin = null
    // 菜单窗销毁时捕获层一并收（选完菜单项自关时也走到这里）
    if (fairyCatchWin && !fairyCatchWin.isDestroyed()) { try { fairyCatchWin.close() } catch { } }
  })
}

function openQuickChat(shotPath) {
  if (quickWin && !quickWin.isDestroyed()) {
    quickWin.show()
    quickWin.focus()
    if (shotPath) sendQuickShot(shotPath)
    return
  }
  quickWin = new BrowserWindow({
    width: 480, height: 740, minWidth: 400, minHeight: 560,
    frame: false, backgroundColor: '#000000',
    resizable: true, skipTaskbar: false,
    show: false,
    webPreferences: _fairyWebPrefs(),
  })
  // 复用主窗口前端应用（#/quickchat 视图 = RightPanel 精简壳）：
  // markdown / viz 思维导图 / 思考折叠 / 附件等渲染能力与主窗完全一致，无需重复实现
  const base = IS_DEV ? DEV_URL : PROD_URL
  quickWin.loadURL(base + '/#/quickchat')
  quickWin.once('ready-to-show', () => quickWin.show())
  if (shotPath) {
    // 等 Vue 挂载完成（RightPanel 的 quick:shot 监听就绪）再发截图路径
    quickWin.webContents.once('did-finish-load', () => {
      setTimeout(() => { if (quickWin && !quickWin.isDestroyed()) sendQuickShot(shotPath) }, 400)
    })
  }
  quickWin.on('closed', () => { quickWin = null })
}

function sendQuickShot(shotPath) {
  if (quickWin && !quickWin.isDestroyed()) {
    quickWin.webContents.send('quick:shot', { path: shotPath })
  }
}

// —— 区域截图：隐藏球 → desktopCapturer 全屏截 → 选区窗框选 → 裁剪保存 → 分发去向 ——
async function startSnip() {
  closeFairyMenu()   // 菜单开着时点截图提问：先进截图流程（捕获层不能入镜）
  // 球置顶会入镜 → 先藏（结束后恢复）
  let fairyHidden = false
  if (fairyWin && !fairyWin.isDestroyed() && fairyWin.isVisible()) {
    fairyWin.hide()
    fairyHidden = true
    await new Promise(r => setTimeout(r, 160))
  }
  try {
    const primary = screen.getPrimaryDisplay()
    const { width, height } = primary.size
    const scaleFactor = primary.scaleFactor || 1
    const thumbW = Math.max(1, Math.round(width * scaleFactor))
    const thumbH = Math.max(1, Math.round(height * scaleFactor))
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: thumbW, height: thumbH },
    })
    const src = sources.find(s => String(s.display_id) === String(primary.id)) || sources[0]
    if (!src || src.thumbnail.isEmpty()) throw new Error('未获取到屏幕内容')
    _snipFullImg = src.thumbnail
    _snipScale = scaleFactor
    snipWin = new BrowserWindow({
      x: primary.bounds.x, y: primary.bounds.y, width, height,
      frame: false, transparent: true, alwaysOnTop: true,
      skipTaskbar: true, resizable: false, fullscreenable: false,
      show: false,
      webPreferences: _fairyWebPrefs(),
    })
    snipWin.setAlwaysOnTop(true, 'screen-saver')
    snipWin.loadURL(`${FAIRY_BASE}/snip`)
    snipWin.once('ready-to-show', () => {
      snipWin.show()
      snipWin.focus()
      snipWin.webContents.send('snip:image', _snipFullImg.toDataURL())
    })
    snipWin.on('closed', () => { snipWin = null })
  } catch (err) {
    _log('[fairy] 截图启动失败：' + (err?.message || err))
    _dialogSync('截图失败', String(err?.message || err))
    closeSnipWin()
    if (fairyHidden && fairyWin && !fairyWin.isDestroyed()) fairyWin.show()
  }
}

function closeSnipWin() {
  if (snipWin && !snipWin.isDestroyed()) snipWin.close()
  snipWin = null
  _snipFullImg = null
}

async function onSnipChoose(data) {
  const target = data?.target
  const restoreFairy = () => { if (fairyWin && !fairyWin.isDestroyed() && !fairyWin.isVisible()) fairyWin.show() }
  if (!target || target === 'cancel') { closeSnipWin(); restoreFairy(); return }
  const rect = data?.rect
  if (!rect || !_snipFullImg) { closeSnipWin(); restoreFairy(); return }
  try {
    // CSS 逻辑坐标 → 物理像素（DPI 缩放）
    const sf = _snipScale
    const cropped = _snipFullImg.crop({
      x: Math.max(0, Math.round(rect.x * sf)),
      y: Math.max(0, Math.round(rect.y * sf)),
      width: Math.max(1, Math.round(rect.w * sf)),
      height: Math.max(1, Math.round(rect.h * sf)),
    })
    // 保存到 /api/fs/image 白名单内的临时截图目录
    const shotDir = path.join(os.homedir(), 'AppData', 'Local', 'Temp', 'agentcluster_screenshots')
    fs.mkdirSync(shotDir, { recursive: true })
    const shotPath = path.join(shotDir, `fairy_${Date.now()}.png`)
    fs.writeFileSync(shotPath, cropped.toPNG())
    closeSnipWin()
    if (target === 'main') {
      // 先回首页（RightPanel 挂载/KeepAlive 恢复）再发截图事件，避免停在设置页收不到
      await showMainWindow('#/')
      setTimeout(() => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('fairy:shot', { path: shotPath })
        }
      }, 500)
    } else if (target === 'quick') {
      openQuickChat(shotPath)
    }
  } catch (err) {
    _log('[fairy] 截图裁剪失败：' + (err?.message || err))
    closeSnipWin()
  } finally {
    restoreFairy()
  }
}

// —— 拓展启停感知：轮询 /enabled 决定球窗开关 ——
function startFairyPolling() {
  if (fairyPollTimer) return
  fairyPollTimer = setInterval(async () => {
    let enabled = false
    try {
      const res = await fetch(`${FAIRY_BASE}/enabled`, { signal: AbortSignal.timeout(2000) })
      if (res.ok) enabled = !!(await res.json()).enabled
    } catch { }
    try {
      if (enabled) {
        if (!fairyWin || fairyWin.isDestroyed()) createFairyWin()
      } else if (fairyWin && !fairyWin.isDestroyed()) {
        fairyWin.close()
        fairyWin = null
        closeFairyMenu()   // 拓展停用：菜单（含捕获层）一并关闭
      }
    } catch (e) {
      _log('[fairy] 球窗管理异常：' + e.message)
    }
  }, 4000)
}

function registerFairyIPC() {
  ipcMain.handle('fairy:openMain', (_e, route) => {
    // 单击切换：主窗不可见/已最小化 → 恢复显示；可见但被遮挡（非焦点）→ 置前聚焦；可见且已聚焦 → 最小化到任务栏
    if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && !mainWindow.isMinimized()) {
      if (mainWindow.isFocused()) {
        mainWindow.minimize()
      } else {
        mainWindow.show()
        mainWindow.focus()
      }
      return
    }
    showMainWindow(route).catch(() => { })
  })
  ipcMain.handle('fairy:action', (_e, name) => {
    closeFairyMenu()   // 菜单动作分发前收起菜单（动作现在来自独立菜单窗）
    if (name === 'screenshot') {
      startSnip().catch(e => _log('[fairy] startSnip: ' + e.message))
    } else if (name === 'quickchat') {
      openQuickChat()
    } else if (name === 'settings') {
      showMainWindow('#/settings').catch(() => { })
    }
  })
  // —— 独立菜单窗 IPC ——
  ipcMain.handle('fairy:openMenu', (_e, x, y) => { openFairyMenu(x, y) })
  ipcMain.handle('fairy:closeMenu', (e) => {
    // 来源区分：菜单页自身收起（选完皮肤/大小）→ 归还主窗焦点；
    // 捕获层点击外部 → 不归还（焦点应留给用户点击的目标应用/窗口）
    const fromMenu = !!(fairyMenuWin && !fairyMenuWin.isDestroyed() && e.sender === fairyMenuWin.webContents)
    closeFairyMenu(fromMenu)
  })
  ipcMain.handle('fairy:menuSize', (e, w, h) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win.isDestroyed()) return
    const cw = Math.max(120, Math.min(Math.round(w) || 200, 420))
    const ch = Math.max(80, Math.min(Math.round(h) || 300, 900))
    win.setContentSize(cw, ch)
    // 视图切换尺寸变化 → 以锚点重新钳制定位（防超屏幕边缘）
    const disp = screen.getDisplayNearestPoint({ x: _menuAnchor.x, y: _menuAnchor.y })
    win.setPosition(..._clampMenuPos(_menuAnchor, cw, ch, disp.workArea))
    if (typeof win._showMenu === 'function') win._showMenu()   // 尺寸就绪立即显示
  })

  ipcMain.handle('fairy:winPos', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    if (!w) return null
    const p = w.getPosition()
    return { x: p[0], y: p[1] }
  })
  // 穿透开关（球页 hit-test 驱动）：forward:true → 穿透期间 mousemove 仍派发给球页做命中测试
  ipcMain.handle('fairy:setIgnoreMouse', (e, ignore) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    if (w) { try { w.setIgnoreMouseEvents(!!ignore, { forward: true }) } catch { } }
  })
  ipcMain.handle('fairy:moveTo', (e, x, y) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    if (w) w.setPosition(Math.round(x), Math.round(y))
  })
  ipcMain.handle('fairy:closeWin', (e) => { BrowserWindow.fromWebContents(e.sender)?.close() })
  ipcMain.handle('snip:choose', (_e, data) => { onSnipChoose(data) })
}

// ---------- IPC：窗口控制 ----------

function registerIPC() {
  // 窗口控制
  ipcMain.handle('win:minimize', () => { _log('[IPC] win:minimize'); mainWindow?.minimize() })
  ipcMain.handle('win:maximize', () => { _log('[IPC] win:maximize'); return mainWindow?.isMaximized() || mainWindow?.maximize() })
  ipcMain.handle('win:unmaximize', () => { _log('[IPC] win:unmaximize'); mainWindow?.unmaximize() })
  ipcMain.handle('win:toggleMax', () => {
    _log('[IPC] win:toggleMax, mainWindow=', !!mainWindow, 'isMaximized=', mainWindow?.isMaximized())
    if (!mainWindow) return false
    if (mainWindow.isMaximized()) { mainWindow.unmaximize(); return false }
    mainWindow.maximize(); return true
  })
  ipcMain.handle('win:restore', () => { _log('[IPC] win:restore'); mainWindow?.unmaximize() || mainWindow?.show() })
  ipcMain.handle('win:close', () => { _log('[IPC] win:close'); mainWindow?.close() })
  ipcMain.handle('win:getBounds', () => {
    if (!mainWindow) return { x: 0, y: 0, width: 1280, height: 820 }
    const b = mainWindow.getBounds()
    return { x: b.x, y: b.y, width: b.width, height: b.height }
  })
  ipcMain.handle('win:setBounds', (_e, bounds) => {
    if (!mainWindow) return
    mainWindow.setBounds({
      x: Math.round(bounds.x),
      y: Math.round(bounds.y),
      width: Math.round(bounds.width),
      height: Math.round(bounds.height),
    })
  })
  ipcMain.handle('win:resize', (_e, w, h) => mainWindow?.setSize(Math.round(w), Math.round(h)))
  ipcMain.handle('win:move', (_e, x, y) => mainWindow?.setPosition(Math.round(x), Math.round(y)))
  ipcMain.handle('win:isMaximized', () => mainWindow?.isMaximized() ?? false)

  // 后端健康检查
  ipcMain.handle('api:health', async () => {
    try {
      const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1500) })
      return res.ok
    } catch { return false }
  })

  // 最大化状态变化同步回前端（前端按钮需要知道 isMax）
  mainWindow?.on('maximize', () => mainWindow.webContents.send('win:maximized', true))
  mainWindow?.on('unmaximize', () => mainWindow.webContents.send('win:maximized', false))
}

// ---------- App 生命周期 ----------

// 单例锁：默认只允许一个实例跑。
// --no-single-instance 参数：跳过单例锁（开发机调试 / Python launcher 启动时用）
// app.isPackaged=false 时（直接跑 electron . 而不是打包后的 exe）也跳过
if (app.isPackaged && !NO_SINGLE_INSTANCE) {
  const gotLock = app.requestSingleInstanceLock()
  _log('isPackaged单例锁 gotLock=' + gotLock)
  if (!gotLock) {
    _log('已有实例在跑，退出本次')
    app.quit()
  } else {
    app.on('second-instance', async () => {
      _log('second-instance: 把已有窗口拉到前台')
      if (!mainWindow) await createMainWindow()
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    })
  }
} else {
  _log('开发模式，跳过单例锁')
}

app.whenReady().then(async () => {
  if (TRAY_ONLY) {
    _log('=== TRAY-ONLY 模式：不启动后端/不建主窗口 ===')
    createTray()
    return
  }

  createSplash()
  splashStep('正在初始化启动器...', 5)

  // Prod 模式：先起 dist server（让 127.0.0.1:5173 可访问）
  if (!IS_DEV) {
    splashStep('正在启动前端静态服务...', 15)
    try {
      await startDistServer()
    } catch (err) {
      console.warn('[Electron] Dist server 启动失败:', err.message)
      _log('Dist server failed: ' + err.message + '\n' + (err.stack || ''))
      const msg = err.message || String(err)
      // 超时：开机冷启动常见（杀软扫描 node.exe 可近 1 分钟），静默继续——
      // distServerProc 未被杀仍在后台启动，主窗口加载失败会自动重试直至就绪。
      // 严禁此处用 showMessageBoxSync：同步弹窗会阻塞主进程直到用户点确定。
      // 仅致命配置错误（找不到 Node.js / dist 未构建）异步提示，不阻塞主进程。
      if (!msg.includes('timeout')) {
        try {
          dialog.showMessageBox({ type: 'info', title: 'AgentCluster', message: `启动警告\n\n${msg}` }).catch(() => { })
        } catch { }
      }
    }
  }

  // 用户已在启动画面点 ✕ 中止启动：不再继续后续阶段（quit 由 splash:abort 统一发起）
  if (_startupAborted) return

  // 启动后端并等待就绪（必须在 createMainWindow 之前完成，否则 Home.vue 的 fetch 会因后端未就绪而失败）
  splashStep('正在启动后端服务...', 25)
  try {
    await startBackend()
    splashStep('后端就绪', 55)
  } catch (err) {
    console.warn('[Electron] 后端拉起失败（窗口仍可打开）:', err.message)
    splashStep('后端启动超时', 55)
  }

  if (_startupAborted) return

  try {
    splashStep('正在准备界面...', 70)
    if (_startupAborted) return
    await createMainWindow()
    createTray()
    registerIPC()
    // 精灵悬浮球拓展（fairy_ball）：IPC 注册 + 轮询拓展启用状态决定球窗开关
    registerFairyIPC()
    startFairyPolling()
    splashStep('DONE')
    setTimeout(closeSplash, 800)
  } catch (err) {
    console.error('[Electron] 初始化失败:', err)
    splashStep('界面加载失败', 90)
    setTimeout(closeSplash, 2000)
  }
})

app.on('window-all-closed', () => {
  // 不关托盘，保持后台运行
})

app.on('before-quit', () => {
  if (TRAY_ONLY) {
    _killWebProcesses()
  } else {
    stopBackend()
    stopDistServer()
  }
})

app.on('activate', async () => {
  if (BrowserWindow.getAllWindows().length === 0) await createMainWindow()
})

