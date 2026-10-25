"""
一键启动器 — 智能体集群 (AgentCluster)
- 启动后端 FastAPI (BACKEND_PORT)
- 启动前端 Vite (localhost:5173)
- 系统托盘图标 + 状态菜单 + 一键打开浏览器/停止服务
- 幂等：已运行就不重复拉起
"""
import os, sys, time, socket, threading, webbrowser, subprocess, http.server, shutil
from pathlib import Path
from PIL import Image, ImageDraw
import pystray

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def _detect_node_dir() -> Path | None:
    """定位 Node 目录：优先项目内置，其次系统 PATH 中的 node。找不到返回 None。"""
    for candidate in (ROOT / "node-v20.18.0-win-x64", ROOT / "node"):
        if (candidate / "node.exe").exists():
            return candidate
    found = shutil.which("node")
    return Path(found).resolve().parent if found else None


NODE_DIR = _detect_node_dir()
NODE_BIN = (NODE_DIR / "node.exe") if NODE_DIR else (ROOT / "node-v20.18.0-win-x64" / "node.exe")
PID_FILE = ROOT / ".agentcluster.pid"
# 启动状态文件：启动中窗口(launch_splash.py)轮询读取，显示当前启动步骤
STATUS_FILE = ROOT / ".startup_status"

BACKEND_PORT = int(os.getenv("PORT", "8870"))
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = "http://localhost:5173"

# Windows 子进程创建flags：完全独立 + 无黑框
WIN_FLAGS = (subprocess.CREATE_NO_WINDOW | 0x00000008 | 0x00000200) if os.name == "nt" else 0

backend_proc = None
frontend_proc = None
icon: pystray.Icon | None = None
status_text = "正在启动..."
# 本托盘进程是否已自动打开过浏览器：仅首次启动成功时自动打开一次，
# 后续的“重启全部 / 启动服务 / 重复拉起”都不再 webbrowser.open，
# 避免浏览器最小化后被反复顶起（见 __main__ 中 _already_running 的说明）。
_auto_opened_browser = False
# 启动中窗口进程句柄：每次 启动/重启 都会重新拉起一个；上一个还在运行则不重复开
_splash_proc: subprocess.Popen | None = None
_app_window_proc: subprocess.Popen | None = None


# ---------- 工具函数 ----------

