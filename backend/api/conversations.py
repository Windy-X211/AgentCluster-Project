import json
import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Any
from core.store import load, upsert, delete_by, get_by, next_id, load_list_payloads
from core import run_registry
from core.file_backup import (
    list_file_ops, restore_op, restore_range, _load_conv_ops,
)

router = APIRouter()


class ConvIn(BaseModel):
    name: str = "新对话"
    mode: str = "independent"  # independent | linked（历史字段，保留兼容）
    agent_ids: list[int] = []
    canvas_id: int | None = None
    workdir: str | None = None  # 会话级工作区（切换对话时前端同步回画布）
    execution_mode: str | None = None  # 会话级串并行模式 serial|parallel（同 workdir 语义）


class MsgIn(BaseModel):
    agent_id: int
    content: str


class StopIn(BaseModel):
    agent_id: int | None = None  # 给定则只停该智能体；None 停整个会话


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


def _is_preview_worthy(msg: dict) -> bool:
    """判断消息是否适合做"会话标签预览"——跳过协议/工具噪音。

    跳过：
      - role='a2a'（A2A 协议层控制消息）
      - role='tool'（工具调用结果，内容通常是 JSON 或错误栈）
      - role='user' 但内容以【A2A 协议消息】开头（集群内转发的协议消息）
      - content 是纯 JSON/数组（dict/list 类型或 { / [ 开头的字符串）
    只保留真正的 user / assistant 自然语言对话。
    """
    role = msg.get("role")
    if role == "a2a" or role == "tool":
        return False
    content = msg.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return False
    stripped = content.lstrip()
    # A2A 协议转发消息（集群内 agent-to-agent 的信封消息，不是真人发的）
    if stripped.startswith("【A2A 协议消息】"):
        return False
    # 纯 JSON / 数组内容（工具返回值）
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    return True


def _slim_payload(payload_bytes: bytes) -> bytes:
    """从完整会话 JSON bytes 中剥离 messages 大数组，保留元数据 + 摘要字段。

    直接操作 json.loads/dumps（比正则安全、足够快）。输出与 FastAPI
    JSONResponse 序列化格式一致（compact separators, ensure_ascii=False）。
    """
    try:
        obj = json.loads(payload_bytes)
        msgs = obj.pop("messages", None) or []
        obj["message_count"] = len(msgs)
        if msgs:
            # 从后往前找第一条适合做标签预览的消息（跳过 a2a/tool/协议转发）
            preview_msg = None
            last_ts = None
            for m in reversed(msgs):
                if last_ts is None:
                    last_ts = m.get("ts")
                if _is_preview_worthy(m):
                    preview_msg = m
                    break
            # 如果全是协议消息，兜底用最后一条（至少有内容）
            if preview_msg is None:
                preview_msg = msgs[-1]

            obj["last_role"] = preview_msg.get("role")
            obj["last_agent_id"] = preview_msg.get("agent_id")
            obj["last_ts"] = last_ts or preview_msg.get("ts")
            content = preview_msg.get("content") or ""
            if isinstance(content, str):
                obj["last_preview"] = content[:80].replace("\n", " ")
            else:
                obj["last_preview"] = ""
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception:
        return payload_bytes  # 解析失败就原样返回


@router.get("")
def list_convs():
    """上新下旧：按最后一条消息时间（或创建时间）降序。

    ⚠️ 性能优化：**剥离 messages 大数组**，只返回列表视图需要的元数据。
    前端点击某个会话时再用 GET /conversations/{cid} 按需加载完整消息历史。

    走 load_list_payloads 的热缓存（文件 mtime + size 命中时跳过读盘/解析）：
    冷启动 ~800ms（首次解析 223 个 JSON），热缓存后 ~60ms + slim ~240ms → 总计 300ms。
    相比原实现（每次 load() 500ms + dumps 300ms = 800ms），热缓存快 2.7x。"""
    entries = load_list_payloads("conversations", _conv_activity_key)
    entries.sort(key=lambda e: e[0], reverse=True)
    # 对每个 payload 剥离 messages + 提取摘要字段
    slimmed = [_slim_payload(payload) for _, payload in entries]
    body = b"[" + b",".join(slimmed) + b"]"
    return Response(content=body, media_type="application/json")


