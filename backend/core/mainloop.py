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


# ==============================================================
# 拓展 / 后台线程通用：LLM 调用便捷函数
# ==============================================================
# 用途：让拓展 backend.py、worker 线程里的命令、后台定时任务
# 不用了解 LLM 内部实现（chat_with_failover / interface 解析 / event loop 桥接），
# 一行 call_llm(...) 就能拿到模型输出。


def get_default_interface(interface_id=None) -> tuple:
    """获取默认模型接口配置。

    Returns: (interface_dict, model_name)
      interface_dict: { id, name, base_url, api_key, default_model, models:[...] }
      model_name: 具体模型字符串（如 "gpt-4o-mini"）

    策略：
      - 指定 interface_id → 取对应条目；不存在回退到第一个可用的
      - 未指定 → 取 model_interfaces 列表里第一个有 default_model 的条目
      - 完全没有任何接口 → 返回 ({}, "gpt-4o-mini") 兜底
    """
    from core.store import load, get_by
    interface = {}
    if interface_id is not None:
        interface = get_by("model_interfaces", interface_id) or {}
    if not interface:
        ifs = load("model_interfaces") or []
        for it in ifs:
            if it.get("default_model"):
                interface = it
                break
        if not interface and ifs:
            interface = ifs[0]
    model = (interface or {}).get("default_model") or "gpt-4o-mini"
    return interface, model


async def _llm_async_core(user_prompt: str, sys_prompt: str | None = None,
                          model: str | None = None, interface_id=None,
                          extra_messages: list | None = None) -> str | None:
    """核心异步实现（内部用，供 call_llm / call_llm_async 共用）。

    返回纯文本内容；失败返回 None。
    """
    from core.llm import chat_with_failover

    interface, resolved_model = get_default_interface(interface_id)
    if model:
        resolved_model = model

    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": user_prompt})

    try:
        last_text = None
        async for kind, data in chat_with_failover(
            interface, resolved_model, messages,
            stream=False, tool_choice="none",
        ):
            if kind == "message":
                c = (data or {}).get("content")
                if isinstance(c, str) and c.strip():
                    last_text = c.strip()
            elif kind == "error":
                print(f"[mainloop.llm] chat_with_failover error: {data}", flush=True)
                return None
        return last_text
    except Exception as e:
        print(f"[mainloop.llm] call exception: {type(e).__name__}: {e}", flush=True)
        return None


def call_llm(user_prompt: str, sys_prompt: str | None = None,
             model: str | None = None, interface_id=None,
             extra_messages: list | None = None,
             timeout: float = 120.0) -> str | None:
    """同步 LLM 调用 —— 适用于 external_tools call_fn、worker 线程、
    同步 FastAPI 路由等任何上下文。

    内部通过 mainloop.run_coro 把协程调度回 FastAPI 主事件循环执行（避免 worker
    线程没有 running_loop 导致 asyncio 调用失败）。

    Args:
        user_prompt: 用户提示文本
        sys_prompt: 可选的 system prompt
        model: 可选的模型名覆盖；None = 用接口的 default_model
        interface_id: 可选的接口 id；None = 取默认接口
        extra_messages: 可选的额外 messages（在 user_prompt 之前插入）
        timeout: 秒（默认 120s，LLM 整理摘要等场景可能较慢）

    Returns: 模型输出纯文本，失败返回 None
    """
    coro = _llm_async_core(user_prompt, sys_prompt, model, interface_id, extra_messages)
    try:
        return run_coro(coro, timeout=timeout)
    except RuntimeError as e:
        print(f"[mainloop.llm] run_coro failed: {e}", flush=True)
        return None


async def call_llm_async(user_prompt: str, sys_prompt: str | None = None,
                         model: str | None = None, interface_id=None,
                         extra_messages: list | None = None) -> str | None:
    """异步 LLM 调用 —— 适用于 FastAPI async 路由、on_mount async 钩子、
    后台 asyncio.Task 等 async 上下文。

    直接 await，无需事件循环桥接。
    """
    return await _llm_async_core(user_prompt, sys_prompt, model, interface_id, extra_messages)


def create_task(coro, name: str | None = None):
    """在 FastAPI 主事件循环里创建一个后台 asyncio.Task。

    任何上下文都能调用（worker 线程 / async 路由 / 同步钩子）。
    返回 Task 对象（已提交执行，无需 await）。

    内部用 asyncio.run_coroutine_threadsafe 跨线程调度，
    所以 worker 线程里调用也安全。
    """
    loop = get()
    return asyncio.run_coroutine_threadsafe(coro, loop)
