@echo off
cd /d "%~dp0"
"runtime\python.exe" -m expman.harness_project wizard
pause
