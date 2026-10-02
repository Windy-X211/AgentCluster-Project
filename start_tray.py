"""
一键启动器 — 智能体集群 (AgentCluster)
- 启动后端 FastAPI (BACKEND_PORT)
- 启动前端 Vite (localhost:5173)
- 系统托盘图标 + 状态菜单 + 一键打开浏览器/停止服务
- 幂等：已运行就不重复拉起
"""
import os, sys, time, socket, threading, webbrowser, subprocess, http.server
from pathlib import Path
from PIL import Image, ImageDraw
import pystray

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
NODE_BIN = ROOT / "node-v20.18.0-win-x64" / "node.exe"
PID_FILE = ROOT / ".agentcluster.pid"

BACKEND_PORT = int(os.getenv("PORT", "8870"))
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = "http://localhost:5173"

# Windows 子进程创建flags：完全独立 + 无黑框
WIN_FLAGS = (subprocess.CREATE_NO_WINDOW | 0x00000008 | 0x00000200) if os.name == "nt" else 0

backend_proc = None
frontend_proc = None
icon: pystray.Icon | None = None
status_text = "正在启动..."


# ---------- 工具函数 ----------

def kill_port(port: int) -> bool:
    """按 netstat 找到占用端口的进程，taskkill 杀掉（尽力而为）"""
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
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
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=3)
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


# ---------- 启动/停止 ----------

def start_backend():
    global backend_proc
    if port_in_use(BACKEND_PORT):
        print(f"[启动器] {BACKEND_PORT} 被占用，尝试清理...")
        kill_port(BACKEND_PORT)
    if port_in_use(BACKEND_PORT):
        print(f"[启动器] 后端端口 {BACKEND_PORT} 无法清理（可能需管理员权限），跳过启动")
        return False
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
    if not ok:
        print("[启动器] 后端启动超时，详见 backend/backend.log")
    return ok


def start_frontend():
    global frontend_proc
    if port_in_use(5173):
        print("[启动器] 5173 被占用，尝试清理...")
        kill_port(5173)
    if port_in_use(5173):
        print("[启动器] 前端端口 5173 无法清理，跳过启动")
        return False
    node_dir = str(ROOT / "node-v20.18.0-win-x64")
    env = os.environ.copy()
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    # 尝试 vite 主入口（node_modules/vite/bin/vite.js），找不到再 fallback
    vite_js = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    vite_bin = FRONTEND / "node_modules" / ".bin" / "vite"
    if vite_js.exists():
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
    if not ok:
        print("[启动器] 前端启动超时")
    return ok


def start_all():
    global status_text
    print("=" * 50)
    print("  智能体集群 AgentCluster - 一键启动")
    print("=" * 50)
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
        # 自动打开浏览器
        threading.Timer(1.2, lambda: webbrowser.open(FRONTEND_URL + "/#/")).start()
    else:
        status_text = f"⚠️ 部分失败 (后端={'OK' if ok1 else 'FAIL'}, 前端={'OK' if ok2 else 'FAIL'})"
    if icon: update_menu()


def stop_all():
    """停止所有子进程（带超时兜底，最多 8 秒）"""
    global backend_proc, frontend_proc
    print("[启动器] 正在停止所有服务...")

    # 1) 优雅 terminate + wait 最多 3s
    for p in (backend_proc, frontend_proc):
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

    # 2) 兜底：按端口强制杀
    for port in (BACKEND_PORT, 5173):
        if port_in_use(port):
            os.system(
                f'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :{port} ^| findstr LISTENING\') '
                f'do taskkill /f /pid %a 2>nul'
            )
    if PID_FILE.exists():
        try: PID_FILE.unlink()
        except Exception: pass
    status_text = "⏹ 已停止"
    if icon:
        try: update_menu()
        except Exception: pass


# ---------- 托盘图标 ----------

