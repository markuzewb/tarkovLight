@echo off
rem ===========================================================================
rem  TarkovBright setup.  Read the header of _pyfind.bat for why this is written
rem  the way it is.  Nothing here is REQUIRED: the app itself runs on bare
rem  Python with zero third-party packages.  pip is only tried for OPTIONAL
rem  speed-ups (numpy = faster analysis, mss/Pillow = other capture backends).
rem
rem  Needs: Python 3.9+ (install with: winget install -e --id Python.Python.3.12)
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
echo.
echo   TarkovBright - setup
echo   ------------------------------------------------------------------
echo   Looking for a Python that actually runs ...
call "%~dp0_pyfind.bat"
if defined PY goto found

echo.
echo   [ERROR] no working Python 3.9+ found.
echo           Checked: py -3, python, python3 and the usual install folders.
echo.
echo           If you saw "Unable to create process using 'D:\python.exe ...'",
echo           that is exactly this: the py launcher is registered, but the
echo           interpreter it points to is gone. Reinstalling Python fixes it.
echo.
echo           Easiest fix, one command in a normal PowerShell window:
echo               winget install -e --id Python.Python.3.12
echo           or: https://www.python.org/downloads/windows/
echo           During setup TICK "Add python.exe to PATH".
echo.
echo           No Python at all? Then use the manual gamma slider instead:
echo               powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Level 60
echo               powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Restore
echo.
pause
exit /b 2

:found
echo   [ok] Python: %PY%
call %PY% -c "import sys; print('        ', sys.version.split()[0], sys.executable)"

echo.
echo   Optional speed-ups (numpy / mss / Pillow). Skipping them is fine.
call %PY% -m pip --version >nul 2>nul
if errorlevel 1 goto nopep
call %PY% -m pip install -r "%~dp0requirements.txt"
if not errorlevel 1 goto deps_ok
echo        global install failed, retrying with --user ...
call %PY% -m pip install --user -r "%~dp0requirements.txt"
if not errorlevel 1 goto deps_ok
echo        [skip] dependencies not installed - the app still works.
goto deps_done

:deps_ok
echo        [ok] optional dependencies installed.
goto deps_done

:nopep
echo        [skip] pip is not available - not needed.

:deps_done
echo.
echo   Self-test of the correction math and the capture layer ...
call %PY% app\main.py --selftest
if errorlevel 1 goto selffail

echo.
echo   Windows check - sets the gamma ramp for a second and puts it back ...
call %PY% app\main.py --check
echo.
echo   ------------------------------------------------------------------
echo   Start:   Start-TarkovBright.bat   - window with sliders
echo            Start-Headless.bat       - background only
echo   In game: F8 on/off   F7 restore   F9 boost 12s   F10 next profile
echo   Game must run in Windowed / Borderless, and Auto HDR must be OFF.
echo   More in README.md
echo.
pause
exit /b 0

:selffail
echo.
echo   [ERROR] self-test failed - please send the lines above.
pause
exit /b 1
