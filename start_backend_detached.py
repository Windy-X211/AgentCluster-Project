# -*- coding: utf-8 -*-
"""分离进程启动后端（绕过 shell/powershell 启动问题）。"""
import subprocess
import sys

DETACHED = 0x00000008  # CREATE_NO_WINDOW 之外再分离
out = open(r'F:\PythonProjects\TraeProjects\智能体集群\backend\restart_out.log', 'ab')
err = open(r'F:\PythonProjects\TraeProjects\智能体集群\backend\restart_err.log', 'ab')
p = subprocess.Popen(
    [sys.executable, '-u', 'main.py'],
    cwd=r'F:\PythonProjects\TraeProjects\智能体集群\backend',
    stdout=out, stderr=err, stdin=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | DETACHED,
    close_fds=True,
)
print('backend pid:', p.pid)