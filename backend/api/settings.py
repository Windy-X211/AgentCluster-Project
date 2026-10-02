from fastapi import APIRouter
from core.store import load, save

router = APIRouter()


@router.get("")
def get_settings():
    return load("settings")


@router.put("")
def update_settings(data: dict):
    cur = load("settings")
    cur.update(data)
    save("settings", cur)
    return cur
