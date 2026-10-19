from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from core.store import load, upsert, delete_by, get_by, next_id
from core import workspace, pyconsole, office_preview
import os, threading, asyncio, json

router = APIRouter()


class Node(BaseModel):
    id: int
    agent_id: int
    x: float = 0
    y: float = 0


class Edge(BaseModel):
    id: int
    from_node: int
    to_node: int
    type: str = "chat"  # chat | handoff


class CanvasIn(BaseModel):
    name: str
    description: str = ""
    nodes: list[Node] = []
    edges: list[Edge] = []
    workdir: str | None = Field(default=None, description="画布工作区文件夹绝对路径，画布中所有智能体的文件操作都在此目录下；null=无工作区")
    execution_mode: str = Field(default="serial", description="执行模式：serial=串行（智能体发送消息后停止运行，接收方开始运行）；parallel=并行（智能体发送消息后可继续运行，接收方并发启动）")
    sort_order: int | None = None  # 列表排序权重；None=未排序，按 id 兜底排到末尾
    ws_pin: dict | None = Field(default=None, description="工作区钉住位置（画布坐标 {x,y}），null=未钉住（浮动状态）")


class ReorderReq(BaseModel):
    ids: list[int]


class TrashReq(BaseModel):
    items: list[dict] = []   # [{"conv_id": int, "op_idx": int}]


class FileWriteReq(BaseModel):
    path: str
    content: str = ""


class FileRenameReq(BaseModel):
    path: str
    new_name: str


class FileCopyReq(BaseModel):
    src: str
    dest_dir: str


class FileDeleteReq(BaseModel):
    path: str


def _canvas_sort_key(c: dict):
    """已显式排序的画布（sort_order 非 None）排在前；未排序的按 id 兜底排在后面。
    旧数据（无 sort_order）保持原 id 升序；首次拖拽排序后全体获得 sort_order；
    排序之后新建的画布（无 sort_order）自然 append 到末尾。"""
    so = c.get("sort_order")
    if so is None:
        return (1, c.get("id", 0))
    return (0, so)


@router.get("")
def list_canvases():
    items = load("canvases")
    items.sort(key=_canvas_sort_key)
    return items


@router.post("")
def create_canvas(c: CanvasIn):
    item = c.model_dump()
    item["id"] = next_id("canvases")
    item["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    # 新建时不赋 sort_order：按 id 自然排到列表末尾，等用户拖拽后再显式排序
    item.pop("sort_order", None)
    if item.get("workdir"):
        item["workdir"] = os.path.abspath(item["workdir"])
        os.makedirs(item["workdir"], exist_ok=True)
    return upsert("canvases", item)


@router.post("/reorder")
def reorder_canvases(r: ReorderReq):
    """按传入 ids 顺序重写每个画布的 sort_order（0,1,2...）。
    幂等：只更新 ids 里出现的画布，未列入的不动。"""
    for idx, cid in enumerate(r.ids):
        cur = get_by("canvases", cid)
        if not cur:
            continue
        cur["sort_order"] = idx
        upsert("canvases", cur)
    return {"ok": True, "count": len(r.ids)}


@router.get("/{cid}")
def get_canvas(cid: int):
    return get_by("canvases", cid) or HTTPException(404, "not found")


@router.put("/{cid}")
def update_canvas(cid: int, c: CanvasIn):
    cur = get_by("canvases", cid)
    if not cur: raise HTTPException(404, "not found")
    d = c.model_dump()
    # sort_order 仅在显式传入时才更新，避免编辑名称/节点时误清排序
    sort_order_val = d.pop("sort_order", None)
    if sort_order_val is not None:
        cur["sort_order"] = sort_order_val
    cur.update(d)
    cur["id"] = cid
    if cur.get("workdir"):
        cur["workdir"] = os.path.abspath(cur["workdir"])
        os.makedirs(cur["workdir"], exist_ok=True)
    else:
        cur["workdir"] = None
    return upsert("canvases", cur)


@router.post("/{cid}/workdir")
def set_workdir(cid: int, body: dict):
    """单独设置工作区路径，自动创建目录"""
    cur = get_by("canvases", cid)
    if not cur: raise HTTPException(404, "not found")
    path = body.get("workdir") or ""
    if not path.strip():
        cur["workdir"] = None
    else:
        abs_path = os.path.abspath(path.strip())
        os.makedirs(abs_path, exist_ok=True)
        cur["workdir"] = abs_path
    upsert("canvases", cur)
    return {"workdir": cur.get("workdir"), "exists": bool(cur.get("workdir")) and os.path.isdir(cur["workdir"])}

@router.delete("/{cid}")
def remove_canvas(cid: int):
    return {"ok": delete_by("canvases", cid)}


@router.post("/browse")
def browse_folder():
    """弹出系统原生文件夹选择对话框，返回用户选中的绝对路径（取消则返回 null）。
    在独立线程里运行 tkinter，避免阻塞 FastAPI 事件循环。"""
    result = {}
    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()          # 不显示主窗口
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="选择智能体工作区文件夹", mustexist=False)
            root.destroy()
            result["path"] = folder if folder else None
        except Exception as e:
            result["error"] = str(e)
            result["path"] = None
    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=120)   # 等最多 2 分钟
    if "error" in result:
        raise HTTPException(500, f"文件夹选择失败：{result['error']}")
    return {"path": result.get("path")}


