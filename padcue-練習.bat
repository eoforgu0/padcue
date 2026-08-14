@echo off
rem ---------------------------------------------------------------
rem  padcue practice mode: starts the mock device, then opens the
rem  control screen. Use padcue.bat instead when the real device is
rem  connected.
rem
rem  Quitting: Ctrl+C here, then answer the "Terminate batch job
rem  (Y/N)?" prompt with either key (see padcue.bat). Close the
rem  minimized "padcue mock device" window separately.
rem
rem  NOTE: keep this file ASCII-only (see padcue.bat).
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0pctool"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 goto nopython

start "padcue mock device" /min python -m padcue mock
rem give the mock a moment to start listening
ping -n 3 127.0.0.1 >nul
python -m padcue --project "%~dp0." device 127.0.0.1

python -m padcue --project "%~dp0." gui
if errorlevel 1 pause
endlocal
exit /b 0

:nopython
echo.
echo Python is not installed (or not on PATH).
echo Install it from https://www.python.org/ and tick
echo "Add python.exe to PATH" during setup, then try again.
echo.
pause
endlocal
exit /b 1
