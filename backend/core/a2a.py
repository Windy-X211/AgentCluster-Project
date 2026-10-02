"""A2A 风格的智能体间通信 — a2a-lite/0.1 信封协议

连线智能体之间用结构化「消息信封」交换信息（字段对齐行业 A2A 的 Message/Task/Artifact）：

    {
      "protocol": "a2a-lite/0.1",
      "message_id": "msg_xxx",          # 本条消息唯一 id
      "task_id":    "task_xxx",         # 一次移交 = 一个 task
      "thread_id":  <conversation_id>,  # 会话线程
      "from": {"agent_id": 3, "name": "小说作家"},
      "to":   {"agent_id": 2, "name": "程序员"},
      "kind": "handoff",                # message | handoff | artifact
      "reply_to": null,
      "parts": [
        {"type": "text", "text": "..."},
        {"type": "artifact", "name": "PRD.md", "mime_type": "text/markdown", "content": "..."}
      ],
      "task": {"state": "submitted"}    # submitted -> completed / failed
    }

当前状态：
- A2A_HANDOFF_ENABLED=False（成果移交/自动出边接力暂时停用）；
- cluster.send_message 仍可用：连线智能体按需调用发送消息（可往返多轮）（kind=message，无附件），
  支持往返多轮，受 MAX_CALL 限额。
"""
import uuid
from contextvars import ContextVar
from typing import Any

from core.store import get_by, upsert

PROTOCOL = "a2a-lite/0.1"
MAX_CHAIN = 5   # 单次用户发言最多接力的智能体数（handoff）
MAX_CALL = 4  # 单次用户发言最多消息数（cluster.send_message 多轮）
MAX_CALLS_PER_PAIR = 2  # 同一对智能体 A→B 之间最多往返次数（防 A↔B 死循环）

# A2A 成果移交协议总开关：False=暂时停用（handoff 工具 + 自动出边接力均不生效）
# 保留 cluster.send_message 发送消息（可往返多轮）（连线智能体按需调用，仍用 a2a-lite 信封 kind=message）
A2A_HANDOFF_ENABLED = False

# 当前对话轮次的共享状态（async 上下文隔离，互不串话）
# {visited, handoffs, calls, call_count, skip_auto_ids, relayed,
#  current_agent_id, all_agents, canvas, reply_to_msg_id, reply_to_from}
TURN_CTX: ContextVar[dict | None] = ContextVar("a2a_turn", default=None)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def make_envelope(conv_id: int | None, from_agent: dict, to_agent: dict,
                  kind: str = "handoff", parts: list[dict] | None = None,
                  reply_to: str | None = None) -> dict:
    """构造一条 a2a-lite 消息信封。"""
    return {
        "protocol": PROTOCOL,
        "message_id": _new_id("msg"),
        "task_id": _new_id("task"),
        "thread_id": conv_id,
        "from": {"agent_id": from_agent.get("id"), "name": from_agent.get("name", "?")},
        "to": {"agent_id": to_agent.get("id"), "name": to_agent.get("name", "?")},
        "kind": kind,
        "reply_to": reply_to,
        "parts": parts or [],
        "task": {"state": "submitted", "error": None},
    }


def render_envelope(env: dict) -> str:
    """把信封渲染成下游智能体收到的输入（handoff=成果移交 / message=消息交流）。"""
    kind = env.get("kind")
    lines = [
        f"【A2A 协议消息】kind={kind} "
        f"task_id={env.get('task_id')} message_id={env.get('message_id')}",
        f"来自：{env.get('from', {}).get('name')}（agent_id={env.get('from', {}).get('agent_id')}）",
        f"递交给你：{env.get('to', {}).get('name')}（agent_id={env.get('to', {}).get('agent_id')}）",
    ]
    if env.get("reply_to"):
        lines.append(f"reply_to={env['reply_to']}（对上一条消息的回复）")
    for p in env.get("parts", []):
        if p.get("type") == "text" and p.get("text"):
            lines.append(str(p["text"]))
        elif p.get("type") == "artifact" and p.get("content") is not None:
            lines.append(f"[成果附件 {p.get('name', 'artifact')} · {p.get('mime_type', 'text/plain')}]\n"
                         f"{p.get('content')}")
    if kind == "message":
        lines.append(
            "这是与你的多轮交流/咨询消息（kind=message，不是成果移交）。请以角色参与消息、回应对方问题；"
            "若需继续往返，调用 cluster.send_message(to_agent=对方, message=你的回复)；"
            "消息结束后直接输出结论回复用户（成果移交协议已暂时停用，勿调用 cluster.handoff）。"
        )
    else:
        lines.append("请以你的角色接收该成果并继续处理，输出你自己的产出。")
    return "\n".join(lines)


