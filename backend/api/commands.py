from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any
from core.commands import run_command, COMMAND_META
from plugins import list_plugins as list_plugins_meta, run_plugin, load_plugins
from skills import list_skills as list_skills_meta, get_skill_prompt, load_skills

router = APIRouter()


class CmdIn(BaseModel):
    name: str
    params: dict[str, Any] = {}


@router.get("")
def list_capabilities():
    """一次性返回所有能力类型"""
    return {
        "commands": COMMAND_META,
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
    """重新扫描插件和技能目录，热加载（无需重启后端）。"""
    plugins_reg = load_plugins()
    skills_reg = load_skills()
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
