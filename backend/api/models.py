from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.store import load, upsert, delete_by, get_by, next_id
from core.reasoning_detector import ReasoningDetector, detect_protocol, inject_reasoning_payload
import httpx

router = APIRouter()
_detector = ReasoningDetector()


class IfaceIn(BaseModel):
    name: str
    url: str
    api_key: str = ""
    default_model: str = "gpt-4o-mini"
    models: list[str] = []
    description: str = ""
    # 每个模型的上下文窗口（token）：{模型名: 窗口}，值支持 int 或 "128k"/"1m" 字符串
    model_windows: dict = {}


class FetchModelsIn(BaseModel):
    url: str
    api_key: str = ""


def _norm_windows(w: dict) -> dict[str, int]:
    """窗口表规范化：容忍 128k/1m/2b 字符串值，剔除非法项。"""
    out: dict[str, int] = {}
    if not isinstance(w, dict):
        return out
    for k, v in w.items():
        n = None
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            n = int(v)
        elif isinstance(v, str):
            s = v.strip().lower()
            mult = 1
            if s.endswith("k"): mult, s = 1_000, s[:-1]
            elif s.endswith("m"): mult, s = 1_000_000, s[:-1]
            elif s.endswith("b"): mult, s = 1_000_000_000, s[:-1]
            try:
                f = float(s)
                n = int(f * mult) if f > 0 else None
            except ValueError:
                n = None
        if n:
            out[str(k)] = n
    return out


@router.get("")
def list_interfaces():
    return load("model_interfaces")


def _extract_model_ids(data) -> list[str]:
    items = data
    if isinstance(data, dict):
        for key in ("data", "models", "result"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for m in items:
        mid = None
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
        elif isinstance(m, str):
            mid = m
        if not isinstance(mid, str):
            continue
        mid = mid.strip()
        if mid.startswith("models/"):
            mid = mid.split("/", 1)[1]
        if mid and mid not in out:
            out.append(mid)
    return out


@router.post("/fetch_models")
async def fetch_models(body: FetchModelsIn):
    base = (body.url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "请先填写接口 URL")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "URL 需以 http:// 或 https:// 开头")

    if base.endswith("/models"):
        candidates = [base]
    else:
        candidates = [f"{base}/models"]
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1/models")

    headers = {"Authorization": f"Bearer {body.api_key}"} if body.api_key else {}
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for u in candidates:
            try:
                resp = await client.get(u, headers=headers)
            except Exception as e:
                errors.append(f"{u}: {e}")
                continue
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    errors.append(f"{u}: HTTP {resp.status_code}（API Key 无效或未填写）")
                else:
                    errors.append(f"{u}: HTTP {resp.status_code}")
                continue
            try:
                ids = _extract_model_ids(resp.json())
            except Exception:
                errors.append(f"{u}: 响应不是合法 JSON")
                continue
            if ids:
                return {"models": ids, "count": len(ids), "endpoint": u}
            errors.append(f"{u}: 未解析到模型字段")
    raise HTTPException(400, "获取模型列表失败：" + "；".join(errors))


@router.post("")
def create_iface(i: IfaceIn):
    item = i.model_dump()
    item["model_windows"] = _norm_windows(item.get("model_windows"))
    item["id"] = next_id("model_interfaces")
    return upsert("model_interfaces", item)


@router.get("/{iid}")
def get_iface(iid: int):
    return get_by("model_interfaces", iid) or HTTPException(404, "not found")


@router.put("/{iid}")
def update_iface(iid: int, i: IfaceIn):
    cur = get_by("model_interfaces", iid)
    if not cur: raise HTTPException(404, "not found")
    cur.update(i.model_dump())
    cur["model_windows"] = _norm_windows(cur.get("model_windows"))
    cur["id"] = iid
    return upsert("model_interfaces", cur)


@router.delete("/{iid}")
def remove_iface(iid: int):
    return {"ok": delete_by("model_interfaces", iid)}

@router.get("/{iid}/reasoning/{model_name}")
async def detect_reasoning(iid: int, model_name: str):
    """检测指定接口下某个模型的推理能力档位。

    流程：调用服务商 /models 端点获取元数据 → 检测器分析 → 返回结果。
    元数据缺失时用模型名启发式匹配兜底。
    """
    iface = get_by("model_interfaces", iid)
    if not iface:
        raise HTTPException(404, "接口不存在")

    base_url = iface.get("url", "").rstrip("/")
    api_key = iface.get("api_key", "")
    protocol = detect_protocol(base_url)

    # 尝试从服务商 /models 端点获取模型元数据
    model_meta: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = await client.get(f"{base_url}/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(models_list, list):
                    for m in models_list:
                        mid = m.get("id", "") if isinstance(m, dict) else str(m)
                        if mid == model_name:
                            model_meta = m if isinstance(m, dict) else {"id": mid}
                            break
    except Exception:
        pass

    result = _detector.detect(model_meta, protocol, model_name)
    return {
        "status": result.status,
        "protocol": result.protocol,
        "parameter": result.parameter,
        "source": result.source,
        "message": result.message,
        "options": [{"label": o.label, "value": o.value} for o in result.options],
    }
