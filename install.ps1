#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the PM & QA Cockpit skills into the agent skill roots.

.DESCRIPTION
    Copies skills\* into ~\.claude\skills, ~\.agents\skills and ~\.cursor\skills-cursor.
    Identical files are skipped; changed files are reported as "differs" unless -Force.
    Installs grill-me (from mattpocock/skills) when neither grill-me nor grilling is present.

.PARAMETER DryRun
    Print what would happen; write nothing, touch no network. Alias: -Check.

.PARAMETER Force
    Overwrite files that differ.

.PARAMETER RequireGrillMe
    Exit non-zero if grill-me is needed but could not be downloaded.

.EXAMPLE
    powershell -NoProfile -File install.ps1 -DryRun
#>
param(
    [Alias("Check")]
    [switch]$DryRun,
    [switch]$Force,
    [switch]$RequireGrillMe
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillsSrc = Join-Path $RepoRoot "skills"
$UserHome = [Environment]::GetFolderPath("UserProfile")

$Roots = @(
    (Join-Path $UserHome ".claude\skills"),
    (Join-Path $UserHome ".agents\skills"),
    (Join-Path $UserHome ".cursor\skills-cursor")
)
$GrillRoots = @($Roots[0], $Roots[1])
$GrillApi = "https://api.github.com/repos/mattpocock/skills/contents/skills/productivity/grill-me"
$GrillRawFallback = "https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/SKILL.md"
$HomeDir = Join-Path $UserHome ".pm-qa-cockpit"

$script:Counts = @{ copied = 0; skipped = 0; differs = 0 }

function Get-FileHashSafe([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
}

function Install-File([string]$Src, [string]$Dest) {
    if (-not (Test-Path -LiteralPath $Dest)) {
        if ($DryRun) { Write-Host "  [would copy ] $Dest" -ForegroundColor Cyan }
        else {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
            Copy-Item -LiteralPath $Src -Destination $Dest
            Write-Host "  [copied     ] $Dest" -ForegroundColor Green
        }
        $script:Counts.copied++
        return
    }
    if ((Get-FileHashSafe $Src) -eq (Get-FileHashSafe $Dest)) {
        Write-Host "  [identical  ] $Dest" -ForegroundColor DarkGray
        $script:Counts.skipped++
        return
    }
    if ($Force) {
        if ($DryRun) { Write-Host "  [would overwrite] $Dest" -ForegroundColor Yellow }
        else {
            Copy-Item -LiteralPath $Src -Destination $Dest -Force
            Write-Host "  [overwritten] $Dest" -ForegroundColor Yellow
        }
        $script:Counts.copied++
    } else {
        Write-Host "  [differs    ] $Dest  (use -Force to overwrite)" -ForegroundColor Yellow
        $script:Counts.differs++
    }
}

Write-Host ""
Write-Host "  PM & QA Cockpit - skill installer" -ForegroundColor Green
if ($DryRun) { Write-Host "  DRY RUN: nothing will be written, no network used." -ForegroundColor Cyan }
Write-Host ""

# 1. Cockpit skills
$skillDirs = @(Get-ChildItem -LiteralPath $SkillsSrc -Directory)
foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root)) {
        if ($DryRun) { Write-Host "Root missing, would create: $root" -ForegroundColor Cyan }
        else { New-Item -ItemType Directory -Force -Path $root | Out-Null }
    }
    Write-Host "Root: $root" -ForegroundColor White
    foreach ($dir in $skillDirs) {
        $files = @(Get-ChildItem -LiteralPath $dir.FullName -File -Recurse)
        foreach ($f in $files) {
            $rel = $f.FullName.Substring($dir.FullName.Length).TrimStart('\', '/')
            $dest = Join-Path (Join-Path $root $dir.Name) $rel
            Install-File $f.FullName $dest
        }
    }
    Write-Host ""
}

# 2. grill-me
$grillPresent = $false
foreach ($root in $Roots) {
    foreach ($name in @("grill-me", "grilling")) {
        if (Test-Path -LiteralPath (Join-Path (Join-Path $root $name) "SKILL.md")) { $grillPresent = $true }
    }
}
$grillFailed = $false
if ($grillPresent) {
    Write-Host "grill-me: already present (grill-me or grilling found) - not fetching." -ForegroundColor Green
} elseif ($DryRun) {
    Write-Host "grill-me: NOT present - would download from mattpocock/skills (skills/productivity/grill-me) into:" -ForegroundColor Cyan
    foreach ($r in $GrillRoots) { Write-Host "  $(Join-Path $r 'grill-me')" -ForegroundColor Cyan }
} else {
    Write-Host "grill-me: not present - downloading from mattpocock/skills..." -ForegroundColor White
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $items = @()
        try {
            $listing = Invoke-RestMethod -Uri $GrillApi -Headers @{ "User-Agent" = "pm-qa-cockpit-installer" }
            $items = @($listing | Where-Object { $_.type -eq "file" } |
                ForEach-Object { @{ name = $_.name; url = $_.download_url } })
        } catch {
            Write-Host "  API listing failed ($($_.Exception.Message)); falling back to SKILL.md only." -ForegroundColor Yellow
        }
        if ($items.Count -eq 0) { $items = @(@{ name = "SKILL.md"; url = $GrillRawFallback }) }
        foreach ($r in $GrillRoots) {
            $target = Join-Path $r "grill-me"
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            foreach ($it in $items) {
                Invoke-WebRequest -Uri $it.url -OutFile (Join-Path $target $it.name) -UseBasicParsing
                Write-Host "  [downloaded ] $(Join-Path $target $it.name)" -ForegroundColor Green
            }
        }
    } catch {
        $grillFailed = $true
        Write-Host "  WARNING: could not download grill-me: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "  Install it manually from https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me" -ForegroundColor Yellow
    }
}
Write-Host ""

# 3. Cockpit home
if (Test-Path -LiteralPath $HomeDir) {
    Write-Host "Registry home exists: $HomeDir" -ForegroundColor DarkGray
} elseif ($DryRun) {
    Write-Host "Would create registry home: $HomeDir" -ForegroundColor Cyan
} else {
    New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
    Write-Host "Created registry home: $HomeDir" -ForegroundColor Green
}

Write-Host ""
Write-Host ("Summary: {0} to copy/copied, {1} identical, {2} differ" -f $script:Counts.copied, $script:Counts.skipped, $script:Counts.differs)
Write-Host ""
Write-Host "Register a project (report first, then add --apply):" -ForegroundColor White
Write-Host "  uv run python $RepoRoot\cockpit\cli.py init <code_path> --name <Name> --prefix <PREFIX> [--docs-path <external docs>] [--stealth]"
Write-Host "Launch:  powershell -File $RepoRoot\cockpit.ps1"
Write-Host ""

if ($grillFailed -and $RequireGrillMe) { exit 1 }
exit 0
