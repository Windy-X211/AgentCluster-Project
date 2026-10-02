from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.store import load, upsert, delete_by, get_by, next_id

router = APIRouter()


class IfaceIn(BaseModel):
    name: str
    url: str
    api_key: str = ""
    default_model: str = "gpt-4o-mini"
    models: list[str] = []
    description: str = ""


@router.get("")
def list_interfaces():
    return load("model_interfaces")


@router.post("")
def create_iface(i: IfaceIn):
    item = i.model_dump()
    item["id"] = next_id("model_interfaces")
    return upsert("model_interfaces", item)


@router.get("/{iid}")
def get_iface(iid: int):
    return get_by("model_interfaces", iid) or HTTPException(404, "not found")


@router.put("/{iid}")
def update_iface(iid: int, i: IfaceIn):
    cur = get_by("model_interfaces", iid)
    if not cur: raise HTTPException(404, "not found")
    cur.update(i.model_dump())
    cur["id"] = iid
    return upsert("model_interfaces", cur)


@router.delete("/{iid}")
def remove_iface(iid: int):
    return {"ok": delete_by("model_interfaces", iid)}
