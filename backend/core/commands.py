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
# 长运行命令关键词（小写匹配），命中任一 → 自动后台启动
_LONG_RUN_KEYWORDS = [
    # 静态服务器
    "http.server", "http.serve", "simplehttpserver",
    # Web 框架 dev server
    "uvicorn", "gunicorn", "flask run", "flask --app run", "django-admin runserver",
    "manage.py runserver", "streamlit run", "gradio",
    # Node.js
    "node ", "npm run dev", "npm run start", "npm run serve", "npx", "vite",
    "webpack-dev-server", "next dev", "next start", "react-scripts start",
    # 文件监听 / 守护进程
    "watch", "tail -f", "tail -F", "nodemon", "forever", "pm2", "supervisord",
    # 数据库 / 缓存服务
    "redis-server", "mongod", "mysqld", "postgres", "postgres -", "memcached",
    "elasticsearch", "kibana", "logstash",
    # 消息队列 / 代理
    "rabbitmq", "kafka", "zookeeper", "nginx", "apache2", "httpd",
    # 其他常驻服务
    "daemon", "serve -l", "serve -p",
]

# 正在运行的后台进程表：pid → {"proc": Popen, "command": str, "cwd": str, "started_at": float, "log_path": str}
_BG_PROCESSES: dict[int, dict] = {}
_BG_LOCK = threading.Lock()


def _is_long_running(command: str) -> bool:
    """判断一条命令是否看起来是长运行（后台）进程。"""
    if not command:
        return False
    lower = command.lower()
    return any(kw in lower for kw in _LONG_RUN_KEYWORDS)


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


