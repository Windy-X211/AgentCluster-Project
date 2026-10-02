"""browser_control 插件 —— Playwright 驱动的统一浏览器控制

支持两种模式（通过 mode 参数选择）：
  1. system  — 系统已安装的 Edge/Chrome（channel 方式启动，有独立 context，cookie 持久化到磁盘）
  2. cdp     — 桥接到正在运行的 Edge/Chrome（connect_over_cdp，共享用户的登录态、cookies、扩展）
  3. chromium — Playwright 内置 Chromium（最干净的沙箱，无头/有头都可）
  4. auto    — 自动选择（优先级 cdp > system > chromium）

CDP 模式用法：
  用户先在命令行启动带远程调试端口的浏览器：
    msedge --remote-debugging-port=9222
    chrome --remote-debugging-port=9222
  然后 agent 调用 browser_control(action="navigate", url="...", mode="cdp")

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="navigate", url="...", selector="...", text="...",
        js="...", value=1, timeout=15, visible=True,
        mode="auto", cdp_endpoint="http://localhost:9222")

设计要点：
  - 所有 Playwright 操作固定在 **同一个 worker 线程** 里执行。
  - 主入口 run() 把请求塞进队列，等 worker 线程回结果。
  - 这样即使 run() 从 FastAPI/agent_runtime 的 async 事件循环里被调用，
    Playwright sync API 也不会因为检测到 loop 而抛 "Please use Async API"。
  - 切换 mode 时会自动关掉旧浏览器、用新模式重启。
"""
from __future__ import annotations

import base64
import io
import json as _json
import sys
import threading
import queue as _queue
import time as _time
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

try:
    from playwright.sync_api import sync_playwright, Page, TimeoutError as _PWTimeout
    _PW_OK = True
except ImportError:
    _PW_OK = False


