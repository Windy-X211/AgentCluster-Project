import json
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Any
from core.store import load, upsert, delete_by, get_by, next_id, load_list_payloads
from core.agent_runtime import agent_reply
from core.file_backup import (
    list_file_ops, restore_op, restore_range,
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
