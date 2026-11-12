"""预设命令系统 - 文件管理、CMD 运行
每个命令带完整的 JSON Schema 描述，用于 LLM function calling。

路径解析约定：
  - 画布模式（canvas.workdir 已注入 WORKDIR_CTX）：相对路径自动拼到 workdir 上；
  - 独立对话模式：优先用全局 settings.default_workdir；都没有则退回进程 CWD。
  - 绝对路径原样使用（无论哪种模式）。
"""
import os, subprocess, shutil, re, signal, socket, time, threading
import base64
import locale
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from contextvars import ContextVar

# cluster.handoff / cluster.send_message（A2A 移交与发送消息（可往返多轮）工具）— 导入进本模块 globals，供 run_command 分发
from core.a2a import cmd_cluster_handoff, HANDOFF_TOOL, cmd_cluster_send_message, CALL_TOOL

# agent.smart_search（智能搜索子智能体）— 同样导入进 globals，供 run_command 反射分发
from core.smart_search import cmd_agent_smart_search, SMART_SEARCH_TOOL  # noqa: F401

# agent.spawn（派生子智能体：主智能体自定义临时子 agent 并配置能力）— 同上
from core.subagent import cmd_agent_spawn, SUBAGENT_TOOL  # noqa: F401


# ======================================================
#  后台进程管理（cmd.run 自动识别长运行命令并后台化）
# ======================================================
# 长运行命令关键词 —— 两类匹配策略：
#   1) _LONG_RUN_EXACT：子串精确匹配（命令里出现这个短语就认为是长运行）
#      这些词都很独特，几乎不可能在普通代码里误命中
#   2) _LONG_RUN_CMD_STARTS：仅当命令以该词开头（或前面是 &&/&/|/;）时才匹配
#      这些词（node、watch、code）太泛，python -c 代码里容易偶然包含
_LONG_RUN_EXACT = [
    # 静态服务器
    "http.server", "http.serve", "simplehttpserver",
    # Web 框架 dev server
    "uvicorn", "gunicorn", "flask run", "flask --app run", "django-admin runserver",
    "manage.py runserver", "streamlit run",
    # Node.js
    "npm run dev", "npm run start", "npm run serve", "npx", "vite",
    "webpack-dev-server", "next dev", "next start", "react-scripts start",
    # 文件监听 / 守护进程
    "nodemon", "forever", "pm2", "supervisord",
    # 数据库 / 缓存服务
    "redis-server", "mongod", "mysqld", "postgres -", "memcached",
    "elasticsearch", "kibana", "logstash",
    # 消息队列 / 代理
    "rabbitmq", "zookeeper",
]
_LONG_RUN_CMD_STARTS = [
    # 这些词太泛，仅在命令开头（或 shell 分隔符之后）作为第一个词时才匹配
    "node", "npm", "python -m http.server", "python -m simplehttpserver",
    "watch", "gradio", "postgres", "kafka", "nginx", "apache2", "httpd", "daemon",
    "serve -l", "serve -p", "tail -f", "tail -F",
]

# 正在运行的后台进程表：pid → {"proc": Popen, "command": str, "cwd": str, "started_at": float, "log_path": str}
_BG_PROCESSES: dict[int, dict] = {}
_BG_LOCK = threading.Lock()


def _is_long_running(command: str) -> bool:
    """判断一条命令是否看起来是长运行（后台）进程。

    使用两层匹配避免误伤：
    - 精确短语匹配（独特词，不太可能误命中）
    - 命令开头匹配（泛词只在作为命令入口时才算）
    """
    if not command:
        return False
    lower = command.lower()
    # 第一层：精确短语匹配
    if any(kw in lower for kw in _LONG_RUN_EXACT):
        return True
    # 第二层：命令开头/分隔符后的起始词匹配
    # 把 &&、||、&、| 当作 shell 命令分隔符（注意不含 ; —— 它在 python -c 里是语句分隔符）
    segments = re.split(r'\s*(?:&&|\|\||[&|])\s*', lower)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # 取命令段第一个"词"（到第一个空格为止），或检查段本身是否以某个精确短语开头
        first_word = seg.split()[0] if seg.split() else ""
        for kw in _LONG_RUN_CMD_STARTS:
            if " " in kw:
                # 含空格的短语（如 "python -m http.server"）：检查段是否以它开头
                if seg.startswith(kw):
                    return True
            else:
                # 单词：检查段的第一个词是否等于它（避免子串匹配）
                if first_word == kw:
                    return True
    return False


def _extract_port(command: str) -> int | None:
    """尝试从命令里解析端口号，用于启动后快速探活。"""
    # 匹配常见的端口写法：-p 8080 / --port 8080 / :8080 / -port 8080 / /8081
    patterns = [
        r'[-\s](?:port|p)\s*[=: ]?\s*(\d{2,5})',
        r':(\d{2,5})\b',
        r'http\.server\s+(\d{2,5})',
        r'localhost:(\d{2,5})',
    ]
    for pat in patterns:
        m = re.search(pat, command, re.IGNORECASE)
        if m:
            try:
                p = int(m.group(1))
                if 1024 <= p <= 65535:
                    return p
            except (ValueError, IndexError):
                continue
    return None