def make_image(running: bool = True) -> Image.Image:
    """生成 64x64 的液态玻璃圆形图标"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 渐变底
    for y in range(64):
        alpha = 230
        c1 = (30, 50, 100, alpha) if running else (80, 30, 30, alpha)
        c2 = (60, 30, 120, alpha) if running else (120, 40, 40, alpha)
        r = int(30 * y / 63)
        g = int((30 + 30 * y / 63) if running else (40 + 80 * y / 63))
        b = int((100 + 20 * y / 63) if running else (40 + 50 * y / 63))
        d.line([(0, y), (64, y)], fill=(r, g, b, alpha))
    d.ellipse((0, 0, 63, 63), fill=None, outline=None)
    # 遮罩圆（带圆角）
    from PIL import ImageFilter
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
    img.putalpha(mask)
    # 中心字母
    d = ImageDraw.Draw(img)
    glyph = "✦" if running else "✕"
    d.text((16, 14), glyph, fill=(230, 240, 255, 255))
    # 边缘高光
    d.ellipse((2, 2, 61, 61), outline=(160, 200, 255, 120), width=1)
    return img


def update_menu():
    """每 3 秒刷新：用实时健康检查回写 status_text，避免启动期 FAIL 文案永久残留。"""
    if not icon: return
    global status_text
    # 启动/停止过渡态由 start_all/stop_all 设置，这里只在明确运行或停止后覆盖
    if status_text.startswith("正在启动") or status_text.startswith("正在停止"):
        icon.title = f"AgentCluster  {status_text}"
        try: icon.update_menu()
        except Exception: pass
        return
    h = check_health()
    if h["backend"] and h["frontend"]:
        status_text = "✅ 运行中"
    elif not h["backend"] and not h["frontend"]:
        if status_text != "⏹ 已停止":
            status_text = "⏹ 已停止"
    else:
        status_text = f"⚠️ 部分失败 (后端={'OK' if h['backend'] else 'FAIL'}, 前端={'OK' if h['frontend'] else 'FAIL'})"
    icon.title = f"AgentCluster  {status_text}"
    try: icon.update_menu()
    except Exception: pass


# ---------- 菜单动作 ----------

def on_open_frontend(icon=None, item=None):
    webbrowser.open(FRONTEND_URL + "/#/")

def on_open_backend(icon=None, item=None):
    webbrowser.open(BACKEND_URL + "/api/health")

def on_restart(icon=None, item=None):
    threading.Thread(target=lambda: (stop_all(), start_all()), daemon=True).start()

def on_stop(icon=None, item=None):
    threading.Thread(target=stop_all, daemon=True).start()

def on_exit(ic=None, item=None):
    """❌ 退出 —— 后台线程停服务，立即关托盘（不等 stop_all 完成）"""
    print("[退出] on_exit")
    # 后台停服务（不阻塞托盘关闭）
    threading.Thread(target=lambda: (stop_all(), sys.exit(0)), daemon=True).start()
    # 立即关托盘
    try:
        if ic: ic.stop()
        elif icon: icon.stop()
    except Exception as e:
        print(f"[退出] icon.stop() 异常: {e}")
    print("[退出] tray icon 已关闭")


def on_force_exit(ic=None, item=None):
    """硬关闭：只关托盘，子进程留给用户手动清理"""
    print("[退出] 硬关闭")
    try:
        if ic: ic.stop()
        elif icon: icon.stop()
    except Exception: pass
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
            lambda item: f"状态: {status_text}", None, enabled=False, default=True
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🚀 启动服务", on_start_if_needed),
        pystray.MenuItem("🔄 重启全部", on_restart),
        pystray.MenuItem("⏹ 停止全部服务", on_stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🌐 打开界面", on_open_frontend),
        pystray.MenuItem("⚙ 后端健康检查", on_open_backend),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ 退出 (停服务)", on_exit),
        pystray.MenuItem("⚠️ 强制关闭托盘", on_force_exit),
    )
    icon = pystray.Icon("agentcluster", make_image(running=True), "AgentCluster", menu)
    # 双击图标 → 直接退出
    icon.default_action = on_exit
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


if __name__ == "__main__":
    import traceback
    try:
        run_tray()
    except Exception as e:
        with open(ROOT / "tray_crash.log", "a", encoding="utf-8") as f:
            f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(traceback.format_exc())
            f.write("\n\n")
        print(f"[启动器] 崩溃！详见 tray_crash.log: {e}")
        traceback.print_exc()
