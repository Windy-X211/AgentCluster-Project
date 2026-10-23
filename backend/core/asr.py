"""SenseVoice 语音识别封装（懒加载，首次调用时才加载模型）"""
import threading

_model = None
_lock = threading.Lock()


def is_available() -> bool:
    try:
        import funasr_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model():
    try:
        from funasr_onnx import SenseVoiceSmall
    except ImportError:
        raise RuntimeError("funasr-onnx 未安装，请在后端目录执行: pip install funasr-onnx")
    return SenseVoiceSmall("iic/SenseVoiceSmall", batch_size=10, quantize=True)


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                _model = _load_model()
    return _model


def transcribe(audio_path: str, language: str = "auto") -> str:
    """识别音频文件，返回纯文本。language: auto/zh/en/yue/ja/ko"""
    try:
        from funasr_onnx.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError:
        raise RuntimeError("funasr-onnx 未安装，请在后端目录执行: pip install funasr-onnx")
    model = get_model()
    res = model([audio_path], language=language, use_itn=True)
    return rich_transcription_postprocess(res[0])