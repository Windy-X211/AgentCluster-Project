 """cad_drawing 插件 —— 连接 CAD 进行制图（AutoCAD / 中望CAD / 浩辰CAD / BricsCAD）

双模式架构：
    1) COM 实时模式：本机 CAD 正在运行时，通过 COM 接口直接在打开的图纸里绘图，
       每次绘制后自动 ZoomExtents，可直接 save / saveAs。
    2) DXF 文件模式：没有 CAD 运行时自动降级，用 ezdxf 在内存中构建图纸，
       绘制期间自动保存为 DXF 文件，可用任意 CAD 软件打开。

调用签名：
    run(action="status")
    run(action="connect", what="auto", launch=False)
    run(action="line", x1=0, y1=0, x2=100, y2=0, layer="", color=0)
    run(action="polyline", points=[[0,0],[100,0],[100,50]], closed=True, ...)
    run(action="circle", cx=50, cy=50, r=25, ...)
    run(action="arc", cx=50, cy=50, r=25, startAngle=0, endAngle=90, ...)
    run(action="ellipse", cx=0, cy=0, majorX=50, majorY=0, ratio=0.5, ...)
    run(action="rectangle", x=0, y=0, width=100, height=50, ...)
    run(action="polygon", cx=0, cy=0, r=40, sides=6, ...)
    run(action="text", text="Hello", x=0, y=0, height=3.5, rotation=0, ...)
    run(action="dimension", x1=0, y1=0, x2=100, y2=0, offset=8, ...)
    run(action="erase", handle="2A1") 或 run(action="erase", last=True)
    run(action="listLayers") / run(action="createLayer", name="中心线", color=1)
    run(action="readDrawing", what="all", limit=200)
    run(action="save") / run(action="saveAs", filePath="D:/part.dwg")
    run(action="exportDxf", filePath="D:/part.dxf")
    run(action="template", what="flange", params={"outerD": 100, ...}, baseX=0, baseY=0)

返回 {ok, ...}；失败返回 {ok: False, error}。
"""
from __future__ import annotations

import json as _json
import math
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _detect_root() -> Path:
    """推导输出根目录：
    - <项目根>/market/plugins/<name>/tool.py → 项目根
    - <数据根>/plugins/<name>/tool.py      → 数据根（AgentCluster）
    - 其他位置 → 插件目录本身
    """
    d = Path(__file__).resolve().parent
    if d.parent.name == "plugins":
        base = d.parent.parent
        if base.name == "market":
            return base.parent
        return base
    return d


_PROJECT_ROOT: Path = _detect_root()
_PLUGIN_DIR: Path = Path(__file__).resolve().parent

DEFAULT_OUTPUT_DIR = "outputs/cad_drawing"

# CAD COM ProgID 注册表（按优先级）
_CAD_APPS: list[tuple[str, str, str]] = [
    ("autocad", "AutoCAD.Application", "AutoCAD"),
    ("zwcad", "ZWCAD.Application", "中望CAD"),
    ("gcad", "Gcad.Application", "浩辰CAD"),
    ("bricscad", "BricscadApp.AcadApplication", "BricsCAD"),
]

# 画图分层约定
L_OUTLINE = "轮廓"      # ACI 7
L_CENTER = "中心线"     # ACI 1
L_DIM = "标注"          # ACI 3
L_TEXT = "文字"         # ACI 4

# 会话状态：mode=none|com|dxf；DXF 模式持有内存文档；handles 记录本次会话创建的实体
_SESSION: dict[str, Any] = {
    "mode": "none",
    "progKey": "",
    "dxfDoc": None,
    "dxfPath": "",
    "handles": [],
}


# ==============================================================
# 依赖懒加载
# ==============================================================
def _get_ezdxf():
    try:
        import ezdxf
        return ezdxf
    except ImportError as e:
        raise RuntimeError(
            "缺少 ezdxf 依赖（DXF 生成需要）。请在插件面板点「重新扫描/安装依赖」，"
            "或手动执行：pip install ezdxf"
        ) from e


def _get_win32():
    try:
        import pythoncom
        import win32com.client
        return win32com.client, pythoncom
    except ImportError as e:
        raise RuntimeError(
            "缺少 pywin32 依赖（COM 连接 CAD 需要）。请执行：pip install pywin32"
        ) from e


