"""市场安装的重名处理回归。

覆盖三条规则：
  1. 画布名与本机重名 → 加序号（游戏团队 → 游戏团队2）
  2. 智能体分组名与本机重名 → 加序号新建分组（不复用本地同名组）
  3. 重名智能体**不改名**，但每条都分配全新且互不冲突的 id
"""
import json
from pathlib import Path

import pytest

import api.market as market
import core.store as store


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """把 store.ROOT 指到临时目录（后端其它测试用不到真实数据）。"""
    root = tmp_path / "store"
    monkeypatch.setattr(store, "ROOT", root)
    monkeypatch.setattr(store, "LEGACY_DIR", root / "_legacy")
    store.init_store()
    return root


@pytest.fixture()
def tmp_market(tmp_path, monkeypatch):
    """把市场目录与索引都指到临时目录。

    必须同时改 INDEX_FILE——它是在模块加载时求值的常量，
    只改 MARKET_ROOT 会让发布结果写进真实市场仓库。
    """
    market_root = tmp_path / "market"
    monkeypatch.setattr(market, "MARKET_ROOT", market_root)
    monkeypatch.setattr(market, "INDEX_FILE", market_root / "index.json")
    return market_root


def _write_canvas_package(market_root: Path, slug: str, payload: dict,
                          agent_files: dict[str, dict] | None = None) -> Path:
    """写一个市场画布包。

    ``agent_files`` = {目录名(agent_key): 智能体快照}，模拟发布侧
    ``agents/<key>/agent.json`` 的新格式；不传则只写 canvas.json（旧格式）。
    """
    dest = market_root / "canvases" / slug
    dest.mkdir(parents=True, exist_ok=True)
    if agent_files:
        for key, snap in agent_files.items():
            a_dir = dest / "agents" / key
            a_dir.mkdir(parents=True, exist_ok=True)
            (a_dir / "agent.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "canvas.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


# ---------------------------------------------------------------
# _unique_name：纯函数
# ---------------------------------------------------------------
def test_unique_name_appends_index():
    taken = {"游戏团队"}
    assert market._unique_name("游戏团队", taken) == "游戏团队2"
    taken.add("游戏团队2")
    assert market._unique_name("游戏团队", taken) == "游戏团队3"


def test_unique_name_increments_existing_numeric_suffix():
    taken = {"游戏团队2"}
    assert market._unique_name("游戏团队2", taken) == "游戏团队3"


def test_unique_name_keeps_free_name():
    assert market._unique_name("游戏团队", {"别的"}) == "游戏团队"


def test_unique_name_pure_numeric_name():
    assert market._unique_name("2024", {"2024"}) == "2025"


# ---------------------------------------------------------------
# 安装画布：重名加序号 + id 不冲突
# ---------------------------------------------------------------
def test_install_canvas_renames_conflicting_canvas_and_group(tmp_store, tmp_market):
    market_root = tmp_market

    # 本机已有一个同名画布 + 同名分组
    store.upsert("canvases", {"id": 1, "name": "游戏团队", "nodes": [], "edges": []})
    store.upsert("agent_groups", {"id": 1, "name": "游戏"})

    _write_canvas_package(market_root, "canvas-x", {
        "name": "游戏团队",
        "description": "来自市场",
        "agent_groups": [{"name": "游戏", "description": ""}],
        "agents": [{"name": "游戏策划师", "group_name": "游戏"}],
        "nodes": [{"id": 1, "agent_ref": "游戏策划师", "x": 0, "y": 0}],
        "edges": [],
    })

    item = {"kind": "canvas", "id": "canvas-x", "dir": "canvases/canvas-x",
            "file": "canvas.json", "name": "游戏团队"}
    res = market._install_canvas(item)

    # 画布加序号
    assert res["name"] == "游戏团队2"
    canvases = {c["id"]: c for c in store.load("canvases")}
    assert canvases[res["id"]]["name"] == "游戏团队2"
    # 原有画布不受影响
    assert canvases[1]["name"] == "游戏团队"

    # 分组加序号新建，不复用本地同名组
    groups = store.load("agent_groups")
    names = sorted(g["name"] for g in groups)
    assert names == ["游戏", "游戏2"]
    new_group = next(g for g in groups if g["name"] == "游戏2")

    # 智能体挂到新分组上
    agents = store.load("agents")
    assert len(agents) == 1
    assert agents[0]["group_id"] == new_group["id"]

    # 节点指向新建的智能体
    assert res["skipped_nodes"] == 0
    assert canvases[res["id"]]["nodes"][0]["agent_id"] == agents[0]["id"]


def test_install_canvas_keeps_original_name_when_no_conflict(tmp_store, tmp_market):
    market_root = tmp_market

    _write_canvas_package(market_root, "canvas-y", {
        "name": "图片绘制团队",
        "agent_groups": [{"name": "图片绘制"}],
        "agents": [{"name": "AI画师", "group_name": "图片绘制"}],
        "nodes": [{"id": 1, "agent_ref": "AI画师", "x": 0, "y": 0}],
    })

    item = {"kind": "canvas", "id": "canvas-y", "dir": "canvases/canvas-y",
            "file": "canvas.json", "name": "图片绘制团队"}
    res = market._install_canvas(item)

    assert res["name"] == "图片绘制团队"
    assert [g["name"] for g in store.load("agent_groups")] == ["图片绘制"]
    assert [a["name"] for a in store.load("agents")] == ["AI画师"]


def test_install_canvas_same_named_agents_keep_name_but_get_distinct_ids(tmp_store, tmp_path, monkeypatch):
    """画布内两个同名智能体：名字都不改，但 id 必须不同，节点不能串位。"""
    market_root = tmp_path / "market"
    monkeypatch.setattr(market, "MARKET_ROOT", market_root)

    # 新格式包：agents/<key>/agent.json + 节点带 agent_key
    _write_canvas_package(
        market_root, "canvas-z",
        {
            "name": "双策划",
            "agents": [
                {"name": "策划", "group_name": "", "agent_key": "zuhua"},
                {"name": "策划", "group_name": "", "agent_key": "zuhua-2"},
            ],
            "nodes": [
                {"id": 1, "agent_ref": "策划", "agent_key": "zuhua", "x": 0, "y": 0},
                {"id": 2, "agent_ref": "策划", "agent_key": "zuhua-2", "x": 1, "y": 1},
            ],
        },
        agent_files={
            "zuhua": {"name": "策划", "group_name": "", "agent_key": "zuhua"},
            "zuhua-2": {"name": "策划", "group_name": "", "agent_key": "zuhua-2"},
        },
    )

    item = {"kind": "canvas", "id": "canvas-z", "dir": "canvases/canvas-z",
            "file": "canvas.json", "name": "双策划"}
    res = market._install_canvas(item)

    agents = store.load("agents")
    assert len(agents) == 2
    assert {a["name"] for a in agents} == {"策划"}          # 名字不改
    assert agents[0]["id"] != agents[1]["id"]                # id 不冲突

    canvas = next(c for c in store.load("canvases") if c["id"] == res["id"])
    node_ids = [n["agent_id"] for n in canvas["nodes"]]
    assert sorted(node_ids) == sorted(a["id"] for a in agents)
    assert node_ids[0] != node_ids[1]                       # 两个节点各自绑定


def test_install_canvas_legacy_package_without_agent_key(tmp_store, tmp_path, monkeypatch):
    """旧包（无 agent_key）仍按名字兜底匹配，节点不丢。"""
    market_root = tmp_path / "market"
    monkeypatch.setattr(market, "MARKET_ROOT", market_root)

    _write_canvas_package(market_root, "canvas-legacy", {
        "name": "旧包画布",
        "agents": [{"name": "策划", "group_name": ""}],
        "nodes": [{"id": 1, "agent_ref": "策划", "x": 0, "y": 0}],
    })

    item = {"kind": "canvas", "id": "canvas-legacy", "dir": "canvases/canvas-legacy",
            "file": "canvas.json", "name": "旧包画布"}
    res = market._install_canvas(item)

    assert res["skipped_nodes"] == 0
    agents = store.load("agents")
    canvas = next(c for c in store.load("canvases") if c["id"] == res["id"])
    assert canvas["nodes"][0]["agent_id"] == agents[0]["id"]


def test_install_canvas_legacy_package_same_name_agents_not_swapped(tmp_store, tmp_path, monkeypatch):
    """旧包里画布内两个同名智能体：即便没有 agent_key，两个节点也不能绑到同一个。"""
    market_root = tmp_path / "market"
    monkeypatch.setattr(market, "MARKET_ROOT", market_root)

    _write_canvas_package(market_root, "canvas-legacy2", {
        "name": "旧包双策划",
        "agents": [
            {"name": "策划", "group_name": ""},
            {"name": "策划", "group_name": ""},
        ],
        "nodes": [
            {"id": 1, "agent_ref": "策划", "x": 0, "y": 0},
            {"id": 2, "agent_ref": "策划", "x": 1, "y": 1},
        ],
    })

    item = {"kind": "canvas", "id": "canvas-legacy2", "dir": "canvases/canvas-legacy2",
            "file": "canvas.json", "name": "旧包双策划"}
    res = market._install_canvas(item)

    agents = store.load("agents")
    assert len(agents) == 2
    canvas = next(c for c in store.load("canvases") if c["id"] == res["id"])
    node_ids = [n["agent_id"] for n in canvas["nodes"]]
    assert node_ids[0] != node_ids[1]              # 两个节点不串位
    assert sorted(node_ids) == sorted(a["id"] for a in agents)


def test_install_canvas_does_not_collide_with_existing_agent_ids(tmp_store, tmp_market):
    market_root = tmp_market

    store.upsert("agents", {"id": 7, "name": "策划", "group_id": None})

    _write_canvas_package(market_root, "canvas-w", {
        "name": "新画布",
        "agents": [{"name": "策划", "group_name": ""}],
        "nodes": [{"id": 1, "agent_ref": "策划", "x": 0, "y": 0}],
    })

    item = {"kind": "canvas", "id": "canvas-w", "dir": "canvases/canvas-w",
            "file": "canvas.json", "name": "新画布"}
    market._install_canvas(item)

    ids = sorted(a["id"] for a in store.load("agents"))
    assert ids == [7, 8]                                    # 新记录拿到全新 id
    old = store.get_by("agents", 7)
    assert old["name"] == "策划"                            # 原有记录未被覆盖


def test_install_agent_keeps_name_and_allocates_new_id(tmp_store, tmp_market):
    market_root = tmp_market

    dest = market_root / "agents" / "ai-hua"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "agent.json").write_text(
        json.dumps({"name": "AI画师", "system_prompt": "x"}, ensure_ascii=False),
        encoding="utf-8")

    store.upsert("agents", {"id": 3, "name": "AI画师"})

    item = {"kind": "agent", "id": "ai-hua", "dir": "agents/ai-hua",
            "file": "agent.json", "name": "AI画师"}
    res = market._install_agent(item)

    assert res["name"] == "AI画师"                          # 重名不改名
    assert res["id"] != 3                                   # id 不冲突
    agents = store.load("agents")
    assert len(agents) == 2
    assert store.get_by("agents", 3)["name"] == "AI画师"      # 旧记录完好


