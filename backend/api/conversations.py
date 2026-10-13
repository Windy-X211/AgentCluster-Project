import json
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Any
from core.store import load, upsert, delete_by, get_by, next_id, load_list_payloads
from core.agent_runtime import agent_reply
from core.file_backup import (
    list_file_ops, restore_op, restore_range, _load_conv_ops,
)

router = APIRouter()


class ConvIn(BaseModel):
    name: str = "新对话"
    mode: str = "independent"  # independent | linked
    agent_ids: list[int] = []
    canvas_id: int | None = None


class MsgIn(BaseModel):
    agent_id: int
    content: str


class RestoreReq(BaseModel):
    op_indices: list[int]          # 要还原的操作索引列表（在该会话 ops 列表内的位置）


def _conv_activity_key(c: dict):
    """会话活跃时间：最后一条消息 ts → created_at → id（保证可比较）"""
    last_ts = None
    for m in c.get("messages") or []:
        ts = m.get("ts")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    return last_ts or c.get("created_at") or f"{c.get('id', 0):020d}"


@router.get("")
def list_convs():
    """上新下旧：按最后一条消息时间（或创建时间）降序。

    走 store.load_list_payloads 的 (mtime,size) 缓存：命中时只 stat、不读盘不解析，
    序列化结果与 FastAPI JSONResponse 完全等价，返回内容不变。"""
    entries = load_list_payloads("conversations", _conv_activity_key)
    entries.sort(key=lambda e: e[0], reverse=True)
    body = b"[" + b",".join(payload for _, payload in entries) + b"]"
    return Response(content=body, media_type="application/json")


