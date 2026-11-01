[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("claude", "kimi", "codex", "agy", "antigravity", "grok", "qwen", "qwen-local", "opencode", "minimax", "muse")]
    [string]$Agent,

    [Parameter(Mandatory = $true)]
    [string]$Prompt,

    [string]$Cwd,

    [string]$Model,

    [string]$ExecutionProfile,

    [ValidateSet("advisory", "review", "implement")]
    [string]$Mode = "advisory",

    [ValidateSet("low", "medium", "high")]
    [string]$Complexity = "medium",

    [string]$AssessmentFile,

    [string]$RequiredInputsFile,

    [switch]$AllowWrite,

    [switch]$AutoApprove,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Cwd)) {
    $Cwd = $ExecutionContext.SessionState.Path.CurrentLocation.Path
}
$Cwd = [System.IO.Path]::GetFullPath($Cwd)
if (-not (Test-Path -LiteralPath $Cwd -PathType Container)) {
    throw "Working directory does not exist: $Cwd"
}

if ($Agent -eq "agy") {
    $Agent = "antigravity"
}
if ($AutoApprove) {
    $Mode = "implement"
    $AllowWrite = $true
}
if ($Mode -eq "implement" -and -not $AllowWrite) {
    throw "Implement mode requires -AllowWrite."
}
if ($Mode -ne "implement" -and $AllowWrite) {
    throw "-AllowWrite is valid only with -Mode implement."
}

if (
    [string]::IsNullOrWhiteSpace($ExecutionProfile) -and
    $Agent -eq "qwen" -and
    $Model -eq "deepseek-v4-pro"
) {
    if ($Complexity -eq "high" -and $Mode -ne "implement") {
        $ExecutionProfile = "qwen-deepseek-v4-pro-max"
    } else {
        $ExecutionProfile = "qwen-deepseek-v4-pro-high"
    }
}

$SuiteRoot = $env:AGENT_FLEET_SUITE_ROOT
if ([string]::IsNullOrWhiteSpace($SuiteRoot)) {
    $SuiteRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$Fleet = Join-Path $SuiteRoot "agent-fleet\scripts\fleet.py"
if (-not (Test-Path -LiteralPath $Fleet -PathType Leaf)) {
    throw "Agent Fleet is not installed: $Fleet"
}
# Fleet owns freshness, certified-profile fallback and quota preflight.
# A second facade gate can otherwise block a valid fallback before Fleet runs.

$Arguments = @(
    $Fleet,
    "run",
    "--agent", $Agent,
    "--task", $Prompt,
    "--workspace", $Cwd,
    "--complexity", $Complexity,
    "--mode", $Mode
)
if ($Model) {
    $Arguments += @("--model", $Model)
}
if ($ExecutionProfile) {
    $Arguments += @("--execution-profile", $ExecutionProfile)
}
if ($AssessmentFile) {
    $Arguments += @("--assessment-file", [System.IO.Path]::GetFullPath($AssessmentFile))
}
if ($RequiredInputsFile) {
    $Arguments += @("--required-inputs-file", [System.IO.Path]::GetFullPath($RequiredInputsFile))
}
if ($AllowWrite) {
    $Arguments += "--allow-write"
}
if ($DryRun) {
    $Arguments += "--dry-run"
}

& python @Arguments
exit $LASTEXITCODE
