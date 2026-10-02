from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from core.store import load, upsert, delete_by, get_by, next_id

router = APIRouter()


class RuleIn(BaseModel):
    content: str
    canvas_id: int | None = None       # 归属画布；None=不限画布（兼容旧数据）
    agent_ids: list[int] | None = None  # None=注入该画布所有智能体；list=仅指定几个
    enabled: bool | None = None         # None=不修改


@router.get("")
def list_rules(canvas_id: int | None = None):
    """canvas_id 有值 → 只返回该画布的规则；无值 → 全部"""
    items = load("rules")
    if canvas_id is not None:
        items = [r for r in items if r.get("canvas_id") == canvas_id]
    return items


@router.post("")
def create_rule(r: RuleIn):
    item = r.model_dump()
    item["id"] = next_id("rules")
    item["created_at"] = datetime.utcnow().isoformat()
    if item.get("enabled") is None:
        item["enabled"] = True
    return upsert("rules", item)


@router.get("/{rid}")
def get_rule(rid: int):
    cur = get_by("rules", rid)
    if not cur:
        raise HTTPException(404, "not found")
    return cur


@router.put("/{rid}")
def update_rule(rid: int, r: RuleIn):
    cur = get_by("rules", rid)
    if not cur:
        raise HTTPException(404, "not found")
    d = r.model_dump()
    if d.pop("enabled", None) is None:
        d.pop("enabled", None)
        cur.setdefault("enabled", True)
    # content/agent_ids/canvas_id 按提供值覆盖（None 表示不改 content 时用 sentinel 较复杂，
    # 这里沿用整体覆盖：前端编辑会带上完整 content）
    if d.get("content") is None:
        d.pop("content", None)
    cur.update({k: v for k, v in d.items() if v is not None or k == "agent_ids"})
    # agent_ids 显式为 None 时也要写入（表示全员）
    if "agent_ids" in d:
        cur["agent_ids"] = d["agent_ids"]
    if "canvas_id" in d and d["canvas_id"] is not None:
        cur["canvas_id"] = d["canvas_id"]
    cur["id"] = rid
    return upsert("rules", cur)


@router.delete("/{rid}")
def remove_rule(rid: int):
    return {"ok": delete_by("rules", rid)}