def _probe_port(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> dict:
    """探测某个本地端口是否已开始监听。返回 {"ok": bool, "note": str}。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            return {"ok": True, "note": f"端口 {port} 已开始监听 ✓"}
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return {"ok": False, "note": f"端口 {port} 尚未就绪（{type(e).__name__}）"}


def _decode_console(data: bytes) -> str:
    """解码子进程输出：Windows 上 cmd 内置命令多按本地代码页（中文为 GBK）输出，
    直接按 UTF-8 解码会得到乱码。这里优先 UTF-8，失败再回退本地编码。"""
    if not data:
        return ""
    encs = ["utf-8", locale.getpreferredencoding(False) or "gbk", "gbk"]
    for enc in encs:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _read_log_text(log_path: str, limit: int = 4000) -> str:
    """读取后台日志（按本地编码容错解码），返回末尾 limit 字符。"""
    try:
        with open(log_path, "rb") as lf:
            raw = lf.read()
    except Exception:
        return ""
    return _decode_console(raw)[-limit:]


# PowerShell 专属的命令入口 / 关键字：cmd.run 默认走 cmd.exe，
# 这些写法在 cmd.exe 下必然报「不是内部或外部命令」，需要自动转 PowerShell 执行。
_PS_CMDLETS = {
    "start-sleep", "write-output", "write-host", "get-date", "test-path",
    "new-item", "remove-item", "copy-item", "move-item", "rename-item",
    "get-childitem", "get-content", "set-content", "add-content", "out-file",
    "get-process", "stop-process", "start-process", "wait-process",
    "get-service", "start-service", "stop-service", "get-item", "join-path",
    "split-path", "resolve-path", "get-location", "set-location",
    "select-object", "where-object", "foreach-object", "sort-object",
    "measure-object", "invoke-webrequest", "invoke-restmethod", "get-help",
    "get-command", "get-module", "import-module", "clear-host",
}


def _is_powershell_command(command: str) -> bool:
    """粗略判断命令是否使用了 PowerShell 语法（cmdlet / $env: / 变量赋值等）。"""
    if not command:
        return False
    lower = command.strip().lower()
    if lower.startswith("powershell") or lower.startswith("pwsh"):
        return False  # 已经显式指定解释器，交给 shell 原样执行
    if "$env:" in lower or re.search(r'\bset-\w+', lower):
        return True
    segments = re.split(r'\s*(?:&&|\|\||[&|;])\s*', lower)
    for seg in segments:
        first = seg.strip().split()[0] if seg.strip() else ""
        if first in _PS_CMDLETS:
            return True
    return False


def _wrap_powershell(command: str) -> str:
    """把 PowerShell 命令包装成可在 cmd.exe 下安全执行的单行调用。

    用 -EncodedCommand（UTF-16LE + Base64）避免引号/换行转义问题；
    同时把输出编码设为 UTF-8，保证中文输出可读。
    """
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + command
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {b64}"


def _cleanup_dead_bg() -> None:
    """清理已退出的后台进程（从字典中移除）。"""
    with _BG_LOCK:
        dead = [pid for pid, info in _BG_PROCESSES.items()
                if info.get("proc") and info["proc"].poll() is not None]
        for pid in dead:
            info = _BG_PROCESSES.pop(pid, None)
            if info:
                try:
                    rc = info["proc"].returncode
                    info["exit_code"] = rc
                except Exception:
                    pass


def _is_gui_launch(command: str) -> bool:
    """判断命令是否是 Windows GUI 启动命令（start / 浏览器 / 常见应用）。
    这类命令的 shell 包装进程（cmd.exe / powershell）会立即退出，
    真正的 GUI 子进程脱离出来运行——Popen 本身不应被当作"秒挂"。

    使用两层精确匹配避免误伤普通 python -c 代码：
      - _GUI_EXACT：独特短语，命令里出现即匹配
      - _GUI_CMD_STARTS：泛词（如 code/explorer）只作为命令入口时才匹配
    """
    if not command:
        return False
    lower = command.lower()

    # 精确短语：不太可能误命中
    _GUI_EXACT = [
        "--remote-debugging-port",
    ]
    for kw in _GUI_EXACT:
        if kw in lower:
            return True

    # 命令开头/分隔符后的起始词匹配（泛词）
    _GUI_CMD_STARTS = [
        "start",         # cmd.exe 的 start 命令（注意：会命中所有 cmd.exe 命令...
                         # 但实际上带 start 的命令几乎都是 GUI/后台启动，保留）
        "explorer.exe", "explorer",
        "msedge", "chrome.exe", "chrome", "firefox.exe", "brave.exe",
        "code.exe", "calc.exe",
        "notepad.exe", "notepad",
    ]
    # 不含 ; —— 它在 python -c 里是 Python 语句分隔符，不是 shell 命令分隔符
    segments = re.split(r'\s*(?:&&|\|\||[&|])\s*', lower)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        first_word = seg.split()[0] if seg.split() else ""
        if first_word in _GUI_CMD_STARTS:
            return True
        # 对 start 特殊处理：start 可以不带扩展名
        if first_word == "start":
            return True

    return False


def _start_bg_process(command: str, cwd: str, shell: bool = True,
                      classify_command: str | None = None) -> dict:
    """在后台启动一条长运行 / GUI 命令，立即返回状态。

    两类场景都走这里：
      (a) 常驻服务（uvicorn/node/redis...）→ Popen 进程自身就是服务进程，会一直活着
      (b) GUI 启动（start / 浏览器...）→ Popen 只是 shell 包装（cmd.exe），
          它会很快退出；真正的 GUI 子进程脱离出来独立运行。
          这种情况下 Popen 的退出是正常的，不算"秒挂"。

    classify_command：用于长运行/GUI/端口识别的原始命令。command 可能已被包装
    （如 PowerShell -EncodedCommand），此时用原始命令做分类更准确。
    """
    import tempfile
    _cleanup_dead_bg()
    classify = classify_command if classify_command is not None else command

    # 把 stdout/stderr 重定向到独立日志文件（Windows 上用 subprocess.DEVNULL 可能丢日志）
    log_dir = os.path.join(tempfile.gettempdir(), "agent_cluster_bg")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"bg_{os.getpid()}_{int(time.time())}.log")

    try:
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        log_fh = None

    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd,
            stdout=log_fh,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # Windows 上 CREATE_NEW_PROCESS_GROUP 让子进程独立于 Python 进程组
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError as e:
        if log_fh: log_fh.close()
        return {"error": f"命令不存在或路径错误: {e}", "command": command}
    except Exception as e:
        if log_fh: log_fh.close()
        return {"error": f"后台启动失败: {e}", "command": command}
    finally:
        # 父进程不再需要写端：子进程已继承句柄，关闭避免句柄泄漏 / 日志尾巴不落盘
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass

    pid = proc.pid
    # GUI 启动命令不把 shell wrapper 的 pid 记入后台表（它会秒退），
    # 直接跳过，让后续逻辑按"已启动"处理
    gui_cmd = _is_gui_launch(classify)
    if not gui_cmd:
        with _BG_LOCK:
            _BG_PROCESSES[pid] = {
                "proc": proc,
                "command": classify,
                "cwd": cwd,
                "started_at": time.time(),
                "log_path": log_path,
            }

    # 快速探活：等一小会儿看端口
    port = _extract_port(classify)
    probe = None
    if port:
        # 最多试 3 次，每次间隔 0.6s（总共等 ~2s）
        for _ in range(3):
            time.sleep(0.6)
            probe = _probe_port(port)
            if probe["ok"]:
                break
    else:
        # 没识别到端口，等 0.3s 看进程是不是已经挂了
        time.sleep(0.3)

    still_alive = proc.poll() is None
    # —— 判定启动成功与否 ——
    # GUI 启动：Popen 进程（shell wrapper）秒退是正常的；
    #           只要端口探活成功 / 或命令本身就是 GUI 启动，就认为成功
    # 常驻服务：Popen 进程必须活着，且端口（如果有）要探活成功
    launch_ok = False
    if gui_cmd:
        # GUI 启动：即使 Popen 退出也没关系
        if probe and probe["ok"]:
            launch_ok = True    # 端口已就绪（浏览器 CDP 等）
        elif still_alive:
            launch_ok = True    # shell wrapper 还活着（有些 GUI 程序会等一会儿）
        else:
            # Popen 已退、端口也没探活到 —— 做最后一次尝试
            # 可能 Edge 启动慢，再等 1s 看看
            if port:
                time.sleep(1.0)
                probe2 = _probe_port(port)
                if probe2["ok"]:
                    probe = probe2
                    launch_ok = True
                else:
                    # 端口还是没起来，但 start 命令本身已经退出了（说明它成功地
                    # 把任务交给了 GUI 子进程），也当作成功，只是 note 里提示
                    launch_ok = True
            else:
                # 无端口 + GUI 命令 + shell 已退 → 大概率成功
                # （start/notepad/explorer 这类，shell 退了就是启动完了）
                launch_ok = True
    else:
        # 常驻服务：Popen 进程必须还活着
        if not still_alive:
            with _BG_LOCK:
                _BG_PROCESSES.pop(pid, None)
            tail = _read_log_text(log_path, 4000)
            exit_code = proc.returncode
            if exit_code == 0:
                # 被关键词误判成长运行的短命令（如 node -e / python -c），
                # 实际已经正常执行完毕 —— 这是成功，不是"秒挂"，返回完整输出
                return {
                    "ok": True,
                    "completed": True,
                    "returncode": 0,
                    "stdout": tail,
                    "stderr": "",
                    "command": command,
                    "classify_command": classify,
                    "cwd": cwd,
                    "background": False,
                    "note": "命令已执行完成（快速退出，exit_code=0）",
                }
            # 非零退出 → 真正的启动/执行失败
            return {
                "error": f"进程启动后立即退出（exit_code={exit_code}）",
                "returncode": exit_code,
                "stdout_tail": tail,
                "stderr": tail,
                "log_path": log_path,
                "command": command,
            }
        launch_ok = True

    # 构建成功返回
    note = ""
    if gui_cmd:
        if probe and probe["ok"]:
            note = "GUI 程序已启动（端口探活就绪），不阻塞智能体"
        elif port:
            note = "GUI 程序已启动（端口尚未就绪，可能需要再等一会儿）"
        else:
            note = "GUI 程序已启动，不阻塞智能体"
    else:
        note = "已后台启动（长运行命令自动后台化，不阻塞智能体）"

    result = {
        "ok": True,
        "pid": pid if still_alive else None,
        "command": classify,
        "cwd": cwd,
        "running": still_alive if not gui_cmd else True,
        "background": True,
        "note": note,
        "log_path": log_path,
    }
    if port:
        result["port"] = port
        result["port_probe"] = probe
    return result


# 前台自动执行：无输出且进程存活满这么多秒 → 判定为 GUI/常驻进程，转后台不再阻塞智能体
_FG_IDLE_TIMEOUT = 6.0


def _run_foreground_auto(command: str, cwd: str, shell: bool = True,
                         timeout: int = 60, classify_command: str | None = None) -> dict:
    """自动模式的前台执行：短命令正常返回输出；长驻/GUI 进程自动转后台。

    与 subprocess.run(capture_output=...) 的区别：
      - stdout/stderr 重定向到文件而非管道 —— GUI 子进程继承的是文件句柄，
        不会再让父进程永远等不到管道 EOF 而卡死；
      - 轮询进程状态：进程退出即返回完整输出；若进程长时间不退出（无论是
        GUI 程序还是未被关键词识别的常驻服务），自动注册为后台进程并立即返回，
        避免智能体执行循环被阻塞到 timeout。
    """
    import tempfile
    _cleanup_dead_bg()
    classify = classify_command if classify_command is not None else command

    log_dir = os.path.join(tempfile.gettempdir(), "agent_cluster_bg")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"fg_{os.getpid()}_{int(time.time())}.log")

    try:
        log_fh = open(log_path, "w+", encoding="utf-8", errors="replace")
    except Exception:
        log_fh = None

    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd,
            stdout=log_fh,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError as e:
        if log_fh: log_fh.close()
        return {"error": f"命令不存在或路径错误: {e}", "command": command}
    except Exception as e:
        if log_fh: log_fh.close()
        return {"error": str(e), "command": command}
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass

    deadline = time.time() + max(1, int(timeout))
    idle_deadline = time.time() + _FG_IDLE_TIMEOUT
    last_size = 0
    while True:
        rc = proc.poll()
        if rc is not None:
            out = _read_log_text(log_path, 4000)
            return {"stdout": out, "stderr": "", "returncode": rc,
                    "command": classify, "cwd": cwd}
        now = time.time()
        # 有输出则刷新静默计时：持续产出说明是真正在干活的长命令，允许继续等
        try:
            cur_size = os.path.getsize(log_path)
        except OSError:
            cur_size = last_size
        if cur_size != last_size:
            last_size = cur_size
            idle_deadline = now + _FG_IDLE_TIMEOUT
        if now >= deadline or now >= idle_deadline:
            # 进程仍存活 → 转后台，立即返回，不阻塞智能体
            tail = _read_log_text(log_path, 4000)
            with _BG_LOCK:
                _BG_PROCESSES[proc.pid] = {
                    "proc": proc,
                    "command": classify,
                    "cwd": cwd,
                    "started_at": time.time(),
                    "log_path": log_path,
                }
            return {
                "ok": True,
                "pid": proc.pid,
                "command": classify,
                "cwd": cwd,
                "running": True,
                "background": True,
                "stdout": tail,
                "log_path": log_path,
                "note": "命令未在预期时间内结束，已转为后台运行（不阻塞智能体）。"
                        "如需等待其完成，显式传 background=false。",
            }
        time.sleep(0.2)


def cmd_cmd_bg_list() -> dict:
    """列出当前所有后台进程的状态。"""
    _cleanup_dead_bg()
    with _BG_LOCK:
        items = []
        for pid, info in _BG_PROCESSES.items():
            proc = info.get("proc")
            items.append({
                "pid": pid,
                "command": info.get("command", ""),
                "cwd": info.get("cwd", ""),
                "running": proc is not None and proc.poll() is None,
                "log_path": info.get("log_path", ""),
                "uptime_s": round(time.time() - info.get("started_at", time.time()), 1),
            })
    return {"processes": items, "count": len(items)}


def cmd_cmd_bg_stop(pid: int, force: bool = False) -> dict:
    """停止指定 PID 的后台进程。force=True 时用 SIGKILL（Windows 上等价于 taskkill /F）。"""
    _cleanup_dead_bg()
    with _BG_LOCK:
        info = _BG_PROCESSES.get(pid)
        if not info:
            return {"error": f"后台进程 {pid} 不存在或已退出",
                    "hint": "先调用 cmd.bg_list 查看当前后台进程"}
        proc = info["proc"]
        try:
            if os.name == "nt":
                # Windows：taskkill /F /PID pid（/T 同时杀子进程）
                flag = "/F" if force else "/T"
                subprocess.run(["taskkill", flag, "/PID", str(pid)],
                               capture_output=True, timeout=5)
            else:
                sig = signal.SIGKILL if force else signal.SIGTERM
                try:
                    os.killpg(os.getpgid(pid), sig)
                except Exception:
                    proc.send_signal(sig)
            # 等它真退出
            try:
                proc.wait(timeout=3 if force else 5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            return {"error": f"停止进程失败: {e}"}
        _BG_PROCESSES.pop(pid, None)
    return {"ok": True, "pid": pid, "stopped": True}


def cmd_cmd_bg_log(pid: int, tail: int = 50) -> dict:
    """查看后台进程的日志尾部（默认最后 50 行）。"""
    _cleanup_dead_bg()
    with _BG_LOCK:
        info = _BG_PROCESSES.get(pid)
    if not info:
        return {"error": f"后台进程 {pid} 不存在或已退出"}
    log_path = info.get("log_path", "")
    if not log_path or not os.path.isfile(log_path):
        return {"error": "该进程没有日志文件"}
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        tail_lines = lines[-tail:] if tail > 0 else lines
        return {"ok": True, "pid": pid, "log_path": log_path,
                "tail_lines": tail, "content": "".join(tail_lines)[-4000:]}
    except Exception as e:
        return {"error": f"读日志失败: {e}"}


# —— 工作区目录上下文（与 A2A 的 TURN_CTX 同风格，在 agent_runtime 每轮执行入口设置）——
WORKDIR_CTX: ContextVar[str | None] = ContextVar("workdir", default=None)

# —— 备份上下文（文件快照）：在 agent_runtime 每次 _execute_tool 前设置 ——
# 值为 dict: {"conv_id": int, "tool_call_id": str, "agent_id": int}
BACKUP_CTX: ContextVar[dict | None] = ContextVar("backup_ctx", default=None)


_DEFAULT_WORKDIR_CACHE: dict = {"path": None, "at": 0.0}


def invalidate_default_workdir_cache() -> None:
    """清空 default_workdir 缓存——设置里改了默认工作区后立即生效（否则最多延迟 2s）。"""
    _DEFAULT_WORKDIR_CACHE["path"] = None
    _DEFAULT_WORKDIR_CACHE["at"] = 0.0


def get_default_workdir() -> str | None:
    """读取设置里的全局默认工作区（settings.default_workdir）。

    与 _effective_workdir 的区别：这里**只读 settings**，不看 WORKDIR_CTX、不走缓存，
    供 agent_runtime 判断「当前生效的工作区是否来自全局默认」时使用。
    目录不存在时返回 None。
    """
    try:
        from core.store import load
        s = load("settings") or {}
        dwd = (s.get("default_workdir") or "").strip()
        if dwd and os.path.isdir(dwd):
            return dwd
    except Exception:
        pass
    return None


def _effective_workdir() -> str | None:
    """返回当前生效的工作区绝对路径（没有则返回 None）。

    性能优化：移除每次调用都 open/write/close 的 debug 日志（多智能体并行时
    会产生上百次磁盘争用，是卡顿根因之一）；settings.default_workdir 用
    短 TTL 缓存，避免每次 fallback 都触发 store.load（JSON 读盘）。

    注意：缓存必须**原地更新字典**。早期版本写成 `_DEFAULT_WORKDIR_CACHE = {...}`，
    Python 会把它当成局部变量 → 读取时抛 UnboundLocalError → 被下面的
    except 静默吞掉 → default_workdir 兜底从未真正生效（实测恒返回 None）。
    """
    # 1) 画布模式注入的 workdir（ContextVar，零成本）
    wd = WORKDIR_CTX.get()
    if wd and os.path.isdir(wd):
        return wd
    # 2) 兜底：settings 里的 default_workdir（短 TTL 缓存 2s，避免频繁读盘）
    try:
        now = time.time()
        cached = _DEFAULT_WORKDIR_CACHE
        if cached["path"] and (now - cached["at"]) < 2.0:
            if os.path.isdir(cached["path"]):
                return cached["path"]
        from core.store import load
        s = load("settings") or {}
        dwd = (s.get("default_workdir") or "").strip()
        if dwd and os.path.isdir(dwd):
            cached["path"] = dwd
            cached["at"] = now
            return dwd
        # 目录失效 → 清缓存（原地更新，勿重新绑定名字）
        cached["path"] = None
        cached["at"] = now
    except Exception:
        pass
    return None


def _resolve(path: str) -> Path:
    """把 LLM 传来的 path 解析成绝对 Path：
    - 已是绝对路径 → 原样 resolve
    - 相对路径 → 拼到生效 workdir 上（有 workdir），否则 resolve 到 CWD
    """
    if not path:
        return Path(path).resolve()
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    wd = _effective_workdir()
    base = Path(wd) if wd else Path.cwd()
    return (base / p).resolve()


# 备份辅助函数（从 BACKUP_CTX 取 conv/tool_call/agent 信息，调 file_backup 模块）
def _ctx() -> dict | None:
    return BACKUP_CTX.get()


def _snapshot(path: str, op_name: str, snapshot_kind: str = "write") -> None:
    """触发文件快照（无 ctx 或快照失败时静默跳过）。"""
    ctx = _ctx()
    if not ctx:
        return
    try:
        from core.file_backup import snapshot_before_write, snapshot_before_delete
        if snapshot_kind == "delete":
            snapshot_before_delete(
                conv_id=ctx["conv_id"],
                tool_call_id=ctx["tool_call_id"],
                agent_id=ctx["agent_id"],
                path=path,
            )
        else:
            snapshot_before_write(
                conv_id=ctx["conv_id"],
                tool_call_id=ctx["tool_call_id"],
                agent_id=ctx["agent_id"],
                path=path,
                op_name=op_name,
            )
    except Exception as _e:
        # 快照失败不能阻塞主操作
        print(f"[backup] WARN snapshot failed for {path}: {_e}")


def _scope_guard(path: str, op: str = "此操作") -> str | None:
    """工作范围校验（settings.work_scope_mode）：返回 None = 放行，否则返回错误文案。

    只拦「会改磁盘 / 起进程」的命令（file.write/delete/replace/patch、cmd.run），
    只读命令不受限。权限模块自身异常时放行 —— 不能因为设置读挂了就写不了文件。
    """
    try:
        from core.permissions import check_path
        return check_path(path, op)
    except Exception:
        return None


def run_command(command_name: str, **kwargs) -> Any:
    fn = globals().get(f"cmd_{command_name.replace('.','_')}")
    return fn(**kwargs) if fn else {"error": f"unknown command: {command_name}"}


_IMG_READ_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_IMG_READ_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")

# Office 文档扩展名：file.read 遇到这些类型时会抽取可读文本返回，而不是直接 UTF-8 解码二进制
_OFFICE_EXTS = (
    ".docx",  # Word 2007+
    ".doc",   # 旧版 Word 二进制（仅 Windows + 本机 Word）
    ".xlsx",  # Excel 2007+
    ".xls",   # 旧版 Excel 二进制（仅 Windows + 本机 Excel）
    ".pptx",  # PowerPoint 2007+
    ".ppt",   # 旧版 PowerPoint 二进制（仅 Windows + 本机 PowerPoint）
    ".pdf",   # PDF
)


def _extract_office_text(p: Path) -> str:
    """把 Office/PDF 文档转成可读纯文本返回（失败则返回错误信息字符串）。

    抽取原则：
      - 不追求完美排版，只要 LLM 能看懂内容就行
      - 现代格式（docx/xlsx/pptx/pdf）走 Python 第三方库（已预装）
      - 旧版二进制（doc/xls/ppt）在 Windows 上走本机 Office COM；无 Office 时返回明确提示
      - 任何异常都降级为 "（错误：...）" 插入返回文本，保证调用方始终能拿到字符串
    """
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            return _extract_docx(p)
        if ext == ".doc":
            return _extract_doc_old(p)
        if ext == ".xlsx":
            return _extract_xlsx(p)
        if ext == ".xls":
            return _extract_xls_old(p)
        if ext == ".pptx":
            return _extract_pptx(p)
        if ext == ".ppt":
            return _extract_ppt_old(p)
        if ext == ".pdf":
            return _extract_pdf(p)
    except Exception as e:  # noqa: BLE001
        return f"（{ext} 解析失败：{e}）"
    return f"（{ext} 不支持文本抽取）"


def _extract_docx(p: Path) -> str:
    from docx import Document
    doc = Document(str(p))
    out: list[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            out.append(t)
    for ti, tbl in enumerate(doc.tables):
        out.append(f"\n[表格 {ti + 1}]")
        for row in tbl.rows:
            cells = [c.text.replace("\n", " ").strip() for c in row.cells]
            out.append(" | ".join(cells))
    return "\n".join(out)


def _extract_doc_old(p: Path) -> str:
    """旧版 .doc —— 用本机 Word COM 读取 Content.Text。"""
    import pythoncom
    from win32com.client import Dispatch
    pythoncom.CoInitialize()
    app = None
    try:
        app = Dispatch("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(str(p), ReadOnly=True, AddToRecentFiles=False)
        try:
            text = doc.Content.Text
        finally:
            doc.Close(SaveChanges=False)
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()
    except ImportError:
        return "（.doc 旧格式需要 pywin32 + 本机安装 Word 才能读取）"
    finally:
        if app is not None:
            try: app.Quit()
            except Exception: pass
        pythoncom.CoUninitialize()


def _extract_xlsx(p: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
    out: list[str] = []
    for name in wb.sheetnames:
        ws = wb[name]
        out.append(f"\n=== Sheet: {name} ===")
        rows_counted = 0
        for row in ws.iter_rows(values_only=True):
            if all(v is None or str(v).strip() == "" for v in row):
                continue
            cells = [str(v).strip() if v is not None else "" for v in row]
            out.append(" | ".join(cells))
            rows_counted += 1
            if rows_counted >= 500:   # 单 sheet 封顶 500 行，避免巨型表格撑爆 LLM 上下文
                out.append(f"... （该 sheet 超过 500 行，已截断）")
                break
        out.append("")
    wb.close()
    return "\n".join(out).strip()


def _extract_xls_old(p: Path) -> str:
    """旧版 .xls —— 用本机 Excel COM 读取 UsedRange。"""
    import pythoncom
    from win32com.client import Dispatch
    pythoncom.CoInitialize()
    app = None
    try:
        app = Dispatch("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        wb = app.Workbooks.Open(str(p), ReadOnly=True)
        try:
            out: list[str] = []
            for ws in wb.Worksheets:
                name = ws.Name
                out.append(f"\n=== Sheet: {name} ===")
                used = ws.UsedRange
                rows = used.Rows.Count
                cols = used.Columns.Count
                # 读值数组（比逐单元格快得多）
                values = used.Value if rows > 0 and cols > 0 else [[]]
                if rows == 1:
                    values = [values]  # 单行时 Excel 返回扁平数组
                rc = 0
                for row in values:
                    cells = []
                    for v in row[:cols]:
                        cells.append(str(v).strip() if v is not None else "")
                    if any(cells):
                        out.append(" | ".join(cells))
                        rc += 1
                        if rc >= 500:
                            out.append("... （该 sheet 超过 500 行，已截断）")
                            break
        finally:
            wb.Close(SaveChanges=False)
        return "\n".join(out).strip()
    except ImportError:
        return "（.xls 旧格式需要 pywin32 + 本机安装 Excel 才能读取）"
    finally:
        if app is not None:
            try: app.Quit()
            except Exception: pass
        pythoncom.CoUninitialize()


def _extract_pptx(p: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(p))
    out: list[str] = []
    for si, slide in enumerate(prs.slides):
        out.append(f"\n=== Slide {si + 1} ===")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = para.text.strip()
                    if t:
                        out.append(t)
            # 表格里的文本
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                    out.append(" | ".join(cells))
    return "\n".join(out).strip()


def _extract_ppt_old(p: Path) -> str:
    """旧版 .ppt —— 用本机 PowerPoint COM 读取。"""
    import pythoncom
    from win32com.client import Dispatch
    pythoncom.CoInitialize()
    app = None
    try:
        app = Dispatch("PowerPoint.Application")
        pres = app.Presentations.Open(str(p), ReadOnly=True, Untitled=False, WithWindow=False)
        try:
            out: list[str] = []
            for si, slide in enumerate(pres.Slides):
                out.append(f"\n=== Slide {si + 1} ===")
                for shape in slide.Shapes:
                    if shape.HasTextFrame and shape.TextFrame.HasText:
                        t = shape.TextFrame.TextRange.Text.strip()
                        if t:
                            out.append(t)
        finally:
            pres.Close()
        return "\n".join(out).strip()
    except ImportError:
        return "（.ppt 旧格式需要 pywin32 + 本机安装 PowerPoint 才能读取）"
    finally:
        if app is not None:
            try: app.Quit()
            except Exception: pass
        pythoncom.CoUninitialize()


def _extract_pdf(p: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    out: list[str] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            out.append(f"\n--- Page {i + 1} ---\n{text.strip()}")
    return "\n".join(out).strip()


def _looks_like_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 4:
        return False
    if any(data.startswith(m) for m in _IMG_READ_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def cmd_file_read(path: str, encoding: str = "utf-8", max_chars: int = 8000,
                  line_numbers: bool = False, start_line: int | None = None,
                  end_line: int | None = None) -> dict:
    """读取文件内容，支持完整读取与区域读取。

    区域读取：传 start_line / end_line（1 起的行号，闭区间）只读指定行范围，
    适合大文件分段查看；line_numbers=True 时行号始终为文件真实行号
    （区域读取时从 start_line 起），方便配合 file.patch 按锚点定点修改。

    图片文件（png/jpg/jpeg/gif/webp/bmp）不返回文本内容（二进制 read_text 会出乱码），
    而是返回 file 字段，由上层 _build_image_followup 把图片以 data URL 注入多模态
    user 消息给模型，模型可直接"看到"图片；前端也通过 _extract_image_refs 拿到缩略图。

    Office 文档（doc/docx/xls/xlsx/ppt/pptx/pdf）不走 UTF-8 二进制解码，
    而是用 python-docx/openpyxl/python-pptx/pypdf 或本机 Office COM 抽取可读纯文本。
    """
    try:
        p = _resolve(path)
        if str(p).lower().endswith(_IMG_READ_EXTS):
            try:
                with open(p, "rb") as _f:
                    _raw = _f.read()
            except Exception as _e:
                return {"error": f"读取图片文件失败：{_e}", "path": str(p)}
            if _looks_like_image_bytes(_raw):
                return {"file": str(p), "path": str(p), "content": "[图片]"}
            return {"error": "文件扩展名是图片类型，但内容魔数不匹配，可能不是有效图片文件", "path": str(p)}

        # —— Office / PDF：用专用库抽取可读文本，不再 UTF-8 解码二进制 ——
        if p.suffix.lower() in _OFFICE_EXTS:
            text = _extract_office_text(p)
            if len(text) > max_chars:
                # Office 文档标题/摘要通常在开头，结论在结尾；保留头尾各一段
                head = int(max_chars * 0.75)
                tail = max_chars - head
                text = (text[:head].rstrip() +
                        f"\n\n...[中间已截断，原文共 {len(text)} 字符]...\n\n" +
                        text[-tail:].lstrip())
            return {"content": text, "path": str(p), "office": True}

        all_lines = p.read_text(encoding=encoding, errors="ignore").splitlines()
        total = len(all_lines)
        region = start_line is not None or end_line is not None
        if region:
            s = 1 if start_line is None else max(1, int(start_line))
            e = total if end_line is None else min(total, int(end_line))
            if s > total:
                return {"error": f"start_line={s} 超出文件范围（文件共 {total} 行）",
                        "path": str(p), "total_lines": total}
            if s > e:
                return {"error": f"start_line={s} > end_line={e}，区间无效",
                        "path": str(p), "total_lines": total}
            lines = all_lines[s - 1:e]
        else:
            s, e, lines = 1, total, all_lines
        text = "\n".join(lines)
        if line_numbers:
            # 带文件真实行号（区域读取时行号从 start_line 起，宽度按全文总行数自适应）
            pad = len(str(total))
            text = "\n".join(f"{s + i:>{pad}}\t{ln}" for i, ln in enumerate(lines))
        if len(text) > max_chars:
            text = f"...truncated ({len(text)} chars)...\n" + text[-max_chars:]
        result = {"content": text, "path": str(p)}
        if region:
            result["region"] = {"start_line": s, "end_line": e, "total_lines": total}
        return result
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_write(path: str, content: str, mode: str = "overwrite") -> dict:
    try:
        p = _resolve(path)
        _err = _scope_guard(str(p), "写文件")
        if _err:
            return {"error": _err, "path": str(p)}
        # 快照（无论 overwrite 还是 append 都记录）
        _snapshot(str(p), "file.write")
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            p.write_text(p.read_text(encoding="utf-8") + content, encoding="utf-8")
        else:
            p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(p), "size": p.stat().st_size}
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_delete(path: str, recursive: bool = False) -> dict:
    try:
        p = _resolve(path)
        _err = _scope_guard(str(p), "删除文件")
        if _err:
            return {"error": _err, "path": str(p)}
        # 快照（记录被删前状态）
        _snapshot(str(p), "file.delete", snapshot_kind="delete")
        if p.is_file(): p.unlink()
        elif p.is_dir():
            if recursive: shutil.rmtree(p)
            else: return {"error": f"directory exists, set recursive=true", "path": str(p)}
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_replace(path: str, old: str, new: str, max_replacements: int = -1) -> dict:
    try:
        p = _resolve(path)
        _err = _scope_guard(str(p), "修改文件")
        if _err:
            return {"error": _err, "path": str(p)}
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return {"error": "old 文本在文件中未找到，未做任何修改；请先 file.read 确认原文",
                    "path": str(p)}
        _snapshot(str(p), "file.replace")
        replaced = count if max_replacements <= 0 else min(count, max_replacements)
        text = text.replace(old, new, -1 if max_replacements <= 0 else max_replacements)
        p.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(p), "replaced": replaced,
                "occurrences": count, "new_size": p.stat().st_size}
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_patch(path: str, old: str, new: str, mode: str = "replace") -> dict:
    """按锚点定位的定点编辑：只改锚点所在位置，不动其他任何内容。

    mode：
      replace      —— 把 old 片段换成 new（old/new 都可以跨行）
      delete       —— 删掉 old 片段
      insert_after —— 在锚点最后一行之后插入 new（按完整行追加）
      insert_before—— 在锚点第一行之前插入 new（按完整行追加）

    锚点匹配规则：
      - old 可以是单行片段，也可以跨行（含 \\n）；比对前统一换行符，CRLF 文件也能匹配；
      - 只替换第一处命中，occurrences 会告知总命中数，便于判断锚点是否足够唯一；
      - 写入时保留文件原有换行风格（CRLF/LF），不会把 LF 文件悄悄改成 CRLF。
    """
    try:
        p = _resolve(path)
        _err = _scope_guard(str(p), "修改文件")
        if _err:
            return {"error": _err, "path": str(p)}
        if not old:
            return {"error": "old（锚点）不能为空", "path": str(p)}
        if mode not in ("replace", "delete", "insert_after", "insert_before"):
            return {"error": f"unknown mode: {mode}, 可选 replace/delete/insert_after/insert_before",
                    "path": str(p)}

        # 统一换行符后做匹配（LLM 传 \n，磁盘上可能是 \r\n）
        norm_old = old.replace("\r\n", "\n").replace("\r", "\n")
        norm_new = new.replace("\r\n", "\n").replace("\r", "\n")
        if mode == "replace" and norm_old == norm_new:
            return {"error": "old 与 new 完全相同，不会产生任何改动；"
                             "如需调整缩进/空白，请先 file.read 确认原文并让 new 真正不同",
                    "path": str(p)}

        _snapshot(str(p), "file.patch")
        raw_bytes = p.read_bytes()
        had_crlf = b"\r\n" in raw_bytes
        text = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

        idx = text.find(norm_old)
        if idx < 0:
            hint = ("（跨行锚点未匹配：请核对每一行的缩进、空行和首尾空白是否与文件完全一致）"
                    if "\n" in norm_old else "")
            return {"error": f"anchor not found: 锚点文本在文件中找不到{hint}，先 file.read 确认原文",
                    "path": str(p)}

        occurrences = text.count(norm_old)
        line_no = text.count("\n", 0, idx) + 1

        if mode == "replace":
            result_text = text[:idx] + norm_new + text[idx + len(norm_old):]
        elif mode == "delete":
            result_text = text[:idx] + text[idx + len(norm_old):]
        elif mode == "insert_after":
            end = idx + len(norm_old)
            if end < len(text) and text[end] == "\n":
                insert_at = end + 1
            else:
                nl = text.find("\n", end)
                insert_at = len(text) if nl < 0 else nl + 1
            if insert_at >= len(text) and text and not text.endswith("\n"):
                result_text = text + "\n" + norm_new + "\n"
            else:
                result_text = text[:insert_at] + norm_new + "\n" + text[insert_at:]
        else:  # insert_before
            line_start = text.rfind("\n", 0, idx) + 1
            result_text = text[:line_start] + norm_new + "\n" + text[line_start:]

        changed = result_text != text
        final = result_text.replace("\n", "\r\n") if had_crlf else result_text
        p.write_bytes(final.encode("utf-8"))

        after_lines = result_text.split("\n")
        line_text = after_lines[line_no - 1][:300] if 0 <= line_no - 1 < len(after_lines) else ""
        result = {"ok": True, "path": str(p), "mode": mode, "changed": changed,
                  "line": line_no, "line_text": line_text,
                  "occurrences": occurrences,
                  "newline": "CRLF" if had_crlf else "LF",
                  "new_size": p.stat().st_size}
        if occurrences > 1:
            result["note"] = f"锚点在文件中命中 {occurrences} 处，本次只改了第 {line_no} 行；如改错位置请用更长的锚点重试"
        return result
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_structure(root: str = ".", depth: int = 3, max_items: int = 200) -> dict:
    try:
        # LLM 省略 root 时默认 "."，此时按 workdir 解析
        root_p = _resolve(root)
        # workdir 目录不存在则创建，避免写文件时报父目录不存在
        root_p.mkdir(parents=True, exist_ok=True)
        out = []; total = 0
        for cur, dirs, files in os.walk(root_p):
            if total >= max_items: break
            rel = Path(cur).relative_to(root_p)
            level = 0 if str(rel) == "." else len(rel.parts)
            if level >= depth: dirs.clear(); continue
            indent = "  " * level
            out.append(f"{indent}{Path(cur).name}/"); total += 1
            for f in sorted(files):
                if total >= max_items: break
                out.append(f"{indent}  {f}"); total += 1
        return {"tree": "\n".join(out), "root": str(root_p)}
    except Exception as e:
        return {"error": str(e)}


# —— 搜索辅助：二进制文件探测 ——
def _is_binary_file(path: Path, max_check: int = 8192) -> bool:
    """快速判断文件是否为二进制：前 max_check 字节含 NUL 即视为二进制。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(max_check)
        return b"\x00" in chunk
    except Exception:
        return True  # 读不出来的当二进制跳过


# 新增文件内容搜索命令（对齐 Trae Work 搜索工具：行号 / 上下文 / 封顶 / 跳过二进制与大文件）
def cmd_file_search(pattern: str,
                    path: str = ".",
                    glob: str = "**/*",
                    i: bool = False,
                    abs: bool = False,
                    context: int = 0,
                    max_matches: int = 50,
                    max_files: int = 20,
                    whole_word: bool = False,
                    max_file_size: int = 1048576) -> dict:
    """在指定目录递归搜索文件内容（正则/关键字），返回命中文件路径 + 行号 + 命中行文本。

    改进点（对齐 Trae Work 搜索工具）：
      - 预编译正则一次，不再每个文件重复编译；
      - 自动跳过二进制文件与大于 max_file_size 的文件；
      - 每个命中带行号；可选 context 行上下文（前后各 context 行）；
      - max_matches / max_files 双重封顶，避免结果撑爆 LLM 上下文；
      - whole_word 模式按词边界 (\\b...\\b) 匹配。

    返回 {matches: [{path, hits:[{line, text, (context?)}]}], count,
          files_searched, files_skipped, truncated}
    """
    try:
        root = _resolve(path)
        pat = rf"\b{pattern}\b" if whole_word else pattern
        try:
            comp = re.compile(pat, re.MULTILINE | (re.IGNORECASE if i else 0))
        except re.error as e:
            return {"error": f"正则表达式非法: {e}"}

        matches: list[dict] = []
        total_hits = 0
        files_searched = 0
        files_skipped = 0
        truncated = False
        ctx = max(0, int(context))
        # path 直接指向单个文件时，只搜这一个文件（rglob 在文件上会返回空结果）
        single_file = root.is_file()
        candidates = [root] if single_file else root.rglob(glob)

        for p in candidates:
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > max_file_size:
                    files_skipped += 1
                    continue
            except Exception:
                files_skipped += 1
                continue
            if _is_binary_file(p):
                files_skipped += 1
                continue
            files_searched += 1
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            hits: list[dict] = []
            for idx, ln in enumerate(lines):
                if comp.search(ln):
                    hit = {"line": idx + 1, "text": ln[:500]}
                    if ctx > 0:
                        start = max(0, idx - ctx)
                        end = min(len(lines), idx + ctx + 1)
                        snippet = []
                        for j in range(start, end):
                            marker = ">>" if j == idx else "  "
                            snippet.append(f"{marker}{j + 1:>5}: {lines[j][:300]}")
                        hit["context"] = "\n".join(snippet)
                    hits.append(hit)
                    total_hits += 1
                    if total_hits >= max_matches:
                        truncated = True
                        break
            if hits:
                matches.append({
                    "path": str(p) if (abs or single_file) else str(p.relative_to(root)),
                    "hits": hits,
                })
                if len(matches) >= max_files:
                    truncated = True
            if truncated:
                break
        return {
            "matches": matches,
            "count": total_hits,
            "files_searched": files_searched,
            "files_skipped": files_skipped,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


def cmd_file_find(pattern: str,
                  path: str = ".",
                  type: str = "any",
                  max_results: int = 100,
                  i: bool = True) -> dict:
    """按文件/目录名查找路径（对齐 Trae Work 的 #File / #Folder）。

    pattern 支持 glob 通配符（如 *.py）或纯子串（如 main → 自动包成 *main*）；
    type = file | dir | any；返回相对 root 的匹配路径列表，最多 max_results 条。
    适合按文件名定位（而非内容），先 find 拿路径再 read / search。
    """
    try:
        import fnmatch
        root = _resolve(path)
        glob_pat = pattern if any(ch in pattern for ch in "*?[") else f"*{pattern}*"
        results: list[str] = []
        # path 直接指向文件/目录本身时也要参与匹配（rglob 不会返回 root 自身）
        single = root.is_file()
        iterable = [root] if single else root.rglob("*")
        for p in iterable:
            if p == root and not single:
                continue
            name = p.name
            ok = fnmatch.fnmatch(name.lower(), glob_pat.lower()) if i else fnmatch.fnmatch(name, glob_pat)
            if not ok:
                continue
            if type == "file" and not p.is_file():
                continue
            if type == "dir" and not p.is_dir():
                continue
            results.append(str(p) if single else str(p.relative_to(root)))
            if len(results) >= max_results:
                break
        return {
            "matches": results,
            "count": len(results),
            "truncated": len(results) >= max_results,
        }
    except Exception as e:
        return {"error": str(e)}


def _tokenize_query(query: str) -> list[str]:
    """简单分词：按非字母数字下划线汉字以外字符切分，转小写，过滤长度<2 的噪声 token。"""
    raw = re.split(r"[^\w\u4e00-\u9fff]+", (query or "").lower())
    return [t for t in raw if len(t) >= 2]


def cmd_workspace_search(query: str,
                         path: str = ".",
                         glob: str = "**/*",
                         max_files: int = 10,
                         max_matches_per_file: int = 3,
                         max_file_size: int = 1048576,
                         i: bool = True) -> dict:
    """工作区关键词检索（对齐 Trae Work 的 #Workspace）。

    无向量索引时使用轻量词频(TF)打分：把 query 分词，对每个文件统计关键词命中数，
    按得分降序返回 top-N 文件及其最佳匹配行（带行号），让智能体在工作区里
    "按语义关键词定位最相关的几处代码/文档"。

    适用：不确定具体文件、需要在多文件间定位与 query 最相关的位置时。
    """
    try:
        root = _resolve(path)
        tokens = _tokenize_query(query)
        if not tokens:
            return {"error": "query 至少需要包含一个长度≥2 的关键词"}
        line_re = re.compile("|".join(re.escape(t) for t in tokens),
                             re.IGNORECASE if i else 0)

        scored: list[tuple[int, str, list[dict]]] = []
        files_searched = 0
        files_skipped = 0
        for p in root.rglob(glob):
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > max_file_size:
                    files_skipped += 1
                    continue
            except Exception:
                files_skipped += 1
                continue
            if _is_binary_file(p):
                files_skipped += 1
                continue
            files_searched += 1
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            score = 0
            snippets: list[dict] = []
            for idx, ln in enumerate(lines):
                found = line_re.findall(ln)
                if found:
                    # 行内命中关键词按 token 去重计数一次（防同一词反复刷分）
                    score += len(set(t.lower() for t in found))
                    if len(snippets) < max_matches_per_file:
                        snippets.append({"line": idx + 1, "text": ln[:300]})
            if score > 0:
                scored.append((score, str(p.relative_to(root)), snippets))

        scored.sort(key=lambda x: x[0], reverse=True)
        truncated = len(scored) > max_files
        top = scored[:max_files]
        results = [{"path": p, "score": s, "snippets": snip} for s, p, snip in top]
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "files_searched": files_searched,
            "files_skipped": files_skipped,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


def cmd_skill_invoke(skill_name: str) -> dict:
    """显式调用指定的技能。返回技能内容供LLM参考使用。

    渐进式读取：
      - 一般大小正文（如 lx-music-control）→ 完全返回正文；
      - 较大正文（如 pptx）→ 只返回目录 toc + 开头预览，
        具体章节用 cmd_skill_read_section 按需读取。
    技能内容不会自动添加到系统提示词中，仅在通过此命令显式调用时使用。
    """
    try:
        # 确保技能注册表是最新的
        from skills import SKILL_REGISTRY, load_skills, skill_toc, FULL_READ_LIMIT, PROGRESSIVE_PREVIEW_CHARS
        load_skills()  # 热重载技能

        # 查找技能
        if skill_name not in SKILL_REGISTRY:
            # 尝试不同的命名格式
            alt_name = skill_name.replace("_", "-") if "_" in skill_name else skill_name.replace("-", "_")
            if alt_name in SKILL_REGISTRY:
                skill_name = alt_name
            else:
                return {"error": f"未找到技能: {skill_name}"}

        skill_data = SKILL_REGISTRY[skill_name]

        # 构建返回结果
        result = {
            "skill_name": skill_name,
            "skill_label": skill_data.get("label", skill_name),
            "description": skill_data.get("description", ""),
        }

        # 技能文件夹绝对路径：让智能体用绝对路径定位技能内部文件（scripts/references/assets），
        # 避免因设置了工作区（WORKDIR）导致相对路径解析到错误位置。
        skill_dir = skill_data.get("_source_dir", "")
        result["skill_dir"] = skill_dir
        result["path_note"] = (
            f"本技能文件夹绝对路径：{skill_dir or '（未知）'}。"
            "技能正文中的脚本/资源路径均位于该文件夹下，"
            "读取或运行技能内部文件时请用 skill_dir 拼接绝对路径，切勿按当前工作区（WORKDIR）的相对路径解析。"
        )

        prompt = skill_data.get("prompt", "")
        result["body_size"] = len(prompt)
        if len(prompt) <= FULL_READ_LIMIT:
            # 一般大小正文：完全读取
            result["mode"] = "full"
            result["prompt"] = prompt
        else:
            # 较大正文：渐进式读取 —— 先给目录，具体章节用 skill.read_section 按需拉取
            result["mode"] = "progressive"
            result["prompt"] = ""  # 不再全文返回，避免大正文一次撑爆上下文
            result["toc"] = [
                {"title": t["title"], "level": t["level"], "line": t["line"]}
                for t in skill_toc(prompt)
            ]
            result["preview"] = prompt[:PROGRESSIVE_PREVIEW_CHARS]
            result["read_note"] = (
                f"本技能正文较长（{len(prompt)} 字符），已按渐进式读取只返回目录，未返回全文。"
                "需要某个章节时，用 skill.read_section 传入 skill_name 与 section（章节标题，从上方 toc 选取）"
                "读取该章节；技能目录内的其他参考文件（如 editing.md / pptxgenjs.md）可直接用 file.read 读取"
                "（绝对路径由 skill_dir 提供）。不要试图从 preview 推断未给出的章节内容。"
            )

        # 如果技能有scripts目录，也可以提供执行入口信息
        if skill_data.get("_subdirs", {}).get("scripts"):
            result["has_scripts"] = True
            result["script_note"] = "此技能包含可执行脚本，如需执行请参考技能文档（脚本位于 skill_dir/scripts 下）"

        return result

    except Exception as e:
        return {"error": f"调用技能失败: {str(e)}"}


def cmd_skill_read_section(skill_name: str, section: str, max_chars: int = 6000) -> dict:
    """渐进式读取：按章节标题读取较大技能（如 pptx）正文的指定部分。

    章节标题从 skill.invoke 返回的 toc 中选取；找不到对应章节时返回错误提示。
    """
    try:
        from skills import SKILL_REGISTRY, load_skills, read_skill_section
        load_skills()  # 热重载技能

        if skill_name not in SKILL_REGISTRY:
            # 尝试不同的命名格式
            alt_name = skill_name.replace("_", "-") if "_" in skill_name else skill_name.replace("-", "_")
            if alt_name in SKILL_REGISTRY:
                skill_name = alt_name
            else:
                return {"error": f"未找到技能: {skill_name}"}

        data = SKILL_REGISTRY[skill_name]
        content, meta = read_skill_section(data.get("prompt", ""), section)
        if not content:
            return {"error": f"未找到章节: {section}（请从 skill.invoke 返回的 toc 中选取章节标题）", "skill_name": skill_name}

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars] + f"\n…（本节过长，已截断，剩余 {len(content) - max_chars} 字符，可调大 max_chars 或缩小 section 范围）…"
        return {
            "skill_name": skill_name,
            "section": meta["title"],
            "level": meta["level"],
            "line": meta["line"],
            "truncated": truncated,
            "content": content,
        }
    except Exception as e:
        return {"error": f"读取技能章节失败: {str(e)}"}


def cmd_cmd_run(command: str, cwd: str = ".", timeout: int = 60, shell: bool = True,
                background: bool | None = None, shell_type: str = "auto") -> dict:
    """执行一条系统命令。

    长运行自动后台化规则：
      - 若显式传 background=True → 一定后台启动，立即返回 pid
      - 若显式传 background=False → 强制前台（阻塞直到退出或超时）
      - 若省略 / null（默认）：自动检测命令关键词（http.server / uvicorn / node / vite / watch 等），
        命中则后台启动；否则前台执行

    shell_type：显式指定执行解释器（仅 Windows 生效），取值 auto / cmd / powershell，默认 auto：
      - auto：沿用 _is_powershell_command 自动判断，命中 PowerShell 语法自动转 PowerShell（保持原行为）
      - cmd：强制走 cmd.exe，不做 PowerShell 转换
      - powershell：强制走 PowerShell（用 -EncodedCommand 包装，多行脚本 / $变量 / cmdlet 均可正确执行）
    """
    try:
        # 显式传了 cwd 就用它，否则回落到 workdir 或 CWD
        if cwd == ".":
            wd = _effective_workdir()
            cwd_eff = wd if wd else os.getcwd()
        else:
            cwd_eff = str(_resolve(cwd))

        # 工作范围：命令的执行目录也必须落在允许范围内（scope只管「落盘/起进程」类操作）
        _err = _scope_guard(cwd_eff, "运行命令")
        if _err:
            return {"error": _err, "command": command, "cwd": cwd_eff}

        # —— 判断是否需要后台启动 ——
        # 两类情况都要后台/立即返回：
        #   (a) 常驻服务（uvicorn/node/redis...）—— 会一直运行不退出
        #   (b) GUI 启动（start/浏览器/explorer...）—— 若走 subprocess.run + capture_output，
        #       GUI 子进程会继承 pipe 句柄，Python 永远等不到输出关闭 → 卡住
        # 两种判断都用原始命令，避免 PowerShell 包装后关键词不可见
        should_bg = background
        if should_bg is None:
            should_bg = _is_long_running(command) or _is_gui_launch(command)

        # 解释器选择（仅 Windows）：auto 沿用自动判断，cmd 强制 cmd.exe，powershell 强制 PowerShell。
        # 显式 powershell 时用 -EncodedCommand 包装，多行脚本也能整体传入（不受 shell 逐行解析影响）。
        exec_command = command
        st = (shell_type or "auto").strip().lower()
        if st not in ("auto", "cmd", "powershell"):
            st = "auto"
        if os.name == "nt" and shell:
            lower_cmd = command.strip().lower()
            explicit_ps = lower_cmd.startswith("powershell") or lower_cmd.startswith("pwsh")
            if st == "powershell":
                # 命令本身已是 powershell/pwsh 入口则原样执行，避免二次包装
                if not explicit_ps:
                    exec_command = _wrap_powershell(command)
            elif st == "auto" and _is_powershell_command(command):
                # Windows 下 shell=True 走 cmd.exe：智能体常用的 PowerShell 写法会直接报
                # 「不是内部或外部命令」，这里自动转成 PowerShell 执行
                exec_command = _wrap_powershell(command)
            # st == "cmd"：原样走 cmd.exe

        if should_bg:
            return _start_bg_process(exec_command, cwd_eff, shell=shell,
                                     classify_command=command)

        # 显式要求前台阻塞（background=false）：保持原语义，等进程结束或超时
        if background is False:
            r = subprocess.run(exec_command, shell=shell, capture_output=True,
                               cwd=cwd_eff, timeout=timeout)
            return {"stdout": _decode_console(r.stdout)[-4000:],
                    "stderr": _decode_console(r.stderr)[-2000:],
                    "returncode": r.returncode, "command": command, "cwd": cwd_eff}

        # 自动模式：前台优先，长驻/GUI 进程自动转后台，避免卡住智能体
        return _run_foreground_auto(exec_command, cwd_eff, shell=shell,
                                    timeout=timeout, classify_command=command)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout（{timeout}s）—— 这条命令似乎在等待某些输出，"
                         f"如果是服务启动/文件监听等长运行进程，可用 background=true 参数后台启动，"
                         f"或让系统自动识别长运行关键词",
                "command": command, "hint": "如果这是长运行服务，请让它以后台方式启动"}
    except Exception as e:
        return {"error": str(e), "command": command}


