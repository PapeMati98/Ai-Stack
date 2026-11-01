function Write-BrainstormAnonymizerInput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Evidence,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $rows = @($Evidence | ForEach-Object {
        $evidencePath = [string]$_.plik
        if ([string]::IsNullOrWhiteSpace($evidencePath) -or -not (Test-Path -LiteralPath $evidencePath -PathType Leaf)) {
            throw "BRAINSTORM_EVIDENCE_MISSING: $evidencePath"
        }

        # File.ReadAllText returns a plain .NET string. Windows PowerShell 5.1
        # otherwise serializes Get-Content's extended file properties into JSON.
        $content = [IO.File]::ReadAllText($evidencePath, [Text.Encoding]::UTF8)
        [pscustomobject]@{ content = $content }
    })

    $json = ConvertTo-Json -InputObject @($rows) -Depth 3
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $json, $utf8)
}

function Invoke-BrainstormAnonymizer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonCommand,
        [Parameter(Mandatory = $true)]
        [string]$ScriptPath,
        [Parameter(Mandatory = $true)]
        [string]$InputPath,
        [Parameter(Mandatory = $true)]
        [string]$OutputPath,
        [string[]]$Redactions = @()
    )

    $helperArgs = @($ScriptPath, "anonymize", "--input", $InputPath, "--output", $OutputPath)
    foreach ($redaction in $Redactions) {
        $helperArgs += @("--redact", [string]$redaction)
    }

    $previousErrorAction = $ErrorActionPreference
    try {
        # Native stderr is a PowerShell ErrorRecord in Windows PowerShell 5.1.
        # Continue lets us capture the complete diagnostic and inspect exit code.
        $ErrorActionPreference = "Continue"
        $helperOutput = @(& $PythonCommand @helperArgs 2>&1 | ForEach-Object { [string]$_ })
        $helperExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }

    if ($helperExitCode -ne 0) {
        $detail = ($helperOutput -join " ").Trim()
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = "helper exited with code $helperExitCode" }
        throw "BRAINSTORM_ANONYMIZATION_FAILED: $detail"
    }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
        throw "BRAINSTORM_ANONYMIZATION_FAILED: helper did not create output"
    }
}

function Get-BrainstormRunStatus {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [string]$Failure,
        [AllowNull()]
        [string]$FailureKind
    )

    if ([string]::IsNullOrWhiteSpace($Failure)) { return "SUCCESS" }
    if ($FailureKind -eq "insufficient_responses") { return "INSUFFICIENT_RESPONSES" }
    return "INTERNAL_ERROR"
}

function Get-BrainstormHealthDisposition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Success,
        [AllowNull()]
        [string]$ErrorKind,
        [int]$OutputTokens = 0,
        [int]$RequestedMaxTokens = 0
    )

    if ($Success) { return "success" }
    if (
        $ErrorKind -eq "empty" -and
        $RequestedMaxTokens -gt 0 -and
        $OutputTokens -ge $RequestedMaxTokens
    ) {
        return "ignore-budget-exhausted"
    }
    return "failure"
}
