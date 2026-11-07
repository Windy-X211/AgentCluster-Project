"""后端黑匣子诊断（旁路观测，不改任何业务逻辑）

解决的问题：现在后端“崩了/卡了/请求全超时”之后没有任何现场可查——
  · 原生崩溃（pythoncom313.dll 0xc0000005 那类）进程直接消失，stdout 里连一行都不留；
  · 事件循环被同步任务占死时，/api/health 超时，托盘报「后端 FAIL」，但进程其实活着，
    恢复后现场全丢；
  · 智能体运行抛异常 → run_registry 只 push 一句「E:后端崩溃：xxx」，
    print 出来的 traceback 进的是 GBK 管道，中文全是乱码，事后无法定位。

本模块只做四件事，全部旁路、全部失败静默：
  1. faulthandler：原生崩溃（段错误/栈溢出/0xc0000005）时把当时的线程栈写进
     diag/faulthandler.log —— 这是进程死掉后唯一能留下的东西。
  2. 事件循环心跳 + 看门狗：卡死超过 STALL_THRESHOLD 秒，自动把「所有线程栈 +
     卡死前最后一次的 asyncio 任务栈 + 最近的慢请求 + 活跃运行数」写进
     diag/stall-*.log，恢复时再补一行持续时长。
  3. 慢请求：>= SLOW_REQUEST_SECONDS 的 HTTP 请求记到 diag/slow.log（走后台写盘线程，
     不占事件循环）。
  4. 运行异常：diag/run-errors.log 里留完整 traceback（含会话/智能体 id）。

开关：AGENTCLUSTER_DIAG=off 可整体关闭；写盘目录固定 backend/diag/（在项目里，好找）。
"""
from __future__ import annotations

import os
import sys
import time
import queue
import asyncio
import threading
import traceback
import faulthandler
from collections import deque
from datetime import datetime
from pathlib import Path

__all__ = [
    "DIAG_DIR",
    "install",
    "attach_loop",
    "start_heartbeat",
    "log_run_exception",
    "force_dump",
    "summary",
    "read_file_tail",
    "SlowRequestMiddleware",
]

# ---------------------------------------------------------------- 配置
_ENABLED = os.getenv("AGENTCLUSTER_DIAG", "on").strip().lower() not in ("off", "0", "false", "no")
DIAG_DIR = Path(__file__).resolve().parent.parent / "diag"
STALL_THRESHOLD = float(os.getenv("AGENTCLUSTER_STALL_SECONDS", "5"))
STALL_RE_DUMP_EVERY = 30.0     # 同一次卡死，最多每 30s 再补一份现场
SLOW_REQUEST_SECONDS = float(os.getenv("AGENTCLUSTER_SLOW_SECONDS", "3"))
KEEP_FILES = 40                # 每类文件最多保留份数
MAX_LOG_BYTES = 8 * 1024 * 1024  # 单文件超过 8MB 旋转为 .1

_LOCK = threading.RLock()
_WRITE_Q: "queue.Queue[tuple[Path, str]]" = queue.Queue(maxsize=2000)
_writer_started = False
_fh_faulthandler = None

_last_beat = time.monotonic()
_loop = None
_loop_ready = False
_stall_started_at: float | None = None
_stall_dumped_at = 0.0
_last_task_snapshot: list[str] = []
_slow_ring: deque = deque(maxlen=30)
_run_errors_ring: deque = deque(maxlen=20)
_started_at = time.time()


# ---------------------------------------------------------------- 写盘（后台线程）
def _ensure_dir() -> None:
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            bak = path.with_suffix(path.suffix + ".1")
            try:
                path.replace(bak)
            except Exception:
                pass
    except Exception:
        pass


def _writer_loop() -> None:
    while True:
        try:
            path, text = _WRITE_Q.get()
        except Exception:
            return
        if path is None:
            return
        try:
            _ensure_dir()
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception:
            pass


def _start_writer() -> None:
    global _writer_started
    with _LOCK:
        if _writer_started:
            return
        _writer_started = True
        t = threading.Thread(target=_writer_loop, name="diag-writer", daemon=True)
        t.start()