# ---- JSON Schema 描述（OpenAI function calling 格式）----
COMMAND_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file.read",
            "description": "读取指定文件的内容。默认返回完整内容；也可以传 start_line / end_line 只读指定行区间（大文件分段查看）。支持的文件类型：\n\n  1) **文本文件**：代码、配置、markdown、JSON、CSV、日志等（UTF-8/GBK）；\n  2) **图片**（png/jpg/jpeg/gif/webp/bmp）：不返回文本，返回 file 字段由上层做视觉注入；\n  3) **Office 文档**（.docx / .doc / .xlsx / .xls / .pptx / .ppt / .pdf）：自动抽取可读纯文本（段落+表格，不走二进制解码，不会有乱码），可直接用于总结、翻译、提取数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对或相对路径，如 ./main.py"},
                    "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
                    "max_chars": {"type": "integer", "description": "最多返回的字符数，超过会截断"},
                    "line_numbers": {"type": "boolean", "description": "是否在每行前加文件真实行号（区域读取时从 start_line 起；配合 file.patch 按行定点编辑时建议开启）"},
                    "start_line": {"type": "integer", "description": "可选：起始行号（1 起），只读该行及之后的内容；与 end_line 配合构成闭区间 [start_line, end_line]，适合大文件分段查看"},
                    "end_line": {"type": "integer", "description": "可选：结束行号（1 起），与 start_line 配合构成闭区间；单独给 end_line 时表示从第 1 行读到此行"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill.invoke",
            "description": "显式调用指定的技能。技能内容不会自动添加到系统提示词中，仅在通过此命令显式调用时返回。一般大小正文（如 lx-music-control）会返回完整正文；较大正文（如 pptx）采用渐进式读取——只返回目录 toc 与开头 preview，不返回全文，需要用 skill.read_section 按需读取具体章节。返回结果还包含技能文件夹绝对路径（skill_dir），读取技能内部文件时必须使用该绝对路径，不能用工作区相对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "要调用的技能名称（对应AppData/skills中的文件夹名）"}
                },
                "required": ["skill_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill.read_section",
            "description": "按章节标题渐进式读取较大技能（如 pptx）正文的指定章节。当 skill.invoke 返回 mode=progressive 时，技能全文不会一次给出，本命令用于按需读取 toc 中的具体章节（section 从 toc 的 title 中选取）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称（与 skill.invoke 的 skill_name 一致）"},
                    "section": {"type": "string", "description": "章节标题，可从 skill.invoke 返回的 toc 中选取（支持全文/前缀/包含匹配）"},
                    "max_chars": {"type": "integer", "description": "单次最多返回的字符数，默认 6000"}
                },
                "required": ["skill_name", "section"]
            }
        }
    },




    {
        "type": "function",
        "function": {
            "name": "file.write",
            "description": "新建或覆盖写入一个文本文件。会自动创建父目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "overwrite=覆盖写入；append=追加到文件末尾"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.delete",
            "description": "删除文件或目录。删除目录需设置 recursive=true。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的路径"},
                    "recursive": {"type": "boolean", "description": "删除目录时是否递归"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.replace",
            "description": "在文件中查找并替换指定文本片段（全局替换，可用 max_replacements 限制次数）。old 未命中时返回错误、不会静默成功。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old": {"type": "string", "description": "要被替换的原文本"},
                    "new": {"type": "string", "description": "替换成的新文本"},
                    "max_replacements": {"type": "integer", "description": "最多替换次数，-1=全部替换"}
                },
                "required": ["path", "old", "new"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.patch",
            "description": "定点修改已有文件：以锚点 old 定位，只改或插入那一处，绝不重写整个文件。先 file.read 拿到锚点原文，再按 mode 选择替换该处 / 删除该处 / 在其前后插入新内容。锚点可单行也可跨行（会统一 CRLF/LF 后匹配），写入保留原换行风格；返回 changed/line_text 可用于确认改动是否真正落盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old": {"type": "string", "description": "锚点：文件中必须存在的原文片段，可单行也可跨行；请尽量复制 file.read 返回的原文（含缩进）以保证唯一命中"},
                    "new": {"type": "string", "description": "新内容：replace/delete 时为替换后文本（delete 可为空字符串）；insert_* 时要插入的完整新行（多行可自带换行）"},
                    "mode": {"type": "string", "enum": ["replace", "delete", "insert_after", "insert_before"],
                             "description": "replace=用 new 替换锚点行内 old 片段；delete=删除锚点行内 old 片段；insert_after=在锚点整行之后插入 new；insert_before=在锚点整行之前插入 new"}
                },
                "required": ["path", "old", "new"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.structure",
            "description": "获取指定目录的文件树结构，返回层级缩进的列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "根目录，默认为当前目录 '.'"},
                    "depth": {"type": "integer", "description": "递归深度，默认3"},
                    "max_items": {"type": "integer", "description": "最多返回条目数，默认200"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.search",
            "description": "按正则/关键字递归搜索文件内容（对齐 Trae Work 搜索工具）。返回命中文件路径 + 行号 + 命中行文本，可选上下文行。自动跳过二进制与大文件，结果按 max_matches / max_files 双重封顶，不会撑爆上下文。适合先定位再 file.read / file.patch。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜索的正则表达式或关键字，如 'def main' 或 'TODO'"},
                    "path": {"type": "string", "description": "搜索根目录，默认当前工作目录（相对路径自动解析到工作目录）"},
                    "glob": {"type": "string", "description": "限定搜索的文件 glob 模式，默认 '**/*'；例 '**/*.py' 只搜 Python 文件"},
                    "i": {"type": "boolean", "description": "忽略大小写搜索，默认 false"},
                    "abs": {"type": "boolean", "description": "返回绝对路径，默认相对搜索根目录"},
                    "context": {"type": "integer", "description": "每个命中行前后显示的上下文行数，默认 0（不显示）"},
                    "max_matches": {"type": "integer", "description": "最多返回的命中条数（跨文件累计），默认 50"},
                    "max_files": {"type": "integer", "description": "最多返回的命中文件数，默认 20"},
                    "whole_word": {"type": "boolean", "description": "按词边界 (\\b...\\b) 匹配，默认 false"},
                    "max_file_size": {"type": "integer", "description": "跳过大于该字节数的文件，默认 1048576（1MB）"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file.find",
            "description": "按文件/目录名查找路径（对齐 Trae Work 的 #File / #Folder）。pattern 支持 glob 通配符（如 '*.py'）或纯子串（如 'main' 自动包成 *main*）。返回相对根目录的匹配路径列表，适合按文件名定位而非内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "文件名 glob 或子串，如 '*.py' 或 'main'"},
                    "path": {"type": "string", "description": "搜索根目录，默认当前工作目录"},
                    "type": {"type": "string", "enum": ["file", "dir", "any"], "description": "只查文件/目录/全部，默认 any"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 100"},
                    "i": {"type": "boolean", "description": "忽略大小写，默认 true"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace.search",
            "description": "工作区关键词检索（对齐 Trae Work 的 #Workspace）。用自然语言关键词在工作区定位最相关的代码/文档位置，基于轻量词频打分返回 top-N 文件及其最佳匹配行（带行号）。不确定具体文件、需要全局定位时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言关键词或描述，如 '用户登录鉴权' 或 'cache invalidation'"},
                    "path": {"type": "string", "description": "搜索根目录，默认当前工作目录"},
                    "glob": {"type": "string", "description": "限定搜索的文件 glob 模式，默认 '**/*'"},
                    "max_files": {"type": "integer", "description": "最多返回的文件数，默认 10"},
                    "max_matches_per_file": {"type": "integer", "description": "每个文件最多展示的命中行数，默认 3"},
                    "max_file_size": {"type": "integer", "description": "跳过大于该字节数的文件，默认 1048576（1MB）"},
                    "i": {"type": "boolean", "description": "忽略大小写，默认 true"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cmd.run",
            "description": (
                "在系统 shell 中运行一条命令（当前环境为 Windows PowerShell 5.1）。"
                "系统会自动识别长运行命令并以后台方式启动（如 http.server / uvicorn / node / vite / watch 等），"
                "后台启动时立即返回 pid、端口探活结果，不会阻塞智能体。"
                "短命令（如 dir / pip install）正常前台执行。"
                "也可显式传 background=true 强制后台，或 background=false 强制前台。"
                "\n\n⚠️ 当前是 Windows PowerShell 5.1 环境："
                "\n- 不要用 Linux 命令：pwd/cat/ls/grep/find/mkdir -p 均不存在或行为不符，改用 cd/Get-Content/Get-ChildItem/Select-String/New-Item -ItemType Directory -Force"
                "\n- dir / cd / where.exe 可用（cmd.exe 语法在 PowerShell 也能跑）"
                "\n- 管道符号用 | 即可；PowerShell 5 不支持 &&，用分号 ; 串联命令"
                "\n- npm/node 前需确保 PATH 含 E:\\app\\node-v20.18.0-win-x64（见项目规则）"
                "\n- 执行 PowerShell 脚本（含 $变量、cmdlet、多行）时，建议显式传 shell_type=powershell"
                "\n慎用，避免破坏性命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "完整命令字符串，如 dir /a"},
                    "cwd": {"type": "string", "description": "工作目录，默认当前目录"},
                    "timeout": {"type": "integer", "description": "前台执行超时秒数，默认60"},
                    "shell": {"type": "boolean", "description": "是否通过 shell 执行，默认true"},
                    "background": {"type": "boolean", "description": "可选：true=强制后台启动，false=强制前台；省略则自动识别长运行关键词"},
                    "shell_type": {"type": "string", "enum": ["auto", "cmd", "powershell"], "description": "执行解释器选择（仅 Windows 生效）：auto=自动识别（默认，命中 PowerShell 语法自动转 PowerShell）；cmd=强制 cmd.exe；powershell=强制 PowerShell（自动包装，支持多行脚本 / $变量 / cmdlet）。执行 PowerShell 脚本时建议显式传 powershell。"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cmd.bg_list",
            "description": "列出当前所有由 cmd.run 后台启动的进程状态（pid、命令、运行时长、日志路径）。【不需要任何参数，留空即可】",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cmd.bg_stop",
            "description": "停止一个后台进程。先调用 cmd.bg_list 查到 pid，再用本命令停止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "要停止的后台进程 PID（从 cmd.bg_list 或 cmd.run 后台返回里拿）"},
                    "force": {"type": "boolean", "description": "是否强制杀掉（SIGKILL / taskkill /F），默认 false"}
                },
                "required": ["pid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cmd.bg_log",
            "description": "查看后台进程的日志尾部（默认最后 50 行），用于排查后台服务是否启动成功。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "后台进程 PID"},
                    "tail": {"type": "integer", "description": "查看最后多少行，默认 50"}
                },
                "required": ["pid"]
            }
        }
    },
]

# 命令中文标签（前端能力标签与对话工具行共用，label 仅用于界面展示，不参与工具调用）
#  emoji 要贴合动作：读→打开、写→✍️、删→🗑、运行→🚀、创建→🤖/🎨/✨、连接→🔗…
COMMAND_CN_LABELS = {
    "file.read": "📖 读文件",
    "file.write": "✍️ 写文件",
    "file.delete": "🗑 删除文件",
    "file.replace": "🔄 替换文本",
    "file.patch": "🩹 补丁编辑",
    "file.structure": "📁 文件树",
    "file.search": "🔍 搜索内容",
    "file.find": "🔎 查找文件",
    "workspace.search": "🌐 工作区检索",
    "agent.smart_search": "🧠 智能搜索",
    "agent.spawn": "🐣 派生子智能体",
    "skill.invoke": "🔧 调用技能",
    "skill.read_section": "📑 读取技能章节",
    "cmd.run": "🚀 运行命令",
    "cmd.bg_list": "📋 后台进程",
    "cmd.bg_stop": "🛑 停止进程",
    "cmd.bg_log": "📜 进程日志",
    "cluster.get_agent": "🔍 查看智能体",
    "cluster.search_agents": "🔎 搜索智能体",
    "cluster.list_capabilities": "🧩 查询可用能力",
    "cluster.create_agent": "🤖 创建智能体",
    "cluster.update_agent": "✏️ 更新智能体",
    "cluster.delete_agent": "🗑 删除智能体",
    "cluster.get_canvas": "👁 查看画布",
    "cluster.search_canvases": "🔎 搜索画布",
    "cluster.create_canvas": "🌟 创建画布",
    "cluster.delete_canvas": "🗑 删除画布",
    "cluster.add_node": "📌 添加节点",
    "cluster.remove_node": "❌ 移除节点",
    "cluster.connect": "🔗 连接节点",
    "cluster.disconnect": "✂️ 断开连线",
    "cluster.list_rules": "📜 列出规章制度",
    "cluster.create_rule": "📋 新增规章制度",
    "cluster.update_rule": "✏️ 修改规章制度",
    "cluster.delete_rule": "🗑 删除规章制度",
    "cluster.list_groups": "📁 列出分组",
    "cluster.create_group": "📂 新建分组",
    "cluster.update_group": "✏️ 重命名分组",
    "cluster.delete_group": "🗑 删除分组",
    "cluster.assign_agent_group": "🏷 归纳分组",
    "cluster.create_conversation": "💬 新建会话",
    "cluster.update_conversation": "✏️ 编辑会话",
    "cluster.continue_conversation": "▶️ 继续会话",
    "cluster.stop_agent": "🛑 停止智能体",
    "cluster.list_running": "📡 查看运行中",
    "cluster.list_conversations": "🗂 列出会话",
    "cluster.search_conversations": "🔎 搜索会话",
    "cluster.get_conversation": "🗨️ 查看对话记录",
    "cluster.handoff": "🤝 A2A 移交",
    "cluster.send_message": "📨 发送消息",
}

# 保留旧字典格式兼容（label 用中文，未覆盖的仍用原名）
COMMAND_META = {t["function"]["name"]: {
    "label": COMMAND_CN_LABELS.get(t["function"]["name"], t["function"]["name"]),
    "params": list(t["function"]["parameters"]["properties"].keys()),
} for t in COMMAND_TOOLS}


def build_plugin_tool(name: str, meta: dict) -> dict:
    """把插件元数据转成 OpenAI tool 格式"""
    properties = {p: {"type": "string", "description": f"参数 {p}"} for p in meta.get("params", [])}
    # 智能参数类型推断
    int_params = {"port", "timeout", "depth", "max_chars"}
    bool_params = {"use_ssl", "recursive"}
    for p in int_params:
        if p in properties: properties[p]["type"] = "integer"
    for p in bool_params:
        if p in properties: properties[p]["type"] = "boolean"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": meta.get("description", meta.get("SKILL_DESC", "")) or meta.get("desc", ""),
            "parameters": {"type": "object", "properties": properties, "required": []}
        }
    }


