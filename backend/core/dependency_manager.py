"""依赖管理器 — 为插件/技能提供自动依赖检测与安装能力

支持的依赖类型：
  1. Python pip 包   → 自动 pip install（缺失时安装）
  2. Node.js/npm 包  → 自动 npm install（插件/技能目录下有 package.json 时）
  3. 外部应用        → 打印下载提示（不自动下载，只给 URL）

manifest.yaml 扩展示例（插件用）：
  dependencies:
    pip:
      - "requests>=2.28.0"
      - "Pillow>=9.0.0"
    external:
      - name: "Everything Search"
        url: "https://www.voidtools.com/"
        description: "快速文件搜索工具（everything_search 插件必需）"

SKILL.md frontmatter 扩展示例（技能用）：
  dependencies:
    pip: ["python-pptx>=0.6.21"]
    external:
      - name: "Node.js"
        url: "https://nodejs.org/"
        description: "前端构建工具（pptx 技能的 pptxgenjs 生成方式需要）"

设计原则：
  - 检测优先：已安装的依赖不重复安装
  - 非阻塞：依赖安装失败不会导致插件/技能加载失败，只打印 WARN
  - 幂等：多次调用结果相同
  - 线程安全：用全局锁保护 pip 安装过程
"""
from __future__ import annotations

import importlib, importlib.util, os, re, subprocess, sys, threading, json
from pathlib import Path
from typing import Any


# ==============================================================
# pip 包检测 & 安装
# ==============================================================

def _pip_already_installed(pkg_spec: str) -> bool:
    """检测某个 pip 包规格（如 'requests>=2.28'）是否已安装且版本满足。

    简化实现：先用 importlib.util.find_spec 检测包是否能导入，
    再用 importlib.metadata.version 检查版本约束。
    对于纯 '包名'（无版本约束），只要能导入就算安装了。

    复杂情况（如带 extras、复杂版本范围）兜底用 pip show 命令。
    """
    # 从 spec 提取包名（去掉版本约束部分）
    pkg_name = re.split(r"[<>=!~\[,;]", pkg_spec.strip())[0].strip()
    if not pkg_name:
        return True  # 空 spec 视为已满足

    # 规范化：pip 包名里的下划线/点/连字符等价
    normalized = re.sub(r"[-_.]", "_", pkg_name).lower()

    # 1) 用 importlib 尝试直接检测（快）
    try:
        spec = importlib.util.find_spec(normalized) or importlib.util.find_spec(pkg_name)
        if spec is None:
            # 有些包 import 名和 pip 名不同（如 Pillow 对应 PIL）
            # 兜底：用 pip show 查
            pass
        else:
            # 有版本约束时检查版本
            version_match = re.search(r">=\s*([\d.]+)", pkg_spec)
            if version_match:
                try:
                    from packaging.version import Version  # noqa: F401
                except ImportError:
                    # packaging 可能没装，跳过版本精确比较，信任 find_spec 结果
                    return True
                try:
                    from importlib.metadata import version as _get_version
                    installed = _get_version(pkg_name)
                    required = version_match.group(1)
                    if Version(installed) >= Version(required):
                        return True
                    return False
                except Exception:
                    return True  # 查不到版本信息就算通过
            return True
    except (ImportError, ValueError):
        pass

    # 2) 兜底：subprocess 调 pip show（慢但准确）
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    return False


# pip 安装全局锁 —— 避免多线程同时 pip install
_PIP_LOCK = threading.Lock()
_PIP_INSTALLED_CACHE: set[str] = set()


def _pip_install(pkg_spec: str) -> bool:
    """安装一个 pip 包。成功返回 True。"""
    with _PIP_LOCK:
        cache_key = pkg_spec.strip()
        if cache_key in _PIP_INSTALLED_CACHE:
            return True

        print(f"[dependency] 正在安装 pip 包: {cache_key}")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", cache_key],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode == 0:
                _PIP_INSTALLED_CACHE.add(cache_key)
                print(f"[dependency] ✓ pip 包已安装: {cache_key}")
                return True
            else:
                print(f"[dependency] ✗ pip install {cache_key} 失败: {result.stderr.strip()}")
                return False
        except subprocess.TimeoutExpired:
            print(f"[dependency] ✗ pip install {cache_key} 超时（180s）")
            return False
        except Exception as e:
            print(f"[dependency] ✗ pip install {cache_key} 异常: {e}")
            return False


