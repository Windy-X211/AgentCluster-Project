"""插件调试接口集成测试（仅挂载 plugins_debug 路由，避免触发主应用 lifespan 副作用）。"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import plugins_debug

MANIFEST = (
    'name: "demo_plugin"\n'
    'label: "演示插件"\n'
    'description: "演示用途"\n'
    'version: "1.2.3"\n'
)
TOOL = (
    'identity:\n'
    '  name: "demo_plugin"\n'
    'description: "工具描述文本"\n'
    'parameters:\n'
    '  - name: "n"\n'
    '    type: integer\n'
    '    required: true\n'
)
TOOL_PY = (
    'def run(**kwargs):\n'
    '    if kwargs.get("n") == -1:\n'
    '        raise RuntimeError("boom")\n'
    '    return {"n": kwargs.get("n")}\n'
)

app = FastAPI()
app.include_router(plugins_debug.router, prefix="/api/plugins_debug")
client = TestClient(app)


@pytest.fixture()
def plugins_root(tmp_path: Path) -> Path:
    d = tmp_path / "demo_plugin"
    d.mkdir()
    (d / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    (d / "tool.yaml").write_text(TOOL, encoding="utf-8")
    (d / "tool.py").write_text(TOOL_PY, encoding="utf-8")
    return tmp_path


def test_list_ok(plugins_root: Path):
    r = client.post("/api/plugins_debug/list", json={"root_dir": str(plugins_root)})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None
    assert body["plugins"][0]["name"] == "demo_plugin"
    assert body["plugins"][0]["tool_description"] == "工具描述文本"


def test_list_blank_root():
    r = client.post("/api/plugins_debug/list", json={"root_dir": ""})
    assert r.status_code == 400


def test_list_missing_dir(tmp_path: Path):
    r = client.post("/api/plugins_debug/list", json={"root_dir": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_run_ok(plugins_root: Path):
    r = client.post(
        "/api/plugins_debug/run",
        json={"root_dir": str(plugins_root), "plugin_name": "demo_plugin", "params": {"n": "3"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"] == {"n": 3}


def test_run_plugin_not_found(plugins_root: Path):
    r = client.post(
        "/api/plugins_debug/run",
        json={"root_dir": str(plugins_root), "plugin_name": "ghost", "params": {}},
    )
    assert r.status_code == 404


def test_run_bad_param_returns_400(plugins_root: Path):
    r = client.post(
        "/api/plugins_debug/run",
        json={"root_dir": str(plugins_root), "plugin_name": "demo_plugin", "params": {"n": "abc"}},
    )
    assert r.status_code == 400


def test_run_runtime_error_returns_200_failure(plugins_root: Path):
    r = client.post(
        "/api/plugins_debug/run",
        json={"root_dir": str(plugins_root), "plugin_name": "demo_plugin", "params": {"n": "-1"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "boom" in body["error"]