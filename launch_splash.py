# -*- coding: utf-8 -*-
"""
智能体集群 — 启动中特效窗口
由 start_tray.py 在启动流程里拉起（pythonw，无控制台）。
通过轮询 .startup_status 文件实时显示启动进度：
    step|<文案>     -> 普通步骤（持续加载动画）
    done|<文案>     -> 收尾：短暂展示文案 + 成功动画后淡出关闭
    文件被删除      -> 托盘已退出，立即关闭

交互：ESC / 右键 / 右上角 × = 立即关闭；硬超时 120s 自动关闭。
"""
import math
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUS_FILE = ROOT / ".startup_status"
ICON_PATHS = [
    ROOT / "frontend" / "public" / "icon.png",
    ROOT / "frontend" / "public" / "icon.jpg",
    ROOT / "1791461860623.webp.jpg",
]

W, H = 520, 340
BG_OUTER = "#070b18"
BG_CARD = "#0f1626"
CARD_LINE = "#243450"
ACCENT = (0x5b, 0x8c, 0xff)
ACCENT2 = (0xa7, 0x8b, 0xfa)
OK = "#3ddc97"
TEXT = "#eaf1ff"
MUTED = "#94a8cf"
# 透明色键：窗口背景与画布用该色填充，配合 -transparentcolor 让四角变透明 -> 圆角窗口。
# 选一个不会出现在卡片/文字中的极暗色，避免把卡片“打孔”。
KEY_COLOR = "#010103"
CARD_MARGIN = 4          # 卡片到窗口边的留白（这部分透明）
CARD_RADIUS = 26         # 圆角半径


def _mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _rgb(h):
    """把 '#rrggbb' 转成 (r,g,b) 元组（_mix 只接受元组）。"""
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(c):
    return "#%02x%02x%02x" % c


