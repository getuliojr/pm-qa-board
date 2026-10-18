#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the PM & QA Cockpit (local dashboard, default port 3333).

.DESCRIPTION
    Optionally selects the active project, frees the port from stale processes,
    opens the browser once the server listens, then runs the zero-dependency server.
    Supports mobile Wi-Fi LAN access (-Mobile) and secure Cloudflare public tunneling (-Tunnel).

.PARAMETER Project
    Registered project slug to make active before launching.

.PARAMETER Port
    Port to listen on (sets PM_QA_COCKPIT_PORT). Default 3333.

.PARAMETER Mobile
    Enables mobile LAN access (binds to 0.0.0.0, enables keep-alive, prints LAN URL).

.PARAMETER Tunnel
    Enables public internet access via Cloudflare Quick Tunnel with random session token protection.

.PARAMETER Public
    Alias for -Tunnel.

.PARAMETER Token
    Explicit session token for remote authentication (auto-generated if -Tunnel is passed without -Token).

.PARAMETER HostName
    Explicit IP address to bind to (e.g. 0.0.0.0 or 127.0.0.1). Defaults to 127.0.0.1 unless -Mobile is passed.

.EXAMPLE
    powershell -File dashboard.ps1 -Project money-nest -Mobile
#>
param(
    [string]$Project,
    [int]$Port = 3333,
    [switch]$Mobile,
    [alias("Public")][switch]$Tunnel,
    [string]$Token = "",
    [string]$HostName = ""
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerPath = Join-Path $RootDir "cockpit\server.py"
$CliPath = Join-Path $RootDir "cockpit\cli.py"

function Get-LanIPAddress {
    $ip = $null
    try {
        $addrs = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Wi-Fi*', 'Ethernet*', 'WLAN*' -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^(169\.254|127\.)' }
        if ($addrs) {
            $ip = ($addrs | Select-Object -ExpandProperty IPAddress -First 1)
        }
    } catch {}

    if (-not $ip) {
        try {
            $sock = New-Object System.Net.Sockets.Socket([System.Net.Sockets.AddressFamily]::InterNetwork, [System.Net.Sockets.SocketType]::Dgram, [System.Net.Sockets.ProtocolType]::Udp)
            $sock.Connect("8.8.8.8", 65530)
            $ip = $sock.LocalEndPoint.Address.IPAddressToString
            $sock.Close()
        } catch {
            $ip = "127.0.0.1"
        }
    }
    return $ip
}

function Get-CloudflaredPath {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $localBin = Join-Path $env:USERPROFILE ".pm-qa-cockpit\bin\cloudflared.exe"
    if (Test-Path $localBin) {
        return $localBin
    }
    return $null
}

function Install-Cloudflared {
    $binDir = Join-Path $env:USERPROFILE ".pm-qa-cockpit\bin"
    if (-not (Test-Path $binDir)) {
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    }
    $targetExe = Join-Path $binDir "cloudflared.exe"
    Write-Host "  [tunnel] cloudflared is required for -Tunnel." -ForegroundColor Yellow
    Write-Host "  Would you like to download standalone cloudflared.exe to $binDir? (Y/n): " -NoNewline -ForegroundColor Cyan
    $answer = Read-Host
    if ($answer -and $answer.Trim().ToLower() -notin @("y", "yes", "")) {
        Write-Host "  [tunnel] Download aborted by user. Exiting." -ForegroundColor Red
        return $null
    }
    Write-Host "  [tunnel] Downloading official cloudflared binary..." -ForegroundColor Gray
    $downloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13
        Invoke-WebRequest -Uri $downloadUrl -OutFile $targetExe -UseBasicParsing
        Write-Host "  [tunnel] Download complete: $targetExe" -ForegroundColor Green
        return $targetExe
    } catch {
        Write-Host "  [tunnel] Download failed: $_" -ForegroundColor Red
        return $null
    }
}

if ($Mobile) {
    if (-not $HostName) {
        $HostName = "0.0.0.0"
    }
    $env:PM_QA_COCKPIT_KEEP_ALIVE = "1"
}
if ($HostName) {
    $env:PM_QA_COCKPIT_HOST = $HostName
}

# Terminate any orphaned/stale process on the port before starting tunnel or server
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

$tunnelProcess = $null
$tunnelUrl = $null
if ($Tunnel) {
    $cfExe = Get-CloudflaredPath
    if (-not $cfExe) {
        $cfExe = Install-Cloudflared
        if (-not $cfExe) {
            exit 1
        }
    }
    if (-not $Token) {
        $Token = [System.Guid]::NewGuid().ToString("N")
    }
    $env:PM_QA_COCKPIT_TOKEN = $Token
    $env:PM_QA_COCKPIT_KEEP_ALIVE = "1"

    Write-Host "  [tunnel] Starting Cloudflare Quick Tunnel to port $Port..." -ForegroundColor Gray
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cfExe
    $psi.Arguments = "tunnel --url http://127.0.0.1:$Port"
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardOutput = $false
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $tunnelProcess = [System.Diagnostics.Process]::Start($psi)

    # Watch output for trycloudflare.com URL without blocking
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt 15000 -and -not $tunnelUrl) {
        Start-Sleep -Milliseconds 250
        if ($tunnelProcess.HasExited) {
            break
        }
        while ($tunnelProcess.StandardError.Peek() -ge 0) {
            $line = $tunnelProcess.StandardError.ReadLine()
            if ($line -match '(https://[a-zA-Z0-9-]+\.trycloudflare\.com)') {
                $tunnelUrl = $matches[1]
                break
            }
        }
    }
    if ($tunnelUrl) {
        $env:PM_QA_COCKPIT_TUNNEL_URL = $tunnelUrl
    } else {
        Write-Host "  [tunnel] Warning: Could not detect Cloudflare tunnel URL within 15 seconds." -ForegroundColor Yellow
    }
}

if ($Token) {
    $env:PM_QA_COCKPIT_TOKEN = $Token
}

$lanIp = Get-LanIPAddress

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Green
Write-Host "    PM & QA Cockpit" -ForegroundColor Green
Write-Host "  ================================================" -ForegroundColor Green
Write-Host "  Local URL:       http://localhost:$Port" -ForegroundColor Cyan
if ($Mobile -or $HostName -eq "0.0.0.0") {
    Write-Host "  Mobile Wi-Fi:    http://${lanIp}:$Port" -ForegroundColor Green
    Write-Host "  Firewall Tip:    If mobile fails, allow TCP port $Port inbound in Windows Defender Firewall" -ForegroundColor DarkGray
}
if ($tunnelUrl) {
    $authSuffix = if ($Token) { "?token=$Token" } else { "" }
    Write-Host "  Public Tunnel:   ${tunnelUrl}${authSuffix}" -ForegroundColor Yellow
}
if ($env:PM_QA_COCKPIT_KEEP_ALIVE -eq "1") {
    Write-Host "  Keep-Alive:      Active (Windows sleep prevented during session)" -ForegroundColor DarkCyan
}
if ($Token) {
    Write-Host "  Session Token:   Active (remote access authenticated)" -ForegroundColor DarkGray
}
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
    if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
        Write-Host "  [cleanup] Stopping Cloudflare tunnel..." -ForegroundColor Gray
        try {
            $tunnelProcess.Kill()
        } catch {}
    }
}
