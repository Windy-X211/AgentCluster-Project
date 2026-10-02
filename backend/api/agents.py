from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any
from core.store import load, save, upsert, delete_by, get_by, next_id

router = APIRouter()


class AgentIn(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    interface_id: int | None = None
    commands: list[str] = []
    skills: list[str] = []
    plugins: list[str] = []
    trigger: dict = {"type": "manual", "cron": "", "message": ""}
    tags: list[str] = []
    max_tool_rounds: int | None = None  # 单次回复最大工具调用轮数，None=跟随全局设置
    enabled: bool | None = None  # 停用/启用；None=不修改（create 时默认 True）
    perception_scope: str = "link"  # "link"（仅连线邻居感知，默认）或 "global"（感知画布全体成员）
    sort_order: int | None = None  # 列表排序权重；None=未排序，按 id 兜底排到末尾


class ReorderReq(BaseModel):
    ids: list[int]


def _agent_sort_key(a: dict):
    """已显式排序的智能体（sort_order 非 None）排在前；未排序的按 id 兜底排在后面。
    这样：旧数据（无 sort_order）保持原 id 升序；首次拖拽排序后全体获得 sort_order；
    排序之后新建的智能体（无 sort_order）自然 append 到末尾。"""
    so = a.get("sort_order")
    if so is None:
        return (1, a.get("id", 0))
    return (0, so)


@router.get("")
def list_agents():
    items = load("agents")
    items.sort(key=_agent_sort_key)
    return items


@router.post("")
def create_agent(a: AgentIn):
    item = a.model_dump()
    item["id"] = next_id("agents")
    item["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    item["enabled"] = True
    # 新建时不赋 sort_order：按 id 自然排到列表末尾，等用户拖拽后再显式排序
    item.pop("sort_order", None)
    return upsert("agents", item)


@router.post("/reorder")
def reorder_agents(r: ReorderReq):
    """按传入 ids 顺序重写每个智能体的 sort_order（0,1,2...）。
    幂等：只更新 ids 里出现的智能体，未列入的不动。"""
    for idx, aid in enumerate(r.ids):
        cur = get_by("agents", aid)
        if not cur:
            continue
        cur["sort_order"] = idx
        upsert("agents", cur)
    return {"ok": True, "count": len(r.ids)}


@router.get("/{aid}")
def get_agent(aid: int):
    return get_by("agents", aid) or HTTPException(404, "not found")


@router.put("/{aid}")
def update_agent(aid: int, a: AgentIn):
    cur = get_by("agents", aid)
    if not cur: raise HTTPException(404, "not found")
    d = a.model_dump()
    enabled_val = d.pop("enabled", None)
    if enabled_val is None:
        cur.setdefault("enabled", True)
    else:
        cur["enabled"] = enabled_val
    # sort_order 仅在显式传入时才更新，避免编辑名称/提示词时误清排序
    sort_order_val = d.pop("sort_order", None)
    if sort_order_val is not None:
        cur["sort_order"] = sort_order_val
    cur.update(d)
    cur["id"] = aid
    return upsert("agents", cur)


@router.delete("/{aid}")
def remove_agent(aid: int):
    # 同时从所有画布清理该智能体的节点与相关连线
    canvases = load("canvases")
    changed = 0
    for c in canvases:
        prev = len(c.get("nodes", []))
        c["nodes"] = [n for n in c.get("nodes", []) if n.get("agent_id") != aid]
        if len(c["nodes"]) != prev:
            changed += 1
            ids = {n["id"] for n in c["nodes"]}
            c["edges"] = [e for e in c.get("edges", [])
                          if e.get("from_node") in ids and e.get("to_node") in ids]
    if changed:
        save("canvases", canvases)
    return {"ok": delete_by("agents", aid), "cleaned_from_canvases": changed}
