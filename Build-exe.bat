@echo off
rem ===========================================================================
rem  Optional: builds a single dist\TarkovBright.exe so you do not need Python
rem  on that PC.  This is the ONE script that really needs pip (PyInstaller).
rem  If pip is broken, just use Start-TarkovBright.bat - it needs no pip.
rem
rem  Note: the .exe cannot self-update (its code lives inside the binary).
rem  Keep the .py version if you want the "Update" button, or rebuild here.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_pyfind.bat"
if defined PY goto havepy
echo.
echo   [ERROR] no working Python 3.9+ found, so the exe cannot be built.
echo           You do not need the exe: Start-TarkovBright.bat works without pip.
echo           To get Python:  winget install -e --id Python.Python.3.12
echo.
pause
exit /b 2

:havepy
echo   Python: %PY%
call %PY% -m pip --version >nul 2>nul
if errorlevel 1 goto nopep
call %PY% -m pip install pyinstaller
if not errorlevel 1 goto build
echo   [ERROR] could not install PyInstaller - you still can use the .py version.
pause
exit /b 1

:nopep
echo   [ERROR] pip is not available for this Python, cannot build the exe.
echo           Use Start-TarkovBright.bat instead - it needs no pip at all.
pause
exit /b 1

:build
echo   building ...
call %PY% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name TarkovBright --paths app app\main.py
if errorlevel 1 (
  echo.
  echo   build failed - the .py version still works: Start-TarkovBright.bat
  pause
  exit /b 1
)
echo.
echo   selftest of the built exe (math only; no monitor needed) ...
call dist\TarkovBright.exe --selftest
echo.
echo   done:  dist\TarkovBright.exe   - copy it anywhere, no Python needed.
start "" explorer "%~dp0dist"
exit /b 0