# ==============================================================
# 通用辅助
# ==============================================================
def _load_config() -> dict[str, Any]:
    cfg_fp = _PLUGIN_DIR / "config.json"
    if cfg_fp.exists():
        try:
            return _json.loads(cfg_fp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _resolve_save_dir(kwargs: dict) -> Path:
    cfg = _load_config()
    d = str(kwargs.get("saveDir") or cfg.get("output_dir") or DEFAULT_OUTPUT_DIR)
    p = Path(d)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _fnum(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get2(kwargs: dict, *names: str, default: float = 0.0) -> float:
    """按 camelCase 名称序列取第一个存在的参数并转 float"""
    for n in names:
        if n in kwargs and kwargs[n] is not None:
            return _fnum(kwargs[n], default)
    return default


def _get_pts(kwargs: dict, key: str = "points") -> list[tuple[float, float]]:
    """points 参数容错解析：支持 [[x,y],...] / [{"x":..,"y":..},...] / "0,0;100,0" 字符串"""
    v = kwargs.get(key)
    if v is None:
        raise ValueError(f"缺少参数 {key}")
    if isinstance(v, str):
        pts = []
        for seg in v.replace("，", ",").split(";"):
            seg = seg.strip()
            if not seg:
                continue
            xy = seg.split(",")
            if len(xy) < 2:
                raise ValueError(f"{key} 格式错误：{seg!r}，应为 x,y")
            pts.append((float(xy[0]), float(xy[1])))
        return pts
    out: list[tuple[float, float]] = []
    for p in v:
        if isinstance(p, dict):
            out.append((_fnum(p.get("x")), _fnum(p.get("y"))))
        else:
            seq = list(p)
            if len(seq) < 2:
                raise ValueError(f"{key} 每项至少需要 x,y 两个数")
            out.append((_fnum(seq[0]), _fnum(seq[1])))
    if len(out) < 2:
        raise ValueError(f"{key} 至少需要 2 个点")
    return out


def _get_layer_color(kwargs: dict) -> tuple[str, int]:
    layer = str(kwargs.get("layer") or "").strip()
    color = int(_fnum(kwargs.get("color"), 0))
    return layer, color


# ==============================================================
# COM 后端（实时操控正在运行的 CAD）
# ==============================================================
class ComBackend:
    """COM 后端：不持久化 COM 对象（后端线程池线程不固定，跨线程访问 COM 会失败）。
    每个操作前重新 GetActiveObject 拿 app/doc，开销小且线程安全。"""

    mode = "com"

    def __init__(self, prog_id: str):
        self.prog_id = prog_id
        self._win32, self._pythoncom = _get_win32()
        self.app = None
        self.doc = None
        self._attach()

    def _attach(self) -> None:
        try:
            self._pythoncom.CoInitialize()
        except Exception:
            pass
        self.app = self._win32.GetActiveObject(self.prog_id)
        self.doc = self.app.ActiveDocument
        if self.doc is None:
            raise RuntimeError("CAD 已连接但没有打开的图纸（ActiveDocument 为空），请先在 CAD 中新建一张图纸")

    # ---- 基础封装 ----
    def _pt(self, x: float, y: float, z: float = 0.0):
        return self._win32.VARIANT(
            self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8,
            (float(x), float(y), float(z)),
        )

    def _flat(self, pts: list[tuple[float, float]]):
        flat: list[float] = []
        for x, y in pts:
            flat += [float(x), float(y)]
        return self._win32.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8, flat)

    def _attrs(self, ent: Any, layer: str, color: int) -> None:
        if layer:
            try:
                ent.Layer = layer
            except Exception:
                pass
        if color:
            try:
                ent.color = int(color)
            except Exception:
                pass

    def _track(self, ent: Any) -> str:
        try:
            h = str(ent.Handle)
        except Exception:
            h = ""
        if h:
            _SESSION["handles"].append(h)
        return h

    def _ensure_layer(self, name: str, color: int) -> None:
        try:
            lyr = self.doc.Layers.Add(name)
            if color:
                lyr.color = int(color)
        except Exception:
            pass

    # ---- 绘图指令 ----
    def line(self, x1: float, y1: float, x2: float, y2: float, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddLine(self._pt(x1, y1), self._pt(x2, y2))
        self._attrs(ent, layer, color)
        return self._track(ent)

    def polyline(self, pts: list[tuple[float, float]], closed: bool = False, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddLightWeightPolyline(self._flat(pts))
        if closed:
            ent.Closed = True
        self._attrs(ent, layer, color)
        return self._track(ent)

    def circle(self, cx: float, cy: float, r: float, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddCircle(self._pt(cx, cy), float(r))
        self._attrs(ent, layer, color)
        return self._track(ent)

    def arc(self, cx: float, cy: float, r: float, start_deg: float, end_deg: float, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddArc(
            self._pt(cx, cy), float(r), math.radians(start_deg), math.radians(end_deg)
        )
        self._attrs(ent, layer, color)
        return self._track(ent)

    def ellipse(self, cx: float, cy: float, mx: float, my: float, ratio: float, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddEllipse(self._pt(cx, cy), self._pt(mx, my), float(ratio))
        self._attrs(ent, layer, color)
        return self._track(ent)

    def text(self, s: str, x: float, y: float, height: float, rotation_deg: float, layer: str = "", color: int = 0) -> str:
        ent = self.doc.ModelSpace.AddText(str(s), self._pt(x, y), float(height))
        if rotation_deg:
            ent.Rotation = math.radians(rotation_deg)
        self._attrs(ent, layer, color)
        return self._track(ent)

    def dimension(self, x1: float, y1: float, x2: float, y2: float, offset: float, layer: str = "", color: int = 0) -> str:
        bx, by = _dim_base(x1, y1, x2, y2, offset)
        ent = self.doc.ModelSpace.AddDimAligned(self._pt(x1, y1), self._pt(x2, y2), self._pt(bx, by))
        self._attrs(ent, layer, color)
        return self._track(ent)

    # ---- 图层 / 读取 / 文件 ----
    def list_layers(self) -> list[dict[str, Any]]:
        out = []
        for i in range(self.doc.Layers.Count):
            lyr = self.doc.Layers.Item(i)
            out.append({
                "name": lyr.Name,
                "color": int(lyr.color),
                "on": bool(lyr.LayerOn),
                "frozen": bool(lyr.Freeze),
            })
        return out

    def create_layer(self, name: str, color: int) -> str:
        lyr = self.doc.Layers.Add(name)
        if color:
            lyr.color = int(color)
        return name

    def read_drawing(self, what: str, limit: int) -> dict[str, Any]:
        counts: dict[str, int] = {}
        texts: list[str] = []
        total = int(self.doc.ModelSpace.Count)
        scanned = min(total, max(limit, 50))
        for i in range(scanned):
            ent = self.doc.ModelSpace.Item(i)
            name = str(ent.ObjectName)
            counts[name] = counts.get(name, 0) + 1
            if name in ("AcDbText", "AcDbMText") and len(texts) < 50:
                try:
                    t = str(ent.TextString).strip()
                    if t:
                        texts.append(t)
                except Exception:
                    pass
        return {"entityTotal": total, "scanned": scanned, "entityCounts": counts, "texts": texts,
                "layers": self.list_layers()}

    def save(self) -> str:
        self.doc.Save()
        try:
            return str(self.doc.FullName)
        except Exception:
            return ""

    def save_as(self, path: Path) -> str:
        self.doc.SaveAs(str(path))
        return str(path)

    def export_dxf(self, path: Path) -> str:
        # AcSaveAsType 枚举随版本不同，逐个尝试常见 DXF 格式值
        for fmt in (64, 52, 40, 28, 13, 1):
            try:
                self.doc.SaveAs(str(path), fmt)
                return str(path)
            except Exception:
                continue
        raise RuntimeError("DXF 导出失败（SaveAs 各格式枚举均被拒绝），请在 CAD 中手动「另存为 DXF」")

    def erase(self, handle: str) -> None:
        ent = self.doc.HandleToObject(handle)
        ent.Delete()

    def after_draw(self) -> None:
        try:
            self.app.ZoomExtents()
        except Exception:
            pass


# ==============================================================
# DXF 后端（ezdxf 内存图纸 → 文件）
# ==============================================================
class DxfBackend:
    mode = "dxf"

    def __init__(self, dxf_path: str = ""):
        ezdxf = _get_ezdxf()
        self.doc = ezdxf.new("R2010", setup=True)
        self.msp = self.doc.modelspace()
        self.dxf_path = dxf_path
        self._dirty = False

    def _attrs(self, layer: str, color: int) -> dict[str, Any]:
        a: dict[str, Any] = {}
        if layer:
            # 引用的图层不存在时自动创建，避免实体挂在未定义图层上
            try:
                if layer not in self.doc.layers:
                    self.doc.layers.add(layer, color=int(color or 7))
            except Exception:
                pass
            a["layer"] = layer
        if color:
            a["color"] = int(color)
        return a

    def _track(self, ent: Any) -> str:
        h = str(ent.dxf.handle)
        _SESSION["handles"].append(h)
        return h

    def _ensure_layer(self, name: str, color: int) -> None:
        try:
            if name not in self.doc.layers:
                self.doc.layers.add(name, color=int(color or 7))
            elif color:
                self.doc.layers.get(name).color = int(color)
        except Exception:
            pass

    # ---- 绘图指令 ----
    def line(self, x1: float, y1: float, x2: float, y2: float, layer: str = "", color: int = 0) -> str:
        e = self.msp.add_line((x1, y1), (x2, y2), dxfattribs=self._attrs(layer, color))
        self._dirty = True
        return self._track(e)

    def polyline(self, pts: list[tuple[float, float]], closed: bool = False, layer: str = "", color: int = 0) -> str:
        e = self.msp.add_lwpolyline([(x, y) for x, y in pts], dxfattribs=self._attrs(layer, color))
        if closed:
            e.closed = True
        self._dirty = True
        return self._track(e)

    def circle(self, cx: float, cy: float, r: float, layer: str = "", color: int = 0) -> str:
        e = self.msp.add_circle((cx, cy), float(r), dxfattribs=self._attrs(layer, color))
        self._dirty = True
        return self._track(e)

    def arc(self, cx: float, cy: float, r: float, start_deg: float, end_deg: float, layer: str = "", color: int = 0) -> str:
        e = self.msp.add_arc((cx, cy), float(r), float(start_deg), float(end_deg), dxfattribs=self._attrs(layer, color))
        self._dirty = True
        return self._track(e)

    def ellipse(self, cx: float, cy: float, mx: float, my: float, ratio: float, layer: str = "", color: int = 0) -> str:
        e = self.msp.add_ellipse((cx, cy), major_axis=(mx, my), ratio=float(ratio),
                                 dxfattribs=self._attrs(layer, color))
        self._dirty = True
        return self._track(e)

    def text(self, s: str, x: float, y: float, height: float, rotation_deg: float, layer: str = "", color: int = 0) -> str:
        attribs = self._attrs(layer, color)
        attribs["height"] = float(height)
        attribs["insert"] = (float(x), float(y))
        if rotation_deg:
            attribs["rotation"] = float(rotation_deg)
        e = self.msp.add_text(str(s), dxfattribs=attribs)
        self._dirty = True
        return self._track(e)

    def dimension(self, x1: float, y1: float, x2: float, y2: float, offset: float, layer: str = "", color: int = 0) -> str:
        bx, by = _dim_base(x1, y1, x2, y2, offset)
        dim = self.msp.add_linear_dim(
            base=(bx, by), p1=(x1, y1), p2=(x2, y2),
            dimstyle="EZDXF", dxfattribs=self._attrs(layer, color),
        )
        dim.render()
        self._dirty = True
        return self._track(dim.dimension)

    # ---- 图层 / 读取 / 文件 ----
    def list_layers(self) -> list[dict[str, Any]]:
        out = []
        for lyr in self.doc.layers:
            out.append({
                "name": lyr.dxf.name,
                "color": int(lyr.dxf.color),
                "on": lyr.is_on(),
                "frozen": lyr.is_frozen(),
            })
        return out

    def create_layer(self, name: str, color: int) -> str:
        if name not in self.doc.layers:
            self.doc.layers.add(name, color=int(color or 7))
        elif color:
            self.doc.layers.get(name).color = int(color)
        return name

    def read_drawing(self, what: str, limit: int) -> dict[str, Any]:
        counts: dict[str, int] = {}
        texts: list[str] = []
        total = 0
        for ent in self.msp:
            total += 1
            t = ent.dxftype()
            counts[t] = counts.get(t, 0) + 1
            if t == "TEXT" and len(texts) < 50:
                s = str(ent.dxf.text).strip()
                if s:
                    texts.append(s)
            elif t == "MTEXT" and len(texts) < 50:
                try:
                    s = ent.plain_text().strip()
                    if s:
                        texts.append(s)
                except Exception:
                    pass
        return {"entityTotal": total, "scanned": total, "entityCounts": counts, "texts": texts,
                "layers": self.list_layers()}

    def save(self) -> str:
        if not self.dxf_path:
            raise RuntimeError("当前 DXF 图纸从未保存过，请用 saveAs 指定路径（saveAs filePath=\"D:/part.dxf\"）")
        self.doc.saveas(self.dxf_path)
        self._dirty = False
        return self.dxf_path

    def save_as(self, path: Path) -> str:
        p = path if path.is_absolute() else (_resolve_save_dir({}) / path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(str(p))
        self.dxf_path = str(p)
        self._dirty = False
        return str(p)

    def export_dxf(self, path: Path) -> str:
        return self.save_as(path)

    def erase(self, handle: str) -> None:
        ent = self.doc.entitydb.get(handle)
        self.msp.delete_entity(ent)
        self._dirty = True

    def after_draw(self) -> None:
        # 绘制期间自动落盘；文件被 CAD 占用（打开中）时静默跳过，等下次再试
        if not (self._dirty and self.dxf_path):
            return
        try:
            self.doc.saveas(self.dxf_path)
            self._dirty = False
        except PermissionError:
            pass
        except Exception:
            pass


def _dim_base(x1: float, y1: float, x2: float, y2: float, offset: float) -> tuple[float, float]:
    """由两标注点中点沿法向偏移 offset，得到尺寸线位置"""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    return (x1 + x2) / 2 + nx * offset, (y1 + y2) / 2 + ny * offset


# ==============================================================
# 后端连接管理
# ==============================================================
def _connect_com(key: str = "auto", launch: bool = False) -> ComBackend:
    targets = [t for t in _CAD_APPS if key in ("auto", t[0])]
    last_err: Exception | None = None
    win32, pythoncom = _get_win32()
    for _k, prog_id, name in targets:
        try:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
            try:
                win32.GetActiveObject(prog_id)  # 先探测已运行实例
            except Exception as e:
                last_err = e
                if not launch:
                    continue
                # launch=True 时允许启动新实例
                try:
                    win32.Dispatch(prog_id)
                except Exception as e2:
                    last_err = e2
                    continue
            return ComBackend(prog_id)
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            continue
    detail = f"{last_err}" if last_err else "未检测到"
    raise RuntimeError(f"未能连接 CAD（{detail}）。若本机未安装 CAD，可使用 DXF 模式：run(action=\"connect\", what=\"dxf\")")


def _new_dxf_backend(save_dir: Path) -> DxfBackend:
    b = DxfBackend()
    save_dir.mkdir(parents=True, exist_ok=True)
    b.dxf_path = str(save_dir / f"drawing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.dxf")
    return b


def _ensure_backend(kwargs: dict):
    """会话不存在时自动连接：优先 COM（仅探测已运行实例），失败降级 DXF"""
    mode = _SESSION["mode"]
    if mode == "com":
        return ComBackend(_SESSION["progKey"])
    if mode == "dxf" and _SESSION["dxfDoc"] is not None:
        return _SESSION["dxfDoc"]
    what = str(kwargs.get("what") or "auto").lower()
    if what in ("dxf", "ezdxf"):
        b = _new_dxf_backend(_resolve_save_dir(kwargs))
    else:
        key = what if what in {t[0] for t in _CAD_APPS} else "auto"
        try:
            b = _connect_com(key, launch=bool(kwargs.get("launch")))
            _SESSION["mode"] = "com"
            _SESSION["progKey"] = key if key != "auto" else _match_prog_key(b.prog_id)
            return b
        except Exception:
            b = _new_dxf_backend(_resolve_save_dir(kwargs))
    _SESSION["mode"] = "dxf"
    _SESSION["dxfDoc"] = b
    return b


def _match_prog_key(prog_id: str) -> str:
    for k, pid, _n in _CAD_APPS:
        if pid == prog_id:
            return k
    return "auto"


# ==============================================================
# 模板库（全部通过统一后端 API 组合绘制）
# ==============================================================
def _ensure_std_layers(b) -> None:
    b._ensure_layer(L_OUTLINE, 7)
    b._ensure_layer(L_CENTER, 1)
    b._ensure_layer(L_DIM, 3)
    b._ensure_layer(L_TEXT, 4)


def _get_params(kwargs: dict) -> dict[str, Any]:
    p = kwargs.get("params")
    if p is None:
        return {}
    if isinstance(p, str):
        try:
            p = _json.loads(p)
        except Exception:
            return {}
    return p if isinstance(p, dict) else {}


def _pf(params: dict, key: str, default: float) -> float:
    return _fnum(params.get(key), default)


def _tmpl_flange(b, params: dict, bx: float, by: float) -> str:
    """法兰盘俯视图：同心圆 + 螺栓孔圆周分布 + 十字中心线"""
    _ensure_std_layers(b)
    outer_d = _pf(params, "outerD", 100.0)
    inner_d = _pf(params, "innerD", 50.0)
    bolt_n = int(_pf(params, "boltCount", 6))
    bolt_cd = _pf(params, "boltCircleD", 75.0)
    bolt_d = _pf(params, "boltD", 9.0)
    r_o, r_i, r_bc, r_b = outer_d / 2, inner_d / 2, bolt_cd / 2, bolt_d / 2
    cx, cy = bx + r_o, by + r_o

    # 十字中心线（超出轮廓 8）
    ext = 8.0
    b.line(cx - r_o - ext, cy, cx + r_o + ext, cy, L_CENTER, 1)
    b.line(cx, cy - r_o - ext, cx, cy + r_o + ext, L_CENTER, 1)
    # 螺栓分布圆中心线
    b.circle(cx, cy, r_bc, L_CENTER, 1)

    b.circle(cx, cy, r_o, L_OUTLINE, 7)
    b.circle(cx, cy, r_i, L_OUTLINE, 7)
    handles = []
    for i in range(max(bolt_n, 1)):
        a = math.radians(360.0 * i / bolt_n + 90.0)
        hx, hy = cx + r_bc * math.cos(a), cy + r_bc * math.sin(a)
        handles.append(b.circle(hx, hy, r_b, L_OUTLINE, 7))
        b.line(hx - r_b * 1.6, hy, hx + r_b * 1.6, hy, L_CENTER, 1)
        b.line(hx, hy - r_b * 1.6, hx, hy + r_b * 1.6, L_CENTER, 1)
    # 直径标注
    b.dimension(cx - r_o, cy, cx + r_o, cy, 10.0, L_DIM, 3)
    b.text(f"法兰 DN{int(inner_d)}", cx, cy - r_o - 22, 5.0, 0, L_TEXT, 4)
    return (f"法兰盘已绘制：外径{outer_d} 内径{inner_d} 螺栓{bolt_n}×⌀{bolt_d} "
            f"分布圆⌀{bolt_cd}（中心在 {cx:.1f},{cy:.1f}）")


def _tmpl_gasket(b, params: dict, bx: float, by: float) -> str:
    """矩形垫片：圆角矩形轮廓 + 边缘均布孔"""
    _ensure_std_layers(b)
    w = _pf(params, "width", 120.0)
    h = _pf(params, "height", 80.0)
    cr = _pf(params, "cornerR", 10.0)
    hole_n = int(_pf(params, "holeCount", 4))
    hole_d = _pf(params, "holeD", 9.0)
    margin = _pf(params, "edgeMargin", 10.0)
    cr = min(cr, w / 2 - 1, h / 2 - 1)

    # 圆角矩形（四段圆弧 + 四条直边）

    x2, y2 = bx + w, by + h
    if cr > 0.5:
        b.arc(bx + cr, by + cr, cr, 180, 270, L_OUTLINE, 7)
        b.arc(x2 - cr, by + cr, cr, 270, 360, L_OUTLINE, 7)
        b.arc(x2 - cr, y2 - cr, cr, 0, 90, L_OUTLINE, 7)
        b.arc(bx + cr, y2 - cr, cr, 90, 180, L_OUTLINE, 7)
        b.line(bx + cr, by, x2 - cr, by, L_OUTLINE, 7)
        b.line(x2, by + cr, x2, y2 - cr, L_OUTLINE, 7)
        b.line(bx + cr, y2, x2 - cr, y2, L_OUTLINE, 7)
        b.line(bx, by + cr, bx, y2 - cr, L_OUTLINE, 7)
    else:
        b.polyline([(bx, by), (x2, by), (x2, y2), (bx, y2)], closed=True, layer=L_OUTLINE, color=7)

    r_h = hole_d / 2
    hole_y = by + margin + r_h
    if hole_n >= 2:
        span = (w - 2 * (margin + r_h))
        for i in range(hole_n):
            hx = bx + margin + r_h + span * i / (hole_n - 1)
            b.circle(hx, hole_y, r_h, L_OUTLINE, 7)
            b.line(hx - r_h * 1.6, hole_y, hx + r_h * 1.6, hole_y, L_CENTER, 1)
            b.line(hx, hole_y - r_h * 1.6, hx, hole_y + r_h * 1.6, L_CENTER, 1)
    else:
        b.circle(bx + w / 2, hole_y, r_h, L_OUTLINE, 7)

    b.dimension(bx, by - 12, x2, by - 12, 0, L_DIM, 3)
    b.dimension(x2 + 12, by, x2 + 12, y2, 0, L_DIM, 3)
    b.text(f"垫片 {w:.0f}×{h:.0f}", bx + w / 2, by - 26, 5.0, 0, L_TEXT, 4)
    return f"垫片已绘制：{w}×{h} 圆角R{cr} {hole_n}×⌀{hole_d} 孔（左下角 {bx:.1f},{by:.1f}）"


def _tmpl_shaft(b, params: dict, bx: float, by: float) -> str:
    """阶梯轴：水平放置的分段圆柱轮廓 + 轴线"""
    _ensure_std_layers(b)
    segs_raw = params.get("segments")
    segs: list[tuple[float, float]] = []
    if isinstance(segs_raw, list):
        for s in segs_raw:
            if isinstance(s, dict):
                segs.append((_pf(s, "d", 30.0), _pf(s, "len", 40.0)))
            elif isinstance(s, (list, tuple)) and len(s) >= 2:
                segs.append((_fnum(s[0], 30.0), _fnum(s[1], 40.0)))
    if not segs:
        segs = [(30.0, 50.0), (45.0, 30.0), (30.0, 50.0)]

    y0 = by
    x = bx
    r_max = max(d for d, _ in segs) / 2
    # 轴线
    total_len = sum(ln for _, ln in segs)
    b.line(bx - 8, y0, bx + total_len + 8, y0, L_CENTER, 1)
    # 上轮廓 + 端面
    top_pts: list[tuple[float, float]] = [(bx, y0 + segs[0][0] / 2)]
    for d, ln in segs:
        r = d / 2
        x += ln
        top_pts.append((x, y0 + r))
    bot_pts: list[tuple[float, float]] = []
    x = bx + total_len
    for d, ln in reversed(segs):
        bot_pts.append((x, y0 - d / 2))
        x -= ln
    b.polyline(top_pts, layer=L_OUTLINE, color=7)
    b.polyline(bot_pts, layer=L_OUTLINE, color=7)
    # 两端面线
    b.line(bx, y0 + segs[0][0] / 2, bx, y0 - segs[0][0] / 2, L_OUTLINE, 7)
    b.line(bx + total_len, y0 + segs[-1][0] / 2, bx + total_len, y0 - segs[-1][0] / 2, L_OUTLINE, 7)
    # 台阶处竖直过渡线
    x = bx
    prev_r = segs[0][0] / 2
    for d, ln in segs[1:]:
        x += ln
        r = d / 2
        if abs(r - prev_r) > 0.01:
            b.line(x - ln, y0 + prev_r, x - ln, y0 + r, L_OUTLINE, 7)
            b.line(x - ln, y0 - prev_r, x - ln, y0 - r, L_OUTLINE, 7)
        prev_r = r
    # 每段长度标注
    x = bx
    for i, (d, ln) in enumerate(segs):
        b.dimension(x, y0 - r_max - 14, x + ln, y0 - r_max - 14, 0, L_DIM, 3)
        b.text(f"⌀{d:.0f}", x + ln / 2, y0 + r_max + 10, 4.0, 0, L_TEXT, 4)
        x += ln
    desc = "、".join(f"⌀{d:.0f}×{ln:.0f}" for d, ln in segs)
    return f"阶梯轴已绘制：{desc}（起点 {bx:.1f},{by:.1f}）"


def _tmpl_hex_bolt(b, params: dict, bx: float, by: float) -> str:
    """六角螺栓俯视图：六角头 + 螺杆圆 + 螺纹底径圆（3/4 圆弧）"""
    _ensure_std_layers(b)
    across = _pf(params, "headAcross", 24.0)   # 对边距
    bolt_d = _pf(params, "boltD", 16.0)
    bolt_len = _pf(params, "boltLen", 50.0)
    r_head = (across / 2) / math.cos(math.radians(30))  # 外接圆半径
    cx, cy = bx + r_head, by + r_head

    # 六边形（顶点朝上）
    pts = []
    for i in range(6):
        a = math.radians(60 * i + 90)
        pts.append((cx + r_head * math.cos(a), cy + r_head * math.sin(a)))
    b.polyline(pts, closed=True, layer=L_OUTLINE, color=7)
    # 内切圆（扳手接触面）
    b.circle(cx, cy, across / 2, L_OUTLINE, 7)
    # 螺杆外径圆
    b.circle(cx, cy, bolt_d / 2, L_OUTLINE, 7)
    # 螺纹底径 3/4 圆弧（示意，跨 0° 弧：120° → 30° 逆时针）
    b.arc(cx, cy, bolt_d / 2 * 0.82, 120, 30, L_OUTLINE, 7)
    # 十字中心线
    e = r_head + 6
    b.line(cx - e, cy, cx + e, cy, L_CENTER, 1)
    b.line(cx, cy - e, cx, cy + e, L_CENTER, 1)
    b.text(f"M{bolt_d:.0f}×{bolt_len:.0f}", cx, cy - r_head - 14, 4.5, 0, L_TEXT, 4)
    return (f"六角螺栓已绘制：M{bolt_d}×{bolt_len} 对边{across}"
            f"（中心 {cx:.1f},{cy:.1f}）")


def _tmpl_room(b, params: dict, bx: float, by: float) -> str:
    """户型房间：双线外墙 + 南墙门洞（含门扇与开启弧）+ 北墙窗"""
    _ensure_std_layers(b)
    L_WALL = "墙体"
    b._ensure_layer(L_WALL, 7)
    w = _pf(params, "width", 6000.0)
    h = _pf(params, "height", 4000.0)
    t = _pf(params, "wallT", 240.0)
    door_w = _pf(params, "doorWidth", 900.0)
    win_w = _pf(params, "windowWidth", 1500.0)

    ox, oy = bx + t / 2, by + t / 2  # 外沿
    ix, iy = bx - t / 2, by - t / 2
    # 外沿矩形（南墙留门洞）
    door_x0 = ox + (w - door_w) / 2
    door_x1 = door_x0 + door_w
    b.polyline([(ox, oy), (door_x0, oy)], layer=L_WALL, color=7)
    b.polyline([(door_x1, oy), (ox + w, oy)], layer=L_WALL, color=7)
    b.polyline([(ox + w, oy), (ox + w, oy + h), (ox, oy + h), (ox, oy)], layer=L_WALL, color=7)
    # 内沿矩形（完整）
    b.polyline([(ix, iy), (ix + w, iy), (ix + w, iy + h), (ix, iy + h)], closed=True, layer=L_WALL, color=7)
    # 门洞两侧封口线
    b.line(door_x0, oy, door_x0, iy, L_WALL, 7)
    b.line(door_x1, oy, door_x1, iy, L_WALL, 7)
    # 门扇（竖直开启）+ 开启弧
    b.line(door_x0, oy, door_x0, oy + door_w, L_WALL, 7)
    b.arc(door_x0, oy, door_w, 0, 90, L_WALL, 7)
    # 北墙窗（四线表示：洞口两条 + 窗框两条）
    wx0 = ox + (w - win_w) / 2
    wx1 = wx0 + win_w
    y_in = oy + h - t
    b.line(wx0, oy + h, wx1, oy + h, L_WALL, 7)
    b.line(wx0, y_in, wx1, y_in, L_WALL, 7)
    b.line(wx0, oy + h - t / 3, wx1, oy + h - t / 3, L_WALL, 7)
    b.line(wx0, oy + h - t * 2 / 3, wx1, oy + h - t * 2 / 3, L_WALL, 7)
    # 尺寸标注
    b.dimension(ox, oy - 600, ox + w, oy - 600, 0, L_DIM, 3)
    b.dimension(ox - 600, oy, ox - 600, oy + h, 0, L_DIM, 3)
    b.text(f"{w:.0f}×{h:.0f} 墙{t:.0f}", ox + w / 2, oy + h + 600, 200.0, 0, L_TEXT, 4)
    return (f"房间已绘制：{w}×{h} 墙厚{t} 南墙门洞{door_w} 北墙窗{win_w}"
            f"（外沿左下角 {ox:.1f},{oy:.1f}）")


def _tmpl_grid(b, params: dict, bx: float, by: float) -> str:
    """结构轴网：柱距/跨距轴线 + 端部轴号圆圈"""
    _ensure_std_layers(b)
    col_sp = params.get("colSpacing")
    row_sp = params.get("rowSpacing")
    if isinstance(col_sp, list) and col_sp:
        col_spans = [_fnum(v, 6000.0) for v in col_sp]
    else:
        col_spans = [_pf(params, "colSpacing", 6000.0)] * max(int(_pf(params, "cols", 3)), 1)
    if isinstance(row_sp, list) and row_sp:
        row_spans = [_fnum(v, 6000.0) for v in row_sp]
    else:
        row_spans = [_pf(params, "rowSpacing", 6000.0)] * max(int(_pf(params, "rows", 2)), 1)

    ext = 1000.0     # 轴线外伸
    r_lab = 400.0    # 轴号圆半径
    W = sum(col_spans)
    H = sum(row_spans)
    xs = [bx]
    for s in col_spans:
        xs.append(xs[-1] + s)
    ys = [by]
    for s in row_spans:
        ys.append(ys[-1] + s)

    for i, x in enumerate(xs):
        b.line(x, by - ext, x, by + H + ext, L_CENTER, 1)
        b.circle(x, by + H + ext + r_lab, r_lab, L_TEXT, 4)
        b.text(str(i + 1), x, by + H + ext + r_lab, r_lab * 1.1, 0, L_TEXT, 4)
    letters = "ABCDEFGHJKLM"
    for j, y in enumerate(ys):
        b.line(bx - ext, y, bx + W + ext, y, L_CENTER, 1)
        b.circle(bx - ext - r_lab, y, r_lab, L_TEXT, 4)
        b.text(letters[j % len(letters)], bx - ext - r_lab, y, r_lab * 1.1, 0, L_TEXT, 4)
    # 总尺寸
    b.dimension(xs[0], by - ext - 500, xs[-1], by - ext - 500, 0, L_DIM, 3)
    b.dimension(bx - ext - 500, ys[0], bx - ext - 500, ys[-1], 0, L_DIM, 3)
    return f"轴网已绘制：{len(col_spans)}跨×{len(row_spans)}跨，总尺寸 {W:.0f}×{H:.0f}（左下角 {bx:.1f},{by:.1f}）"


_TEMPLATES: dict[str, Any] = {
    "flange": _tmpl_flange,
    "gasket": _tmpl_gasket,
    "shaft": _tmpl_shaft,
    "hexBolt": _tmpl_hex_bolt,
    "room": _tmpl_room,
    "grid": _tmpl_grid,
}


# ==============================================================
# action 处理器
# ==============================================================
def _act_status(kwargs: dict) -> dict[str, Any]:
    running = []
    win32 = None
    pythoncom = None
    try:
        win32, pythoncom = _get_win32()
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
    except Exception:
        pass
    for k, pid, name in _CAD_APPS:
        ok = False
        if win32 is not None:
            try:
                win32.GetActiveObject(pid)
                ok = True
            except Exception:
                ok = False
        running.append({"id": k, "name": name, "progId": pid, "running": ok})
    try:
        import ezdxf  # noqa: F401
        ezdxf_ok = True
    except ImportError:
        ezdxf_ok = False
    doc = _SESSION.get("dxfDoc")
    return {
        "ok": True,
        "mode": _SESSION["mode"],
        "modeText": {"none": "未连接", "com": "COM 实时模式", "dxf": "DXF 文件模式"}.get(_SESSION["mode"], _SESSION["mode"]),
        "runningCads": running,
        "ezdxfInstalled": ezdxf_ok,
        "dxfPath": doc.dxf_path if doc else "",
        "dxfUnsavedChanges": bool(doc and doc._dirty),
        "recentHandles": _SESSION["handles"][-20:],
        "templates": sorted(_TEMPLATES.keys()),
    }


def _act_connect(kwargs: dict) -> dict[str, Any]:
    what = str(kwargs.get("what") or "auto").lower()
    save_dir = _resolve_save_dir(kwargs)
    if what in ("dxf", "ezdxf"):
        b = _new_dxf_backend(save_dir)
        _SESSION.update(mode="dxf", dxfDoc=b, progKey="")
        return {"ok": True, "mode": "dxf", "dxfPath": b.dxf_path,
                "hint": f"DXF 模式就绪，图纸将保存到 {b.dxf_path}（绘制过程自动保存）"}
    key = what if what in {t[0] for t in _CAD_APPS} else "auto"
    try:
        b = _connect_com(key, launch=bool(kwargs.get("launch")))
        _SESSION.update(mode="com", progKey=_match_prog_key(b.prog_id), dxfDoc=None, dxfPath="")
        name = next((n for k, _p, n in _CAD_APPS if k == _SESSION["progKey"]), b.prog_id)
        return {"ok": True, "mode": "com", "app": name, "progId": b.prog_id,
                "drawing": str(getattr(b.doc, "Name", "")),
                "hint": "已连接正在运行的 CAD，绘图指令将直接画在当前图纸里"}
    except Exception as e:
        b = _new_dxf_backend(save_dir)
        _SESSION.update(mode="dxf", dxfDoc=b, progKey="")
        return {"ok": True, "mode": "dxf", "dxfPath": b.dxf_path, "comError": str(e),
                "hint": "未检测到正在运行的 CAD，已自动降级为 DXF 文件模式"}


def _act_line(b, kwargs: dict) -> dict[str, Any]:
    h = b.line(_get2(kwargs, "x1"), _get2(kwargs, "y1"),
               _get2(kwargs, "x2"), _get2(kwargs, "y2"), *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "LINE"}


def _act_polyline(b, kwargs: dict) -> dict[str, Any]:
    pts = _get_pts(kwargs)
    layer, color = _get_layer_color(kwargs)
    h = b.polyline(pts, closed=bool(kwargs.get("closed")), layer=layer, color=color)
    return {"ok": True, "handle": h, "entity": "LWPOLYLINE", "vertexCount": len(pts)}


def _act_circle(b, kwargs: dict) -> dict[str, Any]:
    r = _get2(kwargs, "r", "radius")
    if r <= 0:
        return {"ok": False, "error": "半径 r 必须大于 0"}
    h = b.circle(_get2(kwargs, "cx", "x"), _get2(kwargs, "cy", "y"), r, *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "CIRCLE"}


def _act_arc(b, kwargs: dict) -> dict[str, Any]:
    r = _get2(kwargs, "r", "radius")
    if r <= 0:
        return {"ok": False, "error": "半径 r 必须大于 0"}
    h = b.arc(_get2(kwargs, "cx", "x"), _get2(kwargs, "cy", "y"), r,
              _get2(kwargs, "startAngle"), _get2(kwargs, "endAngle", default=90.0),
              *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "ARC"}


def _act_ellipse(b, kwargs: dict) -> dict[str, Any]:
    ratio = _get2(kwargs, "ratio", default=0.5)
    if not (0 < ratio <= 1):
        return {"ok": False, "error": "ratio（短轴/长轴）必须在 (0, 1] 内"}
    h = b.ellipse(_get2(kwargs, "cx", "x"), _get2(kwargs, "cy", "y"),
                  _get2(kwargs, "majorX", default=50.0), _get2(kwargs, "majorY"),
                  ratio, *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "ELLIPSE"}


def _act_rectangle(b, kwargs: dict) -> dict[str, Any]:
    x, y = _get2(kwargs, "x"), _get2(kwargs, "y")
    w, hh = _get2(kwargs, "width"), _get2(kwargs, "height")
    if w <= 0 or hh <= 0:
        return {"ok": False, "error": "width / height 必须大于 0"}
    layer, color = _get_layer_color(kwargs)
    h = b.polyline([(x, y), (x + w, y), (x + w, y + hh), (x, y + hh)],
                   closed=True, layer=layer, color=color)
    return {"ok": True, "handle": h, "entity": "LWPOLYLINE"}


def _act_polygon(b, kwargs: dict) -> dict[str, Any]:
    n = int(_get2(kwargs, "sides", default=6))
    r = _get2(kwargs, "r", "radius")
    if n < 3 or r <= 0:
        return {"ok": False, "error": "sides ≥ 3 且 r > 0"}
    cx, cy = _get2(kwargs, "cx", "x"), _get2(kwargs, "cy", "y")
    pts = [(cx + r * math.cos(math.radians(360 * i / n + 90)),
            cy + r * math.sin(math.radians(360 * i / n + 90))) for i in range(n)]
    layer, color = _get_layer_color(kwargs)
    h = b.polyline(pts, closed=True, layer=layer, color=color)
    return {"ok": True, "handle": h, "entity": "LWPOLYLINE", "sides": n}


def _act_text(b, kwargs: dict) -> dict[str, Any]:
    s = str(kwargs.get("text") or "").strip()
    if not s:
        return {"ok": False, "error": "text 不能为空"}
    h = b.text(s, _get2(kwargs, "x"), _get2(kwargs, "y"),
               _get2(kwargs, "height", default=3.5), _get2(kwargs, "rotation"),
               *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "TEXT"}


def _act_dimension(b, kwargs: dict) -> dict[str, Any]:
    h = b.dimension(_get2(kwargs, "x1"), _get2(kwargs, "y1"),
                    _get2(kwargs, "x2"), _get2(kwargs, "y2"),
                    _get2(kwargs, "offset", default=8.0), *_get_layer_color(kwargs))
    return {"ok": True, "handle": h, "entity": "DIMENSION"}


def _pick_handle(kwargs: dict) -> str:
    if kwargs.get("last"):
        if not _SESSION["handles"]:
            raise ValueError("会话中还没有创建过任何实体")
        return _SESSION["handles"][-1]
    h = str(kwargs.get("handle") or "").strip()
    if not h:
        raise ValueError("需要 handle 参数或 last=True")
    return h


def _act_erase(b, kwargs: dict) -> dict[str, Any]:
    h = _pick_handle(kwargs)
    b.erase(h)
    if h in _SESSION["handles"]:
        _SESSION["handles"].remove(h)
    return {"ok": True, "erased": h}


def _act_list_layers(b, kwargs: dict) -> dict[str, Any]:
    return {"ok": True, "layers": b.list_layers()}


def _act_create_layer(b, kwargs: dict) -> dict[str, Any]:
    name = str(kwargs.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name 不能为空"}
    b.create_layer(name, int(_fnum(kwargs.get("color"), 7)))
    return {"ok": True, "layer": name}


def _act_read_drawing(b, kwargs: dict) -> dict[str, Any]:
    what = str(kwargs.get("what") or "all").lower()
    limit = int(_fnum(kwargs.get("limit"), 200))
    data = b.read_drawing(what, limit)
    if what == "layers":
        data = {"layers": data["layers"]}
    elif what == "texts":
        data = {"texts": data["texts"]}
    return {"ok": True, **data}


def _act_save(b, kwargs: dict) -> dict[str, Any]:
    path = b.save()
    return {"ok": True, "file": path, "mode": b.mode}


def _act_save_as(b, kwargs: dict) -> dict[str, Any]:
    fp = str(kwargs.get("filePath") or "").strip()
    if not fp:
        return {"ok": False, "error": "需要 filePath 参数"}
    p = Path(fp)
    p.parent.mkdir(parents=True, exist_ok=True)
    path = b.save_as(p)
    if b.mode == "dxf":
        _SESSION["dxfPath"] = str(p)
    return {"ok": True, "file": path, "mode": b.mode}


def _act_export_dxf(b, kwargs: dict) -> dict[str, Any]:
    fp = str(kwargs.get("filePath") or "").strip()
    if not fp:
        if b.mode == "dxf" and b.dxf_path:
            fp = b.dxf_path
        else:
            return {"ok": False, "error": "需要 filePath 参数"}
    p = Path(fp)
    if p.suffix.lower() != ".dxf":
        p = p.with_suffix(".dxf")
    p.parent.mkdir(parents=True, exist_ok=True)
    path = b.export_dxf(p)
    return {"ok": True, "file": path, "mode": b.mode}


def _act_template(b, kwargs: dict) -> dict[str, Any]:
    what = str(kwargs.get("what") or "").strip()
    fn = _TEMPLATES.get(what)
    if fn is None:
        return {"ok": False, "error": f"未知模板：{what}，可选：{sorted(_TEMPLATES.keys())}"}
    params = _get_params(kwargs)
    bx, by = _get2(kwargs, "baseX"), _get2(kwargs, "baseY")
    summary = fn(b, params, bx, by)
    return {"ok": True, "template": what, "summary": summary}


_ACTIONS: dict[str, Any] = {
    "status": None,
    "connect": None,
    "line": _act_line,
    "polyline": _act_polyline,
    "circle": _act_circle,
    "arc": _act_arc,
    "ellipse": _act_ellipse,
    "rectangle": _act_rectangle,
    "polygon": _act_polygon,
    "text": _act_text,
    "dimension": _act_dimension,
    "erase": _act_erase,
    "listLayers": _act_list_layers,
    "createLayer": _act_create_layer,
    "readDrawing": _act_read_drawing,
    "save": _act_save,
    "saveAs": _act_save_as,
    "exportDxf": _act_export_dxf,
    "template": _act_template,
}


def run(action: str = "", **kwargs: Any) -> dict[str, Any]:
    if not action:
        return {"ok": False, "error": "action 不能为空"}
    if action not in _ACTIONS:
        return {"ok": False, "error": f"未知 action：{action}，可选：{sorted(_ACTIONS.keys())}"}
    try:
        if action == "status":
            return _act_status(kwargs)
        if action == "connect":
            return _act_connect(kwargs)
        b = _ensure_backend(kwargs)
        result = _ACTIONS[action](b, kwargs)
        b.after_draw()
        if b.mode == "dxf":
            result["file"] = b.dxf_path
            if not b.dxf_path:
                result["hint"] = "尚未保存，请调用 saveAs 指定 filePath"
        result.setdefault("mode", b.mode)
        return result
    except Exception as e:
        msg = str(e)
        if type(e).__name__ == "com_error":
            msg = f"CAD COM 调用失败：{msg}（CAD 可能已被关闭，可重新 connect 或改用 DXF 模式）"
        return {"ok": False, "error": msg}