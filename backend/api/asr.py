"""语音识别 API — 接收 base64 音频，返回识别文本"""
import os
import asyncio
import base64
import tempfile
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/status")
async def asr_status():
    from core.asr import is_available
    return {"available": is_available()}


@router.delete("/uninstall")
async def uninstall_asr():
    import shutil
    model_dir = os.path.expanduser("~/.cache/modelscope/models/iic--SenseVoiceSmall")
    if os.path.exists(model_dir):
        shutil.rmtree(model_dir, ignore_errors=True)
        return {"ok": True, "msg": "模型已卸载"}
    return {"ok": True, "msg": "模型目录不存在"}


@router.post("/transcribe")
async def transcribe_audio(payload: dict):
    audio_b64 = payload.get("audio", "")
    language = payload.get("language", "auto")
    if not audio_b64:
        return JSONResponse({"text": "", "error": "空音频"}, status_code=400)
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception:
        return JSONResponse({"text": "", "error": "音频数据解码失败"}, status_code=400)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        from core.asr import transcribe
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, transcribe, tmp_path, language)
        return {"text": text}
    except Exception as e:
        return JSONResponse({"text": "", "error": str(e)}, status_code=500)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass