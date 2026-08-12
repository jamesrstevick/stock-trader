# One-shot Dell setup: Python 3.11+, venv, deps, config.py, then supervisor.
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_host.ps1

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
        $code = 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'
        $argList = @()
        if ($PrefixArgs) { $argList += $PrefixArgs }
        $argList += @('-c', $code)
        $out = & $Exe @argList 2>$null
        $n = 0
        if ([int]::TryParse(("$out").Trim(), [ref]$n)) { return $n }
    } catch {}
    return 0
}

function Update-SessionPath {
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Get-PythonSearchRoots {
    @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:ProgramFiles\Python313",
        "$env:ProgramFiles\Python312",
        "$env:ProgramFiles\Python311",
        "${env:ProgramFiles(x86)}\Python313",
        "${env:ProgramFiles(x86)}\Python312",
        "${env:ProgramFiles(x86)}\Python311"
    )
}

function Find-Python311 {
    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($pair in @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.13') },
        @{ Exe = 'py'; Args = @('-3.11') }
    )) {
        $candidates.Add($pair)
    }

    foreach ($root in Get-PythonSearchRoots) {
        if (-not (Test-Path $root)) { continue }
        Get-ChildItem -Path $root -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\WindowsApps\\' } |
            ForEach-Object { $candidates.Add(@{ Exe = $_.FullName; Args = @() }) }
    }

    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd -and $pyCmd.Source -and ($pyCmd.Source -notmatch '\\WindowsApps\\')) {
        $candidates.Add(@{ Exe = $pyCmd.Source; Args = @() })
    }

    foreach ($c in $candidates) {
        $isPath = Test-Path -LiteralPath $c.Exe
        $onPath = [bool](Get-Command $c.Exe -ErrorAction SilentlyContinue)
        if (-not $isPath -and -not $onPath) { continue }
        $ver = Get-PyVer -Exe $c.Exe -PrefixArgs $c.Args
        if ($ver -ge 311) { return $c }
    }
    return $null
}

Write-Host "Jame Trader - Dell setup"
Write-Host "Folder: $Root"

Write-Step "Checking Python 3.11+"
Update-SessionPath
$py = Find-Python311
if (-not $py) {
    Write-Host "Python 3.11+ not found. Installing 3.12 with winget..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "Could not find Python 3.11+ or winget." -ForegroundColor Yellow
        Write-Host "Install Python 3.12 from https://www.python.org/downloads/"
        Write-Host "Check Add python.exe to PATH, then run this script again."
        exit 1
    }
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Start-Sleep -Seconds 2
    Update-SessionPath
    $py = Find-Python311
    if (-not $py) {
        Write-Host "Python 3.12 may be installed, but it is not on PATH yet." -ForegroundColor Yellow
        Write-Host "Close this window, open a NEW PowerShell in this folder, then run:"
        Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_host.ps1"
        exit 1
    }
}

$ver = Get-PyVer -Exe $py.Exe -PrefixArgs $py.Args
$minor = $ver % 100
Write-Host ("Using {0} {1}  (3.{2})" -f $py.Exe, ($py.Args -join ' '), $minor)

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
$venvOk = $false
if (Test-Path $venvPython) {
    $venvVer = Get-PyVer -Exe $venvPython
    if ($venvVer -ge 311) {
        $venvOk = $true
    } else {
        Write-Step ("Existing .venv is Python 3.{0} - replacing it" -f ($venvVer % 100))
        Remove-Item -Recurse -Force (Join-Path $Root '.venv')
    }
}

if (-not $venvOk) {
    Write-Step "Creating .venv"
    $venvArgs = @()
    if ($py.Args) { $venvArgs += $py.Args }
    $venvArgs += @('-m', 'venv', (Join-Path $Root '.venv'))
    & $py.Exe @venvArgs
    if ($LASTEXITCODE -ne 0) { throw 'venv create failed' }
}

Write-Step "Installing packages"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

$config = Join-Path $Root 'config.py'
$example = Join-Path $Root 'config.example.py'
if (-not (Test-Path $config)) {
    Write-Step "No config.py - copying from config.example.py"
    Copy-Item $example $config
    Write-Host "Edit config.py and put in your Schwab API key/secret." -ForegroundColor Yellow
    Write-Host ("  notepad {0}" -f $config)
    Write-Host "Leave TRADE_DRY_RUN = True until you mean to go live."
}

if ($InstallStartup) {
    Write-Step "Registering logon task"
    & (Join-Path $Root 'install_host_startup.ps1')
}

Write-Step "Starting supervisor (leave this window open)"
Write-Host "Dashboard: http://127.0.0.1:8787/"
$sup = Join-Path $Root 'supervisor.ps1'
if ($NoTunnel) {
    & $sup -NoTunnel
} else {
    & $sup
}
