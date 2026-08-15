# KeySwitcher — Windows uninstaller.
# Removes the installed app, Start Menu / Startup shortcuts, and the
# HKCU Run entry. Token slots in ~/.codex/accounts/ and Credential
# Manager items are preserved.

$ErrorActionPreference = "Stop"

$Dest = Join-Path $env:LOCALAPPDATA "KeySwitcher"
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\KeySwitcher.lnk"
$Startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\KeySwitcher.lnk"

Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*keyswitcher_app.py*" } |
    ForEach-Object {
        Write-Output "==> Stopping KeySwitcher (pid $($_.ProcessId))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

foreach ($link in @($StartMenu, $Startup)) {
    if (Test-Path $link) {
        Remove-Item -Force $link
        Write-Output "==> Removed $link"
    }
}

try {
    $reg = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "KeySwitcher" -ErrorAction Stop
    if ($reg) {
        Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "KeySwitcher"
        Write-Output "==> Removed HKCU Run entry"
    }
} catch {
}

if (Test-Path $Dest) {
    Remove-Item -Recurse -Force $Dest
    Write-Output "==> Removed $Dest"
}

Write-Output "Done. Codex/Antigravity tokens were left in place."
