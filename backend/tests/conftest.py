"""pytest 配置：把 backend 目录加入 sys.path，使测试可导入 plugins_debug / plugins / core。"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))