# ==============================================================
# 工作区项目树 + 回收站（快照还原）
# ==============================================================

def _require_workdir(cid: int):
    cur = get_by("canvases", cid)
    if not cur:
        raise HTTPException(404, "not found")
    return cur


@router.get("/{cid}/workspace")
def get_workspace(cid: int, rev: int = -1):
    """画布工作区项目树：以 canvas.workdir 为根，标注大小/修改时间/创建者/修改者/参与者。

    rev: 上次拿到的树版本号。与当前一致时返回 changed=false（不带 tree），
    供前端高频轮询做实时更新；rev<0 或已过期则返回全量 tree + 新 rev。
    """
    cur = _require_workdir(cid)
    wd = cur.get("workdir")
    if not wd:
        return {"ok": False, "error": "未设置工作区", "workdir": None, "tree": [], "count": 0, "rev": 0, "changed": True}
    data = workspace.build_tree(wd)
    ok = data.get("exists", False)
    cur_rev = data.get("rev", 0)
    if rev >= 0 and rev == cur_rev:
        return {"ok": ok, "workdir": wd, "error": "" if ok else "工作区目录不存在",
                "tree": [], "count": data.get("count", 0), "rev": cur_rev, "changed": False}
    return {"ok": ok, "workdir": wd, "error": "" if ok else "工作区目录不存在",
            "tree": data.get("tree", []), "count": data.get("count", 0), "rev": cur_rev, "changed": True}


@router.get("/{cid}/trash")
def get_trash(cid: int):
    """列出画布工作区内被智能体删除的文件（来源快照记录）。"""
    cur = _require_workdir(cid)
    wd = cur.get("workdir")
    items = workspace.list_trash(wd) if wd else []
    return {"ok": True, "workdir": wd, "count": len(items), "items": items}


def _trash_results(req: TrashReq, fn) -> dict:
    results = [fn(it.get("conv_id"), it.get("op_idx")) for it in (req.items or [])]
    ok = sum(1 for r in results if r and r.get("ok"))
    return {"ok": True, "count": len(results), "succeeded": ok, "results": results}


@router.post("/{cid}/trash/restore")
def restore_trash(cid: int, req: TrashReq):
    """还原回收站中的选中项（复用快照还原逻辑）。"""
    _require_workdir(cid)
    return _trash_results(req, workspace.restore_trash)


@router.post("/{cid}/trash/purge")
def purge_trash(cid: int, req: TrashReq):
    """彻底删除回收站中的选中项（移除快照备份，之后不可还原）。"""
    _require_workdir(cid)
    return _trash_results(req, workspace.purge_trash)


# ==============================================================
# 工作区内文件操作（双击编辑 / 右键菜单：复制粘贴重命名删除进回收站）
# ==============================================================

def _require_wd(cid: int) -> str:
    cur = _require_workdir(cid)
    wd = cur.get("workdir")
    if not wd:
        raise HTTPException(400, "未设置工作区")
    return wd


@router.get("/{cid}/file")
def read_canvas_file(cid: int, path: str = ""):
    """读取工作区内文本文件（供工作区面板双击编辑）。"""
    wd = _require_wd(cid)
    r = workspace.read_file(wd, path)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


# 内置查看器可直接返回的二进制类型（白名单：非清单内的扩展名一律拒绝）
_RAW_EXT_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "svg": "image/svg+xml", "ico": "image/x-icon", "avif": "image/avif",
    "apng": "image/apng",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    "csv": "text/csv; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "pdf": "application/pdf",
}
_RAW_MAX_BYTES = 50 * 1024 * 1024


@router.get("/{cid}/file/raw")
def raw_canvas_file(cid: int, path: str = "", v: int = 0):
    """返回工作区内二进制文件原始字节（供内置查看器渲染图片/Office/PDF）。

    仅支持 _RAW_EXT_MIME 白名单扩展名，≤50MB；路径经 _resolve_under 防穿越。
    """
    wd = _require_wd(cid)
    p = workspace._resolve_under(wd, path)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    ext = p.suffix.lower().lstrip(".")
    mime = _RAW_EXT_MIME.get(ext)
    if not mime:
        raise HTTPException(415, f"不支持的文件类型：.{ext}")
    if p.stat().st_size > _RAW_MAX_BYTES:
        raise HTTPException(413, "文件过大（>50MB），无法在线预览")
    return FileResponse(p, media_type=mime)


