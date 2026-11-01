$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "brainstorm_transport.ps1")

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION_FAILED: $Message" }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("rada-brainstorm-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot | Out-Null

try {
    $first = Join-Path $testRoot "round1-first.md"
    $second = Join-Path $testRoot "round1-second.md"
    [IO.File]::WriteAllText($first, "ModelA proposes a baseline.", [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($second, "ModelB proposes an ablation.", [Text.Encoding]::UTF8)

    $inputPath = Join-Path $testRoot "input.json"
    $outputPath = Join-Path $testRoot "output.json"
    $evidence = @(
        [pscustomobject]@{ plik = $first },
        [pscustomobject]@{ plik = $second }
    )
    Write-BrainstormAnonymizerInput -Evidence $evidence -Path $inputPath

    $parsedRows = [IO.File]::ReadAllText($inputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $rows = @($parsedRows)
    Assert-True ($rows.Count -eq 2) "transport keeps every healthy response"
    Assert-True ($rows[0].content -is [string]) "content is a JSON string under this PowerShell runtime"
    Assert-True ($rows[0].content -eq "ModelA proposes a baseline.") "transport keeps response text"
    Assert-True ((Get-Item -LiteralPath $inputPath).Length -lt 1024) "transport does not serialize PowerShell file metadata"

    Invoke-BrainstormAnonymizer `
        -PythonCommand "python" `
        -ScriptPath (Join-Path $PSScriptRoot "brainstorm_protocol.py") `
        -InputPath $inputPath `
        -OutputPath $outputPath `
        -Redactions @("ModelA", "ModelB")

    $bundle = ([IO.File]::ReadAllText($outputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json).bundle
    Assert-True ($bundle -like "*POMYSL 01*") "helper emits the first anonymous idea"
    Assert-True ($bundle -like "*POMYSL 02*") "helper emits the second anonymous idea"
    Assert-True ($bundle -notlike "*ModelA*") "helper redacts model identity"
    Assert-True ((Get-BrainstormRunStatus -Failure $null -FailureKind $null) -eq "SUCCESS") "clean run status is truthful"
    Assert-True ((Get-BrainstormRunStatus -Failure "not enough" -FailureKind "insufficient_responses") -eq "INSUFFICIENT_RESPONSES") "capacity failure is classified separately"
    Assert-True ((Get-BrainstormRunStatus -Failure "helper failed" -FailureKind "internal_error") -eq "INTERNAL_ERROR") "internal helper failure is not mislabeled as insufficient responses"
    Assert-True ((Get-BrainstormHealthDisposition -Success $true -ErrorKind $null -OutputTokens 10 -RequestedMaxTokens 2048) -eq "success") "successful response resets model health"
    Assert-True ((Get-BrainstormHealthDisposition -Success $false -ErrorKind "empty" -OutputTokens 2048 -RequestedMaxTokens 2048) -eq "ignore-budget-exhausted") "reasoning budget exhaustion does not poison provider health"
    Assert-True ((Get-BrainstormHealthDisposition -Success $false -ErrorKind "empty" -OutputTokens 100 -RequestedMaxTokens 2048) -eq "failure") "an unexplained empty response still counts as a model failure"
    Assert-True ((Get-BrainstormHealthDisposition -Success $false -ErrorKind "hard" -OutputTokens 0 -RequestedMaxTokens 2048) -eq "failure") "hard provider errors still count as failures"

    $invalidInput = Join-Path $testRoot "invalid.json"
    [IO.File]::WriteAllText($invalidInput, '[{"content":{"value":"not-a-string"}}]', [Text.Encoding]::UTF8)
    try {
        Invoke-BrainstormAnonymizer `
            -PythonCommand "python" `
            -ScriptPath (Join-Path $PSScriptRoot "brainstorm_protocol.py") `
            -InputPath $invalidInput `
            -OutputPath (Join-Path $testRoot "invalid-output.json")
        throw "ASSERTION_FAILED: malformed transport was accepted"
    } catch {
        Assert-True ($_.Exception.Message -like "*BRAINSTORM_EMPTY_RESPONSE: 1*") "helper failure keeps the useful diagnostic"
        Assert-True ($_.Exception.Message -notlike "*Traceback*") "known helper failure does not collapse to a traceback"
    }

    "brainstorm transport tests passed"
} finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if ($resolvedTestRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
