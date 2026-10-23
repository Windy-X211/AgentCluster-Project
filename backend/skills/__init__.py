"""技能系统 — 遵循 Agent Skills 开放标准

https://agentskills.io  (Anthropic 2025 年 12 月发布，已被 30+ 平台采用)

目录结构：
  AppData/skills/
    novel-writing/          ← 一个技能 = 一个文件夹
      SKILL.md              ← 【必选】YAML frontmatter + Markdown body
      scripts/              ← 可选：可执行代码
      references/           ← 可选：参考文档
      assets/               ← 可选：模板、资源
    role-playing/
      SKILL.md

SKILL.md 要求：
  ---                              ← frontmatter 开始
  name: kebab-case-skill-name      ← 【必填】小写 + 连字符，与文件夹名一致
  description: 触发驱动的一句话说明  ← 【必填】当 AI 判断何时加载此技能时读这一行
  version: "1.0.0"                 ← 可选
  author: 作者                     ← 可选
  tags: [creative, writing]        ← 可选

  # 【可选】依赖声明 — 系统加载时自动检测安装
  dependencies:
    pip:
      - "python-pptx>=0.6.21"
    external:
      - name: "Node.js"
        url: "https://nodejs.org/"
        description: "前端构建工具"
  ---                              ← frontmatter 结束
  # Markdown 指令 ...              ← 正文：步骤、规则、模板、边界

渐进式加载：
  Level 1（发现）  — 启动时只把 name + description 给 AI，零成本
  Level 2（激活）  — AI 判断相关时读完整 SKILL.md body（拼到 system prompt）
  Level 3（执行）  — 按指令需要时才读 scripts/ / references/ / assets/

好处：用户可以任意增删技能文件夹，不用改代码；
     同时兼容 Dify、Coze、Claude Code、Cursor 等平台的技能格式。
"""
import json, re, os
from pathlib import Path

import core.store as _store
from core.dependency_manager import parse_dependencies, ensure_dependencies


def _skills_root() -> Path:
    return _store.ROOT / "skills"


_skills_root().mkdir(parents=True, exist_ok=True)

SKILL_REGISTRY: dict[str, dict] = {}

# 技能正文渐进式读取阈值：正文超过该长度时，skill.invoke 只返回目录（toc），
# 再由 skill.read_section 按需读取具体章节（如 pptx 这类大技能）。
FULL_READ_LIMIT = 12000
# 渐进式返回时的开头预览长度（目录 + 开头几节，方便模型快速判断）。
PROGRESSIVE_PREVIEW_CHARS = 2500


# ==============================================================
# YAML frontmatter 简易解析（标准库没 yaml，用正则足够）
# ==============================================================
def _unquote_scalar(v: str) -> object:
    """YAML 标量解析：字符串/整数/浮点/布尔/null/内联数组。"""
    v = v.strip()
    if not v:
        return ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() in ("null", "~"):
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        pass
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    return v


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """从 SKILL.md 里拆出 YAML frontmatter 和 Markdown body。

    支持：
      - 标量值：name: foo, version: "1.0"
      - 内联数组：tags: [a, b, c]
      - 多行数组：tags:\n  - a\n  - b
      - 嵌套对象：dependencies:\n  pip: [requests]\n  external:\n    - name: xxx
    """
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm = m.group(1)
    body = m.group(2)

    # 先简单情况逐行收集 → {key: (first_value, [sub_lines], base_indent)}
    lines = fm.splitlines()
    meta: dict = {}

    i = 0
    while i < len(lines):
        raw_line = lines[i].rstrip()
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(raw_line) - len(stripped)
        if ":" not in stripped:
            i += 1
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()

        if val:
            # key: 标量 或 key: [内联数组]
            meta[key] = _unquote_scalar(val)
            i += 1
        else:
            # key: (空值 → 后续缩进更大的行属于它)
            sub_lines: list[tuple[int, str]] = []
            j = i + 1
            while j < len(lines):
                sl = lines[j].rstrip()
                ss = sl.lstrip()
                if not ss or ss.startswith("#"):
                    j += 1
                    continue
                sub_indent = len(sl) - len(ss)
                if sub_indent <= indent:
                    break
                sub_lines.append((sub_indent, ss))
                j += 1

            if not sub_lines:
                meta[key] = None
                i = j
                continue

            # 判断子内容是 list 还是 dict
            first_stripped = sub_lines[0][1]
            if first_stripped.startswith("- "):
                # 数组
                items = _parse_inline_list(sub_lines)
                meta[key] = items
            else:
                # 嵌套对象（递归简化解析）
                nested = _parse_nested_object(sub_lines)
                meta[key] = nested
            i = j

    return meta, body


