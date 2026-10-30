"""
AgentCluster 静默启动器 —— 无 cmd 控制台版本

用 pythonw.exe 运行（pythonw 从设计上就不会创建 cmd 控制台），
所有子进程也用 CREATE_NO_WINDOW 启动，进程链中彻底没有 cmd.exe。

用法:
  pythonw launcher.py            # 读取 .launch_mode.txt
  pythonw launcher.py dev        # 开发模式
  pythonw launcher.py web        # Web 模式
  pythonw launcher.py client     # 客户端模式
"""
import sys
import os
import time
import subprocess
import urllib.request
import zipfile
import tempfile
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
ELECTRON_EXE = FRONTEND / "node_modules" / "electron" / "dist" / "electron.exe"
DIST_INDEX = FRONTEND / "dist" / "index.html"
ELECTRON_DIST_ZIP_URL = (
    "https://npmmirror.com/mirrors/electron/v33.4.11/"
    "electron-v33.4.11-win32-x64.zip"
)

# Windows 特有：CreateProcess 标志，让子进程完全不创建控制台窗口
# 即使父进程有控制台也不继承，从根源消灭 cmd.exe 窗口
CREATE_NO_WINDOW = 0x08000000


# ==============================================================
# 工具函数
# ==============================================================

def find_python() -> str:
    """查找可用的 Python 解释器（web 模式用于启动 backend）。"""
    for p in [
        r"C:\Users\qiqi\AppData\Local\Programs\Python\Python314\python.exe",
        r"F:\anaconda3\python.exe",
    ]:
        if Path(p).exists():
            return p
    return "python"


def _kill_residual(process_names):
    """启动前清理残留进程（防止 Electron 单例锁冲突）。"""
    if os.name != "nt":
        return
    try:
        for name in process_names:
            subprocess.run(
                ["taskkill", "/F", "/IM", name, "/T"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
    except Exception:
        pass
    time.sleep(0.5)  # 等进程彻底退出


def build_env() -> dict:
    """构造 PATH 包含 node/npm 目录的环境变量。"""
    env = os.environ.copy()
    for node_dir in [
        ROOT / "node-v20.18.0-win-x64",
        ROOT / "node",
        Path(r"E:\app\node-v20.18.0-win-x64"),
    ]:
        if (node_dir / "node.exe").exists():
            env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")
            break
    return env


# 任务管理器“进程”页读取 exe 版本资源的 FileDescription/ProductName 作为进程名，
# electron.exe 默认显示为 "Electron"，重装 electron 后会被还原，启动时补回项目名
DISPLAY_NAME = "智能体集群 · AgentCluster"


def _find_rcedit():
    """在 electron-builder 缓存里找 rcedit（修补 exe 版本资源的工具）。"""
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "electron-builder" / "Cache" / "winCodeSign"
    try:
        hits = sorted(cache.glob("**/rcedit-x64.exe"))
    except OSError:
        return None
    return str(hits[-1]) if hits else None


def ensure_display_name():
    """确保 electron.exe 的 FileDescription/ProductName 是项目名（任务管理器显示名）。"""
    if not ELECTRON_EXE.exists():
        return
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             f"(Get-Item -LiteralPath '{ELECTRON_EXE}').VersionInfo.FileDescription"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        )
        raw = out.stdout.strip()
        try:
            desc = raw.decode("utf-8")
        except UnicodeDecodeError:
            desc = raw.decode("gbk", errors="ignore")
        if desc == DISPLAY_NAME:
            return
        rcedit = _find_rcedit()
        if not rcedit:
            return
        subprocess.run(
            [rcedit, str(ELECTRON_EXE),
             "--set-version-string", "FileDescription", DISPLAY_NAME,
             "--set-version-string", "ProductName", DISPLAY_NAME],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        )
    except Exception:
        pass


def ensure_electron():
    """确保 electron.exe 存在，不存在则下载解压。"""
    if not ELECTRON_EXE.exists():
        print("[AgentCluster] Downloading Electron binary...")
        tmp_zip = Path(tempfile.gettempdir()) / "electron.zip"
        urllib.request.urlretrieve(ELECTRON_DIST_ZIP_URL, tmp_zip)
        dist_dir = ELECTRON_EXE.parent
        dist_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(dist_dir)
        tmp_zip.unlink(missing_ok=True)
    ensure_display_name()


def run_build(env: dict) -> bool:
    """执行 npm run build（阻塞等待完成），返回是否成功。"""
    result = subprocess.run(
        ["npm.cmd", "run", "build"],
        cwd=str(FRONTEND),
        env=env,
    )
    return result.returncode == 0


def spawn(cmd, cwd=None, env=None):
    """
    启动一个完全隐藏的后台子进程。
    CREATE_NO_WINDOW 是关键：告诉 Windows 不要为这个子进程创建任何控制台窗口。
    """
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        creationflags=flags,
    )


# ==============================================================
# 各模式启动逻辑
# ==============================================================

def launch_prod(env: dict):
    """客户端/生产模式：Electron + 打包好的前端 dist。"""
    ensure_electron()
    if not DIST_INDEX.exists():
        if not run_build(env):
            sys.exit(1)
    # 必须传 env！main.cjs 里 _detectSystemNode() 用 `where node`，
    # 需要 PATH 里有 node.exe 的安装目录才能找到系统 Node.js
    # --no-single-instance 绕过 Electron 残留互斥量导致的单例锁冲突
    spawn([str(ELECTRON_EXE), ".", "--prod", "--no-single-instance"],
          cwd=str(FRONTEND), env=env)


def launch_dev(env: dict):
    """开发模式：Vite + Electron（npm run electron:dev 内部处理）。"""
    ensure_display_name()
    spawn(["npm.cmd", "run", "electron:dev"], cwd=str(FRONTEND), env=env)


def launch_web(env: dict):
    """Web 模式：backend API + dist server + tray-only Electron + 浏览器。"""
    ensure_electron()
    if not DIST_INDEX.exists():
        if not run_build(env):
            sys.exit(1)

    python = find_python()

    # 1. Backend API
    spawn([python, "main.py"], cwd=str(BACKEND), env=env)
    time.sleep(4)

    # 2. 静态资源 server（node electron\serve-dist.cjs .\dist 5173）
    spawn(["node", "electron\\serve-dist.cjs", ".\\dist", "5173"],
          cwd=str(FRONTEND), env=env)
    time.sleep(1)

    # 3. Tray-only Electron（只驻托盘，不建主窗口）
    spawn([str(ELECTRON_EXE), ".", "--tray-only"], cwd=str(FRONTEND), env=env)

    # 4. 打开浏览器
    webbrowser.open("http://localhost:5173")


# ==============================================================
# 入口
# ==============================================================

def main():
    os.chdir(str(ROOT))

    # 启动前清理残留进程（Electron 单例锁会因为残留的互斥量而拒绝新实例）
    _kill_residual(["electron.exe", "node.exe"])

    # 解析启动模式
    mode = "client"  # 默认
    args = {a.lower().lstrip("-") for a in sys.argv[1:]}
    explicit = args & {"dev", "web", "client"}
    if explicit:
        mode = explicit.pop()
    else:
        lm_file = ROOT / ".launch_mode.txt"
        if lm_file.exists():
            content = lm_file.read_text().strip().lower()
            if content in ("dev", "web", "client"):
                mode = content

    env = build_env()

    if mode == "dev":
        launch_dev(env)
    elif mode == "web":
        launch_web(env)
    else:
        launch_prod(env)


if __name__ == "__main__":
    main()