def _start_bg_process(command: str, cwd: str, shell: bool = True) -> dict:
    """在后台启动一条长运行命令，立即返回状态。"""
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

    proc_info = _BG_PROCESSES.get(pid, {})
    still_alive = proc.poll() is None
    if not still_alive:
        # 启动秒挂
        with _BG_LOCK:
            _BG_PROCESSES.pop(pid, None)
        # 读一下日志
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

    result = {
        "ok": True,
        "pid": pid,
        "command": command,
        "cwd": cwd,
        "running": True,
        "background": True,
        "note": "已后台启动（长运行命令自动后台化，不阻塞智能体）",
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


def cmd_file_read(path: str, encoding: str = "utf-8", max_chars: int = 8000,
                  line_numbers: bool = False) -> dict:
    try:
        p = _resolve(path)
        text = p.read_text(encoding=encoding, errors="ignore")
        if line_numbers:
            # 带行号（1 起，右对齐宽度自适应），方便配合 file.patch 按行定点修改
            pad = len(str(len(text.splitlines())))
            text = "\n".join(f"{i + 1:>{pad}}\t{ln}" for i, ln in enumerate(text.splitlines()))
        if len(text) > max_chars:
            text = f"...truncated ({len(text)} chars)...\n" + text[-max_chars:]
        return {"content": text, "path": str(p)}
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


# 新增文件内容搜索命令
def cmd_file_search(pattern: str,
                    path: str = ".",
                    glob: str = "**/*",
                    i: bool = False,
                    abs: bool = False) -> dict:
    """在给定路径(默认工作目录)内搜索匹配 pattern 的文件内容。
    返回匹配文件列表及对应匹配行。"""
    try:
        from pathlib import Path
        root = _resolve(path)
        matches = []
        for p in root.rglob(glob):
            if p.is_file():
                try:
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                import re
                flags = re.MULTILINE | (re.IGNORECASE if i else 0)
                comp = re.compile(pattern, flags)
                lines = [ln for ln in txt.splitlines() if comp.search(ln)]
                if lines:
                    matches.append({"path": str(p if abs else p.relative_to(root)), "lines": lines})
        return {"matches": matches, "count": len(matches)}
    except Exception as e:
        return {"error": str(e)}


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
        should_bg = background
        if should_bg is None:
            should_bg = _is_long_running(command)

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
            "description": "读取指定文件的完整内容。用于查看代码、文档、配置等文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对或相对路径，如 ./main.py"},
                    "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
                    "max_chars": {"type": "integer", "description": "最多返回的字符数，超过会截断"},
                    "line_numbers": {"type": "boolean", "description": "是否在每行前加 1 起的行号（配合 file.patch 按行定点编辑时建议开启）"}
                },
                "required": ["path"]
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
            "description": "在文件系统中搜索文件内容：按正则或关键字在指定目录（默认工作目录）递归查找，返回命中的文件路径与匹配行。适合先搜索定位再 file.read / file.patch。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜索的正则表达式或关键字，如 'def main' 或 'TODO'"},
                    "path": {"type": "string", "description": "搜索根目录，默认当前工作目录（相对路径自动解析到工作目录）"},
                    "glob": {"type": "string", "description": "限定搜索的文件 glob 模式，默认 '**/*'；例 '**/*.py' 只搜 Python 文件"},
                    "i": {"type": "boolean", "description": "忽略大小写搜索，默认 false"},
                    "abs": {"type": "boolean", "description": "返回绝对路径，默认相对搜索根目录"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cmd.run",
            "description": (
                "在系统 shell 中运行一条命令（Windows cmd / Linux bash）。"
                "系统会自动识别长运行命令并以后台方式启动（如 http.server / uvicorn / node / vite / watch 等），"
                "后台启动时立即返回 pid、端口探活结果，不会阻塞智能体。"
                "短命令（如 dir / ls / pip install）正常前台执行。"
                "也可显式传 background=true 强制后台，或 background=false 强制前台。"
                "慎用，避免破坏性命令。"
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
            "description": "列出当前所有由 cmd.run 后台启动的进程状态（pid、命令、运行时长、日志路径）。",
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
COMMAND_CN_LABELS = {
    "file.read": "读文件",
    "file.write": "写文件",
    "file.delete": "删文件",
    "file.replace": "替换文本",
    "file.patch": "补丁编辑",
    "file.structure": "文件树",
    "file.search": "搜索内容",
    "cmd.run": "运行命令",
    "cmd.bg_list": "后台进程",
    "cmd.bg_stop": "停止进程",
    "cmd.bg_log": "进程日志",
    "cluster.list_agents": "列出智能体",
    "cluster.create_agent": "创建智能体",
    "cluster.update_agent": "更新智能体",
    "cluster.delete_agent": "删除智能体",
    "cluster.list_canvases": "列出画布",
    "cluster.create_canvas": "创建画布",
    "cluster.delete_canvas": "删除画布",
    "cluster.get_canvas": "查看画布",
    "cluster.add_node": "添加节点",
    "cluster.remove_node": "移除节点",
    "cluster.connect": "连接节点",
    "cluster.disconnect": "断开连线",
    "cluster.list_rules": "列出规章制度",
    "cluster.create_rule": "新增规章制度",
    "cluster.update_rule": "修改规章制度",
    "cluster.delete_rule": "删除规章制度",
    "cluster.handoff": "A2A 移交",
    "cluster.send_message": "发送消息",
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


def filter_tools(command_names: list, plugin_names: list, plugin_registry: dict) -> list:
    """根据 agent 的能力列表，构建要传给 LLM 的 tools"""
    result = []
    cmd_names = set(command_names or [])
    for t in COMMAND_TOOLS:
        if t["function"]["name"] in cmd_names:
            result.append(t)
    for pname in (plugin_names or []):
        meta = plugin_registry.get(pname)
        if meta:
            tool = meta.get("function_schema") or build_plugin_tool(pname, meta)
            result.append(_with_preset(tool, pname))
    return result


def _with_preset(tool: dict, pname: str) -> dict:
    """能力参数预设整形插件 schema：
    - 固定参数：从 properties/required 中剔除（模型不可见、不可改）；
    - 可选参数：只把文字描述附加到 tool description（供模型参考选择）。
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
                             max_tool_rounds: int | None = None) -> dict:
    from datetime import datetime
    item = {
        "id": _next_id("agents"),
        "name": name, "description": description, "system_prompt": system_prompt,
        "model": model, "interface_id": None,
        "commands": commands or [], "skills": skills or [], "plugins": plugins or [],
        "trigger": {"type": "manual", "cron": "", "message": ""},
        "tags": [], "enabled": True, "created_at": datetime.utcnow().isoformat(),
        "max_tool_rounds": max_tool_rounds,
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
                             max_tool_rounds: int | None = None) -> dict:
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


# ---- cluster.* 的 OpenAI function calling schema ----
CLUSTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cluster.list_agents",
            "description": "列出系统中所有已创建的智能体及其简要信息（id、名称、描述、加载的命令/技能/插件）。",
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
                    "max_tool_rounds": {"type": "integer", "description": "可选：单次回复最大工具调用轮数（默认取全局设置 50）"}
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
                    "agent_id": {"type": "integer", "description": "要修改的智能体 id"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "commands": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "plugins": {"type": "array", "items": {"type": "string"}},
                    "max_tool_rounds": {"type": "integer", "description": "单次回复最大工具调用轮数"}
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
                "properties": {"agent_id": {"type": "integer", "description": "要删除的智能体 id"}},
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cluster.list_canvases",
            "description": "列出所有画布（集群编排图），每张画布显示节点/连线数量。",
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
                "properties": {"canvas_id": {"type": "integer", "description": "画布 id"}},
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
# 更新旧元数据字典
COMMAND_META = {t["function"]["name"]: {
    "label": COMMAND_CN_LABELS.get(t["function"]["name"], t["function"]["name"]),
    "params": list(t["function"]["parameters"]["properties"].keys()),
} for t in COMMAND_TOOLS}
