"""派生子智能体 - 由命令 agent.spawn 派生。

工作流：
    主智能体在执行任务过程中，调用 agent.spawn(name=..., role=..., task=...,
    commands=[...]) 现场定义一个**临时子智能体**：
      - name  子智能体名称（如「测试工程师」「文档整理员」）；
      - role  角色设定与工作方式（作为子智能体的 system prompt）；
      - task  要完成的具体任务；
      - commands 允许其使用的命令白名单（能力配置）。
    本模块随即以该配置启动一个独立的多轮 LLM 循环（可调用所配置的命令），
    直到任务完成；最终答复返回给主智能体，子智能体用完即销毁
    （不落库、不出现在智能体列表、不进入集群编排）。

能力范围（安全边界，2026-11-03 用户确认；同日扩展支持插件）：
    - 子智能体可配置的能力 = 内建命令（COMMAND_TOOLS）+ 插件（PLUGIN_REGISTRY，如
      http_request / browser_control）；
    - 默认：可用命令 ⊆ 主智能体已勾选命令、可用插件 ⊆ 主智能体已勾选插件，
      防止借子智能体越权；
    - 例外：主智能体自身拥有 cluster.list_capabilities（可查询全部可用能力）时，
      视为已被授权全能力视野，命令与插件均可从全表自由配置；
    - 任何情况下都禁止 agent.spawn 自身，防止子智能体递归派生导致资源爆炸；
      运行时注入的隐藏命令（cluster.send_message 等）同样不可分配。

设计要点（复用 smart_search 骨架）：
    - 模型/接口来自 SUBAGENT_CTX（agent_runtime 在每次 agent_reply 时注入当前
      智能体选定的接口与模型 + 命令白名单）；无上下文时回退到默认模型接口。
    - 子智能体用非流式 LLM 调用，多轮 until 模型给出最终结论；
      commands 为空时 tools 为空表 → 纯文本对话子智能体（llm.chat 对空 tools
      不注入 tools 字段，天然支持）。
    - 工作区上下文（WORKDIR_CTX）由 worker 线程的 contextvars 继承，
      子智能体的 file.* 操作与主智能体落在同一工作区。
"""
import asyncio
import json
import logging
from contextvars import ContextVar
from typing import Any

from core.llm import chat_with_failover as _chat
# 复用智能搜索模块的纯工具函数：编码名映射 / 工具结果转文本（含截断）
from core.smart_search import _make_name_map, _result_to_text

_logger = logging.getLogger("subagent")

# agent_runtime 在 agent_reply 中注入当前智能体的
# {interface, model, commands, plugins, max_tool_rounds}；
# 通过 asyncio.to_thread 调用本命令时，线程上下文会携带该值。
SUBAGENT_CTX: ContextVar = ContextVar("subagent_ctx", default=None)

# 子智能体 LLM 轮次（每轮 = 一次模型调用 + 其发起的工具执行）：
#   显式传 max_rounds → 由主智能体自定义（max(1, v)，同主循环不设硬上限）；
#   未填 → 继承主智能体已设置的工具调用最大轮数（agent.max_tool_rounds >
#           全局 settings.max_tool_rounds > 默认 50，由 agent_runtime 算好注入）；
#   无上下文回退 → FALLBACK_ROUNDS（保守值）。
MAX_ROUNDS = 8
FALLBACK_ROUNDS = MAX_ROUNDS
# 累积工具结果上限（字符）：超过后提示模型尽快收尾，避免上下文爆炸
TOTAL_RESULT_MAX = 32000

# —— 子智能体运行事件（供主循环 SUB: 行实时转发前端过程框渲染）——
# 事件格式（JSON 单行）：{"t": <类型>, ...}
#   round: {"t":"round","n":1,"of":8}                          每轮开始
#   call : {"t":"call","name":"http_request","args_preview":..} 发起工具（参数预览截断）
#   done : {"t":"done","name":"http_request","ok":true,"preview":"..."} 工具完成（结果预览截断）
#   text : {"t":"text","round":1,"text":"..."}                 本轮正文输出（解说/最终总结，截断保护）
#   note : {"t":"note","text":"..."}                            运行提示（收尾催促等）
_CALL_ARGS_PREVIEW_MAX = 300
_DONE_PREVIEW_MAX = 160
_TEXT_MAX = 4000