# ---------------------------------------------------------------
# 闭环：发布 → 安装（验证 agent_key 能精确还原节点引用）
# ---------------------------------------------------------------
def test_publish_then_install_roundtrip(tmp_store, tmp_market):
    """本机发布含两个同名智能体的画布，再装回来：名字不改、id 不冲突、节点不串位。"""
    store.upsert("agent_groups", {"id": 1, "name": "游戏"})
    store.upsert("agents", {"id": 1, "name": "策划", "group_id": 1, "system_prompt": "A"})
    store.upsert("agents", {"id": 2, "name": "策划", "group_id": 1, "system_prompt": "B"})
    store.upsert("canvases", {
        "id": 1, "name": "游戏团队", "description": "d",
        "nodes": [
            {"id": 1, "agent_id": 1, "x": 0, "y": 0},
            {"id": 2, "agent_id": 2, "x": 100, "y": 0},
        ],
        "edges": [],
    })

    published = market._publish_canvas("1", version="1.0.0")
    item = published["item"]

    # 发布产物：节点带唯一 agent_key，同名智能体分属不同目录
    pkg = tmp_market / "canvases" / item["id"]
    raw = json.loads((pkg / "canvas.json").read_text(encoding="utf-8"))
    keys = [n.get("agent_key") for n in raw["nodes"]]
    assert all(keys) and len(set(keys)) == 2
    assert len(list((pkg / "agents").iterdir())) == 2

    # 模拟另一台机器：本机已有同名画布 + 同名分组
    store.upsert("canvases", {"id": 9, "name": "游戏团队", "nodes": [], "edges": []})
    store.upsert("agent_groups", {"id": 9, "name": "游戏"})

    res = market._install_canvas(item)

    assert res["name"] == "游戏团队2"                  # 画布加序号
    assert res["created_agent_groups"] == ["游戏2"]     # 分组加序号
    assert res["skipped_nodes"] == 0
    assert res["created_agents"] == ["策划", "策划"]     # 智能体不改名

    agents = [a for a in store.load("agents") if a["id"] not in (1, 2)]
    assert len(agents) == 2
    assert agents[0]["id"] != agents[1]["id"]

    canvas = store.get_by("canvases", res["id"])
    node_ids = [n["agent_id"] for n in canvas["nodes"]]
    assert node_ids[0] != node_ids[1]                   # 两个节点不串位
    # 提示词各自对应（没串到另一个同名智能体上）
    prompts = {a["id"]: a.get("system_prompt") for a in agents}
    assert set(prompts[n] for n in node_ids) == {"A", "B"}
