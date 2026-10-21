"""预设命令系统 - 文件管理、CMD 运行
每个命令带完整的 JSON Schema 描述，用于 LLM function calling。

路径解析约定：
  - 画布模式（canvas.workdir 已注入 WORKDIR_CTX）：相对路径自动拼到 workdir 上；
  - 独立对话模式：优先用全局 settings.default_workdir；都没有则退回进程 CWD。
  - 绝对路径原样使用（无论哪种模式）。
"""
import os, subprocess, shutil, re, signal, socket, time, threading
from pathlib import Path
from typing import Any, Callable
from contextvars import ContextVar

# cluster.handoff / cluster.send_message（A2A 移交与发送消息（可往返多轮）工具）— 导入进本模块 globals，供 run_command 分发
from core.a2a import cmd_cluster_handoff, HANDOFF_TOOL, cmd_cluster_send_message, CALL_TOOL


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


def _start_bg_process(command: str, cwd: str, shell: bool = True) -> dict:
    """在后台启动一条长运行 / GUI 命令，立即返回状态。

    两类场景都走这里：
      (a) 常驻服务（uvicorn/node/redis...）→ Popen 进程自身就是服务进程，会一直活着
      (b) GUI 启动（start / 浏览器...）→ Popen 只是 shell 包装（cmd.exe），
          它会很快退出；真正的 GUI 子进程脱离出来独立运行。
          这种情况下 Popen 的退出是正常的，不算"秒挂"。
    """
    import tempfile
    _cleanup_dead_bg()

    # 把 stdout/stderr 重定向到独立日志文件（Windows 上用 subprocess.DEVNULL 可能丢日志）
    log_dir = os.path.join(tempfile.gettempdir(), "agent_cluster_bg")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"bg_{os.getpid()}_{int(time.time())}.log")

    try:
        log_fh = open(log_path, "w", encoding="utf-8", errors="ignore")
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

    pid = proc.pid
    # GUI 启动命令不把 shell wrapper 的 pid 记入后台表（它会秒退），
    # 直接跳过，让后续逻辑按"已启动"处理
    gui_cmd = _is_gui_launch(command)
    if not gui_cmd:
        with _BG_LOCK:
            _BG_PROCESSES[pid] = {
                "proc": proc,
                "command": command,
                "cwd": cwd,
                "started_at": time.time(),
                "log_path": log_path,
            }

    # 快速探活：等一小会儿看端口
    port = _extract_port(command)
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
            # 秒挂 → 记录日志，返回 error
            with _BG_LOCK:
                _BG_PROCESSES.pop(pid, None)
            tail = ""
            if os.path.isfile(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                        tail = lf.read()[-2000:]
                except Exception:
                    pass
            return {
                "error": f"进程启动后立即退出（exit_code={proc.returncode}）",
                "stdout_tail": tail,
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
        "command": command,
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


def _effective_workdir() -> str | None:
    """返回当前生效的工作区绝对路径（没有则返回 None）。"""
    # 1) 画布模式注入的 workdir
    wd = WORKDIR_CTX.get()
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wd_debug.log"), "a", encoding="utf-8") as _f:
            _f.write(f"{time.time():.3f} _effective_workdir ctx={wd!r} isdir={os.path.isdir(wd) if wd else None} cwd={os.getcwd()}\n")
    except Exception:
        pass
    if wd and os.path.isdir(wd):
        return wd
    # 2) 兜底：settings 里的 default_workdir（独立对话模式）
    try:
        from core.store import load
        s = load("settings") or {}
        dwd = s.get("default_workdir")
        if dwd and os.path.isdir(dwd):
            return dwd
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


def run_command(command_name: str, **kwargs) -> Any:
    fn = globals().get(f"cmd_{command_name.replace('.','_')}")
    return fn(**kwargs) if fn else {"error": f"unknown command: {command_name}"}


_IMG_READ_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_IMG_READ_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


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
        # 快照
        _snapshot(str(p), "file.replace")
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if max_replacements > 0: text = text.replace(old, new, max_replacements)
        else: text = text.replace(old, new)
        p.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(p), "replaced": count}
    except Exception as e:
        return {"error": str(e), "resolved": str(_resolve(path))}


def cmd_file_patch(path: str, old: str, new: str, mode: str = "replace") -> dict:
    """按行定位的定点编辑：只改文件中包含锚点 old 的那一行及其插入位置，
    不动其他任何内容，避免为了改一处而重写整个文件。

    mode：
      replace     —— 把该行内 old 片段换成 new
      delete      —— 删掉该行内的 old 片段
      insert_after —— 在锚点整行之后插入 new（按完整行追加）
      insert_before—— 在锚点整行之前插入 new（按完整行追加）
    锚点 old 必须完整落在同一行内（跨行锚点会报错），保证定位精确。
    """
    try:
        p = _resolve(path)
        # 快照
        _snapshot(str(p), "file.patch")
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if old and old in ln]
        if not hits:
            return {"error": "anchor not found: 锚点文本在文件中找不到，先 file.read 确认原文",
                    "path": str(p)}
        li = hits[0]
        block = [b for b in new.splitlines()] if new else []
        if mode == "replace":
            lines = lines[:li] + [lines[li].replace(old, new, 1)] + lines[li + 1:]
        elif mode == "delete":
            lines = lines[:li] + [lines[li].replace(old, "", 1)] + lines[li + 1:]
        elif mode == "insert_after":
            lines = lines[:li + 1] + block + lines[li + 1:]
        elif mode == "insert_before":
            lines = lines[:li] + block + lines[li:]
        else:
            return {"error": f"unknown mode: {mode}, 可选 replace/delete/insert_after/insert_before",
                    "path": str(p)}
        trailing = "\n" if text.endswith("\n") else ""
        p.write_text("\n".join(lines) + trailing, encoding="utf-8")
        return {"ok": True, "path": str(p), "mode": mode,
                "line": li + 1, "occurrences": len(hits),
                "new_size": p.stat().st_size}
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
                    "path": str(p if abs else p.relative_to(root)),
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
        for p in root.rglob("*"):
            if p == root:
                continue
            name = p.name
            ok = fnmatch.fnmatch(name.lower(), glob_pat.lower()) if i else fnmatch.fnmatch(name, glob_pat)
            if not ok:
                continue
            if type == "file" and not p.is_file():
                continue
            if type == "dir" and not p.is_dir():
                continue
            results.append(str(p.relative_to(root)))
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
                background: bool | None = None) -> dict:
    """执行一条系统命令。

    长运行自动后台化规则：
      - 若显式传 background=True → 一定后台启动，立即返回 pid
      - 若显式传 background=False → 强制前台（阻塞直到退出或超时）
      - 若省略 / null（默认）：自动检测命令关键词（http.server / uvicorn / node / vite / watch 等），
        命中则后台启动；否则前台执行
    """
    try:
        # 显式传了 cwd 就用它，否则回落到 workdir 或 CWD
        if cwd == ".":
            wd = _effective_workdir()
            cwd_eff = wd if wd else os.getcwd()
        else:
            cwd_eff = str(_resolve(cwd))

        # —— 判断是否需要后台启动 ——
        # 两类情况都要后台/立即返回：
        #   (a) 常驻服务（uvicorn/node/redis...）—— 会一直运行不退出
        #   (b) GUI 启动（start/浏览器/explorer...）—— 若走 subprocess.run + capture_output，
        #       GUI 子进程会继承 pipe 句柄，Python 永远等不到输出关闭 → 卡住
        should_bg = background
        if should_bg is None:
            should_bg = _is_long_running(command) or _is_gui_launch(command)

        if should_bg:
            return _start_bg_process(command, cwd_eff, shell=shell)

        # 前台阻塞执行（短命令）
        r = subprocess.run(command, shell=shell, capture_output=True, text=True, cwd=cwd_eff,
                           timeout=timeout, encoding="utf-8", errors="ignore")
        return {"stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:],
                "returncode": r.returncode, "command": command, "cwd": cwd_eff}
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
            "description": "读取指定文件的内容。默认返回完整内容；也可以传 start_line / end_line 只读指定行区间（大文件分段查看）。用于查看代码、文档、配置等文本文件。图片文件（png/jpg/jpeg/gif/webp/bmp）会以图片形式返回（不返回文本乱码），模型可直接看到图片内容做视觉分析。",
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
            "description": "在文件中查找并替换指定文本片段。",
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
            "description": "定点修改已有文件：以锚点 old 定位到那一行，只改或插入那一处，绝不重写整个文件。先 file.read 拿到锚点原文，再按 mode 选择替换该处 / 删除该处 / 在该行前后插入新内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old": {"type": "string", "description": "锚点：文件中必须存在且完整落在同一行内的原文片段，用于定位修改位置"},
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
                "\n慎用，避免破坏性命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "完整命令字符串，如 dir /a"},
                    "cwd": {"type": "string", "description": "工作目录，默认当前目录"},
                    "timeout": {"type": "integer", "description": "前台执行超时秒数，默认60"},
                    "shell": {"type": "boolean", "description": "是否通过 shell 执行，默认true"},
                    "background": {"type": "boolean", "description": "可选：true=强制后台启动，false=强制前台；省略则自动识别长运行关键词"}
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
    "skill.invoke": "🔧 调用技能",
    "skill.read_section": "📑 读取技能章节",
    "cmd.run": "🚀 运行命令",
    "cmd.bg_list": "📋 后台进程",
    "cmd.bg_stop": "🛑 停止进程",
    "cmd.bg_log": "📜 进程日志",
    "cluster.list_agents": "👥 列出智能体",
    "cluster.create_agent": "🤖 创建智能体",
    "cluster.update_agent": "✏️ 更新智能体",
    "cluster.delete_agent": "🗑 删除智能体",
    "cluster.list_canvases": "🎨 列出画布",
    "cluster.create_canvas": "🌟 创建画布",
    "cluster.delete_canvas": "🗑 删除画布",
    "cluster.get_canvas": "👁 查看画布",
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


