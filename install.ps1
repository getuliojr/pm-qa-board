#Requires -Version 5.1
<#
.SYNOPSIS
    Bootstrap for PM & QA Cockpit: installs ONLY the `pm-qa-setup` skill. Never overwrites anything.

.DESCRIPTION
    Copies skills\pm-qa-setup into ~\.claude\skills, ~\.agents\skills and ~\.cursor\skills-cursor
    where it is missing. Where a pm-qa-setup already exists it is compared with line endings ignored:
    identical -> left alone; different -> reported and left alone (your agent upgrades it, with your
    approval, through the skill itself). Nothing else is installed and nothing is downloaded.

    Everything else - prerequisites, grill-me, the project's skills, pointers in CLAUDE.md/AGENTS.md,
    registration and gate profiling - is done by an agent running the pm-qa-setup skill, which
    inspects the project and changes only what you approve.

.PARAMETER DryRun
    Print what would happen; write nothing. Alias: -Check.

.EXAMPLE
    powershell -NoProfile -File install.ps1 -DryRun
#>
param(
    [Alias("Check")]
    [switch]$DryRun,
    # Install under another home folder (used by the test suite); defaults to your user profile.
    [string]$TargetHome = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Skill = "pm-qa-setup"
$Src = Join-Path (Join-Path $RepoRoot "skills") $Skill
$UserHome = if ($TargetHome) { $TargetHome } else { [Environment]::GetFolderPath("UserProfile") }
$Version = (Get-Content -LiteralPath (Join-Path $RepoRoot "VERSION") -Raw).Trim()
$Roots = @(
    (Join-Path $UserHome ".claude\skills"),
    (Join-Path $UserHome ".agents\skills"),
    (Join-Path $UserHome ".cursor\skills-cursor")
)

function Get-NormalizedText([string]$Path) {
    # CRLF/CR -> LF so a Windows checkout never reads as "changed"
    return ([IO.File]::ReadAllText($Path)) -replace "`r`n", "`n" -replace "`r", "`n"
}

function Test-SameSkill([string]$A, [string]$B) {
    $fa = @(Get-ChildItem -LiteralPath $A -File -Recurse | Where-Object { $_.Name -ne ".pm-qa-cockpit.json" })
    $fb = @(Get-ChildItem -LiteralPath $B -File -Recurse | Where-Object { $_.Name -ne ".pm-qa-cockpit.json" })
    if ($fa.Count -ne $fb.Count) { return $false }
    foreach ($f in $fa) {
        $rel = $f.FullName.Substring($A.Length).TrimStart('\', '/')
        $other = Join-Path $B $rel
        if (-not (Test-Path -LiteralPath $other)) { return $false }
        if ((Get-NormalizedText $f.FullName) -ne (Get-NormalizedText $other)) { return $false }
    }
    return $true
}

function Get-InstalledVersion([string]$Dir) {
    $m = Join-Path $Dir ".pm-qa-cockpit.json"
    if (Test-Path -LiteralPath $m) {
        try { return (Get-Content -LiteralPath $m -Raw | ConvertFrom-Json).version } catch {}
    }
    $md = Join-Path $Dir "SKILL.md"
    if (Test-Path -LiteralPath $md) {
        # only the frontmatter (between the first two --- lines)
        $text = Get-NormalizedText $md
        if ($text -match '(?s)^---\n(.*?)\n---') {
            if ($Matches[1] -match '(?m)^\s+version:\s*(\S+)') { return $Matches[1] }
        }
    }
    return "unknown"
}

function Get-NormalizedBytes([string]$Path) {
    $b = [IO.File]::ReadAllBytes($Path)
    $out = New-Object System.Collections.Generic.List[byte] ($b.Length)
    for ($i = 0; $i -lt $b.Length; $i++) {
        if ($b[$i] -eq 13) {
            $out.Add(10)
            if ($i + 1 -lt $b.Length -and $b[$i + 1] -eq 10) { $i++ }
        } else { $out.Add($b[$i]) }
    }
    return ,$out.ToArray()
}

function Get-SkillHash([string]$Dir) {
    # Same as setup_flow.content_hash: per file (ordinal order of the relative posix path):
    # path bytes, NUL, LF-normalised content, NUL. The manifest itself is excluded.
    $root = (Resolve-Path -LiteralPath $Dir).Path
    $map = @{}
    foreach ($f in Get-ChildItem -LiteralPath $root -File -Recurse) {
        if ($f.Name -eq ".pm-qa-cockpit.json") { continue }
        $rel = $f.FullName.Substring($root.Length).TrimStart('\', '/') -replace '\\', '/'
        $map[$rel] = $f.FullName
    }
    [string[]]$keys = @($map.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $ms = New-Object IO.MemoryStream
    $utf8 = New-Object Text.UTF8Encoding($false)
    foreach ($k in $keys) {
        $p = $utf8.GetBytes($k); $ms.Write($p, 0, $p.Length); $ms.WriteByte(0)
        $c = Get-NormalizedBytes $map[$k]; $ms.Write($c, 0, $c.Length); $ms.WriteByte(0)
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash($ms.ToArray())
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
}

Write-Host ""
Write-Host "  PM & QA Cockpit - bootstrap ($Skill $Version)" -ForegroundColor Green
if ($DryRun) { Write-Host "  DRY RUN: nothing will be written." -ForegroundColor Cyan }
Write-Host ""

$installed = 0; $current = 0; $differs = 0
foreach ($root in $Roots) {
    $dest = Join-Path $root $Skill
    if ((Test-Path -LiteralPath $dest) -and -not (Test-Path -LiteralPath (Join-Path $dest "SKILL.md"))) {
        # a folder of the same name that isn't a skill: not ours to fill in
        Write-Host "  [differs      ] $dest  (folder exists without SKILL.md) - left untouched" -ForegroundColor Yellow
        $differs++
    } elseif (-not (Test-Path -LiteralPath $dest)) {
        if ($DryRun) {
            Write-Host "  [would install] $dest" -ForegroundColor Cyan
        } else {
            New-Item -ItemType Directory -Force -Path $root | Out-Null
            Copy-Item -LiteralPath $Src -Destination $dest -Recurse
            $manifest = [ordered]@{
                source = "pm-qa-cockpit"; version = $Version; sha256 = (Get-SkillHash $dest)
                installed = (Get-Date -Format s)
            } | ConvertTo-Json
            # UTF-8 without BOM (Python's json reader rejects a BOM)
            [IO.File]::WriteAllText((Join-Path $dest ".pm-qa-cockpit.json"), $manifest, (New-Object Text.UTF8Encoding($false)))
            Write-Host "  [installed    ] $dest" -ForegroundColor Green
        }
        $installed++
    } elseif (Test-SameSkill $Src $dest) {
        Write-Host "  [current      ] $dest" -ForegroundColor DarkGray
        $current++
    } else {
        $v = Get-InstalledVersion $dest
        Write-Host "  [differs      ] $dest  (installed $v, available $Version) - left untouched" -ForegroundColor Yellow
        $differs++
    }
}

Write-Host ""
Write-Host ("Summary: {0} {1}, {2} current, {3} left untouched" -f $installed, $(if ($DryRun) { "to install" } else { "installed" }), $current, $differs)
if ($differs -gt 0) {
    Write-Host "A different $Skill is already installed. Nothing was overwritten. To upgrade it, ask your agent:" -ForegroundColor Yellow
    Write-Host '  "check for PM & QA Cockpit upgrades"' -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next: open a project with your agent (Claude Code, Cursor, Antigravity) and ask:" -ForegroundColor White
Write-Host '  "set up PM & QA Cockpit in this project"' -ForegroundColor White
Write-Host "It inspects the project's own rules and hooks first and changes only what you approve."
Write-Host ""
exit 0
