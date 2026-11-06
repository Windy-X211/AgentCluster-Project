// 临时诊断脚本：打印显示器 DIP/物理尺寸与 desktopCapturer 缩略图实际尺寸
const { app, screen, desktopCapturer } = require('electron')

app.whenReady().then(async () => {
    const out = { displays: [], sources: [] }
    for (const d of screen.getAllDisplays()) {
        out.displays.push({
            id: d.id,
            scaleFactor: d.scaleFactor,
            size: d.size,
            bounds: d.bounds,
            physicalGuess: { w: Math.round(d.size.width * d.scaleFactor), h: Math.round(d.size.height * d.scaleFactor) },
            workAreaSize: d.workAreaSize,
        })
    }
    const primary = screen.getPrimaryDisplay()
    const thumbW = Math.max(1, Math.round(primary.size.width * primary.scaleFactor))
    const thumbH = Math.max(1, Math.round(primary.size.height * primary.scaleFactor))
    try {
        const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: thumbW, height: thumbH } })
        for (const s of sources) {
            const sz = s.thumbnail.getSize()
            out.sources.push({ id: s.id, display_id: s.display_id, requested: { w: thumbW, h: thumbH }, actual: sz, ratioReq: +(thumbW / thumbH).toFixed(5), ratioAct: +(sz.width / sz.height).toFixed(5) })
        }
    } catch (e) {
        out.error = String(e && e.message || e)
    }
    console.log('PROBE_RESULT ' + JSON.stringify(out))
    app.quit()
})