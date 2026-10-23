from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any
from core.commands import run_command, COMMAND_META, COMMAND_GROUPS
from plugins import list_plugins as list_plugins_meta, run_plugin, load_plugins, plugin_dependencies_snapshot
from skills import list_skills as list_skills_meta, get_skill_prompt, load_skills, skill_dependencies_snapshot

router = APIRouter()


class CmdIn(BaseModel):
    name: str
    params: dict[str, Any] = {}


@router.get("")
def list_capabilities():
    """一次性返回所有能力类型"""
    return {
        "commands": COMMAND_META,
        "command_groups": [g[0] for g in COMMAND_GROUPS],
        "skills": list_skills_meta(),
        "plugins": list_plugins_meta(),
    }


@router.get("/commands")
def list_commands():
    return COMMAND_META


@router.get("/skills")
def list_skills():
    return list_skills_meta()


@router.get("/skills/{name}/prompt")
def skill_prompt(name: str):
    return {"prompt": get_skill_prompt(name)}


@router.get("/plugins")
def list_plugins():
    return list_plugins_meta()


@router.post("/reload")
def reload_capabilities():
    """重新扫描插件和技能目录，热加载（无需重启后端）。

    用户主动点击"重新扫描/安装依赖"时，以同步阻塞模式安装缺失的 pip 依赖，
    确保返回时依赖已安装完成，前端能立即看到安装结果（而非后台异步导致快照仍显示缺失）。
    """
    plugins_reg = load_plugins(blocking=True)
    skills_reg = load_skills(blocking=True)
    return {
        "ok": True,
        "plugins_count": len(plugins_reg),
        "skills_count": len(skills_reg),
        "plugins": list(plugins_reg.keys()),
        "skills": list(skills_reg.keys()),
    }


@router.post("/run")
def run(c: CmdIn):
    """先尝试命令，再尝试插件"""
    from core.agent_runtime import _sanitize_tool_arguments
    params = _sanitize_tool_arguments(c.name, c.params or {})
    if c.name in COMMAND_META:
        return run_command(c.name, **params)
    from core.param_presets import get_fixed_values
    fixed = get_fixed_values("plugin", c.name)
    if fixed:
        params = {**params, **fixed}
    return run_plugin(c.name, **params)


@router.get("/dependencies")
def get_dependencies():
    """返回所有插件 + 技能的依赖汇总快照（前端能力管理-依赖安装面板用）。

    返回结构：
      {
        plugins: { name: {type, label, status, load_error,
                          pip, pip_installed, pip_still_missing,
                          npm_installed, npm_missing, external} },
        skills:  { name: {...} },
        summary: { total, with_external, with_pip, with_npm, with_errors }
      }

    status 取值："ok" / "dependency_error" / "load_error"，
    异常项（status != "ok"）不会被跳过，仍会出现在快照中供前端提示。
    每个 external 条目: {name, url, description}  （供前端展示可点击的下载链接）
    """
    plugins_deps = plugin_dependencies_snapshot()
    skills_deps = skill_dependencies_snapshot()

    all_items = list(plugins_deps.values()) + list(skills_deps.values())
    summary = {
        "total": len(all_items),
        "with_external": sum(1 for d in all_items if d.get("external")),
        "with_pip": sum(1 for d in all_items if d.get("pip")),
        "with_npm": sum(1 for d in all_items if d.get("npm_installed") is not None),
        "with_errors": sum(1 for d in all_items if d.get("status") not in (None, "ok")),
    }

    return {
        "plugins": plugins_deps,
        "skills": skills_deps,
        "summary": summary,
    }
