@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-Worker.ps1" %*
if errorlevel 1 pause
