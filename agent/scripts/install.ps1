#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs or updates ggnet-agent as a Windows service.

.EXAMPLE
    .\install.ps1 -ServerUrl http://192.168.0.10:8088
    .\install.ps1 -ServerUrl http://192.168.0.10:8088 -DriveLetter G

    Run from the folder that contains ggnet-agent.exe (the release zip).
#>
param(
    [Parameter(Mandatory)] [string] $ServerUrl,
    [ValidatePattern('^[D-Z]$')] [string] $DriveLetter = 'D'
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'ggnet-agent'
$InstallDir = Join-Path $env:ProgramFiles 'ggnet-agent'
$Exe = Join-Path $InstallDir 'ggnet-agent.exe'
$Source = Join-Path $PSScriptRoot 'ggnet-agent.exe'

$uri = $null
if (-not [Uri]::TryCreate($ServerUrl, 'Absolute', [ref]$uri) -or $uri.Scheme -notin 'http', 'https') {
    throw "ServerUrl must be an http(s) URL, e.g. http://192.168.0.10:8088"
}
if (-not (Test-Path $Source)) { throw "ggnet-agent.exe not found next to this script" }

# Warn early: the game disk prefers this drive letter.
$used = Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue
if ($used) {
    Write-Warning ("Drive ${DriveLetter}: is already used ($($used.FileSystemLabel) $($used.DriveType)); " +
        "the game disk will get the next free letter. Use -DriveLetter to pick another one.")
}

# Fast Startup makes "Shut down" a hibernation, so Windows keeps its boot time
# and the server cannot tell a new boot from the old one. Cafe PCs boot cold.
$power = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
if ((Get-ItemProperty $power -ErrorAction SilentlyContinue).HiberbootEnabled -ne 0) {
    Set-ItemProperty $power -Name HiberbootEnabled -Value 0 -Type DWord
    Write-Host "Fast Startup turned off."
}

$service = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne 'Stopped') {
    Write-Host "Stopping $ServiceName..."
    Stop-Service $ServiceName
}

New-Item -ItemType Directory -Force $InstallDir | Out-Null
Copy-Item $Source $Exe -Force

$settings = [ordered]@{
    Agent = [ordered]@{
        ServerUrl = $ServerUrl.TrimEnd('/')
        DriveLetter = $DriveLetter
        HeartbeatSeconds = 30
        ManageInitiatorName = $true
    }
    Logging = @{ EventLog = @{ LogLevel = @{ Default = 'Information'; Microsoft = 'Warning'; 'System.Net.Http.HttpClient' = 'Warning' } } }
}
$settings | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $InstallDir 'appsettings.json') -Encoding UTF8

if (-not $service) {
    Write-Host "Creating service $ServiceName..."
    New-Service -Name $ServiceName -DisplayName 'ggNet agent' `
        -Description 'Connects the ggNet game disk over iSCSI and reports to the ggNet server.' `
        -BinaryPathName "`"$Exe`"" -StartupType Automatic | Out-Null
}
# Restart after a crash: 5 s, 5 s, then 30 s; reset the counter after a day.
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null

Start-Service $ServiceName
Write-Host "ggnet-agent is running. Logs: Event Viewer > Windows Logs > Application (source ggnet-agent)."
