@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -X utf8 capture_payment.py
) else if exist "%USERPROFILE%\.conda\envs\ticket\python.exe" (
    "%USERPROFILE%\.conda\envs\ticket\python.exe" -X utf8 capture_payment.py
) else (
    python -X utf8 capture_payment.py
)
pause
