#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Stops and removes the ggnet-agent service and disconnects the game disk.
    (Stopping the service alone keeps the disk connected, so an update does
    not pull it from running games.) Fast Startup stays off.
#>
$ErrorActionPreference = 'Stop'
$ServiceName = 'ggnet-agent'
$InstallDir = Join-Path $env:ProgramFiles 'ggnet-agent'
$Settings = Join-Path $InstallDir 'appsettings.json'

# The game disk's portal is the ggNet server named in the settings.
$server = $null
if (Test-Path $Settings) {
    $server = ([Uri](Get-Content $Settings -Raw | ConvertFrom-Json).Agent.ServerUrl).Host
}

if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service $ServiceName -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Write-Host "Service $ServiceName removed."
}
if ($server) {
    Get-IscsiSession -ErrorAction SilentlyContinue |
        Where-Object { (Get-IscsiConnection -IscsiSession $_).TargetAddress -eq $server } |
        ForEach-Object {
            Disconnect-IscsiTarget -NodeAddress $_.TargetNodeAddress -Confirm:$false
            Write-Host "Disconnected $($_.TargetNodeAddress)."
        }
}
if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir."
}
