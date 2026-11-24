@echo off
rem ---------------------------------------------------------------
rem  padcue: double-click this file to open the control screen.
rem  Procedures, frametables and the rest of your data are saved in
rem  data\ (next to this file).
rem
rem  Quitting: press Ctrl+C in this window. cmd.exe then asks
rem  "Terminate batch job (Y/N)?" - either answer just ends it
rem  (the tool has already shut down cleanly at that point).
rem  The prompt itself cannot be suppressed; it is a fixed cmd.exe
rem  behavior for Ctrl+C during a running .bat file. We accept it
rem  to keep everything in one window.
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

rem  First launch: make the sample procedure, so the screen has
rem  something to run (getting-started section 4 uses it).
if not exist "%~dp0data\padcue.json" python -m padcue init

rem  Options after the file name go to the screen, for example
rem  "padcue.bat --port 9000" (list: python -m padcue gui --help).
python -m padcue gui %*
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
