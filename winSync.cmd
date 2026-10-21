@echo off
rem winSync launcher: double-click to start winSync.
rem The first time (or with --setup) it sets up Python and winSync's environment first.
rem   winSync.cmd --setup        redo the setup (e.g. after moving the folder)
rem   winSync.cmd --uninstall    remove the shortcuts and the .venv folder
setlocal
cd /d "%~dp0"
if /i "%~1"=="--setup" goto setup
if /i "%~1"=="--uninstall" goto uninstall
".venv\Scripts\python.exe" -c "import aiohttp, tkinter, win32com.client" >nul 2>&1 || goto setup

:run
start "" ".venv\Scripts\pythonw.exe" -m winSync.gui
exit /b 0

:setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
    echo.
    echo winSync setup did not finish.
    pause
    exit /b 1
)
goto run

:uninstall
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Uninstall
pause
exit /b 0