def _parse_inline_list(sub_lines: list[tuple[int, str]]) -> list:
    """解析多行 YAML 数组（以 '- ' 开头的条目列表）。支持简单条目和对象条目。"""
    result: list = []
    idx = 0
    while idx < len(sub_lines):
        _ind, stripped = sub_lines[idx]
        if not stripped.startswith("- "):
            idx += 1
            continue
        item_text = stripped[2:].strip()
        # 判断 item 是纯标量还是含 key:value
        if ":" not in item_text:
            result.append(_unquote_scalar(item_text))
            idx += 1
            continue

        # 对象条目：收集缩进更大的后续行为子属性
        item_obj: dict = {}
        k, _, v = item_text.partition(":")
        k = k.strip()
        v = v.strip()
        if v:
            item_obj[k] = _unquote_scalar(v)
            idx += 1
        else:
            idx += 1

        # 继续收集缩进大于当前 item 的子行
        item_indent = sub_lines[idx - 1][0]
        while idx < len(sub_lines):
            ci, cl = sub_lines[idx]
            if ci <= item_indent:
                break
            if cl.startswith("- "):
                # 嵌套 list item 结束
                break
            ck, _, cv = cl.partition(":")
            ck = ck.strip()
            cv = cv.strip()
            if cv:
                item_obj[ck] = _unquote_scalar(cv)
            else:
                # 子嵌套对象/数组 — 收集更缩进的行
                idx += 1
                child_lines = []
                child_indent = ci + 2  # 期望子行缩进更大
                while idx < len(sub_lines):
                    cci, ccl = sub_lines[idx]
                    if cci <= ci:
                        break
                    child_lines.append((cci, ccl))
                    idx += 1
                if child_lines:
                    first_c = child_lines[0][1]
                    if first_c.startswith("- "):
                        item_obj[ck] = _parse_inline_list(child_lines)
                    else:
                        item_obj[ck] = _parse_nested_object(child_lines)
                else:
                    item_obj[ck] = None
                continue
            idx += 1

        result.append(item_obj)
    return result


def _parse_nested_object(sub_lines: list[tuple[int, str]]) -> dict:
    """解析嵌套 YAML 对象（子 key: value 对）。"""
    result: dict = {}
    idx = 0
    while idx < len(sub_lines):
        ind, stripped = sub_lines[idx]
        if not stripped or not stripped.startswith(("-", "#")) and ":" not in stripped:
            idx += 1
            continue
        if stripped.startswith("- ") or ":" not in stripped:
            idx += 1
            continue
        k, _, v = stripped.partition(":")
        k = k.strip()
        v = v.strip()
        if v:
            result[k] = _unquote_scalar(v)
            idx += 1
        else:
            idx += 1
            # 收集缩进更大的子行
            child_lines = []
            while idx < len(sub_lines):
                ci, cl = sub_lines[idx]
                if ci <= ind:
                    break
                child_lines.append((ci, cl))
                idx += 1
            if child_lines:
                first_c = child_lines[0][1]
                if first_c.startswith("- "):
                    result[k] = _parse_inline_list(child_lines)
                else:
                    result[k] = _parse_nested_object(child_lines)
            else:
                result[k] = None
    return result


def _frontmatter_field(text: str, field: str) -> str:
    """快速取 frontmatter 里的 name 或 description（发现阶段用，不用完整解析）。"""
    m = re.search(rf"^\s*{field}:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return ""
    v = m.group(1).strip()
    if v.startswith(("'", '"')) and v.endswith(("'", '"')):
        v = v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        v = ", ".join([x.strip().strip("'\"") for x in v[1:-1].split(",")])
    return v


# ==============================================================
# 扫描 & 加载
# ==============================================================
def _is_valid_skill_dir(p: Path) -> bool:
    return p.is_dir() and not p.name.startswith(("_", ".")) and (p / "SKILL.md").exists()


def _load_one(p: Path, blocking: bool = False) -> dict | None:
    md = p / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[skills] WARN 读取 {md} 失败：{e}")
        return None

    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or p.name
    if not name:
        return None

    # ----------------------------------------------------------
    # 依赖检测与安装（技能 frontmatter 的 dependencies 字段）
    # ----------------------------------------------------------
    dep_summary: dict = {}
    raw_deps = meta.get("dependencies")
    if raw_deps is not None:
        parsed = parse_dependencies(raw_deps)
        try:
            dep_summary = ensure_dependencies(parsed, p, source_label=f"skill:{name}", blocking=blocking)
        except Exception as e:
            print(f"[skills] WARN 技能 {name} 依赖检测异常: {e}")

    # 依赖状态（供前端能力管理面板展示）
    has_missing = bool(
        dep_summary.get("pip_still_missing")
        or dep_summary.get("pip_install_failed")
        or dep_summary.get("npm_missing")
    )
    status = "dependency_error" if has_missing else "ok"

    # 技能目录下的相对资源（scripts/references/assets 文件夹）
    subdirs = {
        "scripts": (p / "scripts").exists(),
        "references": (p / "references").exists(),
        "assets": (p / "assets").exists(),
    }

    return {
        "label": meta.get("label") or meta.get("name") or p.name,
        "version": meta.get("version", "1.0.0"),
        "author": meta.get("author", ""),
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        # 兼容旧 API 的字段名
        "triggers": meta.get("triggers", []),
        "prompt": body,  # 运行时拼到 system prompt 的主体就是 SKILL.md body
        "_source_dir": str(p),
        "_subdirs": subdirs,
        "_source_file": str(md),
        "dependencies": dep_summary,
        "status": status,              # "ok" / "dependency_error"
        "load_error": None,            # 技能是文本加载，不会有 exec 错误，保留字段供一致性
    }


def load_skills(blocking: bool = False) -> dict:
    """扫描 AppData/skills/*/SKILL.md，重建 SKILL_REGISTRY。任何模块可调用实现热重载。

    registry key 优先使用 frontmatter 的 name（即 kebab-case），找不到则用文件夹名。

    blocking=True 时同步等待缺失的 pip 依赖安装完成（用户主动点击"重新扫描/安装依赖"时用）。
    """
    registry: dict[str, dict] = {}
    if not _skills_root().exists():
        return registry
    for sub in sorted(_skills_root().iterdir()):
        if not _is_valid_skill_dir(sub):
            continue
        data = _load_one(sub, blocking=blocking)
        if data:
            key = data.get("label") or data.get("description") or sub.name
            # 优先用 frontmatter 的 name 作为 key（kebab-case）
            fp_name = _frontmatter_field((sub / "SKILL.md").read_text(encoding="utf-8"), "name")
            key = fp_name or sub.name
            registry[key] = data
    # 原地更新：保持 SKILL_REGISTRY 对象引用不变，避免值导入的引用在热重载后陈旧
    SKILL_REGISTRY.clear()
    SKILL_REGISTRY.update(registry)
    return registry


def list_skills() -> dict:
    return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in SKILL_REGISTRY.items()}


