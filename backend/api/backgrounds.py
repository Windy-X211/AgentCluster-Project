"""背景管理 API：列出 / 读取 / 上传 / 删除界面背景。

背景存放于 AppData/backgrounds/<id>/ 下，每个背景一个子文件夹，含 manifest.json。
背景规范文档统一放在项目 ``doc/spec/background-spec.md``（唯一来源，随项目分发）。
"""
import json
import mimetypes
import os
import time
import secrets
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel, Field

import core.store as _store

router = APIRouter()


def _bg_root() -> Path:
    """背景根目录。每次读 store.ROOT，切换/迁移存储位置后自动跟随新位置。"""
    return _store.ROOT / "backgrounds"

_MIME_FIX = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# 允许的入口扩展名（防上传任意可执行文件当背景）
_ALLOWED_EXT = {
    "animation": {".html", ".htm"},
    "video": {".mp4", ".webm", ".ogv", ".mov"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"},
    "slideshow": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"},
}

_TRANSITIONS = ("fade", "none")


def _safe_id(bg_id: str) -> str:
    """校验 bg_id 合法性，防路径穿越"""
    if not bg_id or "/" in bg_id or "\\" in bg_id or bg_id in (".", "..") or bg_id.startswith("."):
        raise HTTPException(400, "非法背景 id")
    return bg_id


def _bg_dir(bg_id: str) -> Path:
    """返回背景目录（已校验存在 + 在 backgrounds 根内）"""
    _safe_id(bg_id)
    root = _bg_root()
    root_resolved = root.resolve()
    d = (root / bg_id).resolve()
    try:
        inside = d.is_relative_to(root_resolved)
    except AttributeError:
        inside = str(d).startswith(str(root_resolved))
    if not inside or not d.is_dir():
        raise HTTPException(404, "背景不存在")
    return d


@router.get("")
def list_backgrounds():
    """列出所有背景（扫描 backgrounds/ 下每个子目录的 manifest.json）"""
    result = []
    root = _bg_root()
    if not root.exists():
        return result
    for sub in sorted(root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        data.setdefault("id", sub.name)
        result.append(data)
    return result


@router.get("/{bg_id}/raw/{file_path:path}")
def bg_raw(bg_id: str, file_path: str):
    """返回背景目录下指定文件的原始字节（供 iframe/video/img 加载）"""
    d = _bg_dir(bg_id)
    # 防路径穿越：file_path 解析后必须在 d 内
    fp = (d / file_path).resolve()
    try:
        inside = fp.is_relative_to(d)
    except AttributeError:
        inside = str(fp).startswith(str(d))
    if not inside or not fp.is_file():
        raise HTTPException(404, "文件不存在")
    mime = _MIME_FIX.get(fp.suffix.lower())
    if not mime:
        guess, _ = mimetypes.guess_type(str(fp))
        mime = guess or "application/octet-stream"
    try:
        raw = fp.read_bytes()
    except Exception as e:
        raise HTTPException(500, f"读取失败：{e}")
    return Response(content=raw, media_type=mime)


@router.post("/upload")
async def upload_background(
    name: str = Form(...),
    bg_type: str = Form(..., alias="type"),
    description: str = Form(""),
    entry: str = Form(""),
    interval: float = Form(8.0),
    transition: str = Form("fade"),
    files: list[UploadFile] = File(...),
):
    """上传新背景。

    参数：
      name: 背景显示名
      type: animation / video / image / slideshow
      description: 描述（可选）
      entry: 指定入口文件名（可选）
      interval: slideshow 轮播间隔（秒，可选，默认 8）
      transition: slideshow 变换方式 fade（渐变）/ none（直接切换），默认 fade
      files: 资源文件列表
    """
    if bg_type not in ("animation", "video", "image", "slideshow"):
        raise HTTPException(400, "type 必须是 animation/video/image/slideshow")
    if bg_type == "slideshow" and transition not in _TRANSITIONS:
        raise HTTPException(400, "transition 必须是 fade/none")
    if not files:
        raise HTTPException(400, "至少上传一个文件")

    allowed = _ALLOWED_EXT[bg_type]
    # 过滤合法文件
    valid = []
    for f in files:
        fn = os.path.basename(f.filename or "")
        if not fn or fn.startswith("."):
            continue
        ext = Path(fn).suffix.lower()
        if ext not in allowed:
            # 非该类型允许的扩展名跳过（animation 的辅助 css/js 也放行）
            if bg_type == "animation" and ext in {".css", ".js", ".mjs", ".json", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}:
                pass
            else:
                continue
        valid.append((f, fn))
    if not valid:
        raise HTTPException(400, f"没有合法文件，{bg_type} 类型需要：{', '.join(allowed)}")

    # 生成 id（英文 + 时间戳，避免中文路径问题）
    bg_id = "bg_" + str(int(time.time())) + "_" + secrets.token_hex(3)
    d = _bg_root() / bg_id
    d.mkdir(parents=True, exist_ok=True)

    saved_names = []
    try:
        for f, fn in valid:
            dest = d / fn
            with open(dest, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            saved_names.append(fn)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(500, f"保存失败：{e}")

    # 确定入口
    if entry and entry in saved_names:
        entry_file = entry
    elif bg_type == "animation" and "background.html" in saved_names:
        entry_file = "background.html"
    else:
        # 优先选该类型主扩展名的文件
        main_ext = next(iter(allowed))
        main_match = next((n for n in saved_names if Path(n).suffix.lower() == main_ext), None)
        entry_file = main_match or saved_names[0]

    preview_map = {
        "animation": "radial-gradient(circle at 50% 40%, #080808 0%, #000000 70%)",
        "video": "linear-gradient(135deg, #101010 0%, #000000 100%)",
        "image": "linear-gradient(135deg, #101010 0%, #000000 100%)",
        "slideshow": "linear-gradient(135deg, #101010 0%, #000000 100%)",
    }
    if bg_type == "slideshow":
        # 照片轮播：全部图片按上传顺序进 images，第一张做入口/缩略图
        manifest = {
            "id": bg_id,
            "name": name,
            "type": "slideshow",
            "entry": saved_names[0],
            "description": description,
            "images": saved_names,
            "interval": max(1.0, float(interval)),
            "transition": transition,
            "preview": preview_map["slideshow"],
            "opacity": 0.8,
            "builtin": False,
            "thumbnail": saved_names[0],
        }
    else:
        manifest = {
            "id": bg_id,
            "name": name,
            "type": bg_type,
            "entry": entry_file,
            "description": description,
            "preview": preview_map.get(bg_type, ""),
            "opacity": 0.8 if bg_type != "animation" else 1.0,
            "builtin": False,
        }
        # 图片/视频背景用自身做缩略图
        if bg_type in ("image", "video"):
            manifest["thumbnail"] = entry_file
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


class BgSettingsIn(BaseModel):
    """背景设置更新入参（全部可选，只更新给出的字段）"""
    name: str | None = None
    interval: float | None = Field(None, ge=1, le=600)
    transition: str | None = None


@router.put("/{bg_id}/settings")
def update_bg_settings(bg_id: str, data: BgSettingsIn):
    """更新背景设置（当前用于 slideshow 的轮播间隔 / 变换方式，及显示名）"""
    d = _bg_dir(bg_id)
    mf = d / "manifest.json"
    if not mf.is_file():
        raise HTTPException(404, "manifest.json 不存在")
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(500, "manifest.json 解析失败")
    if data.name is not None and data.name.strip():
        manifest["name"] = data.name.strip()
    if data.interval is not None:
        manifest["interval"] = data.interval
    if data.transition is not None:
        if data.transition not in _TRANSITIONS:
            raise HTTPException(400, "transition 必须是 fade/none")
        manifest["transition"] = data.transition
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


@router.delete("/{bg_id}")
def delete_background(bg_id: str):
    """删除背景（内置背景不可删）"""
    d = _bg_dir(bg_id)
    mf = d / "manifest.json"
    if mf.is_file():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            if data.get("builtin"):
                raise HTTPException(403, "内置背景不可删除")
        except HTTPException:
            raise
        except Exception:
            pass
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}