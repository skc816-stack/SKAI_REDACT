@echo off
REM ============================================================
REM  SKAI_REDACT - one-click launcher
REM  Double-click this file to open the tool. No typing needed.
REM ============================================================
cd /d "%~dp0"
python skai_redact.py
if errorlevel 1 (
    echo.
    echo The app did not start. Make sure skai_redact.py is in this same folder.
    pause
)
