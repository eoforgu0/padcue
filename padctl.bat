@echo off
rem ---------------------------------------------------------------
rem  padctl: double-click this file to open the control screen.
rem  Procedures are saved in procedures\ and parts in parts\
rem  (next to this file).
rem
rem  Quitting: press Ctrl+C in this window. cmd.exe then asks
rem  "Terminate batch job (Y/N)?" - either answer just ends it
rem  (the tool has already shut down cleanly at that point).
rem  The prompt itself cannot be suppressed; it is a fixed cmd.exe
rem  behavior for Ctrl+C during a running .bat file. We accept it
rem  to keep everything in one window (decision 2026-08-01).
rem
rem  NOTE: keep this file ASCII-only. cmd.exe parses it with the
rem  console code page, so non-ASCII bytes can corrupt the commands.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0pctool"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 goto nopython

python -m switchctl --project "%~dp0." gui
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