# 追加到用户自定义 role 之后的通用执行规范（保证子智能体行为可收敛）
_PROMPT_SUFFIX = (
    "\n\n## 执行规范（通用，必须遵守）\n"
    "1. 你拥有独立的工具执行循环：需要操作时调用工具，工具结果会回传给你，"
    "可基于结果继续决策；\n"
    "2. 任务完成（或确认无法完成）后，**停止调用工具**，直接输出最终答复："
    "先一句话给出结论，再分条列出关键过程与产物要点（文件路径、命令输出摘要等），"
    "简洁明了；\n"
    "3. 严禁编造：只依据工具返回的真实结果与任务给定信息作答；\n"
    "4. 若未给你配置任何命令，则直接基于任务信息给出专业答复。"
)

# —— 命令 schema：注册到 COMMAND_TOOLS，供 LLM function calling ——
SUBAGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "agent.spawn",
        "description": (
            "派生子智能体：按任务需要现场自定义一个临时子智能体（名称、角色设定、"
            "能力白名单），由其独立运行多轮「调用工具 → 基于结果决策」循环完成"
            "给定任务，并把最终答复返回给你。子智能体用完即销毁，适合把一件边界清晰"
            "的子任务（如代码审查、批量文件整理、联网搜图、跑测试并汇总）整体委托出去。"
            "commands 只能从允许范围内选择（内建命令如 file.read，或插件如"
            " http_request / browser_control）：默认限你已拥有的能力；若你拥有"
            " cluster.list_capabilities（可查询全部可用能力），则可从全能力表选择。"
            "禁止在 commands 中包含 agent.spawn 自身。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "子智能体名称，简短体现其职责。例如：「测试工程师」「图片搜索员」。"
                    ),
                },
                "role": {
                    "type": "string",
                    "description": (
                        "子智能体的角色设定与工作方式（将作为其 system prompt）：职责范围、"
                        "工作步骤、输出要求、边界。例如：「你是代码审查员，逐文件检查语法与"
                        "逻辑缺陷，按严重程度分级输出问题清单，不负责修复。」"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "要完成的具体任务描述：目标、涉及的路径/范围、期望的产出格式。"
                        "例如：「审查 src/ 下所有 .py 文件的 import 错误，输出文件与行号清单。」"
                    ),
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "允许该子智能体使用的能力列表：内建命令（如 file.read、cmd.run）"
                        "与插件（如 http_request、browser_control、send_email）均可填写。"
                        "从你的能力范围内选择；无需任何能力时传空数组 []（子智能体将纯文本作答）。"
                        "例如：[\"http_request\", \"file.read\", \"browser_control\"]。"
                    ),
                },
                "max_rounds": {
                    "type": "integer",
                    "description": (
                        "可选：子智能体最大执行轮次（每轮 = 一次模型调用 + 工具执行）。"
                        "不填则默认继承你已设置的工具调用最大轮数；任务复杂可自行调大。"
                    ),
                },
            },
            "required": ["name", "role", "task", "commands"],
        },
    },
}


