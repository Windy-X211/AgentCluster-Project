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


def _skills_root() -> Path:
    return _store.ROOT / "skills"


_skills_root().mkdir(parents=True, exist_ok=True)

SKILL_REGISTRY: dict[str, dict] = {}


# ==============================================================
# YAML frontmatter 简易解析（标准库没 yaml，用正则足够）
# ==============================================================
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """从 SKILL.md 里拆出 YAML frontmatter 和 Markdown body。"""
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm = m.group(1)
    body = m.group(2)

    meta: dict = {}
    for line in fm.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # 数组：tags: [a, b, c] 或 plugins:\n  - item1\n  - item2
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip("'\"") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        elif val.startswith(("'", '"')) and val.endswith(("'", '"')):
            meta[key] = val[1:-1]
        elif val.lower() in ("true", "false"):
            meta[key] = val.lower() == "true"
        elif val.replace(".", "", 1).isdigit():
            meta[key] = float(val) if "." in val else int(val)
        else:
            # 可能是多行列表
            if not val:
                items = re.findall(r"^\s*-\s+(.+)$", fm, re.MULTILINE)
                if items:
                    meta[key] = items
                    continue
            meta[key] = val
    return meta, body


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


def _load_one(p: Path) -> dict | None:
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
    }


def load_skills() -> dict:
    """扫描 AppData/skills/*/SKILL.md，重建 SKILL_REGISTRY。任何模块可调用实现热重载。

    registry key 优先使用 frontmatter 的 name（即 kebab-case），找不到则用文件夹名。
    """
    registry: dict[str, dict] = {}
    if not _skills_root().exists():
        return registry
    for sub in sorted(_skills_root().iterdir()):
        if not _is_valid_skill_dir(sub):
            continue
        data = _load_one(sub)
        if data:
            key = data.get("label") or data.get("description") or sub.name
            # 优先用 frontmatter 的 name 作为 key（kebab-case）
            fp_name = _frontmatter_field((sub / "SKILL.md").read_text(encoding="utf-8"), "name")
            key = fp_name or sub.name
            registry[key] = data
    globals()["SKILL_REGISTRY"] = registry
    return registry


def list_skills() -> dict:
    return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in SKILL_REGISTRY.items()}


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
