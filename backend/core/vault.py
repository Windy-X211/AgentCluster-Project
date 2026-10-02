"""本地静态加密 — 用于敏感参数落盘加密（Fernet: AES-128-CBC + HMAC-SHA256）

设计边界（重要）：
  - 密钥文件保存在 backend/.runtime/param_preset.key（**不在 AppData 内**），
    因此无法通过设置页的文件浏览器浏览到；首次使用时自动生成，权限设为 0600。
  - 满足"落盘不是明文、避免误提交/备份外泄"的需求。
  - 不抗"已取得本机文件系统完全访问权限"的攻击者——那种场景下密钥也在盘上。

调用方：core/param_presets.py（能力参数预设的固定参数加密）。
"""
import base64, os
from pathlib import Path

_KEY_FILE = Path(__file__).resolve().parent.parent / ".runtime" / "param_preset.key"

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _KEY_FILE.exists():
            _KEY_FILE.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
            try:
                _KEY_FILE.chmod(0o600)
            except Exception:
                pass
        _fernet = Fernet(_KEY_FILE.read_bytes())
    return _fernet


def encrypt_value(value) -> str:
    """明文 → base64 token 字符串。空值返回空字符串。"""
    if value is None:
        value = ""
    text = str(value)
    if text == "":
        return ""
    return _get_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    """base64 token → 明文。空 token 或解密失败返回空字符串（不抛异常）。"""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def key_file_path() -> str:
    return str(_KEY_FILE)