def _resolve_capabilities(requested: list, ctx: dict) -> tuple[list, list, list, list]:
    """解析子智能体的能力白名单，返回 (allowed_cmds, allowed_plugins, denied_cmds, denied_plugins)。

    能力两类：内建命令（COMMAND_TOOLS）+ 插件（PLUGIN_REGISTRY）。
    - selectable_cmds：命令表中可分配的命令（排除运行时注入的隐藏命令与 agent.spawn 自身）；
    - selectable_plugins：插件表全集（插件无运行时注入项，agent.spawn 不在插件表）；
    - 主智能体拥有 cluster.list_capabilities → 已授权全能力视野，命令与插件均可全表自由配置；
      否则命令限主智能体已勾选命令的子集、插件限已勾选插件的子集；
    - 既不在命令表也不在插件表的名字归入 denied_cmds（不可识别的能力）；
    - denied_* 返回未获授权的请求项，供主智能体感知并调整。
    """
    from core.commands import COMMAND_TOOLS, RUNTIME_INJECTED_COMMANDS
    from plugins import PLUGIN_REGISTRY

    forbidden = set(RUNTIME_INJECTED_COMMANDS) | {"agent.spawn"}
    selectable_cmds = {t["function"]["name"] for t in COMMAND_TOOLS} - forbidden
    selectable_plugins = set(PLUGIN_REGISTRY.keys())

    req_set = set()
    for c in requested or []:
        if isinstance(c, str):
            c = c.strip()
            if c:
                req_set.add(c)

    master_cmds = set(ctx.get("commands") or [])
    master_plugins = set(ctx.get("plugins") or [])
    privileged = "cluster.list_capabilities" in master_cmds

    if privileged:
        allowed_cmds = req_set & selectable_cmds
        allowed_plugins = req_set & selectable_plugins
    else:
        allowed_cmds = req_set & selectable_cmds & master_cmds
        allowed_plugins = req_set & selectable_plugins & master_plugins
    # 未获授权：表内可识别但无权限；表外名字（不可识别）并入 denied_cmds
    denied_cmds = (req_set & selectable_cmds) - allowed_cmds
    denied_plugins = (req_set & selectable_plugins) - allowed_plugins
    denied_cmds |= req_set - selectable_cmds - selectable_plugins
    return sorted(allowed_cmds), sorted(allowed_plugins), sorted(denied_cmds), sorted(denied_plugins)


