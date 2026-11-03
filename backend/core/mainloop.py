"""主事件循环引用：供 worker 线程中的命令把协程调度回主循环执行。

背景：uvicorn 的所有异步运行（run_registry.start / stop、agent_reply 等）
都跑在 FastAPI 主事件循环上，而 cluster.* 命令经 _execute_tool 在
asyncio.to_thread 的 worker 线程里执行。涉及「启动/停止智能体运行」的命令
不能在 worker 线程里直接操作 asyncio.Task，必须调度回主循环。

用法：main.py lifespan 启动时 `mainloop.set(asyncio.get_running_loop())`，
命令里 `mainloop.run_coro(some_coro())` 同步等待结果。
"""
import asyncio

_loop: asyncio.AbstractEventLoop | None = None


def set(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def get() -> asyncio.AbstractEventLoop:
    if _loop is None:
        raise RuntimeError("主事件循环尚未注册（后端未完成启动），无法执行运行管理命令")
    if _loop.is_closed():
        raise RuntimeError("主事件循环已关闭，无法执行运行管理命令")
    return _loop


def run_coro(coro, timeout: float = 60.0):
    """把协程调度到主事件循环执行，并阻塞当前 worker 线程等待结果。"""
    fut = asyncio.run_coroutine_threadsafe(coro, get())
    try:
        return fut.result(timeout=timeout)
    except asyncio.TimeoutError:
        fut.cancel()
        raise RuntimeError(f"运行管理命令在主循环执行超时（>{timeout}s）")