def build_handoff_envelope(conv_id: int | None, from_agent: dict, to_agent: dict,
                           final_text: str, handoff: dict | None = None) -> dict:
    """auto 接力（handoff=None）或显式 cluster.handoff（handoff=dict）→ handoff 信封。"""
    if handoff:
        text = handoff.get("summary") or final_text
        art_name = handoff.get("artifact_name") or f"{from_agent.get('name')} 的交付物"
        art_content = handoff.get("artifact_content") or final_text
    else:
        text = final_text
        art_name = f"{from_agent.get('name')} 的交付物"
        art_content = final_text
    parts: list[dict] = [{"type": "text", "text": (text or "")[:4000]}]
    if art_content:
        parts.append({
            "type": "artifact", "name": art_name,
            "mime_type": "text/plain", "content": art_content[:20000],
        })
    return make_envelope(conv_id, from_agent, to_agent, kind="handoff", parts=parts)


def build_call_envelope(conv_id: int | None, from_agent: dict, to_agent: dict,
                           call_payload: dict) -> dict:
    """cluster.send_message → kind=message 信封（仅消息文本，无成果附件）。"""
    text = call_payload.get("message") or ""
    parts: list[dict] = [{"type": "text", "text": (text or "")[:4000]}]
    return make_envelope(conv_id, from_agent, to_agent, kind="message",
                         parts=parts, reply_to=call_payload.get("reply_to"))


def _agents_connected(canvas: dict, from_id: Any, to_id: Any) -> bool:
    """两智能体在画布上是否有连线（任一方向）。仅供展示/感知用。"""
    fnodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == from_id}
    tnodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == to_id}
    if not fnodes or not tnodes:
        return False
    return any(
        (e.get("from_node") in fnodes and e.get("to_node") in tnodes)
        or (e.get("from_node") in tnodes and e.get("to_node") in fnodes)
        for e in canvas.get("edges", [])
    )


def _can_send_to(canvas: dict, from_id: Any, to_id: Any) -> bool:
    """方向性检查：from_id 是否有出边直接指向 to_id（A→B 边存在）。
    执行层强制校验：只有当 from→to 方向有连线时，from 才能主动调用 cluster.send_message / cluster.handoff 给 to。"""
    fnodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == from_id}
    tnodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == to_id}
    if not fnodes or not tnodes:
        return False
    return any(
        e.get("from_node") in fnodes and e.get("to_node") in tnodes
        for e in canvas.get("edges", [])
    )


def _resolve_target(turn: dict, to_agent: Any) -> tuple[dict | None, str | None]:
    """按 id → 名称精确 → 名称包含 解析目标；返回 (target, error)。"""
    all_agents = turn.get("all_agents", {})
    target = None
    try:
        target = all_agents.get(int(to_agent))
    except (TypeError, ValueError):
        pass
    if not target:
        for a in all_agents.values():
            if a.get("name") == str(to_agent):
                target = a
                break
    if not target:
        for a in all_agents.values():
            if str(to_agent) in (a.get("name") or ""):
                target = a
                break
    if not target:
        return None, f"未找到智能体：{to_agent}"
    if target.get("enabled") is False:
        return None, f"「{target.get('name')}」已停用，无法接收"
    return target, None


def collect_relay_targets(turn: dict, canvas: dict, agent: dict) -> list[tuple[dict, dict | None]]:
    """本轮发言结束后要接力的目标：显式 handoff ∪ 画布自动出边，去重、防环、限量。
    A2A_HANDOFF_ENABLED=False 时整体停用（不自动接力、不处理显式 handoff）。"""
    aid = agent.get("id")
    explicit = [h for h in turn.get("handoffs", []) if h.get("from_id") == aid]
    turn["handoffs"] = [h for h in turn.get("handoffs", []) if h.get("from_id") != aid]
    if not A2A_HANDOFF_ENABLED:
        turn.setdefault("visited", set()).add(aid)
        return []

    auto: list[tuple[Any, None]] = []
    if aid not in turn.get("skip_auto_ids", set()):
        node_ids = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == aid}
        nmap = {n.get("id"): n for n in canvas.get("nodes", [])}
        for e in canvas.get("edges", []):
            if e.get("from_node") in node_ids:
                tn = nmap.get(e.get("to_node"))
                if tn and tn.get("agent_id"):
                    auto.append((tn.get("agent_id"), None))

    explicit_pairs = [(h.get("to_agent_id"), h) for h in explicit]
    merged = explicit_pairs + [x for x in auto if x[0] not in {p[0] for p in explicit_pairs}]

    visited = turn.setdefault("visited", set())
    visited.add(aid)
    all_agents = turn.get("all_agents", {})
    already = turn.get("relayed", 0)
    result: list[tuple[dict, dict | None]] = []
    for tid, h in merged:
        if already + len(result) >= MAX_CHAIN:
            break
        if tid in visited:
            continue
        target = all_agents.get(tid)
        if not target:
            continue
        if target.get("enabled") is False:
            continue  # 已停用的智能体不参与接力
        result.append((target, h))
        visited.add(tid)
    turn["relayed"] = already + len(result)
    return result


