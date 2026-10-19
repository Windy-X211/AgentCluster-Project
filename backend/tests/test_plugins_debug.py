"""插件调试服务模块单元测试。"""
from pathlib import Path

import pytest

from plugins_debug import _coerce_params, execute_plugin_at, scan_plugins_at

MANIFEST = (
    'name: "demo_plugin"\n'
    'label:\n'
    '  zh_Hans: "演示插件"\n'
    'description:\n'
    '  zh_Hans: "演示用途"\n'
    'version: "1.2.3"\n'
    'author: "tester"\n'
    'icon: "X"\n'
    'tags: ["demo", "test"]\n'
)
TOOL = (
    'identity:\n'
    '  name: "demo_plugin"\n'
    'description:\n'
    '  zh_Hans: "工具描述文本"\n'
    'parameters:\n'
    '  - name: "n"\n'
    '    type: integer\n'
    '    required: true\n'
    '    description: "数字"\n'
    '    default: 10\n'
    '  - name: "flag"\n'
    '    type: boolean\n'
    '    required: false\n'
    '  - name: "mode"\n'
    '    type: string\n'
    '    enum: ["a", "b"]\n'
    '  - name: "payload"\n'
    '    type: object\n'
    '    required: false\n'
)
TOOL_PY = (
    'def run(**kwargs):\n'
    '    if kwargs.get("n") == -1:\n'
    '        raise RuntimeError("boom")\n'
    '    return {"types": {k: type(v).__name__ for k, v in kwargs.items()}, "kwargs": kwargs}\n'
)


@pytest.fixture()
def plugins_root(tmp_path: Path) -> Path:
    d = tmp_path / "demo_plugin"
    d.mkdir()
    (d / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    (d / "tool.yaml").write_text(TOOL, encoding="utf-8")
    (d / "tool.py").write_text(TOOL_PY, encoding="utf-8")
    return tmp_path


# ------------------------- scan_plugins_at -------------------------

def test_scan_finds_valid_plugin(plugins_root: Path):
    res = scan_plugins_at(str(plugins_root))
    assert res["error"] is None
    assert res["root_dir"] == str(plugins_root.resolve())
    assert len(res["plugins"]) == 1
    p = res["plugins"][0]
    assert p["name"] == "demo_plugin"
    assert p["label"] == "演示插件"
    assert p["tool_description"] == "工具描述文本"
    assert p["version"] == "1.2.3"
    assert p["author"] == "tester"
    assert p["tags"] == ["demo", "test"]
    assert [q["name"] for q in p["parameters"]] == ["n", "flag", "mode", "payload"]
    assert "_run" not in p  # 内部字段不外泄


def test_scan_empty_dir(tmp_path: Path):
    res = scan_plugins_at(str(tmp_path))
    assert res["plugins"] == []
    assert res["failed"] == []
    assert res["error"] is None


def test_scan_skips_hidden_and_underscore(tmp_path: Path):
    (tmp_path / "_private").mkdir()
    (tmp_path / ".hidden").mkdir()
    res = scan_plugins_at(str(tmp_path))
    assert res["plugins"] == []
    assert res["failed"] == []


def test_scan_missing_dir_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        scan_plugins_at(str(tmp_path / "not-exist"))


def test_scan_blank_root_raises():
    with pytest.raises(ValueError):
        scan_plugins_at("")


def test_scan_isolates_failed_dir(tmp_path: Path):
    (tmp_path / "broken").mkdir()  # 三件套全缺
    good = tmp_path / "demo_plugin"
    good.mkdir()
    (good / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    (good / "tool.yaml").write_text(TOOL, encoding="utf-8")
    (good / "tool.py").write_text(TOOL_PY, encoding="utf-8")

    res = scan_plugins_at(str(tmp_path))
    assert len(res["plugins"]) == 1
    assert len(res["failed"]) == 1
    assert res["failed"][0]["dir_name"] == "broken"
    assert "manifest.yaml" in res["failed"][0]["load_error"]


def test_scan_does_not_pollute_registry(plugins_root: Path):
    import plugins

    scan_plugins_at(str(plugins_root))
    assert "demo_plugin" not in plugins.PLUGIN_REGISTRY


# ------------------------- _coerce_params -------------------------

def test_coerce_params_types():
    defs = [
        {"name": "n", "type": "integer"},
        {"name": "x", "type": "number"},
        {"name": "flag", "type": "boolean"},
        {"name": "payload", "type": "object"},
        {"name": "items", "type": "array"},
        {"name": "s", "type": "string"},
        {"name": "unknown", "type": "weird"},
    ]
    out = _coerce_params(
        {"n": "5", "x": "1.5", "flag": "true", "payload": '{"a": 1}', "items": "[1,2]", "s": "hi", "unknown": 7},
        defs,
    )
    assert out["n"] == 5 and isinstance(out["n"], int)
    assert out["x"] == 1.5 and isinstance(out["x"], float)
    assert out["flag"] is True
    assert out["payload"] == {"a": 1}
    assert out["items"] == [1, 2]
    assert out["s"] == "hi"
    assert out["unknown"] == "7"  # 未知类型降级为字符串


def test_coerce_params_invalid_int():
    with pytest.raises(ValueError):
        _coerce_params({"n": "abc"}, [{"name": "n", "type": "integer"}])


def test_coerce_params_invalid_json():
    with pytest.raises(ValueError):
        _coerce_params({"payload": "{bad"}, [{"name": "payload", "type": "object"}])


def test_coerce_params_unknown_key():
    with pytest.raises(ValueError):
        _coerce_params({"nope": 1}, [{"name": "n", "type": "integer"}])


# ------------------------- execute_plugin_at -------------------------

def test_execute_ok_and_coerces(plugins_root: Path):
    res = execute_plugin_at(str(plugins_root), "demo_plugin", {"n": "7", "flag": "false", "payload": '{"k": 2}'})
    assert res["ok"] is True
    assert res["error"] is None
    assert res["data"]["types"] == {"n": "int", "flag": "bool", "payload": "dict"}
    assert res["data"]["kwargs"]["n"] == 7
    assert isinstance(res["duration_ms"], int)


def test_execute_run_exception_returns_failure(plugins_root: Path):
    res = execute_plugin_at(str(plugins_root), "demo_plugin", {"n": "-1"})
    assert res["ok"] is False
    assert res["data"] is None
    assert "boom" in res["error"]


def test_execute_plugin_not_found(plugins_root: Path):
    with pytest.raises(KeyError):
        execute_plugin_at(str(plugins_root), "ghost", {})


def test_execute_param_type_error(plugins_root: Path):
    with pytest.raises(ValueError):
        execute_plugin_at(str(plugins_root), "demo_plugin", {"n": "not-a-number"})


def test_execute_by_manifest_name_when_dir_differs(tmp_path: Path):
    d = tmp_path / "dir_alias"
    d.mkdir()
    (d / "manifest.yaml").write_text('name: "logical_name"\nlabel: "逻辑名"\n', encoding="utf-8")
    (d / "tool.yaml").write_text('identity:\n  name: "logical_name"\nparameters: []\n', encoding="utf-8")
    (d / "tool.py").write_text('def run(**kwargs):\n    return {"alias": True}\n', encoding="utf-8")

    res = execute_plugin_at(str(tmp_path), "logical_name", {})
    assert res["ok"] is True
    assert res["data"] == {"alias": True}