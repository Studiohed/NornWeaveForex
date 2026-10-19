#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Install / upgrade the Windows-side MetaTrader 5 RPyC server for NornWeaveForex.

.DESCRIPTION
  Copies this checkout to C:\NornWeaveForex, creates a virtualenv, installs the
  package with the [mt5server] extra, generates the shared secret, opens the
  firewall port for the private profile and registers a startup scheduled task
  that runs `nornweave-mt5-server` as SYSTEM.

  The MetaTrader 5 terminal must already be installed and logged in. The Linux
  bot connects to this machine over the LAN/VPN; never expose the port to the
  internet.

.EXAMPLE
  .\deploy\windows\install-mt5-server.ps1
  .\deploy\windows\install-mt5-server.ps1 -Port 18813 -RepoPath C:\src\NornWeaveForex
#>
param(
    [string]$RepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$InstallDir = "C:\NornWeaveForex",
    [int]$Port = 18813,
    [string]$Python = "python",
    [string]$Secret = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "NornWeave MT5 Server"

Write-Host "==> checkout:  $RepoPath"
Write-Host "==> install:   $InstallDir"
Write-Host "==> RPyC port: $Port"

if (-not (Test-Path (Join-Path $RepoPath "pyproject.toml"))) {
    throw "Cannot find pyproject.toml under $RepoPath"
}
& $Python --version | Out-Host

# 1) Sync sources (robocopy exit codes 0-7 are success).
Write-Host "==> syncing sources"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$robo = Start-Process robocopy -Wait -PassThru -NoNewWindow -ArgumentList @(
    $RepoPath, $InstallDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS",
    "/XD", ".git", ".venv", ".venv-pkgtest", "state", "dist", "build",
    "__pycache__", ".pytest_cache",
    "/XF", "*.pyc", ".env"
)
if ($robo.ExitCode -ge 8) { throw "robocopy failed with exit code $($robo.ExitCode)" }

# 2) Virtualenv + package.
$Venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "==> creating virtualenv"
    & $Python -m venv $Venv
}
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip setuptools wheel
Write-Host "==> installing package with [mt5server]"
& $Py -m pip install --quiet --upgrade "$InstallDir[mt5server]"

# 3) Shared secret (machine scope, so the SYSTEM task can read it).
if ([string]::IsNullOrWhiteSpace($Secret)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $Secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
}
[Environment]::SetEnvironmentVariable("NORNWEAVE_MT5_SECRET", $Secret, "Machine")
[Environment]::SetEnvironmentVariable("NORNWEAVE_MT5_PORT", "$Port", "Machine")

# 4) Firewall: private profile only.
$ruleName = "NornWeave MT5 RPyC ($Port)"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow -Profile Private | Out-Null
    Write-Host "==> firewall rule created (private profile)"
}

# 5) Startup task as SYSTEM.
$Exe = Join-Path $Venv "Scripts\nornweave-mt5-server.exe"
$Action = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory $InstallDir `
    -Argument "--host 0.0.0.0 --port $Port"
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -User "SYSTEM" -RunLevel Highest -Settings $Settings -Force | Out-Null
Write-Host "==> scheduled task '$TaskName' registered"

Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Installed and started."
Write-Host "Shared secret (put the SAME value in /etc/nornweave/nornweave.env on Linux):"
Write-Host "  $Secret"
Write-Host ""
Write-Host "Verify:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-List State"
Write-Host "  netstat -ano | findstr :$Port"
Write-Host "  Get-Content `$env:ProgramData\NornWeaveForex\mt5-server.log -Tail 20  (if redirected)"