# ==============================================================
# 专用 worker 线程 —— 所有 Playwright 对象都在这里创建/使用
# ==============================================================
class _BrowserWorker:
    """单例 worker：浏览器、page 全在这个线程里。外面只通过 submit() 调它。"""

    _INST: "_BrowserWorker | None" = None
    _INST_LOCK = threading.Lock()

    def __init__(self) -> None:
        self._q: _queue.Queue[tuple[dict, _queue.Queue]] = _queue.Queue()
        self._thread = threading.Thread(target=self._run, name="browser-worker", daemon=True)
        self._thread.start()

    @classmethod
    def instance(cls) -> "_BrowserWorker":
        if cls._INST is None:
            with cls._INST_LOCK:
                if cls._INST is None:
                    cls._INST = cls()
        return cls._INST

    # —— 给外部用：提交一个 request dict，拿到结果 dict ——
    def submit(self, req: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        if not _PW_OK:
            return {"ok": False, "data": "", "error": "playwright 未安装，请 pip install playwright && playwright install chromium",
                    "current_url": ""}
        out: _queue.Queue = _queue.Queue(maxsize=1)
        self._q.put((req, out))
        try:
            return out.get(timeout=timeout)
        except _queue.Empty:
            return {"ok": False, "data": "", "error": f"browser worker 超时 ({timeout}s)", "current_url": ""}

    # —— worker 主循环 ——
    def _run(self) -> None:
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            self._startup_error = f"Playwright start failed: {e}"
            self._loop_fail()
            return
        self._startup_error = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._visible: bool = False
        self._mode: str = ""
        self._cdp_endpoint: str = ""
        self._page_ws_url: str | None = None  # CDP 模式下保存 page 的 websocket 地址
        # 处理请求
        while True:
            try:
                req, out = self._q.get()
                res = self._handle(req)
                out.put(res)
            except Exception as e:
                try:
                    out.put({"ok": False, "data": "", "error": f"{type(e).__name__}: {e}", "current_url": self.current_url()})
                except Exception:
                    pass

    def _loop_fail(self) -> None:
        while True:
            req, out = self._q.get()
            out.put({"ok": False, "data": "", "error": self._startup_error or "browser worker 未启动", "current_url": ""})

    def current_url(self) -> str:
        try:
            return self._page.url if (self._page is not None and not self._page.is_closed()) else ""
        except Exception:
            return ""

    # ==============================================================
    # 模式检测与选择
    # ==============================================================
    @staticmethod
    def _detect_system_browser() -> str | None:
        """检测系统已安装的 Edge/Chrome，返回 Playwright channel 名。"""
        candidates: list[tuple[str, Path]] = [
            ("msedge", Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
            ("msedge", Path(r"C:/Program Files/Microsoft/Edge/Application/msedge.exe")),
            ("chrome", Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe")),
            ("chrome", Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")),
        ]
        for channel, exe in candidates:
            if exe.is_file():
                return channel
        return None

    @staticmethod
    def _probe_cdp(endpoint: str) -> bool:
        """探测 CDP 端点是否可达（访问 /json/version 或 /json 任一成功即可）。"""
        import urllib.request
        for path in ("/json/version", "/json"):
            try:
                url = endpoint.rstrip("/") + path
                req = urllib.request.Request(url)
                # timeout 必须传给 urlopen，不是 Request！(Windows + IPv6 坑)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    def _resolve_mode(self, requested: str, cdp_endpoint: str) -> tuple[str, str]:
        """解析最终使用的 mode 和 cdp_endpoint。auto 会根据可用性自动选。"""
        req = (requested or "auto").strip().lower()
        ep = self._normalize_cdp_endpoint(cdp_endpoint)

        if req == "auto":
            # 先试 CDP（如果有端点）
            if ep and self._probe_cdp(ep):
                return "cdp", ep
            # 再试系统浏览器
            if self._detect_system_browser():
                return "system", ep
            # 最后用内置 Chromium
            return "chromium", ep

        if req == "cdp":
            return "cdp", ep
        if req == "system":
            return "system", ep
        if req == "chromium":
            return "chromium", ep
        # 兜底
        return "chromium", ep

    @staticmethod
    def _normalize_cdp_endpoint(endpoint: str) -> str:
        """把 localhost 规范化为 127.0.0.1，规避 Windows IPv6(::1) 解析坑。"""
        ep = (endpoint or "").strip()
        if not ep:
            return "http://127.0.0.1:9222"
        # localhost → 127.0.0.1
        ep = ep.replace("localhost", "127.0.0.1")
        # 补全 scheme
        if not ep.startswith(("http://", "https://")):
            ep = "http://" + ep
        return ep.rstrip("/")

    # ==============================================================
    # storage_state 持久化（system/chromium 模式用）
    # ==============================================================
    @staticmethod
    def _state_file() -> Path:
        return Path.home() / "AppData" / "Local" / "Temp" / "agentcluster_browser_state.json"

    def _save_state(self) -> None:
        if self._context is not None and self._mode != "cdp":
            try:
                self._context.storage_state(path=str(self._state_file()))
            except Exception:
                pass

    # ==============================================================
    # CDP 原生 screenshot —— 用 Playwright 内部 CDP session 绕过 fonts 等待
    # ==============================================================
    def _cdp_screenshot_direct(self) -> bytes | None:
        """在 CDP 模式下，通过 Playwright 内置的 CDP session 调 Page.captureScreenshot。
        不受浏览器的 --remote-allow-origins 限制（Playwright 内部已处理）。
        返回 PNG bytes；失败返回 None。"""
        if self._browser is None:
            return None
        try:
            # Playwright 内置 CDP session，不受 origin 限制
            session = self._browser.new_browser_cdp_session()
            # 需要用当前活跃 page 的 target，用 Page.captureScreenshot 的 pageId
            # 但 Playwright 的 cdp session 已经绑定到 browser，发 Page 命令会作用到默认 page
            result = session.send("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": False,
            })
            session.detach()
            if result and "data" in result:
                return base64.b64decode(result["data"])
        except Exception:
            pass
        return None

    # ==============================================================
    # 生命周期
    # ==============================================================
    def _ensure_page(self, visible: bool, mode: str, cdp_endpoint: str) -> Page:
        need_restart = False

        # 模式变了 → 必须重启
        if mode != self._mode or cdp_endpoint != self._cdp_endpoint:
            need_restart = True
        # visible 变了 → system/chromium 需要重启（cdp 模式忽略 visible）
        elif self._mode != "cdp" and self._page is not None and not self._page.is_closed():
            if self._visible != bool(visible):
                need_restart = True

        if need_restart and self._page is not None:
            self._close()

        if self._page is None or self._page.is_closed():
            self._start(visible=visible, mode=mode, cdp_endpoint=cdp_endpoint)

        assert self._page is not None
        return self._page

    def _start(self, visible: bool, mode: str, cdp_endpoint: str) -> None:
        self._visible = bool(visible)
        self._mode = mode
        self._cdp_endpoint = cdp_endpoint

        if mode == "cdp":
            self._start_cdp(cdp_endpoint)
        elif mode == "system":
            self._start_system(self._visible)
        else:  # chromium
            self._start_chromium(self._visible)

    def _start_cdp(self, endpoint: str) -> None:
        """通过 CDP 连接到用户正在运行的浏览器。"""
        import socket, urllib.request as ur
        norm_ep = self._normalize_cdp_endpoint(endpoint)
        parsed = ur.urlparse(norm_ep)
        host, port = parsed.hostname, parsed.port or 9222

        # Step 1: TCP 端口检测
        tcp_ok = False
        try:
            with socket.create_connection((host, port), timeout=2):
                tcp_ok = True
        except OSError:
            tcp_ok = False

        # Step 2: HTTP /json 端点有效性
        http_ok = False
        try:
            resp = ur.urlopen(norm_ep.rstrip("/") + "/json", timeout=3)
            http_ok = resp.status == 200
        except Exception:
            http_ok = False

        if not tcp_ok:
            raise RuntimeError(
                f"CDP 端口 {host}:{port} 未监听。请先启动浏览器并加 --remote-debugging-port 参数：\n"
                f'  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=*\n'
                f'  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=*\n'
                f"注意：必须关掉所有已有浏览器实例再启动，否则新参数不生效。"
            )
        if tcp_ok and not http_ok:
            raise RuntimeError(
                f"CDP 端口 {host}:{port} 有响应但不是有效的 CDP 端点。\n"
                f"可能被其他程序占用，或浏览器启动时未加 --remote-debugging-port。"
            )

        # Step 3: 保存 page 的 websocket URL（供 CDP 底层命令如 screenshot 使用）
        try:
            targets = json.loads(ur.urlopen(norm_ep.rstrip("/") + "/json/list", timeout=3).read())
            page_target = next((t for t in targets if t.get("type") == "page"), None)
            if page_target:
                self._page_ws_url = page_target["webSocketDebuggerUrl"]
            else:
                self._page_ws_url = None
        except Exception:
            self._page_ws_url = None

        # Step 4: Playwright 连接（它内部会正确处理 origin 检查）
        try:
            self._browser = self._pw.chromium.connect_over_cdp(norm_ep)
        except Exception as e:
            raise RuntimeError(f"connect_over_cdp 失败: {e}")

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _start_system(self, visible: bool) -> None:
        """用系统 Edge/Chrome 的 channel 模式启动（有独立 context + storage_state）。"""
        channel = self._detect_system_browser()
        launch_kwargs: dict[str, Any] = {"headless": not visible}
        if channel:
            launch_kwargs["channel"] = channel
        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception as e:
            if channel:
                try:
                    self._browser = self._pw.chromium.launch(headless=not visible)
                except Exception:
                    raise RuntimeError(f"Playwright 无法启动浏览器 (channel={channel})。\n{e}")
            else:
                raise RuntimeError(f"Playwright 无法启动浏览器。请先跑: playwright install chromium\n{e}")

        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        state_file = self._state_file()
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        self._context = self._browser.new_context(**ctx_kwargs)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _start_chromium(self, visible: bool) -> None:
        """用 Playwright 内置 Chromium 启动。"""
        try:
            self._browser = self._pw.chromium.launch(headless=not visible)
        except Exception as e:
            raise RuntimeError(f"Playwright 无法启动 Chromium。请先跑: playwright install chromium\n{e}")

        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        state_file = self._state_file()
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        self._context = self._browser.new_context(**ctx_kwargs)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _close(self) -> None:
        self._save_state()
        # cdp 模式下不能关闭浏览器（那是用户正在用的），只断开 connection
        is_cdp = self._mode == "cdp"
        try:
            if self._page is not None and not self._page.is_closed():
                if not is_cdp:
                    self._page.close()
        except Exception:
            pass
        self._page = None
        try:
            if self._context is not None and not is_cdp:
                self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if hasattr(self, "_browser") and self._browser is not None:
                # cdp: disconnect；其他: close
                if is_cdp:
                    self._browser.close()  # disconnect_over_cdp 没有单独方法，close 等价于断开
                else:
                    self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._mode = ""
        self._cdp_endpoint = ""

    # ==============================================================
    # 请求分发（worker 线程内执行）
    # ==============================================================
    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        action = (req.get("action") or "").strip().lower()
        if not action:
            return {"ok": False, "data": "", "error": "缺少 action 参数", "current_url": self.current_url()}

        # resolve mode
        requested_mode = (req.get("mode") or self._mode or "auto").strip().lower()
        cdp_endpoint = (req.get("cdp_endpoint") or self._cdp_endpoint or "http://localhost:9222").strip()
        final_mode, final_ep = self._resolve_mode(requested_mode, cdp_endpoint)

        if action == "close":
            try:
                self._close()
                return {"ok": True, "data": "浏览器已关闭", "current_url": ""}
            except Exception as e:
                return {"ok": False, "data": "", "error": f"close 失败: {e}", "current_url": self.current_url()}

        timeout_i = max(1, int(float(req.get("timeout") or 15)))
        visible = bool(req.get("visible"))
        try:
            page = self._ensure_page(visible=visible, mode=final_mode, cdp_endpoint=final_ep)
        except Exception as e:
            return {"ok": False, "data": "", "error": f"启动浏览器失败({final_mode}): {e}", "current_url": self.current_url()}

        try:
            if action == "navigate":
                url = req.get("url") or ""
                if not url:
                    return {"ok": False, "data": "", "error": "navigate 需要 url", "current_url": self.current_url()}
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_i * 1000)
                page.wait_for_timeout(500)
                return {"ok": True, "data": f"已打开 {page.url}", "current_url": page.url}

            if action == "click":
                selector = req.get("selector") or ""
                if not selector:
                    return {"ok": False, "data": "", "error": "click 需要 selector", "current_url": self.current_url()}
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout_i * 1000)
                loc.click(timeout=timeout_i * 1000)
                page.wait_for_timeout(300)
                return {"ok": True, "data": f"已点击 {selector}", "current_url": self.current_url()}

            if action == "type":
                selector = req.get("selector") or ""
                text = req.get("text") or ""
                if not selector:
                    return {"ok": False, "data": "", "error": "type 需要 selector", "current_url": self.current_url()}
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout_i * 1000)
                loc.fill("")
                loc.type(text, delay=30)
                return {"ok": True, "data": f"已在 {selector} 输入 {len(text)} 字符", "current_url": self.current_url()}

            if action == "get_text":
                selector = req.get("selector") or ""
                if selector:
                    loc = page.locator(selector).first
                    loc.wait_for(state="visible", timeout=timeout_i * 1000)
                    text = loc.inner_text(timeout=timeout_i * 1000)
                else:
                    text = page.inner_text("body")
                return {"ok": True, "data": (text or "")[:20000], "current_url": self.current_url()}

            if action == "get_html":
                selector = req.get("selector") or ""
                if selector:
                    loc = page.locator(selector).first
                    loc.wait_for(state="visible", timeout=timeout_i * 1000)
                    html = loc.evaluate("(el) => el.outerHTML")
                else:
                    html = page.content()
                return {"ok": True, "data": (html or "")[:30000], "current_url": self.current_url()}

            if action == "screenshot":
                url = req.get("url") or ""
                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_i * 1000)
                    page.wait_for_timeout(500)

                purpose = (req.get("purpose") or "inspect").strip().lower()

                # —— 截图策略 ——
                # CDP 模式下 Playwright screenshot 会卡住（内部等 fonts），
                # 优先用原生 websocket 调 CDP Page.captureScreenshot 绕过它。
                png_bytes: bytes | None = None

                if self._mode == "cdp" and self._page_ws_url:
                    png_bytes = self._cdp_screenshot_direct()

                if png_bytes is None:
                    # Playwright 兜底
                    try:
                        png_bytes = page.screenshot(
                            type="png", full_page=False,
                            animations="disabled", timeout=10000,
                        )
                    except Exception:
                        png_bytes = page.screenshot(type="png", full_page=False, timeout=30000)

                b64 = base64.b64encode(png_bytes).decode("ascii")

                res: dict[str, Any] = {
                    "ok": True,
                    "data": b64,
                    "content_type": "image/png",
                    "current_url": self.current_url(),
                }

                if purpose in ("share", "user", "save"):
                    # —— share 模式：保存到工作区 screenshots/ 目录，给用户看 ——
                    shot_dir = Path.cwd() / "screenshots"
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    shot_path = shot_dir / f"shot_{_time.strftime('%Y%m%d_%H%M%S')}_{int(_time.time()*1000)%1000:03d}.png"
                    try:
                        shot_path.write_bytes(png_bytes)
                        res["file"] = str(shot_path)
                        res["purpose"] = "share"
                        res["_note"] = "截图已保存到工作区 screenshots/ 目录，可供用户查看。"
                    except Exception as e:
                        shot_dir = Path.home() / "AppData" / "Local" / "Temp" / "agentcluster_screenshots"
                        shot_dir.mkdir(parents=True, exist_ok=True)
                        shot_path = shot_dir / f"shot_{_time.strftime('%Y%m%d_%H%M%S')}_{int(_time.time()*1000)%1000:03d}.png"
                        try:
                            shot_path.write_bytes(png_bytes)
                            res["file"] = str(shot_path)
                            res["purpose"] = "share"
                            res["_note"] = f"工作区无法写入({e})，降级保存到临时目录。"
                        except Exception:
                            res["purpose"] = "inspect"
                            res["_note"] = "截图未保存（磁盘写入失败），仅返回给智能体内部判断。"
                else:
                    # —— inspect 模式（默认）：不落盘，只给智能体内部看图 ——
                    res["purpose"] = "inspect"
                    res["_note"] = "仅供智能体内部判断页面状况，未保存到磁盘。"

                return res

            if action == "evaluate":
                code = ((req.get("js") or "") or (req.get("text") or "")).strip()
                if not code:
                    return {"ok": False, "data": "", "error": "evaluate 需要 js", "current_url": self.current_url()}
                wrapped = (
                    "(async () => {"
                    "  try {"
                    "    const __r = await (async function() {" + code + "})();"
                    "    return { __result: __r === undefined ? null : __r };"
                    "  } catch(e) { return { __error: String(e) }; }"
                    "})()"
                )
                res = page.evaluate(wrapped)
                if isinstance(res, dict) and "__error" in res:
                    return {"ok": False, "data": "", "error": f"JS 执行错误: {res['__error']}", "current_url": self.current_url()}
                try:
                    out_data = _json.dumps(res.get("__result"), ensure_ascii=False)[:20000]
                except Exception:
                    out_data = str(res)[:20000]
                return {"ok": True, "data": out_data, "current_url": self.current_url()}

            if action == "wait":
                ms = max(0, float(req.get("value") or 0)) * 1000
                page.wait_for_timeout(int(ms))
                return {"ok": True, "data": f"已等待 {ms:.0f}ms", "current_url": self.current_url()}

            if action == "info":
                """返回当前浏览器状态：mode / cdp_endpoint / url / page_count。"""
                pages = self._context.pages if self._context else []
                return {
                    "ok": True,
                    "mode": final_mode,
                    "cdp_endpoint": final_ep if final_mode == "cdp" else "",
                    "visible": self._visible,
                    "current_url": self.current_url(),
                    "page_count": len(pages),
                    "data": f"mode={final_mode} url={self.current_url()} pages={len(pages)}",
                }

            return {"ok": False, "data": "", "error": f"未知 action: {action}", "current_url": self.current_url()}

        except _PWTimeout as e:
            return {"ok": False, "data": "", "error": f"操作超时: {e}", "current_url": self.current_url()}
        except Exception as e:
            return {"ok": False, "data": "", "error": f"{type(e).__name__}: {e}", "current_url": self.current_url()}


# ==============================================================
# run() —— 插件入口（线程安全、asyncio-loop 无关）
# ==============================================================
def run(action: str = "", url: str = "", selector: str = "",
        text: str = "", js: str = "", value: float | int = 1,
        timeout: float | int = 15, visible: bool = True,
        mode: str = "auto", cdp_endpoint: str = "",
        purpose: str = "",
        **_extra: Any) -> dict[str, Any]:
    """供 LLM 调用的主入口。始终在线程中执行，不依赖当前是否有 asyncio loop。"""
    # cdp_endpoint 为空时由 _normalize_cdp_endpoint 兜底为 127.0.0.1:9222
    req = {
        "action": action, "url": url, "selector": selector,
        "text": text, "js": js, "value": value,
        "timeout": timeout, "visible": visible,
        "mode": mode, "cdp_endpoint": cdp_endpoint,
        "purpose": purpose,
    }
    return _BrowserWorker.instance().submit(req, timeout=max(5, int(float(timeout or 15)) + 30))
