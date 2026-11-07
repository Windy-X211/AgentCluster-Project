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
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import github_auth, store

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARKET_ROOT = _PROJECT_ROOT.parent / "market"
INDEX_FILE = MARKET_ROOT / "index.json"

DEFAULT_REPO_URL = "https://github.com/Windy-X211/AgentCluster-Market.git"
# {url} 会被替换成完整的官方下载地址；顺序即测速优先级（越靠前越快越稳）
# 末尾自动追加官方地址作为兜底（见 mirror_candidates 函数）
DEFAULT_MIRRORS = [
    # 2026-11-06 实测：index.json 并发 race，TOP 2 均 <1s，race 整体 ~1.7s
    "https://gh-proxy.org/{url}",        # ~0.66s 实测最快
    "https://gh-proxy.com/{url}",        # ~0.83s 实测次快
    "https://ghfast.top/{url}",          # 备用 ~7s
    "https://ghproxy.net/{url}",          # 备用 ~8s
    "https://mirror.ghproxy.com/{url}",
    "https://gh-proxy.cn/{url}",
    "https://gh-proxy.cc/{url}",
    "https://gh-proxy.1ms.run/{url}",
    "https://ghproxy.1ms.run/{url}",
    "https://kkgithub.com/{url}",
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
    """读取市场仓库配置（环境变量优先级最高）。

    智能迁移：当 settings 里保存的 mirrors 数量过少（<5）时，自动把
    DEFAULT_MIRRORS 里的新镜像追加到已有列表后面，让旧用户升级后立即受益。
    """
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
    elif len(mirrors) < len(DEFAULT_MIRRORS) // 2 + 2:
        # 旧用户：镜像数太少，追加 DEFAULT_MIRRORS 里的新条目（去重保序）
        existing = set(mirrors)
        for m in DEFAULT_MIRRORS:
            if m not in existing:
                mirrors.append(m)
                existing.add(m)
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
    # 不再自动追加官方 raw.githubusercontent.com 兜底——国内访问经常 8s+ 超时
    # 会在并发 race 里占线程位拖慢整体；如需兜底请手动加进 mirrors 列表
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url[:32]
    except Exception:
        return "unknown"


# ==============================================================
# 下载（并发 race：多源同时启动，谁快用谁）
# ==============================================================
def _safe_host(url: str) -> str:
    """把 host 里的冒号 / 点替换成下划线，用作临时文件名。"""
    return re.sub(r"[^\w\-]", "_", _host_of(url))


def _download_worker(cand_url: str, dest: Path, timeout: float,
                     cancel_event: threading.Event,
                     on_progress=None) -> dict:
    """单个镜像源的下载 worker。cancel_event 置位后主动 close 连接。"""
    started = time.monotonic()
    try:
        req = Request(cand_url, headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            total = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                pass
            loaded = 0
            deadline = started + timeout
            with open(dest, "wb") as f:
                while not cancel_event.is_set():
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"单源超时（>{timeout:.0f}s，已收 {loaded} 字节）")
                    chunk = resp.read(128 * 1024)  # 128KB 一块
                    if not chunk:
                        break
                    f.write(chunk)
                    loaded += len(chunk)
                    if on_progress:
                        try:
                            on_progress(loaded, total)
                        except Exception:
                            pass
            if cancel_event.is_set():
                # 被更快的源抢跑，本 worker 提前退出
                raise RuntimeError("race 失败：被其他源抢先完成")
        size = dest.stat().st_size
        if size <= 0:
            raise RuntimeError("下载内容为空")
        return {"url": cand_url, "mirror": _host_of(cand_url), "size": size,
                "total": total or size}
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


def download_file(url: str, dest: Path, *,
                  mirrors: list[str] | None = None,
                  timeout: float = 120.0,
                  on_progress=None) -> dict:
    """并发 race 下载：多个镜像源同时启动，第一个成功完成的胜出。

    超时策略：整次 race 总时限 ``timeout``（默认 120s），每个 worker 内部再做单源
    超时保护。首个获胜者返回后，其余 worker 收到取消信号并主动关闭连接。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates = mirror_candidates(url, mirrors)
    errors: list[str] = []
    cancel_event = threading.Event()
    start = time.monotonic()

    # 为每个候选源分配独立的临时文件（避免并发写冲突）
    tmp_by_cand: dict[str, Path] = {}
    futures = {}

    # 并发数封顶 6，避免请求过多反而拖慢
    max_workers = min(len(candidates), 6)

    # ⚠️ 不能用 `with ThreadPoolExecutor()`：它会 wait=True 等所有线程结束，
    # winner 2s 就返回但 ghproxy.net 卡 13s urlopen，整体还是 13s+。
    # 必须显式 shutdown(wait=False, cancel_futures=True) 拿到 winner 立即返回。
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        for cand in candidates:
            tmp = dest.with_name(f"{dest.name}.part-{_safe_host(cand)}")
            tmp_by_cand[cand] = tmp
            futures[cand] = pool.submit(
                _download_worker, cand, tmp, timeout, cancel_event, on_progress,
            )

        pending = set(futures.values())
        winner_result: dict | None = None

        while pending and winner_result is None:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                break
            # 每 2 秒检查一次，避免永久阻塞
            done, pending = wait(pending, timeout=min(remaining, 2.0),
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                cand_url = next(k for k, v in futures.items() if v is fut)
                try:
                    winner_result = fut.result(timeout=0)
                    cancel_event.set()  # 通知其他 worker 主动退出
                    break
                except Exception as e:
                    errors.append(f"{cand_url} → {e}")
    finally:
        cancel_event.set()
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pool.shutdown(wait=False)

    if winner_result is None:
        # 全源失败：清理所有 .part 文件
        for tmp in tmp_by_cand.values():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(
            "下载失败（并发 race 已尝试 %d 个源，总耗时 %.1fs）：\n%s"
            % (len(candidates), time.monotonic() - start, "\n".join(errors))
        )

    # 把获胜者的临时文件移到正式目标路径
    winner_url = winner_result["url"]
    winner_tmp = tmp_by_cand.get(winner_url)
    if winner_tmp:
        try:
            os.replace(str(winner_tmp), str(dest))
        except OSError:
            try:
                shutil.move(str(winner_tmp), str(dest))
            except OSError:
                pass

    # 清理其他 worker 的 .part 文件（它们可能还在后台写，无所谓）
    for cand, tmp in tmp_by_cand.items():
        if cand != winner_url:
            try:
                tmp.unlink()
            except OSError:
                pass

    return winner_result


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
                 local_root: Path, local_index: dict,
                 mine_author: str | None = None) -> dict:
    """把远端归档合并进本地工作副本。

    规则：
      - 远端条目为基准；本地 updated_at 更新的同 id 条目视为「尚未推送」，
        保留本地索引与文件；
      - 远端存在而本地缺少的目录/文件直接补齐；
      - 远端不存在的本地条目：仅当 author == mine_author 时保留
        （用户自己发布的还没 push），其余视为僵尸丢弃。
    """
    remote_items = {_key(it): it for it in remote_index.get("items", []) if isinstance(it, dict)}
    local_items = {_key(it): it for it in local_index.get("items", []) if isinstance(it, dict)}

    local_newer: set[tuple[str, str]] = set()
    for key, lit in local_items.items():
        rit = remote_items.get(key)
        if rit and (lit.get("updated_at") or "") > (rit.get("updated_at") or ""):
            local_newer.add(key)
    # 本地独有但 author == mine_author 的也视为未推送，保留其文件目录
    mine_only: set[tuple[str, str]] = set()
    for key, lit in local_items.items():
        rit = remote_items.get(key)
        if rit is None and mine_author and (lit.get("author") or "") == mine_author:
            mine_only.add(key)
    skip_dirs = {_item_dir(local_items[k])
                 for k in (local_newer | mine_only) if _item_dir(local_items[k])}

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

    # —— 索引层：远端基准 + 本地未推送条目 + 我的未 push 条目 ——
    merged: list[dict] = []
    for key, rit in remote_items.items():
        if key in local_newer:
            merged.append(local_items[key])
        else:
            merged.append(rit)
    for key, lit in local_items.items():
        if key not in remote_items and (key in local_newer or key in mine_only):
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
        "kept_local": len(local_newer) + len(mine_only),
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
    """**纯索引同步**：只下载远端 index.json 列表（几 KB），不拉取任何资源文件。

    资源文件在用户「预览」或「安装」时按需拉取（走 ensure_item_local 的
    archive zip 持久化缓存）。彻底避免市场扩大后每次同步都下载几百 MB。
    """
    cfg = get_config()
    started = time.time()
    MARKET_ROOT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ac-market-") as tmp:
        tmpdir = Path(tmp)
        local_index = _read_json(INDEX_FILE, {"items": []})
        if not isinstance(local_index, dict):
            local_index = {"items": []}

        # —— 并发 race 下载 index.json（raw.githubusercontent.com 代理）——
        index_path = tmpdir / "index.json"
        dl = download_file(raw_index_url(), index_path, mirrors=cfg["mirrors"],
                           timeout=60, on_progress=on_progress)

        remote_index = _read_json(index_path, None)
        if not isinstance(remote_index, dict) or not isinstance(remote_index.get("items"), list):
            raise RuntimeError("远端 index.json 格式不正确")

        # updated_at 比对（索引层面）
        local_updated = local_index.get("updated_at")
        remote_updated = remote_index.get("updated_at")
        up_to_date = (not force and remote_updated and remote_updated == local_updated)
        if up_to_date:
            return {
                "ok": True, "up_to_date": True, "index_only": True,
                "repo_url": cfg["repo_url"], "mirror": dl.get("mirror", "unknown"),
                "elapsed": round(time.time() - started, 2),
                "message": "市场索引已是最新",
                "total": len(local_index.get("items", [])),
            }

        # 合并索引：远端为基准 + 本地未推送条目（本地 updated_at 更新的条目保留）
        # 同时保留"当前登录用户发布但还没 push"的本地独有条目
        mine_author = github_auth.get_login() or None
        merged = _merge_index(remote_index, local_index, mine_author=mine_author)

        local_items = {_key(it): it for it in local_index.get("items", [])
                       if isinstance(it, dict)}
        remote_items = {_key(it): it for it in remote_index.get("items", [])
                        if isinstance(it, dict)}
        added = [remote_items[k] for k in remote_items if k not in local_items]
        changed = [remote_items[k] for k in remote_items
                   if k in local_items
                   and (local_items[k].get("updated_at") or "")
                   != (remote_items[k].get("updated_at") or "")]

        _write_json(INDEX_FILE, merged)

    return {
        "ok": True, "up_to_date": up_to_date, "index_only": True,
        "repo_url": cfg["repo_url"], "branch": cfg["branch"],
        "mirror": dl.get("mirror"), "source": dl.get("url"),
        "downloaded": dl.get("size"),
        "elapsed": round(time.time() - started, 2),
        "added": len(added), "changed": len(changed),
        "total": len(merged.get("items", [])),
        "archive_merged": False,
        "message": _build_sync_message(False, len(added), len(changed), False, {}),
    }


def _count_workspace_files() -> int:
    """market/ 下排除 .git、index.json 后的普通文件数。快速判断工作区是否接近空。"""
    count = 0
    try:
        for p in MARKET_ROOT.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(MARKET_ROOT).as_posix()
            if rel.startswith(".git/") or rel == "index.json":
                continue
            count += 1
    except OSError:
        pass
    return count


def _archive_merge_workspace(remote_root_index: dict, local_index: dict,
                             on_progress=None) -> dict:
    """下载 archive zip → 全量 merge 进 market/。用于首次初始化或工作区残缺兜底。"""
    cfg = get_config()
    with tempfile.TemporaryDirectory(prefix="ac-market-full-") as tmp:
        tmpdir = Path(tmp)
        zip_path = tmpdir / "market.zip"
        dl = download_file(archive_url(), zip_path, mirrors=cfg["mirrors"],
                           timeout=180, on_progress=on_progress)
        extract_dir = tmpdir / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise RuntimeError(f"归档包含非法路径：{info.filename}")
            zf.extractall(extract_dir)
        remote_root = _find_extract_root(extract_dir)
        remote_index = _read_json(remote_root / "index.json", remote_root_index)
        stats = merge_market(remote_root, remote_index, MARKET_ROOT, local_index)
    return {"mirror": dl.get("mirror"), "copied_files": stats.get("copied_files"),
            "kept_local": stats.get("kept_local"), "stats": stats}


def _build_sync_message(up_to_date: bool, added: int, changed: int,
                        archive_merged: bool, merge_info: dict) -> str:
    if up_to_date and not archive_merged:
        return "市场索引已是最新"
    parts = [f"索引同步完成：{added} 新增，{changed} 更新"]
    if archive_merged:
        info = merge_info or {}
        parts.append(f"（工作区残缺，已自动从 archive 恢复 {info.get('copied_files', 0)} 个文件）")
    parts.append("资源按需拉取" if not archive_merged else "")
    return "".join(p for p in parts if p)


def _merge_index(remote_index: dict, local_index: dict,
                 mine_author: str | None = None) -> dict:
    """合并远端与本地索引。

    规则：
      - 远端条目为基准；
      - 本地 updated_at 更大的同 id 条目视为「尚未推送」，保留本地；
      - 远端不存在的本地条目：
          author == mine_author  → 保留（用户自己发布的，还没 push）
          否则                   → 丢弃（僵尸：远端仓库已删除的条目）
    """
    remote_items = {_key(it): it for it in remote_index.get("items", [])
                   if isinstance(it, dict)}
    local_items = {_key(it): it for it in local_index.get("items", [])
                   if isinstance(it, dict)}

    merged: list[dict] = []
    for key, rit in remote_items.items():
        lit = local_items.get(key)
        if lit and (lit.get("updated_at") or "") > (rit.get("updated_at") or ""):
            merged.append(lit)
        else:
            merged.append(rit)
    # 本地独有：只保留"当前登录用户发布"的条目（还没 push 的）
    for key, lit in local_items.items():
        if key not in remote_items:
            if mine_author and (lit.get("author") or "") == mine_author:
                merged.append(lit)
    merged.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)

    return {
        "version": remote_index.get("version") or local_index.get("version") or "1.0",
        "updated_at": remote_index.get("updated_at") or local_index.get("updated_at"),
        "items": merged,
    }


# ==============================================================
# 按需拉取单个资源（archive zip 持久化缓存 → 只解压目标子目录）
# ==============================================================
# archive zip 的持久化缓存目录（跨进程、跨会话复用）
import hashlib as _hl
_CACHE_ROOT = Path(os.environ.get("AGENTCLUSTER_CACHE_HOME",
                                Path.home() / ".cache" / "agentcluster" / "market"))
_ZIP_CACHE_DIR = _CACHE_ROOT / "archives"
ZIP_CACHE_TTL_SEC = 24 * 60 * 60  # 24h 过期（远端仓库分支更新后自动失效）


def find_index_item(kind: str, item_id: str) -> dict | None:
    """从本地 index.json 里查找指定条目。"""
    if not INDEX_FILE.exists():
        return None
    idx = _read_json(INDEX_FILE, {"items": []})
    if not isinstance(idx, dict):
        return None
    for it in idx.get("items", []):
        if isinstance(it, dict) and it.get("kind") == kind and it.get("id") == item_id:
            return it
    return None


def _item_cache_dir(item: dict) -> Path:
    """资源在本地 market/ 里的缓存目录。"""
    d = _item_dir(item)
    if not d:
        raise RuntimeError(f"条目缺少 dir 字段：{item.get('kind')}/{item.get('id')}")
    return MARKET_ROOT / d


def _cache_key_for_url(url: str) -> str:
    return _hl.md5(url.encode("utf-8")).hexdigest()[:16]


def _get_cached_archive(archive_url_str: str) -> Path | None:
    """返回有效的缓存 zip 路径，不存在或过期返回 None。"""
    cache_path = _ZIP_CACHE_DIR / f"{_cache_key_for_url(archive_url_str)}.zip"
    if not cache_path.exists():
        return None
    age = time.time() - cache_path.stat().st_mtime
    if age > ZIP_CACHE_TTL_SEC:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return None
    if cache_path.stat().st_size == 0:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return None
    return cache_path


def _put_cached_archive(src: Path, archive_url_str: str) -> Path:
    """把下载好的 zip 移动到持久化缓存目录，返回缓存路径。"""
    _ZIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _ZIP_CACHE_DIR / f"{_cache_key_for_url(archive_url_str)}.zip"
    try:
        os.replace(str(src), str(cache_path))
    except OSError:
        try:
            shutil.copy2(str(src), str(cache_path))
        except OSError:
            pass
    return cache_path


def ensure_item_local(kind: str, item_id: str, *,
                      force: bool = False, on_progress=None,
                      requested_version: str | None = None) -> Path:
    """确保指定资源的目录已下载到本地 market/。

    策略：
      1. 如果资源目录已存在且非强制 → 直接返回（毫秒级）
      2. 如果 archive zip 持久化缓存有效 → 用缓存解压（秒级，无需联网）
      3. 否则 → 下载 archive zip → 缓存 → 解压

    首次下载可能较慢（~40s，取决于网络和镜像），但之后全部秒级完成。

    requested_version：可选，指定要拉取的历史版本（如 "1.0.0"）。
      不传时默认取最新版本；传入时会在 item.versions 数组里查找对应条目，
      用其 dir（通常带 @version 后缀）定位目录。
    """
    item = find_index_item(kind, item_id)
    if not item:
        raise RuntimeError(f"索引中未找到资源：{kind}/{item_id}")

    # 如果指定了历史版本，从 versions 数组里找对应条目的 dir
    if requested_version and requested_version != item.get("version"):
        target_entry = None
        for v in (item.get("versions") or []):
            if v.get("version") == requested_version:
                target_entry = v
                break
        if target_entry:
            item = {**item, "dir": target_entry.get("dir") or item.get("dir")}
        # 找不到也不报错，用顶层 item 的 dir 兜底（可能版本未归档）

    target = _item_cache_dir(item)
    if target.exists() and any(target.iterdir()) and not force:
        return target

    cfg = get_config()
    archive = archive_url()

    # 尝试使用持久化 zip 缓存
    cached_zip = _get_cached_archive(archive)
    zip_to_use: Path
    with tempfile.TemporaryDirectory(prefix="ac-market-unzip-") as tmp_extract:
        extract_dir = Path(tmp_extract)

        if cached_zip:
            zip_to_use = cached_zip
        else:
            # 下载 → 缓存到持久化目录
            with tempfile.TemporaryDirectory(prefix="ac-market-zip-") as tmp_dl:
                tmp_zip = Path(tmp_dl) / "market.zip"
                download_file(archive, tmp_zip, mirrors=cfg["mirrors"],
                              timeout=180, on_progress=on_progress)
                cached_zip = _put_cached_archive(tmp_zip, archive)
            zip_to_use = cached_zip

        # 解压（只用目标子目录，但为了简洁和 zipfile 简单，完整解压到临时目录）
        with zipfile.ZipFile(zip_to_use) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise RuntimeError(f"归档包含非法路径：{info.filename}")
            zf.extractall(extract_dir)

        remote_root = _find_extract_root(extract_dir)
        target_rel = _item_dir(item)
        src_dir = remote_root / target_rel

        if not src_dir.exists():
            raise RuntimeError(
                f"远端仓库中不存在目录 {target_rel}（可能资源已被下架）"
            )

        # 清理旧版本 + 复制
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)

        for src in src_dir.rglob("*"):
            rel = src.relative_to(src_dir)
            if _is_ignored(rel):
                continue
            dest = target / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
            except OSError:
                pass

    return target


def get_item_cache_status(kind: str, item_id: str) -> dict:
    """返回指定资源在本地的缓存状态。"""
    item = find_index_item(kind, item_id)
    if not item:
        return {"exists": False, "error": "not_found"}
    target = _item_cache_dir(item)
    exists = target.exists() and any(target.iterdir())
    size = 0
    file_count = 0
    if exists:
        for p in target.rglob("*"):
            if p.is_file():
                size += p.stat().st_size
                file_count += 1
    return {
        "exists": exists,
        "path": str(target),
        "size": size,
        "file_count": file_count,
        "updated_at": item.get("updated_at"),
    }


def clean_market_cache(preserve_index: bool = True, preserve_git: bool = False) -> dict:
    """清理 market/ 目录下的资源缓存。

    默认行为：
      - 保留 index.json（保留索引元数据，用户列表/预览/安装会按需重新拉取）
      - **删除 .git**（push 时 ensure_repo() 会自动重新 init + 绑定 origin，完全不需要先保留着）

    用户已安装到 store.ROOT 的资源不受影响。
    """
    if not MARKET_ROOT.exists():
        return {"cleaned": 0, "freed_bytes": 0}

    freed = 0
    cleaned = 0
    index_path = MARKET_ROOT / "index.json"
    preserved_index = index_path.read_text(encoding="utf-8") if preserve_index and index_path.exists() else None

    def _rm_child(child: Path) -> int:
        """删除一个子项（文件或目录），返回释放的字节数。Windows 友好。"""
        sz = 0
        try:
            if child.is_dir():
                for f in child.rglob("*"):
                    if f.is_file():
                        try:
                            sz += f.stat().st_size
                        except OSError:
                            pass
                _rmtree_force(child)
            else:
                try:
                    sz = child.stat().st_size
                except OSError:
                    pass
                try:
                    child.unlink()
                except OSError:
                    _rmtree_force(child)
        except Exception:
            pass
        return sz

    for child in list(MARKET_ROOT.iterdir()):
        if preserve_index and child.name == "index.json":
            continue
        if preserve_git and child.name == ".git":
            continue
        freed += _rm_child(child)
        cleaned += 1

    # 重建空目录结构
    MARKET_ROOT.mkdir(parents=True, exist_ok=True)
    if preserved_index:
        index_path.write_text(preserved_index, encoding="utf-8")

    return {
        "cleaned_entries": cleaned,
        "freed_bytes": freed,
        "freed_human": f"{freed / (1024 * 1024):.1f} MB",
        "preserved_index": bool(preserved_index),
        "preserved_git": preserve_git,
    }


def _rmtree_force(path: Path) -> None:
    """Windows 友好的 rmtree：先去除只读属性，再递归删除。"""
    import stat as _stat
    if not path.exists():
        return

    def _onerror(func, p, exc_info):
        # 去掉只读标记后重试
        try:
            os.chmod(p, _stat.S_IWRITE)
            func(p)
        except Exception:
            # 仍失败则尝试用 cmd /c rd /s /q（Windows 下的万能删除）
            try:
                subprocess.run(
                    ["cmd", "/c", "rd", "/s", "/q", str(p)],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass

    try:
        shutil.rmtree(str(path), onerror=_onerror)
    except Exception:
        # 最后兜底：用系统命令
        try:
            subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", str(path)],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass


def market_cache_overview() -> dict:
    """返回 market/ 当前的缓存概况，**分开统计 .git、资源目录、zip 归档缓存**。"""
    if not MARKET_ROOT.exists():
        return {"exists": False}

    total_size = 0
    git_size = 0
    resource_size = 0
    index_size = 0
    file_count = 0
    dir_count = 0

    for p in MARKET_ROOT.rglob("*"):
        if p.is_file():
            try:
                sz = p.stat().st_size
                total_size += sz
                file_count += 1
                rel = p.relative_to(MARKET_ROOT)
                if rel.parts[0] == ".git":
                    git_size += sz
                elif p.name == "index.json":
                    index_size += sz
                else:
                    resource_size += sz
            except OSError:
                pass
        elif p.is_dir():
            dir_count += 1

    # zip 持久化缓存（独立于 market/）
    zip_cache_size = 0
    zip_cache_count = 0
    has_zip_cache = _ZIP_CACHE_DIR.exists()
    if has_zip_cache:
        for zf in _ZIP_CACHE_DIR.glob("*.zip"):
            try:
                zip_cache_size += zf.stat().st_size
                zip_cache_count += 1
            except OSError:
                pass

    return {
        "path": str(MARKET_ROOT),
        "total_bytes": total_size,
        "total_human": f"{total_size / (1024 * 1024):.1f} MB",
        "file_count": file_count,
        "dir_count": dir_count,
        "has_git": git_size > 0,
        "git_bytes": git_size,
        "git_human": f"{git_size / (1024 * 1024):.1f} MB",
        "resource_bytes": resource_size,
        "resource_human": f"{resource_size / (1024 * 1024):.1f} MB",
        "index_size": index_size,
        "has_zip_cache": has_zip_cache,
        "zip_cache_bytes": zip_cache_size,
        "zip_cache_human": f"{zip_cache_size / (1024 * 1024):.1f} MB",
        "zip_cache_count": zip_cache_count,
    }


def clean_zip_cache() -> dict:
    """单独清理 archive zip 持久化缓存目录。"""
    freed = 0
    removed = 0
    if not _ZIP_CACHE_DIR.exists():
        return {"removed": 0, "freed_bytes": 0}
    for zf in list(_ZIP_CACHE_DIR.glob("*.zip")):
        try:
            freed += zf.stat().st_size
            zf.unlink()
            removed += 1
        except OSError:
            pass
    return {
        "removed": removed,
        "freed_bytes": freed,
        "freed_human": f"{freed / (1024 * 1024):.1f} MB",
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


def pending_push_paths() -> dict:
    """返回本地 market 中「尚未推送到远程」的路径（相对 market/）。

    判定来源：
    - 工作区未提交/未跟踪的改动（git status --porcelain）
    - 已提交但远端分支还没有的改动（git diff --name-only origin/<branch>...HEAD）

    index.json 不计入：任何条目变化都会改它，计入会让所有条目都显得未推送。

    返回 ``{"known": bool, "all_pending": bool, "paths": list[str]}``：
    - known=False：不是 git 仓库 / 未安装 git → 调用方不应标记
    - all_pending=True：本地是仓库但没有可用的 origin/<branch> 引用
      （从未 fetch/push 过）→ 所有条目都应视为未推送
    """
    result: dict = {"known": False, "all_pending": False, "paths": []}
    if not (MARKET_ROOT / ".git").exists() or not shutil.which("git"):
        return result
    cfg = get_config()
    paths: set[str] = set()
    try:
        code, out, _ = _git(["status", "--porcelain"], timeout=30)
        if code != 0:
            return result
        for line in out.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:].strip()
            if " -> " in rel:  # 重命名行：取新路径
                rel = rel.split(" -> ", 1)[1]
            rel = rel.strip('"').replace("\\", "/")
            if rel and rel != "index.json":
                paths.add(rel)

        branch = cfg["branch"]
        code, _, _ = _git(["rev-parse", "--verify", f"origin/{branch}"], timeout=20)
        if code != 0:
            result["known"] = True
            result["all_pending"] = True
            return result
        code, out, _ = _git(["diff", "--name-only", f"origin/{branch}...HEAD"], timeout=60)
        if code != 0:
            result["known"] = True
            result["all_pending"] = True
            return result
        for rel in out.splitlines():
            rel = rel.strip().replace("\\", "/")
            if rel and rel != "index.json":
                paths.add(rel)
    except Exception:
        return result
    result["known"] = True
    result["paths"] = sorted(paths)
    return result


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

    无感自动处理：远程领先时 ``git pull --rebase --strategy-option=ours``
    （冲突自动保留本地版本），仍不行则 reset hard 丢弃远程差异再 push。
    只有网络/权限/仓库不存在这类硬错误才会抛 RuntimeError。
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

    # Basic x-access-token:<token> —— GitHub 接受 PAT 作为 HTTPS 密码
    b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    auth_prefix = ["-c", f"http.extraheader=Authorization: Basic {b64}"]

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

    def _do_push() -> tuple[int, str, str]:
        return _git([*auth_prefix, "push", "-u", "origin", branch], timeout=timeout)

    def _do_pull_rebase(strategy: str = "ours") -> tuple[int, str, str]:
        """带 PAT 认证的 pull --rebase。strategy=ours 冲突时保留本地版本。"""
        return _git([*auth_prefix, "pull", "--rebase", "--no-edit",
                     "--strategy-option", strategy,
                     "origin", branch], timeout=timeout)

    def _do_fetch_reset() -> tuple[int, str, str]:
        """fetch 远程然后 reset --hard 丢弃远程差异再 push（终极兜底）。"""
        code_f, out_f, err_f = _git([*auth_prefix, "fetch", "origin", branch], timeout=60)
        if code_f != 0:
            return code_f, out_f, err_f
        return _git(["reset", "--hard", f"origin/{branch}"], timeout=30)

    # —— 第一轮 push ——
    code, out, err = _do_push()
    rebased = False
    conflict_resolved = False

    if code != 0 and ("non-fast-forward" in (err + out) or "rejected" in (err + out)):
        # 远程领先 → 自动 pull --rebase -X ours（冲突保留本地）
        code_r, out_r, err_r = _do_pull_rebase("ours")
        if code_r == 0:
            rebased = True
        else:
            # 仍然冲突（-X ours 也救不了）→ abort rebase + reset hard 到远程
            _git(["rebase", "--abort"], timeout=30)
            conflict_resolved = True
            code_r2, out_r2, err_r2 = _do_fetch_reset()
            if code_r2 != 0:
                # 终极兜底也失败 → 返回错误字符串但不抛异常（调用方决定是否阻断）
                detail = (err_r2 or out_r2 or err_r or out_r or err or out).strip()
                raise RuntimeError(
                    f"推送失败：远程有新提交，自动同步仍无法完成。"
                    f" 你可以稍后重试，或联系仓库维护者手动整理。"
                )
            rebased = True

        # 再 push 一次
        commit_id = ""
        code, out, _ = _git(["rev-parse", "--short", "HEAD"], timeout=20)
        if code == 0:
            commit_id = out.strip()
        code, out, err = _do_push()

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
        raise RuntimeError(f"推送失败：{detail}{hint}")

    msg = "已推送到远程市场仓库"
    if conflict_resolved:
        msg += "（远程有新提交，已自动以本地为准合并）"
    elif rebased:
        msg += "（已自动合并远程最新提交）"
    if not committed:
        msg = "无新变更，已确认远程同步"

    return {
        "ok": True,
        "committed": committed,
        "rebased": rebased,
        "conflict_resolved": conflict_resolved,
        "commit": commit_id,
        "pushed": True,
        "branch": branch,
        "repo_url": cfg["repo_url"],
        "auth": login,
        "message": msg,
        "output": (out or "").strip()[:2000],
    }