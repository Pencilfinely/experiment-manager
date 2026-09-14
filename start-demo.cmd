@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "D:\anaconda3\python.exe" (
  "D:\anaconda3\python.exe" -X utf8 -m expman demo --root .runtime/demo
) else (
  python -X utf8 -m expman demo --root .runtime/demo
)
pause
