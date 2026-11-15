"""设置页「迁移到此处」的回归测试（core.store.migrate_to）。

全部跑在 tmp_path 里造的假数据上，绝不碰真实用户数据：
  - 只复制不删除：源目录文件一个不少
  - 目标已存在的同名文件跳过、不覆盖（目标优先）
  - _legacy 归档 / __pycache__ / 隐藏文件 / *.tmp 不参与迁移
  - dry_run 只统计：不建目录、不写文件、不切 ROOT
  - 同一位置 / 互相包含 / 空路径 → ValueError
  - 迁移成功后 ROOT 与 capability_groups 目录一起切到新位置
"""
import json
from pathlib import Path

import pytest

import core.store as store


@pytest.fixture()
def fake_store(tmp_path, monkeypatch):
    """在临时目录造一份假用户数据，并把 store 的根与 bootstrap 文件都指到临时区。"""
    src = tmp_path / "src-data"
    (src / "agents").mkdir(parents=True)
    (src / "agents" / "1.json").write_text(json.dumps({"id": 1, "name": "假智能体"}), encoding="utf-8")
    (src / "agents" / "2.json").write_text(json.dumps({"id": 2, "name": "假智能体2"}), encoding="utf-8")
    (src / "conversations").mkdir()
    (src / "conversations" / "1.json").write_text(json.dumps({"id": 1, "messages": []}), encoding="utf-8")
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_text("# demo", encoding="utf-8")
    (src / "settings.json").write_text(json.dumps({"storage_version": 2}), encoding="utf-8")

    # 以下内容不应进入迁移结果
    (src / "_legacy").mkdir()
    (src / "_legacy" / "old.json").write_text("{}", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (src / "agents" / ".hidden.json").write_text("{}", encoding="utf-8")
    (src / "agents" / "3.json.tmp").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(store, "ROOT", src)
    monkeypatch.setattr(store, "LEGACY_DIR", src / "_legacy")
    monkeypatch.setattr(store, "ROOT_CAPABILITY_GROUPS", src / "capability_groups")
    # bootstrap 文件也指到临时目录：切根只写这份假的，不动项目根目录的 .storage_location
    monkeypatch.setattr(store, "_BOOTSTRAP_FILE", tmp_path / ".storage_location")
    return src, tmp_path


def _rel_files(root: Path) -> list[Path]:
    """按 migrate_to 的口径列出待迁移文件（相对路径）。"""
    return sorted(rel for _, rel in store._iter_data_files(root))


def test_migrate_copies_data_and_keeps_source(fake_store, tmp_path):
    src, _ = fake_store
    dst = tmp_path / "dst-data"
    before = _rel_files(src)

    r = store.migrate_to(str(dst))

    assert r["switched"] is True
    assert r["dry_run"] is False
    assert r["copied"] == len(before)
    assert r["skipped"] == 0
    assert r["source"] == str(src.resolve())
    assert r["target"] == str(dst.resolve())

    # 源目录一个文件都不少（只复制不删除）
    assert _rel_files(src) == before
    # 目标拿到同一批文件
    for rel in before:
        assert (dst / rel).is_file()

    # 跳过项不进目标
    assert not (dst / "_legacy" / "old.json").exists()
    assert not (dst / "__pycache__").exists()
    assert not (dst / "agents" / ".hidden.json").exists()
    assert not (dst / "agents" / "3.json.tmp").exists()


def test_migrate_skips_existing_files_in_target(fake_store, tmp_path):
    src, _ = fake_store
    dst = tmp_path / "dst-data"
    (dst / "agents").mkdir(parents=True)
    (dst / "agents" / "1.json").write_text(
        json.dumps({"id": 1, "name": "目标里的新版本"}, ensure_ascii=False), encoding="utf-8")

    r = store.migrate_to(str(dst))

    assert r["skipped"] == 1
    # 目标已有文件不被源数据覆盖
    kept = json.loads((dst / "agents" / "1.json").read_text(encoding="utf-8"))
    assert kept["name"] == "目标里的新版本"


def test_dry_run_writes_nothing(fake_store, tmp_path):
    src, _ = fake_store
    dst = tmp_path / "dst-data"

    r = store.migrate_to(str(dst), dry_run=True)

    assert r["dry_run"] is True
    assert r["switched"] is False
    assert r["copied"] == len(_rel_files(src))
    assert not dst.exists()
    assert store.ROOT.resolve() == src.resolve()


def test_migrate_switches_root_and_capability_groups(fake_store, tmp_path):
    src, _ = fake_store
    dst = tmp_path / "dst-data"

    r = store.migrate_to(str(dst))

    new_root = dst.resolve()
    assert store.ROOT.resolve() == new_root
    assert r["paths"]["root"] == str(new_root)
    assert r["paths"]["agents_dir"] == str(new_root / "agents")
    # capability_groups 是模块级常量，切根后必须同步，否则新位置的分组读写会跑到旧目录
    assert r["paths"]["capability_groups_dir"] == str(new_root / "capability_groups")
    assert store.ROOT_CAPABILITY_GROUPS == new_root / "capability_groups"
    # bootstrap 记录绝对路径（临时目录在项目外）
    assert Path(store._BOOTSTRAP_FILE.read_text(encoding="utf-8")) == new_root


def test_reject_invalid_targets(fake_store, tmp_path):
    src, _ = fake_store
    with pytest.raises(ValueError):
        store.migrate_to("   ")
    with pytest.raises(ValueError):
        store.migrate_to(str(src))                 # 与当前存储位置相同
    with pytest.raises(ValueError):
        store.migrate_to(str(src / "inner"))       # 目标在源内部
    with pytest.raises(ValueError):
        store.migrate_to(str(tmp_path))            # 目标包含源（父目录）
