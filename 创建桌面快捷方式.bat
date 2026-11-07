@echo off
rem 在桌面创建「智能体集群」快捷方式（与项目内 AgentCluster.lnk 同一启动链路）:
rem   pythonw.exe launcher.py  —— 无控制台静默启动
rem   图标使用项目自带 agentcluster.ico
rem 注意: 本文件必须保持 GBK(ANSI) 编码, 不要转存为 UTF-8
cd /d "%~dp0"
title 创建桌面快捷方式 - 智能体集群
echo 正在创建桌面快捷方式...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$root='%~dp0'.TrimEnd('\');$launcher=Join-Path $root 'launcher.py';$ico=Join-Path $root 'agentcluster.ico';if(-not(Test-Path $launcher)){Write-Host '[失败] 未找到 launcher.py';exit 1};$pyw=$null;foreach($c in @((Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source,(Get-Command python.exe -ErrorAction SilentlyContinue).Source,'C:\Windows\pyw.exe','F:\anaconda3\pythonw.exe','C:\ProgramData\anaconda3\pythonw.exe')){if($c -and $c -notlike '*WindowsApps*'){if($c -like '*python.exe'){$c=Join-Path (Split-Path $c) 'pythonw.exe'};if($c -and (Test-Path $c)){$pyw=$c;break}}};if(-not $pyw){Write-Host '[失败] 未找到 pythonw.exe，请先安装 Python';exit 1};$desktop=[Environment]::GetFolderPath('Desktop');$lnk=Join-Path $desktop '智能体集群.lnk';$sh=New-Object -ComObject WScript.Shell;$s=$sh.CreateShortcut($lnk);$s.TargetPath=$pyw;$s.Arguments=[char]34+$launcher+[char]34;$s.WorkingDirectory=$root;$s.IconLocation=$ico+',0';$s.Description='AgentCluster 智能体集群';$s.Save();Write-Host '[成功] 桌面快捷方式已创建:' $lnk"
echo.
pause