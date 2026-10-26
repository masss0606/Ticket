@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0ticket_gui.py"
) else if exist "%USERPROFILE%\.conda\envs\ticket\pythonw.exe" (
    start "" "%USERPROFILE%\.conda\envs\ticket\pythonw.exe" "%~dp0ticket_gui.py"
) else (
    start "" pythonw "%~dp0ticket_gui.py"
)
