# Start Jame Trader: loop + dashboard (venv) + Cloudflare tunnel.
# Dev machines (Surface): use this. It does not auto-restart.
# Always-on Dell: use supervisor.ps1 instead (restarts crashes; honors Pull from GitHub).
# Usage:  .\start.ps1
#         .\start.ps1 -NoTunnel
#         .\start.ps1 -TunnelOnly

param(
    [switch]$NoTunnel,
    [switch]$TunnelOnly
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Find-VenvPython {
    foreach ($name in @('venv', '.venv')) {
        $candidate = Join-Path $Root "$name\Scripts\python.exe"
        if (Test-Path $candidate) { return $candidate }
    }
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

$python = Find-VenvPython
if (-not $TunnelOnly) {
    if (-not $python) {
        Write-Error "No venv found. Create one first: python -m venv venv ; .\venv\Scripts\Activate.ps1 ; pip install -r requirements.txt"
    }
    Write-Host "Using $python"
}

$started = @()

if (-not $TunnelOnly) {
    Start-Process -FilePath $python -ArgumentList 'main.py', '--loop' -WorkingDirectory $Root -WindowStyle Normal
    $started += 'trader loop (main.py --loop)'
    Start-Sleep -Milliseconds 400

    Start-Process -FilePath $python -ArgumentList 'web_app.py' -WorkingDirectory $Root -WindowStyle Normal
    $started += 'dashboard (web_app.py)'
    Start-Sleep -Milliseconds 400
}

if (-not $NoTunnel) {
    $cloudflared = Find-Cloudflared
    if (-not $cloudflared) {
        Write-Warning 'cloudflared not found - skipped tunnel. Install it or add it to PATH.'
    } else {
        Start-Process -FilePath $cloudflared -ArgumentList 'tunnel', 'run', 'stock-trader' -WorkingDirectory $Root -WindowStyle Normal
        $started += ('tunnel: ' + $cloudflared + ' tunnel run stock-trader')
    }
}

if ($started.Count -eq 0) {
    Write-Host 'Nothing started.'
    exit 1
}

Write-Host 'Started:'
foreach ($item in $started) { Write-Host "  - $item" }
Write-Host 'Dashboard (local): http://127.0.0.1:8787/'
Write-Host 'Close each window (or Ctrl+C) to stop that process.'
