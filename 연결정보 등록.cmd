@echo off
call "%~dp0naraeon.cmd" register --import-existing
set "NARAEON_EXIT=%ERRORLEVEL%"
pause
exit /b %NARAEON_EXIT%
