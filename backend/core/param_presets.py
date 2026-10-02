"""能力参数预设 — 为插件/技能预设参数

固定参数 (fixed)：
  - 加密落盘（core.vault），运行时解密后注入到插件执行 kwargs；
  - 模型**不可见、不可改**：tool schema 里对应参数会被剔除，模型即便传值也会被覆盖。
  - 典型用途：发件邮箱地址、SMTP 授权码、第三方 API Key。

可选参数 (optional)：
  - 仅文字描述，附加到 tool.description / 技能提示词，供模型参考选择；
  - 典型用途：接收方邮箱 "小明 <a@x.com>, 小红 <b@x.com>"，模型据此选合适邮箱填入。

存储：AppData/param_presets/<id>.json（fixed 值为 Fernet token）。
对设置页 UI 返回明文（管理员可见），运行时内部按需解密。
"""
from datetime import datetime
from core.store import load, upsert, get_by, next_id, delete_by
from core import vault

DOMAIN = "param_presets"
VALID_TYPES = ("plugin", "skill")


# —— UI ↔ 落盘 转换 ——

def _to_stored(d: dict) -> dict:
    """UI 明文 payload → 落盘结构（fixed 加密）。"""
    fixed_in = d.get("fixed") or []
    fixed_out = []
    for x in fixed_in:
        if not isinstance(x, dict):
            continue
        k = str(x.get("key") or "").strip()
        if not k:
            continue
        fixed_out.append({"key": k, "enc": vault.encrypt_value(x.get("value"))})

    optional_in = d.get("optional") or []
    optional_out = []
    for x in optional_in:
        if not isinstance(x, dict):
            continue
        k = str(x.get("key") or "").strip()
        if not k:
            continue
        optional_out.append({"key": k, "desc": str(x.get("desc") or "")})

    ttype = d.get("target_type") or "plugin"
    if ttype not in VALID_TYPES:
        ttype = "plugin"
    return {
        "target_type": ttype,
        "target_name": str(d.get("target_name") or "").strip(),
        "label": str(d.get("label") or ""),
        "note": str(d.get("note") or ""),
        "enabled": bool(d.get("enabled", True)),
        "fixed": fixed_out,
        "optional": optional_out,
    }


def _to_ui(it: dict) -> dict:
    """落盘结构 → UI 明文（fixed 解密）。"""
    fixed_out = []
    for x in (it.get("fixed") or []):
        k = x.get("key")
        if not k:
            continue
        fixed_out.append({"key": k, "value": vault.decrypt_value(x.get("enc") or "")})
    optional_out = []
    for x in (it.get("optional") or []):
        k = x.get("key")
        if not k:
            continue
        optional_out.append({"key": k, "desc": x.get("desc") or ""})
    return {
        "id": it.get("id"),
        "target_type": it.get("target_type") or "plugin",
        "target_name": it.get("target_name") or "",
        "label": it.get("label") or "",
        "note": it.get("note") or "",
        "enabled": it.get("enabled", True),
        "fixed": fixed_out,
        "optional": optional_out,
        "created_at": it.get("created_at"),
        "updated_at": it.get("updated_at"),
    }


# —— CRUD（供 API 路由调用）——

def list_all() -> list[dict]:
    return [_to_ui(it) for it in (load(DOMAIN) or [])]


def get_one(pid: int) -> dict | None:
    it = get_by(DOMAIN, pid)
    return _to_ui(it) if it else None


def create_preset(d: dict) -> dict:
    now = datetime.utcnow().isoformat()
    item = _to_stored(d)
    item["id"] = next_id(DOMAIN)
    item["created_at"] = now
    item["updated_at"] = now
    upsert(DOMAIN, item)
    return _to_ui(item)


def update_preset(pid: int, d: dict) -> dict | None:
    cur = get_by(DOMAIN, pid)
    if not cur:
        return None
    stored = _to_stored(d)
    cur.update(stored)
    cur["id"] = pid
    cur["updated_at"] = datetime.utcnow().isoformat()
    upsert(DOMAIN, cur)
    return _to_ui(cur)


def delete_preset(pid: int) -> bool:
    return delete_by(DOMAIN, pid)


# —— 运行时辅助（供 agent_runtime / commands 调用）——

def _active_for(target_type: str, target_name: str) -> dict | None:
    if not target_name:
        return None
    for it in (load(DOMAIN) or []):
        if (it.get("target_type") == target_type
                and (it.get("target_name") or "") == target_name
                and it.get("enabled", True)):
            return it
    return None


def get_fixed_values(target_type: str, target_name: str) -> dict:
    """运行时：返回已启用预设的固定参数（解密后 dict）。注入到插件执行 kwargs。"""
    it = _active_for(target_type, target_name)
    if not it:
        return {}
    out = {}
    for x in (it.get("fixed") or []):
        k = x.get("key")
        if not k:
            continue
        out[k] = vault.decrypt_value(x.get("enc") or "")
    return out


def get_optional_desc(target_type: str, target_name: str) -> dict:
    """运行时：返回已启用预设的可选参数描述（dict[key, desc]）。附加到 tool 描述/技能提示。"""
    it = _active_for(target_type, target_name)
    if not it:
        return {}
    out = {}
    for x in (it.get("optional") or []):
        k = x.get("key")
        if not k:
            continue
        out[k] = x.get("desc") or ""
    return out
