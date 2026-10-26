// 临时实验脚本：验证 zoom 换行漂移 + transform:scale 清晰度（实验后删除）
const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')

const TEXT = `我来分析一下这个模块的性能瓶颈。The main issue is that the pipeline blocks on synchronous I/O, 导致后续的 agent 无法并行执行。建议改用 asyncio.gather 加 aiofiles，预计吞吐量可以提升 3~5 倍。另外注意 Windows 下的 proactor event loop 对 subprocess 支持有限（参见 PEP 525 相关讨论），必要时可以 fallback 到 ThreadPoolExecutor 兜底。数据层面：缓存命中率只有 42.7%，明显低于预期的 85%，建议把 LRU 容量从 512 调到 2048，同时给热点 key 加预加载。还有一个小问题：日志里的时间戳格式不统一，有的是 ISO 8601，有的是 unix epoch 毫秒值，聚合统计时会报解析错误，需要统一成同一种再入库。`

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
body { margin: 0; background: #fff; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
.box { width: 230px; padding: 8px 10px; font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; color: #111; }
#t1 { margin: 10px; }
#za { margin: 10px; zoom: 2; }
#zb { margin: 10px; transform: scale(2); transform-origin: 0 0; }
#zc { margin: 10px; transform: scale(2); transform-origin: 0 0; will-change: transform; }
.row { overflow: hidden; }
</style></head><body>
<div class="row"><div id="t1" class="box">${TEXT}</div></div>
<div class="row"><div id="za" class="box">${TEXT}</div></div>
<div class="row"><div id="zb" class="box">${TEXT}</div></div>
<div class="row"><div id="zc" class="box">${TEXT}</div></div>
</body></html>`

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false, width: 700, height: 2400,
    webPreferences: { offscreen: true, contextIsolation: true },
  })
  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  await new Promise(r => setTimeout(r, 800))

  // —— 实验一：zoom 各档位下换行点是否漂移 ——
  const drift = await win.webContents.executeJavaScript(`(function(){
    const t = document.getElementById('t1')
    const tn = t.firstChild
    function lineInfo(z) {
      t.style.zoom = z
      void t.offsetHeight
      const zs = z || 1
      let line = 0, lastTop = -1
      const lines = []
      for (let i = 0; i < tn.length; i++) {
        const r = document.createRange()
        r.setStart(tn, i); r.setEnd(tn, i + 1)
        const rect = r.getBoundingClientRect()
        if (rect.height === 0) { lines.push(-1); continue }
        const top = rect.top / zs
        if (lastTop < 0 || Math.abs(top - lastTop) > 5) { line++; lastTop = top }
        lines.push(line)
      }
      return lines
    }
    const base = lineInfo(1)
    const out = {}
    for (const z of [1.25, 1.5625, 0.8, 0.64, 2.5]) {
      const cur = lineInfo(z)
      let n = 0, first = -1, ch = ''
      for (let k = 0; k < base.length; k++) {
        if (base[k] !== cur[k] && base[k] > 0) { n++; if (first < 0) { first = k; ch = tn.textContent[k] } }
      }
      out['zoom_' + z] = { 漂移字符数: n, 首个漂移位置: first, 字符: ch, 总字符: base.length }
    }
    return out
  })()`)
  console.log('实验一 zoom 换行漂移:', JSON.stringify(drift, null, 2))

  // —— 实验二：截图对比 zoom:2 / transform:scale(2) 无 will-change / transform:scale(2) + will-change ——
  const img = await win.webContents.capturePage()
  const info = await win.webContents.executeJavaScript(`(function(){
    function rect(id) { const r = document.getElementById(id).getBoundingClientRect();
      return { x: r.left, y: r.top, w: r.width, h: r.height } }
    return { a: rect('za'), b: rect('zb'), c: rect('zc'), vw: window.innerWidth, ih: window.innerHeight }
  })()`)
  const k = img.getSize().width / info.vw  // 截图像素 / CSS 像素 比例
  const dir = path.join(__dirname, 'zoom-test-out')
  fs.mkdirSync(dir, { recursive: true })
  for (const [name, r] of [['A_zoom2', info.a], ['B_transform2', info.b], ['C_transform2_willchange', info.c]]) {
    img.crop({ x: Math.round(r.x * k), y: Math.round(r.y * k), width: Math.round(r.w * k), height: Math.round(r.h * k) })
      .toPNG ? null : null
  }
  // toPNG 是方法，逐个保存
  for (const [name, r] of [['A_zoom2', info.a], ['B_transform2', info.b], ['C_transform2_willchange', info.c]]) {
    const png = img.crop({ x: Math.round(r.x * k), y: Math.round(r.y * k), width: Math.round(r.w * k), height: Math.round(r.h * k) }).toPNG()
    fs.writeFileSync(path.join(dir, name + '.png'), png)
    console.log('已保存', name + '.png', Math.round(r.w * k) + 'x' + Math.round(r.h * k), 'crop 比例 k=' + k)
  }
  app.quit()
}).catch(e => { console.error(e); app.quit() })