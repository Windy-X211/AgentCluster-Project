from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class FixedItem(BaseModel):
    key: str
    value: str = ""


class OptionalItem(BaseModel):
    key: str
    desc: str = ""


class PresetIn(BaseModel):
    target_type: str = "plugin"
    target_name: str
    label: str = ""
    note: str = ""
    enabled: bool = True
    fixed: list[FixedItem] = []
    optional: list[OptionalItem] = []


@router.get("")
def list_presets():
    from core.param_presets import list_all
    return list_all()


@router.post("")
def create_preset(p: PresetIn):
    from core.param_presets import create_preset
    return create_preset(p.model_dump())


@router.get("/{pid}")
def get_preset(pid: int):
    from core.param_presets import get_one
    it = get_one(pid)
    if not it:
        raise HTTPException(404, "not found")
    return it


@router.put("/{pid}")
def update_preset(pid: int, p: PresetIn):
    from core.param_presets import update_preset
    it = update_preset(pid, p.model_dump())
    if not it:
        raise HTTPException(404, "not found")
    return it


@router.delete("/{pid}")
def remove_preset(pid: int):
    from core.param_presets import delete_preset
    return {"ok": delete_preset(pid)}
