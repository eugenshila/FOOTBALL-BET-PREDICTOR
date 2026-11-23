@echo off
rem Double-click this file to back up VERIFIX right now.
rem It saves a copy on this computer (backups folder) and uploads it to
rem Google Drive / your cloud bucket if that is configured in .env.
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   VERIFIX backup - database + part photos
echo   --------------------------------------
echo.

"%PY%" scripts\backup_to_cloud.py
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" (
  echo   Done. The lines above show where the backup went.
) else (
  echo   Something went wrong - see the message above.
  echo   Nothing on this computer was deleted or changed.
)
echo.
echo   Press any key to close this window.
pause >nul
