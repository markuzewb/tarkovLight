@echo off
rem ===========================================================================
rem  GUI mode: auto gamma + slider window.  Close the window (or F7) to get
rem  the factory picture back.  Hotkeys work over the game:
rem      F8 on/off    F7 restore    F9 boost 12 s    F10 next profile
rem  Extra args go to the app, e.g.:  Start-TarkovBright.bat --minimized
rem
rem  It runs TarkovBright.pyw with python.exe ON PURPOSE: the .pyw started by
rem  double-click has no console, so if something breaks before the window is
rem  built you would see nothing at all.  Here you see the error text.
rem  Double-click TarkovBright.pyw when you just want the window.
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
call %PY% TarkovBright.pyw %*
if errorlevel 1 (
  echo.
  echo   the app exited with an error - see the lines above
  echo   (also check %%APPDATA%%\TarkovBright\error.log).
  pause
)
exit /b 0
