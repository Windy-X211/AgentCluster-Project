"""历史上下文压缩 + token 估算。

机制：
- token 估算为本地规则（中日韩字符 ≈ 1 token/字符，ASCII ≈ 1 token/4 字符），零请求开销
- 压缩线 = 模型上下文窗口（接口配置 model_windows，缺省用全局默认）× 全局阈值百分比
- 达到压缩线即触发压缩：用 LLM 把较早历史总结成一条带 compressed_summary 标记的
  system 摘要消息替换旧消息（会话历史共享，压缩对全会话生效），最近 N 条完整对话
  原样保留；摘要生成失败则放弃压缩（保持原历史，宁超不长错）。
"""
import json
import logging
from datetime import datetime

from core.store import load, upsert, get_by
from core.llm import chat_with_failover

_logger = logging.getLogger("ctx_compress")

_RENDERABLE_ROLES = ("system", "user", "assistant", "tool")

# 摘要请求的 transcript 字符预算 = 窗口 × 0.5（另一半留给输出与格式开销）
_TRANSCRIPT_BUDGET_RATIO = 0.5

_SUMMARY_SYS = (
    "你是对话历史压缩器。把用户提供的对话历史压缩成一份结构化摘要，"
    "作为后续对话的背景记忆使用。要求：\n"
    "1. 保留：用户的核心诉求与约束、关键事实与数据、已完成的任务与结论、"
    "涉及的重要文件路径与参数、未完成事项与待办；\n"
    "2. 忽略：寒暄、重复内容、失败尝试的过程细节（只保留失败原因的结论）；\n"
    "3. 用简洁的中文条目式输出，总长不超过 800 字；不要输出与摘要无关的内容。"
)


# ==============================================================
# token 估算
# ==============================================================
def estimate_tokens(text) -> int:
    """本地估算 token：CJK/全角 ≈ 1 token/字符，ASCII ≈ 1 token/4 字符。"""
    if not text:
        return 0
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    n = len(text)
    ascii_n = len(text.encode("ascii", "ignore"))
    return (n - ascii_n) + (ascii_n + 3) // 4


def estimate_message_tokens(m: dict) -> int:
    """单条落库消息的估算 token（content + tool_calls + tool_call_id 开销）。"""
    if not isinstance(m, dict):
        return 0
    tokens = estimate_tokens(m.get("content"))
    tc = m.get("tool_calls")
    if tc:
        try:
            tokens += estimate_tokens(json.dumps(tc, ensure_ascii=False))
        except Exception:
            pass
    if m.get("tool_call_id"):
        tokens += 8
    return tokens + 4  # role 等固定开销


