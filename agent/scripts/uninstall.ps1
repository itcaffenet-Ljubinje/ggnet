#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Stops and removes the ggnet-agent service. Stopping it disconnects the game disk.
#>
$ErrorActionPreference = 'Stop'
$ServiceName = 'ggnet-agent'
$InstallDir = Join-Path $env:ProgramFiles 'ggnet-agent'

if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service $ServiceName -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Write-Host "Service $ServiceName removed."
}
if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir."
}
