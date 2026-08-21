# KeySwitcher — rebuild/reinstall from the current source tree.
$ErrorActionPreference = "Stop"
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "install.ps1")
