@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Allow-Worker-Connections.ps1"
if errorlevel 1 pause
