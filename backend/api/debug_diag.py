"""诊断接口：把「黑匣子」里的现场通过 HTTP 暴露出来，不用进机器翻文件。

  GET  /api/debug/diag            总览（看门狗状态、事件循环延迟、线程、最近慢请求、diag 文件列表）
  GET  /api/debug/diag/file       读某个 diag 文件的尾部（?name=stall-20261107-142000.log&lines=400）
  POST /api/debug/diag/dump       立刻抓一份现场（卡死/异常复现时点一下）
"""
from fastapi import APIRouter

from core import diagnostics

router = APIRouter()


@router.get("/diag")
def diag_summary():
    return diagnostics.summary()


@router.get("/diag/file")
def diag_file(name: str = "", lines: int = 400):
    return diagnostics.read_file_tail(name, lines)


@router.post("/diag/dump")
def diag_dump(reason: str = "manual"):
    return diagnostics.force_dump(reason)