# ==============================================================
# 窗口 / 压缩线解析
# ==============================================================
def _parse_window_value(v) -> int | None:
    """窗口值容错解析：int/float 直取；字符串支持 128k / 1m / 2b 后缀。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        s = v.strip().lower()
        mult = 1
        if s.endswith("k"):
            mult, s = 1_000, s[:-1]
        elif s.endswith("m"):
            mult, s = 1_000_000, s[:-1]
        elif s.endswith("b"):
            mult, s = 1_000_000_000, s[:-1]
        try:
            n = float(s)
            n = int(n * mult)
            return n if n > 0 else None
        except ValueError:
            return None
    return None


def model_window(interface: dict | None, model: str, settings: dict | None = None) -> int:
    """模型上下文窗口：接口 model_windows[model] → 全局默认。"""
    st = settings if settings is not None else (load("settings") or {})
    default = _parse_window_value(st.get("context_window_default")) or 128_000
    mw = (interface or {}).get("model_windows") or {}
    v = mw.get(model) if isinstance(mw, dict) else None
    return _parse_window_value(v) or default


def compress_settings(settings: dict | None = None) -> dict:
    st = settings if settings is not None else (load("settings") or {})
    try:
        threshold = float(st.get("context_compress_threshold") or 0.8)
    except (TypeError, ValueError):
        threshold = 0.8
    threshold = min(max(threshold, 0.1), 0.98)
    try:
        keep = int(st.get("context_keep_recent") or 20)
    except (TypeError, ValueError):
        keep = 20
    return {
        "enabled": st.get("context_compress_enabled", True) is not False,
        "threshold": threshold,
        "keep_recent": max(1, keep),
    }


# ==============================================================
# 上下文统计（对话区指示器数据源）
# ==============================================================
def assemble_context(conv: dict, agent: dict, canvas: dict | None):
    """与 runtime _agent_reply_inner 一致的上下文组装（不含本轮用户输入），用于 token 估算。"""
    from core.agent_runtime import _system_prompt, _agent_tools, _build_cluster_context

    interface = get_by("model_interfaces", agent.get("interface_id")) or (load("model_interfaces") or [{}])[0]
    model = agent.get("model") or interface.get("default_model") or "gpt-4o-mini"
    mode = "linked" if (conv or {}).get("canvas_id") else "independent"
    tools = _agent_tools(agent)
    cluster_context = ""
    if mode == "linked" and canvas:
        cluster_context, _ = _build_cluster_context(canvas, agent)
    system_prompt = _system_prompt(agent, tools, cluster_context,
                                   canvas.get("id") if canvas else None, canvas, None)
    tokens = estimate_tokens(system_prompt)
    for m in (conv or {}).get("messages") or []:
        tokens += estimate_message_tokens(m)
    return {"interface": interface, "model": model, "system_prompt": system_prompt, "tokens": tokens}


def context_stats(conv: dict, agent: dict, canvas: dict | None) -> dict:
    """GET /conversations/{cid}/context 的载荷：估算 token + 压缩线进度。"""
    asb = assemble_context(conv, agent, canvas)
    cs = compress_settings()
    window = model_window(asb["interface"], asb["model"])
    limit = int(window * cs["threshold"])
    tokens = asb["tokens"]
    return {
        "tokens": tokens,
        "window": window,
        "threshold": cs["threshold"],
        "limit": limit,
        "progress": round(tokens / limit, 4) if limit > 0 else 0,
        "enabled": cs["enabled"],
        "keep_recent": cs["keep_recent"],
        "model": asb["model"],
    }


# ==============================================================
# 压缩执行
# ==============================================================
def _renderable(m) -> bool:
    return isinstance(m, dict) and m.get("role") in _RENDERABLE_ROLES


async def maybe_compress(conv: dict, agent: dict, interface: dict, model: str, system_prompt: str) -> dict | None:
    """组装 LLM 上下文前调用：达到压缩线则压缩 conv.messages 并落库，返回压缩信息。

    压缩语义（会话级共享历史）：
    - 被压缩的旧消息（含旧摘要）→ 一条新摘要消息（role=system + compressed_summary 标记）
    - 最近 keep_recent 条完整对话原样保留（保留段起点对齐到 user 消息边界，
      保证 assistant.tool_calls 与 tool 结果的配对不被拦腰截断）
    - 摘要生成失败 → 返回 None，本轮不压缩（保持原历史）
    """
    try:
        cs = compress_settings()
        if not cs["enabled"]:
            return None
        window = model_window(interface, model)
        limit = int(window * cs["threshold"])
        if limit <= 0:
            return None
        msgs = conv.get("messages") or []
        if not msgs:
            return None
        total = estimate_tokens(system_prompt) + sum(estimate_message_tokens(m) for m in msgs)
        if total <= limit:
            return None

        keep = cs["keep_recent"]
        rend = [i for i, m in enumerate(msgs) if _renderable(m)]
        # 消息条数不足保留量（历史短但单条超长的小窗口场景）→ 收缩保留量，保证仍有可压缩区间
        if len(rend) <= keep:
            keep = max(4, len(rend) // 2)
        if len(rend) <= keep:
            return None  # 历史太短，没有可压缩的区间
        split = rend[-keep]
        # 保留段起点向前对齐到最近的 user 消息（tool 配对完整性）
        j = split
        while j > 0 and msgs[j].get("role") != "user":
            j -= 1
        if msgs[j].get("role") == "user":
            split = j
        if split <= 0:
            return None

        dropped = msgs[:split]
        summary = await _generate_summary(interface, model, dropped, window)
        if not summary:
            return None

        summary_msg = {
            "role": "system",
            "content": "[历史上下文摘要（较早的对话已压缩，供背景参考）]\n" + summary,
            "compressed_summary": True,
            "compressed_count": len(dropped),
            "ts": datetime.utcnow().isoformat(),
        }
        conv["messages"] = [summary_msg] + list(msgs[split:])
        upsert("conversations", conv)
        _logger.info("conv=%s 上下文压缩：%d 条历史 → 摘要（压缩前估算 %d token，窗口 %d，压缩线 %d）",
                     conv.get("id"), len(dropped), total, window, limit)
        return {"dropped": len(dropped), "tokens_before": total, "window": window, "limit": limit}
    except Exception as e:  # 压缩任何异常都不阻断本轮对话
        _logger.warning("上下文压缩失败（忽略，继续用原历史）：%s", e)
        return None


async def _generate_summary(interface: dict, model: str, dropped: list, window: int) -> str | None:
    """用 LLM 把被压缩历史总结成摘要；失败返回 None。"""
    budget_chars = max(4000, int(window * _TRANSCRIPT_BUDGET_RATIO))
    lines = []
    for m in dropped:
        if not _renderable(m):
            continue  # a2a 信封等不进摘要
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content else ""
        if m.get("compressed_summary"):
            lines.append("[此前历史摘要] " + content)
            continue
        role = m.get("role")
        if role == "assistant":
            who = f"（智能体#{m.get('agent_id')}）" if m.get("agent_id") else ""
            extra = " [本轮调用过工具]" if m.get("tool_calls") else ""
            lines.append(f"[assistant{who}]{extra} " + content[:2000])
        elif role == "tool":
            lines.append("[工具结果] " + content[:800])
        else:
            lines.append(f"[{role}] " + content[:2000])
    transcript = "\n".join(lines)
    if not transcript.strip():
        return None
    # 超预算：保头 25%（旧摘要在最前，避免丢失已压缩记忆）+ 尾 75%（较新的上下文更相关）
    if len(transcript) > budget_chars:
        head = transcript[: int(budget_chars * 0.25)]
        tail = transcript[-int(budget_chars * 0.75):]
        transcript = head + "\n…（中间历史过长，已略）…\n" + tail

    req_msgs = [
        {"role": "system", "content": _SUMMARY_SYS},
        {"role": "user", "content": transcript},
    ]
    try:
        async for kind, data in chat_with_failover(interface, model, req_msgs, stream=False, tool_choice="none"):
            if kind == "message":
                c = (data or {}).get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
            elif kind == "error":
                _logger.warning("上下文摘要生成失败：%s", data)
                return None
    except Exception as e:
        _logger.warning("上下文摘要生成异常：%s", e)
        return None
    return None