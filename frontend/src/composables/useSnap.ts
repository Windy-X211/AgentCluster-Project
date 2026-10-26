/**
 * Aero Snap 边缘吸附 / 窗口拖动区域
 *
 * Electron 无边框窗口下，把元素设为 -webkit-app-region: drag
 * 即可获得原生窗口拖动 + Windows Aero Snap（拖到屏幕边缘自动最大化/半屏）。
 * pywebview(WebView2) 下同样支持该 CSS 属性。
 */
export function useSnap() {
  function attachDragRegion(el: HTMLElement | null) {
    if (!el) return
    const style = el.style as any
    style.webkitAppRegion = 'drag'
    style.appRegion = 'drag'
    el.setAttribute('data-drag-region', '1')
  }

  return { attachDragRegion }
}