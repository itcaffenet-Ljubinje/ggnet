#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs or updates ggnet-agent as a Windows service.

.EXAMPLE
    .\install.ps1 -ServerUrl http://192.168.0.10:8088
    .\install.ps1 -ServerUrl http://192.168.0.10:8088 -DriveLetter G
    .\install.ps1 -Update

    Run from the folder that contains ggnet-agent.exe (the release zip).
    -Update (what the agent itself runs when the server has a newer one) keeps
    the installed appsettings.json; a -ServerUrl or -DriveLetter given with it
    still replaces that one setting. Either way the service is started again
    at the end, also when replacing the program failed.
#>
param(
    [string] $ServerUrl,
    [ValidatePattern('^[D-Z]$')] [string] $DriveLetter = 'D',
    [switch] $Update
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'ggnet-agent'
$InstallDir = Join-Path $env:ProgramFiles 'ggnet-agent'
$Exe = Join-Path $InstallDir 'ggnet-agent.exe'
$Source = Join-Path $PSScriptRoot 'ggnet-agent.exe'

$SettingsFile = Join-Path $InstallDir 'appsettings.json'

# The installed settings (an update keeps them), then what this run was given.
$agent = [ordered]@{ ServerUrl = ''; DriveLetter = 'D'; HeartbeatSeconds = 30; ManageInitiatorName = $true; HideLocalDisks = $true }
if ($Update -and (Test-Path $SettingsFile)) {
    $installed = (Get-Content $SettingsFile -Raw | ConvertFrom-Json).Agent
    if ($installed) {
        foreach ($p in $installed.PSObject.Properties) { $agent[$p.Name] = $p.Value }
    }
}
if ($PSBoundParameters.ContainsKey('ServerUrl') -or -not $Update) { $agent.ServerUrl = $ServerUrl }
if ($PSBoundParameters.ContainsKey('DriveLetter') -or -not $Update) { $agent.DriveLetter = $DriveLetter }
$ServerUrl = [string]$agent.ServerUrl
$DriveLetter = [string]$agent.DriveLetter

$uri = $null
if (-not [Uri]::TryCreate($ServerUrl, 'Absolute', [ref]$uri) -or $uri.Scheme -notin 'http', 'https') {
    throw "ServerUrl must be an http(s) URL, e.g. http://192.168.0.10:8088"
}
if (-not (Test-Path $Source)) { throw "ggnet-agent.exe not found next to this script" }

# Warn early: the game disk prefers this drive letter (an update keeps its disk).
$used = if ($Update) { $null } else { Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue }
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
if ($Update) {
    Write-Host "Updating ggnet-agent $(if ($service) { (Get-Item $Exe -ErrorAction SilentlyContinue).VersionInfo.ProductVersion })..."
}
if ($service -and $service.Status -ne 'Stopped') {
    Write-Host "Stopping $ServiceName..."
    Stop-Service $ServiceName
}

try {
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    # The stopped service's process can hold the file for a moment.
    for ($try = 1; ; $try++) {
        try { Copy-Item $Source $Exe -Force; break }
        catch { if ($try -ge 10) { throw }; Start-Sleep -Seconds 1 }
    }

    $agent.ServerUrl = $ServerUrl.TrimEnd('/')
    $settings = [ordered]@{
        Agent = $agent
        Logging = @{ EventLog = @{ LogLevel = @{ Default = 'Information'; Microsoft = 'Warning'; 'System.Net.Http.HttpClient' = 'Warning' } } }
    }
    $settings | ConvertTo-Json -Depth 5 | Set-Content $SettingsFile -Encoding UTF8
}
catch {
    # Never leave the PC without its agent: start whatever is installed.
    if ($service) {
        Write-Warning "Installing failed: $($_.Exception.Message); starting the installed agent again."
        Start-Service $ServiceName -ErrorAction SilentlyContinue
    }
    throw
}

if (-not $service) {
    Write-Host "Creating service $ServiceName..."
    New-Service -Name $ServiceName -DisplayName 'ggNet agent' `
        -Description 'Connects the ggNet game disk over iSCSI and reports to the ggNet server.' `
        -BinaryPathName "`"$Exe`"" -StartupType Automatic | Out-Null
}
# Restart after a crash: 5 s, 5 s, then 30 s; reset the counter after a day.
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null

Start-Service $ServiceName
if ($Update) { Write-Host "Updated to $((Get-Item $Exe).VersionInfo.ProductVersion)." }
Write-Host "ggnet-agent is running. Logs: Event Viewer > Windows Logs > Application (source ggnet-agent)."
