# KeySwitcher — Windows installer.
# Copies the Python engine + tray app to %LOCALAPPDATA%\KeySwitcher,
# imports the current Codex session as slot 1 if needed, creates Start Menu
# / Startup shortcuts, and launches the tray icon.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineSrc = Join-Path $ScriptDir "engine"
$AppSrc = Join-Path $ScriptDir "app\windows\keyswitcher_app.py"
$Dest = Join-Path $env:LOCALAPPDATA "KeySwitcher"
$Python = Get-Command python -ErrorAction SilentlyContinue

if (-not $Python) {
    Write-Error "Python 3.9+ is required and was not found on PATH."
}

$verOut = & $Python.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
$parts = $verOut.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Write-Error "Python 3.9+ is required (found $verOut)."
}

if (-not (Test-Path (Join-Path $EngineSrc "keyswitcher.py"))) {
    Write-Error "engine\keyswitcher.py not found next to install.ps1"
}
if (-not (Test-Path $AppSrc)) {
    Write-Error "app\windows\keyswitcher_app.py not found"
}

Write-Output "==> Installing KeySwitcher to $Dest"
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "engine") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "app\windows") | Out-Null
Copy-Item -Force (Join-Path $EngineSrc "*.py") (Join-Path $Dest "engine")
Copy-Item -Force $AppSrc (Join-Path $Dest "app\windows\keyswitcher_app.py")
Copy-Item -Force (Join-Path $ScriptDir "README.md") (Join-Path $Dest "README.md") -ErrorAction SilentlyContinue
Copy-Item -Force (Join-Path $ScriptDir "LICENSE") (Join-Path $Dest "LICENSE") -ErrorAction SilentlyContinue

Write-Output "==> Importing current Codex session as slot 1 if needed"
& $Python.Source (Join-Path $Dest "engine\rotator.py") setup

$Pythonw = Join-Path (Split-Path $Python.Source) "pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $Pythonw = $Python.Source
}

$Launch = Join-Path $Dest "app\windows\keyswitcher_app.py"
$Wsh = New-Object -ComObject WScript.Shell

$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
$Shortcut = $Wsh.CreateShortcut((Join-Path $StartMenu "KeySwitcher.lnk"))
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = '"' + $Launch + '"'
$Shortcut.WorkingDirectory = $Dest
$Shortcut.WindowStyle = 7
$Shortcut.Description = "KeySwitcher tray"
$Shortcut.Save()

$Startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
New-Item -ItemType Directory -Force -Path $Startup | Out-Null
$StartupShortcut = $Wsh.CreateShortcut((Join-Path $Startup "KeySwitcher.lnk"))
$StartupShortcut.TargetPath = $Pythonw
$StartupShortcut.Arguments = '"' + $Launch + '"'
$StartupShortcut.WorkingDirectory = $Dest
$StartupShortcut.WindowStyle = 7
$StartupShortcut.Description = "KeySwitcher tray"
$StartupShortcut.Save()

Write-Output "==> Launching KeySwitcher"
Start-Process -FilePath $Pythonw -ArgumentList "`"$Launch`"" -WorkingDirectory $Dest

Write-Output ""
Write-Output "Done. KeySwitcher is installed and should appear in the system tray."
Write-Output "Start Menu: $StartMenu\KeySwitcher.lnk"
Write-Output "Launch at login: $Startup\KeySwitcher.lnk"
Write-Output ""
Write-Output "CLI:"
Write-Output "  python `"$Dest\engine\keyswitcher.py`" status"
Write-Output "  python `"$Dest\engine\keyswitcher.py`" switch 1 --no-restart"