def filter_tools(command_names: list, plugin_names: list, plugin_registry: dict, skills: list | None = None) -> list:
    """根据 agent 的能力列表，构建要传给 LLM 的 tools。

    所有 tool 的 parameters 都强制加 additionalProperties=False，
    明确禁止 LLM 传 schema 未声明的多余字段（如凭空加 root / 空键等）。
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


def cmd_cluster_list_agents() -> dict:
    items = _load_all("agents")
    return {"agents": [{
        "id": a.get("id"),
        "name": a.get("name"),
        "description": a.get("description", ""),
        "commands": a.get("commands", []),
        "skills": a.get("skills", []),
        "plugins": a.get("plugins", []),
    } for a in items]}


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


def cmd_cluster_delete_agent(agent_id: int) -> dict:
    # 同时从所有画布的 nodes / edges 里清掉
    canvases = _load_all("canvases")
    changed = 0
    for c in canvases:
        prev = len(c.get("nodes", []))
        c["nodes"] = [n for n in c.get("nodes", []) if n.get("agent_id") != agent_id]
        if len(c["nodes"]) != prev:
            changed += 1
            remaining_ids = {n["id"] for n in c["nodes"]}
            c["edges"] = [e for e in c.get("edges", []) if e.get("from_node") in remaining_ids and e.get("to_node") in remaining_ids]
            _save_all("canvases", canvases)
    ok = _delete_by("agents", agent_id)
    return {"ok": ok, "cleaned_from_canvases": changed}


def cmd_cluster_list_canvases() -> dict:
    items = _load_all("canvases")
    return {"canvases": [{
        "id": c.get("id"), "name": c.get("name"), "description": c.get("description", ""),
        "nodes_count": len(c.get("nodes", [])),
        "edges_count": len(c.get("edges", [])),
    } for c in items]}


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


def cmd_cluster_get_canvas(canvas_id: int | str) -> dict:
    c = _find_canvas(canvas_id)
    if not c: return {"error": f"canvas {canvas_id} not found"}
    # 给每个 node 补上 agent 名称方便 LLM 理解
    agent_map = {a["id"]: a["name"] for a in _load_all("agents")}
    enriched = {
        **c,
        "nodes": [{**n, "agent_name": agent_map.get(n["agent_id"], "?")} for n in c.get("nodes", [])],
    }
    return {"canvas": enriched}


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


# ---- cluster.* 的 OpenAI function calling schema ----
CLUSTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cluster.list_agents",
            "description": "列出系统中所有已创建的智能体及其简要信息。【不需要任何参数，留空即可】",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.create_agent",
            "description": "创建一个新的智能体，包含名称、系统提示词、可选的命令/技能/插件列表。返回新智能体的完整信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "智能体名称，简明扼要"},
                    "description": {"type": "string", "description": "一句话描述这个智能体的职责"},
                    "system_prompt": {"type": "string", "description": "系统提示词，详细定义这个智能体的角色、工作方式和边界"},
                    "model": {"type": "string", "description": "可选：指定使用的模型，留空则用默认"},
                    "commands": {"type": "array", "items": {"type": "string"}, "description": "可选：这个智能体加载的命令工具列表"},
                    "skills": {"type": "array", "items": {"type": "string"}, "description": "可选：加载的技能列表"},
                    "plugins": {"type": "array", "items": {"type": "string"}, "description": "可选：加载的插件列表"},
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
            "description": "更新一个已存在智能体的属性（名称、提示词、工具列表等）。agent_id 必填，其余仅传要改的字段。",
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
            "description": "删除一个智能体，并自动从所有画布中清理掉它对应的节点和连线。",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": ["integer", "string"], "description": "要删除的智能体 id 或名称"}},
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_canvases",
            "description": "列出所有画布（集群编排图），每张画布显示节点/连线数量。【不需要任何参数，留空即可】",
            "parameters": {"type": "object", "properties": {}, "required": []}
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
            "description": "查看一张画布的详细内容：节点、连线、每个节点对应的智能体名称。canvas_id 也支持传画布名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "canvas_id": {"type": ["integer", "string"], "description": "画布 id 或画布名称（支持模糊匹配）"}
                },
                "required": ["canvas_id"]
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

# ---- 命令分组（前端命令选择器按组展示，可整组勾选或展开后单独勾选）----
# 顺序即展示顺序；未列入分组的命令归入「其他」。
COMMAND_GROUPS = [
    ("文件操作", [
        "file.read", "file.write", "file.delete", "file.replace",
        "file.patch", "file.structure", "file.search", "file.find",
    ]),
    ("工作区检索", ["workspace.search"]),
    ("命令运行", ["cmd.run", "cmd.bg_list", "cmd.bg_stop", "cmd.bg_log"]),
    ("集群编排", [
        "cluster.list_agents", "cluster.create_agent", "cluster.update_agent", "cluster.delete_agent",
        "cluster.list_groups", "cluster.create_group", "cluster.update_group",
        "cluster.delete_group", "cluster.assign_agent_group",
        "cluster.list_canvases", "cluster.create_canvas", "cluster.delete_canvas",
        "cluster.get_canvas", "cluster.add_node", "cluster.remove_node",
        "cluster.connect", "cluster.disconnect",
        "cluster.list_rules", "cluster.create_rule", "cluster.update_rule", "cluster.delete_rule",
    ]),
]

# 运行时自动注入的常驻工具：不在智能体配置/默认命令选择器里展示（无需也无法手动勾选）。
RUNTIME_INJECTED_COMMANDS = {"skill.invoke", "skill.read_section", "cluster.send_message", "cluster.handoff"}

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