def collect_call_targets(turn: dict, agent: dict) -> list[tuple[dict, dict]]:
    """消费当前智能体排队的 cluster.send_message 消息。

    熔断双保险：
    1. MAX_CALL 总量限制（单次用户发言最多 N 条消息）；
    2. MAX_CALLS_PER_PAIR 单对限制（A→B 同一方向最多往返次数，防 A↔B 死循环）。
    """
    aid = agent.get("id")
    queued = [d for d in turn.get("calls", []) if d.get("from_id") == aid]
    turn["calls"] = [d for d in turn.get("calls", []) if d.get("from_id") != aid]

    budget = turn.get("call_count", 0)
    all_agents = turn.get("all_agents", {})

    # 单对计数（key=(from_id, to_id)）
    pair_counts = turn.setdefault("_pair_call_counts", {})

    result: list[tuple[dict, dict]] = []
    for d in queued:
        if budget >= MAX_CALL:
            break
        tid = d.get("to_agent_id")
        if tid == aid:
            continue
        pair_key = (aid, tid)
        if pair_counts.get(pair_key, 0) >= MAX_CALLS_PER_PAIR:
            # 熔断：同一对已超过允许往返次数，跳过本次 call
            continue
        target = all_agents.get(tid)
        if not target:
            continue
        if target.get("enabled") is False:
            continue
        result.append((target, d))
        pair_counts[pair_key] = pair_counts.get(pair_key, 0) + 1
        budget += 1
    turn["call_count"] = budget
    return result


def mark_envelope_done(conv_id: int, message_id: str) -> None:
    """下游处理完毕 → 重载会话把该信封的 task 状态置为 completed（避免脏写覆盖）。"""
    try:
        conv = get_by("conversations", conv_id)
        if not conv:
            return
        changed = False
        for m in conv.get("messages", []):
            env = m.get("envelope")
            if isinstance(env, dict) and env.get("message_id") == message_id:
                env.setdefault("task", {})["state"] = "completed"
                changed = True
        if changed:
            upsert("conversations", conv)
    except Exception:
        pass


def cmd_cluster_handoff(to_agent: Any = None, summary: str = "",
                        artifact_name: str = "", artifact_content: str = "",
                        skip_auto: bool = False) -> dict:
    """cluster.handoff — A2A 成果移交 / 跳过自动接力（本轮回复结束后生效）。"""
    if not A2A_HANDOFF_ENABLED:
        return {"error": "A2A 成果移交协议已暂时停用；连线智能体请改用 cluster.send_message 按需沟通，"
                         "或直接输出文字回复用户"}
    turn = TURN_CTX.get()
    if not turn:
        return {"error": "cluster.handoff 仅在集群对话（linked 模式）中可用"}

    if skip_auto:
        turn.setdefault("skip_auto_ids", set()).add(turn.get("current_agent_id"))

    if to_agent in (None, "", "none", "null"):
        if skip_auto:
            return {"ok": True, "skip_auto": True,
                    "note": "本轮回复结束后将不再自动沿连线接力"}
        return {"error": "请提供 to_agent（下游智能体 id 或名称），或设置 skip_auto=true 跳过自动接力"}

    target, err = _resolve_target(turn, to_agent)
    if err:
        return {"error": err}

    # 目标必须在当前画布上
    canvas = turn.get("canvas")
    if canvas is not None:
        on_canvas = any(n.get("agent_id") == target.get("id") for n in canvas.get("nodes", []))
        if not on_canvas:
            return {"error": f"「{target.get('name')}」不在当前画布上，无法接收移交"}

    h = {
        "from_id": turn.get("current_agent_id"),
        "to_agent_id": target.get("id"),
        "summary": summary or "",
        "artifact_name": artifact_name or "",
        "artifact_content": artifact_content or "",
    }
    turn.setdefault("handoffs", []).append(h)
    return {
        "ok": True,
        "handoff": {"to": target.get("name"), "to_agent_id": target.get("id")},
        "note": "已排队：本轮回复结束后按 a2a-lite 协议移交给对方",
    }


