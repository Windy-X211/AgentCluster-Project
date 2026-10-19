"""Office 文件（doc / docx / ppt）→ PDF 转换：调用本机 Office COM，带磁盘缓存。

依赖：Windows + 本机安装 Word / PowerPoint + 后端环境有 pywin32。
任何失败都返回 {"error": ...}，由 API 层转成 HTTP 错误，前端回退到文本预览。
"""
import hashlib
import tempfile
import threading
from pathlib import Path

# 全局锁：COM 对象串行调用，避免多线程并发拉起多个 Office 实例
_LOCK = threading.Lock()

SUPPORTED_EXTS = {"doc", "docx", "ppt"}

# 转换整体超时（秒）：防止隐藏对话框导致请求永久挂起
CONVERT_TIMEOUT = 60


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "agentcluster_office_preview"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(p: Path) -> str:
    st = p.stat()
    raw = f"{str(p).lower()}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def convert_to_pdf(src: str) -> dict:
    """把 Office 文件转成 PDF（幂等，按 路径+mtime+大小 缓存）。返回 {ok, pdf} 或 {error}。"""
    p = Path(src)
    if not p.is_file():
        return {"error": f"文件不存在：{src}"}
    ext = p.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTS:
        return {"error": f"不支持转换的类型：.{ext}"}

    out = _cache_dir() / (_cache_key(p) + ".pdf")
    if _hit(out):
        return {"ok": True, "pdf": str(out), "cached": True}

    # 锁带超时：若上一次转换被隐藏对话框卡死，不永久阻塞后续请求
    if not _LOCK.acquire(timeout=CONVERT_TIMEOUT + 5):
        return {"error": "Office 转换繁忙（可能有任务卡住），请稍后重试"}
    try:
        if _hit(out):   # 双重检查：排队期间可能已被其他请求转好
            return {"ok": True, "pdf": str(out), "cached": True}
        # 在独立线程里跑 COM：整体加超时，防止密码框/交互对话框让请求永久挂起
        box: dict = {}
        done = threading.Event()

        def _run() -> None:
            try:
                box.update(_convert_com(p, out, ext))
            except Exception as e:  # noqa: BLE001 —— COM 各类异常统一降级为 error
                box.update({"error": f"转换失败：{e}"})
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()
        if not done.wait(CONVERT_TIMEOUT):
            return {"error": f"转换超时（>{CONVERT_TIMEOUT}s），文件可能需要密码或卡在对话框"}
        return box
    finally:
        _LOCK.release()


def _hit(out: Path) -> bool:
    try:
        return out.exists() and out.stat().st_size > 0
    except OSError:
        return False


def _convert_com(p: Path, out: Path, ext: str) -> dict:
    import pythoncom
    from win32com.client import Dispatch

    pythoncom.CoInitialize()   # 每个工作线程独立初始化 COM（STA）
    app = None
    tmp = out.with_name(out.stem + ".tmp.pdf")
    try:
        tmp.unlink(missing_ok=True)
        if ext in ("doc", "docx"):
            app = Dispatch("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0   # wdAlertsNone：不弹任何确认框
            doc = app.Documents.Open(str(p), ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(tmp), FileFormat=17)   # 17 = wdFormatPDF
            finally:
                doc.Close(SaveChanges=False)
        else:  # ppt（旧版二进制，前端库只能解析 pptx）
            app = Dispatch("PowerPoint.Application")
            # Open(FileName, ReadOnly, Untitled, WithWindow)；WithWindow=False 免弹窗
            pres = app.Presentations.Open(str(p), ReadOnly=True, Untitled=False, WithWindow=False)
            try:
                pres.SaveAs(str(tmp), 32)   # 32 = ppSaveAsPDF
            finally:
                pres.Close()

        if not _hit(tmp):
            return {"error": "转换未产出 PDF（Office 可能弹出了交互对话框）"}
        tmp.replace(out)
        return {"ok": True, "pdf": str(out)}
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        tmp.unlink(missing_ok=True)   # 半截产物不留在缓存里
        pythoncom.CoUninitialize()
