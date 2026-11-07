#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成户型局部放大 PNG，用于细节自检"""

import ezdxf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import math
import os

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

IN_DXF = r"drawings/RES-110/户型平面布置图.dxf"
OUT_PNG = r"drawings/RES-110/户型平面布置图_局部.png"

LAYER_STYLE = {
    "WALL-EXT":  ("#FFD700", 2.2),
    "WALL-INT":  ("#EEEEEE", 1.6),
    "WALL-LOAD": ("#EEEEEE", 1.8),
    "WINDOW":    ("#00FFFF", 1.2),
    "DOOR":      ("#00FFFF", 1.2),
    "BALCONY":   ("#4488FF", 1.4),
    "AXIS":      ("#FF5555", 0.6),
    "FURNITURE": ("#55EE55", 0.9),
    "DIM":       ("#99FF99", 0.7),
    "TEXT":      ("#66CCFF", 0.5),
    "HATCH":     ("#777777", 0.45),
    "FRAME":     ("#FFFFFF", 1.0),
}


def render(msp, ax):
    for e in msp:
        layer = e.dxf.layer
        color, lw = LAYER_STYLE.get(layer, ("#FFFFFF", 0.5))
        t = e.dxftype()
        try:
            if t == 'LINE':
                s, en = e.dxf.start, e.dxf.end
                ax.plot([s[0], en[0]], [s[1], en[1]], color=color,
                        lw=lw, solid_capstyle='round', zorder=2)
            elif t == 'LWPOLYLINE':
                pts = list(e.get_points())
                xs = [p[0] for p in pts] + ([pts[0][0]] if e.closed else [])
                ys = [p[1] for p in pts] + ([pts[0][1]] if e.closed else [])
                ax.plot(xs, ys, color=color, lw=lw, zorder=2)
            elif t == 'CIRCLE':
                c, r = e.dxf.center, e.dxf.radius
                ax.add_patch(plt.Circle((c[0], c[1]), r, fill=False,
                                        color=color, lw=lw, zorder=2))
            elif t == 'ARC':
                c, r = e.dxf.center, e.dxf.radius
                a1 = math.radians(e.dxf.start_angle)
                a2 = math.radians(e.dxf.end_angle)
                if a2 <= a1:
                    a2 += 2 * math.pi
                ang = np.linspace(a1, a2, 60)
                ax.plot(c[0] + r * np.cos(ang), c[1] + r * np.sin(ang),
                        color=color, lw=lw, zorder=2)
            elif t == 'TEXT':
                p = e.dxf.insert
                h = getattr(e.dxf, 'height', 350)
                ax.text(p[0], p[1], e.dxf.text, color=color,
                        fontsize=h * 0.028, ha='center', va='center', zorder=3)
        except Exception:
            pass


def main():
    doc = ezdxf.readfile(IN_DXF)
    msp = doc.modelspace()

    # 只渲染户型区域（平移后 X:13300~32700, Y:7800~24200）
    x0, x1 = 12500, 34000
    y0, y1 = 7000, 25500

    w = (x1 - x0) / 1000.0
    h = (y1 - y0) / 1000.0
    fig, ax = plt.subplots(figsize=(w * 1.35, h * 1.35))
    ax.set_facecolor('#141414')
    fig.patch.set_facecolor('#141414')

    render(msp, ax)

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect('equal')
    ax.axis('off')
    plt.tight_layout()
    plt.savefig(OUT_PNG, dpi=170, facecolor=fig.get_facecolor())
    plt.close()
    print(f"[OK] {OUT_PNG}  {os.path.getsize(OUT_PNG)} bytes")


if __name__ == "__main__":
    main()