@router.get("/{cid}/file/office-pdf")
def office_pdf(cid: int, path: str = ""):
    """doc/docx/ppt 经本机 Office（COM）转成 PDF，供内置查看器保真渲染（带缓存）。"""
    wd = _require_wd(cid)
    p = workspace._resolve_under(wd, path)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    if p.suffix.lower().lstrip(".") not in office_preview.SUPPORTED_EXTS:
        raise HTTPException(415, "仅支持 doc / docx / ppt 转换")
    r = office_preview.convert_to_pdf(str(p))
    if "error" in r:
        raise HTTPException(400, r["error"])
    return FileResponse(r["pdf"], media_type="application/pdf")


@router.put("/{cid}/file")
def write_canvas_file(cid: int, req: FileWriteReq):
    """写工作区内文本文件（编辑器保存）。"""
    wd = _require_wd(cid)
    r = workspace.write_file(wd, req.path, req.content)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/rename")
def rename_canvas_file(cid: int, req: FileRenameReq):
    """重命名工作区内文件/目录。"""
    wd = _require_wd(cid)
    r = workspace.rename_file(wd, req.path, req.new_name)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/copy")
def copy_canvas_file(cid: int, req: FileCopyReq):
    """把文件/目录复制到目标目录（同名自动加副本后缀）。"""
    wd = _require_wd(cid)
    r = workspace.copy_file(wd, req.src, req.dest_dir)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/delete")
def delete_canvas_file(cid: int, req: FileDeleteReq):
    """删除工作区内文件/目录并进入回收站（快照记录为"用户（手动）"删除）。"""
    wd = _require_wd(cid)
    r = workspace.delete_to_trash(wd, req.path)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/run")
def run_canvas_file(cid: int, req: FileDeleteReq):
    """在工作区内"运行"一个文件（双击 / 右键"运行"）。

    - .py → 启动 Python 控制台会话：返回 session_id，前端接入控制台浮窗（输入输出 / 停止 / 运行）；
    - 其他 → 用系统默认程序打开（等价资源管理器双击）。
    路径先经 _resolve_under 校验，确保落在工作区内（防路径穿越）。
    """
    wd = _require_wd(cid)
    p = workspace._resolve_under(wd, req.path)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    if p.suffix.lower().lstrip(".") == "py":
        r = pyconsole.start(str(p), cwd=wd)
        if "error" in r:
            raise HTTPException(400, r["error"])
        return {"kind": "console", **r}
    r = workspace.open_with_system(str(p))
    if "error" in r:
        raise HTTPException(400, r["error"])
    return {"kind": "open", **r}


class PyInputReq(BaseModel):
    text: str = ""


class PyStopReq(BaseModel):
    force: bool = False


@router.post("/{cid}/file/pyconsole/{sid}/input")
def pyconsole_input(cid: int, sid: str, req: PyInputReq):
    """向 Python 控制台子进程的 stdin 写入文本（模拟键盘输入）。"""
    _require_wd(cid)
    r = pyconsole.write(sid, req.text)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/pyconsole/{sid}/stop")
def pyconsole_stop(cid: int, sid: str, req: PyStopReq = PyStopReq()):
    """停止 Python 控制台子进程（含其子进程树）。"""
    _require_wd(cid)
    r = pyconsole.stop(sid, force=req.force)
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@router.post("/{cid}/file/pyconsole/{sid}/close")
def pyconsole_close(cid: int, sid: str):
    """关闭控制台会话：停掉子进程并清理缓冲区。"""
    _require_wd(cid)
    return pyconsole.close(sid)


@router.get("/{cid}/file/pyconsole/{sid}/stream")
async def pyconsole_stream(cid: int, sid: str):
    """SSE 长连接：持续推送控制台输出（type=out）与结束事件（type=exit / closed）。"""
    _require_wd(cid)

    def _frame(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    async def gen():
        while True:
            r = pyconsole.drain(sid)
            if "error" in r:
                yield _frame({"type": "closed", "error": r["error"]})
                return
            if r["text"]:
                yield _frame({"type": "out", "text": r["text"]})
            if r["running"]:
                await asyncio.sleep(0.08)
                continue
            # 进程已退出：管道 EOF 需要一点时间，再收尾拉取残留输出
            for _ in range(15):
                d = pyconsole.drain(sid)
                if not d.get("text"):
                    break
                yield _frame({"type": "out", "text": d["text"]})
                await asyncio.sleep(0.07)
            yield _frame({"type": "exit", "exit_code": r["exit_code"]})
            return

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")