def filter_tools(command_names: list, plugin_names: list, plugin_registry: dict,
                 skills: list | None = None, agent: dict | None = None) -> list:
    """根据 agent 的能力列表，构建要传给 LLM 的 tools。

    所有 tool 的 parameters 都强制加 additionalProperties=False，
    明确禁止 LLM 传 schema 未声明的多余字段（如凭空加 root / 空键等）。

    agent 传入时额外合并「外部工具提供者」注册的工具（MCP 客户端等拓展通过
    core.external_tools 注册；schema 原样保留，避免破坏 MCP 的 $defs/additionalProperties）。
    """
    result = []
    cmd_names = set(command_names or [])
    for t in COMMAND_TOOLS:
        if t["function"]["name"] in cmd_names:
            result.append(_seal_schema(t))
    # 技能渐进式加载入口：skill.invoke / skill.read_section（技能正文不进 system prompt，
    # 按需显式拉取；大正文按章节切分读取）。
    # 仅当智能体配置了技能（skills 非空）或为未配置的旧数据（skills=None，兼容）时注入；
    # 明确不勾选任何技能（skills=[]）→ 不注入，未勾选技能就没有技能调用能力。
    if skills is None or bool(skills):
        for inject_name in ("skill.invoke", "skill.read_section"):
            if not any(t["function"]["name"] == inject_name for t in result):
                for t in COMMAND_TOOLS:
                    if t["function"]["name"] == inject_name:
                        result.append(_seal_schema(t))
                        break
    for pname in (plugin_names or []):
        meta = plugin_registry.get(pname)
        if meta:
            tool = meta.get("function_schema") or build_plugin_tool(pname, meta)
            result.append(_seal_schema(_with_preset(tool, pname)))
    # —— 外部工具提供者（拓展注册，如 MCP 客户端） ——
    # 放在最后：不覆盖内建命令/插件，schema 原样注入（MCP inputSchema 可能含 $defs）。
    if agent is not None:
        try:
            from core.external_tools import collect_tools
            existing = {(t.get("function") or {}).get("name") for t in result}
            for tool in collect_tools(agent):
                name = (tool.get("function") or {}).get("name")
                if name and name not in existing:
                    result.append(tool)
                    existing.add(name)
        except Exception as e:  # 外部工具故障不影响内建能力
            print(f"[commands] 收集外部工具失败：{e}")
    return result