@router.post("")
def create_conv(c: ConvIn):
    item = c.model_dump()
    item["id"] = next_id("conversations")
    item["messages"] = []
    item["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    return upsert("conversations", item)


@router.put("/{cid}")
def update_conv(cid: int, data: dict):
    """更新会话。
    - 默认只更新元信息（mode / canvas_id / name / agent_ids），保留 messages 历史。
    - 若 data 里显式带了 messages 字段 → 用它整段替换（支持前端流式同步）。
    - data 含 reset_messages=True 时清空对话历史（不删除会话本身）。"""
    cur = get_by("conversations", cid)
    if not cur:
        raise HTTPException(404, "not found")
    for k in ("mode", "canvas_id", "name", "agent_ids"):
        if k in data:
            cur[k] = data[k]
    if "messages" in data and isinstance(data["messages"], list):
        cur["messages"] = data["messages"]
    if data.get("reset_messages"):
        cur["messages"] = []
    return upsert("conversations", cur)


@router.get("/{cid}")
def get_conv(cid: int):
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    return conv


@router.delete("/{cid}")
def remove_conv(cid: int):
    return {"ok": delete_by("conversations", cid)}


@router.post("/{cid}/send")
async def send_message(cid: int, m: MsgIn):
    conv = get_by("conversations", cid) or HTTPException(404, "not found")
    agent = get_by("agents", m.agent_id) or HTTPException(404, "agent not found")
    canvas = get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
    mode = conv.get("mode", "independent")

    async def gen():
        async for chunk in agent_reply(cid, agent, m.content, mode, canvas):
            yield chunk

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ==============================================================
# 文件操作历史 + 还原
# ==============================================================

@router.get("/{cid}/file_ops")
def get_file_ops(cid: int, since_ts: str | None = Query(None, description="只返回 >= 该时间戳的操作（可选）")):
    """列出该会话所有文件修改操作。
    since_ts 可用来只取某条用户消息之后发生的操作。"""
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    ops = list_file_ops(cid, since_ts=since_ts)
    # 补上 agent_name 方便前端直接显示
    agents = {a["id"]: a["name"] for a in (load("agents") or [])}
    for op in ops:
        op["agent_name"] = agents.get(op.get("agent_id"), f"?{op.get('agent_id')}")
    return {"conv_id": cid, "count": len(ops), "ops": ops}


@router.post("/{cid}/file_ops/restore")
def restore_file_ops(cid: int, req: RestoreReq):
    """还原一条或多条文件操作。
    倒序执行（后发生的先还原），适合批量撤销。"""
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    return restore_range(cid, req.op_indices)


@router.post("/{cid}/file_ops/restore_one/{op_idx}")
def restore_one(cid: int, op_idx: int):
    """还原单条文件操作（更便捷的端点）。"""
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    return restore_op(cid, op_idx)


# ==============================================================
# 消息删除 / 撤销（截断消息 + 还原文件）
# ==============================================================

class TruncateMsgReq(BaseModel):
    """截断到指定消息索引（包含该索引）。index 为 -1 表示清空所有。"""
    index: int


class DeleteMsgReq(BaseModel):
    """删除单条消息（按原始 messages 索引）。"""
    index: int


class UndoFromMsgReq(BaseModel):
    """从某条消息开始撤销：删除该消息及之后的所有内容，并还原之后的所有文件操作。"""
    index: int


@router.post("/{cid}/delete_message")
def delete_message(cid: int, req: DeleteMsgReq):
    """删除单条消息（仅删除该条，保留其余消息）。
    index 为原始 messages 数组下标。

    特殊处理：若删除的是带 tool_calls 的 assistant 消息，会一并删除紧随其后的
    所有 tool 角色消息（即该轮的工具结果）。否则会留下孤立的 tool 消息：
      1) 违反 OpenAI 消息顺序契约（tool 必须紧跟在带 tool_calls 的 assistant 之后），
         下一轮调用 LLM 会返回 400；
      2) 工具结果仍含本轮调用的信息，模型能从残留 tool 结果里"还原"刚才说过/做过的事，
         表现为"删除了消息但智能体还记得自己说过什么"。
    """
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    msgs = conv.get("messages") or []
    if req.index < 0 or req.index >= len(msgs):
        raise HTTPException(400, f"index {req.index} 越界（共 {len(msgs)} 条）")

    target = msgs[req.index] or {}
    removed = 1
    del msgs[req.index]

    # 清理该 assistant 轮次后续的 tool 结果（连续的 role=tool 消息）
    if target.get("role") == "assistant" and target.get("tool_calls"):
        while req.index < len(msgs) and msgs[req.index].get("role") == "tool":
            del msgs[req.index]
            removed += 1

    conv["messages"] = msgs
    upsert("conversations", conv)
    return {"ok": True, "kept": len(msgs), "removed": removed}


@router.post("/{cid}/truncate")
def truncate_messages(cid: int, req: TruncateMsgReq):
    """截断对话消息到指定索引（包含该索引）。
    index = -1 → 清空所有消息。
    index = N → 保留 messages[0..N]，删除 messages[N+1..]"""
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    msgs = conv.get("messages") or []
    if req.index < -1 or req.index >= len(msgs):
        raise HTTPException(400, f"index {req.index} 越界（共 {len(msgs)} 条）")
    if req.index == -1:
        conv["messages"] = []
    else:
        conv["messages"] = msgs[:req.index + 1]
    upsert("conversations", conv)
    return {"ok": True, "kept": len(conv["messages"])}


@router.post("/{cid}/undo_from")
def undo_from_message(cid: int, req: UndoFromMsgReq):
    """从某条消息开始撤销：
    1. 记录该消息的 ts
    2. 找到该 ts 之后的所有文件操作
    3. 截断消息到 index-1（不包含该消息本身）
    4. 批量还原文件操作（倒序）
    返回还原结果和保留的消息数。"""
    conv = get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    msgs = conv.get("messages") or []
    if req.index < 0 or req.index >= len(msgs):
        raise HTTPException(400, f"index {req.index} 越界（共 {len(msgs)} 条）")

    target_ts = msgs[req.index].get("ts", "")

    # 1. 截断消息（删除 index 及之后所有）
    kept_msgs = msgs[:req.index]
    conv["messages"] = kept_msgs
    upsert("conversations", conv)

    # 2. 找到该 ts 之后的所有文件操作并批量还原
    all_ops = _load_conv_ops(cid)
    to_restore = [i for i, o in enumerate(all_ops) if o.get("ts", "") >= target_ts and not o.get("restored")]

    restore_results = []
    if to_restore:
        restore_results = restore_range(cid, to_restore).get("results", [])

    return {
        "ok": True,
        "kept_msgs": len(kept_msgs),
        "removed_msgs": len(msgs) - len(kept_msgs),
        "restored_ops": len(to_restore),
        "restore_results": restore_results,
    }
