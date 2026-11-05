"""市场远程仓库同步 —— 多镜像回退下载 + 本地 git 发布推送。

市场资源独立托管在远程仓库（默认 ``AgentCluster-Market``），本地 ``market/``
是它的 git 工作副本，主源码仓库不再跟踪 ``market/``：

  - 下载：按「镜像模板 → 官方地址」的顺序自动回退，镜像主要服务大陆网络；
  - 上传：先把资源发布到本地 ``market/``，再 commit + push 到官方仓库
    （加速镜像基本只代理读取，不支持 push）。

配置保存在 ``settings.json``：
  market_repo_url   仓库地址（默认 GitHub 官方）
  market_mirrors    镜像模板列表，支持 {url} 占位符；不含占位符时按前缀拼接
  market_branch     分支名（默认 main）

环境变量 ``AGENTCLUSTER_MARKET_REPO`` / ``AGENTCLUSTER_MARKET_MIRRORS`` /
``AGENTCLUSTER_MARKET_BRANCH`` 可临时覆盖，便于 CI / 打包脚本使用。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import github_auth, store

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARKET_ROOT = _PROJECT_ROOT / "market"
INDEX_FILE = MARKET_ROOT / "index.json"

DEFAULT_REPO_URL = "https://github.com/Windy-X211/AgentCluster-Market.git"
# {url} 会被替换成完整的官方下载地址；顺序即尝试顺序（越靠前越快）
DEFAULT_MIRRORS = [
    "https://ghfast.top/{url}",
    "https://gh-proxy.com/{url}",
]
DEFAULT_BRANCH = "main"

# 同步时跳过的运行时垃圾（与 api/market.py 的复制排除规则保持一致）
_IGNORE_DIRS = {"__pycache__", "_webengine_profile", "node_modules", ".git",
                ".pytest_cache", ".runtime"}
_IGNORE_SUFFIX = {".pyc", ".pyo", ".log", ".bak", ".tmp"}
_IGNORE_FILES = {".DS_Store", "Thumbs.db"}

_USER_AGENT = "AgentCluster-Market/1.0 (+https://github.com/Windy-X211/AgentCluster-Project)"


# ==============================================================
# 配置
# ==============================================================
def _read_settings() -> dict:
    try:
        data = store.load("settings")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _env_str(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _env_mirrors() -> list[str]:
    raw = _env_str("AGENTCLUSTER_MARKET_MIRRORS")
    if not raw:
        return []
    return [m.strip() for m in raw.replace(",", "\n").splitlines() if m.strip()]


def get_config() -> dict:
    """读取市场仓库配置（环境变量优先级最高）。"""
    s = _read_settings()
    repo_url = _env_str("AGENTCLUSTER_MARKET_REPO") or str(s.get("market_repo_url") or "").strip() \
        or DEFAULT_REPO_URL
    branch = _env_str("AGENTCLUSTER_MARKET_BRANCH") or str(s.get("market_branch") or "").strip() \
        or DEFAULT_BRANCH
    mirrors = _env_mirrors()
    if not mirrors:
        raw = s.get("market_mirrors")
        if isinstance(raw, list):
            mirrors = [str(m).strip() for m in raw if str(m).strip()]
        elif isinstance(raw, str):
            mirrors = [m.strip() for m in raw.replace(",", "\n").splitlines() if m.strip()]
    if not mirrors:
        mirrors = list(DEFAULT_MIRRORS)
    return {"repo_url": repo_url, "mirrors": mirrors, "branch": branch}


def save_config(payload: dict) -> dict:
    """把 market_repo_url / market_mirrors / market_branch 写入 settings.json。"""
    cur = _read_settings()
    if "repo_url" in payload:
        repo = str(payload.get("repo_url") or "").strip()
        if not repo:
            raise ValueError("仓库地址不能为空")
        cur["market_repo_url"] = repo
    if "branch" in payload:
        branch = str(payload.get("branch") or "").strip()
        if not branch:
            raise ValueError("分支名不能为空")
        cur["market_branch"] = branch
    if "mirrors" in payload:
        raw = payload.get("mirrors")
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").splitlines()
        if not isinstance(raw, list):
            raise ValueError("镜像列表格式不正确")
        cur["market_mirrors"] = [str(m).strip() for m in raw if str(m).strip()]
    store.save("settings", cur)
    return get_config()


# ==============================================================
# 地址解析
# ==============================================================
def repo_parts(repo_url: str | None = None) -> tuple[str, str, str]:
    """把仓库地址解析成 (host, owner, repo)。支持 https / ssh / 带 .git 后缀。"""
    url = (repo_url or get_config()["repo_url"]).strip()
    if url.startswith("git@") or re.match(r"^[\w.-]+@[\w.-]+:", url):
        m = re.match(r"^[^@]+@([^:]+):(.+)$", url)
        if not m:
            raise ValueError(f"无法解析仓库地址：{url}")
        host, path = m.group(1), m.group(2)
    elif "://" in url:
        p = urlparse(url)
        host, path = p.netloc, p.path
    else:
        host, path = "github.com", url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2:
        raise ValueError(f"无法解析仓库地址：{url}")
    return host or "github.com", parts[0], parts[1]


def archive_url(repo_url: str | None = None, branch: str | None = None) -> str:
    """GitHub 风格归档 zip 地址（镜像模板围绕该地址工作）。"""
    cfg = get_config()
    _, owner, repo = repo_parts(repo_url or cfg["repo_url"])
    br = branch or cfg["branch"]
    return f"https://github.com/{owner}/{repo}/archive/refs/heads/{br}.zip"


def raw_index_url(repo_url: str | None = None, branch: str | None = None) -> str:
    cfg = get_config()
    _, owner, repo = repo_parts(repo_url or cfg["repo_url"])
    br = branch or cfg["branch"]
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/index.json"


def mirror_candidates(url: str, mirrors: list[str] | None = None) -> list[str]:
    """按镜像模板生成候选地址，最后追加官方原地址兜底（去重保序）。"""
    templates = get_config()["mirrors"] if mirrors is None else mirrors
    out: list[str] = []
    for tpl in templates:
        tpl = (tpl or "").strip()
        if not tpl:
            continue
        if "{url}" in tpl:
            out.append(tpl.replace("{url}", url))
        elif "{path}" in tpl:
            try:
                p = urlparse(url)
                out.append(tpl.replace("{path}", p.path.lstrip("/")))
            except Exception:
                continue
        else:
            out.append(tpl.rstrip("/") + "/" + url)
    out.append(url)
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url[:32]
    except Exception:
        return "unknown"


# ==============================================================
# 下载
# ==============================================================
def download_file(url: str, dest: Path, *,
                  mirrors: list[str] | None = None,
                  timeout: float = 60.0,
                  on_progress=None) -> dict:
    """多镜像回退下载单个文件，成功返回 {url, mirror, size}。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    candidates = mirror_candidates(url, mirrors)
    for cand in candidates:
        try:
            req = Request(cand, headers={"User-Agent": _USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                total = 0
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except Exception:
                    total = 0
                loaded = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        loaded += len(chunk)
                        if on_progress:
                            try:
                                on_progress(loaded, total)
                            except Exception:
                                pass
            size = dest.stat().st_size
            if size <= 0:
                raise RuntimeError("下载内容为空")
            return {"url": cand, "mirror": _host_of(cand), "size": size,
                    "total": total or size}
        except Exception as e:  # 404 / 超时 / 证书等一律换下一个源
            errors.append(f"{cand} → {e}")
            try:
                dest.unlink()
            except OSError:
                pass
    raise RuntimeError("下载失败（已尝试 %d 个源）：\n%s" % (len(candidates), "\n".join(errors)))


# ==============================================================
# 索引与合并
# ==============================================================
def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[market-sync] 读取 {path} 失败：{e}")
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _key(item: dict) -> tuple[str, str]:
    return str(item.get("kind") or ""), str(item.get("id") or "")


def _item_dir(item: dict) -> str:
    return str(item.get("dir") or "").replace("\\", "/").strip("/")


def _is_ignored(rel: Path) -> bool:
    if any(part in _IGNORE_DIRS for part in rel.parts):
        return True
    if rel.name in _IGNORE_FILES:
        return True
    return rel.suffix.lower() in _IGNORE_SUFFIX


def _under(rel_str: str, prefixes: set[str]) -> bool:
    for p in prefixes:
        if p and (rel_str == p or rel_str.startswith(p + "/")):
            return True
    return False


def _find_extract_root(base: Path) -> Path:
    """zip 解压后通常带一层 ``<repo>-<branch>/``，找含 index.json 的那一层。"""
    if (base / "index.json").exists():
        return base
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "index.json").exists():
            return child
    for child in base.rglob("index.json"):
        if child.is_file():
            return child.parent
    raise RuntimeError("归档包内未找到 index.json，可能仓库结构不正确")


def merge_market(remote_root: Path, remote_index: dict,
                 local_root: Path, local_index: dict) -> dict:
    """把远端归档合并进本地工作副本。

    规则：
      - 远端条目为基准；本地 updated_at 更新的同 id 条目（含远端没有的本地新增）
        视为「尚未推送」，保留本地索引与文件，避免同步吞掉未发布内容；
      - 远端存在而本地缺少的目录/文件直接补齐；
      - 不删除远端已移除的本地条目（如需删除，请在市场管理程序里删完再推送）。
    """
    remote_items = {_key(it): it for it in remote_index.get("items", []) if isinstance(it, dict)}
    local_items = {_key(it): it for it in local_index.get("items", []) if isinstance(it, dict)}

    local_newer: set[tuple[str, str]] = set()
    for key, lit in local_items.items():
        rit = remote_items.get(key)
        if rit is None or (lit.get("updated_at") or "") > (rit.get("updated_at") or ""):
            local_newer.add(key)
    skip_dirs = {_item_dir(local_items[k]) for k in local_newer if _item_dir(local_items[k])}

    added, changed, copied = [], [], 0
    for key, rit in remote_items.items():
        if key in local_newer:
            continue
        lit = local_items.get(key)
        if lit is None:
            added.append(rit)
        elif (lit.get("updated_at") or "") != (rit.get("updated_at") or ""):
            changed.append(rit)

    # —— 文件层：远端覆盖本地（跳过尚未推送的本地更新条目）——
    for src in remote_root.rglob("*"):
        rel = src.relative_to(remote_root)
        if _is_ignored(rel) or _under(rel.as_posix(), skip_dirs):
            continue
        dest = local_root / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1

    # —— 索引层：远端基准 + 本地未推送条目 ——
    merged: list[dict] = []
    for key, rit in remote_items.items():
        if key in local_newer:
            merged.append(local_items[key])
        else:
            merged.append(rit)
    for key, lit in local_items.items():
        if key not in remote_items and key in local_newer:
            merged.append(lit)
    merged.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)

    index = {
        "version": remote_index.get("version") or local_index.get("version") or "1.0",
        "updated_at": remote_index.get("updated_at") or local_index.get("updated_at"),
        "items": merged,
    }
    _write_json(local_root / "index.json", index)

    return {
        "added": len(added),
        "changed": len(changed),
        "copied_files": copied,
        "kept_local": len(local_newer),
        "total": len(merged),
        "added_items": [{"kind": it.get("kind"), "id": it.get("id"), "name": it.get("name")}
                        for it in added],
    }