def _seal_schema(tool: dict) -> dict:
    """给一个 OpenAI tool schema 的 parameters 加 additionalProperties=False。
    如果工具带 properties（含空 properties 的零参数工具），统一加。"""
    fn = tool.get("function") or {}
    params = fn.get("parameters")
    if isinstance(params, dict) and params.get("type") == "object":
        # additionalProperties=False 只在 properties 存在时加（空 properties 也可以加）
        params = {**params, "additionalProperties": False}
        tool = {"type": tool.get("type", "function"),
                "function": {**fn, "parameters": params}}
    return tool


def _with_preset(tool: dict, pname: str) -> dict:
    """能力参数预设整形插件 schema：
    - 固定参数：从 properties/required 中剔除（模型不可见、不可改）；
    - 可选参数：把文字描述同时注入到**对应参数的 description** 内，并追加到 tool description 末尾。
      注入到参数自身 description 是关键——模型选择该参数取值时直接看到预设提示，
      否则提示被埋在很长的 tool description 末尾，容易被忽略（如 browser_control 的 action）。
    预设不存在或出错时原样返回 tool。
    """
    try:
        from core.param_presets import get_fixed_values, get_optional_desc
        fn = tool.get("function") or {}
        params = fn.get("parameters") or {}
        fixed = get_fixed_values("plugin", pname)
        optional = get_optional_desc("plugin", pname)
        if not fixed and not optional:
            return tool
        props = dict(params.get("properties") or {})
        required = list(params.get("required") or [])
        for k in fixed:
            props.pop(k, None)
            if k in required:
                required.remove(k)
        desc = fn.get("description") or ""
        if fixed:
            # 固定参数已从 schema 剔除（模型不可见、不可改），但在 description 中
            # 告知智能体哪些参数已被预设，使其感知运行时状态（不暴露预设值）。
            desc = (desc.rstrip() + "\n\n【能力参数预设 · 固定参数（已自动注入，无需填写）】\n"
                    + "\n".join(f"- {k}（已预设，运行时自动生效）" for k in fixed))
        if optional:
            # 1) 把每个可选参数提示注入到该参数自己的 description 里
            new_props: dict = {}
            for pk, pmeta in props.items():
                hint = optional.get(pk)
                if hint and isinstance(pmeta, dict):
                    base = (pmeta.get("description") or "").rstrip()
                    pmeta = {**pmeta, "description": base + f"\n【能力参数预设提示】{hint}"}
                new_props[pk] = pmeta
            props = new_props
            # 2) 仍在 tool description 末尾保留汇总（兜底 + 供无对应参数的提示展示）
            desc = (desc.rstrip() + "\n\n【能力参数预设 · 可选参数（仅供模型参考选择，按需填入）】\n"
                    + "\n".join(f"- {k}：{v}" for k, v in optional.items()))
        return {
            "type": "function",
            "function": {
                **fn,
                "description": desc,
                "parameters": {**params, "properties": props, "required": required},
            },
        }
    except Exception:
        return tool


# ==============================================================
#         集群编排工具（cluster.*）— 智能体 / 画布 / 节点 / 连线
# ==============================================================
def _load_all(name):
    from core.store import load
    return load(name)


def _save_all(name, data):
    from core.store import save
    return save(name, data)


def _next_id(name):
    from core.store import next_id
    return next_id(name)


def _get_by(name, value, key="id"):
    from core.store import get_by
    return get_by(name, value, key)


def _upsert(name, item, key="id"):
    from core.store import upsert
    return upsert(name, item, key)


def _delete_by(name, value, key="id"):
    from core.store import delete_by
    return delete_by(name, value, key)


def _find_agent(name_or_id):
    """按 id (int) 或 name (str) 查找智能体，返回完整对象或 None"""
    from core.store import load
    try:
        aid = int(name_or_id)
        a = _get_by("agents", aid)
        if a: return a
    except Exception:
        pass
    if isinstance(name_or_id, str):
        for a in load("agents"):
            if a.get("name") == name_or_id or (name_or_id in a.get("name", "")):
                return a
    return None


def _find_canvas(name_or_id):
    try:
        cid = int(name_or_id)
        c = _get_by("canvases", cid)
        if c: return c
    except Exception:
        pass
    for c in _load_all("canvases"):
        if c.get("name") == name_or_id or (name_or_id in c.get("name", "")):
            return c
    return None


# 允许 agent / canvas get/search 命令 select 的字段名（白名单模式，写错静默忽略）
_AGENT_FIELDS = {
    "id", "name", "description", "system_prompt", "model",
    "commands", "skills", "plugins", "trigger", "tags", "enabled",
    "group_id", "group_name", "max_tool_rounds", "created_at",
}
_CANVAS_FIELDS = {
    "id", "name", "description", "nodes", "edges", "nodes_count", "edges_count", "created_at",
}


def _pick_fields(item: dict, fields: list | None, allowed: set) -> dict:
    """字段投影：fields=None/空 → 原样返回；否则仅保留 allowed ∩ fields 里的 key"""
    if not fields:
        return item
    keep = [f for f in fields if f in allowed and f in item]
    return {f: item[f] for f in keep}


def cmd_cluster_list_agents() -> dict:
    """列出所有智能体（兼容别名：等价于 cluster.search_agents() 空 keyword）。
    已合并入 cluster.search_agents，新调用请改用 search_agents（支持关键字过滤、字段投影、排序）。"""
    r = cmd_cluster_search_agents()
    # 把 search 返回的 agents 数组转为 list_agents 的简要格式
    return {"agents": [{
        "id": a.get("id"),
        "name": a.get("name"),
        "description": a.get("description", ""),
        "commands": a.get("commands", []),
        "skills": a.get("skills", []),
        "plugins": a.get("plugins", []),
    } for a in r.get("agents", [])]}


def cmd_cluster_get_agent(agents: list | None = None, fields: list | None = None) -> dict:
    """查看一个或多个智能体的信息（支持批量、支持字段投影）。
    agents 元素可以是 id 或名称（支持模糊匹配）。fields 可选：只返回指定字段，如 ["id","name","system_prompt"]；
    不传则返回完整对象。"""
    if not agents:
        return {"error": "请通过 agents 参数传入要查看的智能体 id 或名称列表（支持批量）"}
    if not isinstance(agents, list):
        agents = [agents]
    found, not_found = [], []
    for key in agents:
        a = _find_agent(key)
        if not a:
            not_found.append(key)
            continue
        g = _get_by("agent_groups", a.get("group_id")) if a.get("group_id") else None
        enriched = {**a, "group_name": g.get("name") if g else None}
        found.append(_pick_fields(enriched, fields, _AGENT_FIELDS))
    if not found:
        return {"error": f"未找到智能体：{not_found}", "agents": [], "not_found": not_found}
    return {"agents": found, "not_found": not_found}


def cmd_cluster_search_agents(keyword: str | None = None, fields: list | None = None,
                               limit: int | None = None, match_in: list | None = None) -> dict:
    """按特征搜索智能体。keyword 支持多个（用空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。
    默认在 name / description / system_prompt / model / tags / commands / skills / plugins 里搜索；
    可以传 match_in 数组限定搜索范围（如 ["system_prompt", "name"]）。
    fields 可选：只返回指定字段。limit 可选：最多返回条数。"""
    all_kw = []
    if keyword:
        for sep in (" ", ",", "，", "、"):
            keyword = keyword.replace(sep, " ")
        for k in keyword.split():
            k = k.strip()
            if k:
                all_kw.append(k.lower())
    allowed_match = {"name", "description", "system_prompt", "model",
                     "tags", "commands", "skills", "plugins"}
    match_scope = set(match_in) & allowed_match if match_in else allowed_match
    hits = []
    for a in _load_all("agents"):
        text_parts = []
        if "name" in match_scope: text_parts.append(str(a.get("name", "")))
        if "description" in match_scope: text_parts.append(str(a.get("description", "")))
        if "system_prompt" in match_scope: text_parts.append(str(a.get("system_prompt", "")))
        if "model" in match_scope: text_parts.append(str(a.get("model", "")))
        if "tags" in match_scope: text_parts.append(" ".join(str(t) for t in a.get("tags", [])))
        if "commands" in match_scope: text_parts.append(" ".join(str(c) for c in a.get("commands", [])))
        if "skills" in match_scope: text_parts.append(" ".join(str(s) for s in a.get("skills", [])))
        if "plugins" in match_scope: text_parts.append(" ".join(str(p) for p in a.get("plugins", [])))
        haystack = " ".join(text_parts).lower()
        score = 0
        for kw in all_kw:
            c = haystack.count(kw)
            if c: score += c
        if not all_kw or score > 0:
            g = _get_by("agent_groups", a.get("group_id")) if a.get("group_id") else None
            enriched = {**a, "group_name": g.get("name") if g else None, "_score": score}
            hits.append(_pick_fields(enriched, fields, _AGENT_FIELDS | {"_score"}))
    hits.sort(key=lambda x: x.get("_score", 0), reverse=True)
    if limit:
        hits = hits[:limit]
    return {"total": len(hits), "keyword": keyword, "agents": hits}


def cmd_cluster_create_agent(name: str, description: str = "", system_prompt: str = "",
                             model: str = "", commands: list | None = None,
                             skills: list | None = None, plugins: list | None = None,
                             max_tool_rounds: int | None = None,
                             group_id: int | None = None) -> dict:
    from datetime import datetime
    item = {
        "id": _next_id("agents"),
        "name": name, "description": description, "system_prompt": system_prompt,
        "model": model, "interface_id": None,
        "commands": commands or [], "skills": skills or [], "plugins": plugins or [],
        "trigger": {"type": "manual", "cron": "", "message": ""},
        "tags": [], "enabled": True, "created_at": datetime.utcnow().isoformat(),
        "max_tool_rounds": max_tool_rounds,
        "group_id": group_id,
    }
    _upsert("agents", item)
    return {"ok": True, "agent": item}


def cmd_cluster_update_agent(agent_id: int, name: str | None = None,
                             description: str | None = None,
                             system_prompt: str | None = None,
                             model: str | None = None,
                             commands: list | None = None,
                             skills: list | None = None,
                             plugins: list | None = None,
                             max_tool_rounds: int | None = None,
                             group_id: int | None = None) -> dict:
    cur = _get_by("agents", agent_id)
    if not cur: return {"error": f"agent {agent_id} not found"}
    if name is not None: cur["name"] = name
    if description is not None: cur["description"] = description
    if system_prompt is not None: cur["system_prompt"] = system_prompt
    if model is not None: cur["model"] = model
    if commands is not None: cur["commands"] = commands
    if skills is not None: cur["skills"] = skills
    if plugins is not None: cur["plugins"] = plugins
    if max_tool_rounds is not None: cur["max_tool_rounds"] = max_tool_rounds
    if group_id is not None: cur["group_id"] = group_id
    _upsert("agents", cur)
    return {"ok": True, "agent": cur}


def _delete_agent_one(agent_id) -> dict:
    """删除单个智能体（agent_id 支持 id 或名称），并从所有画布的 nodes / edges 里清掉。"""
    a = _find_agent(agent_id)
    if not a:
        return {"agent_id": agent_id, "ok": False, "error": f"agent {agent_id} not found"}
    aid = a["id"]
    canvases = _load_all("canvases")
    changed = 0
    for c in canvases:
        prev = len(c.get("nodes", []))
        c["nodes"] = [n for n in c.get("nodes", []) if n.get("agent_id") != aid]
        if len(c["nodes"]) != prev:
            changed += 1
            remaining_ids = {n["id"] for n in c["nodes"]}
            c["edges"] = [e for e in c.get("edges", []) if e.get("from_node") in remaining_ids and e.get("to_node") in remaining_ids]
            _save_all("canvases", canvases)
    ok = _delete_by("agents", aid)
    return {"agent_id": aid, "name": a.get("name"), "ok": ok, "cleaned_from_canvases": changed}


def cmd_cluster_delete_agent(agent_id=None, agent_ids: list | None = None) -> dict:
    """删除智能体，支持批量：agent_ids 传数组（元素可以是 id 或名称），agent_id 传单个，两者可同时使用。"""
    targets = list(agent_ids or [])
    if agent_id is not None:
        targets.append(agent_id)
    if not targets:
        return {"error": "请通过 agent_id（单个）或 agent_ids（数组，批量）指定要删除的智能体"}
    results = [_delete_agent_one(t) for t in targets]
    deleted = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": not failed,
        "count": len(deleted),
        "deleted": deleted,
        "failed": failed,
        "cleaned_from_canvases": sum(r.get("cleaned_from_canvases", 0) for r in deleted),
    }


def cmd_cluster_list_canvases() -> dict:
    """列出所有画布（兼容别名：等价于 cluster.search_canvases() 空 keyword）。
    已合并入 cluster.search_canvases，新调用请改用 search_canvases（支持关键字过滤、字段投影、排序）。"""
    r = cmd_cluster_search_canvases()
    return {"canvases": [{
        "id": c.get("id"), "name": c.get("name"), "description": c.get("description", ""),
        "nodes_count": c.get("nodes_count", 0),
        "edges_count": c.get("edges_count", 0),
    } for c in r.get("canvases", [])]}


