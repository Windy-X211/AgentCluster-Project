"""
独立窗口 — 用 pywebview 包装前端 5173 为桌面独立窗口（Windows 上用 WebView2/Edge 内核）。

窗口交互实现（基于 pywebview 源码 customize.js + util.py）：
- 拖动：easy_drag=False + DRAG_REGION_SELECTOR='.pywebview-drag-region'
  pywebview 在 body 级注册 onBodyMouseDown（customize.js 第 89 行，始终注册），
  遍历 event.target 祖先链，匹配 selector 就用 window.move() 相对移动窗口。
  按钮/链接不加该类 → 祖先链不匹配 → 正常点击不触发拖动。
- 边缘缩放：前端 ResizeHandles mousedown → Python PostMessageW(WM_NCLBUTTONDOWN, HT*, 屏幕坐标)
  PostMessageW 异步投递到窗口消息队列（UI 线程），不依赖 ReleaseCapture 的线程 capture。
  SendMessageW + ReleaseCapture 方案失败根因：ReleaseCapture 只释放当前线程 capture，
  但 WebView2 鼠标捕获在 UI 线程，JS API 调在 worker 线程 → ReleaseCapture 是空操作。
- DWM：DwmSetWindowAttribute(DWMWCP_ROUND) 实现 Win11 圆角
"""
import sys, time, socket, argparse, os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "http://localhost:5173/#/"
LOG_FILE = ROOT / "app_window.log"


def _log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _window_already_running() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.GetLastError.restype = wintypes.DWORD
        k32.CreateMutexW(None, False, "Local\\AgentCluster_Window_SingleInstance")
        return k32.GetLastError() == 183
    except Exception:
        return False


def _wait_frontend(url: str, timeout: float = 30.0) -> bool:
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port or (443 if p.scheme == "https" else 80)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            try: s.close()
            except Exception: pass
        time.sleep(0.4)
    return False


def _mark_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q.setdefault('app', 'pywebview')
    return urlunparse(p._replace(query=urlencode(q)))


def _safe_remove(p: Path):
    try: p.unlink()
    except Exception: pass


