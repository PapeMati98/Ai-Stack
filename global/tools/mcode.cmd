@echo off
setlocal
set "MAVIS_REGION=en"
call "%USERPROFILE%\.minimax-code\mcode.cmd" %*
exit /b %ERRORLEVEL%
