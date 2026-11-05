"""GitHub 账号登录（Personal Access Token）与市场仓库写权限校验。

凭据只保存在用户数据目录 ``store.ROOT/github_auth.json``（项目外、不入 git），
对外接口只返回用户名 / 头像 / 登录状态，绝不回传 token 明文。
推送时由 ``market_sync`` 取 token 构造一次性的 HTTP 认证头，不写入 git 配置。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import store

API_BASE = "https://api.github.com"
AUTH_FILE_NAME = "github_auth.json"
_USER_AGENT = "AgentCluster/1.0"
_TIMEOUT = 15.0


# ==============================================================
# 本机凭据文件
# ==============================================================
def auth_file() -> Path:
    return store.ROOT / AUTH_FILE_NAME


def _read_auth() -> dict | None:
    try:
        f = auth_file()
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("token"):
            return data
    except Exception as e:
        print(f"[github-auth] 读取凭据失败：{e}")
    return None


def _write_auth(data: dict) -> None:
    f = auth_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)  # Windows 上可能无效，仅作尽力而为
    except OSError:
        pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ==============================================================
# GitHub API
# ==============================================================
def _api(path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        API_BASE + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        body: dict = {}
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        raise RuntimeError(f"无法连接 GitHub API（请检查网络或系统代理）：{e}")


def verify(username: str, token: str) -> dict:
    """调用 /user 校验 token，并核对用户名是否一致。"""
    token = (token or "").strip()
    username = (username or "").strip()
    if not token:
        raise ValueError("请填写 Personal Access Token")
    status, body = _api("/user", token)
    if status == 401:
        raise ValueError("Token 无效或已过期")
    if status == 403:
        raise ValueError("Token 被拒绝（可能缺少权限或触发限流），请重新生成")
    if status != 200:
        raise ValueError(f"GitHub 校验失败（HTTP {status}）")
    login = str(body.get("login") or "")
    if not login:
        raise ValueError("GitHub 返回数据异常，未取到账号名")
    if username and username.lower() != login.lower():
        raise ValueError(f"用户名与 Token 不匹配：该 Token 属于 @{login}")
    return {
        "login": login,
        "name": body.get("name") or login,
        "avatar_url": body.get("avatar_url") or "",
        "id": body.get("id"),
    }


# ==============================================================
# 登录态
# ==============================================================
def public_auth() -> dict:
    data = _read_auth()
    if not data:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "username": data.get("login") or data.get("username") or "",
        "name": data.get("name") or "",
        "avatar_url": data.get("avatar_url") or "",
        "saved_at": data.get("saved_at") or "",
    }


def get_token() -> str | None:
    data = _read_auth()
    return str(data.get("token")) if data and data.get("token") else None


def get_login() -> str | None:
    data = _read_auth()
    if not data:
        return None
    return str(data.get("login") or data.get("username") or "") or None


def save_auth(username: str, token: str, repo_url: str | None = None) -> dict:
    info = verify(username, token)
    _write_auth({
        "username": info["login"],
        "login": info["login"],
        "name": info["name"],
        "avatar_url": info["avatar_url"],
        "github_id": info.get("id"),
        "token": token.strip(),
        "saved_at": _now(),
    })
    result = public_auth()
    if repo_url:
        result["repo_permission"] = check_push_permission(repo_url)
    return result


def clear_auth() -> None:
    f = auth_file()
    try:
        if f.exists():
            f.unlink()
    except OSError as e:
        print(f"[github-auth] 删除凭据失败：{e}")


def check_push_permission(repo_url: str) -> dict:
    """检查当前登录账号对市场仓库是否有 push 权限。"""
    token = get_token()
    if not token:
        return {"checked": False, "can_push": False, "error": "未登录"}
    try:
        from .market_sync import repo_parts  # 延迟导入避免循环依赖
        _, owner, repo = repo_parts(repo_url)
    except Exception as e:
        return {"checked": False, "can_push": False, "error": str(e)}
    status, body = _api(f"/repos/{owner}/{repo}", token)
    if status == 404:
        return {"checked": True, "can_push": False, "full_name": f"{owner}/{repo}",
                "error": "仓库不存在，或当前账号无权访问"}
    if status != 200:
        return {"checked": True, "can_push": False, "error": f"HTTP {status}"}
    perms = body.get("permissions") or {}
    return {
        "checked": True,
        "can_push": bool(perms.get("push") or perms.get("admin")),
        "full_name": body.get("full_name") or f"{owner}/{repo}",
        "private": bool(body.get("private")),
    }