def kill_port(port: int) -> bool:
    """按 netstat 找到占用端口的进程，taskkill 杀掉（尽力而为）。
    CREATE_NO_WINDOW：netstat/taskkill 是控制台程序，不加会在启动时闪黑框。"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        pids = set()
        for line in out.splitlines():
            # 匹配 LISTENING + 精确端口（避免 :5173 误匹配 :51730）
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[1]
            if local.rsplit(":", 1)[-1] == str(port):
                pid = parts[-1]
                if pid.isdigit(): pids.add(int(pid))
        for pid in pids:
            if pid == os.getpid(): continue  # 跳过自己
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        time.sleep(0.8)
    except Exception: pass
    return not port_in_use(port)


def port_in_use(port: int, host: str | None = None) -> bool:
    """探测端口是否在监听。默认同时测 127.0.0.1 与 ::1（Vite 可能只绑 IPv6）。"""
    hosts = [host] if host else ["127.0.0.1", "::1"]
    for h in hosts:
        family = socket.AF_INET6 if ":" in h else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect((h, port))
            s.close()
            return True
        except Exception:
            try: s.close()
            except Exception: pass
    return False


def wait_port(port: int, timeout: float = 15.0, poll: float = 0.5) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_in_use(port): return True
        time.sleep(poll)
    return False


def check_health() -> dict:
    """轻量健康检查，返回后端/前端运行状态（禁用系统代理，避免误判）"""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result = {"backend": False, "frontend": False, "pidfile": PID_FILE.exists()}
    try:
        opener.open(BACKEND_URL + "/api/health", timeout=1.5).read(); result["backend"] = True
    except Exception: pass
    try:
        opener.open(FRONTEND_URL, timeout=1.5).read(); result["frontend"] = True
    except Exception: pass
    return result


def is_running() -> bool:
    h = check_health()
    return h["backend"] and h["frontend"]


# ---------- 启动中窗口（特效加载动画） ----------

def write_status(text: str):
    """把当前启动步骤写入状态文件，供启动中窗口实时显示。

    约定：普通步骤写 "step|<文案>"；全部结束写 "done|<文案>"（窗口收到 done 后收尾退出）。
    """
    try:
        STATUS_FILE.write_text(text, encoding="utf-8")
    except Exception:
        pass


def clear_status_file():
    """删除状态文件：启动中窗口发现文件消失即自动退出（用于托盘退出/停止服务）。"""
    try:
        STATUS_FILE.unlink()
    except Exception:
        pass


def _splash_python() -> str:
    """启动中窗口用无控制台的 pythonw 运行（避免自身闪黑框）。"""
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _ensure_splash():
    """拉起启动中窗口：每次 启动/重启 都会显示；上一个还在运行则不重复开。"""
    global _splash_proc
    if _splash_proc is not None and _splash_proc.poll() is None:
        return
    try:
        _splash_proc = subprocess.Popen(
            [_splash_python(), str(ROOT / "launch_splash.py")],
            cwd=str(ROOT),
            creationflags=WIN_FLAGS,
            close_fds=True,
        )
    except Exception:
        _splash_proc = None


def _open_app_window():
    """拉起独立窗口（pywebview 包装前端 5173）。独立进程跑，避免与 pystray 事件循环冲突。
    已在运行则不重复开。"""
    global _app_window_proc
    if _app_window_proc is not None and _app_window_proc.poll() is None:
        return
    try:
        _app_window_proc = subprocess.Popen(
            [_splash_python(), str(ROOT / "app_window.py")],
            cwd=str(ROOT),
            creationflags=WIN_FLAGS,
            close_fds=True,
        )
    except Exception as e:
        print(f"[启动器] 独立窗口拉起失败: {e}")
        _app_window_proc = None


def _kill_app_window():
    """通过 PID 文件杀掉独立窗口进程（托盘退出/停服务时清理）。
    覆盖单例命中路径拉起的窗口（_app_window_proc 为 None 的情况）。"""
    try:
        pid_file = ROOT / "app_window.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           timeout=3, capture_output=True,
                           creationflags=WIN_FLAGS)
            try: pid_file.unlink()
            except Exception: pass
    except Exception as e:
        print(f"[启动器] 清理独立窗口异常: {e}")


# ---------- 启动/停止 ----------

def start_backend():
    global backend_proc
    if port_in_use(BACKEND_PORT):
        write_status("step|检测到旧后端进程，正在清理...")
        print(f"[启动器] {BACKEND_PORT} 被占用，尝试清理...")
        kill_port(BACKEND_PORT)
    if port_in_use(BACKEND_PORT):
        print(f"[启动器] 后端端口 {BACKEND_PORT} 无法清理（可能需管理员权限），跳过启动")
        return False
    write_status("step|正在启动后端服务...")
    python = sys.executable
    backend_proc = subprocess.Popen(
        [python, "-u", "main.py"],
        cwd=str(BACKEND),
        stdout=open(BACKEND / "backend.log", "a", encoding="utf-8", buffering=1),
        stderr=subprocess.STDOUT,
        creationflags=WIN_FLAGS,
    )
    print(f"[启动器] 后端 PID={backend_proc.pid}，等待端口 {BACKEND_PORT}...")
    ok = wait_port(BACKEND_PORT, timeout=20)
    if ok:
        write_status("step|后端服务已就绪")
    else:
        print("[启动器] 后端启动超时，详见 backend/backend.log")
    return ok


def start_frontend():
    global frontend_proc
    if port_in_use(5173):
        write_status("step|检测到旧前端进程，正在清理...")
        print("[启动器] 5173 被占用，尝试清理...")
        kill_port(5173)
    if port_in_use(5173):
        print("[启动器] 前端端口 5173 无法清理，跳过启动")
        return False
    write_status("step|正在启动前端服务...")
    node_dir = str(NODE_DIR) if NODE_DIR else ""
    env = os.environ.copy()
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    # 尝试 vite 主入口（node_modules/vite/bin/vite.js），找不到再 fallback
    vite_js = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    vite_bin = FRONTEND / "node_modules" / ".bin" / "vite"
    if NODE_BIN.exists() and vite_js.exists():
        frontend_proc = subprocess.Popen(
            [str(NODE_BIN), str(vite_js), "--port", "5173", "--strictPort", "--host", "127.0.0.1"],
            cwd=str(FRONTEND), env=env,
            stdout=open(FRONTEND / "frontend.log", "a", encoding="utf-8", buffering=1),
            stderr=subprocess.STDOUT, creationflags=WIN_FLAGS,
        )
    elif vite_bin.exists():
        frontend_proc = subprocess.Popen(
            ["cmd", "/c", str(vite_bin), "--port", "5173", "--strictPort", "--host", "127.0.0.1"],
            cwd=str(FRONTEND), env=env,
            stdout=open(FRONTEND / "frontend.log", "a", encoding="utf-8", buffering=1),
            stderr=subprocess.STDOUT, creationflags=WIN_FLAGS,
        )
    else:
        print("[启动器] vite 未找到，fallback npm run dev")
        frontend_proc = subprocess.Popen(
            ["cmd", "/c", "npm", "run", "dev", "--", "--port", "5173", "--strictPort", "--host", "127.0.0.1"],
            cwd=str(FRONTEND), env=env,
            stdout=open(FRONTEND / "frontend.log", "a", encoding="utf-8", buffering=1),
            stderr=subprocess.STDOUT, creationflags=WIN_FLAGS,
        )
    print(f"[启动器] 前端 PID={frontend_proc.pid}，等待端口 5173...")
    ok = wait_port(5173, timeout=45)  # Vite 冷启动/依赖预构建可能 >20s
    if ok:
        write_status("step|前端服务已就绪")
    else:
        print("[启动器] 前端启动超时")
    return ok


def start_all():
    global status_text
    print("=" * 50)
    print("  智能体集群 AgentCluster - 一键启动")
    print("=" * 50)
    # 拉起“启动中”特效窗口，并在各阶段写入状态供其显示
    _ensure_splash()
    write_status("step|正在初始化启动器...")
    # start_all 开头探测：本次启动前前端是否已在服务（说明页面大概率已打开）。
    frontend_was_up = port_in_use(5173)
    status_text = "正在启动后端..."
    ok1 = start_backend()
    status_text = "正在启动前端..."
    ok2 = start_frontend()
    if ok1 and ok2:
        status_text = "✅ 运行中"
        PID_FILE.write_text(str(os.getpid()))
        print(f"\n✅ 全部启动成功！")
        print(f"   前端: {FRONTEND_URL}")
        print(f"   后端: {BACKEND_URL}")
    else:
        status_text = f"⚠️ 部分失败 (后端={'OK' if ok1 else 'FAIL'}, 前端={'OK' if ok2 else 'FAIL'})"
    # 自动打开独立窗口：只要前端就绪就开（窗口包装的是前端 5173，不依赖后端）。
    # 本进程内最多自动开一次；且仅当前端此前未在服务（页面还没打开过）时才开，
    # 避免重启时把已关闭的窗口反复顶起。
    # 自动开窗：只要前端是新起的且就绪就开（不依赖后端；重启后也会开）
    opened = False
    if not frontend_was_up and ok2:
        opened = True
        threading.Timer(1.2, _open_app_window).start()
    # 通知启动窗口收尾
    if opened:
        write_status("done|启动完成，正在打开界面...")
    elif ok1 and ok2:
        write_status("done|✅ 服务已就绪")
    else:
        write_status("done|⚠️ 部分服务启动失败，请查看托盘状态")
    if icon: update_menu()


def stop_all():
    """停止所有子进程（带超时兜底，最多 8 秒）"""
    global backend_proc, frontend_proc, _app_window_proc, _splash_proc
    print("[启动器] 正在停止所有服务...")

    # 1) 优雅 terminate + wait 最多 3s（含启动窗口/独立窗口进程）
    for p in (backend_proc, frontend_proc, _app_window_proc, _splash_proc):
        if p and p.poll() is None:
            try:
                p.terminate()
                try: p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    print(f"[启动器] PID={p.pid} terminate 超时，改用 kill")
                    p.kill()
            except Exception as e:
                print(f"[启动器] terminate 失败 ({e})，尝试 kill")
                try: p.kill()
                except Exception: pass
    backend_proc = frontend_proc = None
    _app_window_proc = None
    _splash_proc = None

    # 兜底：通过 PID 文件杀独立窗口（覆盖单例路径拉起的窗口）
    _kill_app_window()

    # 2) 兜底：按端口强制杀（kill_port 内部已用 CREATE_NO_WINDOW，避免闪 cmd 黑框）
    for port in (BACKEND_PORT, 5173):
        if port_in_use(port):
            kill_port(port)
    if PID_FILE.exists():
        try: PID_FILE.unlink()
        except Exception: pass
    clear_status_file()
    status_text = "⏹ 已停止"
    if icon:
        try: update_menu()
        except Exception: pass


# ---------- 托盘图标 ----------

def make_image(running: bool = True) -> Image.Image:
    """从项目图标加载 64x64 托盘图标，找不到时 fallback 到动态绘制。"""
    icon_paths = [
        ROOT / "frontend" / "public" / "icon.png",
        ROOT / "frontend" / "public" / "icon.jpg",
        ROOT / "1791461860623.webp.jpg",
    ]
    for p in icon_paths:
        if p.exists():
            try:
                img = Image.open(p).convert("RGBA")
                img = img.resize((64, 64), Image.LANCZOS)
                # icon.png 已是圆角 + 透明背景，直接用；仅对非 PNG（无 alpha）源套用圆角遮罩
                if img.mode == "RGBA" and img.split()[-1].getextrema()[0] == 0:
                    return img
                # Fallback: 非透明源 → 套圆角遮罩（22% 同 icon.png）
                mask = Image.new("L", (64, 64), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, 63, 63), radius=14, fill=255)
                img.putalpha(mask)
                return img
            except Exception:
                pass
    # Fallback: 动态绘制
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(64):
        alpha = 230
        c1 = (30, 50, 100, alpha) if running else (80, 30, 30, alpha)
        c2 = (60, 30, 120, alpha) if running else (120, 40, 40, alpha)
        r = int(30 * y / 63)
        g = int((30 + 30 * y / 63) if running else (40 + 80 * y / 63))
        b = int((100 + 20 * y / 63) if running else (40 + 50 * y / 63))
        d.line([(0, y), (64, y)], fill=(r, g, b, alpha))
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    glyph = "✦" if running else "✕"
    d.text((16, 14), glyph, fill=(230, 240, 255, 255))
    d.ellipse((2, 2, 61, 61), outline=(160, 200, 255, 120), width=1)
    return img


def update_menu():
    """每 3 秒刷新：用实时健康检查回写 status_text，避免启动期 FAIL 文案永久残留。

    关键：仅在 status_text 真正变化时才重建菜单（icon.update_menu()）。
    因为 pystray 的 update_menu() 内部会 DestroyMenu + CreateMenu，
    若右键菜单正打开着就会把它销毁，导致鼠标悬停高亮异常。
    平时只更新 icon.title（tooltip）即可。
    """
    if not icon: return
    global status_text
    # 启动/停止过渡态由 start_all/stop_all 设置，这里只在明确运行或停止后覆盖
    if status_text.startswith("正在启动") or status_text.startswith("正在停止"):
        icon.title = f"AgentCluster  {status_text}"
        return
    h = check_health()
    if h["backend"] and h["frontend"]:
        new_status = "✅ 运行中"
    elif not h["backend"] and not h["frontend"]:
        new_status = "⏹ 已停止"
    else:
        new_status = f"⚠️ 部分失败 (后端={'OK' if h['backend'] else 'FAIL'}, 前端={'OK' if h['frontend'] else 'FAIL'})"

    icon.title = f"AgentCluster  {new_status}"
    if new_status != status_text:
        status_text = new_status
        try: icon.update_menu()
        except Exception: pass


# ---------- 菜单动作 ----------

def on_open_window(icon=None, item=None):
    _open_app_window()

def on_open_frontend(icon=None, item=None):
    webbrowser.open(FRONTEND_URL + "/#/")

def on_open_backend(icon=None, item=None):
    webbrowser.open(BACKEND_URL + "/api/health")

def on_restart(icon=None, item=None):
    threading.Thread(target=lambda: (stop_all(), start_all()), daemon=True).start()

def on_stop(icon=None, item=None):
    threading.Thread(target=stop_all, daemon=True).start()

def on_exit(ic=None, item=None):
    """❌ 退出 —— 同步停服务确保退出干净，再关托盘"""
    print("[退出] on_exit")
    clear_status_file()  # 让还在等待的启动中窗口退出
    # 同步停所有服务（确保退出干净，最多 8s；stop_all 内含 _kill_app_window）
    stop_all()
    # 关托盘
    try:
        if ic: ic.stop()
        elif icon: icon.stop()
    except Exception as e:
        print(f"[退出] icon.stop() 异常: {e}")
    print("[退出] tray icon 已关闭")
    sys.exit(0)



def on_start_if_needed(icon=None, item=None):
    if is_running():
        icon.title = "AgentCluster ✅ 已在运行"
    else:
        threading.Thread(target=start_all, daemon=True).start()


# ---------- 主入口 ----------

def run_tray():
    global icon
    menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: f"状态: {status_text}", None, enabled=False
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🚀 启动服务", on_start_if_needed),
        pystray.MenuItem("🔄 重启全部", on_restart),
        pystray.MenuItem("⏹ 停止全部服务", on_stop),
        pystray.Menu.SEPARATOR,
        # 左键点击托盘图标 → 直接打开界面（pystray 左键事件会找到 default=True 的项）
        pystray.MenuItem("🌐 打开界面", on_open_window, default=True),
        pystray.MenuItem("🌐 用浏览器打开", on_open_frontend),
        pystray.MenuItem("⚙ 后端健康检查", on_open_backend),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ 退出程序", on_exit),
    )
    icon = pystray.Icon("agentcluster", make_image(running=True), "AgentCluster", menu)
    icon.title = f"AgentCluster  {status_text}"

    # 后台自动启动（包装异常）
    def safe_start():
        try:
            with open(ROOT / "tray_startup.log", "a", encoding="utf-8") as f:
                f.write(f"===== {time.strftime('%H:%M:%S')} start_all =====\n")
            start_all()
        except Exception:
            import traceback
            with open(ROOT / "tray_startup.log", "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
    threading.Thread(target=safe_start, daemon=True).start()

    # 周期性刷新菜单（每 3 秒）
    def refresh_loop():
        while True:
            try:
                update_menu()
            except Exception: pass
            time.sleep(3)
    threading.Thread(target=refresh_loop, daemon=True).start()

    icon.run()


def _already_running() -> bool:
    """Windows 命名互斥量单例检测：已有同名互斥量 → True。

    手柄随本进程存活期常驻（不主动 close），第二个启动器检测到已存在就直接退出，
    避免重复 start_all / 重复 webbrowser.open / 多个 Vite 抢端口互相把对方顶起来。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.GetLastError.argtypes = []
        k32.GetLastError.restype = wintypes.DWORD
        k32.CreateMutexW(None, False, "Local\\AgentCluster_Tray_SingleInstance")
        return k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


if __name__ == "__main__":
    import traceback
    # 单例：已有托盘在跑就不再拉起第二个（避免重复弹浏览器 / 端口打架 / 多实例 Vite 互抢，
    # 这是“最小化后浏览器又被反复顶起”的根因之一）。需要打开界面请点托盘菜单「🌐 打开界面」。
    if _already_running():
        # 服务已在跑，直接拉起独立窗口（用户点启动多半是想看到界面），再退出
        _open_app_window()
        time.sleep(2)
        sys.exit(0)
    try:
        run_tray()
    except Exception as e:
        clear_status_file()  # 启动窗口别再傻等
        with open(ROOT / "tray_crash.log", "a", encoding="utf-8") as f:
            f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(traceback.format_exc())
            f.write("\n\n")
        print(f"[启动器] 崩溃！详见 tray_crash.log: {e}")
        traceback.print_exc()
