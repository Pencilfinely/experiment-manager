@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Worker.ps1" -ConfigureProject
if errorlevel 1 pause
