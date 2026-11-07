#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
110㎡ 户型平面布置图 — DXF 生成脚本 v2
依据 cad_spec.md (RES-110-001)，几何按"墙落轴线"原则重建

坐标: 原户型西南角外墙轴线交点 = (0,0)，X向东，Y向北，单位 mm
图幅: A2 59400x42000 (1:100)，模型先画在原点，再整体平移入图框

图层:
  WALL-EXT/WALL-INT/WALL-LOAD/WINDOW/DOOR/BALCONY/AXIS
  FURNITURE/DIM/TEXT/HATCH/FRAME
"""
import os
import ezdxf
from ezdxf.enums import TextEntityAlignment
from collections import Counter

OUT_DXF = r"drawings/RES-110/户型平面布置图.dxf"

# ── 轴网 ──────────────────────────────────────────────
GRID_X = [0, 3200, 6300, 8300, 11000]      # A B C D E
GRID_Y = [0, 4000, 4900, 7800, 9600]       # 1 2 3 4 5
AXL = ['A', 'B', 'C', 'D', 'E']
AYL = ['1', '2', '3', '4', '5']

T_EXT, T_INT, T_LOAD = 240, 120, 200
X0, X1 = 0, 11000
Y0, Y1 = 0, 9600

# ── 房间净尺寸 (内墙面) ───────────────────────────────
ROOMS = [
    ("次卧",   120,  120,  3140, 3880),
    ("客厅",  3260,  120,  7380, 3880),
    ("主卧",  7520,  120, 10880, 3880),
    ("书房",   120, 5020,  3140, 9480),
    ("餐厅",  3260, 5020,  6180, 7680),
    ("卫生间", 6360, 5020,  8240, 7680),
    ("厨房",  8360, 5020, 10880, 7680),
    ("玄关",  3260, 7920, 10880, 9480),
    ("走廊",  3260, 4060, 10880, 4840),
]

# ── 图框 ──────────────────────────────────────────────
FRAME_W, FRAME_H = 59400, 42000
MARGIN = 2500
DX, DY = 17500, 12000          # 模型平移量


# ═══════════ 基础工具 ═══════════
def setup(doc):
    assert "DASHDOT" in doc.linetypes, "DASHDOT 线型缺失"
    layers = [
        ("WALL-EXT",  2, "CONTINUOUS", 50),
        ("WALL-INT",  7, "CONTINUOUS", 35),
        ("WALL-LOAD", 7, "CONTINUOUS", 35),
        ("WINDOW",    4, "CONTINUOUS", 25),
        ("DOOR",      4, "CONTINUOUS", 25),
        ("BALCONY",   5, "CONTINUOUS", 25),
        ("AXIS",      1, "DASHDOT",    13),
        ("FURNITURE", 3, "CONTINUOUS", 15),
        ("DIM",       3, "CONTINUOUS", 18),
        ("TEXT",      4, "CONTINUOUS", 18),
        ("HATCH",     8, "CONTINUOUS",  9),
        ("FRAME",     7, "CONTINUOUS", 50),
    ]
    for name, color, lt, lw in layers:
        lay = doc.layers.add(name, color=color, linetype=lt)
        try:
            lay.dxf.lineweight = lw
        except Exception:
            pass


def segments(a, b, gaps):
    """把区间 [a,b] 按 gaps 切分成若干实心段"""
    out, cur = [], a
    for g1, g2 in sorted(gaps):
        g1, g2 = max(a, g1), min(b, g2)
        if g2 <= cur:
            continue
        if g1 > cur:
            out.append((cur, g1))
        cur = max(cur, g2)
    if cur < b:
        out.append((cur, b))
    return out


def wall_h(msp, layer, y, x1, x2, t, gaps=()):
    for a, b in segments(x1, x2, gaps):
        msp.add_line((a, y + t / 2), (b, y + t / 2), dxfattribs={"layer": layer})
        msp.add_line((a, y - t / 2), (b, y - t / 2), dxfattribs={"layer": layer})


def wall_v(msp, layer, x, y1, y2, t, gaps=()):
    for a, b in segments(y1, y2, gaps):
        msp.add_line((x - t / 2, a), (x - t / 2, b), dxfattribs={"layer": layer})
        msp.add_line((x + t / 2, a), (x + t / 2, b), dxfattribs={"layer": layer})


def fill45(msp, x1, y1, x2, y2, spacing=260, layer="HATCH"):
    """在矩形内画 45° 剖面线（墙填充），正确裁剪"""
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return
    n = int((w + h) / spacing) + 1
    for i in range(n + 1):
        d = i * spacing
        t1 = max(0.0, d - w)
        t2 = min(float(h), float(d))
        if t2 - t1 < 1:
            continue
        msp.add_line((x1 + d - t1, y1 + t1), (x1 + d - t2, y1 + t2),
                     dxfattribs={"layer": layer})


def rect(msp, x1, y1, x2, y2, layer="FURNITURE"):
    msp.add_lwpolyline([(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
                       close=True, dxfattribs={"layer": layer})


def rect_c(msp, cx, cy, w, h, layer="FURNITURE"):
    rect(msp, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, layer)


def text(msp, s, x, y, h=350, layer="TEXT", align=TextEntityAlignment.MIDDLE_CENTER):
    t = msp.add_text(s, dxfattribs={"layer": layer, "height": h})
    t.set_placement((x, y), align=align)
    return t


# ═══════════ 墙体 ═══════════
def draw_walls(msp):
    # ── 外墙 240 ──
    # 南墙: 客厅推拉门 3400~7200, 主卧飘窗 7500~10800
    wall_h(msp, "WALL-EXT", 0, X0 - T_EXT / 2, X1 + T_EXT / 2, T_EXT,
           gaps=[(3400, 7200), (7500, 10800)])
    # 北墙: 餐厅窗 3700~5200 + 入户门 4300~5300(合并) , 厨房窗 8600~9800
    wall_h(msp, "WALL-EXT", Y1, X0 - T_EXT / 2, X1 + T_EXT / 2, T_EXT,
           gaps=[(3700, 5300), (8600, 9800)])
    # 西墙: 次卧窗 1400~2900, 书房窗 5800~7300
    wall_v(msp, "WALL-EXT", 0, Y0 - T_EXT / 2, Y1 + T_EXT / 2, T_EXT,
           gaps=[(1400, 2900), (5800, 7300)])
    # 东墙: 卫生间小窗 5200~5800
    wall_v(msp, "WALL-EXT", X1, Y0 - T_EXT / 2, Y1 + T_EXT / 2, T_EXT,
           gaps=[(5200, 5800)])

    # ── 承重内墙 200 ──
    # B轴 x=3200: 次卧门 D2(3100~3900), 走廊连通带(4000~4900)断开
    wall_v(msp, "WALL-LOAD", 3200, Y0, Y1, T_LOAD,
           gaps=[(3100, 3900), (4000, 4900)])
    # 客厅/主卧之间 x=7400 (0~4000 实墙)
    wall_v(msp, "WALL-LOAD", 7400, Y0, 4000, T_LOAD)

    # ── 普通内墙 120 ──
    # C轴 x=6300: 餐厅|卫生间 分隔, 4900~7800
    wall_v(msp, "WALL-INT", 6300, 4900, 7800, T_INT)
    # D轴 x=8300: 卫生间|厨房 分隔, 4900~7800
    wall_v(msp, "WALL-INT", 8300, 4900, 7800, T_INT)
    # 2轴 y=4000: 次卧北墙(0~3200实); 主卧北墙(7400~11000) 含主卧门 D3 7900~8700
    wall_h(msp, "WALL-INT", 4000, X0, 3200, T_INT)
    wall_h(msp, "WALL-INT", 4000, 7400, X1, T_INT, gaps=[(7900, 8700)])
    # 3轴 y=4900: 书房南墙(0~3200, 门1400~2200); 餐厅开口3500~5000; 卫生间门6500~7200; 厨房门8500~9300
    wall_h(msp, "WALL-INT", 4900, X0, 3200, T_INT, gaps=[(1400, 2200)])
    wall_h(msp, "WALL-INT", 4900, 3200, X1, T_INT,
           gaps=[(3500, 5000), (6500, 7200), (8500, 9300)])
    # 4轴 y=7800: 餐厅|玄关 开口4000~4800; 卫生间/厨房北墙实
    wall_h(msp, "WALL-INT", 7800, 3200, X1, T_INT, gaps=[(4000, 4800)])


def draw_wall_hatch(msp):
    """墙体45°填充（只填实体墙段，跳过洞口）"""
    E, I, L = T_EXT, T_INT, T_LOAD
    jobs = []

    def h_jobs(y, x1, x2, t, gaps=()):
        for a, b in segments(x1, x2, gaps):
            jobs.append((a, y - t / 2, b, y + t / 2))

    def v_jobs(x, y1, y2, t, gaps=()):
        for a, b in segments(y1, y2, gaps):
            jobs.append((x - t / 2, a, x + t / 2, b))

    h_jobs(0, X0 - E / 2, X1 + E / 2, E, [(3400, 7200), (7500, 10800)])
    h_jobs(Y1, X0 - E / 2, X1 + E / 2, E, [(3700, 5300), (8600, 9800)])
    v_jobs(0, Y0 - E / 2, Y1 + E / 2, E, [(1400, 2900), (5800, 7300)])
    v_jobs(X1, Y0 - E / 2, Y1 + E / 2, E, [(5200, 5800)])

    v_jobs(3200, Y0, Y1, L, [(3100, 3900), (4000, 4900)])
    v_jobs(7400, Y0, 4000, L)
    v_jobs(6300, 4900, 7800, I)
    v_jobs(8300, 4900, 7800, I)

    h_jobs(4000, X0, 3200, I, [])
    h_jobs(4000, 7400, X1, I, [(7900, 8700)])
    h_jobs(4900, X0, 3200, I, [(1400, 2200)])
    h_jobs(4900, 3200, X1, I, [(3500, 5000), (6500, 7200), (8500, 9300)])
    h_jobs(7800, 3200, X1, I, [(4000, 4800)])

    for x1, y1, x2, y2 in jobs:
        fill45(msp, x1, y1, x2, y2, spacing=260, layer="HATCH")


# ═══════════ 门窗 ═══════════
def window_h(msp, y, x1, x2, t):
    msp.add_line((x1, y - t / 2), (x1, y + t / 2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x2, y - t / 2), (x2, y + t / 2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x1, y - t / 2), (x2, y - t / 2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x1, y + t / 2), (x2, y + t / 2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x1, y), (x2, y), dxfattribs={"layer": "WINDOW"})


def window_v(msp, x, y1, y2, t):
    msp.add_line((x - t / 2, y1), (x + t / 2, y1), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x - t / 2, y2), (x + t / 2, y2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x - t / 2, y1), (x - t / 2, y2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x + t / 2, y1), (x + t / 2, y2), dxfattribs={"layer": "WINDOW"})
    msp.add_line((x, y1), (x, y2), dxfattribs={"layer": "WINDOW"})


def sliding_h(msp, y, x1, x2):
    """推拉门符号: 两条错位平行线"""
    m = (x1 + x2) / 2
    msp.add_line((x1, y + 60), (m + 60, y + 60), dxfattribs={"layer": "WINDOW"})
    msp.add_line((m - 60, y - 60), (x2, y - 60), dxfattribs={"layer": "WINDOW"})


def door_h(msp, y, x1, x2, hinge="left", swing="up"):
    """水平墙上的门: 门扇+90°开启弧"""
    w = x2 - x1
    hx = x1 if hinge == "left" else x2
    d = 1 if swing == "up" else -1
    msp.add_line((hx, y), (hx, y + d * w), dxfattribs={"layer": "DOOR"})
    if swing == "up":
        a1, a2 = (0, 90) if hinge == "left" else (90, 180)
    else:
        a1, a2 = (270, 360) if hinge == "left" else (180, 270)
    msp.add_arc((hx, y), w, a1, a2, dxfattribs={"layer": "DOOR"})


def door_v(msp, x, y1, y2, hinge="bottom", swing="right"):
    w = y2 - y1
    hy = y1 if hinge == "bottom" else y2
    d = 1 if swing == "right" else -1
    msp.add_line((x, hy), (x + d * w, hy), dxfattribs={"layer": "DOOR"})
    if swing == "right":
        a1, a2 = (0, 90) if hinge == "bottom" else (270, 360)
    else:
        a1, a2 = (90, 180) if hinge == "bottom" else (180, 270)
    msp.add_arc((x, hy), w, a1, a2, dxfattribs={"layer": "DOOR"})


def draw_openings(msp):
    E = T_EXT
    # 窗
    window_v(msp, 0, 1400, 2900, E)            # W1 次卧西窗
    window_v(msp, 0, 5800, 7300, E)            # W2 书房西窗
    window_h(msp, Y1, 3700, 4300, E)           # W3 餐厅北窗
    window_h(msp, Y1, 8600, 9800, E)           # W4 厨房北窗
    window_v(msp, X1, 5200, 5800, E)           # W5 卫生间东窗
    # 推拉门
    sliding_h(msp, 0, 3400, 7200)              # SD1 客厅-南阳台
    sliding_h(msp, Y1, 8600, 9800)             # SD2 厨房-北阳台
    # 门
    door_h(msp, Y1, 4300, 5300, hinge="left", swing="down")   # D1 入户门
    door_v(msp, 3200, 3100, 3900, hinge="bottom", swing="left")  # D2 次卧门(向西开)
    door_h(msp, 4000, 7900, 8700, hinge="left", swing="down")    # D3 主卧门
    door_h(msp, 4900, 1400, 2200, hinge="right", swing="up")     # D4 书房门
    door_h(msp, 4900, 6500, 7200, hinge="left", swing="up")      # D5 卫生间门
    door_h(msp, 4900, 8500, 9300, hinge="right", swing="up")     # D6 厨房门


# ═══════════ 阳台/飘窗 ═══════════
def draw_balconies(msp):
    L = "BALCONY"
    # 南阳台 3200~7400, y -1500~0, 栏板厚100
    msp.add_line((3200, -1500), (7400, -1500), dxfattribs={"layer": L})
    msp.add_line((3200, -1400), (7400, -1400), dxfattribs={"layer": L})
    msp.add_line((3200, -1500), (3200, -120), dxfattribs={"layer": L})
    msp.add_line((7400, -1500), (7400, -120), dxfattribs={"layer": L})
    msp.add_line((3200, -1450), (7400, -1450), dxfattribs={"layer": L})
    # 主卧飘窗 7500~10800, y -600~0
    msp.add_line((7500, -600), (10800, -600), dxfattribs={"layer": L})
    msp.add_line((7500, -480), (10800, -480), dxfattribs={"layer": L})
    msp.add_line((7500, -600), (7500, -120), dxfattribs={"layer": L})
    msp.add_line((10800, -600), (10800, -120), dxfattribs={"layer": L})
    # 飘窗玻璃
    msp.add_line((7500, -240), (10800, -240), dxfattribs={"layer": "WINDOW"})
    msp.add_line((7500, -600), (7500, 0), dxfattribs={"layer": "WINDOW"})
    msp.add_line((10800, -600), (10800, 0), dxfattribs={"layer": "WINDOW"})
    # 北阳台 8300~11000, y 9600~11100
    msp.add_line((8300, 11100), (11000, 11100), dxfattribs={"layer": L})
    msp.add_line((8300, 11000), (11000, 11000), dxfattribs={"layer": L})
    msp.add_line((8300, 9720), (8300, 11100), dxfattribs={"layer": L})
    msp.add_line((11000, 9720), (11000, 11100), dxfattribs={"layer": L})
    msp.add_line((8300, 11050), (11000, 11050), dxfattribs={"layer": L})


# ═══════════ 轴线 ═══════════
def draw_axis(msp):
    yb, yt = -4200, 12200
    xl, xr = -4200, 15200
    for i, x in enumerate(GRID_X):
        msp.add_line((x, yb), (x, yt), dxfattribs={"layer": "AXIS"})
        msp.add_circle((x, yb - 900), 450, dxfattribs={"layer": "AXIS"})
        text(msp, AXL[i], x, yb - 900, 400, "AXIS")
    for i, y in enumerate(GRID_Y):
        msp.add_line((xl, y), (xr, y), dxfattribs={"layer": "AXIS"})
        msp.add_circle((xl - 900, y), 450, dxfattribs={"layer": "AXIS"})
        text(msp, AYL[i], xl - 900, y, 400, "AXIS")


# ═══════════ 标注（建筑斜线箭头）═══════════
TICK = 160


def dim_h(msp, x1, x2, y, y_from, val=None):
    """水平尺寸: 尺寸界线从 y_from 到 y+over，45°斜线箭头"""
    over = 250
    for x in (x1, x2):
        msp.add_line((x, y_from), (x, y + (over if y > y_from else -over)),
                     dxfattribs={"layer": "DIM"})
    msp.add_line((x1, y), (x2, y), dxfattribs={"layer": "DIM"})
    for x in (x1, x2):
        msp.add_line((x - TICK, y - TICK), (x + TICK, y + TICK),
                     dxfattribs={"layer": "DIM"})
    v = val if val is not None else int(round(abs(x2 - x1)))
    text(msp, str(v), (x1 + x2) / 2, y + 320, 320, "DIM",
         TextEntityAlignment.BOTTOM_CENTER)


def dim_v(msp, y1, y2, x, x_from, val=None):
    over = 250
    for y in (y1, y2):
        msp.add_line((x_from, y), (x + (over if x > x_from else -over), y),
                     dxfattribs={"layer": "DIM"})
    msp.add_line((x, y1), (x, y2), dxfattribs={"layer": "DIM"})
    for y in (y1, y2):
        msp.add_line((x - TICK, y - TICK), (x + TICK, y + TICK),
                     dxfattribs={"layer": "DIM"})
    v = val if val is not None else int(round(abs(y2 - y1)))
    t = msp.add_text(str(v), dxfattribs={"layer": "DIM", "height": 320})
    t.set_placement((x - 380, (y1 + y2) / 2), align=TextEntityAlignment.MIDDLE_CENTER)
    t.dxf.rotation = 90


def draw_dims(msp):
    # 南侧: 分段链 y=-2400, 总尺寸 y=-3100
    y_seg, y_tot = -2400, -3100
    prev = X0
    for x in GRID_X[1:]:
        dim_h(msp, prev, x, y_seg, -200)
        prev = x
    dim_h(msp, X0, X1, y_tot, y_seg)
    # 北侧: 分段链 y=11800
    prev = X0
    for x in GRID_X[1:]:
        dim_h(msp, prev, x, 11800, Y1)
        prev = x
    # 西侧: 分段链 x=-2400, 总 x=-3100
    prev = Y0
    for y in GRID_Y[1:]:
        dim_v(msp, prev, y, -2400, -200)
        prev = y
    dim_v(msp, Y0, Y1, -3100, -2400)
    # 东侧: 分段链 x=11800
    prev = Y0
    for y in GRID_Y[1:]:
        dim_v(msp, prev, y, 11800, Y1)
        prev = y
    # 阳台外凸
    dim_v(msp, -1500, 0, 2500, 3200, 1500)      # 南阳台深
    dim_v(msp, -600, 0, 11400, 10800, 600)      # 飘窗深
    dim_h(msp, 8300, 11000, 11600, 11100, 2700)  # 北阳台宽
    # 门窗洞口（主要几个）
    dim_h(msp, 3400, 7200, -800, -200, 3800)     # 客厅推拉门
    dim_h(msp, 7500, 10800, -1800, -700, 3300)   # 飘窗宽


# ═══════════ 家具 ═══════════
def draw_furniture(msp):
    L = "FURNITURE"
    # 客厅 (3260,120)-(7380,3880)
    rect(msp, 3320, 1200, 4220, 3300, L)          # 三人沙发 900x2100 靠西墙
    rect(msp, 3320, 1200, 4220, 1450, L)          # 沙发靠背分隔
    rect_c(msp, 5400, 2200, 1200, 600, L)         # 茶几
    rect(msp, 6880, 1400, 7280, 3200, L)          # 电视柜 400x1800 靠东墙
    rect(msp, 4320, 3320, 5820, 3800, L)          # 地毯/矮柜(南)

    # 主卧 (7520,120)-(10880,3880)
    rect(msp, 8200, 1780, 10000, 3780, L)         # 双人床 1800x2000
    rect(msp, 8200, 3480, 10000, 3780, L)         # 床头
    rect(msp, 7650, 3380, 8150, 3780, L)          # 床头柜 左
    rect(msp, 10050, 3380, 10550, 3780, L)        # 床头柜 右
    rect(msp, 7580, 1000, 8180, 3000, L)          # 衣柜 600x2000 靠西墙
    rect(msp, 10380, 1200, 10830, 2000, L)        # 梳妆台靠东墙

    # 次卧 (120,120)-(3140,3880)
    rect(msp, 900, 1780, 2100, 3780, L)           # 单人床 1200x2000
    rect(msp, 900, 3480, 2100, 3780, L)           # 床头
    rect(msp, 180, 1000, 780, 2200, L)            # 书桌 600x1200 靠西墙
    rect(msp, 2480, 1000, 3080, 2500, L)          # 衣柜 600x1500 靠东墙

    # 书房 (120,5020)-(3140,9480)
    rect(msp, 180, 6400, 880, 7800, L)            # 书桌 700x1400 靠西墙
    rect(msp, 1130, 9010, 3130, 9360, L)          # 书柜 2000x350 靠北墙
    rect(msp, 1940, 5600, 3140, 7600, L)          # 单人床 1200x2000 靠东墙
    rect(msp, 1940, 7300, 3140, 7600, L)          # 床头

    # 餐厅 (3260,5020)-(6180,7680)
    rect_c(msp, 4720, 6250, 1400, 800, L)         # 餐桌
    for dx in (-560, 0, 560):
        rect_c(msp, 4720 + dx, 5520, 450, 500, L)  # 南侧椅
        rect_c(msp, 4720 + dx, 6980, 450, 500, L)  # 北侧椅
    rect_c(msp, 4720, 7380, 1500, 400, L)         # 餐边柜 靠北墙

    # 卫生间 (6360,5020)-(8240,7680)
    rect(msp, 6420, 6600, 6920, 7400, L)          # 洗手台 500x800 西
    rect_c(msp, 7400, 7230, 400, 700, L)          # 坐便器 靠北墙
    rect(msp, 7240, 5080, 8140, 5980, L)          # 淋浴 900x900
    msp.add_line((7240, 5080), (8140, 5980), dxfattribs={"layer": L})
    msp.add_line((7240, 5980), (8140, 5080), dxfattribs={"layer": L})

    # 厨房 (8360,5020)-(10880,7680)
    rect(msp, 8420, 7080, 10820, 7680, L)         # 北墙台面 600 深
    rect(msp, 10280, 5260, 10880, 7080, L)        # 东墙台面 600 深
    rect_c(msp, 10560, 7380, 560, 420, L)         # 水槽
    for cx in (8700, 9100):                        # 灶眼
        msp.add_circle((cx, 7380), 130, dxfattribs={"layer": L})
    rect(msp, 8420, 5260, 9020, 5860, L)          # 冰箱 600x600

    # 玄关 (3260,7920)-(10880,9480)
    rect(msp, 3320, 8100, 3670, 9300, L)          # 鞋柜 350x1200 靠西墙
    rect(msp, 6100, 8020, 10820, 8370, L)         # 玄关柜/储物 靠南侧

    # 南阳台 (3200,-1400)-(7400,0)
    rect(msp, 6650, -1350, 7250, -750, L)         # 洗衣机 600x600
    msp.add_circle((6950, -1050), 220, dxfattribs={"layer": L})


# ═══════════ 文字 ═══════════
def draw_labels(msp):
    for name, x1, y1, x2, y2 in ROOMS:
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        area = (x2 - x1) * (y2 - y1) / 1e6
        text(msp, name, cx, cy + 220, 520, "TEXT")
        text(msp, f"{area:.2f}m²", cx, cy - 420, 320, "TEXT")

    # 阳台/飘窗名
    text(msp, "南阳台", 5300, -1100, 380, "TEXT")
    text(msp, "北阳台", 9650, 10550, 380, "TEXT")
    text(msp, "飘窗", 9150, -330, 300, "TEXT")

    # 门窗编号
    for s, x, y in [("D1", 4800, 10000), ("D2", 2900, 3500), ("D3", 8300, 3600),
                    ("D4", 1800, 5300), ("D5", 6850, 5300), ("D6", 8900, 5300)]:
        text(msp, s, x, y, 260, "TEXT")
    for s, x, y in [("W1", -1600, 2150), ("W2", -1600, 6550), ("W3", 4000, 10100),
                    ("W4", 9200, 10100), ("W5", 12100, 5500), ("W6", 9150, -750)]:
        text(msp, s, x, y, 260, "TEXT")
    for s, x, y in [("SD1", 5300, -350), ("SD2", 9200, 9200)]:
        pass  # 推拉门编号省略，避免与W编号重叠

    # 面积合计
    net = sum((x2 - x1) * (y2 - y1) for _, x1, y1, x2, y2 in ROOMS) / 1e6
    text(msp, f"套内使用面积合计: {net:.2f}m²", 5500, 12000, 380, "TEXT")


# ═══════════ 图框/标题栏 ═══════════
def draw_frame(msp):
    fx0, fy0 = 0, 0
    fx1, fy1 = fx0 + FRAME_W, fy0 + FRAME_H
    for a, b in [((fx0, fy0), (fx1, fy0)), ((fx1, fy0), (fx1, fy1)),
                 ((fx1, fy1), (fx0, fy1)), ((fx0, fy1), (fx0, fy0))]:
        msp.add_line(a, b, dxfattribs={"layer": "FRAME"})

    ix0, iy0 = fx0 + MARGIN, fy0 + MARGIN
    ix1, iy1 = fx1 - MARGIN, fy1 - MARGIN
    for a, b in [((ix0, iy0), (ix1, iy0)), ((ix1, iy0), (ix1, iy1)),
                 ((ix1, iy1), (ix0, iy1)), ((ix0, iy1), (ix0, iy0))]:
        msp.add_line(a, b, dxfattribs={"layer": "FRAME"})

    # ── 标题栏 右下 (18000 x 7000) ──
    tw, th = 18000, 7000
    tx0, ty0 = ix1 - tw, iy0
    tx1, ty1 = ix1, iy0 + th
    for a, b in [((tx0, ty0), (tx1, ty0)), ((tx1, ty0), (tx1, ty1)),
                 ((tx1, ty1), (tx0, ty1)), ((tx0, ty1), (tx0, ty0))]:
        msp.add_line(a, b, dxfattribs={"layer": "FRAME"})
    rows = 4
    rh = th / rows
    for i in range(1, rows):
        y = ty0 + rh * i
        msp.add_line((tx0, y), (tx1, y), dxfattribs={"layer": "FRAME"})
    cw = tw / 3
    for i in range(1, 3):
        x = tx0 + cw * i
        msp.add_line((x, ty0), (x, ty1), dxfattribs={"layer": "FRAME"})

    def cell(s, col, row, h=320):
        x = tx0 + cw * col + cw / 2
        y = ty0 + rh * row + rh / 2
        text(msp, s, x, y, h, "TEXT")

    cell("工程名称  110㎡住宅户型", 1, 3, 360)
    cell("图  名  户型平面布置图", 1, 2, 360)
    cell("设计号 RES-110", 0, 1)
    cell("比  例  1:100", 1, 1)
    cell("单  位  mm", 2, 1)
    cell("制  图  CAD绘图工程师", 0, 0)
    cell("审  核  (待审)", 1, 0)
    cell("日  期  2025", 2, 0)

    # ── 指北针 右上 ──
    nx, ny, nr = ix1 - 4200, iy1 - 4200, 1600
    msp.add_circle((nx, ny), nr, dxfattribs={"layer": "FRAME"})
    msp.add_line((nx, ny - nr), (nx, ny + nr), dxfattribs={"layer": "FRAME"})
    msp.add_line((nx - 420, ny + nr - 700), (nx, ny + nr), dxfattribs={"layer": "FRAME"})
    msp.add_line((nx + 420, ny + nr - 700), (nx, ny + nr), dxfattribs={"layer": "FRAME"})
    text(msp, "N", nx, ny + nr + 600, 450, "TEXT")

    # ── 图名（户型正下方）──
    tx = 23000
    text(msp, "户型平面布置图", tx, 7400, 900, "TEXT")
    text(msp, "1:100", tx, 6400, 450, "TEXT")
    msp.add_line((tx - 7000, 5900), (tx + 7000, 5900), dxfattribs={"layer": "FRAME"})
    msp.add_line((tx - 7000, 5650), (tx + 7000, 5650), dxfattribs={"layer": "FRAME"})

    # ── 技术要求 左下 ──
    lines = [
        "1. 本图为住宅平面布置图，尺寸单位除注明外均为毫米(mm)，标高以米(m)计。",
        "2. 图中标注均为轴线间尺寸；括号内数值为净尺寸，施工以结构施工图为准。",
        "3. 外墙厚240mm，承重内墙厚200mm，普通内墙厚120mm，未注明者均为120mm。",
        "4. 门窗洞口为结构预留洞口，门窗加工尺寸按实测洞口扣减安装缝隙确定。",
        "5. 南阳台栏板高1100mm，主卧飘窗窗台高450mm，均须满足现行规范要求。",
        "6. 室内家具均为示意布置，仅表达功能分区，不代表实际订购家具尺寸。",
        "7. 本图仅为方案阶段参考图纸，最终以施工图及现行国家规范为准。",
        "8. 未注公差按 GB/T 1804-m（中等精度）执行。",
    ]
    lh = 700
    bx, by = ix0 + 1200, iy0 + 900
    text(msp, "技术要求", bx, by + len(lines) * lh + 300, 480, "TEXT",
         TextEntityAlignment.BOTTOM_LEFT)
    for i, s in enumerate(lines):
        text(msp, s, bx, by + (len(lines) - 1 - i) * lh, 300, "TEXT",
             TextEntityAlignment.BOTTOM_LEFT)


# ═══════════ 主流程 ═══════════
def main():
    os.makedirs(os.path.dirname(OUT_DXF), exist_ok=True)
    doc = ezdxf.new("R2010", setup=True)
    setup(doc)
    msp = doc.modelspace()

    draw_walls(msp)
    draw_wall_hatch(msp)
    draw_openings(msp)
    draw_balconies(msp)
    draw_axis(msp)
    draw_furniture(msp)
    draw_dims(msp)
    draw_labels(msp)

    # 模型整体移入图框
    for e in list(msp):
        try:
            e.translate(DX, DY, 0)
        except Exception:
            pass

    draw_frame(msp)

    doc.saveas(OUT_DXF)
    print(f"[OK] {OUT_DXF}  {os.path.getsize(OUT_DXF)} bytes")
    cnt = Counter(e.dxf.layer for e in msp)
    print("[图层统计]")
    for k in sorted(cnt):
        print(f"  {k:12s}{cnt[k]:5d}")
    print(f"  0 层图元: {cnt.get('0', 0)}")
    print(f"  图元总数: {len(list(msp))}")
    net = sum((x2 - x1) * (y2 - y1) for _, x1, y1, x2, y2 in ROOMS) / 1e6
    print(f"  套内净面积合计: {net:.2f} m2")


if __name__ == "__main__":
    main()
