"""Python 控制台：把 .py 文件作为交互式子进程运行，实时转发 stdout/stderr，
支持向 stdin 写入输入与强制停止（供工作区面板的「控制台」使用）。

设计：
- 每个会话一个独立子进程（<sys.executable> -u <file>），stdin/stdout/stderr 全用管道；
- 两个守护线程用 os.read 按原始字节读 stdout/stderr（不用 readline：
  input() 等提示语没有换行，逐行读取会把提示语卡在缓冲里），解码后追加到会话缓冲区；
- 前端通过 SSE 长连接持续 drain 缓冲区新内容；输入 / 停止 / 关闭走独立 POST 接口；
- 缓冲区有字符数上限，失控输出只保留最近 N 字符。
"""
import os
import sys
import time
import uuid
import signal
import threading
import subprocess
from collections import deque

# 缓冲区最大字符数（超出丢最旧的）
_MAX_BUFFER = 200_000

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _alive(proc) -> bool:
    return proc is not None and proc.poll() is None


def start(path: str, cwd: str | None = None) -> dict:
    """启动一个 Python 控制台会话。返回 {session_id, pid, command}。"""
    sid = uuid.uuid4().hex[:12]
    py = sys.executable or "python"
    cmd = [py, "-u", path]
    # 强制子进程 stdin/stdout/stderr 使用 UTF-8，否则 Windows 下默认 cp936
    # 会让中文输入/输出与本模块的 UTF-8 编码来回翻译产生乱码。
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or os.path.dirname(path) or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    except Exception as e:
        return {"error": f"启动失败：{e}"}
    info = {
        "proc": proc,
        "path": path,
        "cwd": cwd or os.path.dirname(path),
        "started_at": time.time(),
        "buffer": deque(),
        "chars": 0,
        "lock": threading.Lock(),
        "exit_code": None,
        "command": " ".join(cmd),
    }
    with _LOCK:
        _SESSIONS[sid] = info
    for stream in (proc.stdout, proc.stderr):
        threading.Thread(target=_pump, args=(sid, stream), daemon=True).start()
    threading.Thread(target=_monitor, args=(sid, proc), daemon=True).start()
    return {"session_id": sid, "pid": proc.pid, "command": info["command"]}


def _append(info: dict, text: str) -> None:
    with info["lock"]:
        info["buffer"].append(text)
        info["chars"] += len(text)
        while info["chars"] > _MAX_BUFFER and len(info["buffer"]) > 1:
            info["chars"] -= len(info["buffer"].popleft())


def _pump(sid: str, stream) -> None:
    """用 os.read 按原始字节读（readline 会把无换行的提示语卡住）。

    关闭时通过文件对象 stream.close() 释放 fd，避免直接 os.close 后
    文件对象在 GC 时重复关闭导致 "Bad file descriptor"。
    """
    try:
        fd = stream.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            info = _SESSIONS.get(sid)
            if info is None:
                break
            _append(info, chunk.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _monitor(sid: str, proc) -> None:
    try:
        proc.wait()
    except Exception:
        pass
    info = _SESSIONS.get(sid)
    if info is not None:
        info["exit_code"] = proc.returncode


def drain(sid: str) -> dict:
    """拉取并清空缓冲区新内容，附带运行状态。"""
    info = _SESSIONS.get(sid)
    if not info:
        return {"error": "会话不存在或已结束"}
    with info["lock"]:
        parts = list(info["buffer"])
        info["buffer"].clear()
        info["chars"] = 0
    running = _alive(info["proc"])
    code = info["exit_code"]
    if not running and code is None:
        try:
            code = info["proc"].returncode
        except Exception:
            code = None
    return {"ok": True, "session_id": sid, "text": "".join(parts),
            "running": running, "exit_code": code}


def write(sid: str, text: str) -> dict:
    """向子进程 stdin 写入文本（模拟键盘输入）。"""
    info = _SESSIONS.get(sid)
    if not info:
        return {"error": "会话不存在或已结束"}
    if not _alive(info["proc"]):
        return {"error": "进程已退出，无法输入"}
    try:
        info["proc"].stdin.write((text or "").encode("utf-8"))
        info["proc"].stdin.flush()
    except Exception as e:
        return {"error": f"写入失败：{e}"}
    return {"ok": True}


def stop(sid: str, force: bool = False) -> dict:
    """停止子进程（Windows 用 taskkill /T /F 杀进程树）。"""
    info = _SESSIONS.get(sid)
    if not info:
        return {"error": "会话不存在或已结束"}
    proc = info["proc"]
    if not _alive(proc):
        return {"ok": True, "already_exited": True, "exit_code": info["exit_code"]}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
            except Exception:
                proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        return {"error": f"停止失败：{e}"}
    return {"ok": True, "stopped": True, "exit_code": info["exit_code"]}


def status(sid: str) -> dict:
    info = _SESSIONS.get(sid)
    if not info:
        return {"error": "会话不存在或已结束"}
    return {"ok": True, "session_id": sid, "running": _alive(info["proc"]),
            "exit_code": info["exit_code"], "pid": info["proc"].pid}


def close(sid: str) -> dict:
    """关闭并清理会话（先停进程，再移除记录与缓冲区）。"""
    info = _SESSIONS.get(sid)
    if not info:
        return {"ok": True}
    try:
        if _alive(info["proc"]):
            stop(sid, force=True)
    finally:
        with _LOCK:
            _SESSIONS.pop(sid, None)
    return {"ok": True}
