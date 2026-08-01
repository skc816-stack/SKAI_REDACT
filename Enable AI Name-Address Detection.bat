@echo off
REM ============================================================
REM  OPTIONAL: enable AI detection of names & addresses that are
REM  NOT next to a label. Double-click ONCE. Downloads a model.
REM ============================================================
cd /d "%~dp0"
echo Installing AI language model (one time, ~50 MB)...
python -m pip install spacy
python -m spacy download en_core_web_sm
echo.
echo Done. Re-open SKAI_REDACT to use the AI button.
pause