def cmd_cluster_create_canvas(name: str, description: str = "") -> dict:
    from datetime import datetime
    item = {
        "id": _next_id("canvases"), "name": name, "description": description,
        "nodes": [], "edges": [], "created_at": datetime.utcnow().isoformat(),
    }
    _upsert("canvases", item)
    return {"ok": True, "canvas": item}


def cmd_cluster_delete_canvas(canvas_id: int) -> dict:
    return {"ok": _delete_by("canvases", canvas_id)}


def cmd_cluster_get_canvas(canvas_id: int | str, fields: list | None = None) -> dict:
    """查看画布详细内容（支持字段投影）。fields 可选：只返回指定字段，如 ["id","name","nodes"]；
    不传则返回完整画布（节点含 agent_name 补充）。支持的字段见 cluster.search_canvases 说明。"""
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    agent_map = {a["id"]: a["name"] for a in _load_all("agents")}
    enriched = {
        **c,
        "nodes": [{**n, "agent_name": agent_map.get(n["agent_id"], "?")} for n in c.get("nodes", [])],
        "nodes_count": len(c.get("nodes", [])),
        "edges_count": len(c.get("edges", [])),
    }
    return {"canvas": _pick_fields(enriched, fields, _CANVAS_FIELDS)}


def cmd_cluster_search_canvases(keyword: str | None = None, fields: list | None = None,
                                limit: int | None = None, match_in: list | None = None) -> dict:
    """按特征搜索画布。keyword 支持多个（空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。
    默认在 name / description / 画布关联的规章制度 / 节点所包含的智能体名称 里搜索；
    可传 match_in 数组限定范围，可选值：name / description / rules / agents_in_canvas。
    fields 可选：只返回指定字段（id / name / description / nodes_count / edges_count / created_at / nodes / edges）。
    limit 可选：最多返回条数。"""
    all_kw = []
    if keyword:
        for sep in (" ", ",", "，", "、"):
            keyword = keyword.replace(sep, " ")
        for k in keyword.split():
            k = k.strip()
            if k:
                all_kw.append(k.lower())
    allowed_match = {"name", "description", "rules", "agents_in_canvas"}
    match_scope = set(match_in) & allowed_match if match_in else allowed_match
    # 预先准备 rules 和 agent_name 映射
    rules_by_canvas: dict[int, list[str]] = {}
    if "rules" in match_scope:
        for r in _load_all("rules"):
            cid = r.get("canvas_id")
            if cid is None: continue
            rules_by_canvas.setdefault(cid, []).append(str(r.get("content", "")))
    agent_map = {a["id"]: a["name"] for a in _load_all("agents")} if "agents_in_canvas" in match_scope else {}
    hits = []
    for c in _load_all("canvases"):
        text_parts = []
        if "name" in match_scope: text_parts.append(str(c.get("name", "")))
        if "description" in match_scope: text_parts.append(str(c.get("description", "")))
        if "rules" in match_scope:
            for r_content in rules_by_canvas.get(c["id"], []):
                text_parts.append(r_content)
        if "agents_in_canvas" in match_scope:
            for n in c.get("nodes", []):
                aname = agent_map.get(n.get("agent_id"), "")
                if aname: text_parts.append(aname)
        haystack = " ".join(text_parts).lower()
        score = 0
        for kw in all_kw:
            cnt = haystack.count(kw)
            if cnt: score += cnt
        if not all_kw or score > 0:
            enriched = {
                **c,
                "nodes_count": len(c.get("nodes", [])),
                "edges_count": len(c.get("edges", [])),
                "_score": score,
            }
            hits.append(_pick_fields(enriched, fields, _CANVAS_FIELDS | {"_score"}))
    hits.sort(key=lambda x: x.get("_score", 0), reverse=True)
    if limit:
        hits = hits[:limit]
    return {"total": len(hits), "keyword": keyword, "canvases": hits}


def cmd_cluster_add_node(canvas_id: int | str, agent_id: int | str,
                         x: float | None = None, y: float | None = None) -> dict:
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    a = _find_agent(agent_id)
    if not a: return {"error": f"agent {agent_id} not found"}
    c.setdefault("nodes", [])
    c.setdefault("edges", [])
    next_node_id = max([0] + [n["id"] for n in c["nodes"]] + [e["id"] for e in c["edges"]]) + 1
    # 默认位置：按已有节点数网格排列
    cols = 3; gap_x = 220; gap_y = 140
    col = len(c["nodes"]) % cols
    row = len(c["nodes"]) // cols
    px = x if x is not None else 60 + col * gap_x
    py = y if y is not None else 60 + row * gap_y
    node = {"id": next_node_id, "agent_id": a["id"], "x": px, "y": py}
    c["nodes"].append(node)
    _upsert("canvases", c)
    return {"ok": True, "canvas_id": c["id"], "node": node, "agent_name": a["name"]}


def cmd_cluster_remove_node(canvas_id: int | str, node_id: int) -> dict:
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    c["nodes"] = [n for n in c.get("nodes", []) if n.get("id") != node_id]
    c["edges"] = [e for e in c.get("edges", []) if e.get("from_node") != node_id and e.get("to_node") != node_id]
    _upsert("canvases", c)
    return {"ok": True, "canvas_id": c["id"]}


def cmd_cluster_connect(canvas_id: int | str, from_node_id: int, to_node_id: int,
                        edge_type: str = "chat") -> dict:
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    c.setdefault("edges", [])
    # 防重复
    for e in c["edges"]:
        if e["from_node"] == from_node_id and e["to_node"] == to_node_id:
            return {"ok": True, "note": "edge already exists", "edge": e}
    next_edge_id = max([0] + [n["id"] for n in c.get("nodes", [])] + [e2["id"] for e2 in c["edges"]]) + 1
    edge = {"id": next_edge_id, "from_node": from_node_id, "to_node": to_node_id, "type": edge_type}
    c["edges"].append(edge)
    _upsert("canvases", c)
    return {"ok": True, "canvas_id": c["id"], "edge": edge}


def cmd_cluster_disconnect(canvas_id: int | str, from_node_id: int, to_node_id: int) -> dict:
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    before = len(c.get("edges", []))
    c["edges"] = [e for e in c.get("edges", []) if not (e["from_node"] == from_node_id and e["to_node"] == to_node_id)]
    _upsert("canvases", c)
    return {"ok": True, "removed": before - len(c["edges"])}


# ---- 规章制度（按画布隔离；注入该画布智能体的系统提示词）----
def _require_canvas(canvas_id: int | str) -> dict | None:
    c = _find_canvas(canvas_id)
    return c


def cmd_cluster_list_rules(canvas_id: int | str) -> dict:
    """列出某画布的规章制度"""
    c = _require_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    items = [r for r in _load_all("rules") if r.get("canvas_id") == c.get("id")]
    agent_map = {a["id"]: a.get("name") for a in _load_all("agents")}
    out = []
    for r in items:
        ids = r.get("agent_ids")
        out.append({
            "id": r.get("id"),
            "content": r.get("content", ""),
            "enabled": r.get("enabled", True),
            "agent_ids": ids,
            "scope": "全部智能体" if ids is None else [agent_map.get(i, i) for i in ids],
        })
    return {"canvas_id": c.get("id"), "canvas_name": c.get("name"), "rules": out, "count": len(out)}


def cmd_cluster_create_rule(content: str, canvas_id: int | str,
                            agent_ids: list[int] | None = None) -> dict:
    """新增一条规章制度。agent_ids 省略/null=该画布全部智能体；数组=仅指定智能体"""
    from datetime import datetime
    c = _require_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    if not (content or "").strip():
        return {"error": "content 不能为空"}
    item = {
        "id": _next_id("rules"),
        "content": content.strip(),
        "canvas_id": c.get("id"),
        "agent_ids": agent_ids,
        "enabled": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    _upsert("rules", item)
    return {"ok": True, "rule": item, "canvas_id": c.get("id")}


def cmd_cluster_update_rule(rule_id: int, content: str | None = None,
                            agent_ids: list[int] | None = None,
                            apply_to_all: bool = False,
                            enabled: bool | None = None) -> dict:
    """修改规章制度。content=改内容；apply_to_all=True 将注入范围改为全部；
    否则若给了 agent_ids 则改为指定智能体；enabled=启用/停用"""
    cur = _get_by("rules", rule_id)
    if not cur: return {"error": f"rule {rule_id} not found"}
    if content is not None:
        if not content.strip():
            return {"error": "content 不能为空"}
        cur["content"] = content.strip()
    if apply_to_all:
        cur["agent_ids"] = None
    elif agent_ids is not None:
        cur["agent_ids"] = agent_ids
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    _upsert("rules", cur)
    return {"ok": True, "rule": cur}


def cmd_cluster_delete_rule(rule_id: int) -> dict:
    ok = _delete_by("rules", rule_id)
    if not ok: return {"error": f"rule {rule_id} not found"}
    return {"ok": True, "rule_id": rule_id}


# ---- 智能体分组（agent_groups）----
def _group_sort_key(g: dict):
    so = g.get("sort_order")
    if so is None:
        return (1, g.get("id", 0))
    return (0, so)


def cmd_cluster_list_groups() -> dict:
    items = _load_all("agent_groups")
    items.sort(key=_group_sort_key)
    agents = _load_all("agents")
    out = []
    for g in items:
        count = sum(1 for a in agents if a.get("group_id") == g.get("id"))
        out.append({
            "id": g.get("id"),
            "name": g.get("name"),
            "agents_count": count,
        })
    return {"groups": out, "count": len(out)}


def cmd_cluster_create_group(name: str) -> dict:
    from datetime import datetime
    if not (name or "").strip():
        return {"error": "name 不能为空"}
    item = {
        "id": _next_id("agent_groups"),
        "name": name.strip(),
        "created_at": datetime.utcnow().isoformat(),
    }
    _upsert("agent_groups", item)
    return {"ok": True, "group": item}


def cmd_cluster_update_group(group_id: int, name: str) -> dict:
    cur = _get_by("agent_groups", group_id)
    if not cur: return {"error": f"group {group_id} not found"}
    if not (name or "").strip():
        return {"error": "name 不能为空"}
    cur["name"] = name.strip()
    _upsert("agent_groups", cur)
    return {"ok": True, "group": cur}


def cmd_cluster_delete_group(group_id: int) -> dict:
    ok = _delete_by("agent_groups", group_id)
    if not ok: return {"error": f"group {group_id} not found"}
    # 组内智能体移回"全部"组（group_id 置空）
    agents = _load_all("agents")
    ungrouped = 0
    for a in agents:
        if a.get("group_id") == group_id:
            a["group_id"] = None
            _upsert("agents", a)
            ungrouped += 1
    return {"ok": True, "agents_ungrouped": ungrouped}


def cmd_cluster_assign_agent_group(agent_id: int | str,
                                   group_id: int | None = None) -> dict:
    """把智能体归纳到某个分组；group_id 传 null/省略=移回"全部"组"""
    a = _find_agent(agent_id)
    if not a: return {"error": f"agent {agent_id} not found"}
    if group_id is not None and not _get_by("agent_groups", group_id):
        return {"error": f"group {group_id} not found"}
    a["group_id"] = group_id
    _upsert("agents", a)
    return {"ok": True, "agent": {"id": a["id"], "name": a["name"], "group_id": group_id}}


# ---- 会话 / 运行管理（新建/编辑/继续会话、停止运行、查看运行态与对话记录）----
def _find_conversation(conversation_id):
    """按 id 查找会话：优先运行期共享对象（运行中落库不互踩），兜底磁盘。"""
    from core import run_registry
    try:
        cid = int(conversation_id)
    except Exception:
        return None
    return run_registry.peek_live_conv(cid) or _get_by("conversations", cid)


def _conv_activity_ts(c: dict):
    """会话活跃时间：最后一条消息 ts → created_at → id（与 api/conversations.py 同口径，保证可比较）"""
    last_ts = None
    for m in c.get("messages") or []:
        ts = m.get("ts")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    return last_ts or c.get("created_at") or f"{c.get('id', 0):020d}"


def _normalize_workdir(path):
    """工作区路径归一：空串/None → None；否则转绝对路径并自动建目录。失败返回 None 并由调用方报错。"""
    if path is None:
        return None
    p = str(path).strip()
    if not p:
        return None
    import os as _os
    abs_p = _os.path.abspath(p)
    _os.makedirs(abs_p, exist_ok=True)
    return abs_p


def _normalize_exec_mode(mode):
    """串并行模式归一：空串/None → None；仅允许 serial | parallel。非法返回 error dict。"""
    if mode is None:
        return None
    m = str(mode).strip().lower()
    if not m:
        return None
    if m not in ("serial", "parallel"):
        return {"error": f"execution_mode 只允许 serial（串行）或 parallel（并行），收到：{mode}"}
    return m


def _agent_display_map() -> dict:
    return {a.get("id"): a.get("name") for a in _load_all("agents")}


async def _send_and_run(conv: dict, agent: dict, message: str) -> dict:
    """在主事件循环内：防重入 → 落用户消息 → run_registry.start 启动后台运行。
    与 api/conversations.py send_message 流程同口径（原子：整个协程在主循环线程执行）。"""
    import copy as _copy
    from core import run_registry
    cid = conv.get("id")
    if run_registry.is_running(cid, agent.get("id")):
        return {"error": f"该智能体正在运行中（会话 {cid}），请先 cluster.stop_agent 或稍后再试",
                "conversation_id": cid, "agent_id": agent.get("id")}
    canvas = _get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
    # 会话级串并行模式优先覆盖画布当前设置（与 send 端点同语义）
    if canvas is not None and conv.get("execution_mode"):
        canvas = {**canvas, "execution_mode": conv["execution_mode"]}
    mode = "linked" if conv.get("canvas_id") else "independent"
    conv.setdefault("messages", []).append({
        "role": "user", "content": message, "ts": datetime.utcnow().isoformat(),
        "to_agent_id": agent.get("id"),
    })
    from core.store import upsert as _upsert_store
    _upsert_store("conversations", conv)
    run_registry.start(cid, agent, message, mode, canvas,
                       base_conv=_copy.deepcopy(conv), workdir=conv.get("workdir"))
    return {
        "ok": True, "running": True,
        "conversation_id": cid, "conversation_name": conv.get("name"),
        "agent_id": agent.get("id"), "agent_name": agent.get("name"),
        "mode": mode, "workdir": conv.get("workdir"),
        "execution_mode": conv.get("execution_mode") or (canvas or {}).get("execution_mode") or "serial",
        "note": "运行已在后台启动，可用 cluster.get_conversation 查看对话进展、cluster.stop_agent 停止",
    }


def cmd_cluster_create_conversation(name: str = "", canvas_id: int | str | None = None,
                                    agent_ids: list | None = None,
                                    workdir: str | None = None,
                                    execution_mode: str | None = None) -> dict:
    """新建会话。可绑定画布（linked 集群模式）、指定成员智能体、工作区与串/并行运行模式；
    绑画布时 workdir / execution_mode 省略则继承画布设置，显式给出则作为会话级覆盖。"""
    item = {
        "name": (name or "").strip() or "新对话",
        "mode": "independent",
        "agent_ids": [],
        "canvas_id": None,
        "workdir": None,
        "execution_mode": None,
        "messages": [],
        "created_at": datetime.utcnow().isoformat(),
    }
    if canvas_id is not None and str(canvas_id).strip() != "":
        c = _find_canvas(canvas_id)
        if not c: return {"error": f"canvas {canvas_id} not found"}
        item["mode"] = "linked"
        item["canvas_id"] = c.get("id")
        if workdir is None:
            item["workdir"] = c.get("workdir")
        if execution_mode is None:
            item["execution_mode"] = c.get("execution_mode")
        if not (name or "").strip():
            item["name"] = f"{c.get('name', '画布')} · 集群"
    if workdir is not None:
        wd = _normalize_workdir(workdir)
        if wd is None and str(workdir).strip():
            return {"error": f"工作区路径不可用：{workdir}"}
        item["workdir"] = wd
    em = _normalize_exec_mode(execution_mode)
    if isinstance(em, dict): return em
    item["execution_mode"] = em if execution_mode is not None else item["execution_mode"]
    if agent_ids:
        norm_ids = []
        for aid in agent_ids:
            a = _find_agent(aid)
            if not a: return {"error": f"agent {aid} not found"}
            norm_ids.append(a.get("id"))
        item["agent_ids"] = sorted(set(norm_ids))
    item["id"] = _next_id("conversations")
    _upsert("conversations", item)
    return {"ok": True, "conversation": item}


def cmd_cluster_update_conversation(conversation_id: int | str, name: str | None = None,
                                    workdir: str | None = None,
                                    execution_mode: str | None = None,
                                    agent_ids: list | None = None,
                                    reset_messages: bool = False) -> dict:
    """编辑会话元信息：改名、改工作区、切换串/并行模式、改成员智能体、清空消息。
    workdir / execution_mode 传空串表示清除（回归画布/全局默认）。不支持改绑画布（需要换画布请新建会话）。"""
    conv = _find_conversation(conversation_id)
    if not conv: return {"error": f"conversation {conversation_id} not found"}
    if name is not None:
        if not str(name).strip():
            return {"error": "name 不能为空"}
        conv["name"] = str(name).strip()
    if workdir is not None:
        wd = _normalize_workdir(workdir)
        if wd is None and str(workdir).strip():
            return {"error": f"工作区路径不可用：{workdir}"}
        conv["workdir"] = wd  # 空串/None → None 清除
    if execution_mode is not None:
        em = _normalize_exec_mode(execution_mode)
        if isinstance(em, dict): return em
        conv["execution_mode"] = em  # 空串/None → None 清除
    if agent_ids is not None:
        norm_ids = []
        for aid in agent_ids:
            a = _find_agent(aid)
            if not a: return {"error": f"agent {aid} not found"}
            norm_ids.append(a.get("id"))
        conv["agent_ids"] = sorted(set(norm_ids))
    if reset_messages:
        conv["messages"] = []
    _upsert("conversations", conv)
    return {"ok": True, "conversation": {
        "id": conv.get("id"), "name": conv.get("name"), "mode": conv.get("mode"),
        "canvas_id": conv.get("canvas_id"), "agent_ids": conv.get("agent_ids"),
        "workdir": conv.get("workdir"), "execution_mode": conv.get("execution_mode"),
        "message_count": len(conv.get("messages") or []),
    }}


def cmd_cluster_continue_conversation(conversation_id: int | str, agent_id: int | str,
                                      message: str, workdir: str | None = None,
                                      execution_mode: str | None = None) -> dict:
    """继续会话：向会话中某智能体发消息并启动后台运行（该智能体未入会时自动加入）。
    workdir / execution_mode 给定时先写入会话再运行（一次调用完成切换）。
    「以画布中某个智能体启动画布」= 先 cluster.create_conversation 绑画布，再向该节点智能体 continue_conversation。"""
    conv = _find_conversation(conversation_id)
    if not conv: return {"error": f"conversation {conversation_id} not found"}
    agent = _find_agent(agent_id)
    if not agent: return {"error": f"agent {agent_id} not found"}
    if not (message or "").strip():
        return {"error": "message 不能为空"}
    changed = False
    if workdir is not None:
        wd = _normalize_workdir(workdir)
        if wd is None and str(workdir).strip():
            return {"error": f"工作区路径不可用：{workdir}"}
        conv["workdir"] = wd
        changed = True
    if execution_mode is not None:
        em = _normalize_exec_mode(execution_mode)
        if isinstance(em, dict): return em
        conv["execution_mode"] = em
        changed = True
    aid = agent.get("id")
    if aid not in (conv.get("agent_ids") or []):
        conv.setdefault("agent_ids", []).append(aid)
        changed = True
    if changed:
        _upsert("conversations", conv)
    # 调度到主事件循环执行（run_registry.start 必须在事件循环内调用）
    from core import mainloop
    return mainloop.run_coro(_send_and_run(conv, agent, message.strip()))


def cmd_cluster_stop_agent(agent_id: int | str | None = None,
                           conversation_id: int | str | None = None) -> dict:
    """停止运行中的智能体。conversation_id 给定 → 停该会话（agent_id 可选精确停某智能体，缺省停整个会话）；
    只给 agent_id → 停该智能体在所有会话中的运行。二者至少给一个。"""
    if agent_id is None and conversation_id is None:
        return {"error": "agent_id 与 conversation_id 至少给一个"}
    from core import mainloop, run_registry

    async def _do_stop(cid: int | None, aid: int | None):
        stopped = run_registry.stop(cid, aid) if cid is not None else False
        return stopped

    stopped_items = []
    if conversation_id is not None:
        conv = _find_conversation(conversation_id)
        if not conv: return {"error": f"conversation {conversation_id} not found"}
        aid = None
        if agent_id is not None:
            a = _find_agent(agent_id)
            if not a: return {"error": f"agent {agent_id} not found"}
            aid = a.get("id")
        ok = mainloop.run_coro(_do_stop(conv.get("id"), aid))
        if ok:
            stopped_items.append({"conversation_id": conv.get("id"), "conversation_name": conv.get("name"),
                                  "agent_id": aid, "agent_name": a.get("name") if aid else None})
    else:
        a = _find_agent(agent_id)
        if not a: return {"error": f"agent {agent_id} not found"}
        aid = a.get("id")
        convs = {r.get("conv_id") for r in run_registry.list_running() if r.get("agent_id") == aid}
        for cid in convs:
            if mainloop.run_coro(_do_stop(cid, aid)):
                c = _get_by("conversations", cid) or {}
                stopped_items.append({"conversation_id": cid, "conversation_name": c.get("name"),
                                      "agent_id": aid, "agent_name": a.get("name")})
    if not stopped_items:
        return {"ok": False, "stopped": [], "note": "没有正在运行的任务"}
    return {"ok": True, "stopped": stopped_items, "count": len(stopped_items)}


def cmd_cluster_list_running() -> dict:
    """查看当前所有正在运行的智能体（会话、归属画布、开始时间）。"""
    from core import run_registry
    agent_map = _agent_display_map()
    conv_map = {c.get("id"): c for c in _load_all("conversations")}
    canvas_map = {c.get("id"): c for c in _load_all("canvases")}
    out = []
    for r in run_registry.list_running():
        conv = conv_map.get(r.get("conv_id")) or {}
        canvas = canvas_map.get(conv.get("canvas_id")) if conv.get("canvas_id") else None
        out.append({
            "conversation_id": r.get("conv_id"),
            "conversation_name": conv.get("name"),
            "agent_id": r.get("agent_id"),
            "agent_name": agent_map.get(r.get("agent_id"), f"?{r.get('agent_id')}"),
            "canvas_id": conv.get("canvas_id"),
            "canvas_name": canvas.get("name") if canvas else None,
            "started_at": r.get("started_at"),
            "seq": r.get("seq"),
        })
    return {"running": out, "count": len(out)}


def cmd_cluster_search_conversations(keyword: str | None = None,
                                      kinds: list | None = None,
                                      limit: int | None = None) -> dict:
    """按特征关键字搜索会话。keyword 支持多个（空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。
    默认在会话 name / 最后一条消息预览（last_preview）/ 绑定的智能体名称 / 绑定的画布名称 这四类信息里搜索；
    kinds 可限定范围，可选值：name / preview / agent_names / canvas_name。
    limit 可选：最多返回条数；不传则按活跃时间返回全部。返回结构与 cluster.list_conversations 一致，每条附带 _score 相关度分。"""
    from core import run_registry
    kws = []
    if keyword:
        s = keyword
        for sep in (" ", ",", "，", "、"):
            s = s.replace(sep, " ")
        for k in s.split():
            k = k.strip().lower()
            if k: kws.append(k)
    allowed_match = {"name", "preview", "agent_names", "canvas_name"}
    match_scope = set(kinds) & allowed_match if kinds else allowed_match

    items = _load_all("conversations")
    agent_map = _agent_display_map()
    canvas_map = {c.get("id"): c.get("name") for c in _load_all("canvases")}
    running_convs = {r.get("conv_id") for r in run_registry.list_running()}
    running_pairs = {(r.get("conv_id"), r.get("agent_id")) for r in run_registry.list_running()}

    hits = []
    for c in items:
        msgs = c.get("messages") or []
        last_preview = ""
        for m in reversed(msgs):
            content = m.get("content")
            if isinstance(content, str) and content.strip() and m.get("role") in ("user", "assistant"):
                last_preview = content[:80].replace("\n", " ")
                break
        agent_names = [agent_map.get(i, f"?{i}") for i in (c.get("agent_ids") or [])]
        canvas_name = canvas_map.get(c.get("canvas_id")) if c.get("canvas_id") else None

        text_parts = []
        if "name" in match_scope: text_parts.append(str(c.get("name", "")))
        if "preview" in match_scope: text_parts.append(last_preview)
        if "agent_names" in match_scope: text_parts.extend(agent_names)
        if "canvas_name" in match_scope and canvas_name: text_parts.append(canvas_name)
        haystack = " ".join(text_parts).lower()

        score = 0
        for kw in kws:
            cnt = haystack.count(kw)
            if cnt: score += cnt
        if not kws or score > 0:
            hits.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "mode": c.get("mode", "independent"),
                "canvas_id": c.get("canvas_id"),
                "canvas_name": canvas_name,
                "agent_ids": c.get("agent_ids", []),
                "agent_names": agent_names,
                "workdir": c.get("workdir"),
                "execution_mode": c.get("execution_mode"),
                "message_count": len(msgs),
                "last_preview": last_preview,
                "last_ts": _conv_activity_ts(c),
                "running": c.get("id") in running_convs,
                "running_agents": [aid for (cid, aid) in running_pairs if cid == c.get("id")],
                "_score": score,
            })
    hits.sort(key=lambda x: x.get("_score", 0), reverse=True)
    if limit:
        hits = hits[:limit]
    return {"total": len(hits), "keyword": keyword, "conversations": hits}