# ==============================================================
# 同步
# ==============================================================
def market_ready() -> bool:
    return INDEX_FILE.exists()


def sync_market(force: bool = False, on_progress=None) -> dict:
    """从远程仓库同步市场资源到本地 ``market/``。

    force=False 时先比对远端 index.json 的 updated_at，无变化直接返回「已是最新」。
    """
    cfg = get_config()
    started = time.time()
    MARKET_ROOT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ac-market-") as tmp:
        tmpdir = Path(tmp)
        local_index = _read_json(INDEX_FILE, {"items": []})
        if not isinstance(local_index, dict):
            local_index = {"items": []}

        # 1) 轻量探测远端索引：只有 20KB 左右，镜像不支持 raw 时静默跳过
        remote_index = None
        probe_note = ""
        try:
            probe = tmpdir / "remote-index.json"
            download_file(raw_index_url(), probe, mirrors=cfg["mirrors"], timeout=20)
            remote_index = json.loads(probe.read_text(encoding="utf-8"))
            if not isinstance(remote_index, dict) or not isinstance(remote_index.get("items"), list):
                remote_index = None
        except Exception as e:
            probe_note = f"索引探测失败，直接下载归档：{e}"

        if (not force and remote_index is not None
                and remote_index.get("updated_at")
                and remote_index.get("updated_at") == local_index.get("updated_at")):
            return {
                "ok": True, "up_to_date": True, "added": 0, "changed": 0,
                "total": len(local_index.get("items", [])),
                "repo_url": cfg["repo_url"], "mirror": "index",
                "elapsed": round(time.time() - started, 2),
                "message": "市场已是最新",
            }

        # 2) 下载归档并解压
        zip_path = tmpdir / "market.zip"
        if on_progress:
            try:
                on_progress(0, 0)
            except Exception:
                pass
        dl = download_file(archive_url(), zip_path, mirrors=cfg["mirrors"],
                           timeout=180, on_progress=on_progress)

        extract_dir = tmpdir / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise RuntimeError(f"归档包含非法路径：{info.filename}")
            zf.extractall(extract_dir)

        remote_root = _find_extract_root(extract_dir)
        archive_index = _read_json(remote_root / "index.json", None)
        if not isinstance(archive_index, dict) or not isinstance(archive_index.get("items"), list):
            raise RuntimeError("归档内 index.json 格式不正确")
        if remote_index is not None:
            archive_index = remote_index  # 以 raw 探测到的索引为准（同一提交，避免 zip 缓存）

        # 3) 合并进本地工作副本
        stats = merge_market(remote_root, archive_index, MARKET_ROOT, local_index)

    return {
        "ok": True, "up_to_date": False,
        "repo_url": cfg["repo_url"], "branch": cfg["branch"],
        "mirror": dl["mirror"], "source": dl["url"], "downloaded": dl["size"],
        "probe_note": probe_note,
        "elapsed": round(time.time() - started, 2),
        **stats,
        "message": f"同步完成：新增 {stats['added']} 项，更新 {stats['changed']} 项",
    }


