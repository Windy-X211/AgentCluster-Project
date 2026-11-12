from fastapi import APIRouter, HTTPException

from core import permissions

router = APIRouter()


@router.get("/pending")
def list_pending():
    """当前挂起的「手动确认」请求（前端刷新 / 断线重连后补拉弹窗用）。"""
    return {"requests": permissions.pending_list()}


@router.get("/mode")
def get_mode():
    """读取权限设置（前端权限弹窗/设置页回显；无副作用）。"""
    return permissions.get_settings()


@router.post("/confirm")
def confirm(data: dict):
    """用户对某个危险操作点了「允许」或「拒绝」。

    body：{"request_id": str, "approve": bool}
    幂等性：请求不存在（已超时 / 已被停止 / 已确认过）→ 404，前端据此关掉弹窗。
    """
    rid = str(data.get("request_id") or "").strip()
    if not rid:
        raise HTTPException(400, "缺少 request_id")
    approve = bool(data.get("approve"))
    if not permissions.resolve(rid, approve):
        raise HTTPException(404, "确认请求不存在或已超时")
    return {"ok": True, "request_id": rid, "approved": approve}