def cmd_cluster_list_conversations(limit: int = 50) -> dict:
    """列出会话（按活跃时间新→旧），含运行状态，供编排师了解有哪些对话、哪些正在跑。"""
    from core import run_registry
    items = _load_all("conversations")
    items.sort(key=_conv_activity_ts, reverse=True)
    agent_map = _agent_display_map()
    canvas_map = {c.get("id"): c.get("name") for c in _load_all("canvases")}
    running_pairs = {(r.get("conv_id"), r.get("agent_id")) for r in run_registry.list_running()}
    running_convs = {r.get("conv_id") for r in run_registry.list_running()}
    out = []
    for c in items[: max(1, int(limit))]:
        msgs = c.get("messages") or []
        last_preview = ""
        for m in reversed(msgs):
            content = m.get("content")
            if isinstance(content, str) and content.strip() and m.get("role") in ("user", "assistant"):
                last_preview = content[:80].replace("\n", " ")
                break
        out.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "mode": c.get("mode", "independent"),
            "canvas_id": c.get("canvas_id"),
            "canvas_name": canvas_map.get(c.get("canvas_id")) if c.get("canvas_id") else None,
            "agent_ids": c.get("agent_ids", []),
            "agent_names": [agent_map.get(i, f"?{i}") for i in (c.get("agent_ids") or [])],
            "workdir": c.get("workdir"),
            "execution_mode": c.get("execution_mode"),
            "message_count": len(msgs),
            "last_preview": last_preview,
            "last_ts": _conv_activity_ts(c),
            "running": c.get("id") in running_convs,
            "running_agents": [aid for (cid, aid) in running_pairs if cid == c.get("id")],
        })
    return {"conversations": out, "count": len(out)}


def cmd_cluster_get_conversation(conversation_id: int | str | None = None,
                                 agent: int | str | None = None,
                                 last_n: int = 30, full: bool = False) -> dict:
    """查看对话记录。conversation_id 给定直接用；否则按 agent（id 或名称）找其最新会话。
    默认只返回最近 30 条、每条内容截断 400 字（full=true 返回完整内容）。"""
    if conversation_id is not None:
        conv = _find_conversation(conversation_id)
        if not conv: return {"error": f"conversation {conversation_id} not found"}
    elif agent is not None:
        a = _find_agent(agent)
        if not a: return {"error": f"agent {agent} not found"}
        convs = [c for c in _load_all("conversations") if a.get("id") in (c.get("agent_ids") or [])]
        if not convs:
            return {"error": f"智能体「{a.get('name')}」没有参与任何会话", "agent_id": a.get("id")}
        convs.sort(key=_conv_activity_ts, reverse=True)
        conv = convs[0]
    else:
        return {"error": "conversation_id 与 agent 至少给一个"}
    agent_map = _agent_display_map()
    canvas = _get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
    from core import run_registry
    running = run_registry.is_running(conv.get("id"))
    msgs = conv.get("messages") or []
    try:
        n = max(1, int(last_n))
    except Exception:
        n = 30
    shown = msgs[-n:] if n < len(msgs) else msgs
    out_msgs = []
    for idx, m in enumerate(shown):
        start_idx = len(msgs) - len(shown)
        content = m.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if not full and len(content) > 400:
            content = content[:400] + f"…（共 {len(content)} 字，full=true 查看全文）"
        aid = m.get("agent_id") or m.get("to_agent_id")
        out_msgs.append({
            "index": start_idx + idx,
            "role": m.get("role"),
            "agent_id": aid,
            "agent_name": agent_map.get(aid) if aid is not None else None,
            "ts": m.get("ts"),
            "content": content,
        })
    return {
        "conversation_id": conv.get("id"), "name": conv.get("name"),
        "mode": conv.get("mode", "independent"),
        "canvas_id": conv.get("canvas_id"),
        "canvas_name": canvas.get("name") if canvas else None,
        "workdir": conv.get("workdir"), "execution_mode": conv.get("execution_mode"),
        "running": running, "message_count": len(msgs),
        "shown_count": len(out_msgs), "messages": out_msgs,
    }


def cmd_cluster_list_capabilities(keyword: str | None = None,
                                   kinds: list | None = None) -> dict:
    """返回系统中可用的命令、技能、插件清单（含描述）。
    keyword 可选：按关键字过滤，匹配命令名/技能名/插件名、label、description（子串匹配，大小写不敏感，多关键字可空格/逗号分隔，任一命中即保留）。
    kinds 可选：限定返回类型，可选值 ["commands","skills","plugins"]；不传则全部返回。"""
    from plugins import list_plugins as _list_plugins
    from skills import list_skills as _list_skills

    # 关键字预处理：多关键字拆开、统一小写
    kws = []
    if keyword:
        s = keyword
        for sep in (" ", ",", "，", "、"):
            s = s.replace(sep, " ")
        for k in s.split():
            k = k.strip().lower()
            if k: kws.append(k)

    kinds_filter = set(kinds) & {"commands", "skills", "plugins"} if kinds else None

    def _match(haystack: str) -> bool:
        if not kws: return True
        h = haystack.lower()
        return any(kw in h for kw in kws)

    # 命令
    commands_by_group: dict[str, list[dict]] = {}
    hidden = RUNTIME_INJECTED_COMMANDS
    if kinds_filter is None or "commands" in kinds_filter:
        for t in COMMAND_TOOLS:
            name = t["function"]["name"]
            if name in hidden or not name.startswith(("file.", "workspace.", "cmd.", "cluster.", "agent.")):
                continue
            meta = COMMAND_META.get(name, {})
            label = meta.get("label", name)
            desc = t["function"].get("description", "")
            params = meta.get("params", [])
            if not _match(f"{name} {label} {desc} {' '.join(params)}"):
                continue
            group = meta.get("group", "其他")
            commands_by_group.setdefault(group, []).append({
                "name": name, "label": label, "description": desc, "params": params,
            })

    # 技能（按用户自定义分组返回）
    skill_list: list[dict] = []
    skills_by_group: dict[str, list[dict]] = {}
    if kinds_filter is None or "skills" in kinds_filter:
        user_groups = _store.load_capability_groups("skills")
        # 反向索引：skill_name → group
        _sg_map: dict[str, str] = {}
        for g, names in user_groups.items():
            for n in names:
                _sg_map[n] = g
        for k, v in _list_skills().items():
            label = v.get("label", k)
            desc = v.get("description", "")
            if not _match(f"{k} {label} {desc}"):
                continue
            entry = {"name": k, "label": label, "description": desc}
            skill_list.append(entry)
            g = _sg_map.get(k, "未分组")
            skills_by_group.setdefault(g, []).append(entry)

    # 插件（按用户自定义分组返回）
    plugin_list: list[dict] = []
    plugins_by_group: dict[str, list[dict]] = {}
    if kinds_filter is None or "plugins" in kinds_filter:
        user_groups = _store.load_capability_groups("plugins")
        _pg_map: dict[str, str] = {}
        for g, names in user_groups.items():
            for n in names:
                _pg_map[n] = g
        for k, v in _list_plugins().items():
            label = v.get("label", k)
            desc = v.get("description", "")
            if not _match(f"{k} {label} {desc}"):
                continue
            entry = {"name": k, "label": label, "description": desc}
            plugin_list.append(entry)
            g = _pg_map.get(k, "未分组")
            plugins_by_group.setdefault(g, []).append(entry)

    return {
        "commands": commands_by_group,
        "skills": skills_by_group,
        "plugins": plugins_by_group,
        "_flat_skills": skill_list,
        "_flat_plugins": plugin_list,
        "keyword": keyword,
        "_tips": [
            "创建智能体时请根据其职责从上述列表中选择合适的 commands / skills / plugins 配置进去，不要留空",
            "命令 (commands) 是内建工具，技能 (skills) 是专业工作流，插件 (plugins) 是外部功能扩展",
            "cluster.* 系列命令已默认给编排师自己使用，不需要分配给普通智能体",
            "命令按内置功能分类分组（文件操作/智能命令/命令运行/集群编排），技能和插件按用户自定义分组组织",
        ],
    }