async def _run_async(name: str, role: str, task: str, allowed_commands: list,
                     allowed_plugins: list, interface: dict, model: str, max_rounds: int,
                     emit=None) -> dict:
    """子智能体的多轮循环（async）。

    emit：可选事件回调 emit(event: dict)，在每个运行节点（轮次开始 / 工具调用 /
    工具完成 / 收尾提示）触发，供主循环通过 SUB: 行实时转发给前端过程框渲染；
    回调异常一律吞掉，不影响子智能体主流程。

    返回 {"ok": True, "summary": <最终答复>} 或 {"ok": False, "error": <错误信息>}。
    """
    # 延迟导入，避免与 commands 模块的循环依赖（commands 顶层 import 本模块的 schema 与入口）
    from core.commands import filter_tools, run_command
    from plugins import PLUGIN_REGISTRY, run_plugin
    # 参数清洗与主循环 _execute_tool 完全一致（URL/selector 清洗、内容字段原样透传）
    from core.agent_runtime import _sanitize_tool_arguments

    def _emit(ev: dict):
        if emit is None:
            return
        try:
            emit(ev)
        except Exception:  # noqa: BLE001  事件回调异常不中断子智能体主流程
            pass

    tools = filter_tools(allowed_commands, allowed_plugins, PLUGIN_REGISTRY, skills=[])
    name_map = _make_name_map(tools)
    allowed_set = set(allowed_commands) | set(allowed_plugins)
    allowed_cmd_set = set(allowed_commands)
    allowed_plugin_set = set(allowed_plugins)

    granted = allowed_commands + allowed_plugins
    if granted:
        cmd_hint = "、".join(granted)
    else:
        cmd_hint = "（未配置任何能力，请直接作答）"
    messages: list = [
        {"role": "system", "content": role + _PROMPT_SUFFIX},
        {"role": "user", "content": (
            f"你是被临时派生的子智能体「{name}」。\n"
            f"可用命令：{cmd_hint}\n\n"
            f"任务：\n{task}"
        )},
    ]

    accumulated = 0
    for round_no in range(max_rounds):
        _emit({"t": "round", "n": round_no + 1, "of": max_rounds})
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
            _logger.warning("子智能体「%s」LLM 调用异常：%s: %s", name, type(e).__name__, e)
            return {"ok": False, "error": f"子智能体「{name}」调用失败：{type(e).__name__}: {e}"}

        if err:
            detail = str(err.get("detail", ""))[:200]
            _logger.warning("子智能体「%s」上游错误：HTTP %s %s", name, err.get("status"), detail)
            return {"ok": False, "error": f"子智能体「{name}」调用失败：HTTP {err.get('status')} {detail}"}
        if not assistant:
            return {"ok": False, "error": f"子智能体「{name}」未返回有效响应"}

        tool_calls = assistant.get("tool_calls") or []
        # 回灌精简版 assistant 消息（去掉 reasoning_content 等上游私有字段）
        assistant_msg: dict = {"role": "assistant", "content": assistant.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # 本轮正文输出（发起工具前的过渡解说 / 最终总结）→ text 事件，
        # 前端子过程框实时显示子智能体的输出正文（截断保护防超长落库）
        _content = (assistant.get("content") or "").strip()
        if _content:
            _txt = _content[:_TEXT_MAX] + ("…[已截断]" if len(_content) > _TEXT_MAX else "")
            _emit({"t": "text", "round": round_no + 1, "text": _txt})

        if not tool_calls:
            # 模型不再调用工具 → 视为给出最终结论
            summary = (assistant.get("content") or "").strip()
            return {"ok": True, "summary": summary or f"【无输出】子智能体「{name}」未产生可总结的内容"}

        # 执行本轮所有工具调用（分发路径与主循环 _execute_tool 一致：
        # 命令 → run_command；插件 → param_presets 固定值注入后 run_plugin）
        for tc in tool_calls:
            fn = tc.get("function") or {}
            enc_name = fn.get("name") or ""
            cmd = name_map.get(enc_name, enc_name)
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if cmd in allowed_set:
                try:
                    _emit({"t": "call", "name": cmd,
                           "args_preview": json.dumps(args, ensure_ascii=False)[:_CALL_ARGS_PREVIEW_MAX]})
                    args = _sanitize_tool_arguments(cmd, args)
                    # —— 权限闸门（手动确认模式）：子智能体的危险操作同样要问用户 ——
                    # 请求经事件队列发 permission 事件 → 主循环 SUB: 行 → 前端确认弹窗，
                    # 用户点允许/拒绝后由 POST /api/permissions/confirm 唤醒本线程。
                    from core import permissions
                    _conv_id = None
                    _sctx = SUBAGENT_CTX.get()
                    if isinstance(_sctx, dict):
                        _conv_id = _sctx.get("conv_id")
                    _req = permissions.begin_request(cmd, args, conv_id=_conv_id)
                    _denied = False
                    if _req is not None:
                        _emit({"t": "permission", **_req})
                        if not permissions.wait_sync(_req["id"]):
                            _denied = True
                            result = {"error": f"用户拒绝执行：{cmd}（手动确认模式，"
                                               f"本次调用未获授权，请先征得用户同意）"}
                    if not _denied:
                        if cmd in allowed_cmd_set:
                            result = run_command(cmd, **(args or {}))
                        else:
                            # 插件执行：注入能力参数预设的固定参数（模型不可见、不可改，固定值优先覆盖）
                            from core.param_presets import get_fixed_values
                            fixed = get_fixed_values("plugin", cmd)
                            if fixed:
                                args = {**(args or {}), **fixed}
                            result = run_plugin(cmd, **(args or {}))
                except Exception as e:
                    result = {"error": f"工具 {cmd} 执行失败：{e}"}
                _emit({"t": "done", "name": cmd,
                       "ok": not (isinstance(result, dict) and "error" in result),
                       "preview": _result_to_text(result)[:_DONE_PREVIEW_MAX]})
            else:
                result = {"error": f"不允许的工具：{cmd}（不在该子智能体的能力白名单内）"}
            rtext = _result_to_text(result)
            accumulated += len(rtext)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": rtext})

        # 信息量已很大 → 提示模型尽快收尾，避免无谓地继续膨胀上下文
        if accumulated > TOTAL_RESULT_MAX and round_no >= 1:
            messages.append({
                "role": "user",
                "content": "已收集到足够信息，请立即基于以上工具结果给出最终答复，不要再调用工具。",
            })
            _emit({"t": "note", "text": "已收集到足够信息，提示子智能体尽快收尾"})

    return {"ok": True, "summary": f"【未完成】子智能体「{name}」达到最大轮次（{max_rounds}）仍未收敛，请缩小任务范围后重试。"}


def run_subagent(name: str, role: str, task: str, commands: list | None = None,
                 max_rounds: int | None = None, event_queue=None) -> dict:
    """agent.spawn 命令的同步入口（在 _execute_tool 的 worker 线程中调用）。

    commands 可混合内建命令名与插件名。模型/接口与能力边界优先取 SUBAGENT_CTX
    （当前智能体的选定值与 commands/plugins 白名单），否则回退默认模型接口
    （此时无主智能体白名单 → 只允许空能力集）。
    max_rounds 未填时默认继承主智能体已设置的工具调用最大轮数（ctx.max_tool_rounds）。

    event_queue：可选 queue.Queue，子智能体运行事件实时 put 进队列，供主循环
    消费并转发（SUB: 行）；无论是否传队列，事件副本都会收集进返回值
    result["_sub_events"]（主循环取出后放 tool 消息 sub_process 字段落库，
    前端历史加载可恢复）。
    """
    from core.store import load

    name = (name or "").strip() or "子智能体"
    role = (role or "").strip()
    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "缺少任务描述 task"}
    if not role:
        return {"ok": False, "error": "缺少角色设定 role"}

    ctx = SUBAGENT_CTX.get()
    if not isinstance(ctx, dict):
        ctx = {}
    interface = ctx.get("interface")
    model = ctx.get("model")
    if not interface:
        interface = (load("model_interfaces") or [{}])[0]
    if not interface:
        return {"ok": False, "error": "未配置模型接口，无法派生子智能体"}
    model = model or interface.get("default_model") or "gpt-4o-mini"

    allowed_cmds, allowed_plugins, denied_cmds, denied_plugins = _resolve_capabilities(commands, ctx)
    # 轮次：显式传 max_rounds → 主智能体自定义；未填 → 继承主智能体已设置的
    # 工具调用最大轮数（agent_runtime 注入 ctx.max_tool_rounds，含全局回退链）；
    # 无上下文 → 保守回退值。统一 max(1, v) 保下限，同主循环不设硬上限。
    if isinstance(max_rounds, int) and 1 <= max_rounds:
        rounds = max_rounds
    elif isinstance(ctx.get("max_tool_rounds"), int) and 1 <= ctx["max_tool_rounds"]:
        rounds = ctx["max_tool_rounds"]
    else:
        rounds = FALLBACK_ROUNDS

    if denied_cmds or denied_plugins:
        _logger.info(
            "子智能体「%s」未获授权的能力（已剔除）：命令 [%s] 插件 [%s]",
            name, ", ".join(denied_cmds), ", ".join(denied_plugins),
        )

    events: list = []

    def _emit(ev: dict):
        events.append(ev)
        if event_queue is not None:
            try:
                event_queue.put(ev)
            except Exception:  # noqa: BLE001  队列异常不影响子智能体主流程
                pass

    try:
        result = asyncio.run(_run_async(name, role, task, allowed_cmds, allowed_plugins,
                                        interface, model, rounds, emit=_emit))
    except RuntimeError as e:
        # 理论上不会发生（本命令始终在 worker 线程执行，无运行中的事件循环）
        _logger.warning("子智能体「%s」启动事件循环失败：%s", name, e)
        return {"ok": False, "error": f"子智能体「{name}」启动失败：{e}"}

    result["subagent"] = name
    result["granted_commands"] = allowed_cmds
    result["granted_plugins"] = allowed_plugins
    if events:
        # 运行事件副本：主循环取出放 tool 消息 sub_process 字段（落库 + 前端历史恢复）
        result["_sub_events"] = events
    # 把未获授权项回传给主智能体，便于其感知边界并调整后续派生请求
    if denied_cmds:
        result["denied_commands"] = denied_cmds
    if denied_plugins:
        result["denied_plugins"] = denied_plugins
    return result


def cmd_agent_spawn(name: str, role: str, task: str, commands: list | None = None,
                    max_rounds: int | None = None) -> dict:
    """agent.spawn 命令实现（run_command 反射调用）。"""
    return run_subagent(name, role, task, commands, max_rounds)