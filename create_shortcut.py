"""
生成 AgentCluster 快捷方式 + 圆角 ICO 图标
用法: python create_shortcut.py
产物: agentcluster.ico + AgentCluster.lnk  (都在项目根目录)

启动链路（进程链中不出现 cmd.exe，避免双击时弹出控制台）:
    AgentCluster.lnk → pythonw.exe launcher.py → electron / backend / node
pythonw.exe 是 Windows 专属的无控制台 Python 启动器，不创建控制台窗口；
launcher.py 内部所有子进程也带 CREATE_NO_WINDOW 标志。
"""
import sys
import shutil
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
def _find_pythonw() -> str:
    """查找 pythonw.exe（无控制台的 Python 启动器）。"""
    candidates = []
    # 首选：当前正在运行的解释器同目录（python.exe 和 pythonw.exe 总是成对存在）
    try:
        candidates.append(Path(sys.executable).with_name("pythonw.exe"))
    except Exception:
        pass
    candidates += [
        Path(r"F:\anaconda3\pythonw.exe"),
        Path(r"C:\Users\yangq\anaconda3\pythonw.exe"),
        Path(r"C:\ProgramData\anaconda3\pythonw.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # fallback: 在 PATH 里找
    found = shutil.which("pythonw.exe")
    if found:
        return found
    raise FileNotFoundError("找不到 pythonw.exe，无法生成快捷方式")


def create_shortcut(ico: Path):
    import pythoncom
    from win32com.client import Dispatch

    launcher = ROOT / "launcher.py"
    if not launcher.exists():
        raise FileNotFoundError(f"找不到 launcher.py: {launcher}")

    pythonw = _find_pythonw()
    lnk_path = ROOT / "AgentCluster.lnk"

    pythoncom.CoInitialize()
    shell = Dispatch("WScript.Shell")
    lnk = shell.CreateShortcut(str(lnk_path))
    # 直接用 pythonw.exe 启动 launcher.py
    # pythonw.exe 是 Windows 专属的无控制台 Python 启动器，
    # 进程链中不会出现任何 cmd.exe 窗口
    lnk.TargetPath = pythonw
    # 用绝对路径传脚本，不依赖 WorkingDirectory 是否被系统接受
    lnk.Arguments = f'"{launcher}"'
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
