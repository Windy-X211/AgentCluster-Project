"""智能搜索子智能体 - 由命令 agent.smart_search 派生。

工作流：
    主智能体在收到（复杂项目改动的）任务后，调用 agent.smart_search(task=<搜索任务提示词>)；
    本模块随即启动一个专职「搜索子智能体」的 LLM 循环：
      先搜索相关文件 → 读取内容 → 判断信息是否充足
        · 充足 → 输出简洁概括返回；
        · 不足 → 继续搜索（换关键词 / 读取更多候选文件）；
        · 工作区确无相关 → 输出「未找到」反馈。

设计要点：
    - 只向子智能体暴露"搜索 / 读取"类命令（file.search / file.find / workspace.search /
      file.read / file.structure），不暴露任何写操作，保证智能搜索无副作用。
    - 子智能体用非流式 LLM 调用（无需实时流式展示），多轮 until 模型给出最终结论。
    - 独立于主 agent_reply 循环：由 _execute_tool 在 worker 线程中同步调用，
      内部用 asyncio.run 起一个独立事件循环执行 LLM 往返。
    - 模型/接口来自 SMART_SEARCH_CTX（agent_runtime 在每次 agent_reply 时注入当前
      智能体选定的接口与模型）；无上下文时回退到默认模型接口。
"""
import asyncio
import json
import logging
from contextvars import ContextVar
from typing import Any

from core.llm import chat_with_failover as _chat, _encode_tool_name

_logger = logging.getLogger("smart_search")

# agent_runtime 在 agent_reply 中注入当前智能体的 {interface, model}；
# 通过 asyncio.to_thread 调用本命令时，线程上下文会携带该值。
SMART_SEARCH_CTX: ContextVar = ContextVar("smart_search_ctx", default=None)

# 搜索子智能体最多进行的 LLM 轮次（每轮 = 一次模型调用 + 其发起的工具执行）
MAX_ROUNDS = 8
# 单条工具结果回灌上限（字符）
TOOL_RESULT_MAX = 8000
# 累积工具结果上限（字符）：超过后提示模型尽快收尾，避免上下文爆炸
TOTAL_RESULT_MAX = 32000

# 允许子智能体使用的命令（只读搜索/读取类）
SEARCH_COMMANDS = ["file.search", "file.find", "workspace.search", "file.read", "file.structure"]

_SYSTEM_PROMPT = (
    "你是「智能搜索」子智能体，专职在项目工作区内定位与任务相关的文件并提炼信息。\n\n"
    "工作流程（严格遵守）：\n"
    "1. 先用 file.search / file.find / workspace.search 等搜索工具，围绕任务提示词中的关键词、"
    "文件名、函数名、概念定位「相关文件」；\n"
    "2. 对命中的候选文件用 file.read 读取内容，确认其与任务的相关性及关键细节；\n"
    "3. 每轮结束后判断：已收集的信息是否**足以**回答或支撑该搜索任务？\n"
    "   · 充足 → 停止搜索，直接输出**简洁概括**（见下方输出规范）；\n"
    "   · 不足 → 调整或更换关键词继续搜索，或读取更多候选文件；\n"
    "   · 确认工作区内**不存在**相关内容 → 明确输出「未找到」及已尝试的搜索范围。\n\n"
    "输出规范（最终答复，此时不要再调用任何工具）：\n"
    "· 若找到相关内容：先用一句话给出结论，然后分条列出**相关文件路径**及其**关键内容 / 作用**"
    "（简洁概括，不要粘贴大段原文），总长控制在 500 字以内。\n"
    "· 若未找到：以「【未找到】」开头用一句话说明尝试过的关键词与目录范围。\n"
    "· 严禁编造文件或内容，只依据工具返回的真实结果作答。"
)

# —— 命令 schema：注册到 COMMAND_TOOLS，供 LLM function calling ——
SMART_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "agent.smart_search",
        "description": (
            "智能搜索：派生一个专职「搜索子智能体」，围绕给定的搜索任务提示词在项目工作区内"
            "自动完成「搜索相关文件 → 读取内容 → 判断信息是否充足（不足则继续搜索）→ 简洁总结」"
            "的多轮检索，用于复杂项目改动前的上下文调研。返回相关文件的路径与关键内容概括；"
            "若工作区内无相关内容则返回「未找到」反馈。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "搜索任务提示词：说明本次要搜索 / 调研什么、为什么。"
                        "例如：「我要把画布缩放的 transform 改成 zoom，请找出现有缩放实现涉及的所有文件与关键函数」。"
                    ),
                }
            },
            "required": ["task"],
        },
    },
}


