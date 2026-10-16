#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the PM & QA Cockpit (local dashboard, default port 3333).

.DESCRIPTION
    Optionally selects the active project, frees the port from stale processes,
    opens the browser once the server listens, then runs the zero-dependency server.

.PARAMETER Project
    Registered project slug to make active before launching.

.PARAMETER Port
    Port to listen on (sets PM_QA_COCKPIT_PORT). Default 3333.

.EXAMPLE
    powershell -File cockpit.ps1 -Project money-nest
#>
param(
    [string]$Project,
    [int]$Port = 3333
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerPath = Join-Path $RootDir "cockpit\server.py"
$CliPath = Join-Path $RootDir "cockpit\cli.py"

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Green
Write-Host "    PM & QA Cockpit" -ForegroundColor Green
Write-Host "  ================================================" -ForegroundColor Green
Write-Host "  Dashboard URL: http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  Starting server and opening browser..." -ForegroundColor Gray
Write-Host ""

$env:PM_QA_COCKPIT_PORT = "$Port"
Set-Location $RootDir

if ($Project) {
    Write-Host "  [project] Selecting '$Project'..." -ForegroundColor Gray
    & uv run python $CliPath projects --select $Project
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [project] Could not select '$Project' (see above)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# Terminate any orphaned/stale process on the port
try {
    $existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        $pids = $existing | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($p in $pids) {
            if ($p -gt 0) {
                Write-Host "  [cleanup] Stopping existing process on port $Port (PID $p)..." -ForegroundColor Yellow
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Milliseconds 400
    }
} catch {
    # Non-fatal if Get-NetTCPConnection fails
}

# Open browser once the server is listening
$browserJob = Start-Job -ArgumentList $Port -ScriptBlock {
    param($ListenPort)
    for ($i = 0; $i -lt 50; $i++) {
        Start-Sleep -Milliseconds 200
        try {
            $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
            if ($conn) {
                Start-Process "http://localhost:$ListenPort"
                break
            }
        } catch {}
    }
}

try {
    & uv run python $ServerPath
} finally {
    Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
}
