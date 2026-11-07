"""SenseVoice 语音识别封装 —— 通过子进程调用 funasr(3.12) 环境的常驻 worker。

后端运行在 Python 3.13，funasr-onnx 仅兼容 Python 3.12（numpy<=1.26.4），
因此不直接 import funasr_onnx，而是 spawn funasr conda 环境下的独立 worker 进程，
通过 stdin/stdout 的 JSON 行协议通信。模型在 worker 进程内只加载一次。
"""
import os
import sys
import json
import subprocess
import threading

_worker = None
_lock = threading.Lock()
_available_cache = None


def _find_funasr_python():
    """探测 funasr conda 环境的 python.exe 绝对路径。"""
    p = os.environ.get("FUNASR_PYTHON")
    if p and os.path.isfile(p):
        return p
    candidates = []
    conda_prefix = os.environ.get("CONDA_PREFIX", "")
    if conda_prefix:
        candidates.append(os.path.join(os.path.dirname(conda_prefix), "envs", "funasr", "python.exe"))
    for root in (r"F:\anaconda3", r"C:\anaconda3",
                 os.path.expanduser("~/anaconda3"), os.path.expanduser("~/miniconda3")):
        candidates.append(os.path.join(root, "envs", "funasr", "python.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def is_available() -> bool:
    global _available_cache
    if _available_cache is not None:
        return _available_cache
    py = _find_funasr_python()
    if not py:
        _available_cache = False
        return False
    try:
        r = subprocess.run([py, "-c", "import funasr_onnx"],
                           capture_output=True, timeout=20,
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        _available_cache = (r.returncode == 0)
    except Exception:
        _available_cache = False
    return _available_cache


def _start_worker():
    py = _find_funasr_python()
    if not py:
        raise RuntimeError(
            "未找到 funasr 环境，请设置环境变量 FUNASR_PYTHON 指向 funasr 环境的 python.exe"
        )
    worker_script = os.path.join(os.path.dirname(__file__), "asr_worker.py")
    proc = subprocess.Popen(
        [py, "-u", worker_script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", bufsize=1,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    # 异步消费 stderr，避免管道缓冲区满导致死锁
    def _drain_stderr():
        for line in proc.stderr:
            sys.stderr.write(f"[asr-worker] {line}")
    threading.Thread(target=_drain_stderr, daemon=True).start()
    # 等待 ready 信号
    ready_line = proc.stdout.readline()
    if not ready_line:
        raise RuntimeError("ASR worker 启动失败：无输出")
    try:
        if json.loads(ready_line).get("ready") is not True:
            raise RuntimeError(f"ASR worker 启动失败：{ready_line.strip()}")
    except json.JSONDecodeError:
        raise RuntimeError(f"ASR worker 启动失败：{ready_line.strip()}")
    return proc


def _get_worker():
    global _worker
    if _worker is not None and _worker.poll() is None:
        return _worker
    _worker = _start_worker()
    return _worker


def _readline_with_timeout(proc, timeout_s: float):
    """带超时读 worker 一行响应：worker 卡死时旧实现 readline 永久阻塞，
    会一直占住调用线程（耗尽线程池）且界面无任何反馈。"""
    result: list = [None]

    def _rd():
        try:
            result[0] = proc.stdout.readline()
        except Exception:
            result[0] = None

    t = threading.Thread(target=_rd, daemon=True)
    t.start()
    t.join(timeout_s)
    return result[0]


def _kill_worker(proc):
    """停掉旧 worker（超时/无响应重启前）：避免旧进程与挂在其 stdout 上的读取线程残留。"""
    try:
        proc.terminate()
    except Exception:
        pass


def transcribe(audio_path: str, language: str = "auto") -> str:
    """识别音频文件，返回纯文本。language: auto/zh/en/yue/ja/ko"""
    global _worker
    req = json.dumps({"audio": audio_path, "language": language}, ensure_ascii=False) + "\n"
    with _lock:
        w = _get_worker()
        w.stdin.write(req)
        w.stdin.flush()
        resp_line = _readline_with_timeout(w, 120)
        if not resp_line or w.poll() is not None:
            # worker 意外退出/卡死（读超时）→ 杀掉重启后重试一次
            _kill_worker(w)
            _worker = _start_worker()
            w = _worker
            w.stdin.write(req)
            w.stdin.flush()
            resp_line = _readline_with_timeout(w, 120)
    if not resp_line:
        raise RuntimeError("ASR worker 无响应")
    resp = json.loads(resp_line)
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp.get("text", "")