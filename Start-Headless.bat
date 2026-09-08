@echo off
rem ===========================================================================
rem  No window, background only: auto gamma + global hotkeys.
rem  Ctrl+C in this console stops it (the gamma ramp is restored on exit).
rem  F8 on/off    F7 restore    F9 boost 12 s    F10 next profile
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_pyfind.bat"
if defined PY goto run
echo.
echo   [ERROR] Python 3.9+ that actually runs was not found. Run install.bat.
echo.
pause
exit /b 2

:run
echo   TarkovBright headless. Ctrl+C = quit and restore original gamma.
call %PY% app\main.py --headless %*
if errorlevel 1 (
  echo.
  echo   the app exited with an error - see the lines above.
  pause
)
exit /b 0
