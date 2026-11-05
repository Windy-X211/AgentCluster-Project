#!/usr/bin/env pythonw
"""智能体集群 · 市场管理（独立程序）

独立启动的 Tkinter 桌面程序，不依赖主程序后端：
- 浏览 / 搜索 / 按分类与安装状态筛选 market/ 中的资源
- 把市场资源安装到本机（用户数据目录 %LOCALAPPDATA%\\AgentCluster）
- 把本机已有资源发布到市场
- 删除市场条目（不影响本机已安装副本）
- 打开市场目录

直接复用 ``backend/api/market.py`` 的安装 / 发布 / 删除逻辑，保证与主程序一致。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    import core.store as store
    from api import market
except Exception as _e:  # 依赖缺失时给出可读提示，而不是闪退
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror("市场管理启动失败", f"无法加载市场模块：\n{_e}")
    raise

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
ICON_FILE = PROJECT_ROOT / "agentcluster.ico"

KIND_ICON = {
    "agent": "🤖", "canvas": "🗺", "background": "🎨",
    "skill": "🧠", "plugin": "🔧", "extension": "🧩",
}
PUBLISH_KINDS = [(k, label) for k, label in market.KIND_ORDER if k != "all"]


def _err_text(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    return str(detail or e)


class MarketManager(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("智能体集群 · 市场管理")
        self.geometry("980x640")
        self.minsize(820, 520)
        if ICON_FILE.exists():
            try:
                self.iconbitmap(str(ICON_FILE))
            except Exception:
                pass

        self.kind_keys: list[str] = []
        self.kind_labels: list[str] = []
        self.items: list[dict] = []
        self.item_map: dict[str, dict] = {}
        self._search_job: str | None = None

        self.keyword = tk.StringVar()
        self.uninstalled_only = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪")

        self._build_ui()
        self.after(120, self._bootstrap)

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", font=FONT_SMALL, rowheight=27)
        style.configure("Treeview.Heading", font=FONT_SMALL)
        style.configure("TLabel", font=FONT)
        style.configure("TButton", font=FONT)

        # 顶部标题条
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="🛒 市场管理", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Label(top, text="浏览、安装、发布与删除市场资源", foreground="#777").pack(side="left", padx=10)
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")

        body = ttk.Frame(self, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)

        # 左侧分类
        left = ttk.Frame(body, width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="资源分类", foreground="#777", font=FONT_SMALL).pack(anchor="w", pady=(0, 4))
        self.kind_list = tk.Listbox(
            left, exportselection=False, font=FONT, activestyle="none",
            borderwidth=1, relief="solid", highlightthickness=0,
        )
        self.kind_list.pack(fill="both", expand=True)
        self.kind_list.bind("<<ListboxSelect>>", lambda _e: self.refresh())

        # 右侧内容
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        search_row = ttk.Frame(right)
        search_row.pack(fill="x", pady=(0, 8))
        ttk.Label(search_row, text="搜索").pack(side="left")
        self.search_entry = ttk.Entry(search_row, textvariable=self.keyword)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        ttk.Checkbutton(
            search_row, text="只看未安装", variable=self.uninstalled_only,
            command=self.render_items,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(search_row, text="清除", command=self._clear_search).pack(side="left")

        # 资源列表
        columns = ("name", "kind", "version", "author", "installed", "desc")
        tree_wrap = ttk.Frame(right)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", selectmode="browse")
        headings = {
            "name": ("名称", 190), "kind": ("类型", 80), "version": ("版本", 70),
            "author": ("作者", 110), "installed": ("状态", 70), "desc": ("说明", 300),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=50,
                             anchor="w" if col in ("name", "author", "desc") else "center")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("installed", foreground="#2e8b57")
        self.tree.bind("<Double-1>", lambda _e: self.install_selected())

        # 操作按钮
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="下载安装", command=self.install_selected).pack(side="left")
        ttk.Button(actions, text="发布到市场", command=self.open_publish).pack(side="left", padx=8)
        ttk.Button(actions, text="删除市场条目", command=self.delete_selected).pack(side="left")
        ttk.Button(actions, text="打开目录", command=self.open_selected_dir).pack(side="left", padx=8)
        ttk.Button(actions, text="打开市场根目录", command=lambda: self._open_path(market.MARKET_ROOT)).pack(side="left")

        # 状态栏
        status_bar = ttk.Frame(self, padding=(12, 6, 12, 8))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, textvariable=self.status, foreground="#666", font=FONT_SMALL).pack(side="left")

        self.bind("<F5>", lambda _e: self.refresh())
        self.bind("<Return>", lambda _e: self.install_selected())

    # ---------------- 数据 ----------------
    def _bootstrap(self) -> None:
        self.status.set("正在初始化数据目录…")
        self.update_idletasks()
        try:
            store.init_store()
        except Exception as e:
            messagebox.showerror("初始化失败", f"无法初始化用户数据目录：\n{e}")
        self.refresh()

    def refresh(self) -> None:
        kind = self._current_kind()
        try:
            data = market.list_kinds()
            self._fill_kinds(data.get("kinds", []), kind)
            items = market.list_items("" if kind == "all" else kind, self.keyword.get().strip())
            self.items = items.get("items", [])
            self.render_items()
        except Exception as e:
            self.status.set(f"加载失败：{_err_text(e)}")
            messagebox.showerror("加载失败", _err_text(e))

    def _fill_kinds(self, kinds: list[dict], keep: str) -> None:
        self.kind_list.delete(0, "end")
        self.kind_keys, self.kind_labels = [], []
        for k in kinds:
            key = k.get("key", "all")
            label = k.get("label", key)
            self.kind_keys.append(key)
            self.kind_labels.append(f"{label}  ({k.get('count', 0)})")
            self.kind_list.insert("end", self.kind_labels[-1])
        if keep in self.kind_keys:
            idx = self.kind_keys.index(keep)
            self.kind_list.selection_set(idx)
            self.kind_list.activate(idx)

    def _current_kind(self) -> str:
        sel = self.kind_list.curselection()
        if sel and sel[0] < len(self.kind_keys):
            return self.kind_keys[sel[0]]
        return "all"

    def render_items(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.item_map.clear()
        visible = [m for m in self.items
                   if not (self.uninstalled_only.get() and m.get("installed"))]
        for m in visible:
            key = f"{m.get('kind')}:{m.get('id')}"
            self.item_map[key] = m
            desc = (m.get("description") or "").replace("\n", " ").strip()
            if len(desc) > 90:
                desc = desc[:90] + "…"
            kind_label = market.KIND_LABEL.get(m.get("kind"), m.get("kind", ""))
            values = (
                f"{KIND_ICON.get(m.get('kind'), '📦')} {m.get('name', '')}",
                kind_label,
                m.get("version", ""),
                m.get("author", ""),
                "已安装" if m.get("installed") else "未安装",
                desc,
            )
            tags = ("installed",) if m.get("installed") else ()
            self.tree.insert("", "end", iid=key, values=values, tags=tags)
        installed = sum(1 for m in self.items if m.get("installed"))
        self.status.set(f"共 {len(self.items)} 项 · 已安装 {installed} 项 · 当前显示 {len(visible)} 项")

    def _selected_item(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.item_map.get(sel[0])

    # ---------------- 搜索 ----------------
    def _on_search_key(self, _event=None) -> None:
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(260, self.refresh)

    def _clear_search(self) -> None:
        self.keyword.set("")
        self.refresh()

    # ---------------- 操作 ----------------
    def install_selected(self) -> None:
        m = self._selected_item()
        if not m:
            messagebox.showinfo("提示", "请先在列表中选择一个资源")
            return
        if m.get("installed") and m.get("kind") in ("skill", "plugin", "background", "extension"):
            messagebox.showinfo("已安装", f"「{m.get('name')}」已安装，无需重复下载。")
            return
        if not messagebox.askyesno("确认下载", f"把「{m.get('name')}」安装到本机？"):
            return
        self.status.set(f"正在安装：{m.get('name')} …")
        self.update_idletasks()
        try:
            r = market.install({"kind": m.get("kind"), "id": m.get("id")})
            tip = f"已安装：{r.get('name') or m.get('name')}"
            if r.get("kind") == "canvas" and r.get("created_agents"):
                tip += f"（同时创建智能体：{'、'.join(r['created_agents'])}）"
            if r.get("kind") == "extension" and r.get("need_restart"):
                tip += "，含后端服务，需重启主程序后端后生效"
            self.status.set(tip)
            messagebox.showinfo("完成", tip)
            self.refresh()
        except Exception as e:
            self.status.set(f"安装失败：{_err_text(e)}")
            messagebox.showerror("安装失败", _err_text(e))

    def delete_selected(self) -> None:
        m = self._selected_item()
        if not m:
            messagebox.showinfo("提示", "请先在列表中选择一个资源")
            return
        name = m.get("name") or m.get("id")
        if not messagebox.askyesno(
            "确认删除",
            f"从市场删除「{name}」？\n\n本机已安装的副本不会被删除，但该资源从市场页面消失。",
        ):
            return
        try:
            market.delete_item(m.get("kind"), m.get("id"))
            self.status.set(f"已从市场删除：{name}")
            self.refresh()
        except Exception as e:
            self.status.set(f"删除失败：{_err_text(e)}")
            messagebox.showerror("删除失败", _err_text(e))

    def open_selected_dir(self) -> None:
        m = self._selected_item()
        target = market.MARKET_ROOT
        if m:
            target = market.MARKET_ROOT / (m.get("dir") or "")
        self._open_path(target)

    def _open_path(self, path: Path) -> None:
        try:
            path = Path(path)
            if not path.exists():
                messagebox.showinfo("提示", f"目录不存在：\n{path}")
                return
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---------------- 发布 ----------------
    def open_publish(self) -> None:
        PublishDialog(self, on_done=self.refresh)


class PublishDialog(tk.Toplevel):
    def __init__(self, master: MarketManager, on_done) -> None:
        super().__init__(master)
        self.title("发布本机资源到市场")
        self.geometry("620x480")
        self.transient(master)
        self.grab_set()
        self.on_done = on_done
        self.items: list[dict] = []
        self._job: str | None = None

        if ICON_FILE.exists():
            try:
                self.iconbitmap(str(ICON_FILE))
            except Exception:
                pass

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="资源类型").pack(side="left")
        self.kind_var = tk.StringVar(value=PUBLISH_KINDS[0][1])
        self.kind_box = ttk.Combobox(
            row, textvariable=self.kind_var, state="readonly",
            values=[label for _, label in PUBLISH_KINDS], width=16,
        )
        self.kind_box.pack(side="left", padx=8)
        self.kind_box.bind("<<ComboboxSelected>>", lambda _e: self.load_publishable())
        ttk.Button(row, text="刷新", command=self.load_publishable).pack(side="left")

        ttk.Label(
            frame, text="选择本机已有的资源，发布后会写入 market/ 并出现在市场列表中。",
            foreground="#777", font=FONT_SMALL,
        ).pack(anchor="w", pady=(0, 6))

        list_wrap = ttk.Frame(frame)
        list_wrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_wrap, font=FONT_SMALL, activestyle="none")
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        foot = ttk.Frame(frame)
        foot.pack(fill="x", pady=(10, 0))
        self.status = tk.StringVar(value="请选择资源")
        ttk.Label(foot, textvariable=self.status, foreground="#666", font=FONT_SMALL).pack(side="left")
        ttk.Button(foot, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="发布到市场", command=self.publish_selected).pack(side="right", padx=8)

        self.after(80, self.load_publishable)

    def _kind_key(self) -> str:
        label = self.kind_var.get()
        for key, text in PUBLISH_KINDS:
            if text == label:
                return key
        return PUBLISH_KINDS[0][0]

    def load_publishable(self) -> None:
        kind = self._kind_key()
        try:
            data = market.publishable(kind)
            self.items = data.get("items", [])
        except Exception as e:
            self.items = []
            messagebox.showerror("读取失败", _err_text(e))
        self.listbox.delete(0, "end")
        for it in self.items:
            desc = (it.get("description") or "").replace("\n", " ")
            if len(desc) > 44:
                desc = desc[:44] + "…"
            label = it.get("name") or it.get("id")
            self.listbox.insert("end", f"{label}    {desc}" if desc else label)
        self.status.set(f"本机共 {len(self.items)} 项可发布")

    def publish_selected(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要发布的资源", parent=self)
            return
        it = self.items[sel[0]]
        kind = self._kind_key()
        name = it.get("name") or it.get("id")
        if not messagebox.askyesno(
            "确认发布", f"把「{name}」发布到市场？\n\n若市场已有同 id 资源，将更新其内容。", parent=self,
        ):
            return
        try:
            r = market.publish({"kind": kind, "id": it.get("id")})
            item = r.get("item", {})
            self.status.set(f"已发布：{item.get('name', name)}")
            messagebox.showinfo("发布成功", f"已发布到市场：\n{item.get('name', name)}", parent=self)
            try:
                self.on_done()
            except Exception:
                pass
        except Exception as e:
            messagebox.showerror("发布失败", _err_text(e), parent=self)


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def _center(win: tk.Tk) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 30)}")


def _self_check() -> int:
    """--check：只验证数据层，不弹窗口（供命令行/自动化验证）。"""
    store.init_store()
    kinds = market.list_kinds()
    items = market.list_items("", "")
    print(f"data root : {store.ROOT}")
    print(f"market    : {market.MARKET_ROOT}")
    for k in kinds["kinds"]:
        print(f"  {k['key']:10s} {k['label']:6s} {k['count']}")
    print(f"total items: {items['total']}")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return _self_check()
    _enable_dpi_awareness()
    try:
        app = MarketManager()
        _center(app)
        app.mainloop()
        return 0
    except Exception as e:
        traceback.print_exc()
        try:
            messagebox.showerror("市场管理启动失败", str(e))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())