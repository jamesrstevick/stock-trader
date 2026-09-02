# Always-on host: keep trader loop, dashboard, and Cloudflare tunnel running.
# Restarts any child that exits. Honors data/host_command.json from the dashboard
# (Pull from GitHub / Restart processes).
#
# Usage:
#   .\supervisor.ps1
#   .\supervisor.ps1 -NoTunnel
#
# Dev machines should keep using start.ps1 (no auto-restart).

param(
    [switch]$NoTunnel
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
Set-Location $Root

# One supervisor per Windows session. A second launch (Startup .cmd while
# one is already open) exits instead of fighting over loop/dashboard/tunnel.
$createdMutex = $false
$script:SupervisorMutex = New-Object System.Threading.Mutex(
    $true,
    'Local\JameTraderSupervisor',
    [ref]$createdMutex
)
if (-not $createdMutex) {
    Write-Host 'Supervisor is already running in another window. Keep that one; close this.'
    Start-Sleep -Seconds 3
    exit 0
}

$DataDir = Join-Path $Root 'data'
$LogDir = Join-Path $Root 'logs'
$StatusPath = Join-Path $DataDir 'host_status.json'
$CommandPath = Join-Path $DataDir 'host_command.json'
$SupervisorLog = Join-Path $LogDir 'supervisor.log'

foreach ($dir in @($DataDir, $LogDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

function Write-HostLog {
    param([string]$Message)
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -Path $SupervisorLog -Value $line -Encoding UTF8
}

function Find-VenvPython {
    $found = @()
    foreach ($name in @('.venv', 'venv')) {
        $candidate = Join-Path $Root "$name\Scripts\python.exe"
        if (Test-Path $candidate) { $found += $candidate }
    }
    foreach ($candidate in $found) {
        try {
            $out = & $candidate -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null
            $n = 0
            if ([int]::TryParse(("$out").Trim(), [ref]$n) -and $n -ge 311) {
                return $candidate
            }
        } catch {}
    }
    if ($found.Count -gt 0) { return $found[0] }
    return $null
}

function Find-Cloudflared {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($path in @(
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "${env:ProgramFiles}\cloudflared\cloudflared.exe",
        "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
    )) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Test-Alive {
    param($Proc)
    if (-not $Proc) { return $false }
    try {
        $Proc.Refresh()
        return -not $Proc.HasExited
    } catch {
        return $false
    }
}

function Stop-Child {
    param($Proc)
    if (-not (Test-Alive $Proc)) { return }
    try { $Proc.Kill() } catch {}
    try { [void]$Proc.WaitForExit(8000) } catch {}
}

function Stop-MatchingCommandLine {
    param([string]$Pattern)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern } |
        ForEach-Object {
            Write-HostLog ("Stopping leftover pid {0}: {1}" -f $_.ProcessId, $Pattern)
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Stop-RepoPython {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match 'python' -and $_.CommandLine -and (
                $_.CommandLine -match 'main\.py' -or
                $_.CommandLine -match 'web_app\.py'
            )
        } |
        ForEach-Object {
            Write-HostLog ("Stopping leftover python pid {0}" -f $_.ProcessId)
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Start-HiddenProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$LogStem
    )
    # Do not use Start-Process: it opens a new console per child (restart storm).
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    if ($ArgumentList -and $ArgumentList.Count -gt 0) {
        $quoted = New-Object System.Collections.Generic.List[string]
        foreach ($a in $ArgumentList) {
            if ($a -match '\s') { [void]$quoted.Add('"' + $a + '"') }
            else { [void]$quoted.Add($a) }
        }
        $psi.Arguments = ($quoted -join ' ')
    }
    return [System.Diagnostics.Process]::Start($psi)
}

function Write-LogTail {
    param([string]$Name)
    $errLog = Join-Path $LogDir ($Name + '.err.log')
    if (-not (Test-Path $errLog)) { return }
    $lines = Get-Content -Path $errLog -Tail 12 -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($line) { Write-HostLog ("  {0}" -f $line) }
    }
}

$python = Find-VenvPython
if (-not $python) {
    Write-Error "No venv found. Create one: python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt"
    exit 1
}

$cloudflared = $null
$tunnelEnabled = -not $NoTunnel
if ($tunnelEnabled) {
    $cloudflared = Find-Cloudflared
    if (-not $cloudflared) {
        Write-HostLog 'cloudflared not found  - tunnel skipped. Install it or use -NoTunnel.'
        $tunnelEnabled = $false
    }
}

$script:children = @{
    loop = $null
    dashboard = $null
    tunnel = $null
}
$script:restarts = @{ loop = 0; dashboard = 0; tunnel = 0 }
$script:crashStreak = @{ loop = 0; dashboard = 0; tunnel = 0 }
$script:startedAt = @{
    loop = [datetime]::MinValue
    dashboard = [datetime]::MinValue
    tunnel = [datetime]::MinValue
}
$script:nextStart = @{
    loop = [datetime]::MinValue
    dashboard = [datetime]::MinValue
    tunnel = [datetime]::MinValue
}
$script:busy = $false
$script:lastCommand = $null
$script:skipTunnel = -not $tunnelEnabled
$script:reexecing = $false

function Get-GitSnapshot {
    $branch = $null
    $sha = $null
    try { $branch = (git -C $Root rev-parse --abbrev-ref HEAD 2>$null).Trim() } catch {}
    try { $sha = (git -C $Root rev-parse --short HEAD 2>$null).Trim() } catch {}
    return @{
        branch = $branch
        sha = $sha
        repo = Test-Path (Join-Path $Root '.git')
    }
}

function Write-StatusFile {
    $git = Get-GitSnapshot
    $childMap = @{}
    foreach ($name in @('loop', 'dashboard', 'tunnel')) {
        $p = $script:children[$name]
        $alive = Test-Alive $p
        $entry = @{
            running = [bool]$alive
            pid = if ($alive) { $p.Id } else { $null }
            restarts = [int]$script:restarts[$name]
        }
        if ($name -eq 'tunnel' -and $script:skipTunnel) {
            $entry.skipped = $true
            $entry.running = $false
        }
        $childMap[$name] = $entry
    }
    $obj = [ordered]@{
        ok = $true
        supervisor = $true
        pid = $PID
        updated_at = (Get-Date).ToString('s')
        busy = [bool]$script:busy
        git = $git
        children = $childMap
        last_command = $script:lastCommand
    }
    $json = $obj | ConvertTo-Json -Depth 8
    $tmp = "$StatusPath.tmp"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmp, $json, $utf8)
    Move-Item -LiteralPath $tmp -Destination $StatusPath -Force
}

function Start-NamedChild {
    param([string]$Name)
    if ($Name -eq 'tunnel' -and $script:skipTunnel) { return }
    if ((Get-Date) -lt $script:nextStart[$Name]) { return }
    if (Test-Alive $script:children[$Name]) { return }

    if ($Name -eq 'loop') {
        $script:children.loop = Start-HiddenProcess $python @('main.py', '--loop') 'loop'
    } elseif ($Name -eq 'dashboard') {
        $script:children.dashboard = Start-HiddenProcess $python @('web_app.py') 'dashboard'
    } elseif ($Name -eq 'tunnel') {
        $script:children.tunnel = Start-HiddenProcess $cloudflared @('tunnel', 'run', 'stock-trader') 'tunnel'
    }
    $script:startedAt[$Name] = Get-Date
    $p = $script:children[$Name]
    $pidVal = if ($p) { $p.Id } else { '?' }
    Write-HostLog ("Started {0} (pid {1})" -f $Name, $pidVal)
}

function Note-ChildExit {
    param([string]$Name)
    $p = $script:children[$Name]
    if (-not $p) { return }
    if (Test-Alive $p) { return }
    $code = $null
    try { $code = $p.ExitCode } catch {}
    $script:children[$Name] = $null
    $script:restarts[$Name] = [int]$script:restarts[$Name] + 1
    $ran = (Get-Date) - $script:startedAt[$Name]
    if ($ran.TotalSeconds -lt 15) {
        $script:crashStreak[$Name] = [int]$script:crashStreak[$Name] + 1
    } else {
        $script:crashStreak[$Name] = 0
    }
    $delay = [Math]::Min(60, [Math]::Pow(2, [Math]::Min(5, [int]$script:crashStreak[$Name])))
    $script:nextStart[$Name] = (Get-Date).AddSeconds($delay)
    Write-HostLog ("{0} exited (code {1})  - restart in {2}s" -f $Name, $code, $delay)
    Write-LogTail $Name
}

function Stop-AllChildren {
    foreach ($name in @('loop', 'dashboard', 'tunnel')) {
        Stop-Child $script:children[$name]
        $script:children[$name] = $null
    }
}

function Request-CatchUp {
    $path = Join-Path $DataDir 'catch_up_requested.json'
    $payload = @{
        requested_at = (Get-Date).ToString('s')
        reason = 'git_pull'
    } | ConvertTo-Json
    Set-Content -Path $path -Value $payload -Encoding UTF8
    Write-HostLog 'Requested deploy catch-up (loop will run it before normal jobs)'
}

function Invoke-GitPull {
    $branch = (git -C $Root rev-parse --abbrev-ref HEAD).Trim()
    if (-not $branch) { throw 'Not a git checkout (no branch)' }
    Write-HostLog ("git fetch origin (branch {0})" -f $branch)
    git -C $Root fetch origin
    if ($LASTEXITCODE -ne 0) { throw 'git fetch failed' }
    git -C $Root pull --ff-only origin $branch
    if ($LASTEXITCODE -ne 0) { throw 'git pull --ff-only failed (Dell has local commits, or branch diverged)' }
}

function Invoke-PipInstall {
    $req = Join-Path $Root 'requirements.txt'
    if (-not (Test-Path $req)) { return }
    Write-HostLog 'pip install -r requirements.txt'
    & $python -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

function Release-SupervisorMutex {
    try {
        if ($script:SupervisorMutex) {
            $script:SupervisorMutex.ReleaseMutex() | Out-Null
            $script:SupervisorMutex.Dispose()
            $script:SupervisorMutex = $null
        }
    } catch {}
}

function Start-ReplacementSupervisor {
    # Build one argument string *before* Start-Process. Windows PowerShell 5.1
    # treats `+` after -ArgumentList as another positional parameter.
    $quotedFile = '"' + $PSCommandPath + '"'
    $quotedRoot = '"' + $Root + '"'
    $psArgs = '-NoProfile -ExecutionPolicy Bypass -File ' + $quotedFile
    if ($NoTunnel) { $psArgs = $psArgs + ' -NoTunnel' }
    $cmdLine = 'start "JameTraderHost" /D ' + $quotedRoot + ' powershell.exe ' + $psArgs
    $argList = '/c ' + $cmdLine
    Start-Process -FilePath "$env:ComSpec" -WorkingDirectory $Root -ArgumentList $argList
}

function Restart-AllChildren {
    Write-HostLog 'Restarting children'
    Stop-AllChildren
    Start-Sleep -Seconds 1
    Stop-MatchingCommandLine 'main\.py["\s]+--loop'
    Stop-MatchingCommandLine 'web_app\.py'
    if (-not $script:skipTunnel) {
        Stop-MatchingCommandLine 'cloudflared.*tunnel run stock-trader'
    }
    Start-Sleep -Milliseconds 400
    $script:nextStart.loop = [datetime]::MinValue
    $script:nextStart.dashboard = [datetime]::MinValue
    $script:nextStart.tunnel = [datetime]::MinValue
    Start-NamedChild 'loop'
    Start-Sleep -Milliseconds 400
    Start-NamedChild 'dashboard'
    Start-Sleep -Milliseconds 400
    Start-NamedChild 'tunnel'
}

function Read-QueuedCommand {
    if (-not (Test-Path $CommandPath)) { return $null }
    try {
        $raw = Get-Content -Path $CommandPath -Raw -ErrorAction Stop
        Remove-Item -Path $CommandPath -Force -ErrorAction SilentlyContinue
        if (-not $raw) { return $null }
        return $raw | ConvertFrom-Json
    } catch {
        Remove-Item -Path $CommandPath -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Invoke-HostCommand {
    param($Cmd)
    $action = [string]$Cmd.action
    $script:busy = $true
    Write-StatusFile
    $ok = $true
    $message = ''
    $shaBefore = (Get-GitSnapshot).sha
    $supHashBefore = $null
    try {
        $supHashBefore = (Get-FileHash -Path $PSCommandPath -Algorithm SHA256).Hash
    } catch {}
    try {
        if ($action -eq 'pull') {
            Invoke-GitPull
            Invoke-PipInstall
            $shaAfter = (Get-GitSnapshot).sha
            if ($shaAfter -eq $shaBefore) {
                $message = 'Already up to date ({0})  - restarting processes' -f $shaAfter
            } else {
                $message = 'Updated {0} -> {1}  - restarting processes' -f $shaBefore, $shaAfter
            }
            Request-CatchUp
            Restart-AllChildren
            $supHashAfter = $null
            try { $supHashAfter = (Get-FileHash -Path $PSCommandPath -Algorithm SHA256).Hash } catch {}
            if ($supHashBefore -and $supHashAfter -and ($supHashBefore -ne $supHashAfter)) {
                Write-HostLog 'supervisor.ps1 changed  - re-exec after restarting children'
                $script:lastCommand = @{
                    id = [string]$Cmd.id
                    action = $action
                    ok = $true
                    message = $message + ' (supervisor script updated, re-exec)'
                    finished_at = (Get-Date).ToString('s')
                }
                $script:busy = $false
                Write-StatusFile
                try {
                    $script:reexecing = $true
                    Release-SupervisorMutex
                    Start-ReplacementSupervisor
                    exit 0
                } catch {
                    $script:reexecing = $false
                    Write-HostLog ("Re-exec failed: {0}  - keep this window; children already restarted" -f $_.Exception.Message)
                    $message = $message + ' (reopen supervisor.ps1 once to load the new script)'
                }
            }
        } elseif ($action -eq 'restart') {
            Restart-AllChildren
            $message = 'Restarted loop, dashboard, and tunnel'
        } else {
            $ok = $false
            $message = 'Unknown action: ' + $action
        }
    } catch {
        $ok = $false
        $message = $_.Exception.Message
        Write-HostLog ("Command {0} failed: {1}" -f $action, $message)
    }
    $script:lastCommand = @{
        id = [string]$Cmd.id
        action = $action
        ok = $ok
        message = $message
        finished_at = (Get-Date).ToString('s')
    }
    $script:busy = $false
    Write-HostLog $message
    Write-StatusFile
}

Write-HostLog ("Supervisor starting (python {0})" -f $python)
if (-not (Test-Path (Join-Path $Root 'config.py'))) {
    Write-HostLog 'Missing config.py. Copy it from the Surface into this folder, then restart supervisor.'
}
Stop-RepoPython
Stop-MatchingCommandLine 'main\.py["\s]+--loop'
Stop-MatchingCommandLine 'web_app\.py'
if ($tunnelEnabled) {
    Stop-MatchingCommandLine 'cloudflared.*tunnel run stock-trader'
}
Start-Sleep -Seconds 1

Restart-AllChildren
Write-Host 'Dashboard (local): http://127.0.0.1:8787/'
Write-Host 'Leave this window open. Ctrl+C stops the supervisor and children.'
Write-HostLog 'Watching children + host_command.json'

try {
    while ($true) {
        foreach ($name in @('loop', 'dashboard', 'tunnel')) {
            if ($name -eq 'tunnel' -and $script:skipTunnel) { continue }
            if (-not (Test-Alive $script:children[$name])) {
                if ($script:children[$name]) { Note-ChildExit $name }
                Start-NamedChild $name
            }
        }
        $queued = Read-QueuedCommand
        if ($queued -and $queued.action) {
            Write-HostLog ("Queued command: {0}" -f $queued.action)
            Invoke-HostCommand $queued
        }
        Write-StatusFile
        Start-Sleep -Seconds 2
    }
} finally {
    if ($script:reexecing) {
        Write-HostLog 'Supervisor handing off to replacement process'
        Release-SupervisorMutex
    } else {
        Write-HostLog 'Supervisor stopping  - killing children'
        Stop-AllChildren
        if (Test-Path $StatusPath) {
            Remove-Item $StatusPath -Force -ErrorAction SilentlyContinue
        }
        Release-SupervisorMutex
    }
}
