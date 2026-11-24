@echo off
rem ---------------------------------------------------------------
rem  padcue demo mode: everything happens inside a sandbox
rem  project folder "demo" (created next to this file). Two mock
rem  devices (1P and 2P) run inside this same window and are
rem  registered only in demo\padcue.json, so the real project's
rem  registered devices and procedures are never touched. Use
rem  padcue.bat instead once the real device is connected.
rem
rem  Quitting: Ctrl+C here ends the screen and the mock devices
rem  together. Answer the "Terminate batch job (Y/N)?" prompt with
rem  either key (see padcue.bat).
rem
rem  NOTE: keep this file ASCII-only (see padcue.bat).
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PRJ=%~dp0demo"
set "PYTHONPATH=%~dp0pctool"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 goto nopython

if not exist "%PRJ%\padcue.json" python -m padcue --project "%PRJ%" init

rem  Options after the file name go to the screen (see padcue.bat).
python -m padcue --project "%PRJ%" gui --mock 2 %*
if errorlevel 1 pause
endlocal
exit /b 0

:nopython
echo.
echo Python was not found.
echo Install Python 3.11 or later from https://www.python.org/downloads/
echo and check that the "python" command works in a new window.
echo Then try again.
echo.
pause
endlocal
exit /b 1