# ==============================================================
# git 仓库与发布推送
# ==============================================================
def _git(args: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("未找到 git 命令，无法执行仓库操作")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.run(
        [exe, "-C", str(MARKET_ROOT), *args],
        capture_output=True, timeout=timeout, env=env,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, err


def repo_state() -> dict:
    """返回本地市场目录的 git 状态（供界面展示）。"""
    cfg = get_config()
    state: dict = {
        "path": str(MARKET_ROOT),
        "is_repo": (MARKET_ROOT / ".git").exists(),
        "git_available": bool(shutil.which("git")),
        "branch": None, "remote": None, "dirty": False,
        "dirty_files": 0, "ahead": 0, "behind": 0,
        "repo_url": cfg["repo_url"],
    }
    if not state["is_repo"] or not state["git_available"]:
        return state
    try:
        code, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
        if code == 0:
            state["branch"] = out.strip()
        code, out, _ = _git(["remote", "get-url", "origin"], timeout=20)
        if code == 0:
            state["remote"] = out.strip()
        code, out, _ = _git(["status", "--porcelain"], timeout=30)
        if code == 0:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            state["dirty_files"] = len(lines)
            state["dirty"] = bool(lines)
        branch = state["branch"] or cfg["branch"]
        code, out, _ = _git(["rev-list", "--left-right", "--count",
                             f"origin/{branch}...HEAD"], timeout=30)
        if code == 0:
            parts = out.split()
            if len(parts) == 2:
                state["behind"], state["ahead"] = int(parts[0]), int(parts[1])
    except Exception as e:
        state["error"] = str(e)
    return state


def ensure_repo() -> dict:
    """确保本地 market/ 是配置仓库的 git 工作副本（必要时 init + 绑定 origin）。"""
    cfg = get_config()
    if not shutil.which("git"):
        raise RuntimeError("未找到 git 命令，无法初始化市场仓库")
    MARKET_ROOT.mkdir(parents=True, exist_ok=True)
    created = False
    if not (MARKET_ROOT / ".git").exists():
        code, out, err = _git(["init", "-b", cfg["branch"]], timeout=30)
        if code != 0:
            raise RuntimeError(f"git init 失败：{err or out}")
        created = True

    code, out, _ = _git(["remote", "get-url", "origin"], timeout=20)
    current = out.strip() if code == 0 else ""
    if not current:
        code, out, err = _git(["remote", "add", "origin", cfg["repo_url"]], timeout=20)
        if code != 0:
            raise RuntimeError(f"绑定远程仓库失败：{err or out}")
    elif current != cfg["repo_url"]:
        code, out, err = _git(["remote", "set-url", "origin", cfg["repo_url"]], timeout=20)
        if code != 0:
            raise RuntimeError(f"更新远程仓库地址失败：{err or out}")
    return {"ok": True, "created": created, "remote": cfg["repo_url"]}


def push_market(message: str | None = None, timeout: float = 240.0) -> dict:
    """把本地 market/ 的改动提交并推送到远程官方仓库。

    使用市场页登录的 GitHub 账号（PAT）：token 以一次性 HTTP 认证头传给 git，
    不写入 git config / remote URL；未登录直接拒绝，不回退本机凭据。
    """
    cfg = get_config()
    token = github_auth.get_token()
    if not token:
        raise RuntimeError(
            "尚未登录 GitHub：请先在市场页右上角登录后再上传。\n"
            "（加速镜像只负责下载，推送需要你的 GitHub 账号有市场仓库写权限）"
        )
    login = github_auth.get_login() or ""
    ensure_repo()
    branch = cfg["branch"]

    _git(["add", "-A"], timeout=60)
    commit_msg = message or f"market: publish {datetime.now():%Y-%m-%d %H:%M:%S}"
    code, out, err = _git(["commit", "-m", commit_msg], timeout=60)
    committed = code == 0
    if not committed and "nothing to commit" not in (out + err):
        raise RuntimeError(f"提交失败：{err or out}")

    commit_id = ""
    code, out, _ = _git(["rev-parse", "--short", "HEAD"], timeout=20)
    if code == 0:
        commit_id = out.strip()

    # Basic x-access-token:<token> —— GitHub 接受 PAT 作为 HTTPS 密码
    b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    code, out, err = _git(
        ["-c", f"http.extraheader=Authorization: Basic {b64}",
         "push", "-u", "origin", branch],
        timeout=timeout,
    )
    if code != 0:
        detail = (err or out).strip()
        hint = ""
        if "Repository not found" in detail or "repository not found" in detail:
            hint = "\n\n远程仓库不存在：请先在 GitHub 创建空仓库，并把地址填入市场设置。"
        elif "timed out" in detail.lower() or "could not resolve" in detail.lower():
            hint = ("\n\n网络不可达：推送需要直连官方 GitHub（镜像只加速下载），"
                    "请检查系统代理 / 网络。")
        elif ("403" in detail or "Authentication failed" in detail
              or "Permission" in detail or "denied" in detail):
            hint = (f"\n\n当前 GitHub 账号 @{login} 没有推送权限："
                    "请联系市场维护者把该账号添加为仓库协作者，"
                    "并确认 Token 勾选了 repo 写权限。")
        elif "non-fast-forward" in detail or "rejected" in detail:
            hint = "\n\n本地与远程历史分叉：请先点「同步最新」合并远程改动后再推送。"
        raise RuntimeError(f"推送失败：{detail}{hint}")

    return {
        "ok": True,
        "committed": committed,
        "commit": commit_id,
        "pushed": True,
        "branch": branch,
        "repo_url": cfg["repo_url"],
        "auth": login,
        "message": "已推送到远程市场仓库" if committed else "无新变更，已确认远程同步",
        "output": (out or "").strip()[:2000],
    }