def _ensure_pip_deps(pip_deps: list[str]) -> dict:
    """检测并安装 pip 依赖。返回结构化结果：

      {
        "checked": [所有声明的 pip 规格],
        "need_install": [检测到缺失、需要安装的],
        "installed_ok": [本次 pip install 成功的],
        "install_failed": [尝试安装但失败的],
        "still_missing": [最终仍未满足的（含安装失败的）],
      }
    """
    checked: list[str] = []
    need_install: list[str] = []
    for spec in pip_deps:
        spec = str(spec).strip()
        if not spec:
            continue
        checked.append(spec)
        if _pip_already_installed(spec):
            continue
        need_install.append(spec)

    installed_ok: list[str] = []
    install_failed: list[str] = []
    for spec in need_install:
        if _pip_install(spec):
            installed_ok.append(spec)
        else:
            install_failed.append(spec)

    return {
        "checked": checked,
        "need_install": need_install,
        "installed_ok": installed_ok,
        "install_failed": install_failed,
        "still_missing": install_failed[:],  # 安装失败的 = 最终仍缺失的
    }


# ==============================================================
# npm 包检测 & 安装
# ==============================================================

def _npm_available() -> bool:
    """检测 node/npm 是否可用。"""
    try:
        r = subprocess.run(
            ["npm", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_npm_deps(pkg_dir: Path) -> bool:
    """若目录下有 package.json，执行 npm install。成功返回 True。"""
    pkg_json = pkg_dir / "package.json"
    if not pkg_json.exists():
        return True  # 没有 package.json 视为不需要 npm 依赖

    if not _npm_available():
        print(f"[dependency] WARN 目录 {pkg_dir} 有 package.json 但 npm 不可用，跳过安装")
        return False

    node_modules = pkg_dir / "node_modules"
    if node_modules.exists():
        # 粗略判断：node_modules 目录存在就算已安装
        return True

    print(f"[dependency] 正在安装 npm 依赖: {pkg_dir}")
    try:
        result = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(pkg_dir),
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print(f"[dependency] ✓ npm 依赖已安装: {pkg_dir}")
            return True
        else:
            print(f"[dependency] ✗ npm install 失败: {result.stderr.strip()[:500]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"[dependency] ✗ npm install 超时（300s）: {pkg_dir}")
        return False
    except Exception as e:
        print(f"[dependency] ✗ npm install 异常: {e}")
        return False


# ==============================================================
# 外部应用提示（不自动安装，只打印）
# ==============================================================

_EXTERNAL_REMINDER_CACHE: set[str] = set()


def _remind_external_deps(external_deps: list[dict]) -> None:
    """提示需要用户手动安装的外部应用。只提示一次。"""
    for dep in external_deps:
        name = str(dep.get("name", "")).strip()
        url = str(dep.get("url", "")).strip()
        desc = str(dep.get("description", "")).strip()
        key = f"{name}|{url}"

        if not name or key in _EXTERNAL_REMINDER_CACHE:
            continue
        _EXTERNAL_REMINDER_CACHE.add(key)

        print(f"\n{'='*60}")
        print(f"[dependency] 🔔 需要外部应用（请手动安装）")
        print(f"  名称: {name}")
        if desc:
            print(f"  说明: {desc}")
        if url:
            print(f"  下载: {url}")
        print(f"{'='*60}\n")


# ==============================================================
# 入口：统一依赖检测与安装
# ==============================================================

def parse_dependencies(raw: Any) -> dict:
    """从 manifest.yaml 或 SKILL.md frontmatter 的 'dependencies' 字段解析出结构化依赖。

    支持的格式（都能识别）：
      # 纯 pip 列表（简写）
      dependencies: ["requests>=2.28", "Pillow"]

      # 分组对象（完整格式）
      dependencies:
        pip: ["requests", "Pillow>=9"]
        npm: true                    # 等价于检查目录下 package.json
        external:
          - name: Everything Search
            url: https://www.voidtools.com/
            description: 文件搜索工具

    返回: {"pip": [...], "npm": bool, "external": [...]}
    """
    result: dict[str, Any] = {"pip": [], "npm": False, "external": []}

    if raw is None:
        return result

    # 1) 简写：纯数组 → 视为 pip 包列表
    if isinstance(raw, list):
        result["pip"] = [str(x).strip() for x in raw if str(x).strip()]
        return result

    # 2) 对象格式
    if isinstance(raw, dict):
        pip = raw.get("pip") or raw.get("python")
        if isinstance(pip, str) and pip.strip():
            result["pip"] = [pip.strip()]
        elif isinstance(pip, list):
            result["pip"] = [str(x).strip() for x in pip if str(x).strip()]

        npm = raw.get("npm") or raw.get("node")
        if npm is True or (isinstance(npm, str) and npm.strip()) or (
            isinstance(npm, list) and len(npm) > 0
        ):
            result["npm"] = True

        external = raw.get("external") or raw.get("apps") or raw.get("binaries")
        if isinstance(external, list):
            for item in external:
                if isinstance(item, str) and item.strip():
                    result["external"].append({"name": item.strip()})
                elif isinstance(item, dict):
                    result["external"].append({
                        "name": str(item.get("name", "")).strip(),
                        "url": str(item.get("url", "")).strip(),
                        "description": str(item.get("description", "")).strip(),
                    })

    return result


def ensure_dependencies(
    deps: dict,
    source_dir: Path,
    source_label: str = "",
) -> dict:
    """统一入口：检测并安装依赖。返回依赖安装结果摘要。

    Args:
        deps: parse_dependencies() 返回的结构化依赖 dict
        source_dir: 插件/技能所在目录（npm install 用）
        source_label: 来源标识（打印日志时更清晰）

    Returns:
        {"pip_installed": [...], "npm_installed": bool|None, "external_reminded": [...]}
    """
    label = f"[{source_label}]" if source_label else "[dependency]"
    summary: dict[str, Any] = {
        "pip_checked": [],
        "pip_installed": [],
        "pip_still_missing": [],    # 最终仍未满足的 pip 包（含安装失败的）
        "pip_install_failed": [],   # 尝试安装但失败的 pip 包
        "npm_installed": None,
        "npm_missing": False,       # 声明了 npm 依赖但 npm 不可用或 install 失败
        "external_reminded": [],
    }

    # 1) pip
    pip_deps = deps.get("pip") or []
    if pip_deps:
        print(f"{label} 检测 {len(pip_deps)} 个 pip 依赖...")
        pip_result = _ensure_pip_deps(pip_deps)
        summary["pip_checked"] = pip_result["checked"]
        summary["pip_installed"] = pip_result["installed_ok"]
        summary["pip_install_failed"] = pip_result["install_failed"]
        summary["pip_still_missing"] = pip_result["still_missing"]

    # 2) npm —— 只在声明了 npm 依赖或目录有 package.json 时检查
    npm_declared = deps.get("npm") or (source_dir / "package.json").exists()
    if npm_declared:
        ok = _ensure_npm_deps(source_dir)
        summary["npm_installed"] = ok
        summary["npm_missing"] = not ok

    # 3) 外部应用（只提示，不安装；完整条目含 url/description 供前端展示下载链接）
    external = deps.get("external") or []
    if external:
        _remind_external_deps(external)
        summary["external"] = [
            {
                "name": d.get("name", ""),
                "url": d.get("url", ""),
                "description": d.get("description", ""),
            }
            for d in external
            if isinstance(d, dict) and d.get("name")
        ]

    return summary
