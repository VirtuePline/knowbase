@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title 企业知识库 - 上传服务器
set "deploy_python=%~dp0.venv\Scripts\python.exe"
set "deploy_script=%~dp0scripts\deploy_direct.py"
set "deploy_status=1"
if not exist "%deploy_python%" (
    echo 找不到项目的 .venv\Scripts\python.exe，请先安装部署环境。
    goto :finish
)
if not exist "%deploy_script%" (
    echo 找不到 scripts\deploy_direct.py，请检查项目是否完整。
    goto :finish
)

echo 正在上传并验证服务...
"%deploy_python%" "%deploy_script%"
set "deploy_status=%errorlevel%"

:finish
echo.
if "%deploy_status%"=="0" (
    echo 部署完成，服务检查通过。
) else (
    echo 部署失败，退出码 %deploy_status%。请查看上方错误信息。
)
echo 按任意键关闭窗口。
pause >nul
exit /b %deploy_status%
