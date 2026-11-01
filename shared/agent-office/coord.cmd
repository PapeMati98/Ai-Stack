@echo off
python -I "%~dp0coord.py" %*
exit /b %ERRORLEVEL%
