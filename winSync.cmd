@echo off
rem winSync launcher: double-click to start winSync.
rem The first time (or with --setup) it sets up Python and winSync's environment first.
rem   winSync.cmd --setup        redo the setup (e.g. after moving the folder)
rem   winSync.cmd --uninstall    remove the shortcuts and the .venv folder
setlocal
rem Run from inside the zip, Windows copies only this file to a Temp folder: say how to extract it, then stop.
set "HERE=%~dp0"
for %%f in (setup.ps1 requirements.txt config.toml winSync\gui.py static\index.html) do if not exist "%HERE%%%f" goto notextracted
if not "%HERE:\Temp\=%"=="%HERE%" goto notextracted
cd /d "%~dp0"
if /i "%~1"=="--setup" goto setup
if /i "%~1"=="--uninstall" goto uninstall
rem Anything missing (e.g. pyserial, new in v1.3, on a v1.2 install) runs the setup, which adds it.
".venv\Scripts\python.exe" -c "import aiohttp, tkinter, win32com.client, serial" >nul 2>&1 || goto setup

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

:notextracted
rem WINSYNC_NO_MSGBOX (the tests) prints the message instead of showing it.
set "WINSYNC_MSG=winSync can't start from inside the zip file, or from a Temp folder.~~Right-click the winSync zip file, choose Extract All, then open the extracted folder and double-click winSync.cmd there."
powershell.exe -NoProfile -Command "$m = $env:WINSYNC_MSG.Replace('~', [char]10); if ($env:WINSYNC_NO_MSGBOX) { $m } else { Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($m, 'winSync', 'OK', 'Warning') }"
exit /b 1
