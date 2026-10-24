"""ASR 识别子进程 worker —— 在 funasr(3.12) 环境下常驻运行。

协议：
  启动就绪后先向 stdout 输出一行: {"ready": true}
  随后从 stdin 逐行读 JSON 请求，向 stdout 逐行写 JSON 响应。
  请求: {"audio": "<wav 路径>", "language": "auto"}
  响应: {"text": "..."} 或 {"error": "..."}
所有日志走 stderr，不污染 stdout 的 JSON 协议。
"""
import sys
import json
import threading

_model = None
_lock = threading.Lock()


def _load_model():
    from funasr_onnx import SenseVoiceSmall
    return SenseVoiceSmall("iic/SenseVoiceSmall", batch_size=10, quantize=True)


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                _model = _load_model()
    return _model


def transcribe(audio_path, language="auto"):
    from funasr_onnx.utils.postprocess_utils import rich_transcription_postprocess
    model = get_model()
    res = model([audio_path], language=language, use_itn=True)
    return rich_transcription_postprocess(res[0])


def main():
    # 保存原始 stdout（连接后端 Popen 的管道），把第三方库的 print 重定向到 stderr
    _orig_stdout = sys.stdout
    sys.stdout = sys.stderr

    # 恢复原始 stdout 输出协议 JSON（ready + 后续每个响应）
    _orig_stdout.write(json.dumps({"ready": True}, ensure_ascii=False) + "\n")
    _orig_stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # 请求处理期间，funasr/onnx 的任何 print 都走 stderr
        sys.stdout = sys.stderr
        try:
            req = json.loads(line)
            audio = req.get("audio", "")
            lang = req.get("language", "auto")
            if not audio:
                resp = {"error": "缺少 audio 路径"}
            else:
                text = transcribe(audio, lang)
                resp = {"text": text}
        except Exception as e:
            resp = {"error": str(e)}
        # 恢复原始 stdout 输出协议 JSON 响应
        _orig_stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        _orig_stdout.flush()


if __name__ == "__main__":
    main()