class _WindowApi:
    """暴露给前端 JS 的窗口控制 API（window.pywebview.api.xxx）。"""

    def _w(self):
        import webview
        return webview.windows[0] if webview.windows else None

    def get_bounds(self):
        """返回当前窗口边界 {x, y, width, height}。"""
        w = self._w()
        if not w: return {}
        return {'x': w.x, 'y': w.y, 'width': w.width, 'height': w.height}

    def get_work_area(self):
        """返回屏幕工作区（排除任务栏）{x, y, width, height}。"""
        import ctypes
        from ctypes import wintypes
        SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        user32 = ctypes.windll.user32
        user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        user32.SystemParametersInfoW.restype = wintypes.BOOL
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return {
                'x': rect.left, 'y': rect.top,
                'width': rect.right - rect.left,
                'height': rect.bottom - rect.top,
            }
        return {'x': 0, 'y': 0, 'width': 1920, 'height': 1080}

    def resize(self, width, height):
        """手动设置窗口尺寸（JS 缩放在 mousemove 时持续调用）。"""
        w = self._w()
        if w and width > 0 and height > 0:
            w.resize(int(width), int(height))

    def set_bounds(self, x, y, width, height):
        """单次 SetWindowPos 原子完成移动+缩放（缩放拖动专用，避免每帧两次桥调用引发白闪/卡顿）。"""
        w = self._w()
        if not w or width <= 0 or height <= 0:
            return
        try:
            if os.name != "nt":
                w.move(int(x), int(y)); w.resize(int(width), int(height)); return
            import ctypes
            from ctypes import wintypes
            hwnd = w.native.Handle.ToInt32()
            SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
            ctypes.windll.user32.SetWindowPos(
                wintypes.HWND(hwnd), 0,
                int(x), int(y), int(width), int(height),
                SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:
            w.move(int(x), int(y)); w.resize(int(width), int(height))

    def move(self, x, y):
        """手动设置窗口位置（左/上边缘缩放时需要同时移动左上角）。"""
        w = self._w()
        if w:
            w.move(int(x), int(y))

    def minimize(self):
        w = self._w(); w.minimize() if w else None
    def maximize(self):
        w = self._w(); w.maximize() if w else None
    def restore(self):
        w = self._w(); w.restore() if w else None
    def close(self):
        w = self._w(); w.destroy() if w else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--title", default="智能体集群")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--min-width", type=int, default=960)
    parser.add_argument("--min-height", type=int, default=600)
    parser.add_argument("--wait", type=float, default=30.0)
    parser.add_argument("--icon", default=str(ROOT / "agentcluster.ico"))
    args = parser.parse_args()
    url = _mark_url(args.url)

    _log(f"启动: url={url} title={args.title}")

    if _window_already_running():
        _log("已有独立窗口在运行，退出")
        sys.exit(0)

    pid_file = ROOT / "app_window.pid"
    try: pid_file.write_text(str(os.getpid()))
    except Exception: pass

    if not _wait_frontend(url, timeout=args.wait):
        _log(f"前端 {url} 等待 {args.wait}s 未就绪，退出")
        _safe_remove(pid_file)
        sys.exit(2)
    _log("前端已就绪，创建窗口")

    try:
        import webview

        # pywebview 原生区域拖动机制（customize.js 第 89 行 onBodyMouseDown 始终注册）：
        # body 级 mousedown 监听 → 遍历 event.target 祖先链 → 匹配 selector 时用 window.move() 拖动
        # easy_drag=False：不注册 window 级 onMouseDown（不让整个窗口都可拖）
        # 只让带 .pywebview-drag-region 类的元素触发拖动
        webview.settings['DRAG_REGION_SELECTOR'] = '.pywebview-drag-region'

        def _on_window_ready():
            """窗口首次显示后设置 DWM 圆角（Win11+）。"""
            if os.name != "nt": return
            try:
                import ctypes
                from ctypes import wintypes
                hwnd = webview.windows[0].native.Handle.ToInt64()
                if not hwnd: return
                DwmSetWindowAttribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
                DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.POINTER(ctypes.c_int), wintypes.DWORD]
                DwmSetWindowAttribute.restype = ctypes.c_long  # Python 3.13 无 HRESULT
                attr = ctypes.c_int(2)  # DWMWCP_ROUND
                res = DwmSetWindowAttribute(hwnd, 33, ctypes.byref(attr), ctypes.sizeof(attr))
                _log(f"DWM ROUND=0x{res & 0xFFFFFFFF:08x}")
            except Exception as e:
                _log(f"DWM 异常: {e}")

        # 防白闪：WebView2 DefaultBackgroundColor / WinForms BackColor 与页面 --bg-base 一致，
        # 页面加载中与 resize 重绘未完成区域露深色而非默认白色
        window = webview.create_window(
            args.title, url,
            width=args.width, height=args.height,
            min_size=(args.min_width, args.min_height),
            confirm_close=False,
            frameless=True,
            easy_drag=False,   # 关键：不让整个 window 都有 mousedown 监听
            shadow=True,
            resizable=True,
            background_color="#0a0e1a",
            js_api=_WindowApi(),
        )

        if window is not None:
            window.events.loaded += _on_window_ready
            _log("已注册 window.events.loaded 回调")
        else:
            _log("create_window 返回 None")

        icon_path = args.icon if args.icon and Path(args.icon).exists() else None
        storage_path = str(ROOT / ".webview_data")
        _log(f"webview.start icon={icon_path} private_mode=False storage_path={storage_path}")
        webview.start(icon=icon_path, private_mode=False, storage_path=storage_path)
        _log("窗口已关闭，退出")
    except Exception as e:
        _log(f"异常: {e}")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
    finally:
        _safe_remove(pid_file)


if __name__ == "__main__":
    main()
