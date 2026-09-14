@echo off
cd /d "%~dp0"
"runtime\python.exe" -m expman.launcher controller
if errorlevel 1 pause