class Splash(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AgentCluster 启动中")
        self.overrideredirect(True)
        self.attributes("-topmost", False)
        try:
            self.attributes("-toolwindow", True)   # 不进任务栏
        except Exception:
            pass
        # 圆角窗口：背景填透明色键，配合 -transparentcolor 让四角镂空
        self.configure(bg=KEY_COLOR)
        try:
            self.attributes("-transparentcolor", KEY_COLOR)
        except Exception:
            pass
        try:
            self.attributes("-alpha", 0.02)
        except Exception:
            pass
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

        self.cv = tk.Canvas(self, width=W, height=H, bg=KEY_COLOR,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.frame = 0
        self.angle = 0.0
        self.step = "正在初始化启动器..."
        self.done = False
        self._closing = False
        self._close_at = None
        self._photo = None
        self.start_ts = time.time()

        self._load_icon()
        # 静态层：卡片、关闭×（不参与逐帧重绘，保留交互绑定）
        try:
            self._draw_static()
        except Exception:
            pass  # 绘制失败也不影响窗口存在
        self._setup_bindings()
        self.after(24, self._fade_in)
        self.after(60, self._tick)
        self.after(160, self._poll)
        self.after(120000, self._timeout_close)

    # ---------- 图标 ----------
    def _load_icon(self):
        for p in ICON_PATHS:
            if not p.exists():
                continue
            try:
                from PIL import Image, ImageTk
                img = Image.open(p).convert("RGBA").resize((72, 72), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(img)
                return
            except Exception:
                continue

    # ---------- 静态层 ----------
    def _rrect(self, x1, y1, x2, y2, r, **kw):
        self.cv.create_polygon(
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
            smooth=True, **kw
        )

    def _draw_static(self):
        # 卡片（四周留 CARD_MARGIN 给透明色键镂空 -> 圆角窗口）
        self._rrect(CARD_MARGIN, CARD_MARGIN, W - CARD_MARGIN, H - CARD_MARGIN,
                    CARD_RADIUS, fill=BG_CARD, outline=CARD_LINE, width=1)
        # 顶部渐变光带
        card_rgb = _rgb(BG_CARD)
        for i in range(46):
            t = i / 45
            self.cv.create_line(40, 34 + i, W - 40, 34 + i,
                                fill=_hex(_mix(ACCENT, card_rgb, 0.45 + t * 0.55)))
        # 关闭 ×（静态、可点击）
        self._x_btn = self.cv.create_text(W - 24, 22, text="×", fill=MUTED,
                                          font=("Segoe UI", 15, "bold"))

    def _setup_bindings(self):
        self.bind("<Escape>", lambda e: self._close_now())
        self.cv.bind("<Button-3>", lambda e: self._close_now())
        self.cv.tag_bind(self._x_btn, "<Button-1>", lambda e: self._close_now())
        self.cv.tag_bind(self._x_btn, "<Enter>",
                         lambda e: self.cv.itemconfig(self._x_btn, fill="#ff9a9a"))
        self.cv.tag_bind(self._x_btn, "<Leave>",
                         lambda e: self.cv.itemconfig(self._x_btn, fill=MUTED))

    # ---------- 淡入 / 淡出 ----------
    def _fade_in(self):
        if self._closing:
            return
        a = min(0.97, (time.time() - self.start_ts) / 0.32)
        try:
            self.attributes("-alpha", max(0.02, a))
        except Exception:
            pass
        if a < 0.97:
            self.after(18, self._fade_in)

    def _close_now(self):
        if self._closing:
            return
        self._closing = True
        self._fade_out(0.97)

    def _fade_out(self, a):
        a = max(0.0, a - 0.13)
        try:
            self.attributes("-alpha", a)
        except Exception:
            pass
        if a <= 0.0:
            try:
                self.destroy()
            except Exception:
                pass
            return
        self.after(16, lambda: self._fade_out(a))

    def _timeout_close(self):
        if not self._closing:
            self._close_now()

    # ---------- 状态轮询 ----------
    def _poll(self):
        if self._closing:
            return
        try:
            if not STATUS_FILE.exists():
                self._close_now()      # 托盘退出 → 立即收起
                return
            raw = STATUS_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            raw = ""
        if raw:
            kind, _, text = raw.partition("|")
            text = text.strip() or raw
            if kind == "done":
                self.done = True
                self.step = text
                if self._close_at is None:
                    self._close_at = time.time() + 1.4
            elif kind == "step":
                self.done = False
                self._close_at = None
                self.step = text
        if self._close_at and time.time() >= self._close_at:
            self._close_now()
            return
        self.after(160, self._poll)

    # ---------- 动画 ----------
    def _tick(self):
        if self._closing:
            return
        self.frame += 1
        self.angle = (self.angle + 6.0) % 360.0
        cv = self.cv
        cv.delete("dyn")
        try:
            self._draw_dynamic()
        except Exception:
            pass  # 单帧绘制失败不中断动画，避免窗口莫名消失
        self.after(16, self._tick)

    def _draw_dynamic(self):
        cv, T = self.cv, ("dyn",)
        cx = W // 2

        # 呼吸微光边框
        g = 0.5 + 0.5 * math.sin(self.frame * 0.05)
        self._rrect(CARD_MARGIN, CARD_MARGIN, W - CARD_MARGIN, H - CARD_MARGIN,
                    CARD_RADIUS, outline=_hex(_mix(ACCENT, ACCENT2, g)), width=1)

        # 图标 / 字标
        if self._photo:
            cv.create_image(cx, 96, image=self._photo, tags=T)
        else:
            cv.create_text(cx, 96, text="✦", fill=_hex(ACCENT),
                           font=("Segoe UI", 34, "bold"), tags=T)

        cv.create_text(cx, 156, text="智能体集群", fill=TEXT,
                       font=("Microsoft YaHei UI", 20, "bold"), tags=T)
        cv.create_text(cx, 180, text="AgentCluster", fill=MUTED,
                       font=("Segoe UI", 10), tags=T)

        # 旋转光环
        self._draw_ring(cx, 224, 25, T)

        # 进度条
        bar_y = 272
        cv.create_rectangle(84, bar_y, W - 84, bar_y + 7, fill="#1a2540", outline="", tags=T)
        if self.done:
            cv.create_rectangle(84, bar_y, W - 84, bar_y + 7, fill=OK, outline="", tags=T)
        else:
            span = W - 168
            head = (self.frame * 5) % (span + 130) - 65
            x0 = 84 + max(0, head)
            x1 = 84 + min(span, head + 100)
            if x1 > x0:
                for i in range(0, int(x1 - x0), 2):
                    t = i / max(1.0, (x1 - x0))
                    cv.create_rectangle(x0 + i, bar_y, x0 + i + 2, bar_y + 7,
                                        fill=_hex(_mix(ACCENT, ACCENT2, t)), outline="", tags=T)

        # 步骤文案 + 成功标记
        cv.create_text(cx, bar_y + 28, text=self.step or "正在启动...",
                       fill=TEXT, font=("Microsoft YaHei UI", 11), tags=T)
        if self.done:
            cv.create_text(W - 34, bar_y + 28, text="✓", fill=OK,
                           font=("Segoe UI", 12, "bold"), tags=T)

        cv.create_text(cx, H - 16, text="首次启动需几秒，正在加载服务…",
                       fill=MUTED, font=("Microsoft YaHei UI", 9), tags=T)

    def _draw_ring(self, cx, cy, r, T):
        self.cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                            outline="#1a2540", width=5, tags=T)
        n = 12
        for i in range(n):
            a0 = self.angle + i * (360.0 / n)
            a1 = a0 + (360.0 / n) - 6
            col = OK if self.done else _hex(_mix(ACCENT, ACCENT2, i / (n - 1)))
            self.cv.create_arc(cx - r, cy - r, cx + r, cy + r,
                               start=a0, extent=(a1 - a0), style="arc",
                               outline=col, width=5, tags=T)
        p = 0.5 + 0.5 * math.sin(self.frame * 0.12)
        pr = 4 + p * 2.5
        self.cv.create_oval(cx - pr, cy - pr, cx + pr, cy + pr,
                            fill=(OK if self.done else _hex(ACCENT)), outline="", tags=T)


def main():
    Splash().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 启动窗口自身异常绝不影响主程序；记录到崩溃日志便于排查
        try:
            import traceback
            with open(ROOT / "tray_crash.log", "a", encoding="utf-8") as f:
                f.write("===== launch_splash 崩溃 =====\n")
                f.write(traceback.format_exc())
                f.write("\n\n")
        except Exception:
            pass
        sys.exit(0)
