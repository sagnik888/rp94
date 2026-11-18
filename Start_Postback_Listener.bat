@echo off
:: Public listener for Upstox OAuth redirect and order postbacks.
::
:: Double-click this, or run it from anywhere — it pins its own directory and
:: interpreter.

title UT1 Upstox Listener ^& OAuth Redirect

cd /d "%~dp0"

set "PY=%CD%\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv interpreter not found at "%PY%".
    echo Recreate it with:  python -m venv venv
    pause
    exit /b 1
)

echo ========================================================
echo    UT1 - UPSTOX LISTENER ^& OAUTH REDIRECT (PORT 8500)
echo ========================================================
echo [INFO] Working Directory: %CD%
echo.
echo Handles:
echo   1. OAuth redirect callback: http://127.0.0.1:8500/?code=...
echo   2. Order postbacks/webhooks: /webhook, /api/upstox_postback
echo ========================================================
echo.

"%PY%" scripts\upstox_postback_listener.py

echo.
echo ========================================================
echo [WARNING] Upstox listener has stopped.
echo ========================================================
pause
