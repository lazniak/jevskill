# Install jevskill as a Skill for the harness(es) on this machine.
#
#   pwsh -File install.ps1              # auto-detect and install everywhere found
#   pwsh -File install.ps1 -Target dsh  # only the DeepSeek Harness skills dir
#   pwsh -File install.ps1 -DryRun      # show what would happen
#
# The Skill itself (skills/jev/SKILL.md + references) is copied; the CLI is installed
# in editable mode so `jevskill` resolves from this checkout.

[CmdletBinding()]
param(
    [ValidateSet('auto', 'claude', 'dsh', 'generic')]
    [string]$Target = 'auto',
    [string]$SkillsRoot,
    [switch]$DryRun,
    [switch]$SkipCli
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step  { param($m) Write-Host "  $m" }
function Write-Head  { param($m) Write-Host "`n$m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "  [ok]   $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "  [warn] $m" -ForegroundColor Yellow }

Write-Head "jevskill installer"
Write-Step "repo: $RepoRoot"

# ---------------------------------------------------------------- skill sources
$SkillSource = Join-Path $RepoRoot 'skill'
if (-not (Test-Path (Join-Path $SkillSource 'SKILL.md'))) {
    throw "skills/jev/SKILL.md not found under $RepoRoot — run this from the repository root."
}

# ---------------------------------------------------------------- destinations
$destinations = [System.Collections.Generic.List[object]]::new()

function Add-Destination {
    param([string]$Name, [string]$Root)
    if ($SkillsRoot) { $Root = Join-Path $SkillsRoot $Name }
    $destinations.Add([pscustomobject]@{ Name = $Name; Path = (Join-Path $Root 'jev') })
}

if ($Target -eq 'auto' -or $Target -eq 'claude') {
    Add-Destination -Name 'claude' -Root (Join-Path $HOME '.claude\skills')
}
if ($Target -eq 'auto' -or $Target -eq 'dsh') {
    Add-Destination -Name 'dsh' -Root (Join-Path $HOME '.dsh\skills')
}
if ($Target -eq 'auto' -or $Target -eq 'generic') {
    Add-Destination -Name 'generic' -Root (Join-Path $HOME '.config\skills')
}

if ($Target -ne 'auto') {
    $destinations = @($destinations | Where-Object { $_.Name -eq $Target })
}

# ---------------------------------------------------------------- install skill
Write-Head "Installing the Skill"
foreach ($dest in $destinations) {
    if ($DryRun) { Write-Step "would copy  $SkillSource -> $($dest.Path)" ; continue }
    try {
        New-Item -ItemType Directory -Force -Path $dest.Path | Out-Null
        Copy-Item -Path (Join-Path $SkillSource '*') -Destination $dest.Path -Recurse -Force
        Write-Ok "$($dest.Name): $($dest.Path)"
    } catch {
        Write-Warn2 "$($dest.Name): $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- install CLI
if (-not $SkipCli) {
    Write-Head "Installing the CLI"
    if ($DryRun) {
        Write-Step "would run  python -m pip install -e `"$RepoRoot`""
    } else {
        & python -m pip install -e $RepoRoot --quiet
        if ($LASTEXITCODE -eq 0) { Write-Ok 'CLI installed (editable)' }
        else { Write-Warn2 'pip install failed — the Skill still works if you call python -m jevskill' }
    }
}

# ---------------------------------------------------------------- api key
Write-Head "Checking the API key"
$existing = @('JEVSKILL_API_KEY', 'OPENROUTER_API_KEY', 'OPEN_ROUTER_API_KEY', 'JEVUSE_API_KEY') |
    ForEach-Object { [Environment]::GetEnvironmentVariable($_, 'User') } |
    Where-Object { $_ } | Select-Object -First 1

if ($existing) {
    $preview = $existing.Substring(0, [Math]::Min(12, $existing.Length))
    Write-Ok "found a key ($preview...)"
} else {
    Write-Warn2 'no key found in the user environment.'
    Write-Step 'set one with:  setx OPENROUTER_API_KEY "sk-or-v1-..."'
    Write-Step 'then open a NEW terminal (setx does not affect running shells).'
}

# ---------------------------------------------------------------- verify
if (-not $DryRun) {
    Write-Head "Verifying"
    try {
        & python -m jevskill doctor
    } catch {
        Write-Warn2 "doctor failed: $($_.Exception.Message)"
    }
}

Write-Host ''
