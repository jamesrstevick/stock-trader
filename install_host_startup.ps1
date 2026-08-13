# Start supervisor.ps1 at Windows logon on the Dell.
# Prefers a Scheduled Task (needs Administrator). If that is Access denied,
# falls back to this user's Startup folder (no admin).
#
#   .\install_host_startup.ps1
#   .\install_host_startup.ps1 -NoTunnel
#   .\install_host_startup.ps1 -Uninstall

param(
    [switch]$NoTunnel,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$TaskName = 'JameTraderHost'
$Supervisor = Join-Path $Root 'supervisor.ps1'
$StartupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$StartupCmd = Join-Path $StartupDir 'JameTraderHost.cmd'

function Get-SupervisorArg {
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$Supervisor`""
    if ($NoTunnel) { $arg += ' -NoTunnel' }
    return $arg
}

function Install-StartupFolderLauncher {
    if (-not (Test-Path $StartupDir)) {
        New-Item -ItemType Directory -Path $StartupDir -Force | Out-Null
    }
    $extra = ''
    if ($NoTunnel) { $extra = ' -NoTunnel' }
    $lines = @(
        '@echo off',
        'cd /d "' + $Root + '"',
        'start "JameTraderHost" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $Supervisor + '"' + $extra
    )
    Set-Content -Path $StartupCmd -Value $lines -Encoding ASCII
    Write-Host "Installed Startup shortcut:"
    Write-Host "  $StartupCmd"
    Write-Host "It runs supervisor.ps1 when this Windows user logs on (no admin needed)."
}

function Remove-StartupFolderLauncher {
    if (Test-Path $StartupCmd) {
        Remove-Item $StartupCmd -Force
        Write-Host "Removed Startup shortcut."
    }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    Remove-StartupFolderLauncher
    exit 0
}

if (-not (Test-Path $Supervisor)) {
    throw "Missing $Supervisor"
}

$arg = Get-SupervisorArg
$taskOk = $false
try {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -DontStopOnIdleEnd `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    $taskOk = $true
} catch {
    $msg = $_.Exception.Message
    Write-Host "Scheduled task failed ($msg)"
    Write-Host "Falling back to the current user's Startup folder (no Administrator needed)."
    Install-StartupFolderLauncher
}

if ($taskOk) {
    Remove-StartupFolderLauncher
    Write-Host "Registered '$TaskName' to run supervisor.ps1 at logon."
    Write-Host "Keep this Windows user logged in, and set Sleep to Never."
    Write-Host "Start now:  Start-ScheduledTask -TaskName $TaskName"
    Write-Host "  or:       .\supervisor.ps1"
} else {
    Write-Host "Keep this Windows user logged in, and set Sleep to Never."
    Write-Host "Start now:  .\supervisor.ps1"
    Write-Host "To use a Scheduled Task instead, right-click PowerShell -> Run as administrator,"
    Write-Host "cd to this folder, and run .\install_host_startup.ps1 again."
}
