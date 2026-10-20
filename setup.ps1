# winSync setup for Windows 10/11. Run by winSync.cmd on first start (or `winSync.cmd --setup`);
# you do not normally run it yourself.
#
#   1. finds Python 3.11 or newer with tkinter (or offers to install Python 3.12 with winget,
#      for this user only, no administrator rights needed)
#   2. creates the .venv folder here and installs requirements.txt into it
#   3. adds winSync shortcuts to the Desktop and the Start menu (skip with -NoShortcuts)
#
# -Uninstall removes the shortcuts and the .venv folder (not the winSync folder or your settings).
param([switch]$Uninstall, [switch]$NoShortcuts)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$ShortcutName = 'winSync.lnk'
$Shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) $ShortcutName),
    (Join-Path ([Environment]::GetFolderPath('Programs')) $ShortcutName)
)
# Must succeed for a Python to be usable: version, tkinter for the window, venv/ensurepip for setup.
$Check = 'import sys, tkinter, venv, ensurepip; assert sys.version_info >= (3, 11); print(sys.executable)'

function Say($text, $colour = 'Gray') { Write-Host $text -ForegroundColor $colour }

function Test-Python($exe, [string[]]$pre = @()) {
    $ErrorActionPreference = 'Continue'      # a failing candidate is not an error here
    try {
        $out = & $exe @pre -c $Check 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    # The py launcher knows every python.org install.
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($v in '-3.13', '-3.12', '-3.11', '-3') {
            $p = Test-Python 'py.exe' @($v)
            if ($p) { return $p }
        }
    }
    # Usual per-user and all-users install folders (also right after a winget install, before PATH
    # is refreshed in this window).
    $dirs = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($d in $dirs) {
        if (-not $d -or -not (Test-Path $d)) { continue }
        $found = Get-ChildItem $d -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending
        foreach ($f in $found) {
            $exe = Join-Path $f.FullName 'python.exe'
            if (Test-Path $exe) { $p = Test-Python $exe; if ($p) { return $p } }
        }
    }
    # Anything else on PATH, except the WindowsApps stubs (running one opens the Microsoft Store).
    foreach ($c in (Get-Command python.exe, python3.exe -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -like '*\WindowsApps\*') { continue }
        $p = Test-Python $c.Source
        if ($p) { return $p }
    }
    return $null
}

function Install-Python {
    Say ''
    Say 'winSync needs Python 3.11 or newer (with tkinter), and none was found.' 'Yellow'
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        Say 'Install Python from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),'
        Say 'then double-click winSync.cmd again.'
        Start-Process 'https://www.python.org/downloads/windows/'
        return $false
    }
    $answer = Read-Host 'Install Python 3.12 now with winget, for this user only (no admin needed)? [Y/N]'
    if ($answer -notmatch '^(y|yes)$') {
        Say 'Not installed. Install Python 3.11 or newer yourself, then double-click winSync.cmd again.'
        return $false
    }
    Say 'Installing Python 3.12 (this can take a minute or two)...' 'Cyan'
    & winget.exe install --id Python.Python.3.12 --exact --scope user --silent `
        --accept-package-agreements --accept-source-agreements
    # winget's exit code is not reliable when the package is already installed; just look again.
    return $true
}

function Remove-Shortcuts {
    foreach ($s in $Shortcuts) {
        if (Test-Path $s) { Remove-Item $s -Force; Say "Removed $s" }
    }
}

function New-Shortcuts {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($s in $Shortcuts) {
        $lnk = $shell.CreateShortcut($s)
        $lnk.TargetPath = Join-Path $Venv 'Scripts\pythonw.exe'    # pythonw: no console window
        $lnk.Arguments = '-m winSync.gui'
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = (Join-Path $Root 'assets\winsync.ico') + ',0'
        $lnk.Description = 'Keep a KiwiSDR or WebSDR tuned to Thetis'
        $lnk.Save()
        Say "Shortcut: $s"
    }
}

if ($Uninstall) {
    Remove-Shortcuts
    if (Test-Path $Venv) { Remove-Item $Venv -Recurse -Force; Say "Removed $Venv" }
    Say 'winSync is uninstalled. You can now delete the winSync folder if you no longer want it.' 'Green'
    exit 0
}

Say 'Setting up winSync in' 'Cyan'
Say "  $Root"

$python = Find-Python
if (-not $python) {
    if (-not (Install-Python)) { exit 1 }
    $python = Find-Python
    if (-not $python) {
        Say 'Python still was not found. Close this window, then double-click winSync.cmd again' 'Red'
        Say '(a fresh window sees the new installation).' 'Red'
        exit 1
    }
}
Say "Using Python: $python"

# A .venv that no longer works (e.g. its Python was uninstalled) is rebuilt from scratch.
if ((Test-Path $Venv) -and -not (Test-Python $VenvPy)) {
    Say 'The existing .venv does not work any more; recreating it.' 'Yellow'
    Remove-Item $Venv -Recurse -Force
}
if (-not (Test-Path $VenvPy)) {
    Say 'Creating the Python environment (.venv)...' 'Cyan'
    & $python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Say 'Could not create .venv.' 'Red'; exit 1 }
}

Say 'Installing winSync requirements (aiohttp)...' 'Cyan'
& $VenvPy -m pip install --disable-pip-version-check --quiet -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Say 'Installing the requirements failed (is this PC online?).' 'Red'
    exit 1
}
& $VenvPy -c 'import aiohttp, tkinter'
if ($LASTEXITCODE -ne 0) { Say 'The new environment is not usable.' 'Red'; exit 1 }

if (-not $NoShortcuts) { New-Shortcuts }

Say ''
Say 'winSync is set up. Opening winSync...' 'Green'
exit 0
