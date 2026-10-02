"""文件快照备份与还原系统

每次智能体执行文件修改命令前，自动保存快照，支持用户一键还原。

存储结构：
  AppData/file_backups/
    conversations/
      {conv_id}.json                    ← 该会话所有文件操作记录
    snapshots/
      {conv_id}/
        {tool_call_id}__{op_hash}/      ← 每次操作的快照目录
          {filename_before}.bak         ← 操作前的文件内容（可能不存在表示新建）

conversations/{conv_id}.json 结构：
  [
    {
      "tool_call_id": "call_xxx",
      "agent_id": 3,
      "ts": "2026-09-29T10:00:00",
      "op": "file.write",
      "path": "D:/workspace/report.md",
      "snapshot_dir": "conv_5/call_xxx__abc123",
      "before_exists": true,          ← 操作前文件是否存在
      "note": ""                       ← 备注（如 delete 成功 / write 覆盖等）
    },
    ...
  ]
"""
import os, json, hashlib, shutil, threading, time
from pathlib import Path
from datetime import datetime
from copy import deepcopy

import core.store as _store


def _backup_root() -> Path:
    return _store.ROOT / "file_backups"


def _conv_index() -> Path:
    return _backup_root() / "conversations"


def _snapshots() -> Path:
    return _backup_root() / "snapshots"

_lock = threading.RLock()


def _ensure_dirs():
    _conv_index().mkdir(parents=True, exist_ok=True)
    _snapshots().mkdir(parents=True, exist_ok=True)


def _conv_index_path(conv_id: int) -> Path:
    return _conv_index() / f"{conv_id}.json"


