@echo off
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -X utf8 -B "%~dp0skills\remote-naraeon\scripts\remote_naraeon.py" %*
exit /b %errorlevel%
:use_python
python -X utf8 -B "%~dp0skills\remote-naraeon\scripts\remote_naraeon.py" %*
exit /b %errorlevel%
