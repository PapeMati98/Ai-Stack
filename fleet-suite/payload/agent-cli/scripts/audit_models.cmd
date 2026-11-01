@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0audit_models.ps1" %*
