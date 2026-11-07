#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 DXF 转为 PDF 预览图 (使用 matplotlib)"""

import ezdxf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import os
import matplotlib.font_manager as fm

# 尝试设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

OUT_PDF = r"drawings/RES-110/户型平面布置图.pdf"
IN_DXF = r"drawings/RES-110/户型平面布置图.dxf"

# 图层颜色映射 (ACI → matplotlib颜色)
ACI_COLORS = {
    1: '#FF0000',   # 红
    2: '#FFD700',   # 黄
    3: '#00FF00',   # 绿
    4: '#00FFFF',   # 青
    5: '#0000FF',   # 蓝
    7: '#FFFFFF',   # 白
    8: '#808080',   # 灰
}

# 图层→ACI色号映射
LAYER_ACI = {
    "WALL-EXT":   2,
    "WALL-INT":   7,
    "WALL-LOAD":  7,
    "WINDOW":     4,
    "DOOR":       4,
    "BALCONY":    5,
    "AXIS":       1,
    "FURNITURE":  3,
    "DIM":        3,
    "TEXT":       4,
    "HATCH":      8,
    "FRAME":      7,
}


def main():
    doc = ezdxf.readfile(IN_DXF)
    msp = doc.modelspace()

    fig, ax = plt.subplots(1, 1, figsize=(16.93, 11.96))  # A2横向 mm→inch
    ax.set_facecolor('#1a1a1a')  # 深色背景
    fig.patch.set_facecolor('#1a1a1a')

    # 背景色：仿AutoCAD深色
    for entity in msp:
        layer = entity.dxf.layer
        aci = LAYER_ACI.get(layer, 7)
        color = ACI_COLORS.get(aci, '#FFFFFF')

        # 白色线在深色背景上用浅灰
        if color == '#FFFFFF':
            color = '#E0E0E0'

        lw = 0.5
        if layer.startswith("WALL"):
            lw = 1.0
        elif layer == "FRAME":
            lw = 1.2
        elif layer == "AXIS":
            lw = 0.3
            color = '#FF6666'
        elif layer == "HATCH":
            lw = 0.2
            color = '#666666'
        elif layer == "DIM":
            lw = 0.4
            color = '#88FF88'
        elif layer == "FURNITURE":
            lw = 0.4
            color = '#88FF88'
        elif layer == "TEXT":
            lw = 0.3
            color = '#88DDFF'
        elif layer in ("DOOR", "WINDOW"):
            lw = 0.6

        try:
            dxftype = entity.dxftype()
            if dxftype == 'LINE':
                start = entity.dxf.start
                end = entity.dxf.end
                ax.plot([start[0], end[0]], [start[1], end[1]],
                        color=color, linewidth=lw, solid_capstyle='butt')
            elif dxftype == 'LWPOLYLINE':
                points = list(entity.get_points())
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                if entity.closed:
                    xs.append(xs[0])
                    ys.append(ys[0])
                ax.plot(xs, ys, color=color, linewidth=lw)
            elif dxftype == 'CIRCLE':
                center = entity.dxf.center
                radius = entity.dxf.radius
                circle = plt.Circle((center[0], center[1]), radius,
                                    fill=False, color=color, linewidth=lw)
                ax.add_patch(circle)
            elif dxftype == 'ARC':
                center = entity.dxf.center
                radius = entity.dxf.radius
                import math
                a1 = math.radians(entity.dxf.start_angle)
                a2 = math.radians(entity.dxf.end_angle)
                import numpy as np
                angles = np.linspace(a1, a2, 50)
                xs = center[0] + radius * np.cos(angles)
                ys = center[1] + radius * np.sin(angles)
                ax.plot(xs, ys, color=color, linewidth=lw)
            elif dxftype == 'TEXT':
                pos = entity.dxf.insert
                text = entity.dxf.text
                h = entity.dxf.height if entity.dxf.hasattr('height') else 350
                fontsize = h / 200  # 缩放
                ax.text(pos[0], pos[1], text, color=color,
                        fontsize=max(3, min(fontsize, 10)),
                        ha='center', va='center')
        except Exception as e:
            pass

    # 设置坐标范围
    ax.set_xlim(-10000, 53000)
    ax.set_ylim(-11000, 36000)
    ax.set_aspect('equal')
    ax.axis('off')

    plt.tight_layout()
    plt.savefig(OUT_PDF, format='pdf', dpi=150,
                facecolor=fig.get_facecolor())
    out_png = OUT_PDF.replace('.pdf', '.png')
    plt.savefig(out_png, format='png', dpi=110,
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"[OK] PDF saved: {OUT_PDF}")
    print(f"[INFO] File size: {os.path.getsize(OUT_PDF)} bytes")
    print(f"[OK] PNG saved: {out_png}")
    print(f"[INFO] File size: {os.path.getsize(out_png)} bytes")


if __name__ == "__main__":
    main()
