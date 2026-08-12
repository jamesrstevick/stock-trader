# Register a logon Scheduled Task so supervisor.ps1 starts on the Dell.
# Run once from an elevated or same-user PowerShell in the repo folder:
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

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

if (-not (Test-Path $Supervisor)) {
    throw "Missing $Supervisor"
}

$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$Supervisor`""
if ($NoTunnel) { $arg += ' -NoTunnel' }

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
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered '$TaskName' to run supervisor.ps1 at logon."
Write-Host "Keep this Windows user logged in, and set Sleep to Never."
Write-Host "Start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  or:       .\supervisor.ps1"