def _load_conv_ops(conv_id: int) -> list:
    """读取某会话的文件操作历史（深拷贝返回）"""
    p = _conv_index_path(conv_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_conv_ops(conv_id: int, ops: list) -> None:
    """原子保存"""
    _ensure_dirs()
    p = _conv_index_path(conv_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ops, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _snapshot_dir(conv_id: int, tool_call_id: str, path: str) -> Path:
    """生成该次操作的快照目录路径（含短 hash 防路径冲突）"""
    h = hashlib.sha256(path.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return _snapshots() / str(conv_id) / f"{tool_call_id}__{h}"


def snapshot_before_write(conv_id: int, tool_call_id: str, agent_id: int,
                          path: str, op_name: str) -> dict:
    """在写/覆盖/删除/替换/patch 等破坏性操作前调用。
    
    返回一个 record dict，后续可直接 append 到会话操作记录。
    如果文件不存在（新建场景），before_exists=False，快照目录不创建。
    """
    _ensure_dirs()
    abs_path = Path(path).resolve() if path else Path(path)

    record = {
        "tool_call_id": tool_call_id,
        "agent_id": agent_id,
        "ts": datetime.utcnow().isoformat(),
        "op": op_name,
        "path": str(abs_path),
        "snapshot_dir": "",
        "before_exists": abs_path.is_file(),
        "note": "",
    }

    if abs_path.is_file():
        snap_dir = _snapshot_dir(conv_id, tool_call_id, str(abs_path))
        snap_dir.mkdir(parents=True, exist_ok=True)
        bak_path = snap_dir / abs_path.name
        try:
            shutil.copy2(abs_path, bak_path)
            record["snapshot_dir"] = str(snap_dir)
        except Exception as e:
            record["note"] = f"快照保存失败：{e}"
    else:
        record["note"] = "操作前文件不存在（可能是新建）"

    # 原子追加到会话操作记录
    with _lock:
        ops = _load_conv_ops(conv_id)
        ops.append(record)
        _save_conv_ops(conv_id, ops)

    return record


def snapshot_before_delete(conv_id: int, tool_call_id: str, agent_id: int,
                           path: str, recursive: bool = False) -> dict:
    """删除操作的快照：文件/目录整个复制。"""
    _ensure_dirs()
    abs_path = Path(path).resolve() if path else Path(path)
    record = {
        "tool_call_id": tool_call_id,
        "agent_id": agent_id,
        "ts": datetime.utcnow().isoformat(),
        "op": "file.delete",
        "path": str(abs_path),
        "snapshot_dir": "",
        "before_exists": abs_path.exists(),
        "note": "",
    }

    if abs_path.exists():
        snap_dir = _snapshot_dir(conv_id, tool_call_id, str(abs_path))
        snap_dir.mkdir(parents=True, exist_ok=True)
        dest = snap_dir / abs_path.name
        try:
            if abs_path.is_file():
                shutil.copy2(abs_path, dest)
            elif abs_path.is_dir():
                if recursive:
                    shutil.copytree(abs_path, dest)
                else:
                    record["note"] = "目录未删除（recursive=false），跳过快照"
                    # 撤销时无需恢复
                    with _lock:
                        ops = _load_conv_ops(conv_id)
                        ops.append(record)
                        _save_conv_ops(conv_id, ops)
                    return record
            record["snapshot_dir"] = str(snap_dir)
        except Exception as e:
            record["note"] = f"快照保存失败：{e}"
    else:
        record["note"] = "目标不存在，无需快照"

    with _lock:
        ops = _load_conv_ops(conv_id)
        ops.append(record)
        _save_conv_ops(conv_id, ops)

    return record


# ==============================================================
# 查询接口（给前端用）
# ==============================================================
def list_file_ops(conv_id: int, since_ts: str | None = None) -> list:
    """列出某会话的所有文件操作。
    since_ts: 可选，只返回 >= 该时间戳的操作。
    """
    ops = _load_conv_ops(conv_id)
    if since_ts:
        ops = [o for o in ops if o.get("ts", "") >= since_ts]
    return ops


def list_all_ops() -> list:
    """跨会话聚合所有文件操作记录（供工作区/回收站按路径过滤使用）。

    每条记录补上 _conv_id / _idx 两个字段，还原/彻底删除时可直接定位。
    """
    _ensure_dirs()
    out = []
    for p in sorted(_conv_index().glob("*.json")):
        try:
            cid = int(p.stem)
        except ValueError:
            continue
        ops = _load_conv_ops(cid)
        for i, op in enumerate(ops):
            rec = dict(op)
            rec["_conv_id"] = cid
            rec["_idx"] = i
            out.append(rec)
    return out


def purge_op(conv_id: int, op_idx: int) -> dict:
    """彻底删除某条操作的快照备份（回收站永久清除，之后不可还原）。"""
    with _lock:
        ops = _load_conv_ops(conv_id)
        if op_idx < 0 or op_idx >= len(ops):
            return {"error": f"op 索引越界: {op_idx}"}
        op = ops[op_idx]
        snap_dir = op.get("snapshot_dir", "")
        removed = False
        if snap_dir and Path(snap_dir).exists():
            try:
                shutil.rmtree(snap_dir, ignore_errors=True)
                removed = True
            except Exception as e:
                return {"error": f"删除快照失败: {e}", "op": op}
        op["purged"] = True
        op["purged_at"] = datetime.utcnow().isoformat()
        _save_conv_ops(conv_id, ops)
        return {"ok": True, "removed_snapshot": removed, "op": op}


def list_file_ops_between_messages(conv_id: int, messages: list, user_msg_idx: int) -> list:
    """从完整消息数组里提取：user_msg_idx 对应消息之后发生的所有文件操作。
    
    因为 tool_call 消息里有 args 含 path，且 tool_call 带 ts。
    这里直接用 snapshot 系统记录的 ops 列表，按 ts 过滤即可。
    """
    if user_msg_idx < 0 or user_msg_idx >= len(messages):
        return []
    user_ts = messages[user_msg_idx].get("ts", "")
    # 找截止：用户消息之后的所有文件操作（不限定截止，因为用户消息发出后一直到最后都算）
    return list_file_ops(conv_id, since_ts=user_ts)


# ==============================================================
# 还原接口
# ==============================================================
def restore_op(conv_id: int, op_idx: int) -> dict:
    """还原某一条文件操作（按会话内的 ops 索引）。
    策略：
      - file.write/file.replace/file.patch（覆盖）→ 恢复快照文件到原路径
      - file.delete → 如果有快照（被删文件之前存在）→ 复制回来
      - 如果 before_exists=False 且是新建 → 删除新建文件
    """
    ops = _load_conv_ops(conv_id)
    if op_idx < 0 or op_idx >= len(ops):
        return {"error": f"op 索引越界: {op_idx}"}
    op = ops[op_idx]
    snap_dir = op.get("snapshot_dir", "")
    target = Path(op["path"])
    before_exists = op.get("before_exists", False)
    op_name = op.get("op", "")

    try:
        if op_name == "file.delete":
            # 恢复被删除的文件/目录
            if not snap_dir:
                return {"error": "该操作无快照（可能目标之前就不存在），无法还原", "op": op}
            src_dir = Path(snap_dir)
            item_name = target.name
            src_item = src_dir / item_name
            if not src_item.exists():
                # 目录递归复制时可能结构变深，尝试 glob
                candidates = list(src_dir.rglob(item_name))
                if not candidates:
                    return {"error": f"快照内找不到备份文件 {item_name}", "snap_dir": snap_dir}
                src_item = candidates[0]

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return {"error": f"目标路径已存在（{target}），请先手动清理或用'还原到新位置'", "op": op}
            if src_item.is_file():
                shutil.copy2(src_item, target)
            elif src_item.is_dir():
                shutil.copytree(src_item, target)
            _mark_restored(conv_id, op_idx, "restored_from_delete")
            return {"ok": True, "restored": str(target), "op": op}

        # file.write / file.replace / file.patch（覆盖修改）
        if before_exists and snap_dir:
            # 恢复快照版本
            src_item = Path(snap_dir) / target.name
            if not src_item.exists():
                return {"error": f"快照文件不存在: {src_item}", "op": op}
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_item, target)
            _mark_restored(conv_id, op_idx, "restored_to_before")
            return {"ok": True, "restored": str(target), "op": op}
        elif not before_exists:
            # 新建操作 → 删掉这个文件
            if target.exists():
                target.unlink()
                _mark_restored(conv_id, op_idx, "restored_deleted_new")
                return {"ok": True, "deleted_new": str(target), "op": op}
            else:
                _mark_restored(conv_id, op_idx, "already_gone")
                return {"ok": True, "note": "该新建文件已不存在，无需还原", "op": op}
        else:
            return {"error": "无快照可还原（操作前文件不存在，可能是新建且已删除）", "op": op}
    except Exception as e:
        return {"error": f"还原失败: {e}", "op": op}


def restore_range(conv_id: int, op_indices: list[int]) -> dict:
    """批量还原一组文件操作，倒序还原（后发生的先还原）。"""
    results = []
    for idx in sorted(op_indices, reverse=True):
        r = restore_op(conv_id, idx)
        results.append({"idx": idx, "result": r})
    return {"ok": True, "count": len(results), "results": results}


def _mark_restored(conv_id: int, op_idx: int, status: str) -> None:
    """在 ops 记录里标记某条已还原。"""
    with _lock:
        ops = _load_conv_ops(conv_id)
        if 0 <= op_idx < len(ops):
            ops[op_idx].setdefault("restored", []).append(status)
            ops[op_idx]["restored_at"] = datetime.utcnow().isoformat()
        _save_conv_ops(conv_id, ops)
