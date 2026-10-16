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
        $hit = Select-String -LiteralPath $md -Pattern '^\s+version:\s*(\S+)' | Select-Object -First 1
        if ($hit) { return $hit.Matches[0].Groups[1].Value }
    }
    return "unknown"
}

Write-Host ""
Write-Host "  PM & QA Cockpit - bootstrap ($Skill $Version)" -ForegroundColor Green
if ($DryRun) { Write-Host "  DRY RUN: nothing will be written." -ForegroundColor Cyan }
Write-Host ""

$installed = 0; $current = 0; $differs = 0
foreach ($root in $Roots) {
    $dest = Join-Path $root $Skill
    if (-not (Test-Path -LiteralPath (Join-Path $dest "SKILL.md"))) {
        if ($DryRun) {
            Write-Host "  [would install] $dest" -ForegroundColor Cyan
        } else {
            New-Item -ItemType Directory -Force -Path $root | Out-Null
            Copy-Item -LiteralPath $Src -Destination $dest -Recurse
            $manifest = @{ source = "pm-qa-cockpit"; version = $Version; installed = (Get-Date -Format s) } | ConvertTo-Json
            Set-Content -LiteralPath (Join-Path $dest ".pm-qa-cockpit.json") -Value $manifest -Encoding UTF8
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
