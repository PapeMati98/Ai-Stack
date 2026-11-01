@echo off
python -I "%~dp0office.py" %*
exit /b %ERRORLEVEL%