def _enqueue_write(path: Path, text: str) -> None:
    try:
        _WRITE_Q.put_nowait((path, text))
    except Exception:
        pass


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- 进程指标
def _rss_mb() -> float:
    """当前进程常驻内存（MB）。失败返回 -1，不抛。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        # argtypes 必须显式设置：不设置时 64 位句柄会被截断（本项目 store.py 踩过同款坑）
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        h = k32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return -1.0
        return round(pmc.WorkingSetSize / 1024 / 1024, 1)
    except Exception:
        return -1.0


def _live_run_count() -> int:
    try:
        from core import run_registry
        return len(getattr(run_registry, "_RUNS", {}) or {})
    except Exception:
        return -1


# ---------------------------------------------------------------- 栈采集
def _thread_stacks() -> str:
    """所有线程的栈（含主线程 = 事件循环所在线程，卡死时它最有价值）。"""
    out: list[str] = []
    try:
        frames = sys._current_frames()
        names = {t.ident: t.name for t in threading.enumerate()}
        main_id = threading.main_thread().ident
        items = sorted(frames.items(), key=lambda kv: (kv[0] != main_id, str(names.get(kv[0], ""))))
        for tid, frame in items:
            tname = names.get(tid, f"<unknown:{tid}>")
            tag = "  <== 主线程（事件循环）" if tid == main_id else ""
            out.append(f"\n--- 线程 {tname} (ident={tid}){tag}")
            try:
                out.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
            except Exception as e:
                out.append(f"    <栈读取失败: {e}>")
    except Exception as e:
        out.append(f"<线程栈采集失败: {e}>")
    return "\n".join(out)


def _snapshot_tasks_sync() -> list[str]:
    """在事件循环里调用：把当前 asyncio 任务的栈拍个快照（只留每个任务前几层）。"""
    out: list[str] = []
    try:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        out.append(f"活动任务数: {len(tasks)}")
        for t in tasks[:60]:
            try:
                name = t.get_name()
            except Exception:
                name = "?"
            out.append(f"\n  · 任务 {name} done={t.done()} cancelled={t.cancelled()}")
            try:
                for fr in (t.get_stack(limit=6) or []):
                    line = traceback.extract_stack(fr, limit=1)
                    if line:
                        f0 = line[-1]
                        out.append(f"      {_base(f0.filename)}:{f0.lineno} in {f0.name}")
            except Exception:
                pass
    except Exception as e:
        out.append(f"<任务快照失败: {e}>")
    return out


def _base(p: str) -> str:
    try:
        return os.path.basename(p)
    except Exception:
        return p


def _recent_slow() -> str:
    if not _slow_ring:
        return "（无）"
    return "\n".join(_slow_ring)


# ---------------------------------------------------------------- 现场落盘
def _write_stall_dump(stall_seconds: float, reason: str) -> Path | None:
    """写一份卡死现场，返回文件路径。"""
    try:
        _ensure_dir()
        name = f"stall-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        path = DIAG_DIR / name
        header = [
            "=" * 78,
            f"=== 后端卡死现场 ===  {_stamp()}",
            f"原因: {reason}",
            f"事件循环已 {stall_seconds:.1f}s 没有心跳（阈值 {STALL_THRESHOLD:.0f}s）",
            f"进程: pid={os.getpid()}  线程数={threading.active_count()}  "
            f"RSS={_rss_mb()}MB  进程CPU={time.process_time():.1f}s  已运行 {(time.time() - _started_at) / 60:.1f} 分钟",
            f"活跃运行(run_registry): {_live_run_count()}",
            "",
            "--- 卡死前最后一次心跳时的 asyncio 任务 ---",
            *(_last_task_snapshot or ["（未采集到）"]),
            "",
            "--- 最近的慢请求（>= %.1fs） ---" % SLOW_REQUEST_SECONDS,
            _recent_slow(),
            "",
            "--- 所有线程栈（主线程即事件循环，通常就是罪魁） ---",
            _thread_stacks(),
            "",
            "=" * 78,
            "",
        ]
        _enqueue_write(path, "\n".join(header) + "\n")
        _prune()
        return path
    except Exception:
        return None


def _prune() -> None:
    for pattern in ("stall-*.log", "manual-*.log"):
        try:
            files = [p for p in DIAG_DIR.glob(pattern) if p.is_file()]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for old in files[KEEP_FILES:]:
            try:
                old.unlink()
            except Exception:
                pass


def force_dump(reason: str = "manual") -> dict:
    """立刻抓一份现场（调试用，也可以从 /api/debug/diag/dump 触发）。"""
    if not _ENABLED:
        return {"ok": False, "error": "诊断已关闭（AGENTCLUSTER_DIAG=off）"}
    try:
        _ensure_dir()
        name = f"manual-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        path = DIAG_DIR / name
        stall = time.monotonic() - _last_beat if _loop_ready else -1.0
        body = [
            "=" * 78,
            f"=== 手动现场快照 ===  {_stamp()}",
            f"原因: {reason}",
            f"事件循环最近心跳: {stall:.3f}s 前" if stall >= 0 else "事件循环: 尚未开始心跳",
            f"进程: pid={os.getpid()}  线程数={threading.active_count()}  RSS={_rss_mb()}MB  "
            f"活跃运行={_live_run_count()}",
            "",
            "--- 最后一次心跳时的 asyncio 任务 ---",
            *(_last_task_snapshot or ["（未采集到）"]),
            "",
            "--- 最近的慢请求 ---",
            _recent_slow(),
            "",
            "--- 所有线程栈 ---",
            _thread_stacks(),
            "",
            "=" * 78,
            "",
        ]
        _enqueue_write(path, "\n".join(body) + "\n")
        _prune()
        return {"ok": True, "file": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 心跳 & 看门狗
def attach_loop(loop) -> None:
    """事件循环就绪：开始计时（此前看门狗不判卡死）。"""
    global _loop, _loop_ready, _last_beat
    _loop = loop
    _loop_ready = True
    _last_beat = time.monotonic()


async def _heartbeat() -> None:
    """0.5s 一次心跳；每 4 次（约 2s）顺手给 asyncio 任务拍个快照，卡死时用。"""
    global _last_beat, _last_task_snapshot
    i = 0
    while True:
        try:
            _last_beat = time.monotonic()
            i += 1
            # 首次心跳就拍一版任务快照，之后约每 2s 一版（卡死时用来看"谁在跑"）
            if i % 4 == 1:
                snap = _snapshot_tasks_sync()
                if snap:
                    _last_task_snapshot = snap
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            time.sleep(0.5)


def start_heartbeat() -> None:
    """在事件循环里创建心跳任务（lifespan 启动时调用）。"""
    if not _ENABLED:
        return
    try:
        asyncio.get_running_loop().create_task(_heartbeat())
    except Exception as e:
        print(f"[diag] WARN 心跳任务创建失败：{e}", flush=True)


def _watchdog_loop() -> None:
    global _stall_started_at, _stall_dumped_at
    while True:
        time.sleep(0.5)
        try:
            if not _loop_ready:
                continue
            gap = time.monotonic() - _last_beat
            if gap >= STALL_THRESHOLD:
                if _stall_started_at is None:
                    _stall_started_at = time.monotonic() - gap
                if time.monotonic() - _stall_dumped_at >= STALL_RE_DUMP_EVERY:
                    _stall_dumped_at = time.monotonic()
                    p = _write_stall_dump(gap, "事件循环无心跳（同步任务占死事件循环，或死锁）")
                    try:
                        print(f"[diag] ⚠ 事件循环卡死 {gap:.1f}s，现场已写入 {p}", flush=True)
                    except Exception:
                        pass
            else:
                if _stall_started_at is not None:
                    lasted = time.monotonic() - _stall_started_at
                    _stall_started_at = None
                    _stall_dumped_at = 0.0
                    if lasted >= STALL_THRESHOLD:
                        _enqueue_write(
                            DIAG_DIR / "recoveries.log",
                            f"{_stamp()}  事件循环恢复：累计卡死 {lasted:.1f}s  线程数={threading.active_count()}\n",
                        )
        except Exception:
            pass


# ---------------------------------------------------------------- 慢请求
class SlowRequestMiddleware:
    """纯 ASGI 旁路计时：>= SLOW_REQUEST_SECONDS 的请求记一行到 diag/slow.log。

    写盘走后台线程；流式接口（对话流/事件流）跳过，避免把正常的长连接算成慢请求。
    """

    _SKIP_HINTS = ("/send", "/events", "/stream", "/raw", "/office-pdf", "/asr")

    def __init__(self, app, threshold: float | None = None):
        self.app = app
        self.threshold = SLOW_REQUEST_SECONDS if threshold is None else threshold

    async def __call__(self, scope, receive, send):
        if not _ENABLED or scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "") or ""
        if any(h in path for h in self._SKIP_HINTS):
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = {"code": 0}

        async def _send(msg):
            try:
                if msg.get("type") == "http.response.start":
                    status["code"] = int(msg.get("status") or 0)
            except Exception:
                pass
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            dt = time.perf_counter() - t0
            if dt >= self.threshold:
                try:
                    line = (f"{_stamp()}  {dt:7.2f}s  {status['code']}  "
                            f"{scope.get('method', '?')} {path}  线程={threading.active_count()}  "
                            f"活跃运行={_live_run_count()}\n")
                    _slow_ring.append(line.rstrip("\n"))
                    _enqueue_write(DIAG_DIR / "slow.log", line)
                except Exception:
                    pass


# ---------------------------------------------------------------- 异常落盘
def log_run_exception(conv_id, agent_id, exc: BaseException, extra: str = "") -> None:
    """智能体运行抛异常：完整 traceback 落盘（原来只有一句 E:后端崩溃 发给前端）。"""
    if not _ENABLED:
        return
    try:
        text = [
            "=" * 78,
            f"{_stamp()}  运行异常  conv={conv_id} agent={agent_id}",
            f"{type(exc).__name__}: {exc}",
        ]
        if extra:
            text.append(extra)
        text.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        text.append("=" * 78)
        block = "\n".join(text) + "\n"
        _run_errors_ring.append(block)
        _enqueue_write(DIAG_DIR / "run-errors.log", block)
    except Exception:
        pass


def log_exception(kind: str, exc: BaseException) -> None:
    """其它需要留痕的异常（线程 excepthook 等）。"""
    if not _ENABLED:
        return
    try:
        block = (f"{_stamp()}  [{kind}] {type(exc).__name__}: {exc}\n"
                 + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                 + "-" * 60 + "\n")
        _enqueue_write(DIAG_DIR / "errors.log", block)
    except Exception:
        pass


# ---------------------------------------------------------------- 安装
def install() -> None:
    """进程启动时调用一次（幂等）。"""
    if not _ENABLED:
        try:
            print("[diag] 已关闭（AGENTCLUSTER_DIAG=off）", flush=True)
        except Exception:
            pass
        return
    try:
        _ensure_dir()
    except Exception:
        pass
    _start_writer()

    # 1) 原生崩溃：段错误 / 栈溢出 / 0xc0000005 —— 进程死掉后唯一线索
    global _fh_faulthandler
    try:
        if _fh_faulthandler is None:
            _fh_faulthandler = open(DIAG_DIR / "faulthandler.log", "a",
                                    encoding="utf-8", errors="replace", buffering=1)
        faulthandler.enable(file=_fh_faulthandler, all_threads=True)
    except Exception as e:
        try:
            print(f"[diag] WARN faulthandler 启用失败：{e}", flush=True)
        except Exception:
            pass

    # 2) 未捕获异常：主线程 + 子线程
    try:
        _prev_hook = sys.excepthook

        def _sys_hook(exc_type, exc_value, exc_tb):
            try:
                log_exception("sys.excepthook", exc_value or exc_type())
            except Exception:
                pass
            try:
                _prev_hook(exc_type, exc_value, exc_tb)
            except Exception:
                pass

        sys.excepthook = _sys_hook
    except Exception:
        pass
    try:
        _prev_thread_hook = threading.excepthook

        def _thread_hook(args):
            try:
                if args.exc_value is not None:
                    log_exception(f"thread:{getattr(args.thread, 'name', '?')}", args.exc_value)
            except Exception:
                pass
            try:
                _prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook
    except Exception:
        pass

    # 3) 卡死看门狗
    try:
        threading.Thread(target=_watchdog_loop, name="diag-watchdog", daemon=True).start()
    except Exception:
        pass
    try:
        print(f"[diag] 黑匣子已启用 → {DIAG_DIR}（卡死阈值 {STALL_THRESHOLD:.0f}s，慢请求 {SLOW_REQUEST_SECONDS:.0f}s）",
              flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 查询（给接口用）
def _file_info(p: Path) -> dict:
    try:
        st = p.stat()
        return {"name": p.name, "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    except Exception:
        return {"name": p.name, "size": -1, "mtime": ""}


def summary() -> dict:
    _ensure_dir()
    files = []
    try:
        for p in sorted(DIAG_DIR.glob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if p.is_file():
                files.append(_file_info(p))
    except Exception:
        pass
    gap = (time.monotonic() - _last_beat) if _loop_ready else -1
    return {
        "enabled": _ENABLED,
        "dir": str(DIAG_DIR),
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - _started_at, 1),
        "event_loop_lag_seconds": round(gap, 3),
        "loop_ready": _loop_ready,
        "stalled": bool(_loop_ready and gap >= STALL_THRESHOLD),
        "stall_threshold": STALL_THRESHOLD,
        "threads": [{"name": t.name, "ident": t.ident, "daemon": t.daemon, "alive": t.is_alive()}
                    for t in threading.enumerate()],
        "thread_count": threading.active_count(),
        "rss_mb": _rss_mb(),
        "live_runs": _live_run_count(),
        "recent_slow": list(_slow_ring),
        "recent_run_errors": list(_run_errors_ring)[-3:],
        "files": files,
    }


def read_file_tail(name: str, max_lines: int = 400) -> dict:
    """读取 diag 目录下某个文件的尾部（只允许目录内的纯文件名，防穿越）。"""
    try:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return {"ok": False, "error": "非法文件名"}
        p = (DIAG_DIR / name).resolve()
        if p.parent != DIAG_DIR.resolve():
            return {"ok": False, "error": "非法路径"}
        if not p.exists():
            return {"ok": False, "error": f"文件不存在：{name}"}
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        n = max(1, min(int(max_lines or 400), 5000))
        return {"ok": True, "name": name, "total_lines": len(lines),
                "content": "\n".join(lines[-n:])}
    except Exception as e:
        return {"ok": False, "error": str(e)}
