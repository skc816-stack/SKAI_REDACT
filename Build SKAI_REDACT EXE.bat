@echo off
REM ============================================================
REM  Build a standalone, branded Windows program (SKAI_REDACT.exe)
REM  Double-click ONCE. The finished program appears in the
REM  "dist" folder as SKAI_REDACT.exe - with the SKAI logo as its
REM  icon. No Python needed to run it.
REM ============================================================
cd /d "%~dp0"
echo Installing the builder (one time)...
python -m pip install pyinstaller
echo.
echo Building SKAI_REDACT.exe ... this can take a few minutes.
pyinstaller --onefile --windowed --name "SKAI_REDACT" --icon "skai_icon.ico" ^
  --add-data "skai_logo.png;." --add-data "skai_logo_128.png;." skai_redact.py
echo.
echo ============================================================
echo  DONE. Open the "dist" folder and double-click
echo  "SKAI_REDACT.exe". Copy that one file anywhere you like.
echo ============================================================
pause
