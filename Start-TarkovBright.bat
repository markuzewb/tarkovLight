@echo off
rem ===========================================================================
rem  GUI mode: auto gamma + slider window.  Close the window (or F8 twice and
rem  quit) to get the original picture back.  Hotkeys work over the game:
rem      F8 on/off    F7 restore    F9 boost 12 s    F10 next profile
rem  Extra args are passed to the app, e.g.:  Start-TarkovBright.bat --minimized
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_pyfind.bat"
if defined PY goto run
echo.
echo   [ERROR] Python 3.9+ that actually runs was not found.
echo           Run install.bat - it explains the fix, or use:
echo             powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Level 60
echo.
pause
exit /b 2

:run
call %PY% app\main.py %*
if errorlevel 1 (
  echo.
  echo   the app exited with an error - see the lines above.
  pause
)
exit /b 0