def cmd_cluster_send_message(to_agent: Any = None, message: str = "") -> dict:
    """cluster.send_message — 连线智能体发送消息/咨询/协作（kind=message，本轮结束时送达）。"""
    turn = TURN_CTX.get()
    if not turn:
        return {"error": "cluster.send_message 仅在集群对话（linked 模式）中可用"}

    if to_agent in (None, "", "none", "null"):
        return {"error": "请提供 to_agent（画布上已连线的智能体 id 或名称）"}
    if not (message or "").strip():
        return {"error": "请提供 message（要消息/咨询的内容）"}

    target, err = _resolve_target(turn, to_agent)
    if err:
        return {"error": err}

    from_id = turn.get("current_agent_id")
    if target.get("id") == from_id:
        return {"error": "不能与自己消息"}

    canvas = turn.get("canvas")
    if canvas is not None:
        if not any(n.get("agent_id") == target.get("id") for n in canvas.get("nodes", [])):
            return {"error": f"「{target.get('name')}」不在当前画布上"}

        can_initiate = _can_send_to(canvas, from_id, target.get("id"))
        has_any_link = _agents_connected(canvas, from_id, target.get("id"))
        # 回复豁免：如果 turn.reply_to_from 就是目标 agent → 是对方向你发过消息后你在回复，
        # 即使方向上只有对方→你也允许（否则 relay 单向链无法往返）
        is_reply = (turn.get("reply_to_from") == target.get("id"))

        if not can_initiate and not (has_any_link and is_reply):
            if has_any_link:
                return {"error": f"与「{target.get('name')}」的连线方向是对方→你，你不能**主动发起 **call；"
                                 f"但你可以在对方先找你之后再回复"}
            return {"error": f"与「{target.get('name')}」尚未连线，无法消息；请先在画布上从你的节点拖一条连线指向对方"}

    # 消息 ≠ 成果移交：本轮自动沿出边的成果接力默认关闭，避免消息被当成果扩散
    turn.setdefault("skip_auto_ids", set()).add(from_id)

    d = {
        "from_id": from_id,
        "to_agent_id": target.get("id"),
        "message": message,
        "reply_to": turn.get("reply_to_msg_id"),
    }
    turn.setdefault("calls", []).append(d)
    return {
        "ok": True,
        "call": {"to": target.get("name"), "to_agent_id": target.get("id")},
        "note": "已排队：本轮结束后送达对方进行发送消息（可往返多轮）。对方可用 cluster.send_message 回复你继续往返。",
    }


# cluster.handoff 的 OpenAI function-calling schema（并入 CLUSTER_TOOLS）
HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "cluster.handoff",
        "description": "A2A 成果移交（按需调用）：有明确成果需下游接力处理时调用。"
                       "用户直接向你提问或只是寒暄 → 直接输出文字回复用户，不要调用任何沟通工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {"type": "string",
                             "description": "下游智能体的**名称字符串**（不是 id，不是裸标识符！必须加 JSON 双引号，例如 \"总经理\"）。禁止用于传递问候/普通问答；仅真正移交成果时填写"},
                "summary": {"type": "string", "description": "移交说明：交接背景与需要对方做什么"},
                "artifact_name": {"type": "string", "description": "成果名称，如 PRD草稿.md"},
                "artifact_content": {"type": "string", "description": "成果内容，留空则用你本轮回复全文"},
                "skip_auto": {"type": "boolean",
                              "description": "true=本轮结束后不自动沿连线接力。单用且不填 to_agent=只关自动接力，不向任何人移交"},
            },
            "required": ["to_agent"],
        },
    },
}


# cluster.send_message 的 OpenAI function-calling schema（并入 CLUSTER_TOOLS）
CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "cluster.send_message",
        "description": (
            "发送结构化协作消息（按需调用，**唯一**用于智能体间沟通的工具）："
            "向画布上与你有**出边（你→对方）**的智能体交流信息或发出任务，支持往返多轮。"
            "打招呼/用户直接问你简单问题 → 直接输出文字回复，不要调用本工具。\n"
            "⚠️ 方向约束：to_agent 只能填 system prompt 里明确列出的「你可主动发起 call」的成员，"
            "即与你双向连线 或 仅你有出边指向的成员；仅对方有出边指向你的（单向入边）不能填。\n"
            "你想要谁收到消息，必须显式调用 cluster.send_message。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {"type": "string",
                             "description": "对方智能体的**名称字符串**（不是 id，不是裸标识符！必须加 JSON 双引号，例如 \"总经理\"）。"
                                            "名称须在 system prompt 的「可主动发起」列表里"},
                "message": {"type": "string",
                            "description": "要发给对方的**核心**问题/信息：只写对方需要的内容（问题、观点、请求确认的点），"
                                           "不要把整段面向用户的回复原文塞进来——对方不需要看到给用户的那部分铺垫或总结。"},
            },
            "required": ["to_agent", "message"],
        },
    },
}
