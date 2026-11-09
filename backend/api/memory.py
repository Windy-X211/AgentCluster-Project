"""记忆管理 API：能力管理 → 记忆管理 Tab 使用。

记忆条目**所有智能体共用一份**（memories/shared.json），模式（off/manual/summary）
仍按智能体配置，决定各智能体如何参与共享记忆。

路由顺序注意：静态路径（overview / shared / batch / global / entries / summary / clear）
必须注册在 /{agent_id} 之前，否则会被 /{agent_id} 的 int 解析抢先匹配返回 422。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import memory as mem
from core.store import load, save as store_save, upsert, get_by

router = APIRouter()


class EntryIn(BaseModel):
    content: str
    pinned: bool = False


class EntryUpdate(BaseModel):
    content: str | None = None
    pinned: bool | None = None


class SummaryIn(BaseModel):
    content: str = ""


class ModeIn(BaseModel):
    mode: str  # off / manual / summary


class BatchIn(BaseModel):
    agent_ids: list[int]
    mode: str  # off / manual / summary


class GlobalIn(BaseModel):
    enabled: bool


class ClearIn(BaseModel):
    keep_pinned: bool = True


def _agent_or_404(agent_id: int) -> dict:
    a = get_by("agents", agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return a


@router.get("/overview")
def overview():
    """所有智能体的记忆模式概况（批量管理列表用；记忆内容本身共享，见 /shared）。"""
    g_enabled = mem.memory_enabled_global()
    agents = load("agents")
    out = []
    for a in agents:
        aid = a.get("id")
        out.append({
            "agent_id": aid,
            "name": a.get("name") or f"智能体#{aid}",
            "avatar_color": a.get("avatar_color"),
            "configured_mode": a.get("memory_mode") or "off",   # 用户配置值
            "mode": mem.memory_mode_of(a),                       # 实际生效（含全局开关）
            "global_enabled": g_enabled,
        })
    out.sort(key=lambda x: x["agent_id"] or 0)
    return out


@router.get("/shared")
def shared_detail():
    """共享记忆详情（查看区展示：固定条目 + 摘要 + 其他条目）。"""
    return mem.read_memory()


@router.post("/entries")
def add_entry(e: EntryIn):
    """向共享记忆添加条目（管理界面编辑用）。"""
    entry = mem.add_entry(e.content, pinned=e.pinned, source="manual")
    if not entry:
        raise HTTPException(400, "记忆内容为空")
    return entry


@router.put("/entries/{entry_id}")
def update_entry(entry_id: str, e: EntryUpdate):
    r = mem.update_entry(entry_id, content=e.content, pinned=e.pinned)
    if not r:
        raise HTTPException(404, "entry not found")
    return r


@router.delete("/entries/{entry_id}")
def remove_entry(entry_id: str):
    if not mem.delete_entry(entry_id):
        raise HTTPException(404, "entry not found")
    return {"ok": True}


@router.put("/summary")
def set_summary(s: SummaryIn):
    """直接编辑共享滚动摘要（管理界面用，不影响固定记忆）。"""
    mem.set_summary_text(s.content)
    return {"ok": True}


@router.post("/clear")
def clear(c: ClearIn):
    """清空共享记忆（keep_pinned=True 保留固定记忆，默认；摘要一并清空）。"""
    mem.clear_memory(keep_pinned=c.keep_pinned)
    return {"ok": True}


@router.post("/batch")
def batch(b: BatchIn):
    """批量设置智能体的记忆模式（off=批量关闭记忆技能）。"""
    if b.mode not in mem.MODES:
        raise HTTPException(400, "mode 须为 off / manual / summary")
    n = 0
    for aid in b.agent_ids:
        a = get_by("agents", aid)
        if not a:
            continue
        a["memory_mode"] = b.mode
        upsert("agents", a)
        n += 1
    return {"ok": True, "updated": n, "mode": b.mode}


@router.put("/global")
def set_global(g: GlobalIn):
    """记忆功能全局总开关（settings.memory_enabled）。"""
    st = load("settings") or {}
    st["memory_enabled"] = bool(g.enabled)
    store_save("settings", st)
    return {"ok": True, "enabled": bool(g.enabled)}


@router.put("/{agent_id}/mode")
def set_mode(agent_id: int, m: ModeIn):
    """设置单个智能体的记忆模式（off=关闭记忆技能）。"""
    a = _agent_or_404(agent_id)
    if m.mode not in mem.MODES:
        raise HTTPException(400, "mode 须为 off / manual / summary")
    a["memory_mode"] = m.mode
    upsert("agents", a)
    return {"ok": True, "mode": m.mode}