@echo off
:: Public listener for Zerodha order postbacks.
::
:: Double-click this, or run it from anywhere — it pins its own directory and
:: interpreter. Running "venv/Scripts/python.exe scripts/..." from the wrong folder
:: is a recurring trap: the paths are relative, so it fails with a confusing
:: "No such file or directory" that names a path you never typed.

title UT1 Kite Postback Listener

:: %~dp0 is this script's folder, so the working directory is correct no matter
:: where it was launched from.
cd /d "%~dp0"

set "PY=%CD%\venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv interpreter not found at "%PY%".
    echo Recreate it with:  python -m venv venv
    pause
    exit /b 1
)

echo ========================================================
echo    UT1 - ZERODHA POSTBACK LISTENER
echo ========================================================
echo [INFO] Working Directory: %CD%
echo.
echo This exposes ONE path and nothing else: /api/kite_postback
echo Your trading dashboard on port 8500 is NOT exposed by this.
echo.
echo To publish it, run in a SECOND terminal:
echo     cloudflared tunnel --url http://127.0.0.1:8555
echo Then register  https://^<tunnel-host^>/api/kite_postback
echo at developers.kite.trade
echo ========================================================
echo.

"%PY%" scripts\kite_postback_listener.py

echo.
echo ========================================================
echo [WARNING] The postback listener has stopped.
echo Postbacks are optional - the system still reconciles
echo through order_history() and trades() without them.
echo ========================================================
pause
