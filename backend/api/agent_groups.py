from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.store import load, upsert, delete_by, get_by, next_id

router = APIRouter()


class AgentGroupIn(BaseModel):
    name: str
    sort_order: int | None = None  # None=未排序，按 id 兜底排到末尾


class ReorderReq(BaseModel):
    ids: list[int]


def _group_sort_key(g: dict):
    so = g.get("sort_order")
    if so is None:
        return (1, g.get("id", 0))
    return (0, so)


@router.get("")
def list_groups():
    items = load("agent_groups")
    items.sort(key=_group_sort_key)
    return items


@router.post("")
def create_group(g: AgentGroupIn):
    item = g.model_dump()
    item["id"] = next_id("agent_groups")
    item["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    # 新建组不赋 sort_order：按 id 自然排到末尾
    item.pop("sort_order", None)
    return upsert("agent_groups", item)


@router.post("/reorder")
def reorder_groups(r: ReorderReq):
    """按传入 ids 顺序重写每个组的 sort_order（0,1,2...）。幂等：只更新 ids 里出现的组。"""
    for idx, gid in enumerate(r.ids):
        cur = get_by("agent_groups", gid)
        if not cur:
            continue
        cur["sort_order"] = idx
        upsert("agent_groups", cur)
    return {"ok": True, "count": len(r.ids)}


@router.put("/{gid}")
def update_group(gid: int, g: AgentGroupIn):
    cur = get_by("agent_groups", gid)
    if not cur:
        raise HTTPException(404, "not found")
    d = g.model_dump()
    # sort_order 仅在显式传入时才更新，避免重命名时误清排序
    sort_order_val = d.pop("sort_order", None)
    if sort_order_val is not None:
        cur["sort_order"] = sort_order_val
    cur.update(d)
    cur["id"] = gid
    return upsert("agent_groups", cur)


@router.delete("/{gid}")
def remove_group(gid: int):
    # 删除组前，把该组内所有智能体的 group_id 置空（移回"全部"组）
    agents = load("agents")
    ungrouped = 0
    for a in agents:
        if a.get("group_id") == gid:
            a["group_id"] = None
            upsert("agents", a)
            ungrouped += 1
    return {"ok": delete_by("agent_groups", gid), "agents_ungrouped": ungrouped}
