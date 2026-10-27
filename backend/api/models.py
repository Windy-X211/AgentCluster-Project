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


@router.get("")
def list_interfaces():
    return load("model_interfaces")


@router.post("")
def create_iface(i: IfaceIn):
    item = i.model_dump()
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
