[CmdletBinding(DefaultParameterSetName = 'Routing')]
param(
    [Parameter(Mandatory=$true, ParameterSetName='Routing')][string]$TaskFile,
    [Parameter(Mandatory=$true, ParameterSetName='Routing')][string]$SnapshotFile,
    [Parameter(Mandatory=$true, ParameterSetName='Routing')][double]$MaxCostUsd,
    [Parameter(ParameterSetName='Routing')][string]$TodoFile,
    [Parameter(ParameterSetName='Routing')][string]$TaskId,
    [Parameter(ParameterSetName='Routing')][string]$ReuseReport,
    [Parameter(Mandatory=$true, ParameterSetName='Escalation')][string]$CaseFile,
    [Parameter(ParameterSetName='Escalation')][switch]$Baseline,
    [Parameter(ParameterSetName='Escalation')][switch]$IncludeRedactedSummary,
    [Parameter(ParameterSetName='Escalation')][ValidateSet('shadow','guarded')][string]$PolicyMode = 'guarded',
    [Parameter(ParameterSetName='Escalation')][double]$EscalationMaxCostUsd,
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [string]$SecretReferenceFile
)
$ErrorActionPreference = 'Stop'
# Escalation mode (-CaseFile) is additive: default parameter set 'Routing'
# keeps every existing routing call (-TaskFile/-SnapshotFile/-MaxCostUsd)
# unchanged. -Baseline is fully offline and never touches the API key.
$escalationMode = $PSCmdlet.ParameterSetName -eq 'Escalation'
$offline = ($escalationMode -and $Baseline) -or (-not $escalationMode -and $ReuseReport)
$previous = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY','Process')
$secure = $null
$resultCode = 2
try {
    if (-not $escalationMode -and ([bool]$TodoFile -ne [bool]$TaskId)) {
        throw 'TODO_AND_TASK_ID_REQUIRED_TOGETHER'
    }
    if ($escalationMode -and -not $Baseline -and -not $EscalationMaxCostUsd) {
        throw 'ESCALATION_MAX_COST_REQUIRED'
    }
    if (-not $offline) {
        try {
        # Python may pass PowerShell 7's PSModulePath to Windows PowerShell 5.1.
        # Import this host's built-ins explicitly rather than autoloading an
        # incompatible module during DPAPI/config access. No global setting changes.
        Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Management" -ErrorAction Stop
        Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Utility" -ErrorAction Stop
        Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Security" -ErrorAction Stop
        # Reuse the existing in-memory key retrieval unchanged; this task never
        # reads or writes credentials/config beyond what already existed.
        if ([string]::IsNullOrWhiteSpace($previous)) {
            if (-not $SecretReferenceFile) {
                $userKey = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY','User')
                if (-not [string]::IsNullOrWhiteSpace($userKey)) {
                    $env:OPENROUTER_API_KEY = $userKey
                } else {
                    $configPath = Join-Path $env:USERPROFILE '.agents\jev-router\config.json'
                    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
                        $localConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
                        $SecretReferenceFile = $localConfig.secret_reference_file
                    }
                }
            }
            if ($SecretReferenceFile) {
                # Existing owner-created DPAPI secret. Never create or rewrite it.
                $reference = Get-Content -LiteralPath $SecretReferenceFile -Raw | ConvertFrom-Json
                $secure = ConvertTo-SecureString -String (Get-Content -LiteralPath $reference.secret_ref -Raw)
                $env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $secure).Password
            }
        }
        # An unavailable credential is an unavailable advisor, not a reason to
        # prevent the Python entrypoint from producing its local fallback.
        } catch {
            [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', $null, 'Process')
        }
    }
    if ($escalationMode) {
        if ($Baseline) {
            & python -B "$PSScriptRoot\escalation_advisor.py" baseline --case $CaseFile --output $OutputFile
        } else {
            $cost = $EscalationMaxCostUsd.ToString([Globalization.CultureInfo]::InvariantCulture)
            $summaryArgs = @()
            if ($IncludeRedactedSummary) { $summaryArgs += '--include-redacted-summary' }
            & python -B "$PSScriptRoot\escalation_advisor.py" advise --case $CaseFile --max-cost-usd $cost --output $OutputFile --policy-mode $PolicyMode @summaryArgs
        }
    } else {
        $cost = $MaxCostUsd.ToString([Globalization.CultureInfo]::InvariantCulture)
        $bindingArgs = @()
        if ($TodoFile) { $bindingArgs += @('--todo', $TodoFile, '--task-id', $TaskId) }
        if ($ReuseReport) { $bindingArgs += @('--reuse-report', $ReuseReport) }
        & python -B "$PSScriptRoot\routing_advisor.py" advise --task $TaskFile --snapshot $SnapshotFile --max-cost-usd $cost --output $OutputFile @bindingArgs
    }
    $resultCode = $LASTEXITCODE
} catch {
    # Preserve a failure exit status without disclosing DPAPI content or paths.
    Write-Output '{"status":"ERROR","code":"JEV_WRAPPER_FAILED"}'
} finally {
    [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY',$previous,'Process')
    if ($null -ne $secure) { $secure.Dispose() }
}
exit $resultCode
