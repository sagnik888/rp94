@echo off
setlocal EnableExtensions EnableDelayedExpansion
title UT1 Index Trading System
color 0B

set "APP_EXIT=0"
set "APP_DIR=%~dp0"
set "DASHBOARD_HOST=127.0.0.1"
set "DASHBOARD_PORT=8500"
set "DASHBOARD_URL=http://%DASHBOARD_HOST%:%DASHBOARD_PORT%/"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

echo ========================================================
echo        UT1 INDEX TRADING SYSTEM - STARTUP
echo ========================================================
echo.

cd /d "%APP_DIR%"
echo [INFO] Working Directory: %CD%

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
    set "APP_EXIT=1"
    goto done
)

if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment not found. Creating venv...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        set "APP_EXIT=1"
        goto done
    )
)

echo [INFO] Activating virtual environment...
call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    set "APP_EXIT=1"
    goto done
)

:: Pin the interpreter explicitly — `activate.bat` only prepends to PATH, and when
:: that fails to take effect a global python answers instead. Every older dependency
:: still imports from global site-packages, so nothing looks wrong until a newer one
:: (kiteconnect) is missing at runtime.
set "PY=%CD%\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv interpreter not found at "%PY%". Recreate: python -m venv venv
    set "APP_EXIT=1"
    goto done
)

echo [INFO] Python:
"%PY%" --version
"%PY%" scripts\check_python_runtime.py
if errorlevel 1 (
    echo [ERROR] Unsupported Python runtime. Recreate venv with Python 3.13.x.
    set "APP_EXIT=1"
    goto done
)

echo [INFO] Checking core dependencies...
"%PY%" -c "import uvicorn, fastapi, loguru, pandas, numpy, yfinance, pyarrow; import fyers_apiv3; import kiteconnect" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Missing dependencies found. Installing from requirements.txt...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check the pip error above.
        set "APP_EXIT=1"
        goto done
    )
)

if not exist "logs" mkdir logs

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %DASHBOARD_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } exit 1" >nul 2>&1
if not errorlevel 1 (
    echo [WARNING] Port %DASHBOARD_PORT% is already in use. UT1 may already be running.
    echo [INFO] Opening existing dashboard at %DASHBOARD_URL%
    if /I not "%UTBOT_SKIP_BROWSER%"=="1" start "" /B powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%scripts\open_dashboard_when_ready.ps1" -Url "%DASHBOARD_URL%" -Port %DASHBOARD_PORT%
    goto done
)

echo ========================================================
echo [INFO] Starting server on %DASHBOARD_URL%
echo ========================================================
echo.

:: Open the browser only after the API has live dashboard state, not just an open port.
if /I not "%UTBOT_SKIP_BROWSER%"=="1" start "" /B powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%scripts\open_dashboard_when_ready.ps1" -Url "%DASHBOARD_URL%" -Port %DASHBOARD_PORT%
"%PY%" main.py
set "APP_EXIT=%ERRORLEVEL%"

echo.
echo ========================================================
echo [WARNING] The server process has stopped.
if not "%APP_EXIT%"=="0" echo [ERROR] main.py exited with code %APP_EXIT%.
echo ========================================================

:done
if /I not "%UTBOT_NO_PAUSE%"=="1" pause
exit /b %APP_EXIT%