# ---- cluster.* 的 OpenAI function calling schema ----
CLUSTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cluster.get_agent",
            "description": "查看一个或多个智能体的详细信息，支持批量传入。agents 元素可以是 id 或名称（支持模糊匹配）。fields 可选：只返回指定字段（如 ['id','name','system_prompt']），避免无关的命令/插件/提示词等冗长内容挤占上下文；不传则返回完整对象。想先搜索/筛选有哪些智能体时用 cluster.search_agents。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {"type": ["integer", "string"]},
                        "description": "要查看的智能体 id 或名称列表（支持批量、支持模糊匹配），如 [3, \"数据分析师\"]"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：只返回这些字段，如 ['id','name','system_prompt','commands']。可填字段：id / name / description / system_prompt / model / commands / skills / plugins / trigger / tags / enabled / group_id / group_name / max_tool_rounds / created_at。不传=完整返回。"
                    }
                },
                "required": ["agents"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.search_agents",
            "description": "按特征关键字搜索智能体。keyword 支持多个（空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。默认在 name / description / system_prompt / model / tags / commands / skills / plugins 全文搜索。match_in 可选限定范围（如 ['system_prompt','name']）。fields 可选只返回指定字段。limit 可选限制返回条数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键字，多个可空格/逗号分隔"},
                    "match_in": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["name","description","system_prompt","model","tags","commands","skills","plugins"]},
                        "description": "可选：限定在哪些字段里搜索；不传则在全部可搜索字段里匹配"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：只返回这些字段（同 cluster.get_agent 的 fields 参数说明）"
                    },
                    "limit": {"type": "integer", "description": "可选：最多返回多少条"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_capabilities",
            "description": "查询系统中可用的命令 / 技能 / 插件能力清单。keyword 可选：按关键字过滤（多关键字空格/逗号分隔，任一命中即保留），匹配名称/label/description；不传则返回全部。kinds 可选：限定只返回哪些类型（commands/skills/plugins），避免无关信息。在创建或修改智能体之前用它挑选合适的 commands / skills / plugins。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "可选：关键字过滤（大小写不敏感，多关键字空格/逗号分隔，任一命中即保留）"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["commands", "skills", "plugins"]},
                        "description": "可选：限定返回哪些类型，如 ['commands','skills']；不传则三种都返回"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_agent",
            "description": "创建一个新的智能体。强烈建议在调用前先执行 cluster.list_capabilities 了解有哪些命令/技能/插件可用，然后根据这个智能体的职责主动配置 commands、skills、plugins（不要留空）。返回新智能体的完整信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "智能体名称，简明扼要"},
                    "description": {"type": "string", "description": "一句话描述这个智能体的职责"},
                    "system_prompt": {"type": "string", "description": "系统提示词，详细定义这个智能体的角色、工作方式和边界"},
                    "model": {"type": "string", "description": "可选：指定使用的模型，留空则用默认"},
                    "commands": {"type": "array", "items": {"type": "string"}, "description": "建议填写：这个智能体加载的命令工具列表（如 file.read、file.write、cmd.run 等）。可用 cluster.list_capabilities 查询所有可用命令名称"},
                    "skills": {"type": "array", "items": {"type": "string"}, "description": "建议填写：加载的技能列表（如 pptx、novel_writing 等）。可用 cluster.list_capabilities 查询所有可用技能名称"},
                    "plugins": {"type": "array", "items": {"type": "string"}, "description": "建议填写：加载的插件列表（如 browser_control、send_email 等）。可用 cluster.list_capabilities 查询所有可用插件名称"},
                    "max_tool_rounds": {"type": "integer", "description": "可选：单次回复最大工具调用轮数（默认取全局设置 50）"},
                    "group_id": {"type": "integer", "description": "可选：智能体所属分组 id（用 cluster.list_groups 查询）；省略=未分组"}
                },
                "required": ["name", "system_prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.update_agent",
            "description": "更新一个已存在智能体的属性（名称、提示词、工具列表等）。agent_id 必填，其余仅传要改的字段。如需修改 commands / skills / plugins，建议先执行 cluster.list_capabilities 查询当前可用的能力清单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": ["integer", "string"], "description": "要修改的智能体 id 或名称"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "commands": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "plugins": {"type": "array", "items": {"type": "string"}},
                    "max_tool_rounds": {"type": "integer", "description": "单次回复最大工具调用轮数"},
                    "group_id": {"type": "integer", "description": "所属分组 id（用 cluster.list_groups 查询）"}
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.delete_agent",
            "description": "删除一个或多个智能体（支持批量），并自动从所有画布中清理掉它们对应的节点和连线。批量删除优先用 agent_ids 传数组；agent_id 只删单个，两者可同时使用。返回每个智能体的删除结果（含失败项）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": ["integer", "string"], "description": "要删除的单个智能体 id 或名称"},
                    "agent_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "批量删除：智能体 id 或名称数组，如 [3, 5, \"数据分析师\"]"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_canvas",
            "description": "新建一张空的画布（集群编排图）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "画布名称，如「产品研发集群」「营销策划集群」"},
                    "description": {"type": "string", "description": "可选：集群的整体描述"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.delete_canvas",
            "description": "删除一张画布及其所有节点和连线。",
            "parameters": {
                "type": "object",
                "properties": {"canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"}},
                "required": ["canvas_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.get_canvas",
            "description": "查看一张画布的详细内容：节点、连线、每个节点对应的智能体名称。canvas_id 支持传画布名称（模糊匹配）。fields 可选：只返回指定字段（如 ['id','name','nodes_count','edges_count']），避免拿到冗长的 nodes/edges 列表；不传则返回完整画布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或画布名称（支持模糊匹配）"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：只返回这些字段，如 ['id','name','description','nodes_count','edges_count','created_at','nodes','edges']。不传=完整返回（nodes 内含 agent_name 补充）。"
                    }
                },
                "required": ["canvas_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.search_canvases",
            "description": "按特征关键字搜索画布。keyword 支持多个（空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。默认在画布 name / description / 画布关联的规章制度内容 / 画布节点所包含的智能体名称 这四类信息里搜索；match_in 可限定范围（rules 表示画布规章制度内容，agents_in_canvas 表示画布上有哪些智能体）。fields 可选只返回指定字段，limit 可选限制条数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键字，多个可空格/逗号分隔"},
                    "match_in": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["name","description","rules","agents_in_canvas"]},
                        "description": "可选：限定搜索范围。name=画布名称，description=画布描述，rules=画布绑定的规章制度内容，agents_in_canvas=画布节点上的智能体名称"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：只返回这些字段（同 cluster.get_canvas 的 fields 参数说明）"
                    },
                    "limit": {"type": "integer", "description": "可选：最多返回多少条"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.add_node",
            "description": "把一个智能体添加为画布上的节点。canvas_id / agent_id 都可以传 id 或名称。位置自动排布，也可以手动指定 x、y。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"},
                    "agent_id": {"type": ["integer", "string"], "description": "智能体 id 或名称"},
                    "x": {"type": "number", "description": "可选：节点 x 坐标，留空自动排布"},
                    "y": {"type": "number", "description": "可选：节点 y 坐标，留空自动排布"}
                },
                "required": ["canvas_id", "agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.remove_node",
            "description": "从画布上移除一个节点（node_id），同时清理相关连线。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"},
                    "node_id": {"type": "integer", "description": "要移除的节点 id"}
                },
                "required": ["canvas_id", "node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.connect",
            "description": "在同一张画布上，把 from_node_id 和 to_node_id 用箭头连线，表示协作依赖或信息流向。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"},
                    "from_node_id": {"type": "integer", "description": "起点节点 id"},
                    "to_node_id": {"type": "integer", "description": "终点节点 id"},
                    "edge_type": {"type": "string", "enum": ["chat", "handoff"], "description": "连线类型，默认 chat"}
                },
                "required": ["canvas_id", "from_node_id", "to_node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.disconnect",
            "description": "删除一条画布上的连线。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"},
                    "from_node_id": {"type": "integer"},
                    "to_node_id": {"type": "integer"}
                },
                "required": ["canvas_id", "from_node_id", "to_node_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_rules",
            "description": "列出指定画布的规章制度（集群工作流程/规则）。规章制度按画布隔离，不同画布独立设置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或名称"}
                },
                "required": ["canvas_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_rule",
            "description": "向指定画布新增一条规章制度，运行时会注入该画布智能体的系统提示词。agent_ids 省略或 null 表示注入该画布全部智能体；传数组表示仅指定智能体。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "规章制度内容，一条一句/一段，如「先输出大纲再写正文」"},
                    "canvas_id": {"type": ["integer", "string"], "description": "归属画布 id 或名称"},
                    "agent_ids": {"type": ["array", "null"], "items": {"type": "integer"}, "description": "可选：限定注入的智能体 id 列表；省略/null=该画布全部智能体"}
                },
                "required": ["content", "canvas_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.update_rule",
            "description": "修改已有规章制度：改内容、改注入范围（指定智能体或全部）、启用/停用。rule_id 用 cluster.list_rules 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer", "description": "规章制度 id"},
                    "content": {"type": "string", "description": "可选：新内容"},
                    "agent_ids": {"type": ["array", "null"], "items": {"type": "integer"}, "description": "可选：改为仅这些智能体注入"},
                    "apply_to_all": {"type": "boolean", "description": "true=改为注入该画布全部智能体（忽略 agent_ids）"},
                    "enabled": {"type": "boolean", "description": "true=启用；false=停用（停用后不注入）"}
                },
                "required": ["rule_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.delete_rule",
            "description": "删除一条规章制度。rule_id 用 cluster.list_rules 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer", "description": "规章制度 id"}
                },
                "required": ["rule_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_groups",
            "description": "列出所有智能体分组及其包含的智能体数量。【不需要任何参数，留空即可】",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_group",
            "description": "新建一个智能体分组，用于在智能体列表里收纳归类。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "分组名称，如「内容创作」「工程研发」"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.update_group",
            "description": "重命名一个智能体分组。group_id 用 cluster.list_groups 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "分组 id"},
                    "name": {"type": "string", "description": "新的分组名称"}
                },
                "required": ["group_id", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.delete_group",
            "description": "删除一个智能体分组；组内的智能体会自动移回「全部」组（不会被删除）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "要删除的分组 id"}
                },
                "required": ["group_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.assign_agent_group",
            "description": "把某个智能体归纳到指定分组；group_id 传 null 或省略表示移回「全部」组。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": ["integer", "string"], "description": "智能体 id 或名称"},
                    "group_id": {"type": "integer", "description": "目标分组 id（用 cluster.list_groups 查询）；null/省略=移回「全部」组"}
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_conversation",
            "description": "新建一个会话。可绑定画布（绑定为集群协作模式，未指定工作区/运行模式时自动继承画布设置）、指定成员智能体、设置工作区绝对路径 workdir 与串/并行运行模式 execution_mode（serial=串行接力，parallel=并行并发）。「启动一张画布」的第一步：先用本命令绑画布建会话，再 cluster.continue_conversation 向画布上某个智能体发初始任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "可选：会话名称；绑画布且省略时自动取「画布名 · 集群」"},
                    "canvas_id": {"type": ["integer", "string"], "description": "可选：绑定的画布 id 或名称（集群协作模式）；省略=独立对话"},
                    "agent_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "可选：会话成员智能体（id 或名称），运行时可用 cluster.continue_conversation 追加"},
                    "workdir": {"type": "string", "description": "可选：工作区绝对路径（不存在会自动创建）；省略且绑画布时继承画布工作区"},
                    "execution_mode": {"type": "string", "enum": ["serial", "parallel"], "description": "可选：串/并行运行模式；省略且绑画布时继承画布设置"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.update_conversation",
            "description": "编辑一个已有会话：改名、改工作区（workdir）、切换串/并行运行模式（execution_mode）、增删成员智能体、清空对话历史。workdir / execution_mode 传空字符串表示清除（回归画布/全局默认）。不支持更换绑定的画布（需要换画布请新建会话）。conversation_id 用 cluster.list_conversations 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": ["integer", "string"], "description": "会话 id"},
                    "name": {"type": "string", "description": "可选：新会话名"},
                    "workdir": {"type": "string", "description": "可选：新工作区绝对路径；空串=清除"},
                    "execution_mode": {"type": "string", "enum": ["serial", "parallel"], "description": "可选：切换串/并行模式；空串=清除（回归画布默认）"},
                    "agent_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "可选：整组替换会话成员智能体"},
                    "reset_messages": {"type": "boolean", "description": "true=清空对话历史（不删除会话）"}
                },
                "required": ["conversation_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.continue_conversation",
            "description": "继续会话：向会话中的某个智能体发送消息并立即启动后台运行（该智能体不在会话成员里会自动加入）。这是「运行/启动智能体」的标准方式；画布会话中向任一节点智能体发消息即可启动整张画布的串/并行协作。可在继续时顺手传 workdir / execution_mode 切换该会话的工作区或运行模式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": ["integer", "string"], "description": "会话 id（cluster.list_conversations 查询）"},
                    "agent_id": {"type": ["integer", "string"], "description": "要发言的智能体 id 或名称（画布场景=从该节点启动画布）"},
                    "message": {"type": "string", "description": "发给该智能体的消息/任务内容"},
                    "workdir": {"type": "string", "description": "可选：继续前切换会话工作区绝对路径"},
                    "execution_mode": {"type": "string", "enum": ["serial", "parallel"], "description": "可选：继续前切换串/并行运行模式"}
                },
                "required": ["conversation_id", "agent_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.stop_agent",
            "description": "停止运行中的智能体。传 conversation_id（可选配 agent_id 精确停该会话中某个智能体；缺省停整个会话全部运行）；或只传 agent_id 停该智能体在所有会话中的运行。建议先 cluster.list_running 查看当前运行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": ["integer", "string"], "description": "可选：要停止的智能体 id 或名称"},
                    "conversation_id": {"type": ["integer", "string"], "description": "可选：要停止的会话 id"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_running",
            "description": "查看当前所有正在运行的智能体：所在会话、归属画布、开始时间。用于回答「现在谁在跑」「启动画布前/停止前先看看状态」。【不需要任何参数，留空即可】",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.search_conversations",
            "description": "按特征关键字搜索会话。keyword 支持多个（空格/逗号/中文顿号分隔，任一命中即召回），大小写不敏感。默认在会话 name / 最后一条消息预览 / 会话绑定的智能体名称 / 会话绑定的画布名称 这四类信息里搜索；kinds 可限定范围（name / preview / agent_names / canvas_name）。limit 可选限制条数。返回结构与 cluster.list_conversations 一致，按相关度排序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键字，多个可空格/逗号分隔"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["name", "preview", "agent_names", "canvas_name"]},
                        "description": "可选：限定搜索范围。name=会话名，preview=最后一条消息预览，agent_names=会话成员智能体，canvas_name=会话绑定的画布"
                    },
                    "limit": {"type": "integer", "description": "可选：最多返回多少条"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_conversations",
            "description": "列出会话（新→旧），含每个会话的名称、绑定画布、成员智能体、工作区、串/并行模式、消息数、最后预览与运行状态。用于找 conversation_id、了解有哪些对话在进行。需要按关键字过滤时改用 cluster.search_conversations。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "可选：最多返回条数，默认 50"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.get_conversation",
            "description": "查看某个会话的对话记录（角色、发言智能体、时间、内容）。conversation_id 直接指定；或传 agent（智能体 id/名称）自动找其最新会话。默认只取最近 30 条、内容截断 400 字；full=true 返回完整内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": ["integer", "string"], "description": "可选：会话 id"},
                    "agent": {"type": ["integer", "string"], "description": "可选：按智能体（id 或名称）找其最新会话"},
                    "last_n": {"type": "integer", "description": "可选：只看最近 N 条消息，默认 30"},
                    "full": {"type": "boolean", "description": "可选：true=不截断消息内容，默认 false"}
                },
                "required": []
            }
        }
    },
]

# A2A 工具并入 cluster.* 工具表
# 成果移交协议停用（A2A_HANDOFF_ENABLED=False）时 handoff 不注册到可调用工具表，
# 避免 LLM 仍看到/调用 cluster.handoff；cmd_cluster_handoff 函数仍在 globals 中兜底报错
from core.a2a import A2A_HANDOFF_ENABLED
if A2A_HANDOFF_ENABLED:
    CLUSTER_TOOLS.append(HANDOFF_TOOL)
CLUSTER_TOOLS.append(CALL_TOOL)

# 合并到全局命令表，让 filter_tools 自动生效
COMMAND_TOOLS.extend(CLUSTER_TOOLS)

# 智能搜索子智能体命令（独立于 cluster.*，作为一项可勾选能力）
COMMAND_TOOLS.append(SMART_SEARCH_TOOL)

# 派生子智能体命令（主智能体自定义临时子 agent 并配置能力）
COMMAND_TOOLS.append(SUBAGENT_TOOL)

# ---- 命令分组（前端命令选择器按组展示，可整组勾选或展开后单独勾选）----
# 顺序即展示顺序；未列入分组的命令归入「其他」。
COMMAND_GROUPS = [
    ("文件操作", [
        "file.read", "file.write", "file.delete", "file.replace",
        "file.patch", "file.structure", "file.search", "file.find",
        "workspace.search",
    ]),
    ("智能命令", ["agent.smart_search", "agent.spawn"]),
    ("命令运行", ["cmd.run", "cmd.bg_list", "cmd.bg_stop", "cmd.bg_log"]),
    ("集群编排", [
        "cluster.get_agent", "cluster.search_agents",
        "cluster.list_capabilities",
        "cluster.create_agent", "cluster.update_agent", "cluster.delete_agent",
        "cluster.list_groups", "cluster.create_group", "cluster.update_group",
        "cluster.delete_group", "cluster.assign_agent_group",
        "cluster.create_canvas", "cluster.delete_canvas",
        "cluster.get_canvas", "cluster.search_canvases",
        "cluster.add_node", "cluster.remove_node",
        "cluster.connect", "cluster.disconnect",
        "cluster.list_rules", "cluster.create_rule", "cluster.update_rule", "cluster.delete_rule",
        "cluster.create_conversation", "cluster.update_conversation", "cluster.continue_conversation",
        "cluster.stop_agent", "cluster.list_running",
        "cluster.list_conversations", "cluster.search_conversations", "cluster.get_conversation",
    ]),
]

# 运行时自动注入的常驻工具：不在智能体配置/默认命令选择器里展示（无需也无法手动勾选）。
RUNTIME_INJECTED_COMMANDS = {
    "skill.invoke", "skill.read_section", "cluster.send_message", "cluster.handoff",
}

_NAME_TO_GROUP: dict[str, str] = {}
for _gname, _names in COMMAND_GROUPS:
    for _n in _names:
        _NAME_TO_GROUP[_n] = _gname

COMMAND_META = {}
for t in COMMAND_TOOLS:
    name = t["function"]["name"]
    meta = {
        "label": COMMAND_CN_LABELS.get(name, name),
        "params": list(t["function"]["parameters"]["properties"].keys()),
    }
    if name in _NAME_TO_GROUP:
        meta["group"] = _NAME_TO_GROUP[name]
    if name in RUNTIME_INJECTED_COMMANDS:
        meta["hidden"] = True
    COMMAND_META[name] = meta