def _make_name_map(tools: list) -> dict:
    """构建 编码名 → 原名 映射。

    非流式返回的 tool_call.function.name 是上游编码名（点号被替换为下划线），
    需要还原成本项目内部原名（file.read 等）才能正确分发。
    """
    return {
        _encode_tool_name(t["function"]["name"]): t["function"]["name"]
        for t in tools
        if isinstance(t, dict) and (t.get("function") or {}).get("name")
    }


def _result_to_text(result: Any) -> str:
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        s = str(result)
    if len(s) > TOOL_RESULT_MAX:
        s = s[:TOOL_RESULT_MAX] + f"...[结果截断，原始 {len(s)} 字符]"
    return s


async def _run_async(task: str, interface: dict, model: str, max_rounds: int = MAX_ROUNDS) -> dict:
    """搜索子智能体的多轮循环（async）。

    返回 {"ok": True, "summary": <概括文本>} 或 {"ok": False, "error": <错误信息>}。
    """
    # 延迟导入，避免与 commands 模块的循环依赖（commands 顶层 import 本模块的 schema 与入口）
    from core.commands import filter_tools, run_command

    tools = filter_tools(SEARCH_COMMANDS, [], {}, skills=[])
    name_map = _make_name_map(tools)

    messages: list = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"搜索任务：\n{task}"},
    ]

    accumulated = 0
    for round_no in range(max_rounds):
        assistant = None
        err = None
        try:
            async for kind, payload in _chat(
                interface, model, messages, stream=False, tools=tools, tool_choice="auto"
            ):
                if kind == "message":
                    assistant = payload
                elif kind == "error":
                    err = payload
        except Exception as e:  # 兜底：模型/传输层异常
            _logger.warning("智能搜索 LLM 调用异常：%s: %s", type(e).__name__, e)
            return {"ok": False, "error": f"搜索子智能体调用失败：{type(e).__name__}: {e}"}

        if err:
            detail = str(err.get("detail", ""))[:200]
            _logger.warning("智能搜索上游错误：HTTP %s %s", err.get("status"), detail)
            return {"ok": False, "error": f"搜索子智能体调用失败：HTTP {err.get('status')} {detail}"}
        if not assistant:
            return {"ok": False, "error": "搜索子智能体未返回有效响应"}

        tool_calls = assistant.get("tool_calls") or []
        # 回灌精简版 assistant 消息（去掉 reasoning_content 等上游私有字段）
        assistant_msg: dict = {"role": "assistant", "content": assistant.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # 模型不再调用工具 → 视为给出最终结论
            summary = (assistant.get("content") or "").strip()
            return {"ok": True, "summary": summary or "【未找到】搜索未产生可总结的内容"}

        # 执行本轮所有工具调用
        for tc in tool_calls:
            fn = tc.get("function") or {}
            enc_name = fn.get("name") or ""
            name = name_map.get(enc_name, enc_name)
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {}
            if name in SEARCH_COMMANDS:
                try:
                    result = run_command(name, **(args or {}))
                except Exception as e:
                    result = {"error": f"工具 {name} 执行失败：{e}"}
            else:
                result = {"error": f"不允许的工具：{name}（智能搜索仅允许搜索 / 读取类命令）"}
            rtext = _result_to_text(result)
            accumulated += len(rtext)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": rtext})

        # 信息量已很大 → 提示模型尽快收尾，避免无谓地继续膨胀上下文
        if accumulated > TOTAL_RESULT_MAX and round_no >= 1:
            messages.append({
                "role": "user",
                "content": "已收集到足够信息，请立即基于以上工具结果给出简洁总结，不要再调用工具。",
            })

    return {"ok": True, "summary": "【未完成】搜索达到最大轮次仍未收敛，请缩小搜索任务范围后重试。"}


def run_smart_search(task: str) -> dict:
    """智能搜索命令的同步入口（在 _execute_tool 的 worker 线程中调用）。

    模型/接口优先取 SMART_SEARCH_CTX（当前智能体的选定值），否则回退默认模型接口。
    """
    from core.store import load

    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "缺少搜索任务提示词 task"}

    ctx = SMART_SEARCH_CTX.get()
    interface = None
    model = None
    if isinstance(ctx, dict):
        interface = ctx.get("interface")
        model = ctx.get("model")
    if not interface:
        interface = (load("model_interfaces") or [{}])[0]
    if not interface:
        return {"ok": False, "error": "未配置模型接口，无法启动智能搜索"}
    model = model or interface.get("default_model") or "gpt-4o-mini"

    try:
        return asyncio.run(_run_async(task, interface, model))
    except RuntimeError as e:
        # 理论上不会发生（本命令始终在 worker 线程执行，无运行中的事件循环）
        _logger.warning("智能搜索启动事件循环失败：%s", e)
        return {"ok": False, "error": f"搜索子智能体启动失败：{e}"}


def cmd_agent_smart_search(task: str) -> dict:
    """agent.smart_search 命令实现（run_command 反射调用）。"""
    return run_smart_search(task)