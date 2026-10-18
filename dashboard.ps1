#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the PM & QA Board (local dashboard, default port 3333).

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

.PARAMETER Local
    Enables local-only access (binds to 127.0.0.1, PC only).

.PARAMETER Tunnel
    Enables public internet access via Cloudflare Quick Tunnel with random session token protection.

.PARAMETER Public
    Alias for -Tunnel.

.PARAMETER Mode
    Explicit mode string: 'mobile', 'local', or 'tunnel'.

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
    [switch]$Local,
    [string]$Mode = "",
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

if ($Mode) {
    switch ($Mode.ToLower()) {
        "mobile" { $Mobile = $true }
        "local"  { $Local = $true }
        "tunnel" { $Tunnel = $true }
        "public" { $Tunnel = $true }
        default  { $Mobile = $true }
    }
}

if (-not $PSBoundParameters.ContainsKey('Mobile') -and
    -not $PSBoundParameters.ContainsKey('Tunnel') -and
    -not $PSBoundParameters.ContainsKey('Public') -and
    -not $PSBoundParameters.ContainsKey('Local') -and
    -not $Mode) {

    $isInteractive = [Environment]::UserInteractive
    try {
        if ([Console]::IsInputRedirected) { $isInteractive = $false }
    } catch {}

    if ($isInteractive) {
        Write-Host ""
        Write-Host "  ================================================" -ForegroundColor Cyan
        Write-Host "    PM & QA Board Launcher" -ForegroundColor Cyan
        Write-Host "  ================================================" -ForegroundColor Cyan
        Write-Host "  Select dashboard access mode:" -ForegroundColor Cyan
        Write-Host "    [1] Mobile Wi-Fi (LAN access for phone on same Wi-Fi, keep-awake) [default]" -ForegroundColor Green
        Write-Host "    [2] Local only   (127.0.0.1 loopback, PC only)" -ForegroundColor DarkGray
        Write-Host "    [3] Public Tunnel (Cloudflare Quick Tunnel with secure session token)" -ForegroundColor Yellow
        Write-Host ""
        $choice = Read-Host "  Choose mode [1]"
        $choice = if ($choice) { $choice.Trim() } else { "" }
        if ($choice -eq "" -or $choice -eq "1" -or $choice.ToLower() -eq "mobile" -or $choice.ToLower() -eq "m") {
            $Mobile = $true
        } elseif ($choice -eq "2" -or $choice.ToLower() -eq "local" -or $choice.ToLower() -eq "l") {
            $Local = $true
        } elseif ($choice -eq "3" -or $choice.ToLower() -eq "tunnel" -or $choice.ToLower() -eq "t" -or $choice.ToLower() -eq "public") {
            $Tunnel = $true
        } else {
            Write-Host "  Unknown choice '$choice', defaulting to Mobile." -ForegroundColor DarkYellow
            $Mobile = $true
        }
    } else {
        # Default to Mobile mode when started without arguments
        $Mobile = $true
    }
}

if ($Mobile) {
    if (-not $HostName) {
        $HostName = "0.0.0.0"
    }
    $env:PM_QA_COCKPIT_KEEP_ALIVE = "1"
} elseif ($Local) {
    if (-not $HostName) {
        $HostName = "127.0.0.1"
    }
    $env:PM_QA_COCKPIT_KEEP_ALIVE = "0"
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
$tunnelLog = $null
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
    $tunnelLog = [System.IO.Path]::GetTempFileName()
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cfExe
    $psi.Arguments = "tunnel --logfile `"$tunnelLog`" --url http://127.0.0.1:$Port"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $tunnelProcess = [System.Diagnostics.Process]::Start($psi)

    # Watch log for trycloudflare.com URL without blocking
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt 15000 -and -not $tunnelUrl) {
        Start-Sleep -Milliseconds 250
        if ($tunnelProcess.HasExited) {
            break
        }
        if (Test-Path $tunnelLog) {
            try {
                $fs = [System.IO.File]::Open($tunnelLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                $sr = New-Object System.IO.StreamReader($fs)
                $logContent = $sr.ReadToEnd()
                $sr.Close()
                $fs.Close()
                if ($logContent -match '(https://[a-zA-Z0-9-]+\.trycloudflare\.com)') {
                    $tunnelUrl = $matches[1]
                    break
                }
            } catch {}
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
Write-Host "    PM & QA Board" -ForegroundColor Green
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
    if ($tunnelLog -and (Test-Path $tunnelLog)) {
        Remove-Item $tunnelLog -Force -ErrorAction SilentlyContinue
    }
}
