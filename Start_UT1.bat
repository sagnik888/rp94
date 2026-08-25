@echo off
setlocal EnableDelayedExpansion
title UT1 Index Trading System - Advanced Launcher
color 0B

:: Console log lines carry emoji. Without UTF-8 they arrive as mojibake or as
:: backslash escapes.
chcp 65001 >nul

echo ========================================================
echo        UT1 INDEX TRADING SYSTEM - STARTUP SCRIPT
echo ========================================================
echo.

:: 1. Force script to run in its own directory
cd /d "%~dp0"
echo [INFO] Working Directory: %CD%

:: 2. Check for Python installation
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH.
    echo Please install Python 3.10+ from python.org and check "Add Python to PATH".
    pause
    exit /b
)

:: 3. Setup Virtual Environment if missing
if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment 'venv' not found. Creating one now...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b
    )
    echo [INFO] Virtual environment created successfully.
)

:: 4. Activate Virtual Environment
echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b
)

:: Pin the interpreter explicitly. `activate.bat` only prepends to PATH, and when that
:: does not take effect a global python silently answers instead. That is how the
:: system ran against global site-packages: every older dependency imported fine, so
:: nothing looked wrong until kiteconnect - installed only in venv - was missing.
set "PY=%CD%\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv interpreter not found at "%PY%".
    echo Recreate it with: python -m venv venv
    pause
    exit /b
)

"%PY%" scripts\check_python_runtime.py
if %errorlevel% neq 0 (
    echo [ERROR] Unsupported Python runtime. Recreate venv with Python 3.13.x.
    pause
    exit /b
)

:: 5. Ensure dependencies are installed (Quick check)
echo [INFO] Verifying dependencies...
"%PY%" -c "import uvicorn, loguru, kiteconnect" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Missing core dependencies. Installing from requirements.txt...
    "%PY%" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies. Check your internet connection.
        pause
        exit /b
    )
)

:: 6. Pre-flight: ensure the dashboard port is free (stale instance check)
echo [INFO] Checking port 8500 for a stale instance...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\preflight_port.ps1" -Port 8500
if %errorlevel% neq 0 (
    echo [INFO] Launch aborted ^(port 8500 still in use or left running by choice^).
    pause
    exit /b
)

:: 7. Launch the Server
echo ========================================================
echo [INFO] Starting Backend Server (main.py)...
echo [INFO] Dashboard will be available at http://127.0.0.1:8500/
echo ========================================================

:: Open the browser only after the API has live dashboard state, not just an open port.
start "" /B powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\open_dashboard_when_ready.ps1" -Url "http://127.0.0.1:8500/" -Port 8500

:: Start the python process
:: stderr is TEED to a file, not just the console. On 2026-08-24 the process died at
:: 10:59:58 mid-scan-cycle with nothing in logs/ut1.log -- no error, no traceback, no
:: shutdown line -- because Windows killed it for memory (System log, event 26:
:: "Out of Virtual Memory", with three python.exe holding 2.41 / 2.01 / 1.54 GB). A
:: MemoryError traceback or a Windows fatal message goes to STDERR, which was being
:: written to a console window that is gone by the time anyone looks. Keep it.
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set "_LDT=%%I"
set "_STAMP=%_LDT:~0,8%_%_LDT:~8,6%"
if not exist "logs" mkdir "logs"
echo [INFO] stderr -> logs\startup_stderr_%_STAMP%.log
"%PY%" main.py 2> "logs\startup_stderr_%_STAMP%.log"

:: 8. Error Handling
echo.
echo ========================================================
echo [WARNING] The server process has terminated or crashed.
echo [WARNING] Exit code: %ERRORLEVEL%
echo [WARNING] Check logs\startup_stderr_%_STAMP%.log for anything the
echo [WARNING] logger never got to write (MemoryError, fatal, OOM kill).
echo ========================================================
pause
