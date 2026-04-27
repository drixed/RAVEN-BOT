@echo off
setlocal enabledelayedexpansion
cd /d "F:\RAVEN BOT\release_test\v0.1.0"

echo [UPDATER] Waiting for app to exit...
timeout /t 2 /nobreak >nul

echo [UPDATER] Copying files...
robocopy "C:\Users\drixxxed\AppData\Local\Temp\RAVEN_BOT_update\extracted_20260427_183550" "F:\RAVEN BOT\release_test\v0.1.0" /E /NFL /NDL /NJH /NJS /NP /R:3 /W:1
set RC=%ERRORLEVEL%

echo [UPDATER] Robocopy exit code: %RC%

echo [UPDATER] Starting app...
start "" "F:\RAVEN BOT\release_test\v0.1.0\RAVEN_BOT.exe"

echo [UPDATER] Done.
exit /b 0
