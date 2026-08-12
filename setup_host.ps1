# One-shot Dell setup: Python 3.11+, venv, deps, config.py, then supervisor.
# Double-click setup_host.bat (no need to cd or set execution policy).
#
# Usage:
#   .\setup_host.bat
#   .\setup_host.ps1
#   .\setup_host.ps1 -NoTunnel
#   .\setup_host.ps1 -InstallStartup

param(
    [switch]$NoTunnel,
    [switch]$InstallStartup
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-PyVer {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        $out = & $Exe @($PrefixArgs + @('-c', 'import sys; print("%d.%d" % (sys.version_info[0], sys.version_info[1]))')) 2>$null
        if ($out -match '^(\d+)\.(\d+)$') {
            return [int]$Matches[1] * 100 + [int]$Matches[2]
        }
    } catch {}
    return 0
}

function Refresh-Path {
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Find-Python311 {
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.13') },
        @{ Exe = 'py'; Args = @('-3.11') },
        @{ Exe = 'python'; Args = @() }
    )
    $paths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) { $candidates += @{ Exe = $p; Args = @() } }
    }
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd -and -not (Test-Path $c.Exe)) { continue }
        $ver = Get-PyVer -Exe $c.Exe -PrefixArgs $c.Args
        if ($ver -ge 311) {
            return $c
        }
    }
    return $null
}

Write-Host "Jame Trader — Dell setup"
Write-Host "Folder: $Root"

Write-Step "Checking Python 3.11+"
$py = Find-Python311
if (-not $py) {
    Write-Host "Python 3.11+ not found. Installing 3.12 with winget..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host @"
Could not find Python 3.11+ or winget.

Install Python 3.12 from https://www.python.org/downloads/
Check "Add python.exe to PATH", then double-click setup_host.bat again.
"@ -ForegroundColor Yellow
        exit 1
    }
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python311
    if (-not $py) {
        Write-Host "Python 3.12 installed, but this window cannot see it yet." -ForegroundColor Yellow
        Write-Host "Close this window, then double-click setup_host.bat again."
        exit 1
    }
}

$ver = Get-PyVer -Exe $py.Exe -PrefixArgs $py.Args
Write-Host ("Using {0} {1}  (3.{2})" -f $py.Exe, ($py.Args -join ' '), ($ver % 100))

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
$venvOk = $false
if (Test-Path $venvPython) {
    $venvVer = Get-PyVer -Exe $venvPython
    if ($venvVer -ge 311) { $venvOk = $true }
    else {
        Write-Step "Existing .venv is Python 3.$($venvVer % 100) — replacing it"
        Remove-Item -Recurse -Force (Join-Path $Root '.venv')
    }
}

if (-not $venvOk) {
    Write-Step "Creating .venv"
    & $py.Exe @($py.Args + @('-m', 'venv', (Join-Path $Root '.venv')))
    if ($LASTEXITCODE -ne 0) { throw 'venv create failed' }
}

Write-Step "Installing packages"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

$config = Join-Path $Root 'config.py'
$example = Join-Path $Root 'config.example.py'
if (-not (Test-Path $config)) {
    Write-Step "No config.py — copying from config.example.py"
    Copy-Item $example $config
    Write-Host "Edit config.py and put in your Schwab API key/secret (Notepad is fine)." -ForegroundColor Yellow
    Write-Host "  notepad `"$config`""
    Write-Host "Leave TRADE_DRY_RUN = True until you mean to go live."
}

if ($InstallStartup) {
    Write-Step "Registering logon task"
    $install = Join-Path $Root 'install_host_startup.ps1'
    & $install
}

Write-Step "Starting supervisor (leave this window open)"
Write-Host "Dashboard: http://127.0.0.1:8787/"
$sup = Join-Path $Root 'supervisor.ps1'
if ($NoTunnel) {
    & $sup -NoTunnel
} else {
    & $sup
}