def skill_dependencies_snapshot() -> dict[str, dict]:
    """返回所有技能的依赖汇总快照（前端依赖管理面板用）。"""
    out: dict[str, dict] = {}
    for name, s in SKILL_REGISTRY.items():
        deps = s.get("dependencies") or {}
        out[name] = {
            "type": "skill",
            "label": s.get("label", name),
            "status": s.get("status", "ok"),
            "load_error": s.get("load_error"),
            "pip": deps.get("pip_checked", []),
            "pip_installed": deps.get("pip_installed", []),
            "pip_still_missing": deps.get("pip_still_missing", []),
            "npm_installed": deps.get("npm_installed"),
            "npm_missing": deps.get("npm_missing", False),
            "external": deps.get("external", []),
        }
    return out


def skill_toc(body: str) -> list[dict]:
    """从 Markdown 正文提取章节目录（## ~ ##### 级标题），带行号与字符区间。

    每个条目：{title, level, line, start, end}（start/end 为字符偏移，供按需切片）。
    """
    toc: list[dict] = []
    pat = re.compile(r"^(#{2,5})\s+(.*)$", re.MULTILINE)
    for m in pat.finditer(body):
        toc.append({
            "title": m.group(2).strip(),
            "level": len(m.group(1)),
            "line": body.count("\n", 0, m.start()) + 1,
            "start": m.start(),
            "end": m.end(),
        })
    return toc


def _normalize_heading(s: str) -> str:
    """归一化章节标题：去 markdown 修饰符、空白、大小写，便于模糊匹配。"""
    s = re.sub(r"[*_`#]", "", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def read_skill_section(body: str, section: str) -> tuple[str, dict | None]:
    """按标题读取正文的一个章节（含该标题，到下一个同级或更高级标题前）。

    匹配优先级：标题全文相等 > 前缀匹配 > 包含匹配。
    返回 (content, meta)；找不到返回 ("", None)。
    """
    toc = skill_toc(body)
    if not toc:
        return "", None
    q = _normalize_heading(section)

    def key(i: int) -> tuple[int, int]:
        t = _normalize_heading(toc[i]["title"])
        if t == q:
            return 0, -i
        if t.startswith(q):
            return 1, -i
        if q in t:
            return 2, -i
        return 9, -i

    idx = min(range(len(toc)), key=key)
    if key(idx)[0] >= 9:
        return "", None
    meta = toc[idx]
    # 章节结束：下一个同层级或更高级（level <= 当前）的标题
    end = len(body)
    for j in range(idx + 1, len(toc)):
        if toc[j]["level"] <= meta["level"]:
            end = toc[j]["start"]
            break
    return body[meta["start"]:end].rstrip(), meta


def get_skill_prompt(name: str) -> str:
    """返回 SKILL.md 的完整正文（含 frontmatter，这样 AI 能看到 name/description 定位自身）。"""
    if name in SKILL_REGISTRY:
        return SKILL_REGISTRY[name].get("prompt", "")
    # 兼容：用文件夹名查
    dir_name = name.replace("-", "_") if "-" in name else name.replace("_", "-")
    if dir_name in SKILL_REGISTRY:
        return SKILL_REGISTRY[dir_name].get("prompt", "")
    return ""


def save_skill_from_md(name: str, skill_md_content: str) -> dict:
    """直接用一段 SKILL.md 文本写技能（会自动解析 frontmatter）。"""
    target = _skills_root() / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    load_skills()
    return SKILL_REGISTRY.get(name, {})


def delete_skill(name: str) -> bool:
    fp = _skills_root() / name / "SKILL.md"
    if not fp.exists():
        return False
    import shutil
    shutil.rmtree(fp.parent)
    load_skills()
    return True


# 启动时加载一次
load_skills()
