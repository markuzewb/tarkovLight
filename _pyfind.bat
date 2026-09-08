@echo off
rem ===========================================================================
rem  Shared helper: find a WORKING Python.
rem
rem  Why this exists: `where py` / `where python` only prove that the name
rem  exists. On this user's machine the "py" launcher was registered but pointed
rem  at D:\python.exe, which was gone -> every pip call died with
rem      "Unable to create process using 'D:\python.exe -m pip install ...'"
rem  while `where py` happily returned 0.
rem
rem  So here every candidate is actually EXECUTED and must report Python 3.9+.
rem  Result: variable PY is set to the command to use (e.g.  py -3  or a quoted
rem  full path), or left empty when nothing works.
rem ===========================================================================
set "PY="
call :try py -3
call :try py -3.13
call :try py -3.12
call :try py -3.11
call :try py -3.10
call :try py -3.9
call :try python
call :try python3
call :try python.exe
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
call :try "%ProgramFiles%\Python313\python.exe"
call :try "%ProgramFiles%\Python312\python.exe"
call :try "C:\Python313\python.exe"
call :try "C:\Python312\python.exe"
exit /b 0

:try
rem Runs: <candidate> -c "<version check>". Anything that fails to start, or
rem starts and is older than 3.9, is rejected. %* keeps a quoted full path quoted.
if defined PY exit /b 0
call %* -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 4)" >nul 2>nul
if errorlevel 4 exit /b 0
if errorlevel 1 exit /b 0
set "PY=%*"
exit /b 0