@router.post("")
def create_conv(c: ConvIn):
    item = c.model_dump()
    item["id"] = next_id("conversations")
    item["messages"] = []
    item["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    return upsert("conversations", item)


@router.get("/runs")
def list_runs():
    """所有正在运行的会话（跨窗口恢复用：Electron / 浏览器任一前端挂载时查询）。
    注意：必须注册在 GET /{cid} 之前，否则 "runs" 会被当作 cid 匹配。"""
    return {"running": run_registry.list_running()}


@router.put("/{cid}")
def update_conv(cid: int, data: dict):
    """更新会话。
    - 只更新元信息（mode / canvas_id / name / agent_ids / workdir / execution_mode）。
    - 落库单通道化：messages 一律由后端写入（send 端点落用户消息 / agent_reply 落产出），
      即使 data 里带了 messages 字段也忽略——前端整段替换会与后端 append 竞态双写。
    - data 含 reset_messages=True 时清空对话历史（不删除会话本身）。"""
    cur = run_registry.peek_live_conv(cid) or get_by("conversations", cid)
    if not cur:
        raise HTTPException(404, "not found")
    for k in ("mode", "canvas_id", "name", "agent_ids", "workdir", "execution_mode"):
        if k in data:
            cur[k] = data[k]
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
    conv = run_registry.peek_live_conv(cid) or get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "not found")
    agent = get_by("agents", m.agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    # 防重入按「会话 + 智能体」：同一会话其它智能体可并发运行
    if run_registry.is_running(cid, m.agent_id):
        raise HTTPException(409, "该智能体正在运行中")
    canvas = get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
    # 会话级串并行模式：会话记录优先覆盖画布当前设置（与 workdir 同语义）
    if canvas is not None and conv.get("execution_mode"):
        canvas = {**canvas, "execution_mode": conv["execution_mode"]}
    # 统一为「新建会话」语义：绑了画布 → 集群上下文（cluster 工具/集群提示）；未绑 → 纯对话
    mode = "linked" if conv.get("canvas_id") else "independent"

    # —— 落库单通道化：用户消息由 send 端点落库（messages 只由后端写）——
    # 先于快照 append：base_conv 即含用户消息，重开窗口 events 重放恢复时完整可见；
    # 也消除旧实现里前端 send 前 PUT 整段替换与后端 append 的双写竞态。
    conv.setdefault("messages", []).append({
        "role": "user", "content": m.content, "ts": datetime.utcnow().isoformat(),
        "to_agent_id": m.agent_id,
    })
    upsert("conversations", conv)

    # 运行与连接解耦：agent_reply 在后台任务里跑（事件缓冲 + 广播），
    # 本响应只是"订阅视图"——客户端断开（关窗/断网/刷新）不再中止运行，
    # 重开窗口经 GET /{cid}/running + GET /{cid}/events 恢复显示。
    session = run_registry.start(cid, agent, m.content, mode, canvas, base_conv=conv,
                                 workdir=conv.get("workdir"))

    async def gen():
        try:
            async for line in session.subscribe(0):
                yield line
        except asyncio.CancelledError:
            # 客户端断开只结束本次订阅，后台运行继续
            raise

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@router.get("/{cid}/running")
def get_running(cid: int, include_base: bool = Query(False, description="附带 send 时刻的会话基底快照（重连恢复用）")):
    """查询该会话的运行：running=bool + runs[]（每个 (会话,智能体) 运行一条）。
    include_base=1 时每条附带基底消息（取最早开始的运行做重置基底，最保守公共前缀）。"""
    runs = run_registry.runs_of(cid)
    running = [s.describe(include_base=include_base) for s in runs if s.running]
    if not running:
        return {"running": False, "runs": [], "conv_id": cid}
    return {"running": True, "runs": running, "conv_id": cid}


@router.get("/{cid}/events")
async def stream_events(cid: int,
                        since: int = Query(0, description="从该序号之后重放（恢复场景固定 since=0 全量重放）"),
                        agent_id: int | None = Query(None, description="只订阅该智能体的运行；缺省取最早开始的运行")):
    """订阅该会话某个运行的事件流：先重放缓冲，再实时跟随，直到 DONE 结束。"""
    s = run_registry.get(cid, agent_id)
    if not s:
        raise HTTPException(404, "没有可恢复的运行（已结束且缓冲已过期）")

    async def gen():
        try:
            async for line in s.subscribe(since):
                yield line
        except asyncio.CancelledError:
            raise

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@router.post("/{cid}/stop")
def stop_run(cid: int, body: StopIn | None = None):
    """显式停止该会话的后台运行。
    body.agent_id 给定 → 只停该智能体（对话区停止按钮）；
    缺省/None → 停掉整个会话全部智能体（画布停止按钮）。"""
    agent_id = body.agent_id if body else None
    return {"ok": run_registry.stop(cid, agent_id)}


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
