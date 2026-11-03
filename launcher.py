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


def _find_node_dirs() -> list[str]:
    """扫描系统所有可能的 node.exe 位置，返回包含 node.exe 的目录列表（按优先级排序）。"""
    candidates = []

    # 1. 项目内置的 node（最确定，不依赖用户机器安装）
    candidates.append(ROOT / "node-v20.18.0-win-x64")
    candidates.append(ROOT / "node")

    # 2. 项目规则里约定的开发机 Node 位置
    candidates.append(Path(r"E:\app\node-v20.18.0-win-x64"))

    # 3. 系统常见安装位置（MSI / winget / 手动安装）
    candidates.append(Path(r"C:\Program Files\nodejs"))
    candidates.append(Path(r"C:\Program Files (x86)\nodejs"))
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs")

    # 4. nvm-windows（用户级 Node 版本管理器）
    nvm_root = Path(os.environ.get("APPDATA", "")) / "nvm"
    if nvm_root.exists():
        try:
            for ver_dir in sorted(nvm_root.iterdir()):
                if ver_dir.is_dir() and (ver_dir / "node.exe").exists():
                    candidates.append(ver_dir)
        except OSError:
            pass

    # 5. 其他包管理器
    candidates.append(Path(os.environ.get("USERPROFILE", "")) / "scoop" / "apps" / "nodejs" / "current")
    candidates.append(Path(os.environ.get("USERPROFILE", "")) / ".nvm" / "versions" / "node")

    # 6. 从当前 os.environ["PATH"] 里解析 node.exe（如果系统 PATH 本身已经配置好了）
    import shutil as _shutil
    which = _shutil.which("node")
    if which:
        candidates.append(Path(which).resolve().parent)

    # 过滤出真正有 node.exe 的目录，并去重保持顺序
    seen = set()
    result = []
    for c in candidates:
        try:
            c = Path(c)
        except (TypeError, ValueError):
            continue
        if not c or not str(c):
            continue
        resolved = str(c.resolve()) if c.exists() else str(c)
        if resolved in seen:
            continue
        if (c / "node.exe").exists():
            seen.add(resolved)
            result.append(str(c))
    return result


def build_env() -> dict:
    """构造 PATH 包含 node/npm 目录的环境变量，保证开机自启的裁剪 PATH 也能找到 Node。"""
    env = os.environ.copy()
    node_dirs = _find_node_dirs()

    if not node_dirs:
        # 一个都没找到，把当前状态记下来方便排障（launcher.py 本身无控制台，
        # 写进 ROOT 下的 launcher_node.log 供用户事后查看）
        try:
            log = ROOT / "launcher_node.log"
            log.write_text(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 警告：未在任何候选位置找到 node.exe\n"
                f"  当前 PATH: {env.get('PATH', '')}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        return env

    # 把所有找到的 Node 目录统一前置到 PATH（多个版本时取第一个，其他供 fallback）
    env["PATH"] = os.pathsep.join(node_dirs) + os.pathsep + env.get("PATH", "")

    # 同时显式设置 NODE_PATH（有些脚本会读这个变量）
    env["NODE_PATH"] = node_dirs[0]

    # 记录命中情况（排障用）
    try:
        log = ROOT / "launcher_node.log"
        log.write_text(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Node 搜索命中 {len(node_dirs)} 处:\n"
            + "\n".join(f"  - {d}" for d in node_dirs)
            + f"\n  PATH head: {env['PATH'][:200]}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

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
    """执行 npm run build（阻塞等待完成），返回是否成功。
    构建日志写到 ROOT/build_frontend.log，失败时把错误输出完整记录下来方便排障。"""
    import time as _time
    log_path = ROOT / "build_frontend.log"
    header = (
        f"===== {_time.strftime('%Y-%m-%d %H:%M:%S')} npm run build =====\n"
        f"cwd={FRONTEND}\n"
        f"PATH head={env.get('PATH', '')[:200]}\n\n"
    )
    log_path.write_text(header, encoding="utf-8")
    try:
        result = subprocess.run(
            ["npm.cmd", "run", "build"],
            cwd=str(FRONTEND),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_path.write_text(
            header + result.stdout + f"\n\n===== returncode={result.returncode} =====\n",
            encoding="utf-8",
        )
        return result.returncode == 0
    except FileNotFoundError as e:
        log_path.write_text(
            header + f"❌ npm.cmd 找不到: {e}\n"
                     f"PATH={env.get('PATH', '')}\n",
            encoding="utf-8",
        )
        return False
    except Exception as e:
        log_path.write_text(header + f"❌ 构建异常: {e}\n", encoding="utf-8")
        return False


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
