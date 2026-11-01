// 校验 fairy_ball 菜单独立窗改造：页面 JS 语法 + 关键逻辑（防超屏/失焦关/焦点归还/换装同步）
const fs = require('fs')

let fail = 0
const ok = (name) => console.log('✓ ' + name)
const bad = (name) => { console.log('✗ ' + name); fail++ }

for (const f of ['AppData/extensions/fairy_ball/fairy.html', 'AppData/extensions/fairy_ball/menu.html']) {
    const html = fs.readFileSync(f, 'utf8')
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    let err = null
    scripts.forEach(m => { try { new Function(m[1]) } catch (e) { err = e } })
    if (err) { bad('JS 语法 ' + f + ' → ' + err.message) } else { ok('JS 语法 ' + f + ' (' + scripts.length + ' 块)') }
}

const main = fs.readFileSync('frontend/electron/main.cjs', 'utf8')
const preload = fs.readFileSync('frontend/electron/preload.cjs', 'utf8')
const fairyHtml = fs.readFileSync('AppData/extensions/fairy_ball/fairy.html', 'utf8')
const menuHtml = fs.readFileSync('AppData/extensions/fairy_ball/menu.html', 'utf8')
const manifest = JSON.parse(fs.readFileSync('AppData/extensions/fairy_ball/manifest.json', 'utf8'))

const mainChecks = [
    ['main: openFairyMenu 定义', /function openFairyMenu/],
    ['main: closeFairyMenu 定义', /function closeFairyMenu/],
    ['main: 捕获层 HTML 常量', /_FAIRY_CATCH_HTML/],
    ['main: 捕获层 level 1', /'screen-saver', 1/],
    ['main: 菜单窗 level 2', /'screen-saver', 2/],
    ['main: openMenu IPC handler', /fairy:openMenu/],
    ['main: closeMenu IPC handler', /fairy:closeMenu/],
    ['main: menuSize IPC handler', /fairy:menuSize/],
    ['main: 动作分发前关菜单', /closeFairyMenu\(\).*?菜单动作分发前收起菜单/s],
    ['main: 截图前关菜单', /closeFairyMenu\(\).*?菜单开着时点截图提问/s],
    ['main: 停用关菜单', /closeFairyMenu\(\).*?拓展停用/s],
    ['main: 菜单窗可聚焦', /focusable: true/],
    ['main: 失焦自动关', /on\('blur'.*?closeFairyMenu\(false\)/s],
    ['main: 弹出即抢焦点', /fairyMenuWin\.focus\(\)/],
    ['main: 焦点记录与归还', /_menuHadMainFocus/],
    ['main: closeMenu 来源区分', /e\.sender === fairyMenuWin\.webContents/],
    ['main: 工作区钳制', /_clampMenuPos/],
    ['main: 关闭 native occlusion 计算（防捕获层遮挡致球窗动画冻结）', /CalculateNativeWinOcclusion/],
    ['main: fairy 窗口禁后台节流', /backgroundThrottling: false/],
    ['preload: openMenu 桥', /openMenu:/],
    ['preload: closeMenu 桥', /closeMenu:/],
    ['preload: menuSize 桥', /menuSize:/],
]
for (const [name, re] of mainChecks) {
    if (re.test(main) || re.test(preload)) ok(name); else bad(name)
}

const pageChecks = [
    ['fairy: 右键调 openMenu（屏幕坐标）', fairyHtml, /fairyAPI\.openMenu && fairyAPI\.openMenu\(e\.screenX, e\.screenY\)/],
    ['fairy: 内嵌菜单 DOM 已移除', fairyHtml, !/id="menu"/],
    ['fairy: 内嵌菜单 JS 已移除', fairyHtml, !/menuOpen/],
    ['fairy: storage 换装同步', fairyHtml, /addEventListener\('storage'/],
    ['menu: ESC 关闭', menuHtml, /'Escape'/],
    ['menu: 尺寸上报', menuHtml, /menuSize/],
]
for (const [name, src, re] of pageChecks) {
    if (re instanceof RegExp ? re.test(src) : !!src) ok(name); else bad(name)
}

if (manifest.version === '1.3.1') ok('manifest: 版本 1.3.1'); else bad('manifest: 版本应为 1.3.1，实际 ' + manifest.version)
if (manifest.pages.includes('menu.html')) ok('manifest: pages 含 menu.html'); else bad('manifest: pages 缺 menu.html')

// _builtin 与已安装目录同步一致性
for (const f of ['fairy.html', 'menu.html', 'manifest.json']) {
    const a = fs.readFileSync('AppData/extensions/_builtin/fairy_ball/' + f)
    const b = fs.readFileSync('AppData/extensions/fairy_ball/' + f)
    if (a.equals(b)) ok('同步一致 ' + f); else bad('同步不一致 ' + f)
}

console.log(fail ? 'RESULT: FAIL ×' + fail : 'RESULT: ALL PASS')
process.exit(fail ? 1 : 0)