"""
生成 AgentCluster 快捷方式 + 圆角 ICO 图标
用法: python create_shortcut.py
产物: agentcluster.ico +AgentCluster.lnk  (都在项目根目录)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------- 1. 生成圆角多尺寸 ICO ----------
def build_ico() -> Path:
    from PIL import Image, ImageDraw

    src_candidates = [
        ROOT / "frontend" / "public" / "icon.png",
        ROOT / "frontend" / "public" / "icon.jpg",
        ROOT / "1791461860623.webp.jpg",
    ]
    src = next((p for p in src_candidates if p.exists()), None)
    if src is None:
        raise FileNotFoundError("找不到源图标文件")

    img = Image.open(src).convert("RGBA")
    # 中心裁剪成正方形
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))

    # 先放大到 1024 再裁圆角（圆角更平滑）
    base_size = 1024
    img = img.resize((base_size, base_size), Image.LANCZOS)
    # 圆角半径 = 边长的 22%（Windows 11 风格）
    r = int(base_size * 0.22)
    mask = Image.new("L", (base_size, base_size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, base_size - 1, base_size - 1),
                                            radius=r, fill=255)
    rounded = Image.new("RGBA", (base_size, base_size), (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask)

    ico_path = ROOT / "agentcluster.ico"
    sizes = [(16, 16), (32, 32), (48, 48), (256, 256)]
    rounded.save(ico_path, format="ICO", sizes=sizes)
    print(f"[ICO] 已生成 (圆角 22%): {ico_path}")
    return ico_path


# ---------- 2. 创建项目内快捷方式 ----------
def create_shortcut(ico: Path):
    import pythoncom
    from win32com.client import Dispatch

    start_bat = ROOT / "start.bat"
    if not start_bat.exists():
        raise FileNotFoundError(f"找不到 start.bat: {start_bat}")

    lnk_path = ROOT / "AgentCluster.lnk"
    vbs_path = ROOT / "launcher.vbs"
    if not vbs_path.exists():
        raise FileNotFoundError(f"找不到 launcher.vbs: {vbs_path}")

    pythoncom.CoInitialize()
    shell = Dispatch("WScript.Shell")
    lnk = shell.CreateShortcut(str(lnk_path))
    # 用 wscript.exe 静默执行 launcher.vbs，VBS 内部用 SW_HIDE 启动 start.bat
    # 彻底避免任何终端窗口闪烁
    lnk.TargetPath = "wscript.exe"
    lnk.Arguments = f'"{vbs_path}"'
    lnk.WorkingDirectory = str(ROOT)
    lnk.IconLocation = str(ico) + ",0"
    lnk.Description = "AgentCluster 智能体集群"
    lnk.Save()
    pythoncom.CoUninitialize()
    print(f"[LNK] 已生成: {lnk_path}")
    print(f"      Target: {lnk.TargetPath} {lnk.Arguments}")
    return lnk_path


if __name__ == "__main__":
    ico = build_ico()
    lnk = create_shortcut(ico)
    print(f"\n✅ 完成！\n   图标: {ico}\n   快捷方式: {lnk}")
