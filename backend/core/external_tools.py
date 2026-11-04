"""外部工具提供者（External Tool Providers）

用途：让「功能拓展」（尤其是 MCP 客户端拓展）把本程序之外的工具（外部 MCP 服务器
提供的工具等）注入到智能体的 function calling 列表，并负责执行分发。

设计要点：
- 与 AppData/plugins 下的原生插件互不影响：插件仍走 PLUGIN_REGISTRY；
  外部工具只通过本注册表接入。
- 拓展 backend.py 在挂载时调用 :func:`register_provider` 注册；拓展停用/卸载后
  核心只在模块重载时才会撤销，但 provider 内部应自行检查 `is_extension_enabled`。
- 工具名约定由 provider 自行保证全局唯一（MCP 客户端使用 ``mcp__<server>__<tool>``）。
- 所有函数都在同步上下文（worker 线程 / 请求线程）调用，provider 内部需自行完成
  异步桥接（例如 MCP 客户端使用专用事件循环线程）。

对外 API：
    register_provider(provider_id, tools_fn, call_fn, prefix="", label="")
    unregister_provider(provider_id)
    collect_tools(agent=None) -> list[dict]        # OpenAI tool schema 列表
    owner_of(tool_name) -> str | None              # 工具归属的 provider_id
    dispatch(tool_name, arguments, agent=None) -> dict
"""
from __future__ import annotations

import threading
from typing import Any, Callable

__all__ = [
    "register_provider",
    "unregister_provider",
    "collect_tools",
    "owner_of",
    "dispatch",
    "list_providers",
]

_LOCK = threading.RLock()
# provider_id -> {tools_fn, call_fn, prefix, label}
_PROVIDERS: dict[str, dict[str, Any]] = {}


def register_provider(
    provider_id: str,
    tools_fn: Callable[[dict | None], list[dict]],
    call_fn: Callable[[str, dict, dict | None], dict],
    prefix: str = "",
    label: str = "",
) -> None:
    """注册一个外部工具提供者。

    Args:
        provider_id: 唯一标识（如 ``mcp_client``）。
        tools_fn: ``(agent: dict | None) -> list[OpenAI tool schema]``。
                  返回该智能体可见的工具列表；异常应由实现方内部消化。
        call_fn:  ``(tool_name, arguments, agent) -> dict``，执行工具。
        prefix:   工具名前缀（用于快速判断归属，如 ``mcp__``）。
        label:    展示名（日志用）。
    """
    if not provider_id:
        raise ValueError("provider_id 不能为空")
    with _LOCK:
        _PROVIDERS[provider_id] = {
            "tools_fn": tools_fn,
            "call_fn": call_fn,
            "prefix": prefix or "",
            "label": label or provider_id,
        }


def unregister_provider(provider_id: str) -> None:
    with _LOCK:
        _PROVIDERS.pop(provider_id, None)


def list_providers() -> list[dict]:
    with _LOCK:
        return [
            {"id": pid, "prefix": p.get("prefix", ""), "label": p.get("label", pid)}
            for pid, p in _PROVIDERS.items()
        ]


def collect_tools(agent: dict | None = None) -> list[dict]:
    """收集所有 provider 为该智能体提供的工具 schema（按工具名去重）。"""
    with _LOCK:
        providers = list(_PROVIDERS.items())
    out: list[dict] = []
    seen: set[str] = set()
    for pid, p in providers:
        try:
            tools = p["tools_fn"](agent) or []
        except Exception as e:  # 单个 provider 故障不影响其他能力注入
            print(f"[external_tools] provider {pid} 生成工具失败：{e}")
            continue
        for tool in tools:
            try:
                name = (tool.get("function") or {}).get("name") or ""
            except Exception:
                name = ""
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(tool)
    return out


def owner_of(tool_name: str) -> str | None:
    """返回工具名归属的 provider_id；无归属返回 None。"""
    if not tool_name:
        return None
    with _LOCK:
        providers = list(_PROVIDERS.items())
    for pid, p in providers:
        prefix = p.get("prefix") or ""
        if prefix and tool_name.startswith(prefix):
            return pid
    return None


def dispatch(tool_name: str, arguments: dict | None, agent: dict | None = None) -> dict:
    """执行外部工具。未找到归属 provider 时返回错误字典。"""
    pid = owner_of(tool_name)
    if not pid:
        return {"error": f"unknown external tool: {tool_name}"}
    with _LOCK:
        p = _PROVIDERS.get(pid)
    if not p:
        return {"error": f"external tool provider 已卸载: {pid}"}
    try:
        result = p["call_fn"](tool_name, arguments or {}, agent)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": f"external tool {tool_name} 执行异常: {type(e).__name__}: {e}"}